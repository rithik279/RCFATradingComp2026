# -*- coding: utf-8 -*-
# ALGO2e v2.1 — v2 + Trend-Aware Reduce-Only Harvest Mode
# Goal: stop harvest losses by triggering only on persistent wrong-way trends
# and quoting reduce-only side only (at touch, no dual quotes)

import signal
import time
from collections import deque
from datetime import datetime
import os

import requests

# === ALGO2e EXTENSION POINT ===
# Add RY (or fee-aware logic) here later without redesigning the engine.
TRADED_SYMBOLS = ["CNR", "AC"]

# Risk universe for portfolio exposure + cancellations.
RISK_SYMBOLS = ["CNR", "RY", "AC"]

# Fee / rebate config (stored only; not used in v2.1 decision logic)
SYMBOL_CONFIG = {
    "CNR": {"fee_take": 0.0027, "rebate_make": 0.0023},
    "AC": {"fee_take": 0.0015, "rebate_make": 0.0011},
}

DEFAULT_API_KEY = "RotmanTrading"
API_KEY = "RotmanTrading"


class ExposureContext:
    def __init__(self, net_cap, gross_cap, net_used, gross_used, per_symbol):
        self.net_cap = int(net_cap)
        self.gross_cap = int(gross_cap)
        self.net_used = int(net_used)
        self.gross_used = int(gross_used)
        self.per_symbol = dict(per_symbol)


def build_exposure_context(session):
    net_cap = GLOBAL_NET_LIMIT - GLOBAL_LIMIT_BUFFER
    gross_cap = GLOBAL_GROSS_LIMIT - GLOBAL_LIMIT_BUFFER

    net_used, gross_used, breakdown = global_exposure(session)
    for sym in RISK_SYMBOLS:
        breakdown.setdefault(
            sym,
            {"pos": 0, "open_buy": 0, "open_sell": 0, "net": 0, "gross": 0},
        )
    return ExposureContext(net_cap, gross_cap, net_used, gross_used, breakdown)


def build_api_headers(api_key: str):
    # Some environments are picky about header casing; set both.
    return {"X-API-Key": api_key, "X-API-key": api_key}


TICK_SLEEP = 0.10
REFRESH_EVERY_SECONDS = 0.45

POSITION_LIMIT = 25000
LIMIT_BUFFER = 1200
PER_ORDER_MAX = 5000

# Net clamp to prevent NET limit fines (per symbol)
NET_LIMIT = POSITION_LIMIT - 1600

# Global (portfolio-level) limits across LIMIT-STOCK category.
GLOBAL_NET_LIMIT = 25000
GLOBAL_GROSS_LIMIT = 25000
GLOBAL_LIMIT_BUFFER = 1500

BASE_HALF_SPREAD = 0.005
INVENTORY_SKEW_MAX = 0.07

# --- Mode C trigger + sticky window ---
MODE_C_SPREAD_TRIGGER = 0.03
STICKY_CYCLES = 6

# Ladder settings in Mode C
L2_DIST = 0.02
L3_DIST = 0.04

# Endgame
FLATTEN_SOFT_TICK = 290
FINAL_CLOSE_TICK = 295

# Regime helpers
MID_WIN = 25
VOL_THRESHOLD = 0.0011

# Conditional cancel controls
MID_MOVE_CANCEL = 0.01
MAX_POS_CANCEL = 16000

# Harvest Mode (reduce-only, trend-aware)
HARVEST_STICKY = 8
HARVEST_HALF_SPREAD_ADD = 0.003

# Trend detection (persistent wrong-way)
TREND_FAST = 18
TREND_SLOW = 45
TREND_STRONG_SLOPE = 0.00055
TREND_PERSIST_MIN = 6
WRONG_WAY_POS = 9000
LATE_TREND_TICK = 220

SHUTDOWN = False


def ts():
    return datetime.now().strftime("%H:%M:%S")


def on_sigint(signum, frame):
    global SHUTDOWN
    SHUTDOWN = True


class SymbolState:
    def __init__(self):
        self.mid_history = deque(maxlen=MID_WIN)
        self.last_mid = None
        self.last_refresh_time = 0.0
        self.sticky_left = 0
        self.harvest_left = 0
        self.trend_persist = 0
        self.last_quotes = None


