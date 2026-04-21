# -*- coding: utf-8 -*-
# MODE C v2.3 FINAL (Sticky Mode C) — STICKY_CYCLES = 6
# Safe upgrades vs v2.2:
#  1) NET exposure clamp inside cap_qty() -> prevents NET fines
#  2) Mode C high-inventory taper: (4000,1500,0) instead of (4000,2000,0)

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

# NEW (v2.3): net clamp to prevent NET limit fines
NET_LIMIT = POSITION_LIMIT - 1600

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
FINAL_CLOSE_TICK  = 295

# Regime helpers
MID_WIN = 25
MID_HISTORY = deque(maxlen=MID_WIN)
VOL_THRESHOLD = 0.0011

# Conditional cancel controls
LAST_MID = None
MID_MOVE_CANCEL = 0.01
MAX_POS_CANCEL  = 16000

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
        gross_headroom = (POSITION_LIMIT - LIMIT_BUFFER) - (pos + ob)
        net_headroom   = NET_LIMIT - pos
        max_add = min(gross_headroom, net_headroom)
    else:
        gross_headroom = (POSITION_LIMIT - LIMIT_BUFFER) + (pos - os)
        net_headroom   = NET_LIMIT + pos
        max_add = min(gross_headroom, net_headroom)

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

def calc_vol():
    if len(MID_HISTORY) < MID_WIN:
        return 0.0
    mids = list(MID_HISTORY)
    rets = []
    for i in range(1, len(mids)):
        if mids[i-1] > 0:
            rets.append(abs((mids[i] - mids[i-1]) / mids[i-1]))
    return sum(rets) / max(1, len(rets))

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

def safe_final_flatten(session):
    cancel_all(session)
    time.sleep(0.05)

    for _ in range(14):
        pos = get_position(session)
        book = get_book(session)
        bb, ba = best_bid_ask(book)
        if pos == 0 or bb is None or ba is None:
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
    global SHUTDOWN, LAST_MID

    with requests.Session() as s:
        s.headers.update(API_KEY)
        print(f"[{ts()}] MODE C v2.3 FINAL starting (sticky={STICKY_CYCLES}, NET_LIMIT={NET_LIMIT}).")

        last_refresh = 0.0
        sticky_left = 0

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
                book = get_book(s)
                bb, ba = best_bid_ask(book)
                if bb is None or ba is None:
                    time.sleep(TICK_SLEEP)
                    continue

                spread = ba - bb
                mid = (bb + ba) / 2.0
                MID_HISTORY.append(mid)
                vol = calc_vol()
                pos = get_position(s)

                triggered = (spread >= MODE_C_SPREAD_TRIGGER) or (vol >= VOL_THRESHOLD)
                regime_switch_on = False
                if triggered:
                    if sticky_left == 0:
                        regime_switch_on = True
                    sticky_left = STICKY_CYCLES
                else:
                    sticky_left = max(0, sticky_left - 1)

                mode_c = sticky_left > 0

                if tick >= FLATTEN_SOFT_TICK:
                    mode_c = False
                    sticky_left = 0

                need_cancel = (
                    LAST_MID is None or
                    abs(mid - LAST_MID) >= MID_MOVE_CANCEL or
                    abs(pos) >= MAX_POS_CANCEL or
                    regime_switch_on
                )

                if need_cancel:
                    cancel_all(s)

                half_spread = choose_half_spread(spread, vol, mode_c)
                bid, ask, _ = compute_quotes(bb, ba, pos, half_spread)

                if not mode_c:
                    s1, s2, s3 = (5000, 0, 0) if abs(pos) < 7000 else (4000, 0, 0)
                else:
                    if abs(pos) < 7000:
                        s1, s2, s3 = 5000, 4000, 2500
                    elif abs(pos) < 14000:
                        s1, s2, s3 = 5000, 2500, 1500
                    else:
                        s1, s2, s3 = 4000, 1500, 0

                place_limit(s, "BUY",  s1, bid)
                place_limit(s, "SELL", s1, ask)

                if s2 > 0:
                    place_limit(s, "BUY",  s2, round(bid - L2_DIST, 2))
                    place_limit(s, "SELL", s2, round(ask + L2_DIST, 2))

                if s3 > 0:
                    place_limit(s, "BUY",  s3, round(bid - L3_DIST, 2))
                    place_limit(s, "SELL", s3, round(ask + L3_DIST, 2))

                LAST_MID = mid
                print(f"[{ts()}] tick={tick} pos={pos:+} spr={spread:.2f} vol={vol:.4f} "
                      f"modeC={int(mode_c)} sticky_left={sticky_left} cancel={int(need_cancel)} "
                      f"half={half_spread:.4f} sizes=({s1},{s2},{s3})")

                last_refresh = now

            time.sleep(TICK_SLEEP)

        print(f"[{ts()}] MODE C v2.3 FINAL stopped.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, on_sigint)
    main()
