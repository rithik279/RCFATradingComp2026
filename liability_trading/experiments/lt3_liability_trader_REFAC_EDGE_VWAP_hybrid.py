"""LT3 Liability Trading Case (RIT) — Tender-only liability trader.

STRICT BEHAVIORAL CONSTRAINTS (enforced by design):
- No trading unless a tender has been accepted.
- No front-running / pre-hedging.
- No market making (never post both bid and ask; we only unwind one direction).
- Only trades used to flatten an accepted tender position.

How to run:
  python lt3_liability_trader.py

Config knobs are in CONFIG below.

Assumptions about RIT endpoints (standard RIT REST):
- GET  /case
- GET  /tenders
- POST /tenders/{id}
- GET  /securities?ticker=XYZ
- GET  /securities/book?ticker=XYZ
- GET  /orders (optional; used to cancel)
- POST /orders
- POST /commands/cancel

This script is intentionally minimal and competition-safe.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


# =====================
# Config
# =====================

CONFIG: Dict[str, Any] = {
    "base_url": "http://localhost:9999/v1",
    "api_key": "RotmanTrading",

    # Polling cadence (seconds)
    "poll_tenders_sec": 0.25,
    "poll_case_sec": 0.5,

    # Tender acceptance parameters
    "book_levels": 5,  # use first 5 price levels for VWAP

    "acceptance_book_levels": 5,

    "minimum_profit_threshold_per_share": 0.01,  # accept only if edge > this after costs
    "slippage_buffer_per_share": 0.01,  # conservative buffer to avoid marginal accepts

    # Execution parameters
    "use_marketable_limits": True,
    "marketable_limit_extra_ticks": 2,  # how far through top-of-book to cross
    "cancel_all_before_new_order": True,

    # End-of-simulation safety
    "force_flatten_ticks_remaining": 25,

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
    expires_in_seconds: Optional[float]


@dataclass
class AcceptedPosition:
    ticker: str
    # positive = long, negative = short
    remaining_shares: int
    accepted_price: float
    accepted_at: float


# =====================
# API / HTTP helpers
# =====================


def _headers() -> Dict[str, str]:
    return {
        "X-API-Key": CONFIG["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _url(path: str) -> str:
    base = CONFIG["base_url"].rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def api_request(session: requests.Session, method: str, path: str, *, params: Optional[dict] = None, json_body: Any = None) -> Any:
    """Request wrapper with basic 401/429 handling and bounded retries."""

    retries = 0
    while True:
        try:
            resp = session.request(
                method=method,
                url=_url(path),
                headers=_headers(),
                params=params,
                json=json_body,
                timeout=2.5,
            )
        except requests.RequestException as exc:
            retries += 1
            if retries > CONFIG["max_retries"]:
                raise
            sleep_s = CONFIG["retry_backoff_sec"] * retries
            print(f"[WARN] HTTP error ({exc}); retrying in {sleep_s:.2f}s")
            time.sleep(sleep_s)
            continue

        # Auth errors should stop trading
        if resp.status_code == 401:
            raise RuntimeError("HTTP 401 Unauthorized: check X-API-Key")

        # Rate limit: respect Retry-After if present
        if resp.status_code == 429:
            retries += 1
            if retries > CONFIG["max_retries"]:
                raise RuntimeError("HTTP 429 Rate limited too often")
            retry_after = resp.headers.get("Retry-After")
            sleep_s = float(retry_after) if retry_after else (CONFIG["retry_backoff_sec"] * retries)
            print(f"[WARN] HTTP 429 rate limit; sleeping {sleep_s:.2f}s")
            time.sleep(sleep_s)
            continue

        # Other non-2xx
        if not (200 <= resp.status_code < 300):
            msg = resp.text
            raise RuntimeError(f"HTTP {resp.status_code} for {method} {path}: {msg}")

        if resp.content is None or len(resp.content) == 0:
            return None

        # Some RIT endpoints return JSON objects or arrays
        try:
            return resp.json()
        except ValueError:
            return resp.text


# =====================
# RIT-specific helpers
# =====================


def get_case(session: requests.Session) -> Dict[str, Any]:
    return api_request(session, "GET", "/case")


def get_tenders(session: requests.Session) -> List[Dict[str, Any]]:
    data = api_request(session, "GET", "/tenders")
    if isinstance(data, list):
        return data
    # Some versions wrap in {"tenders": [...]}
    if isinstance(data, dict) and "tenders" in data:
        return data["tenders"]
    return []


def accept_tender(session: requests.Session, tender_id: int) -> Any:
    return api_request(session, "POST", f"/tenders/{tender_id}")


def get_security(session: requests.Session, ticker: str) -> Dict[str, Any]:
    data = api_request(session, "GET", "/securities", params={"ticker": ticker})
    if isinstance(data, list):
        if len(data) == 0:
            raise RuntimeError(f"No security data for {ticker}")
        return data[0]
    return data


def get_book(session: requests.Session, ticker: str) -> Dict[str, Any]:
    return api_request(session, "GET", "/securities/book", params={"ticker": ticker})


def get_all_positions(session: requests.Session) -> Dict[str, int]:
    """Fetch positions for all tickers.

    Used only for end-of-simulation safety flattening.
    """
    data = api_request(session, "GET", "/securities")
    positions: Dict[str, int] = {}
    if isinstance(data, list):
        for sec in data:
            ticker = sec.get("ticker")
            if not ticker:
                continue
            positions[str(ticker)] = int(sec.get("position", 0))
    return positions




# --------------------------
# Tender entry logic (ported from liability_trader.py)
# Goal: accept only when expected unwind VWAP (using visible depth) clears tender price
# after fees, and when orderbook depth is sufficient to unwind without fines.
# --------------------------

from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

@dataclass(frozen=True)
class TenderEntryConfig:
    # Minimum per-share net edge required after fees (dollars/share).
    min_net_edge: float = 0.00
    # Require this fraction of tender quantity to be unwindable from visible depth.
    min_unwind_fill_ratio: float = 0.95
    # Reject if spread is wider than this (dollars). Set None to disable.
    max_spread: Optional[float] = None
    # Per-share transaction fee assumption for marketable orders (dollars/share).
    transaction_fee: float = 0.02

ENTRY_CFG = TenderEntryConfig()

def _vwap_from_levels(levels: list, qty: int) -> Tuple[Optional[float], int]:
    """Compute VWAP using price/quantity levels until qty is filled.
    Returns (vwap, filled_qty)."""
    remaining = qty
    notional = 0.0
    filled = 0
    for lvl in levels:
        # levels are expected like [{"price": 9.94, "quantity": 5000}, ...]
        p = float(lvl.get("price"))
        q = int(lvl.get("quantity"))
        if q <= 0:
            continue
        take = q if q <= remaining else remaining
        notional += p * take
        filled += take
        remaining -= take
        if remaining == 0:
            break
    if filled == 0:
        return None, 0
    return notional / filled, filled

def _top_of_book(book: dict) -> Tuple[Optional[float], Optional[float]]:
    bids = book.get("bids", []) if isinstance(book, dict) else []
    asks = book.get("asks", []) if isinstance(book, dict) else []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    return best_bid, best_ask

def _spread_ok(book: dict) -> bool:
    if ENTRY_CFG.max_spread is None:
        return True
    best_bid, best_ask = _top_of_book(book)
    if best_bid is None or best_ask is None:
        return False
    return (best_ask - best_bid) <= ENTRY_CFG.max_spread

def _analyze_buy_tender(book: dict, tender_px: float, qty: int) -> Dict[str, Any]:
    """Tender BUY means we BUY shares at tender_px, then unwind by SELLING into bids."""
    bids = book.get("bids", []) if isinstance(book, dict) else []
    unwind_vwap, filled = _vwap_from_levels(bids, qty)
    fill_ratio = (filled / qty) if qty > 0 else 0.0

    if unwind_vwap is None:
        return {"accept": False, "reason": "No bid liquidity", "net_edge": None, "fill_ratio": fill_ratio}

    gross_edge = unwind_vwap - float(tender_px)
    net_edge = gross_edge - ENTRY_CFG.transaction_fee

    if fill_ratio < ENTRY_CFG.min_unwind_fill_ratio:
        return {
            "accept": False,
            "reason": f"Insufficient unwind depth ({filled}/{qty})",
            "net_edge": net_edge,
            "fill_ratio": fill_ratio,
            "unwind_vwap": unwind_vwap,
        }

    if net_edge < ENTRY_CFG.min_net_edge:
        return {
            "accept": False,
            "reason": f"Net edge too small ({net_edge:.4f} <= threshold)",
            "net_edge": net_edge,
            "fill_ratio": fill_ratio,
            "unwind_vwap": unwind_vwap,
        }

    return {
        "accept": True,
        "reason": "OK",
        "net_edge": net_edge,
        "fill_ratio": fill_ratio,
        "unwind_vwap": unwind_vwap,
    }

def _analyze_sell_tender(book: dict, tender_px: float, qty: int) -> Dict[str, Any]:
    """Tender SELL means we SELL shares at tender_px, then unwind by BUYING into asks."""
    asks = book.get("asks", []) if isinstance(book, dict) else []
    unwind_vwap, filled = _vwap_from_levels(asks, qty)
    fill_ratio = (filled / qty) if qty > 0 else 0.0

    if unwind_vwap is None:
        return {"accept": False, "reason": "No ask liquidity", "net_edge": None, "fill_ratio": fill_ratio}

    gross_edge = float(tender_px) - unwind_vwap
    net_edge = gross_edge - ENTRY_CFG.transaction_fee

    if fill_ratio < ENTRY_CFG.min_unwind_fill_ratio:
        return {
            "accept": False,
            "reason": f"Insufficient unwind depth ({filled}/{qty})",
            "net_edge": net_edge,
            "fill_ratio": fill_ratio,
            "unwind_vwap": unwind_vwap,
        }

    if net_edge < ENTRY_CFG.min_net_edge:
        return {
            "accept": False,
            "reason": f"Net edge too small ({net_edge:.4f} <= threshold)",
            "net_edge": net_edge,
            "fill_ratio": fill_ratio,
            "unwind_vwap": unwind_vwap,
        }

    return {
        "accept": True,
        "reason": "OK",
        "net_edge": net_edge,
        "fill_ratio": fill_ratio,
        "unwind_vwap": unwind_vwap,
    }

def cancel_all_orders(session: requests.Session) -> None:
    # Standard RIT command
    try:
        # Some RIT versions require a query parameter (e.g., all=1)
        api_request(session, "POST", "/commands/cancel", params={"all": 1})
    except Exception as exc:
        # Cancel is safety-only; don't hard-fail if endpoint differs
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

    # RIT order endpoint expects query parameters (not JSON body) in this environment.
    return api_request(session, "POST", "/orders", params=params)


# =====================
# Tender evaluation
# =====================


def parse_tender(raw: Dict[str, Any]) -> Optional[Tender]:
    # Field names vary by RIT version; keep permissive mapping.
    tender_id = raw.get("tender_id", raw.get("id"))
    ticker = raw.get("ticker")
    action = raw.get("action")
    quantity = raw.get("quantity")
    price = raw.get("price")

    if tender_id is None or ticker is None or action is None or quantity is None or price is None:
        return None

    expires_in = raw.get("expires_in_seconds")
    if expires_in is None:
        # Sometimes: "expires" epoch ms or remaining "time"
        expires_in = raw.get("time_remaining")

    return Tender(
        tender_id=int(tender_id),
        ticker=str(ticker),
        action=str(action).upper(),
        quantity=int(quantity),
        price=float(price),
        expires_in_seconds=float(expires_in) if expires_in is not None else None,
    )


def _normalize_tender_action(action: str) -> str:
    """Normalize tender action to 'BUY' or 'SELL'."""
    a = (action or "").strip().upper()
    if a not in ("BUY", "SELL"):
        raise ValueError(f"Unknown tender action {action}")
    return a


def unwind_plan_for_tender(t: Tender) -> Tuple[str, str, str]:
    """Returns (our_position_type, unwind_action, book_side_to_hit).

    Two common interpretations exist in RIT integrations:
    1) action is from the INSTITUTION perspective ("institution would like to BUY/SELL")
    2) action is from the TRADER perspective ("you should BUY/SELL")

    Config:
      CONFIG["tender_action_perspective"] in {"INSTITUTION","TRADER"}
      CONFIG["unwind_book_side_mode"] in {"STANDARD","REVERSED"}

    STANDARD unwind:
      - If we unwind by SELL, we hit bids.
      - If we unwind by BUY, we hit asks.

    REVERSED unwind (for debugging RIT-specific semantics):
      - If we unwind by SELL, we hit asks.
      - If we unwind by BUY, we hit bids.
    """
    action = _normalize_tender_action(t.action)

    # If action is from TRADER perspective, invert to institution perspective.
    # Example: trader action = SELL means institution BUYs from us.
    if CONFIG.get("tender_action_perspective", "INSTITUTION").upper() == "TRADER":
        action = "BUY" if action == "SELL" else "SELL"

    # Institution SELLS to us -> we go LONG -> unwind by SELLing.
    # Institution BUYs from us -> we go SHORT -> unwind by BUYing.
    if action == "SELL":
        pos_type, unwind_action = ("LONG", "SELL")
    else:
        pos_type, unwind_action = ("SHORT", "BUY")

    mode = CONFIG.get("unwind_book_side_mode", "STANDARD").upper()
    if mode == "STANDARD":
        book_side = "bids" if unwind_action == "SELL" else "asks"
    elif mode == "REVERSED":
        book_side = "asks" if unwind_action == "SELL" else "bids"
    else:
        raise ValueError(f"Unknown unwind_book_side_mode={mode}")

    return pos_type, unwind_action, book_side


# Backwards-compatible alias
def unwind_side_for_tender(t: Tender) -> Tuple[str, str]:
    pos_type, unwind_action, _ = unwind_plan_for_tender(t)
    return pos_type, unwind_action


def extract_levels(book: Dict[str, Any], side: str, max_levels: int) -> List[Tuple[float, int]]:
    """Return [(price, qty)] from book side."""
    levels = book.get(side, [])
    out: List[Tuple[float, int]] = []
    for lvl in levels[:max_levels]:
        # Typical: {"price": 10.01, "quantity": 1000}
        p = lvl.get("price")
        q = lvl.get("quantity")
        if p is None or q is None:
            continue
        out.append((float(p), int(q)))
    return out

def shallow_vwap(levels: List[Tuple[float, int]], max_levels: int) -> Optional[float]:
    if not levels:
        return None
    used = levels[:max_levels]
    total_qty = sum(q for _, q in used)
    if total_qty == 0:
        return None
    notional = sum(p * q for p, q in used)
    return notional / total_qty



def vwap_for_required_qty(levels: List[Tuple[float, int]], required_qty: int) -> Tuple[Optional[float], int]:
    """Compute liquidation VWAP for required_qty using book levels.

    Returns:
        (vwap, displayed_depth_shares_used)

    If displayed depth is insufficient to cover required_qty, we compute a *conservative*
    VWAP estimate by pricing the remaining shares at the worst displayed level price (last level).
    This is conservative for both sides given typical book ordering.

    This enables a controlled "partial liquidity accept" gate in evaluate_tender().
    """

    required = int(required_qty)
    if required <= 0:
        return (None, 0)

    filled = 0
    notional = 0.0

    for price, qty in levels:
        if filled >= required:
            break
        take = min(int(qty), required - filled)
        if take <= 0:
            continue
        filled += take
        notional += float(price) * float(take)

    if filled == 0:
        return (None, 0)

    if filled < required:
        # Conservative remainder pricing at worst displayed level.
        last_price = float(levels[-1][0])
        remaining = required - filled
        notional += last_price * float(remaining)

    vwap = notional / float(required)
    return (vwap, filled)


def portfolio_limit_ok(security: Dict[str, Any], tender_delta: int) -> bool:
    # RIT commonly provides: position, max_position
    pos = int(security.get("position", 0))
    max_pos = security.get("max_position")
    if max_pos is None:
        # If not provided, be conservative and allow, but log.
        print("[WARN] max_position not provided; skipping limit check")
        return True

    max_pos = int(max_pos)
    new_pos = pos + tender_delta
    return abs(new_pos) <= abs(max_pos)


def evaluate_tender(session: requests.Session, tender: Any) -> tuple[bool, str, dict]:
    """Entry decision for a tender.

    Mirrors the working 'liability_trader.py' approach:
    - Pull a fresh orderbook snapshot.
    - Estimate unwind VWAP using visible depth on the unwind side.
    - Require sufficient depth (min_unwind_fill_ratio).
    - Accept only if net edge (after fees) meets threshold.
    """
    if isinstance(tender, Tender):
        ticker = tender.ticker
        action = tender.action
        qty = int(tender.quantity)
        px = float(tender.price)
    else:
        ticker = tender.get("ticker")
        action = tender.get("action")
        qty = int(tender.get("quantity", 0))
        px = float(tender.get("price", 0.0))

    # Fetch book snapshot (fixes the previous 'book' undefined bug)
    book = get_book(session, ticker)

    if not _spread_ok(book):
        return False, "Spread too wide / bad book", {"ticker": ticker}

    if action == "BUY":
        analysis = _analyze_buy_tender(book, px, qty)
    elif action == "SELL":
        analysis = _analyze_sell_tender(book, px, qty)
    else:
        return False, f"Unknown action {action}", {"ticker": ticker}

    if not analysis.get("accept", False):
        return False, analysis.get("reason", "Rejected"), analysis

    return True, "Accepted", analysis


def best_price(book: Dict[str, Any], side: str) -> Optional[float]:
    lvls = extract_levels(book, side, 1)
    if not lvls:
        return None
    return lvls[0][0]


def marketable_limit_price(session: requests.Session, ticker: str, action: str) -> Optional[float]:
    """Price that should execute immediately (crosses the spread).

    action BUY: set limit above best ask.
    action SELL: set limit below best bid.

    We don't assume tick size; we approximate using the top-of-book price
    and a small relative bump via "ticks" mapped as 0.01 increments.
    """

    book = get_book(session, ticker)
    extra_ticks = int(CONFIG["marketable_limit_extra_ticks"])
    tick = 0.01

    if action == "BUY":
        ask = best_price(book, "asks")
        if ask is None:
            return None
        return float(ask + extra_ticks * tick)

    bid = best_price(book, "bids")
    if bid is None:
        return None
    return float(bid - extra_ticks * tick)


def unwind_position(session: requests.Session, pos: AcceptedPosition) -> None:
    """Flatten pos.remaining_shares ASAP using MARKET-only sliced orders.

    This unwind behavior intentionally mirrors the working pattern in `liability_trader.py`:
    - Cancel outstanding orders (optional safety).
    - Place MARKET orders in max-trade-sized slices.
    - Re-check position after each order and continue until flat.
    """

    ticker = pos.ticker

    # Always trust the live position more than our internal tracker.
    sec0 = get_security(session, ticker)
    current_pos0 = int(sec0.get("position", 0))
    if current_pos0 == 0:
        pos.remaining_shares = 0
        return

    pos.remaining_shares = current_pos0

    max_trade = int(sec0.get("max_trade_size", abs(current_pos0)))
    if max_trade <= 0:
        max_trade = abs(current_pos0)

    placed_orders = 0
    last_seen_pos = current_pos0

    print(f"[UNWIND] MARKET start {ticker}: pos_before={current_pos0}")

    while True:
        sec = get_security(session, ticker)
        current_pos = int(sec.get("position", 0))
        pos.remaining_shares = current_pos

        if current_pos == 0:
            print(f"[UNWIND] MARKET end {ticker}: pos_after=0")
            return

        action = "SELL" if current_pos > 0 else "BUY"
        remaining = abs(current_pos)
        slice_qty = remaining if remaining <= max_trade else max_trade
        if slice_qty <= 0:
            # Should not happen, but avoid a tight loop.
            time.sleep(0.08)
            continue

        if CONFIG.get("cancel_all_before_new_order", True):
            cancel_all_orders(session)

        try:
            post_order(session, ticker=ticker, action=action, quantity=slice_qty, order_type="MARKET")
            placed_orders += 1
            print(f"[ORDER] {ticker} {action} {slice_qty} MARKET")
        except Exception as exc:
            print(f"[WARN] Order failed: {exc}; sleeping briefly")
            time.sleep(0.25)
            continue

        # Allow fills to land, then re-check actual position.
        time.sleep(0.20)
        sec_after = get_security(session, ticker)
        pos_after = int(sec_after.get("position", 0))
        pos.remaining_shares = pos_after

        # If we're not making progress, do one extra "backup" MARKET attempt.
        if pos_after == last_seen_pos and placed_orders == 1:
            backup_qty = min(abs(pos_after), max_trade)
            if backup_qty > 0:
                backup_action = "SELL" if pos_after > 0 else "BUY"
                print(f"[UNWIND] MARKET backup {ticker}: pos={pos_after}, {backup_action} {backup_qty}")
                try:
                    post_order(session, ticker=ticker, action=backup_action, quantity=backup_qty, order_type="MARKET")
                    time.sleep(0.20)
                except Exception as exc:
                    print(f"[WARN] Backup order failed: {exc}")

        last_seen_pos = pos_after


def force_flatten_all(session: requests.Session, tickers: List[str]) -> None:
    """Emergency flatten: cancel orders, then market/marketable-limit to flat."""

    cancel_all_orders(session)
    for ticker in tickers:
        sec = get_security(session, ticker)
        pos = int(sec.get("position", 0))
        if pos == 0:
            continue
        ap = AcceptedPosition(ticker=ticker, remaining_shares=pos, accepted_price=0.0, accepted_at=time.time())
        unwind_position(session, ap)
        # Per-ticker spacing to avoid overwhelming the API during emergency flatten.
        time.sleep(0.20)


def force_close_all_positions(session: requests.Session) -> None:
    """Emergency end-of-case flatten for ALL tickers.

    This mirrors the safety behavior in `liability_trader.py`: when time is almost over,
    cancel and aggressively flatten everything to avoid penalties.
    """

    print("[RISK] FORCE CLOSING ALL POSITIONS")
    cancel_all_orders(session)

    positions = get_all_positions(session)
    for ticker, position in positions.items():
        if position == 0:
            continue
        print(f"[RISK] Force closing {ticker}: {position}")
        ap = AcceptedPosition(ticker=ticker, remaining_shares=position, accepted_price=0.0, accepted_at=time.time())
        unwind_position(session, ap)
        time.sleep(0.20)

    # Double check and retry once if something is still open.
    time.sleep(0.50)
    positions2 = get_all_positions(session)
    for ticker, position in positions2.items():
        if position == 0:
            continue
        print(f"[RISK] Still have position: {ticker} = {position}; retrying")
        ap = AcceptedPosition(ticker=ticker, remaining_shares=position, accepted_price=0.0, accepted_at=time.time())
        unwind_position(session, ap)


# =====================
# Main loop
# =====================


def main() -> None:
    session = requests.Session()

    accepted: Dict[str, AcceptedPosition] = {}
    last_case_poll = 0.0
    time_warning_issued = False
    critical_flatten_done = False

    print("[START] LT3 tender-only liability trader")

    while True:
        now = time.time()

        # Periodically check case status / time remaining
        if now - last_case_poll >= float(CONFIG["poll_case_sec"]):
            last_case_poll = now
            case = get_case(session)
            status = str(case.get("status", "")).upper()
            ticks_remaining = case.get("ticks_remaining")

            if status and status not in ("ACTIVE", "RUNNING"):
                print(f"[CASE] status={status}; stopping")
                break

            if ticks_remaining is not None:
                tr = int(ticks_remaining)
                if tr <= 60 and not time_warning_issued:
                    print(f"[RISK] LAST MINUTE WARNING: ticks_remaining={tr}")
                    time_warning_issued = True

                if tr <= 30 and not critical_flatten_done:
                    print(f"[RISK] CRITICAL: ticks_remaining={tr} => force close ALL positions")
                    critical_flatten_done = True
                    force_close_all_positions(session)

                if tr <= int(CONFIG["force_flatten_ticks_remaining"]):
                    print(f"[RISK] ticks_remaining={tr} => force flatten")
                    # Flatten all tickers we have touched
                    force_flatten_all(session, list(accepted.keys()))

        # If we have accepted positions, unwind immediately (sequentially)
        # This guarantees no trading unless a tender was accepted.
        if accepted:
            for ticker in list(accepted.keys()):
                pos = accepted[ticker]
                unwind_position(session, pos)
                # If flat, remove tracking
                sec = get_security(session, ticker)
                if int(sec.get("position", 0)) == 0:
                    accepted.pop(ticker, None)

            time.sleep(0.05)
            continue

        # Only when flat / no accepted positions: check tenders
        tenders_raw = get_tenders(session)
        tenders: List[Tender] = []
        for raw in tenders_raw:
            t = parse_tender(raw)
            if t is not None:
                tenders.append(t)

        for t in tenders:
            ok, reason, diag = evaluate_tender(session, t)
            print(f"[TENDER] id={t.tender_id} {t.ticker} {t.action} qty={t.quantity} px={t.price:.2f} => {ok} ({reason})")
            if not ok:
                continue

            # ACCEPT tender, then immediately start unwind
            try:
                accept_tender(session, t.tender_id)
            except Exception as exc:
                print(f"[WARN] Failed to accept tender {t.tender_id}: {exc}")
                continue

            pos_type, _ = unwind_side_for_tender(t)
            remaining = +t.quantity if pos_type == "LONG" else -t.quantity
            accepted[t.ticker] = AcceptedPosition(
                ticker=t.ticker,
                remaining_shares=remaining,
                accepted_price=t.price,
                accepted_at=time.time(),
            )
            print(f"[ACCEPTED] id={t.tender_id} {t.ticker} => tracking {accepted[t.ticker].remaining_shares}")

            # Start unwind next loop iteration; no other tenders processed until flat.
            break

        time.sleep(float(CONFIG["poll_tenders_sec"]))


if __name__ == "__main__":
    main()
