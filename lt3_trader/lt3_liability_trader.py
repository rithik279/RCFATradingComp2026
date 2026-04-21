"""LT3 Liability Trading Case (RIT) — Tender-only liability trader.

This script is designed to comply with the LT3 behavioral contract:
- NO trading unless a tender has been accepted.
- NO front-running / NO pre-hedging.
- NO market making behavior.
- Only unwind accepted tender inventory.
- Flatten before end of simulation.

API contract compliance:
- Uses Client REST API: http://localhost:9999/v1
- Includes X-API-Key header.
- Uses query parameters ONLY for trading endpoints (NO JSON bodies).
- Handles HTTP 401 and HTTP 429 (Retry-After).

How to run:
    python lt3_liability_trader.py
"""

from __future__ import annotations

import re
import signal
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from lt3_market_utils import ApiException, RateLimitException, depth_view


# =====================
# Config
# =====================

CONFIG: Dict[str, Any] = {
    "base_url": "http://localhost:9999/v1",
    "api_key": "RotmanTrading",

    # Share-based hard caps
    "max_gross_position": 250_000,  # shares
    "max_net_position": 100_000,  # shares

    # DEBUG / one-run override: allow accepting when book depth < tender size.
    # This relaxes the liquidity gate ONLY; all other tender-only constraints remain.
    "allow_partial_liquidity_accept": True,
    "partial_liquidity_min_ratio": 0.40,  # accept only if available depth >= ratio * tender_qty
    "partial_liquidity_min_shares": 30_000,  # and depth >= this many shares

    # Tender acceptance parameters (non-speculative)
    "book_levels": 10,
    "minimum_profit_threshold_per_share": 0.001,  # accept only if net edge >= this (0.10 cents)
    "slippage_buffer_per_share": 0.01,  # conservative buffer to avoid marginal accepts

    # Trailing exit parameters (reactive, non-predictive)
    "trail_distance_per_share": 0.02,

    # Execution parameters
    "prefer_market_orders": True,

    # End-of-simulation safety
    "force_flatten_ticks_remaining": 5,

    # Rate-limit handling
    "max_retries": 5,
    "retry_backoff_sec": 0.25,
}


# =====================
# Data structures
# =====================


@dataclass(frozen=True)
class Tender:
    tender_id: int
    ticker: str
    action: str  # "BUY" or "SELL" from institution's perspective
    quantity: int
    price: float
    is_fixed_bid: bool
    expires_in_seconds: Optional[float]


@dataclass
class AcceptedPosition:
    ticker: str
    # positive = long, negative = short
    remaining_shares: int
    accepted_price: float
    accepted_at: float


@dataclass
class TenderState:
    tender: "Tender"
    first_seen_tick: int
    last_seen_tick: int
    last_logged_decision: Optional[bool] = None
    last_logged_reason: str = ""


# =====================
# API / HTTP helpers
# =====================


shutdown = False
_warned_missing_max_position = False


def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


def _url(path: str) -> str:
    base = CONFIG["base_url"].rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def api_request(session: requests.Session, method: str, path: str, *, params: Optional[dict] = None) -> Any:
    """Request wrapper that follows the API contract.

    - Session must include X-API-Key.
    - Trading endpoints use query parameters only.
    - Sleeps ONLY for rate-limit compliance (HTTP 429 Retry-After).
    """

    retries = 0
    while True:
        try:
            resp = session.request(
                method=method,
                url=_url(path),
                params=params,
                timeout=2.5,
            )
        except requests.RequestException as exc:
            retries += 1
            if retries > int(CONFIG["max_retries"]):
                raise
            # Backoff is for resilience; not used for strategy timing.
            backoff = float(CONFIG["retry_backoff_sec"]) * retries
            time.sleep(backoff)
            continue

        if resp.status_code == 401:
            raise RuntimeError(
                "HTTP 401 Unauthorized: API key mismatch. Ensure X-API-Key matches the RIT client key."
            )

        if resp.status_code == 429:
            retries += 1
            if retries > int(CONFIG["max_retries"]):
                raise RuntimeError("HTTP 429 Rate limited too often")
            retry_after = resp.headers.get("Retry-After")
            sleep_s = float(retry_after) if retry_after else float(CONFIG["retry_backoff_sec"]) * retries
            time.sleep(sleep_s)
            continue

        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"HTTP {resp.status_code} for {method} {path}: {resp.text}")

        return resp.json()