def get_tick(session):
    r = session.get("http://localhost:9999/v1/case")
    r.raise_for_status()
    return int(r.json()["tick"])


def get_book(session, symbol):
    r = session.get("http://localhost:9999/v1/securities/book", params={"ticker": symbol})
    r.raise_for_status()
    return r.json()


def best_bid_ask(book):
    if not book.get("bids") or not book.get("asks"):
        return None, None
    return float(book["bids"][0]["price"]), float(book["asks"][0]["price"])


def get_position(session, symbol):
    r = session.get("http://localhost:9999/v1/securities", params={"ticker": symbol})
    r.raise_for_status()
    data = r.json()
    if not data:
        return 0
    return int(data[0]["position"])


def get_all_open_orders(session):
    """Fetch all OPEN orders once."""
    r = session.get("http://localhost:9999/v1/orders", params={"status": "OPEN"})
    r.raise_for_status()
    return r.json()


def global_exposure(session):
    """Conservative portfolio exposure across RISK_SYMBOLS including OPEN orders.

    Returns: (net_exposure, gross_exposure, breakdown)
      - net_exposure  = sum(pos + open_buy - open_sell)
      - gross_exposure = sum(abs(pos) + open_buy + open_sell)
    """
    r = session.get("http://localhost:9999/v1/securities")
    r.raise_for_status()
    positions = {sec.get("ticker"): int(sec.get("position", 0)) for sec in r.json()}

    open_orders = get_all_open_orders(session)

    breakdown = {}
    net_exposure = 0
    gross_exposure = 0

    for sym in RISK_SYMBOLS:
        pos = int(positions.get(sym, 0))
        open_buy = sum(
            int(o.get("quantity", 0))
            for o in open_orders
            if o.get("ticker") == sym and o.get("action") == "BUY"
        )
        open_sell = sum(
            int(o.get("quantity", 0))
            for o in open_orders
            if o.get("ticker") == sym and o.get("action") == "SELL"
        )

        sym_net = pos + open_buy - open_sell
        sym_gross = abs(pos) + open_buy + open_sell

        breakdown[sym] = {
            "pos": pos,
            "open_buy": open_buy,
            "open_sell": open_sell,
            "net": sym_net,
            "gross": sym_gross,
        }

        net_exposure += sym_net
        gross_exposure += sym_gross

    return int(net_exposure), int(gross_exposure), breakdown


def get_open_orders(session, symbol):
    return [o for o in get_all_open_orders(session) if o.get("ticker") == symbol]


def cancel_symbol(session, symbol):
    # Symbol-scoped cancel by ticker (RIT native).
    try:
        session.post("http://localhost:9999/v1/commands/cancel", params={"ticker": symbol})
    except Exception:
        pass


def cancel_risk_universe(session):
    for sym in RISK_SYMBOLS:
        cancel_symbol(session, sym)


def open_qty(session, symbol):
    orders = get_open_orders(session, symbol)
    open_buy = sum(int(x["quantity"]) for x in orders if x.get("action") == "BUY")
    open_sell = sum(int(x["quantity"]) for x in orders if x.get("action") == "SELL")
    return open_buy, open_sell


