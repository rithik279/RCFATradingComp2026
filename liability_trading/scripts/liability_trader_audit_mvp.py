import csv
import os
import time
from datetime import datetime, timezone

import requests


class LiabilityTrader:
    """Audit MVP wrapper around the working Liability Trader 3.0 logic.

    Hard constraints for this file:
    - Preserve the same trading logic and decisions.
    - Add deterministic, timestamped CSV logging to disk.
    - Add *passive* detectors that only log flags and never block trading.

    NOTE: Threshold-based detectors are configurable and intentionally unset by default
    to avoid guessing values. Set them via the constants below.
    """

    # =====================
    # Detector thresholds (configure explicitly)
    # =====================
    # LIMIT orders resting longer than X seconds
    DETECT_RESTING_LIMIT_SEC = None  # e.g. 5.0

    # More than N orders per second (global)
    DETECT_ORDERS_PER_SEC_N = None  # e.g. 8

    # Unwinds that take longer than Y seconds
    DETECT_UNWIND_LONG_SEC = None  # e.g. 15.0

    # Position held longer than Z seconds after tender accept
    DETECT_POSITION_HELD_SEC = None  # e.g. 20.0

    # Window for "both sides" detection (BUY and SELL in same ticker)
    DETECT_BOTH_SIDES_WINDOW_SEC = 2.0

    # =====================
    # Trading logic config (copied from liability_trader3.0.py)
    # =====================

    def __init__(self, api_key, base_url="http://localhost:9999/v1"):
        self.session = requests.Session()
        self.session.headers.update({'X-API-Key': api_key})
        self.base_url = base_url
        self.transaction_fee = 0.02
        self.max_order_sizes = {'CRZY': 25000, 'TAME': 10000}

        # Risk limits
        self.net_limit = 100000
        self.gross_limit = 250000

        # Current state
        self.positions = {'CRZY': 0, 'TAME': 0}
        self.tenders_accepted = 0
        self.tenders_received = 0

        # Performance tracking
        self.start_time = time.time()
        self.total_pnl = 0

        # Time management
        self.end_time = None  # Will be set when case starts
        self.time_warning_issued = False

        # Active orders tracking
        self.active_orders = []

        # Track most recent tender price per ticker for execution diagnostics/guardrails.
        self.last_tender_price = {}

        # =====================
        # EXECUTION-ONLY KNOBS
        # (Tender acceptance logic must remain unchanged)
        # =====================

        # PHASE 2: passive reprice controls
        self.phase2_max_reprices = 8
        self.phase2_check_sleep = 0.20
        self.phase2_min_tick = 0.01
        self.phase2_max_spread = 0.10  # if spread wider than this, stop being passive

        # pacing (avoid overwhelming API)
        self.sleep_between_orders = 0.08

        # Backoff / safety for transient server errors (HTTP 5xx)
        self.order_backoff_base = 0.25
        self.order_backoff_max = 1.50
        self.max_consecutive_order_failures = 10
        self.position_settle_sleep = 0.20

        # Market sizing controls (per spec)
        self.market_tob_mult = 1.25

        # Internal: last order status for execution logic
        self._last_order_status_code = None
        self._last_order_error_text = None

        # =============
        # Audit state
        # =============
        self._last_case = None
        self._last_case_ts = 0.0

        # Tender lifecycle tracking
        self._active_tender_id = None
        self._active_tender_ticker = None
        self._active_tender_started_ts = None

        # Position holding tracking per ticker
        self._tender_accept_ts_by_ticker = {}

        # Order tracking for detectors
        self._order_placed_ts = {}  # order_id -> ts
        self._order_meta = {}  # order_id -> dict(ticker, action, type, reason)
        self._recent_order_events = []  # list of (ts)
        self._recent_actions_by_ticker = {}  # ticker -> list[(ts, action)]

        # Unwind tracking for detectors
        self._unwind_started_ts_by_ticker = {}

        # One-time warnings for unset thresholds
        self._warned_missing_thresholds = set()

        # CSV logger
        self._audit_path = os.path.join(os.getcwd(), "audit_log.csv")
        self._audit_fp = open(self._audit_path, "a", newline="", encoding="utf-8")
        self._audit_writer = csv.DictWriter(self._audit_fp, fieldnames=self._audit_fieldnames())
        if self._audit_fp.tell() == 0:
            self._audit_writer.writeheader()
            self._audit_fp.flush()

        self._audit_event("RUN_START", details="audit mvp started")

        print(f"[{self._timestamp()}] Liability Trader (AUDIT MVP) initialized")
        print(f"[{self._timestamp()}] Writing audit log to: {self._audit_path}")

    # =====================
    # Audit logging
    # =====================

    def _audit_fieldnames(self):
        # Superset schema. Each event writes blanks for irrelevant fields.
        return [
            "timestamp",
            "tick",
            "time_remaining",
            "event_type",
            # Tender fields
            "tender_id",
            "ticker",
            "quantity",
            "price",
            "decision_reason",
            "estimated_edge",
            "estimated_profit",
            # Order fields
            "order_id",
            "action",
            "order_type",
            "order_price",
            "reason",
            # Position fields
            "position",
            "unrealized_pnl",
            "realized_pnl",
            "position_before",
            "position_after",
            "method",
            # Fines
            "total_fines",
            "pnl_at_time",
            # Detector flags
            "flag_type",
            "flag_details",
            # Misc
            "details",
        ]

    def _iso_ts(self):
        return datetime.now(timezone.utc).isoformat()

    def _safe_tick(self):
        case = self.get_case_info()
        if isinstance(case, dict):
            try:
                return int(case.get("tick", 0) or 0)
            except Exception:
                return None
        return None

    def _safe_time_remaining(self):
        try:
            return float(self.get_remaining_time())
        except Exception:
            return None

    def _audit_event(self, event_type, **fields):
        row = {k: "" for k in self._audit_fieldnames()}
        row["timestamp"] = self._iso_ts()
        tick = self._safe_tick()
        if tick is not None:
            row["tick"] = tick
        tr = self._safe_time_remaining()
        if tr is not None:
            row["time_remaining"] = f"{tr:.4f}"

        row["event_type"] = str(event_type)
        for k, v in fields.items():
            if k not in row:
                # pack extra fields into details deterministically
                prev = row.get("details") or ""
                extra = f"{k}={v}"
                row["details"] = (prev + (";" if prev else "") + extra)
            else:
                row[k] = v

        self._audit_writer.writerow(row)
        self._audit_fp.flush()

    def _flag(self, flag_type, **fields):
        fields = dict(fields)
        fields["flag_type"] = flag_type
        self._audit_event("FLAG", **fields)

    def _warn_threshold_missing_once(self, key, message):
        if key in self._warned_missing_thresholds:
            return
        self._warned_missing_thresholds.add(key)
        self._flag("DETECTOR_CONFIG_MISSING", flag_details=message)

    # =====================
    # Base helpers (copied logic)
    # =====================

    def _timestamp(self):
        return datetime.now().strftime("%H:%M:%S")

    def get_case_info(self):
        """Get case information including remaining time (cached for audit)."""
        # Cache briefly to avoid adding excessive audit overhead.
        now = time.time()
        if self._last_case is not None and (now - self._last_case_ts) < 0.25:
            return self._last_case

        try:
            resp = self.session.get(f'{self.base_url}/case')
            if resp.status_code == 200:
                case = resp.json()
                self._last_case = case
                self._last_case_ts = now
                if self.end_time is None:
                    self.end_time = case.get('tick', 0) + 300
                    print(f"[{self._timestamp()}] Case ends at tick: {self.end_time}")
                return case
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting case info: {e}")
        return None

    def get_remaining_time(self):
        """Get remaining time in seconds"""
        case = self.get_case_info()
        if case and self.end_time:
            current_tick = case.get('tick', 0)
            remaining = self.end_time - current_tick
            return max(0, remaining)
        return 300

    def check_time_warning(self):
        """Check if we're running out of time and adjust strategy"""
        remaining = self.get_remaining_time()

        if remaining <= 60 and not self.time_warning_issued:
            print(f"[{self._timestamp()}] ⚠ LAST MINUTE WARNING: {remaining}s remaining")
            print(f"[{self._timestamp()}] Switching to aggressive closing mode")
            self.time_warning_issued = True

        elif remaining <= 30:
            print(f"[{self._timestamp()}] ⚠ CRITICAL: {remaining}s remaining")
            print(f"[{self._timestamp()}] Forcing position closure")
            self.force_close_all_positions()

        elif remaining <= 120:
            return 'aggressive'

        return 'normal'

    # =====================
    # API helpers for audit
    # =====================

    def get_limits(self):
        try:
            resp = self.session.get(f'{self.base_url}/limits')
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def audit_fine_snapshot(self, context=""):
        limits = self.get_limits()
        gross_fine = limits.get("gross_fine")
        net_fine = limits.get("net_fine")
        total_fines = ""
        try:
            total_fines = float(gross_fine or 0) + float(net_fine or 0)
        except Exception:
            total_fines = ""

        self._audit_event(
            "FINE_SNAPSHOT",
            total_fines=total_fines,
            pnl_at_time="",
            details=f"gross_fine={gross_fine};net_fine={net_fine};ctx={context}",
        )

    # =====================
    # Positions + orders
    # =====================

    def get_all_positions(self):
        """Get all current positions"""
        try:
            resp = self.session.get(f'{self.base_url}/securities')
            if resp.status_code == 200:
                securities = resp.json()
                positions = {}
                for sec in securities:
                    ticker = sec.get('ticker')
                    if not ticker:
                        continue
                    positions[ticker] = sec.get('position', 0)

                    # POSITION_SNAPSHOT (per ticker) — pnl fields may not exist in this API; leave blank.
                    self._audit_event(
                        "POSITION_SNAPSHOT",
                        ticker=ticker,
                        position=sec.get('position', 0),
                        unrealized_pnl=sec.get('unrealized_pnl', ""),
                        realized_pnl=sec.get('realized_pnl', ""),
                        details=f"vwap={sec.get('vwap')};last={sec.get('last')};bid={sec.get('bid')};ask={sec.get('ask')}",
                    )

                self.positions = positions
                return positions
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting positions: {e}")
        return self.positions

    def cancel_all_orders(self, reason="cancel_all"):
        """Cancel all active orders"""
        print(f"[{self._timestamp()}] Cancelling all open orders...")

        try:
            resp = self.session.get(f'{self.base_url}/orders')
            if resp.status_code == 200:
                orders = resp.json()
                for order in orders:
                    order_id = order.get('order_id')
                    if not order_id:
                        continue
                    try:
                        cancel_resp = self.session.delete(f'{self.base_url}/orders/{order_id}')
                        if cancel_resp.status_code == 200:
                            print(f"[{self._timestamp()}] ✓ Canceled order {order_id}")
                            self._audit_event(
                                "ORDER_CANCELLED",
                                order_id=order_id,
                                reason=reason,
                                ticker=order.get('ticker', ""),
                                details=f"status={order.get('status', '')}",
                            )

                            # Resting detector (if configured)
                            if order_id in self._order_placed_ts and self.DETECT_RESTING_LIMIT_SEC is not None:
                                age = time.time() - float(self._order_placed_ts.get(order_id, time.time()))
                                if age > float(self.DETECT_RESTING_LIMIT_SEC):
                                    self._flag(
                                        "SUSPECT_PASSIVE_ORDER",
                                        order_id=order_id,
                                        ticker=order.get('ticker', ""),
                                        flag_details=f"rested_sec={age:.3f} > {self.DETECT_RESTING_LIMIT_SEC}",
                                    )

                            self._order_placed_ts.pop(order_id, None)
                            self._order_meta.pop(order_id, None)
                        else:
                            print(f"[{self._timestamp()}] ✗ Failed to cancel order {order_id}")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[{self._timestamp()}] Error cancelling orders: {e}")

        self.active_orders = []

    def get_tender_offers(self):
        """Check for new tender offers"""
        try:
            resp = self.session.get(f'{self.base_url}/tenders')
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting tenders: {e}")
        return []

    def get_order_book(self, ticker, levels=20):
        """Get order book for a specific ticker"""
        try:
            resp = self.session.get(f'{self.base_url}/securities/book', params={'ticker': ticker})
            if resp.status_code == 200:
                book = resp.json()
                bids = sorted(book.get('bids', book.get('bid', [])), key=lambda x: x['price'], reverse=True)[:levels]
                asks = sorted(book.get('asks', book.get('ask', [])), key=lambda x: x['price'])[:levels]
                return {'bids': bids, 'asks': asks}
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting order book: {e}")
        return {'bids': [], 'asks': []}

    def get_security(self, ticker):
        """Get security details for a ticker (execution helper)."""
        try:
            resp = self.session.get(f'{self.base_url}/securities', params={'ticker': ticker})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting security: {e}")
        return {}

    def _effective_max_trade_size(self, ticker, fallback):
        """Prefer live RIT max_trade_size, fall back to static config."""
        sec = self.get_security(ticker)
        max_trade = sec.get('max_trade_size')
        try:
            if max_trade is not None:
                max_trade = int(max_trade)
        except Exception:
            max_trade = None

        static_limit = int(self.max_order_sizes.get(ticker, fallback))
        if max_trade is None or max_trade <= 0:
            return max(1, static_limit)
        return max(1, min(static_limit, max_trade))

    def _sleep_with_backoff(self, failures):
        failures = int(max(1, failures))
        sleep_s = min(float(self.order_backoff_max), float(self.order_backoff_base) * failures)
        time.sleep(sleep_s)

    def place_order(self, ticker, action, qty, price=None, order_type="LIMIT", reason=""):
        """Place an order (audit-instrumented; behavior unchanged)."""
        if qty <= 0:
            self._flag("SUSPECT_ZERO_POSITION_ORDER", ticker=ticker, flag_details="attempted order with qty<=0")
            return None

        # Detector: order placed when position == 0 (best-effort from cached positions)
        try:
            pos = int(self.positions.get(ticker, 0))
            if pos == 0:
                self._flag("SUSPECT_ZERO_POSITION_ORDER", ticker=ticker, flag_details=f"pos==0; reason={reason}")
        except Exception:
            pass

        # Detector: LIMIT order when no tender lifecycle active
        if str(order_type).upper() == "LIMIT":
            if self._active_tender_id is None:
                self._flag(
                    "SUSPECT_TENDER_VIOLATION",
                    ticker=ticker,
                    flag_details=f"LIMIT while no tender active; reason={reason}",
                )

        # Detector: more than N orders per second
        if self.DETECT_ORDERS_PER_SEC_N is None:
            self._warn_threshold_missing_once(
                "orders_per_sec",
                "DETECT_ORDERS_PER_SEC_N is unset; orders/sec detector disabled",
            )
        else:
            now = time.time()
            self._recent_order_events.append(now)
            # keep last 1s
            self._recent_order_events = [t for t in self._recent_order_events if now - t <= 1.0]
            if len(self._recent_order_events) > int(self.DETECT_ORDERS_PER_SEC_N):
                self._flag(
                    "SUSPECT_RATE",
                    ticker=ticker,
                    flag_details=f"orders_last_1s={len(self._recent_order_events)} > {self.DETECT_ORDERS_PER_SEC_N}",
                )

        # Detector: both sides of book within a short window
        now = time.time()
        ticker_actions = self._recent_actions_by_ticker.setdefault(str(ticker), [])
        ticker_actions.append((now, str(action).upper()))
        ticker_actions = [(t, a) for (t, a) in ticker_actions if now - t <= float(self.DETECT_BOTH_SIDES_WINDOW_SEC)]
        self._recent_actions_by_ticker[str(ticker)] = ticker_actions
        if {a for _, a in ticker_actions} >= {"BUY", "SELL"}:
            self._flag(
                "SUSPECT_MARKET_MAKING",
                ticker=ticker,
                flag_details=f"BUY and SELL within {self.DETECT_BOTH_SIDES_WINDOW_SEC}s; reason={reason}",
            )

        try:
            slice_qty = int(min(int(qty), int(self.max_order_sizes[ticker])))

            params = {
                'ticker': ticker,
                'type': str(order_type).upper(),
                'quantity': slice_qty,
                'action': str(action).upper(),
            }
            if price is not None and str(order_type).upper() == "LIMIT":
                params['price'] = float(price)

            resp = self.session.post(f'{self.base_url}/orders', params=params)

            self._last_order_status_code = resp.status_code
            try:
                self._last_order_error_text = resp.text
            except Exception:
                self._last_order_error_text = None

            if resp.status_code == 200:
                order_info = resp.json()
                order_id = order_info.get('order_id')
                if order_id:
                    self.active_orders.append(order_id)
                    self._order_placed_ts[order_id] = time.time()
                    self._order_meta[order_id] = {
                        "ticker": ticker,
                        "action": str(action).upper(),
                        "type": str(order_type).upper(),
                        "reason": reason,
                    }

                # Audit ORDER_PLACED
                self._audit_event(
                    "ORDER_PLACED",
                    order_id=order_id or "",
                    ticker=ticker,
                    action=str(action).upper(),
                    quantity=slice_qty,
                    order_type=str(order_type).upper(),
                    order_price=("" if price is None else float(price)),
                    reason=reason,
                )

                price_str = f"@ ${price:.2f}" if price else "@ MARKET"
                print(f"[{self._timestamp()}] ✓ Placed {order_type} order: {action} {slice_qty} {ticker} {price_str}")
                return order_info
            else:
                print(f"[{self._timestamp()}] ✗ Order failed: HTTP {resp.status_code}")

        except Exception as e:
            print(f"[{self._timestamp()}] Error placing order: {e}")

        return None

    # =====================
    # Tender logic (unchanged)
    # =====================

    def evaluate_tender(self, tender):
        """Evaluate tender offer - returns (accept, profit, reason, unwind_plan)"""
        ticker = tender['ticker']
        qty = tender['quantity']
        price = tender['price']

        remaining = self.get_remaining_time()
        time_mode = self.check_time_warning()

        book = self.get_order_book(ticker)

        if qty > 0:
            return self._analyze_buy_tender(ticker, qty, price, book['asks'], time_mode, remaining)
        else:
            return self._analyze_sell_tender(ticker, abs(qty), price, book['bids'], time_mode, remaining)

    def _analyze_buy_tender(self, ticker, block_qty, block_price, asks, time_mode, remaining):
        if not asks:
            return False, 0, "No ask prices available", []

        if time_mode == 'aggressive' or remaining < 120:
            min_profit = 1500
            min_per_share = 0.05
            min_liquidity = 0.7
        else:
            min_profit = 1000
            min_per_share = 0.04
            min_liquidity = 0.6

        remaining_qty = block_qty
        profit = 0
        immediate_liquidity = 0
        unwind_plan = []

        for level in asks:
            ask_price = level['price']
            ask_qty = level['quantity'] - level.get('quantity_filled', 0)

            profit_per_share = ask_price - block_price - 2 * self.transaction_fee

            if profit_per_share <= 0 or ask_qty <= 0:
                continue

            qty = min(remaining_qty, ask_qty, self.max_order_sizes[ticker])
            level_profit = profit_per_share * qty
            profit += level_profit
            immediate_liquidity += qty

            unwind_plan.append({'price': ask_price, 'quantity': qty, 'profit': level_profit, 'action': 'SELL'})

            remaining_qty -= qty
            if remaining_qty <= 0:
                break

        liquidity_coverage = immediate_liquidity / block_qty if block_qty > 0 else 0
        avg_profit = profit / block_qty if block_qty > 0 else 0

        accept = (profit > min_profit and avg_profit >= min_per_share and liquidity_coverage >= min_liquidity)

        if remaining <= 30:
            accept = False
            reason = "REJECTED: Last 30 seconds - no new positions"
        else:
            reason = f"Profit: ${profit:.2f}, Avg: ${avg_profit:.4f}/share, Liquidity: {liquidity_coverage:.1%}"
            if not accept and profit > 0:
                reason += " (Rejected: thresholds not met)"

        return accept, profit, reason, unwind_plan

    def _analyze_sell_tender(self, ticker, block_qty, block_price, bids, time_mode, remaining):
        if not bids:
            return False, 0, "No bid prices available", []

        if time_mode == 'aggressive' or remaining < 120:
            min_profit = 1500
            min_per_share = 0.05
            min_liquidity = 0.7
        else:
            min_profit = 1000
            min_per_share = 0.04
            min_liquidity = 0.6

        remaining_qty = block_qty
        profit = 0
        immediate_liquidity = 0
        unwind_plan = []

        for level in bids:
            bid_price = level['price']
            bid_qty = level['quantity'] - level.get('quantity_filled', 0)

            profit_per_share = block_price - bid_price - 2 * self.transaction_fee

            if profit_per_share <= 0 or bid_qty <= 0:
                continue

            qty = min(remaining_qty, bid_qty, self.max_order_sizes[ticker])
            level_profit = profit_per_share * qty
            profit += level_profit
            immediate_liquidity += qty

            unwind_plan.append({'price': bid_price, 'quantity': qty, 'profit': level_profit, 'action': 'BUY'})

            remaining_qty -= qty
            if remaining_qty <= 0:
                break

        liquidity_coverage = immediate_liquidity / block_qty if block_qty > 0 else 0
        avg_profit = profit / block_qty if block_qty > 0 else 0

        accept = (profit > min_profit and avg_profit >= min_per_share and liquidity_coverage >= min_liquidity)

        if remaining <= 30:
            accept = False
            reason = "REJECTED: Last 30 seconds - no new positions"
        else:
            reason = f"Profit: ${profit:.2f}, Avg: ${avg_profit:.4f}/share, Liquidity: {liquidity_coverage:.1%}"
            if not accept and profit > 0:
                reason += " (Rejected: thresholds not met)"

        return accept, profit, reason, unwind_plan

    def accept_tender(self, tender_id):
        try:
            resp = self.session.post(f'{self.base_url}/tenders/{tender_id}', json={'status': 'ACCEPTED'})
            if resp.status_code == 200:
                print(f"[{self._timestamp()}] ✓ Tender {tender_id} accepted")
                return True
        except Exception as e:
            print(f"[{self._timestamp()}] Error accepting tender: {e}")
        return False

    def decline_tender(self, tender_id):
        try:
            resp = self.session.post(f'{self.base_url}/tenders/{tender_id}', json={'status': 'DECLINED'})
            if resp.status_code == 200:
                print(f"[{self._timestamp()}] ✗ Tender {tender_id} declined")
                return True
        except Exception:
            pass
        return False

    # =====================
    # Execution logic (copied from 3.0; add audit hooks only)
    # =====================

    def _visible_level_qty(self, level):
        try:
            return max(0, float(level.get('quantity', 0)) - float(level.get('quantity_filled', 0)))
        except Exception:
            return 0.0

    def _book_side_levels(self, ticker, side, depth=5):
        book = self.get_order_book(ticker, levels=max(20, depth))
        if str(side).upper() == 'SELL':
            levels = book.get('bids', [])
        else:
            levels = book.get('asks', [])

        levels = list(levels)[:depth]
        top1_qty = self._visible_level_qty(levels[0]) if levels else 0.0
        top5_vol = sum(self._visible_level_qty(lvl) for lvl in levels)
        return levels, float(top1_qty), float(top5_vol)

    def _expected_vwap(self, levels, slice_qty):
        need = float(max(0, int(slice_qty)))
        if need <= 0:
            return None

        cost = 0.0
        filled = 0.0
        for lvl in levels:
            avail = self._visible_level_qty(lvl)
            if avail <= 0:
                continue
            take = min(avail, need - filled)
            if take <= 0:
                break
            try:
                px = float(lvl['price'])
            except Exception:
                continue
            cost += px * take
            filled += take
            if filled >= need:
                break

        if filled <= 0:
            return None
        return cost / filled

    def _edge_per_share(self, side, tender_price, expected_vwap):
        if tender_price is None or expected_vwap is None:
            return None
        tender_price = float(tender_price)
        expected_vwap = float(expected_vwap)
        fee = 2.0 * float(self.transaction_fee)

        if str(side).upper() == 'SELL':
            return (expected_vwap - tender_price) - fee
        else:
            return (tender_price - expected_vwap) - fee

    def _slippage_per_share(self, side, tender_price, expected_vwap):
        if tender_price is None or expected_vwap is None:
            return None
        tender_price = float(tender_price)
        expected_vwap = float(expected_vwap)

        if str(side).upper() == 'SELL':
            return tender_price - expected_vwap
        else:
            return expected_vwap - tender_price

    def _top_of_book(self, ticker):
        book = self.get_order_book(ticker, levels=10)
        best_bid = None
        best_ask = None
        if book.get('bids'):
            best_bid = float(book['bids'][0]['price'])
        if book.get('asks'):
            best_ask = float(book['asks'][0]['price'])
        return best_bid, best_ask, book

    def _market_slice_qty(self, ticker, side, remaining_inventory, max_trade_size):
        remaining_inventory = int(abs(int(remaining_inventory)))
        if remaining_inventory <= 0:
            return 0, None

        levels, top1_qty, top5_vol = self._book_side_levels(ticker, side, depth=5)

        slice_qty = min(
            int(max_trade_size),
            int(max(1, round(0.15 * float(top5_vol)))),
            int(max(1, round(0.30 * float(remaining_inventory)))),
        )

        tob_cap = int(max(1, round(float(top1_qty) * float(self.market_tob_mult))))
        inv_cap = int(max(1, round(0.25 * float(remaining_inventory))))
        cap = min(inv_cap, tob_cap)

        slice_qty = max(1, min(int(slice_qty), int(cap), int(remaining_inventory)))
        return slice_qty, levels

    def _passive_limit_work(self, symbol, side, max_slice, tender_price):
        remaining_inventory = abs(int(self.get_all_positions().get(symbol, 0)))
        if remaining_inventory <= 0:
            return

        reprices = 0
        last_posted_price = None

        while remaining_inventory > 0 and reprices < int(self.phase2_max_reprices):
            if self.get_remaining_time() <= 60:
                return

            best_bid, best_ask, _ = self._top_of_book(symbol)
            if best_bid is None or best_ask is None:
                return

            spread = float(best_ask - best_bid)
            if spread > float(self.phase2_max_spread):
                return

            if side == 'SELL':
                price = float(best_ask)
            else:
                price = float(best_bid)

            if last_posted_price is not None and abs(price - last_posted_price) < 1e-9:
                if side == 'SELL':
                    price = max(float(best_bid), price - float(self.phase2_min_tick))
                else:
                    price = min(float(best_ask), price + float(self.phase2_min_tick))

            last_posted_price = price

            if reprices > 0:
                self.cancel_all_orders(reason="reprice")

            slice_qty = min(int(remaining_inventory), int(max_slice))

            self.place_order(symbol, side, slice_qty, price=price, order_type="LIMIT", reason="unwind")

            time.sleep(float(self.phase2_check_sleep))
            pos_now = int(self.get_all_positions().get(symbol, 0))
            remaining_inventory = abs(pos_now)
            if remaining_inventory == 0:
                return

            reprices += 1

    def execute_unwind(self, symbol, side, qty):
        symbol = str(symbol)
        side = str(side).upper()
        total_qty = int(abs(int(qty)))
        if total_qty <= 0:
            return

        positions = self.get_all_positions()
        live_pos = int(positions.get(symbol, 0))
        if live_pos == 0:
            return

        side = 'SELL' if live_pos > 0 else 'BUY'
        tender_price = self.last_tender_price.get(symbol)

        max_slice = self._effective_max_trade_size(symbol, total_qty)

        # UNWIND_START
        pos_before = live_pos
        self._audit_event(
            "UNWIND_START",
            ticker=symbol,
            position_before=pos_before,
            method="MIXED",
            details=f"side={side};qty={total_qty}",
        )
        self._unwind_started_ts_by_ticker[symbol] = time.time()

        consecutive_failures = 0
        edge_blocked = False

        while True:
            remaining_time = self.get_remaining_time()
            pos_now = int(self.get_all_positions().get(symbol, 0))
            remaining_inventory = abs(pos_now)
            if remaining_inventory <= 0:
                break

            if remaining_time <= 60:
                slice_qty, levels = self._market_slice_qty(symbol, side, remaining_inventory, max_slice)
                expected_vwap = self._expected_vwap(levels or [], slice_qty)

                order = self.place_order(symbol, side, slice_qty, order_type="MARKET", reason="unwind_force_last_minute")

                if not order:
                    consecutive_failures += 1
                    self._sleep_with_backoff(consecutive_failures)
                else:
                    consecutive_failures = 0
                    time.sleep(float(self.sleep_between_orders))
                continue

            side = 'SELL' if pos_now > 0 else 'BUY'
            if tender_price is None:
                edge_blocked = True

            slice_qty, levels = self._market_slice_qty(symbol, side, remaining_inventory, max_slice)
            expected_vwap = self._expected_vwap(levels or [], slice_qty)
            edge_ps = self._edge_per_share(side, tender_price, expected_vwap)

            if edge_blocked or (edge_ps is not None and edge_ps <= 0):
                edge_blocked = True
                self._flag(
                    "SUSPECT_PASSIVE_ORDER",
                    ticker=symbol,
                    flag_details=f"edge_guardrail_active edge_ps={edge_ps}",
                )
                self._passive_limit_work(symbol, side, max_slice, tender_price)

                if tender_price is None:
                    time.sleep(0.20)
                    continue

                pos_now = int(self.get_all_positions().get(symbol, 0))
                remaining_inventory = abs(pos_now)
                if remaining_inventory <= 0:
                    break

                slice_qty, levels = self._market_slice_qty(symbol, side, remaining_inventory, max_slice)
                expected_vwap = self._expected_vwap(levels or [], slice_qty)
                edge_ps = self._edge_per_share(side, tender_price, expected_vwap)
                if edge_ps is not None and edge_ps > 0:
                    edge_blocked = False
                else:
                    time.sleep(0.20)
                    continue

            order = self.place_order(symbol, side, slice_qty, order_type="MARKET", reason="unwind")

            if not order:
                consecutive_failures += 1
                if consecutive_failures >= int(self.max_consecutive_order_failures):
                    self._sleep_with_backoff(consecutive_failures)
                    edge_blocked = True
                else:
                    self._sleep_with_backoff(consecutive_failures)
                continue

            consecutive_failures = 0
            time.sleep(float(self.sleep_between_orders))

        pos_after = int(self.get_all_positions().get(symbol, 0))

        # Long unwind detector
        if self.DETECT_UNWIND_LONG_SEC is None:
            self._warn_threshold_missing_once(
                "unwind_long",
                "DETECT_UNWIND_LONG_SEC is unset; long-unwind detector disabled",
            )
        else:
            start_ts = self._unwind_started_ts_by_ticker.get(symbol)
            if start_ts is not None:
                dur = time.time() - float(start_ts)
                if dur > float(self.DETECT_UNWIND_LONG_SEC):
                    self._flag(
                        "SUSPECT_UNWIND_SLOW",
                        ticker=symbol,
                        flag_details=f"unwind_sec={dur:.3f} > {self.DETECT_UNWIND_LONG_SEC}",
                    )

        # Position held too long after tender
        if self.DETECT_POSITION_HELD_SEC is None:
            self._warn_threshold_missing_once(
                "pos_held",
                "DETECT_POSITION_HELD_SEC is unset; position-held detector disabled",
            )
        else:
            t0 = self._tender_accept_ts_by_ticker.get(symbol)
            if t0 is not None:
                held = time.time() - float(t0)
                if held > float(self.DETECT_POSITION_HELD_SEC):
                    self._flag(
                        "SUSPECT_TENDER_VIOLATION",
                        ticker=symbol,
                        flag_details=f"held_sec={held:.3f} > {self.DETECT_POSITION_HELD_SEC}",
                    )

        # UNWIND_END
        self._audit_event(
            "UNWIND_END",
            ticker=symbol,
            position_before=pos_before,
            position_after=pos_after,
            method="MIXED",
        )

        # Clear tender lifecycle for this ticker if flat
        if pos_after == 0 and self._active_tender_ticker == symbol:
            self._active_tender_id = None
            self._active_tender_ticker = None
            self._active_tender_started_ts = None
            self._tender_accept_ts_by_ticker.pop(symbol, None)

    def execute_unwind_plan(self, ticker, unwind_plan, total_qty):
        positions_before = self.get_all_positions()
        pos_before = positions_before.get(ticker, 0)

        self.execute_unwind(ticker, 'SELL' if int(pos_before) > 0 else 'BUY', abs(int(total_qty)))

    def manage_positions(self):
        remaining = self.get_remaining_time()
        positions = self.get_all_positions()

        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] Managing {ticker}: {position} (Time left: {remaining}s)")
                side = 'SELL' if int(position) > 0 else 'BUY'
                self.execute_unwind(ticker, side, abs(int(position)))

    def force_close_all_positions(self):
        print(f"[{self._timestamp()}] FORCE CLOSING ALL POSITIONS")

        self.cancel_all_orders(reason="force_close")
        time.sleep(0.5)

        positions = self.get_all_positions()

        for ticker, position in positions.items():
            if position != 0:
                self._audit_event(
                    "FORCE_CLOSE",
                    ticker=ticker,
                    quantity=abs(position),
                    reason="time_expiry_or_shutdown",
                )

                if position > 0:
                    self.place_order(ticker, "SELL", abs(position), order_type="MARKET", reason="force_close")
                else:
                    self.place_order(ticker, "BUY", abs(position), order_type="MARKET", reason="force_close")

                time.sleep(0.2)

        time.sleep(1)
        positions = self.get_all_positions()
        for ticker, position in positions.items():
            if position != 0:
                if position > 0:
                    self.place_order(ticker, "SELL", abs(position), order_type="MARKET", reason="force_close_retry")
                else:
                    self.place_order(ticker, "BUY", abs(position), order_type="MARKET", reason="force_close_retry")

    def run(self):
        print(f"[{self._timestamp()}] Starting Liability Trader (AUDIT MVP)...")
        print("Press Ctrl+C to stop")

        self.get_case_info()

        try:
            while True:
                remaining = self.get_remaining_time()
                if remaining <= 0:
                    print(f"[{self._timestamp()}] ⚠ TIME'S UP! Closing all positions...")
                    self.audit_fine_snapshot(context="time_up")
                    self.force_close_all_positions()
                    break

                if int(remaining) % 30 == 0 and remaining > 0:
                    print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")

                tenders = self.get_tender_offers()

                for tender in tenders:
                    self.tenders_received += 1
                    ticker = tender['ticker']
                    qty = tender['quantity']
                    price = tender['price']
                    tender_id = tender['tender_id']

                    # TENDER_RECEIVED
                    self._audit_event(
                        "TENDER_RECEIVED",
                        tender_id=tender_id,
                        ticker=ticker,
                        quantity=qty,
                        price=price,
                    )

                    print(f"\n[{self._timestamp()}] {'='*50}")
                    print(f"[{self._timestamp()}] TENDER #{self.tenders_received}")
                    print(f"[{self._timestamp()}] {ticker}: {qty:+} shares @ ${price:.2f}")
                    print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")

                    accept, profit, reason, unwind_plan = self.evaluate_tender(tender)

                    # Logging-only estimated edge
                    est_edge = ""
                    try:
                        est_edge = float(profit) / float(abs(int(qty)))
                    except Exception:
                        est_edge = ""

                    if accept:
                        decision_type = "TENDER_ACCEPTED"
                    else:
                        decision_type = "TENDER_DECLINED"

                    self._audit_event(
                        decision_type,
                        tender_id=tender_id,
                        ticker=ticker,
                        quantity=qty,
                        price=price,
                        decision_reason=reason,
                        estimated_edge=est_edge,
                        estimated_profit=profit,
                    )

                    print(f"[{self._timestamp()}] Analysis: {reason}")
                    print(f"[{self._timestamp()}] Decision: {'ACCEPT' if accept else 'DECLINE'}")

                    self.audit_fine_snapshot(context=f"tender_decision:{tender_id}")

                    if accept:
                        if self.accept_tender(tender_id):
                            self.tenders_accepted += 1
                            self.total_pnl += profit

                            # Mark tender lifecycle active
                            self._active_tender_id = tender_id
                            self._active_tender_ticker = ticker
                            self._active_tender_started_ts = time.time()
                            self._tender_accept_ts_by_ticker[ticker] = time.time()

                            self.last_tender_price[ticker] = float(price)

                            time.sleep(0.5)
                            self.execute_unwind_plan(ticker, unwind_plan, qty)
                    else:
                        self.decline_tender(tender_id)

                    print(f"[{self._timestamp()}] Accepted: {self.tenders_accepted}/{self.tenders_received}")
                    print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")

                if remaining <= 120:
                    if int(time.time()) % 5 == 0:
                        self.manage_positions()
                else:
                    if int(time.time()) % 15 == 0:
                        self.manage_positions()

                time.sleep(0.5)

        except KeyboardInterrupt:
            print(f"\n[{self._timestamp()}] Manual shutdown requested...")
            self._audit_event("RUN_STOP", details="keyboard_interrupt")

        finally:
            print(f"\n[{self._timestamp()}] =========================================")
            print(f"[{self._timestamp()}] FINAL CLEANUP - ENSURING ZERO POSITIONS")
            print(f"[{self._timestamp()}] =========================================")

            self.audit_fine_snapshot(context="final_cleanup_start")
            self.force_close_all_positions()

            time.sleep(2)
            positions = self.get_all_positions()
            all_closed = True
            for ticker, position in positions.items():
                if position != 0:
                    all_closed = False

            if all_closed:
                self._audit_event("RUN_END", details="all_positions_flat")
            else:
                self._audit_event("RUN_END", details="positions_remaining_after_cleanup")

            self.audit_fine_snapshot(context="final_cleanup_end")

            try:
                self._audit_fp.flush()
                self._audit_fp.close()
            except Exception:
                pass


def main():
    print("=== RCFA Liability Trading Bot (AUDIT MVP) ===")
    print("IMPORTANT: This bot will FORCE CLOSE all positions at the end")
    print("Make sure RIT client is running and connected!")
    print("DO NOT run concurrently with any other trader.")
    print()

    api_key = input("Enter your RIT API Key (get from RIT client): ").strip()

    if not api_key:
        print("No API key provided. Using default 'YOUR_API_KEY'")
        api_key = "YOUR_API_KEY"

    trader = LiabilityTrader(api_key=api_key)
    trader.run()


if __name__ == "__main__":
    main()