# =====================
# RIT-specific helpers
# =====================


def get_case(session: requests.Session) -> Dict[str, Any]:
    return api_request(session, "GET", "/case")


def get_tenders(session: requests.Session) -> List[Dict[str, Any]]:
    data = api_request(session, "GET", "/tenders")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "tenders" in data:
        return data["tenders"]
    return []


def accept_tender(session: requests.Session, tender_id: int, *, price: Optional[float] = None) -> Any:
    # Price query parameter is required only for non-fixed-bid tenders.
    params = {"price": float(price)} if price is not None else None
    return api_request(session, "POST", f"/tenders/{tender_id}", params=params)


def get_security(session: requests.Session, ticker: str) -> Dict[str, Any]:
    data = api_request(session, "GET", "/securities", params={"ticker": ticker})
    if isinstance(data, list):
        if len(data) == 0:
            raise RuntimeError(f"No security data for {ticker}")
        return data[0]
    return data


def get_securities(session: requests.Session) -> List[Dict[str, Any]]:
    data = api_request(session, "GET", "/securities")
    return data if isinstance(data, list) else []


def get_book(session: requests.Session, ticker: str) -> Dict[str, Any]:
    # Book naming can vary (bid/ask vs bids/asks). We fetch raw here for execution pricing.
    return api_request(session, "GET", "/securities/book", params={"ticker": ticker})


def cancel_all_orders(session: requests.Session) -> None:
    # Contract: POST /commands/cancel must include exactly one query parameter.
    try:
        api_request(session, "POST", "/commands/cancel", params={"all": 1})
    except Exception as exc:
        print(f"[WARN] cancel_all_orders failed: {exc}")


def post_order(
    session: requests.Session,
    *,
    ticker: str,
    action: str,  # "BUY" or "SELL" from our perspective
    quantity: int,
    order_type: str,  # "MARKET" or "LIMIT"
    price: Optional[float] = None,
) -> Any:
    # Contract: NO JSON body. All parameters are query params.
    params: Dict[str, Any] = {
        "ticker": ticker,
        "type": order_type,
        "quantity": int(quantity),
        "action": action,
    }
    if order_type == "LIMIT":
        if price is None:
            raise ValueError("LIMIT order requires price")
        params["price"] = float(price)
    return api_request(session, "POST", "/orders", params=params)