def cap_qty_ctx(session, symbol, side, qty, ctx: ExposureContext):
    # Clamp requested qty so the bot never submits an order that would breach
    # either per-symbol limits OR global (portfolio-level) net/gross limits.
    side = str(side).upper()

    req_qty = min(int(qty), PER_ORDER_MAX)
    if req_qty <= 0:
        return 0

    # --- Per-symbol limits (preserved, but computed via projected positions) ---
    sym_state = ctx.per_symbol.get(symbol) or {"pos": 0, "open_buy": 0, "open_sell": 0}
    pos_sym = int(sym_state.get("pos", 0))
    open_buy = int(sym_state.get("open_buy", 0))
    open_sell = int(sym_state.get("open_sell", 0))

    max_abs_sym = POSITION_LIMIT - LIMIT_BUFFER

    if side == "BUY":
        max_by_symbol = max_abs_sym - (pos_sym + open_buy)
        max_by_symbol_net = NET_LIMIT - pos_sym
    else:
        max_by_symbol = pos_sym - open_sell + max_abs_sym
        max_by_symbol_net = NET_LIMIT + pos_sym

    max_local = max(0, min(req_qty, int(max_by_symbol), int(max_by_symbol_net)))
    if max_local <= 0:
        return 0

    # --- Reduce-only capacity (accounts for OPEN orders) ---
    if side == "BUY":
        reduce_cap = max(0, int(open_sell) - int(pos_sym))
    else:
        reduce_cap = max(0, int(open_buy) + int(pos_sym))

    reduce_only_qty = min(int(max_local), int(reduce_cap))
    risk_inc_need = int(max_local) - int(reduce_only_qty)

    def reserve(q_res):
        q_res = int(q_res)
        if q_res <= 0:
            return
        ctx.gross_used += q_res
        ctx.net_used += (q_res if side == "BUY" else -q_res)
        if side == "BUY":
            sym_state["open_buy"] = int(sym_state.get("open_buy", 0)) + q_res
        else:
            sym_state["open_sell"] = int(sym_state.get("open_sell", 0)) + q_res
        sym_state["pos"] = int(sym_state.get("pos", 0))
        ctx.per_symbol[symbol] = sym_state

    if reduce_only_qty > 0:
        reserve(reduce_only_qty)

    def ok(q_inc):
        q_inc = int(q_inc)
        if q_inc <= 0:
            return True

        net_after = ctx.net_used + (q_inc if side == "BUY" else -q_inc)
        gross_after = ctx.gross_used + q_inc
        return (abs(int(net_after)) <= int(ctx.net_cap)) and (int(gross_after) <= int(ctx.gross_cap))

    lo, hi = 0, int(risk_inc_need)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1

    q_inc = int(lo)
    if q_inc > 0:
        reserve(q_inc)

    return int(int(reduce_only_qty) + int(q_inc))


def place_limit(session, symbol, side, qty, px, ctx):
    qty = cap_qty_ctx(session, symbol, side, qty, ctx)
    if qty <= 0:
        return 0

    session.post(
        "http://localhost:9999/v1/orders",
        params={
            "ticker": symbol,
            "type": "LIMIT",
            "action": side,
            "quantity": int(qty),
            "price": round(float(px), 2),
        },
    )
    return qty


def place_market(session, symbol, side, qty, ctx):
    # Used only for endgame flattening.
    qty = cap_qty_ctx(session, symbol, side, qty, ctx)
    if qty <= 0:
        return 0

    session.post(
        "http://localhost:9999/v1/orders",
        params={
            "ticker": symbol,
            "type": "MARKET",
            "action": side,
            "quantity": int(qty),
        },
    )
    return qty


def calc_vol(state: SymbolState):
    if len(state.mid_history) < MID_WIN:
        return 0.0

    mids = list(state.mid_history)
    rets = []
    for i in range(1, len(mids)):
        if mids[i - 1] > 0:
            rets.append(abs((mids[i] - mids[i - 1]) / mids[i - 1]))

    return sum(rets) / max(1, len(rets))


def slope(mids, w):
    if len(mids) < w:
        return 0.0
    return (float(mids[-1]) - float(mids[-w])) / max(1e-9, float(mids[-w]))


def trend_dir(mids):
    sf = slope(mids, TREND_FAST)
    ss = slope(mids, TREND_SLOW)
    if sf > TREND_STRONG_SLOPE and ss > TREND_STRONG_SLOPE * 0.8:
        return 1, sf, ss
    if sf < -TREND_STRONG_SLOPE and ss < -TREND_STRONG_SLOPE * 0.8:
        return -1, sf, ss
    return 0, sf, ss


def compute_quotes(bb, ba, pos, half_spread):
    mid = (bb + ba) / 2.0
    inv = max(-1.0, min(1.0, pos / 20000.0))
    skew = INVENTORY_SKEW_MAX * inv
    bid = min(mid - half_spread - max(0.0, skew), bb)
    ask = max(mid + half_spread - min(0.0, skew), ba)
    return round(bid, 2), round(ask, 2), mid


