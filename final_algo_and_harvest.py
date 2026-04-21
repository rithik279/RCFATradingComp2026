# -*- coding: utf-8 -*-
# MODE C v2.3 -> v2.3.1 (Trend Diagnose + Auto Switch to Harvest Mode)

import signal
import requests
import time
from datetime import datetime
from collections import deque

API_KEY = {"X-API-Key": "T7YOGVYJ"}
TICKER = "ALGO"

TICK_SLEEP = 0.10
REFRESH_EVERY_SECONDS = 0.45

POSITION_LIMIT = 25000
LIMIT_BUFFER   = 1200
PER_ORDER_MAX  = 5000

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
FLATTEN_HARD_TICK = 294
FINAL_CLOSE_TICK  = 295

# ---------------- Trend Diagnose ----------------
MID_WIN = 30
MID_HISTORY = deque(maxlen=200)

# Trend windows
TREND_FAST = 18   # short window
TREND_SLOW = 45   # longer window

# Thresholds (tuned to be "safe": only triggers on real drift)
TREND_STRONG_SLOPE = 0.00055   # ~0.055% per tick-window (approx scale)
TREND_PERSIST_MIN  = 6         # consecutive refresh cycles needed

# When to worry (late)
LATE_TREND_TICK = 220

# Wrong-way + size threshold
WRONG_WAY_POS = 9000

# Harvest mode behavior
HARVEST_HALF_SPREAD_ADD = 0.004   # widen a bit for safety
HARVEST_UNWIND_BIAS = 0.012       # shift quote toward flattening inventory
HARVEST_MAX_S1 = 4500             # reduce size in harvest
HARVEST_STICKY = 8                # stay in harvest for N refresh cycles after trigger

# Vol trigger (kept)
VOL_THRESHOLD = 0.0011

SHUTDOWN = False

def ts():
    return datetime.now().strftime("%H:%M:%S")

def on_sigint(signum, frame):
    global SHUTDOWN
    SHUTDOWN = True

def get_tick(session):
    r = session.get("http://localhost:9999/v1/case")
    r.raise_for_status()
    return r.json()["tick"]

def get_book(session):
    r = session.get("http://localhost:9999/v1/securities/book", params={"ticker": TICKER})
    r.raise_for_status()
    return r.json()

def best_bid_ask(book):
    if not book.get("bids") or not book.get("asks"):
        return None, None
    return float(book["bids"][0]["price"]), float(book["asks"][0]["price"])

def get_position(session):
    r = session.get("http://localhost:9999/v1/securities", params={"ticker": TICKER})
    r.raise_for_status()
    return int(r.json()[0]["position"])

def get_open_orders(session):
    r = session.get("http://localhost:9999/v1/orders", params={"status": "OPEN"})
    r.raise_for_status()
    return [o for o in r.json() if o.get("ticker") == TICKER]

def cancel_all(session):
    try:
        session.post("http://localhost:9999/v1/commands/cancel?all=1")
    except Exception:
        pass

def open_qty(session):
    o = get_open_orders(session)
    ob = sum(int(x["quantity"]) for x in o if x.get("action") == "BUY")
    os = sum(int(x["quantity"]) for x in o if x.get("action") == "SELL")
    return ob, os

def cap_qty(session, side, qty):
    pos = get_position(session)
    ob, os = open_qty(session)
    qty = min(int(qty), PER_ORDER_MAX)
    if qty <= 0:
        return 0
    if side == "BUY":
        max_add = (POSITION_LIMIT - LIMIT_BUFFER) - (pos + ob)
    else:
        max_add = (POSITION_LIMIT - LIMIT_BUFFER) + (pos - os)
    return max(0, min(qty, int(max_add)))

def place_limit(session, side, qty, px):
    qty = cap_qty(session, side, qty)
    if qty <= 0:
        return 0
    session.post("http://localhost:9999/v1/orders", params={
        "ticker": TICKER,
        "type": "LIMIT",
        "action": side,
        "quantity": int(qty),
        "price": round(float(px), 2)
    })
    return qty

def place_market(session, side, qty):
    qty = cap_qty(session, side, qty)
    if qty <= 0:
        return 0
    session.post("http://localhost:9999/v1/orders", params={
        "ticker": TICKER,
        "type": "MARKET",
        "action": side,
        "quantity": int(qty)
    })
    return qty

def calc_vol(mids):
    if len(mids) < MID_WIN:
        return 0.0
    rets = []
    for i in range(-MID_WIN+1, 0):
        a = mids[i-1]
        b = mids[i]
        if a > 0:
            rets.append(abs((b - a) / a))
    return sum(rets) / max(1, len(rets))

def slope(mids, w):
    if len(mids) < w:
        return 0.0
    return (mids[-1] - mids[-w]) / max(1e-9, mids[-w])

def trend_signal():
    mids = list(MID_HISTORY)
    sf = slope(mids, TREND_FAST)
    ss = slope(mids, TREND_SLOW)
    if sf > TREND_STRONG_SLOPE and ss > (TREND_STRONG_SLOPE * 0.8):
        return +1, sf, ss
    if sf < -TREND_STRONG_SLOPE and ss < -(TREND_STRONG_SLOPE * 0.8):
        return -1, sf, ss
    return 0, sf, ss