def get_orders(session: requests.Session, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {"status": status} if status is not None else None
    data = api_request(session, "GET", "/orders", params=params)
    return data if isinstance(data, list) else []


def get_order(session: requests.Session, order_id: int) -> Dict[str, Any]:
    return api_request(session, "GET", f"/orders/{order_id}")


def cancel_order(session: requests.Session, order_id: int) -> Any:
    return api_request(session, "DELETE", f"/orders/{order_id}")


# =====================
# Tender evaluation
# =====================


def parse_tender(raw: Dict[str, Any]) -> Optional[Tender]:
    # API contract: ticker is not a structured tender field; must parse from caption.
    tender_id = raw.get("tender_id", raw.get("id"))
    action = raw.get("action")
    quantity = raw.get("quantity")
    price = raw.get("price")
    caption = str(raw.get("caption", "") or "")

    if tender_id is None or action is None or quantity is None or price is None:
        return None

    # expires is typically a tick/time in the tender object; store raw numeric if present.
    expires = raw.get("expires")

    is_fixed_bid = bool(raw.get("is_fixed_bid", True))

    # Placeholder ticker until replaced by parse_ticker_from_caption().
    # (We intentionally keep parsing centralized.)
    ticker = ""

    return Tender(
        tender_id=int(tender_id),
        ticker=ticker,
        action=str(action).upper(),
        quantity=int(quantity),
        price=float(price),
        is_fixed_bid=is_fixed_bid,
        expires_in_seconds=float(expires) if expires is not None else None,
    )


def shares_limits_ok_for_tender(
    session: requests.Session,
    *,
    ticker: str,
    delta_shares: int,
    current_ticker_position: int,
) -> bool:
    """Enforce additional share-based max gross/net caps.

    Gross (shares) = sum of absolute positions across all securities.
    Net (shares) = sum of signed positions across all securities.
    """

    max_gross = int(CONFIG.get("max_gross_position", 0) or 0)
    max_net = int(CONFIG.get("max_net_position", 0) or 0)
    if max_gross <= 0 and max_net <= 0:
        return True

    secs = get_securities(session)
    pos_by_ticker: Dict[str, int] = {}
    for s in secs:
        tk = s.get("ticker")
        if not tk:
            continue
        try:
            pos_by_ticker[str(tk)] = int(s.get("position", 0) or 0)
        except Exception:
            pos_by_ticker[str(tk)] = 0

    # Prefer the just-fetched per-ticker position for consistency.
    pos_by_ticker[str(ticker)] = int(current_ticker_position)

    current_gross = sum(abs(p) for p in pos_by_ticker.values())
    current_net = sum(p for p in pos_by_ticker.values())

    cur = int(pos_by_ticker.get(str(ticker), 0))
    new = cur + int(delta_shares)
    projected_gross = current_gross - abs(cur) + abs(new)
    projected_net = current_net + int(delta_shares)

    if max_gross > 0 and projected_gross > max_gross:
        return False
    if max_net > 0 and abs(projected_net) > max_net:
        return False
    return True


def unwind_side_for_tender(t: Tender) -> Tuple[str, str]:
    """Returns (our_position_sign, unwind_action).

    If institution SELLS to us => we become LONG => unwind by SELLing.
    If institution BUYS from us => we become SHORT => unwind by BUYing.
    """
    if t.action == "SELL":
        return ("LONG", "SELL")
    if t.action == "BUY":
        return ("SHORT", "BUY")
    raise ValueError(f"Unknown tender action {t.action}")


def parse_ticker_from_caption(caption: str, known_tickers: List[str]) -> Optional[str]:
    # Prefer matching known tickers as whole words.
    for tk in known_tickers:
        if re.search(rf"\b{re.escape(tk)}\b", caption):
            return tk

    # Fallback: best-effort for uppercase tokens (still deterministic).
    m = re.search(r"\b[A-Z]{2,6}\b", caption)
    return m.group(0) if m else None


def portfolio_limit_ok(security: Dict[str, Any], tender_delta: int) -> bool:
    # RIT commonly provides: position, max_position
    pos = int(security.get("position", 0))
    max_pos = security.get("max_position")
    if max_pos is None:
        # If not provided, allow but warn once (avoid log spam).
        global _warned_missing_max_position
        if not _warned_missing_max_position:
            print("[WARN] max_position not provided; skipping per-security limit check")
            _warned_missing_max_position = True
        return True

    max_pos = int(max_pos)
    new_pos = pos + tender_delta
    return abs(new_pos) <= abs(max_pos)


def cumulative_vwap_for_qty(
    session: requests.Session,
    *,
    ticker: str,
    side: str,
    quantity: int,
    allow_fallback: bool = False,
) -> Tuple[Optional[float], int]:
    """Use decision-support depth_view() to obtain cumulative VWAP and volume.

    Acceptance rule:
    - Choose the first depth level where cumulative_vol >= quantity
    - Use that level's cumulative_vwap as the liquidation VWAP
    """

    books = depth_view(
        session,
        tickers=[ticker],
        base_url=CONFIG["base_url"],
        limit_levels=CONFIG["book_levels"],
    )
    book = books.get(ticker, {})
    levels = book.get(side, [])
    if not levels:
        return (None, 0)

    for lvl in levels:
        if int(lvl.get("cumulative_vol", 0) or 0) >= int(quantity):
            return (float(lvl.get("cumulative_vwap", 0.0) or 0.0), int(lvl.get("cumulative_vol", 0) or 0))

    used = int(levels[-1].get("cumulative_vol", 0) or 0)
    if allow_fallback:
        return (float(levels[-1].get("cumulative_vwap", 0.0) or 0.0), used)
    return (None, used)


def evaluate_tender(session: requests.Session, t: Tender) -> Tuple[bool, str, Dict[str, Any]]:
    """Return (accept?, reason, diagnostics)."""

    pos_type, unwind_action = unwind_side_for_tender(t)

    if not t.ticker:
        return (False, "Ticker not parsed", {})

    security = get_security(session, t.ticker)
    if not bool(security.get("is_tradeable", True)):
        return (False, "Security not tradeable", {})

    # Determine unwind side + exposure delta
    if unwind_action == "SELL":
        side = "bids"
        tender_delta_shares = +t.quantity
    else:
        side = "asks"
        tender_delta_shares = -t.quantity

    # Enforce portfolio limits BEFORE acceptance to avoid fines.
    if not portfolio_limit_ok(security, tender_delta_shares):
        return (False, "Per-security position limit would be breached", {})

    # Additional share-based caps (gross/net) before acceptance.
    if not shares_limits_ok_for_tender(
        session,
        ticker=t.ticker,
        delta_shares=tender_delta_shares,
        current_ticker_position=int(security.get("position", 0) or 0),
    ):
        return (False, "Share-based gross/net cap would be breached", {})

    try:
        vwap, used = cumulative_vwap_for_qty(
            session,
            ticker=t.ticker,
            side=side,
            quantity=t.quantity,
            allow_fallback=bool(CONFIG.get("allow_partial_liquidity_accept", False)),
        )
    except RateLimitException as exc:
        # This is not strategy timing; it is mandatory rate-limit compliance.
        time.sleep(float(exc.retry_after) if exc.retry_after is not None else float(CONFIG["retry_backoff_sec"]))
        return (False, "Rate limited during VWAP check", {})
    except ApiException as exc:
        return (False, f"VWAP check failed: {exc}", {})

    if vwap is None:
        return (
            False,
            f"Insufficient liquidity on unwind side (only {used}/{t.quantity})",
            {"pos_type": pos_type, "unwind_action": unwind_action, "used": used},
        )

    # If we used a fallback VWAP (because displayed depth < tender size), enforce explicit thresholds.
    if int(used) < int(t.quantity):
        min_ratio = float(CONFIG.get("partial_liquidity_min_ratio", 1.0))
        min_shares = int(CONFIG.get("partial_liquidity_min_shares", 0) or 0)
        ratio = (float(used) / float(t.quantity)) if int(t.quantity) > 0 else 0.0
        if ratio < min_ratio or int(used) < min_shares:
            return (
                False,
                f"Insufficient liquidity on unwind side (only {used}/{t.quantity}; ratio={ratio:.2f} < {min_ratio:.2f})",
                {"pos_type": pos_type, "unwind_action": unwind_action, "used": used, "ratio": ratio},
            )

    # Edge per share based on immediate unwind VWAP vs tender price
    if pos_type == "LONG":
        edge = vwap - t.price
    else:
        edge = t.price - vwap

    trading_fee = float(security.get("trading_fee", 0.0))
    slippage = float(CONFIG["slippage_buffer_per_share"])
    net_edge = edge - trading_fee - slippage

    if net_edge <= float(CONFIG["minimum_profit_threshold_per_share"]):
        return (
            False,
            f"Net edge too small ({net_edge:.4f} <= threshold)",
            {"pos_type": pos_type, "unwind_action": unwind_action, "vwap": vwap, "edge": edge, "net_edge": net_edge},
        )

    return (
        True,
        "ACCEPT: net edge positive and liquid",
        {"pos_type": pos_type, "unwind_action": unwind_action, "vwap": vwap, "edge": edge, "net_edge": net_edge},
    )


# =====================
# Unwind execution
# =====================


def best_price(book: Dict[str, Any], side: str) -> Optional[float]:
    # Defensive handling: some clients use bid/ask keys.
    levels = book.get(side)
    if levels is None:
        if side == "bids":
            levels = book.get("bid", [])
        elif side == "asks":
            levels = book.get("ask", [])
        else:
            levels = []
    if not levels:
        return None
    p = levels[0].get("price")
    return float(p) if p is not None else None


def wait_for_order_done(session: requests.Session, order_id: int, *, max_checks: int = 50) -> Dict[str, Any]:
    """Poll /orders/{id} until the order is no longer OPEN, or checks exhausted.

    No blind sleeping; relies on request latency and rate-limit backoff.
    """
    last: Dict[str, Any] = {}
    for _ in range(max_checks):
        last = get_order(session, order_id)
        status = str(last.get("status", "")).upper()
        if status and status != "OPEN":
            return last
    return last


def market_flatten(session: requests.Session, ticker: str) -> None:
    """Flatten a single ticker immediately using MARKET orders.

    Allowed because it only reduces exposure.
    """

    sec = get_security(session, ticker)
    pos = int(sec.get("position", 0))
    if pos == 0:
        return

    max_trade = int(sec.get("max_trade_size", abs(pos)) or abs(pos))
    if max_trade <= 0:
        max_trade = abs(pos)

    action = "SELL" if pos > 0 else "BUY"
    remaining = abs(pos)

    while remaining > 0:
        slice_qty = remaining if remaining <= max_trade else max_trade
        resp = post_order(session, ticker=ticker, action=action, quantity=slice_qty, order_type="MARKET")
        order_id = resp.get("order_id", resp.get("id"))
        if order_id is not None:
            wait_for_order_done(session, int(order_id))
        sec = get_security(session, ticker)
        pos = int(sec.get("position", 0))
        remaining = abs(pos)


def unwind_position(session: requests.Session, pos: AcceptedPosition) -> None:
    """Flatten pos.remaining_shares ASAP using sliced MARKET orders.

    Allowed because it only reduces exposure created by an accepted tender.
    """

    if pos.remaining_shares == 0:
        return

    ticker = pos.ticker
    security = get_security(session, ticker)
    max_trade = int(security.get("max_trade_size", abs(pos.remaining_shares)))
    if max_trade <= 0:
        max_trade = abs(pos.remaining_shares)

    action = "SELL" if pos.remaining_shares > 0 else "BUY"
    remaining = abs(pos.remaining_shares)

    print(f"[UNWIND] {ticker} remaining={pos.remaining_shares} action={action}")

    while remaining > 0:
        slice_qty = remaining if remaining <= max_trade else max_trade

        try:
            resp = post_order(session, ticker=ticker, action=action, quantity=slice_qty, order_type="MARKET")
            order_id = resp.get("order_id", resp.get("id"))
            print(f"[ORDER] {ticker} {action} qty={slice_qty} type=MARKET id={order_id}")
            if order_id is not None:
                wait_for_order_done(session, int(order_id))
        except Exception as exc:
            print(f"[WARN] Order failed: {exc}")
            continue

        # Update remaining by checking actual position
        sec = get_security(session, ticker)
        current_pos = int(sec.get("position", 0))
        if pos.remaining_shares > 0:
            remaining = max(0, current_pos)  # still long
            pos.remaining_shares = current_pos
        else:
            remaining = max(0, -current_pos)  # still short
            pos.remaining_shares = current_pos

        # If position moved to/through flat, stop
        if current_pos == 0:
            print(f"[UNWIND] {ticker} flat")
            return


def trailing_exit_unwind(
    session: requests.Session,
    *,
    ticker: str,
    tender_price: float,
    unwind_action: str,
    tender_qty: int,
) -> None:
    """Reactive trailing exit after tender acceptance.

    - Never increases exposure.
    - Never assumes future direction.
    - Exits immediately when the best-achievable liquidation VWAP reverses by trail_distance.
    """

    # Determine liquidation VWAP side for the *remaining* size.
    vwap_side = "bids" if unwind_action == "SELL" else "asks"
    trail = float(CONFIG["trail_distance_per_share"])
    min_profit = float(CONFIG["minimum_profit_threshold_per_share"])

    best_vwap: Optional[float] = None
    trail_stop: Optional[float] = None

    print(f"[TRAIL] start ticker={ticker} action={unwind_action} qty={tender_qty}")

    while True:
        case = get_case(session)
        ticks_remaining = case.get("ticks_remaining")
        if ticks_remaining is not None and int(ticks_remaining) <= int(CONFIG["force_flatten_ticks_remaining"]):
            print(f"[TRAIL] forced close ticker={ticker} ticks_remaining={ticks_remaining}")
            cancel_all_orders(session)
            market_flatten(session, ticker)
            return

        sec = get_security(session, ticker)
        pos = int(sec.get("position", 0))
        if pos == 0:
            print(f"[TRAIL] flat ticker={ticker}")
            return

        remaining_qty = abs(pos)

        try:
            current_vwap, used = cumulative_vwap_for_qty(session, ticker=ticker, side=vwap_side, quantity=remaining_qty)
        except RateLimitException as exc:
            time.sleep(float(exc.retry_after) if exc.retry_after is not None else float(CONFIG["retry_backoff_sec"]))
            continue
        except ApiException:
            # If we cannot compute VWAP reliably, exit to reduce risk.
            print(f"[TRAIL] VWAP unavailable -> exit ticker={ticker}")
            cancel_all_orders(session)
            market_flatten(session, ticker)
            return

        if current_vwap is None:
            print(f"[TRAIL] insufficient liquidity -> exit ticker={ticker} used={used}/{remaining_qty}")
            cancel_all_orders(session)
            market_flatten(session, ticker)
            return

        # Compute net edge for logging and for a safe minimum-profit gate.
        trading_fee = float(sec.get("trading_fee", 0.0))
        slippage = float(CONFIG["slippage_buffer_per_share"])

        if unwind_action == "SELL":
            edge = float(current_vwap) - float(tender_price)
            net_edge = edge - trading_fee - slippage
            if best_vwap is None or float(current_vwap) > best_vwap:
                best_vwap = float(current_vwap)
                trail_stop = best_vwap - trail
                print(f"[TRAIL] best_vwap={best_vwap:.4f} trail_stop={trail_stop:.4f} net_edge={net_edge:.4f}")

            # If we have at least minimum profit and price reverses by trail_distance, exit.
            if trail_stop is not None and net_edge >= min_profit and float(current_vwap) <= trail_stop:
                print(f"[TRAIL] stop hit -> exit ticker={ticker} vwap={current_vwap:.4f} stop={trail_stop:.4f}")
                cancel_all_orders(session)
                market_flatten(session, ticker)
                return
        else:
            edge = float(tender_price) - float(current_vwap)
            net_edge = edge - trading_fee - slippage
            if best_vwap is None or float(current_vwap) < best_vwap:
                best_vwap = float(current_vwap)
                trail_stop = best_vwap + trail
                print(f"[TRAIL] best_vwap={best_vwap:.4f} trail_stop={trail_stop:.4f} net_edge={net_edge:.4f}")

            if trail_stop is not None and net_edge >= min_profit and float(current_vwap) >= trail_stop:
                print(f"[TRAIL] stop hit -> exit ticker={ticker} vwap={current_vwap:.4f} stop={trail_stop:.4f}")
                cancel_all_orders(session)
                market_flatten(session, ticker)
                return


def force_flatten_all(session: requests.Session, tickers: List[str]) -> None:
    """Emergency flatten: cancel orders, then market/marketable-limit to flat."""

    cancel_all_orders(session)
    for ticker in tickers:
        market_flatten(session, ticker)


# =====================
# Main loop
# =====================


def main() -> None:
    session = requests.Session()
    session.headers.update({"X-API-Key": CONFIG["api_key"]})

    accepted: Dict[str, AcceptedPosition] = {}
    tender_states: Dict[int, TenderState] = {}

    # Build ticker universe for parsing tender captions.
    securities = get_securities(session)
    known_tickers = [str(s.get("ticker")) for s in securities if s.get("ticker")]

    print("[START] LT3 tender-only liability trader")

    while not shutdown:
        case = get_case(session)
        status = str(case.get("status", "")).upper()
        ticks_remaining = case.get("ticks_remaining")
        tick = int(case.get("tick", 0) or 0)

        if status and status not in ("ACTIVE", "RUNNING"):
            print(f"[CASE] status={status}; stopping")
            break

        if ticks_remaining is not None and int(ticks_remaining) <= int(CONFIG["force_flatten_ticks_remaining"]):
            print(f"[RISK] ticks_remaining={ticks_remaining} -> force flatten")
            force_flatten_all(session, list(known_tickers))
            break

        # If we have accepted exposure, we do not look at new tenders.
        # This preserves tender-only sequencing and debuggability.
        if accepted:
            for ticker, pos in list(accepted.items()):
                # Trailing exit loop is the only post-acceptance logic.
                unwind_action = "SELL" if pos.remaining_shares > 0 else "BUY"
                trailing_exit_unwind(
                    session,
                    ticker=ticker,
                    tender_price=pos.accepted_price,
                    unwind_action=unwind_action,
                    tender_qty=abs(pos.remaining_shares),
                )
                accepted.pop(ticker, None)
            continue

        # Flat state: poll tenders and reassess within their window.
        tenders_raw = get_tenders(session)
        seen_ids: set[int] = set()
        for raw in tenders_raw:
            t0 = parse_tender(raw)
            if t0 is None:
                continue
            caption = str(raw.get("caption", "") or "")
            ticker = parse_ticker_from_caption(caption, known_tickers)
            if ticker is None:
                continue
            t = Tender(
                tender_id=t0.tender_id,
                ticker=ticker,
                action=t0.action,
                quantity=t0.quantity,
                price=t0.price,
                is_fixed_bid=t0.is_fixed_bid,
                expires_in_seconds=t0.expires_in_seconds,
            )

            seen_ids.add(t.tender_id)
            if t.tender_id not in tender_states:
                tender_states[t.tender_id] = TenderState(tender=t, first_seen_tick=tick, last_seen_tick=tick)
            else:
                tender_states[t.tender_id].tender = t
                tender_states[t.tender_id].last_seen_tick = tick

        # Remove stale tenders (expired or no longer returned).
        for tid in list(tender_states.keys()):
            if tid not in seen_ids:
                tender_states.pop(tid, None)

        # Evaluate tenders deterministically in id order.
        for tid in sorted(tender_states.keys()):
            state = tender_states[tid]
            t = state.tender

            ok, reason, _diag = evaluate_tender(session, t)
            if state.last_logged_decision != ok or state.last_logged_reason != reason:
                print(
                    f"[TENDER] tick={tick} id={t.tender_id} ticker={t.ticker} action={t.action} qty={t.quantity} px={t.price:.2f} -> {ok} ({reason})"
                )
                state.last_logged_decision = ok
                state.last_logged_reason = reason

            if not ok:
                continue

            # ACCEPT tender, then immediately start trailing unwind monitoring.
            # Allowed because acceptance precedes any market trading.
            try:
                accept_tender(session, t.tender_id, price=None if t.is_fixed_bid else t.price)
            except Exception as exc:
                print(f"[WARN] Accept failed id={t.tender_id}: {exc}")
                continue

            pos_type, _ = unwind_side_for_tender(t)
            remaining = +t.quantity if pos_type == "LONG" else -t.quantity
            accepted[t.ticker] = AcceptedPosition(
                ticker=t.ticker,
                remaining_shares=remaining,
                accepted_price=t.price,
                accepted_at=time.time(),
            )
            print(f"[ACCEPT] tick={tick} id={t.tender_id} ticker={t.ticker} position={remaining}")
            break


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