def choose_half_spread(spread, vol, mode_c):
    half = BASE_HALF_SPREAD
    if spread <= 0.02 and vol < VOL_THRESHOLD:
        half = max(0.0035, BASE_HALF_SPREAD - 0.0015)
    if spread >= 0.03 or vol >= VOL_THRESHOLD:
        half = min(0.012, BASE_HALF_SPREAD + 0.0025)
    if mode_c:
        half = min(0.016, half + 0.004)
    return half


def safe_final_flatten(session, symbol):
    cancel_symbol(session, symbol)
    time.sleep(0.05)

    ctx = build_exposure_context(session)

    for _ in range(14):
        pos = get_position(session, symbol)
        book = get_book(session, symbol)
        bb, ba = best_bid_ask(book)
        if pos == 0 or bb is None or ba is None:
            break
        side = "SELL" if pos > 0 else "BUY"
        px = bb if pos > 0 else ba
        place_limit(session, symbol, side, min(PER_ORDER_MAX, abs(pos)), px, ctx)
        time.sleep(0.12)

    pos = get_position(session, symbol)
    if pos != 0:
        side = "SELL" if pos > 0 else "BUY"
        place_market(session, symbol, side, abs(pos), ctx)


def main():
    global SHUTDOWN

    for sym in TRADED_SYMBOLS:
        if sym not in SYMBOL_CONFIG:
            raise ValueError(f"Missing SYMBOL_CONFIG for {sym}")

    states = {sym: SymbolState() for sym in TRADED_SYMBOLS}

    with requests.Session() as s:
        s.headers.update(build_api_headers(API_KEY))

        cancel_risk_universe(s)
        time.sleep(0.15)

        print(
            f"[{ts()}] ALGO2e v2.1 starting: symbols={TRADED_SYMBOLS}, sticky={STICKY_CYCLES}, NET_LIMIT={NET_LIMIT}."
        )

        while not SHUTDOWN:
            tick = get_tick(s)

            ctx = build_exposure_context(s)

            if tick < 5:
                time.sleep(TICK_SLEEP)
                continue

            if tick >= FINAL_CLOSE_TICK:
                print(f"[{ts()}] Final flatten...")
                for sym in TRADED_SYMBOLS:
                    safe_final_flatten(s, sym)
                break

            now = time.time()

            for sym in TRADED_SYMBOLS:
                st = states[sym]

                if now - st.last_refresh_time <= REFRESH_EVERY_SECONDS:
                    continue

                book = get_book(s, sym)
                bb, ba = best_bid_ask(book)
                if bb is None or ba is None:
                    st.last_refresh_time = now
                    continue

                spread = ba - bb
                mid = (bb + ba) / 2.0
                st.mid_history.append(mid)
                vol = calc_vol(st)
                pos = get_position(s, sym)

                # Trend detection (persistent + wrong-way).
                tdir, sf, ss = trend_dir(list(st.mid_history))

                if tick >= LATE_TREND_TICK and tdir != 0:
                    st.trend_persist += 1
                else:
                    st.trend_persist = max(0, st.trend_persist - 1)

                wrong_way = (tdir == 1 and pos < -WRONG_WAY_POS) or (tdir == -1 and pos > WRONG_WAY_POS)

                # Harvest triggers ONLY on persistent wrong-way OR end-of-case.
                harvest_triggered = (st.trend_persist >= TREND_PERSIST_MIN and wrong_way) or (
                    tick >= FLATTEN_SOFT_TICK
                )
                if harvest_triggered:
                    st.harvest_left = HARVEST_STICKY
                else:
                    st.harvest_left = max(0, st.harvest_left - 1)

                harvest_mode = st.harvest_left > 0

                triggered = (spread >= MODE_C_SPREAD_TRIGGER) or (vol >= VOL_THRESHOLD)
                regime_switch_on = False
                if triggered:
                    if st.sticky_left == 0:
                        regime_switch_on = True
                    st.sticky_left = STICKY_CYCLES
                else:
                    st.sticky_left = max(0, st.sticky_left - 1)

                mode_c = st.sticky_left > 0

                if tick >= FLATTEN_SOFT_TICK:
                    mode_c = False
                    st.sticky_left = 0

                # Harvest overrides Mode C.
                if harvest_mode:
                    mode_c = False
                    cancel_symbol(s, sym)
                    time.sleep(0.05)
                    ctx = build_exposure_context(s)

                need_cancel = (
                    st.last_mid is None
                    or abs(mid - st.last_mid) >= MID_MOVE_CANCEL
                    or abs(pos) >= MAX_POS_CANCEL
                    or regime_switch_on
                )

                if need_cancel and not harvest_mode:
                    cancel_symbol(s, sym)
                    time.sleep(0.10)
                    ctx = build_exposure_context(s)

                # Harvest mode: reduce-only, one-sided at touch.
                if harvest_mode:
                    if pos > 0:
                        place_limit(s, sym, "SELL", min(PER_ORDER_MAX, abs(pos)), ba, ctx)
                    elif pos < 0:
                        place_limit(s, sym, "BUY", min(PER_ORDER_MAX, abs(pos)), bb, ctx)

                    st.last_mid = mid
                    st.last_refresh_time = now

                    print(
                        f"[{ts()}] {sym} tick={tick} pos={pos:+} spr={spread:.2f} vol={vol:.4f} "
                        f"harvest={int(harvest_mode)} harvest_left={st.harvest_left} trend_persist={st.trend_persist} "
                        f"tdir={tdir} sf={sf:.4f} ss={ss:.4f} wrong_way={int(wrong_way)}"
                    )
                    continue

                half_spread = choose_half_spread(spread, vol, mode_c)
                bid, ask, _ = compute_quotes(bb, ba, pos, half_spread)
                st.last_quotes = (bid, ask)

                if not mode_c:
                    s1, s2, s3 = (5000, 0, 0) if abs(pos) < 7000 else (4000, 0, 0)
                else:
                    if abs(pos) < 7000:
                        s1, s2, s3 = 5000, 4000, 2500
                    elif abs(pos) < 14000:
                        s1, s2, s3 = 5000, 2500, 1500
                    else:
                        s1, s2, s3 = 4000, 1500, 0

                gross_headroom = int(ctx.gross_cap) - int(ctx.gross_used)
                desired_total = int(2 * (int(s1) + int(s2) + int(s3)))
                if desired_total > 0 and gross_headroom < desired_total:
                    scale = max(0.0, min(1.0, (gross_headroom / desired_total) * 0.90))

                    def round100(x):
                        return max(0, int(x) // 100 * 100)

                    s1 = round100(int(s1 * scale))
                    s2 = round100(int(s2 * scale))
                    s3 = round100(int(s3 * scale))

                    print(
                        f"[{ts()}] RISK ctx net={ctx.net_used}/{ctx.net_cap} gross={ctx.gross_used}/{ctx.gross_cap} "
                        f"headroom={gross_headroom} scale={scale:.2f} sizes=({s1},{s2},{s3})"
                    )
                else:
                    print(
                        f"[{ts()}] RISK ctx net={ctx.net_used}/{ctx.net_cap} gross={ctx.gross_used}/{ctx.gross_cap} "
                        f"headroom={gross_headroom} sizes=({s1},{s2},{s3})"
                    )

                place_limit(s, sym, "BUY", s1, bid, ctx)
                place_limit(s, sym, "SELL", s1, ask, ctx)

                if s2 > 0:
                    place_limit(s, sym, "BUY", s2, round(bid - L2_DIST, 2), ctx)
                    place_limit(s, sym, "SELL", s2, round(ask + L2_DIST, 2), ctx)

                if s3 > 0:
                    place_limit(s, sym, "BUY", s3, round(bid - L3_DIST, 2), ctx)
                    place_limit(s, sym, "SELL", s3, round(ask + L3_DIST, 2), ctx)

                st.last_mid = mid
                st.last_refresh_time = now

                print(
                    f"[{ts()}] {sym} tick={tick} pos={pos:+} spr={spread:.2f} vol={vol:.4f} "
                    f"modeC={int(mode_c)} sticky_left={st.sticky_left} trend_persist={st.trend_persist} "
                    f"tdir={tdir} sf={sf:.4f} ss={ss:.4f} wrong_way={int(wrong_way)} cancel={int(need_cancel)} "
                    f"half={half_spread:.4f} sizes=({s1},{s2},{s3})"
                )

            time.sleep(TICK_SLEEP)

        print(f"[{ts()}] ALGO2e v2.1 stopped.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, on_sigint)
    main()