def compute_quotes(bb, ba, pos, half_spread, extra_bias=0.0):
    mid = (bb + ba) / 2.0
    inv = max(-1.0, min(1.0, pos / 20000.0))
    skew = INVENTORY_SKEW_MAX * inv

    bias = extra_bias * inv

    bid = mid - half_spread - max(0.0, skew) - max(0.0, bias)
    ask = mid + half_spread - min(0.0, skew) - min(0.0, bias)

    bid = min(bid, bb)
    ask = max(ask, ba)

    return round(bid, 2), round(ask, 2), mid

def safe_final_flatten(session):
    cancel_all(session)
    time.sleep(0.05)

    for _ in range(14):
        pos = get_position(session)
        book = get_book(session)
        bb, ba = best_bid_ask(book)
        if pos == 0 or bb is None:
            break
        side = "SELL" if pos > 0 else "BUY"
        px = bb if pos > 0 else ba
        place_limit(session, side, min(PER_ORDER_MAX, abs(pos)), px)
        time.sleep(0.12)

    pos = get_position(session)
    if pos != 0:
        side = "SELL" if pos > 0 else "BUY"
        place_market(session, side, abs(pos))

def main():
    global SHUTDOWN
    with requests.Session() as s:
        s.headers.update(API_KEY)

        print(f"[{ts()}] MODE C v2.3.1 starting (sticky Mode C={STICKY_CYCLES})")

        last_refresh = 0.0
        sticky_left = 0

        trend_persist = 0
        harvest_left = 0

        while not SHUTDOWN:
            tick = get_tick(s)

            if tick < 5:
                time.sleep(TICK_SLEEP)
                continue

            if tick >= FINAL_CLOSE_TICK:
                print(f"[{ts()}] Final flatten...")
                safe_final_flatten(s)
                break

            now = time.time()
            if now - last_refresh > REFRESH_EVERY_SECONDS:
                cancel_all(s)

                book = get_book(s)
                bb, ba = best_bid_ask(book)
                if bb is None:
                    time.sleep(TICK_SLEEP)
                    continue

                spread = ba - bb
                mid = (bb + ba) / 2.0
                MID_HISTORY.append(mid)

                mids = list(MID_HISTORY)
                vol = calc_vol(mids)
                pos = get_position(s)

                triggered_c = (spread >= MODE_C_SPREAD_TRIGGER) or (vol >= VOL_THRESHOLD)
                if triggered_c:
                    sticky_left = STICKY_CYCLES
                else:
                    sticky_left = max(0, sticky_left - 1)

                tdir, sf, ss = trend_signal()
                if tdir != 0 and tick >= LATE_TREND_TICK:
                    trend_persist += 1
                else:
                    trend_persist = max(0, trend_persist - 1)

                wrong_way = (tdir == +1 and pos < -WRONG_WAY_POS) or (tdir == -1 and pos > WRONG_WAY_POS)

                very_strong = abs(sf) > (TREND_STRONG_SLOPE * 1.7) and abs(ss) > (TREND_STRONG_SLOPE * 1.3)
                if (trend_persist >= TREND_PERSIST_MIN and wrong_way) or (tick >= 250 and very_strong and abs(pos) > WRONG_WAY_POS):
                    harvest_left = HARVEST_STICKY
                else:
                    harvest_left = max(0, harvest_left - 1)

                harvest_mode = harvest_left > 0

                if tick >= FLATTEN_SOFT_TICK:
                    sticky_left = 0
                    harvest_left = max(harvest_left, 3)

                mode_c = (sticky_left > 0) and (not harvest_mode) and (tick < FLATTEN_SOFT_TICK)

                half_spread = BASE_HALF_SPREAD
                extra_bias = 0.0

                if mode_c:
                    half_spread += 0.004
                if harvest_mode:
                    half_spread += HARVEST_HALF_SPREAD_ADD
                    extra_bias = HARVEST_UNWIND_BIAS

                bid, ask, _ = compute_quotes(bb, ba, pos, half_spread, extra_bias=extra_bias)

                if harvest_mode:
                    s1 = HARVEST_MAX_S1 if abs(pos) < 7000 else 3500
                    s2 = 2000 if abs(pos) < 9000 else 0
                    s3 = 0
                else:
                    if not mode_c:
                        s1, s2, s3 = (5000, 0, 0) if abs(pos) < 7000 else (4000, 0, 0)
                    else:
                        if abs(pos) < 7000:
                            s1, s2, s3 = 5000, 4000, 2500
                        elif abs(pos) < 14000:
                            s1, s2, s3 = 5000, 2500, 1500
                        else:
                            s1, s2, s3 = 4000, 2000, 0

                place_limit(s, "BUY",  s1, bid)
                place_limit(s, "SELL", s1, ask)

                if s2 > 0:
                    place_limit(s, "BUY",  s2, round(bid - L2_DIST, 2))
                    place_limit(s, "SELL", s2, round(ask + L2_DIST, 2))

                if s3 > 0:
                    place_limit(s, "BUY",  s3, round(bid - L3_DIST, 2))
                    place_limit(s, "SELL", s3, round(ask + L3_DIST, 2))

                print(f"[{ts()}] tick={tick} pos={pos:+} spread={spread:.2f} vol={vol:.4f} "
                      f"tdir={tdir} sf={sf:.4f} ss={ss:.4f} persist={trend_persist} "
                      f"modeC={int(mode_c)} harvest={int(harvest_mode)} stickyC={sticky_left} harvest_left={harvest_left} "
                      f"sizes=({s1},{s2},{s3})")

                last_refresh = now

            time.sleep(TICK_SLEEP)

        print(f"[{ts()}] MODE C v2.3.1 stopped.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, on_sigint)
    main()