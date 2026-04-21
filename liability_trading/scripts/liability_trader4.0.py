import time
from datetime import datetime

import requests


class LiabilityTrader:
    """LT3 Liability Trading — compliance-first tender unwind bot.

    This file is a control-flow + execution refactor of `liabilty_trader2.0.py`.

    CRITICAL COMPLIANCE INTENT
    - Trade ONLY to clean up inventory created by an ACCEPTED tender.
    - Never look like market making or speculative trading.

    Architecture: strict finite state machine (FSM)
      IDLE      -> no inventory, place ZERO orders
      UNWIND    -> unwind accepted-tender inventory, one direction only
      EMERGENCY -> last-window cleanup: cancel all, MARKET only, flatten everything

    Tender evaluation/acceptance logic is preserved from the source.
    Only the control flow and order execution rules are changed.
    """

    # --- FSM states ---
    STATE_IDLE = "IDLE"
    STATE_UNWIND = "UNWIND"
    STATE_EMERGENCY = "EMERGENCY"

    def __init__(self, api_key, base_url="http://localhost:9999/v1"):
        self.session = requests.Session()
        self.session.headers.update({'X-API-Key': api_key})
        self.base_url = base_url
        self.transaction_fee = 0.02
        self.max_order_sizes = {'CRZY': 25000, 'TAME': 10000}

        # Risk limits (preserved; not redesigned)
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

        # =====================
        # Execution knobs (ONLY execution/control flow)
        # =====================

        # EMERGENCY window: last ~60s
        self.emergency_threshold_sec = 60

        # UNWIND policy: market-first portion (60–80%)
        self.market_first_fraction = 0.60

        # LIMIT safety: any limit order must be short-lived
        self.limit_timeout_sec = 1.2

        # pacing (avoid overwhelming API)
        self.sleep_between_orders = 0.10
        self.position_settle_sleep = 0.20

        # Internal: last order status for execution logic
        self._last_order_status_code = None
        self._last_order_error_text = None

        # =====================
        # FSM state (NEW)
        # =====================

        # Explicit state variable
        self.state = self.STATE_IDLE

        # Active unwind context (valid only in UNWIND)
        self.active_unwind_ticker = None
        self.unwind_side = None  # locked when entering UNWIND
        self.unwind_initial_abs = None

        # Recovery unwind flag (NEW; compliance): if we enter UNWIND due to
        # unexpected existing inventory, we will unwind MARKET-only.
        self.recovery_unwind = False

        print(f"[{self._timestamp()}] Liability Trader 4.0 initialized")

    def _timestamp(self):
        return datetime.now().strftime("%H:%M:%S")

    # =====================
    # Case/time (preserved)
    # =====================

    def get_case_info(self):
        """Get case information including remaining time"""
        try:
            resp = self.session.get(f'{self.base_url}/case')
            if resp.status_code == 200:
                case = resp.json()
                if self.end_time is None:
                    # Set end time (5 minutes = 300 seconds from start)
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
        return 300  # Default 5 minutes

    def check_time_warning(self):
        """Check if we're running out of time.

        Compliance requirement:
        - Must be side-effect free (NO order placement / cancels).
        - Must return ONLY: "normal", "aggressive", or "emergency".
        """
        remaining = self.get_remaining_time()

        if remaining <= self.emergency_threshold_sec:
            if not self.time_warning_issued:
                print(f"[{self._timestamp()}] ⚠ LAST MINUTE WARNING: {remaining}s remaining")
                print(f"[{self._timestamp()}] Switching to EMERGENCY via FSM")
                self.time_warning_issued = True
            return "emergency"

        if remaining <= 120:
            return "aggressive"

        return "normal"

    # =====================
    # Orders/positions (preserved endpoints)
    # =====================

    def get_all_positions(self):
        """Get all current positions"""
        try:
            resp = self.session.get(f'{self.base_url}/securities')
            if resp.status_code == 200:
                securities = resp.json()
                positions = {}
                for sec in securities:
                    positions[sec['ticker']] = sec.get('position', 0)
                self.positions = positions
                return positions
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting positions: {e}")
        return self.positions

    def cancel_all_orders(self):
        """Cancel all active orders

        Compliance rationale:
        - Prevents resting orders and reduces risk of fines.
        - Called immediately upon entering UNWIND and EMERGENCY.
        """
        print(f"[{self._timestamp()}] Cancelling all open orders...")

        try:
            resp = self.session.get(f'{self.base_url}/orders')
            if resp.status_code == 200:
                orders = resp.json()
                for order in orders:
                    order_id = order.get('order_id')
                    if order_id:
                        try:
                            cancel_resp = self.session.delete(f'{self.base_url}/orders/{order_id}')
                            if cancel_resp.status_code == 200:
                                print(f"[{self._timestamp()}] ✓ Canceled order {order_id}")
                            else:
                                print(f"[{self._timestamp()}] ✗ Failed to cancel order {order_id}")
                        except Exception:
                            pass
        except Exception as e:
            print(f"[{self._timestamp()}] Error cancelling orders: {e}")

        self.active_orders = []

    def place_order(self, ticker, action, qty, price=None, order_type="LIMIT"):
        """Place an order (RIT client REST API expects query params)"""
        if qty <= 0:
            return None

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

                price_str = f"@ ${price:.2f}" if price else "@ MARKET"
                print(f"[{self._timestamp()}] ✓ Placed {order_type} order: {action} {slice_qty} {ticker} {price_str}")
                return order_info

            print(f"[{self._timestamp()}] ✗ Order failed: HTTP {resp.status_code}")

        except Exception as e:
            print(f"[{self._timestamp()}] Error placing order: {e}")

        return None

    def get_order_book(self, ticker, levels=20):
        """Get order book for a specific ticker"""
        try:
            resp = self.session.get(f'{self.base_url}/securities/book', params={'ticker': ticker})
            if resp.status_code == 200:
                book = resp.json()
                bids = sorted(book.get('bids', []), key=lambda x: x['price'], reverse=True)[:levels]
                asks = sorted(book.get('asks', []), key=lambda x: x['price'])[:levels]
                return {'bids': bids, 'asks': asks}
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting order book: {e}")
        return {'bids': [], 'asks': []}

    def get_tender_offers(self):
        """Check for new tender offers"""
        try:
            resp = self.session.get(f'{self.base_url}/tenders')
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"[{self._timestamp()}] Error getting tenders: {e}")
        return []

    def accept_tender(self, tender_id):
        """Accept a tender offer"""
        try:
            resp = self.session.post(f'{self.base_url}/tenders/{tender_id}', json={'status': 'ACCEPTED'})
            if resp.status_code == 200:
                print(f"[{self._timestamp()}] ✓ Tender {tender_id} accepted")
                return True
        except Exception as e:
            print(f"[{self._timestamp()}] Error accepting tender: {e}")
        return False

    def decline_tender(self, tender_id):
        """Decline a tender offer"""
        try:
            resp = self.session.post(f'{self.base_url}/tenders/{tender_id}', json={'status': 'DECLINED'})
            if resp.status_code == 200:
                print(f"[{self._timestamp()}] ✗ Tender {tender_id} declined")
                return True
        except Exception:
            pass
        return False

    # =====================
    # Tender evaluation (preserved)
    # =====================

    def evaluate_tender(self, tender):
        """Evaluate tender offer - returns (accept, profit, reason, unwind_plan)"""
        ticker = tender['ticker']
        qty = tender['quantity']
        price = tender['price']

        remaining = self.get_remaining_time()

        # FIX 4 — Hard tender rejection window near EMERGENCY.
        if remaining <= (self.emergency_threshold_sec + 10):
            return (False, 0, "REJECTED: too close to emergency window", [])

        time_mode = self.check_time_warning()

        book = self.get_order_book(ticker)

        if qty > 0:  # They SELL to us
            return self._analyze_buy_tender(ticker, qty, price, book['asks'], time_mode, remaining)
        # They BUY from us
        return self._analyze_sell_tender(ticker, abs(qty), price, book['bids'], time_mode, remaining)

    def _analyze_buy_tender(self, ticker, block_qty, block_price, asks, time_mode, remaining):
        """Analyze tender where we BUY from client"""
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
        """Analyze tender where we SELL to client"""
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

    # =====================
    # FSM transitions (NEW)
    # =====================

    def _enter_unwind(self, ticker):
        """Enter UNWIND after accepting a tender that created inventory.

        Invariants:
        - Cancel all outstanding orders immediately.
        - Lock unwind_side once (never change until flat).
        """
        self.cancel_all_orders()

        positions = self.get_all_positions()
        pos = int(positions.get(ticker, 0))
        if pos == 0:
            # No inventory (unexpected), remain idle.
            self.state = self.STATE_IDLE
            self.active_unwind_ticker = None
            self.unwind_side = None
            self.unwind_initial_abs = None
            return

        self.state = self.STATE_UNWIND
        self.active_unwind_ticker = str(ticker)
        self.unwind_side = "SELL" if pos > 0 else "BUY"  # direction lock
        self.unwind_initial_abs = abs(pos)

        print(
            f"[{self._timestamp()}] [FSM] -> UNWIND ticker={self.active_unwind_ticker} "
            f"pos={pos} side_locked={self.unwind_side}"
        )

    def _enter_emergency(self, reason):
        """Enter EMERGENCY.

        Rules:
        - Reject all new tenders.
        - Cancel all outstanding orders.
        - MARKET orders only.
        - Flatten all positions immediately.
        """
        if self.state != self.STATE_EMERGENCY:
            print(f"[{self._timestamp()}] [FSM] -> EMERGENCY ({reason})")
        self.state = self.STATE_EMERGENCY

        self.cancel_all_orders()
        self.force_close_all_positions()

    # =====================
    # Execution safety checks (NEW; centralized)
    # =====================

    def _estimate_position_after(self, pos_before, side, qty):
        side = str(side).upper()
        qty = int(qty)
        if side == "SELL":
            return int(pos_before) - qty
        if side == "BUY":
            return int(pos_before) + qty
        return int(pos_before)

    def _safe_send_unwind_order(self, ticker, side, qty, order_type, price=None):
        """Centralized absolute safety check.

        Before sending ANY order, verify:
          abs(position_after) < abs(position_before)

        Also enforces:
        - one-direction unwind only
        - overshoot protection (never cross zero)
        """
        if self.state != self.STATE_UNWIND:
            return None

        ticker = str(ticker)
        side = str(side).upper()

        if ticker != self.active_unwind_ticker:
            return None

        # Direction lock: once in UNWIND, side must never change.
        if self.unwind_side is None or side != self.unwind_side:
            print(f"[{self._timestamp()}] [SAFE] blocked: side change attempted ({side} != {self.unwind_side})")
            return None

        positions = self.get_all_positions()
        pos_before = int(positions.get(ticker, 0))

        # One-direction unwind only: SELL only if long; BUY only if short.
        if side == "SELL" and pos_before <= 0:
            print(f"[{self._timestamp()}] [SAFE] blocked: SELL while pos={pos_before}")
            return None
        if side == "BUY" and pos_before >= 0:
            print(f"[{self._timestamp()}] [SAFE] blocked: BUY while pos={pos_before}")
            return None

        # Overshoot protection: clamp so we never cross zero.
        qty = int(min(int(qty), abs(pos_before)))
        if qty <= 0:
            return None

        pos_after_est = self._estimate_position_after(pos_before, side, qty)

        # Absolute safety check.
        if abs(pos_after_est) >= abs(pos_before):
            print(
                f"[{self._timestamp()}] [SAFE] blocked: abs(after) not < abs(before) "
                f"before={pos_before} after_est={pos_after_est} qty={qty} side={side}"
            )
            return None

        return self.place_order(ticker, side, qty, price=price, order_type=order_type)

    # =====================
    # Unwind execution (REPLACED; rules-focused)
    # =====================

    def _unwind_step(self):
        """Perform a single compliance-safe unwind step.

        Execution rules:
        - Direction lock: do not flip sides.
        - Market-first: first 60–80% MARKET.
        - Remaining: optional LIMIT with strict timer, otherwise MARKET.
        - Never increase absolute position.
        """
        if self.state != self.STATE_UNWIND:
            return

        ticker = self.active_unwind_ticker
        if not ticker:
            self.state = self.STATE_IDLE
            return

        remaining_time = self.get_remaining_time()
        if remaining_time <= self.emergency_threshold_sec:
            self._enter_emergency(reason=f"time_remaining={remaining_time}")
            return

        positions = self.get_all_positions()
        pos = int(positions.get(ticker, 0))

        if pos == 0:
            print(f"[{self._timestamp()}] [FSM] UNWIND complete -> IDLE")
            self.state = self.STATE_IDLE
            self.active_unwind_ticker = None
            self.unwind_side = None
            self.unwind_initial_abs = None
            self.recovery_unwind = False
            return

        # If position sign contradicts the locked side, stop and clean up.
        if (self.unwind_side == "SELL" and pos < 0) or (self.unwind_side == "BUY" and pos > 0):
            self._enter_emergency(reason=f"unexpected_sign_flip pos={pos} side_locked={self.unwind_side}")
            return

        abs_pos = abs(pos)
        if self.unwind_initial_abs is None:
            self.unwind_initial_abs = abs_pos

        # Decide whether we are still in the market-first phase.
        limit_phase_threshold = max(1, int(round((1.0 - float(self.market_first_fraction)) * float(self.unwind_initial_abs))))
        in_market_phase = abs_pos > limit_phase_threshold

        # Conservative slice size to look like pure cleanup (not aggressive trading).
        # Clamp to <=25% of current inventory to avoid large single prints.
        slice_qty = max(1, int(round(0.25 * float(abs_pos))))
        slice_qty = min(slice_qty, abs_pos)

        if in_market_phase:
            # MARKET-only in market-first phase.
            print(f"[{self._timestamp()}] [UNWIND] MARKET phase ticker={ticker} pos={pos} slice={slice_qty}")
            self._safe_send_unwind_order(ticker, self.unwind_side, slice_qty, order_type="MARKET")
            time.sleep(float(self.position_settle_sleep))
            time.sleep(float(self.sleep_between_orders))
            return

        # FIX 5 — Recovery unwind must be MARKET-ONLY.
        if self.recovery_unwind:
            print(f"[{self._timestamp()}] [UNWIND] RECOVERY MARKET-only ticker={ticker} pos={pos} slice={slice_qty}")
            self._safe_send_unwind_order(ticker, self.unwind_side, slice_qty, order_type="MARKET")
            time.sleep(float(self.position_settle_sleep))
            time.sleep(float(self.sleep_between_orders))
            return

        # LIMIT phase: only if time allows, and never resting.
        # We use a marketable LIMIT (crossing the spread) to reduce resting risk.
        best_bid, best_ask, _ = self._top_of_book(ticker)
        limit_price = None

        if self.unwind_side == "SELL" and best_bid is not None:
            limit_price = float(best_bid) - 0.01
        elif self.unwind_side == "BUY" and best_ask is not None:
            limit_price = float(best_ask) + 0.01

        if limit_price is None:
            print(f"[{self._timestamp()}] [UNWIND] LIMIT phase but no book -> MARKET ticker={ticker} pos={pos} slice={slice_qty}")
            self._safe_send_unwind_order(ticker, self.unwind_side, slice_qty, order_type="MARKET")
            time.sleep(float(self.position_settle_sleep))
            return

        print(
            f"[{self._timestamp()}] [UNWIND] LIMIT phase ticker={ticker} pos={pos} "
            f"slice={slice_qty} px={limit_price:.2f} (timeout={self.limit_timeout_sec}s)"
        )

        # Cancel any stale orders before posting a new short-lived limit.
        self.cancel_all_orders()

        self._safe_send_unwind_order(ticker, self.unwind_side, slice_qty, order_type="LIMIT", price=limit_price)

        # Strict timer: ALWAYS cancel then finish the slice with MARKET.
        time.sleep(float(self.limit_timeout_sec))

        print(f"[{self._timestamp()}] [UNWIND] LIMIT timeout -> cancel + MARKET")
        self.cancel_all_orders()
        self._safe_send_unwind_order(ticker, self.unwind_side, slice_qty, order_type="MARKET")

        # Avoid tight loops
        time.sleep(float(self.position_settle_sleep))
        time.sleep(float(self.sleep_between_orders))

    def _top_of_book(self, ticker):
        book = self.get_order_book(ticker, levels=10)
        best_bid = None
        best_ask = None
        if book.get('bids'):
            best_bid = float(book['bids'][0]['price'])
        if book.get('asks'):
            best_ask = float(book['asks'][0]['price'])
        return best_bid, best_ask, book

    # =====================
    # EMERGENCY cleanup (preserved behavior)
    # =====================

    def force_close_all_positions(self):
        """Force close ALL positions with market orders"""
        print(f"[{self._timestamp()}] FORCE CLOSING ALL POSITIONS")

        self.cancel_all_orders()
        time.sleep(0.5)

        positions = self.get_all_positions()

        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] Force closing {ticker}: {position}")
                if position > 0:
                    self.place_order(ticker, "SELL", abs(position), order_type="MARKET")
                else:
                    self.place_order(ticker, "BUY", abs(position), order_type="MARKET")
                time.sleep(0.2)

        time.sleep(1)
        positions = self.get_all_positions()
        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] ⚠ Still have position: {ticker} = {position}")
                if position > 0:
                    self.place_order(ticker, "SELL", abs(position), order_type="MARKET")
                else:
                    self.place_order(ticker, "BUY", abs(position), order_type="MARKET")

    # =====================
    # Main loop (FSM; refactored)
    # =====================

    def run(self):
        print(f"[{self._timestamp()}] Starting Liability Trader 4.0...")
        print("Press Ctrl+C to stop")

        self.get_case_info()

        try:
            while True:
                remaining = self.get_remaining_time()

                # --- Global guard (MOST IMPORTANT) ---
                # If there is NO active tender inventory and state != EMERGENCY:
                #   -> place ZERO orders.
                if self.state != self.STATE_EMERGENCY:
                    # Recovery safeguard: if we somehow have inventory, switch to UNWIND.
                    positions = self.get_all_positions()
                    if self.state == self.STATE_IDLE:
                        nonzero = [(t, int(p)) for t, p in positions.items() if int(p) != 0]
                        if nonzero:
                            # We treat this as cleanup inventory (never speculative).
                            t0, _p0 = nonzero[0]
                            print(f"[{self._timestamp()}] [FSM] Detected existing position -> UNWIND recovery ticker={t0}")
                            self.recovery_unwind = True
                            self._enter_unwind(t0)

                # Case end
                if remaining <= 0:
                    print(f"[{self._timestamp()}] ⚠ TIME'S UP! Closing all positions...")
                    self._enter_emergency(reason="time_up")
                    break

                # EMERGENCY entry
                if remaining <= self.emergency_threshold_sec:
                    self._enter_emergency(reason=f"time_remaining={remaining}")
                    break

                # Show time remaining periodically
                if int(remaining) % 30 == 0 and remaining > 0:
                    print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")

                if self.state == self.STATE_IDLE:
                    # IDLE rules:
                    # - Place ZERO orders
                    # - Ignore new orders/market activity
                    # - Only poll tenders and check time
                    tenders = self.get_tender_offers()

                    for tender in tenders:
                        # If we somehow transitioned, stop processing.
                        if self.state != self.STATE_IDLE:
                            break

                        self.tenders_received += 1
                        ticker = tender['ticker']
                        qty = tender['quantity']
                        price = tender['price']
                        tender_id = tender['tender_id']

                        print(f"\n[{self._timestamp()}] {'='*50}")
                        print(f"[{self._timestamp()}] TENDER #{self.tenders_received}")
                        print(f"[{self._timestamp()}] {ticker}: {qty:+} shares @ ${price:.2f}")
                        print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")

                        accept, profit, reason, _unwind_plan = self.evaluate_tender(tender)

                        print(f"[{self._timestamp()}] Analysis: {reason}")
                        print(f"[{self._timestamp()}] Decision: {'ACCEPT' if accept else 'DECLINE'}")

                        if accept:
                            if self.accept_tender(tender_id):
                                self.tenders_accepted += 1
                                self.total_pnl += profit

                                # Tender accepted -> transition to UNWIND.
                                time.sleep(0.5)  # Wait for position update
                                self._enter_unwind(ticker)
                                break
                        else:
                            self.decline_tender(tender_id)

                        print(f"[{self._timestamp()}] Accepted: {self.tenders_accepted}/{self.tenders_received}")
                        print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")

                    time.sleep(0.5)
                    continue

                if self.state == self.STATE_UNWIND:
                    # UNWIND rules:
                    # - Ignore new tenders
                    # - One-direction unwind only
                    # - Never increase abs(position)
                    self._unwind_step()
                    continue

                # EMERGENCY state handled by transitions; keep loop safe.
                time.sleep(0.25)

        except KeyboardInterrupt:
            print(f"\n[{self._timestamp()}] Manual shutdown requested...")

        finally:
            print(f"\n[{self._timestamp()}] =========================================")
            print(f"[{self._timestamp()}] FINAL CLEANUP - ENSURING ZERO POSITIONS")
            print(f"[{self._timestamp()}] =========================================")

            # Always finish flat.
            self._enter_emergency(reason="final_cleanup")

            time.sleep(2)
            positions = self.get_all_positions()
            print(f"\n[{self._timestamp()}] FINAL POSITION CHECK:")
            all_closed = True
            for ticker, position in positions.items():
                print(f"[{self._timestamp()}] {ticker}: {position}")
                if int(position) != 0:
                    all_closed = False
                    print(f"[{self._timestamp()}] ⚠ WARNING: {ticker} still has position {position}")

            if all_closed:
                print(f"[{self._timestamp()}] ✓ SUCCESS: All positions closed!")
            else:
                print(f"[{self._timestamp()}] ⚠ FAILED: Some positions remain open")
                self.force_close_all_positions()

            print(f"\n[{self._timestamp()}] FINAL STATS:")
            print(f"[{self._timestamp()}] Tenders: {self.tenders_accepted}/{self.tenders_received}")
            print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")
            print(f"[{self._timestamp()}] Runtime: {time.time() - self.start_time:.1f}s")

            positions = self.get_all_positions()
            for ticker, position in positions.items():
                if int(position) != 0:
                    print(f"\n[{self._timestamp()}] ❗❗❗ CRITICAL WARNING ❗❗❗")
                    print(f"[{self._timestamp()}] {ticker} position: {position} NOT CLOSED")
                    print(f"[{self._timestamp()}] YOU WILL BE PENALIZED!")
                    print(f"[{self._timestamp()}] Manually close position in RIT client NOW!")


def main():
    print("=== RCFA Liability Trading Bot 4.0 ===")
    print("IMPORTANT: This bot will FORCE CLOSE all positions at the end")
    print("Compliance-first: trades only to unwind accepted tender inventory")
    print("Make sure RIT client is running and connected!")
    print()

    api_key = input("Enter your RIT API Key (get from RIT client): ").strip()

    if not api_key:
        print("No API key provided. Using default 'YOUR_API_KEY'")
        api_key = "YOUR_API_KEY"

    trader = LiabilityTrader(api_key=api_key)
    trader.run()


if __name__ == "__main__":
    main()
