import requests
import time
from datetime import datetime


class LiabilityTrader:
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

        # =====================
        # EXECUTION-ONLY KNOBS
        # (Tender acceptance logic must remain unchanged)
        # =====================

        # PHASE 1: immediate lock-in fraction (30–50%)
        self.phase1_fraction = 0.40

        # PHASE 2: passive reprice controls
        self.phase2_max_reprices = 8
        self.phase2_check_sleep = 0.20
        self.phase2_min_tick = 0.01
        self.phase2_max_spread = 0.10  # if spread wider than this, stop being passive

        # PHASE 3: failsafe cutoff controls
        self.phase3_time_cutoff_sec = 35  # if less than this remains, force MARKET exit

        # pacing (avoid overwhelming API)
        self.sleep_between_orders = 0.08

        # Backoff / safety for transient server errors (HTTP 5xx)
        self.order_backoff_base = 0.25
        self.order_backoff_max = 1.50
        self.max_consecutive_order_failures = 10
        self.position_settle_sleep = 0.20

        # Internal: last order status for execution logic
        self._last_order_status_code = None
        self._last_order_error_text = None

        print(f"[{self._timestamp()}] Liability Trader initialized")

    def _timestamp(self):
        return datetime.now().strftime("%H:%M:%S")

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
        """Check if we're running out of time and adjust strategy"""
        remaining = self.get_remaining_time()

        if remaining <= 60 and not self.time_warning_issued:  # Last minute
            print(f"[{self._timestamp()}] ⚠ LAST MINUTE WARNING: {remaining}s remaining")
            print(f"[{self._timestamp()}] Switching to aggressive closing mode")
            self.time_warning_issued = True

        elif remaining <= 30:  # Last 30 seconds
            print(f"[{self._timestamp()}] ⚠ CRITICAL: {remaining}s remaining")
            print(f"[{self._timestamp()}] Forcing position closure")
            self.force_close_all_positions()

        elif remaining <= 120:  # Last 2 minutes
            # Increase aggressiveness in last 2 minutes
            return 'aggressive'

        return 'normal'

    def force_close_all_positions(self):
        """Force close ALL positions with market orders"""
        print(f"[{self._timestamp()}] FORCE CLOSING ALL POSITIONS")

        # Cancel all open orders first
        self.cancel_all_orders()
        time.sleep(0.5)

        # Get current positions
        positions = self.get_all_positions()

        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] Force closing {ticker}: {position}")

                # Use MARKET orders for immediate execution
                if position > 0:  # Long, need to SELL
                    self.place_order(ticker, "SELL", abs(position), order_type="MARKET")
                else:  # Short, need to BUY
                    self.place_order(ticker, "BUY", abs(position), order_type="MARKET")

                # Small delay to avoid overwhelming API
                time.sleep(0.2)

        # Double check and retry if needed
        time.sleep(1)
        positions = self.get_all_positions()
        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] ⚠ Still have position: {ticker} = {position}")
                # Try one more time with larger size
                if position > 0:
                    self.place_order(ticker, "SELL", abs(position), order_type="MARKET")
                else:
                    self.place_order(ticker, "BUY", abs(position), order_type="MARKET")

    def cancel_all_orders(self):
        """Cancel all active orders"""
        print(f"[{self._timestamp()}] Cancelling all open orders...")

        # Get all active orders
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
                        except:
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
            resp = self.session.get(f'{self.base_url}/securities/book',
                                   params={'ticker': ticker})
            if resp.status_code == 200:
                book = resp.json()
                bids = sorted(book.get('bids', []),
                            key=lambda x: x['price'], reverse=True)[:levels]
                asks = sorted(book.get('asks', []),
                            key=lambda x: x['price'])[:levels]
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

    def evaluate_tender(self, tender):
        """Evaluate tender offer - returns (accept, profit, reason, unwind_plan)"""
        ticker = tender['ticker']
        qty = tender['quantity']
        price = tender['price']

        # Check time remaining - BE MORE SELECTIVE near the end
        remaining = self.get_remaining_time()
        time_mode = self.check_time_warning()

        # Get order book
        book = self.get_order_book(ticker)

        if qty > 0:  # They SELL to us
            return self._analyze_buy_tender(ticker, qty, price, book['asks'], time_mode, remaining)
        else:  # They BUY from us
            return self._analyze_sell_tender(ticker, abs(qty), price, book['bids'], time_mode, remaining)

    def _analyze_buy_tender(self, ticker, block_qty, block_price, asks, time_mode, remaining):
        """Analyze tender where we BUY from client"""
        if not asks:
            return False, 0, "No ask prices available", []

        # ADJUST STRATEGY BASED ON TIME
        if time_mode == 'aggressive' or remaining < 120:
            # Last 2 minutes: require HIGHER profit (more selective)
            min_profit = 1500  # Higher threshold
            min_per_share = 0.05
            min_liquidity = 0.7  # Need more liquidity near end
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

            profit_per_share = ask_price - block_price - 2*self.transaction_fee

            if profit_per_share <= 0 or ask_qty <= 0:
                continue

            qty = min(remaining_qty, ask_qty, self.max_order_sizes[ticker])
            level_profit = profit_per_share * qty
            profit += level_profit
            immediate_liquidity += qty

            unwind_plan.append({
                'price': ask_price,
                'quantity': qty,
                'profit': level_profit,
                'action': 'SELL'
            })

            remaining_qty -= qty

            if remaining_qty <= 0:
                break

        liquidity_coverage = immediate_liquidity / block_qty if block_qty > 0 else 0
        avg_profit = profit / block_qty if block_qty > 0 else 0

        # Decision logic with time-based thresholds
        accept = (
            profit > min_profit and
            avg_profit >= min_per_share and
            liquidity_coverage >= min_liquidity
        )

        # LAST 30 SECONDS: REJECT ALL NEW TENDERS
        if remaining <= 30:
            accept = False
            reason = f"REJECTED: Last 30 seconds - no new positions"
        else:
            reason = f"Profit: ${profit:.2f}, Avg: ${avg_profit:.4f}/share, Liquidity: {liquidity_coverage:.1%}"
            if not accept and profit > 0:
                reason += f" (Rejected: thresholds not met)"

        return accept, profit, reason, unwind_plan

    def _analyze_sell_tender(self, ticker, block_qty, block_price, bids, time_mode, remaining):
        """Analyze tender where we SELL to client"""
        if not bids:
            return False, 0, "No bid prices available", []

        # ADJUST STRATEGY BASED ON TIME
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

            profit_per_share = block_price - bid_price - 2*self.transaction_fee

            if profit_per_share <= 0 or bid_qty <= 0:
                continue

            qty = min(remaining_qty, bid_qty, self.max_order_sizes[ticker])
            level_profit = profit_per_share * qty
            profit += level_profit
            immediate_liquidity += qty

            unwind_plan.append({
                'price': bid_price,
                'quantity': qty,
                'profit': level_profit,
                'action': 'BUY'
            })

            remaining_qty -= qty

            if remaining_qty <= 0:
                break

        liquidity_coverage = immediate_liquidity / block_qty if block_qty > 0 else 0
        avg_profit = profit / block_qty if block_qty > 0 else 0

        accept = (
            profit > min_profit and
            avg_profit >= min_per_share and
            liquidity_coverage >= min_liquidity
        )

        # LAST 30 SECONDS: REJECT ALL NEW TENDERS
        if remaining <= 30:
            accept = False
            reason = f"REJECTED: Last 30 seconds - no new positions"
        else:
            reason = f"Profit: ${profit:.2f}, Avg: ${avg_profit:.4f}/share, Liquidity: {liquidity_coverage:.1%}"
            if not accept and profit > 0:
                reason += f" (Rejected: thresholds not met)"

        return accept, profit, reason, unwind_plan

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

    def accept_tender(self, tender_id):
        """Accept a tender offer"""
        try:
            resp = self.session.post(f'{self.base_url}/tenders/{tender_id}',
                                    json={'status': 'ACCEPTED'})
            if resp.status_code == 200:
                print(f"[{self._timestamp()}] ✓ Tender {tender_id} accepted")
                return True
        except Exception as e:
            print(f"[{self._timestamp()}] Error accepting tender: {e}")
        return False

    def decline_tender(self, tender_id):
        """Decline a tender offer"""
        try:
            resp = self.session.post(f'{self.base_url}/tenders/{tender_id}',
                                    json={'status': 'DECLINED'})
            if resp.status_code == 200:
                print(f"[{self._timestamp()}] ✗ Tender {tender_id} declined")
                return True
        except:
            pass
        return False

    def place_order(self, ticker, action, qty, price=None, order_type="LIMIT"):
        """Place an order"""
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

            # RIT order endpoint expects query parameters (not JSON body)
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
            else:
                print(f"[{self._timestamp()}] ✗ Order failed: HTTP {resp.status_code}")

        except Exception as e:
            print(f"[{self._timestamp()}] Error placing order: {e}")

        return None

    # =====================
    # EXECUTION LOGIC ONLY
    # =====================

    def _top_of_book(self, ticker):
        book = self.get_order_book(ticker, levels=10)
        best_bid = None
        best_ask = None
        if book.get('bids'):
            best_bid = float(book['bids'][0]['price'])
        if book.get('asks'):
            best_ask = float(book['asks'][0]['price'])
        return best_bid, best_ask, book

    def execute_unwind(self, symbol, side, qty):
        """Hybrid 3-phase unwind strategy.

        PHASE 1: Immediate Edge Lock-In (Aggressive Execution)
          - Execute 30–50% immediately using MARKET orders.

        PHASE 2: Passive Price Optimization
          - Work remaining using LIMIT orders at/near top-of-book.
          - Cancel/reprice if spread widens, book deteriorates, or price moves.

        PHASE 3: Failsafe Market Exit
          - If inventory remains after time cutoff or max reprices, exit with MARKET.

        Notes:
          - This function refactors ONLY unwind execution.
          - Tender acceptance logic and tender semantics are unchanged.
        """

        symbol = str(symbol)
        side = str(side).upper()
        total_qty = int(abs(int(qty)))
        if total_qty <= 0:
            return

        # Determine current live position direction; we always unwind towards flat.
        positions = self.get_all_positions()
        live_pos = int(positions.get(symbol, 0))
        if live_pos == 0:
            return

        # Sanity: force side to flatten live inventory.
        side = 'SELL' if live_pos > 0 else 'BUY'

        remaining_time = self.get_remaining_time()
        max_slice = self._effective_max_trade_size(symbol, total_qty)

        print(f"[{self._timestamp()}] [EXEC] execute_unwind {symbol}: live_pos={live_pos}, side={side}, qty={total_qty}, t_remain={remaining_time}s")

        # -----------------
        # PHASE 1 (Aggressive)
        # -----------------
        phase1_qty = int(max(1, round(total_qty * float(self.phase1_fraction))))
        phase1_qty = min(phase1_qty, total_qty)
        print(f"[{self._timestamp()}] [EXEC][P1] Aggressive MARKET qty={phase1_qty}")

        # IMPORTANT: Do NOT fire multiple MARKET orders back-to-back without
        # checking live position. This can overwhelm the RIT server and cause HTTP 500.
        p1_target_remaining = int(phase1_qty)
        consecutive_failures = 0
        stall_checks = 0

        while p1_target_remaining > 0:
            # Refresh live position and recompute direction toward flat.
            pos_before = int(self.get_all_positions().get(symbol, 0))
            if pos_before == 0:
                break
            side = 'SELL' if pos_before > 0 else 'BUY'

            slice_qty = min(int(p1_target_remaining), int(abs(pos_before)), int(max_slice))
            if slice_qty <= 0:
                break

            order = self.place_order(symbol, side, slice_qty, order_type="MARKET")
            if not order:
                consecutive_failures += 1
                code = self._last_order_status_code
                if code == 500:
                    print(f"[{self._timestamp()}] [EXEC][P1] HTTP 500 placing MARKET; backing off ({consecutive_failures})")
                if consecutive_failures >= int(self.max_consecutive_order_failures):
                    print(f"[{self._timestamp()}] [EXEC][P1] Too many failures; proceeding to P3")
                    break
                self._sleep_with_backoff(consecutive_failures)
                continue

            consecutive_failures = 0
            time.sleep(float(self.position_settle_sleep))
            pos_after = int(self.get_all_positions().get(symbol, 0))

            reduction = abs(int(pos_before)) - abs(int(pos_after))
            if reduction > 0:
                p1_target_remaining = max(0, int(p1_target_remaining) - int(reduction))
                stall_checks = 0
            else:
                # No visible progress yet; wait a bit instead of sending more orders.
                stall_checks += 1
                time.sleep(0.20)
                if stall_checks >= 3:
                    # Stop aggressive phase if we can't observe fills.
                    break

            time.sleep(float(self.sleep_between_orders))

        time.sleep(float(self.position_settle_sleep))
        live_pos_after_p1 = int(self.get_all_positions().get(symbol, 0))
        print(f"[{self._timestamp()}] [EXEC][P1] pos_after={live_pos_after_p1}")

        # Compute remaining inventory to unwind *from live position*, not from plans.
        remaining_inventory = abs(int(live_pos_after_p1))
        if remaining_inventory == 0:
            print(f"[{self._timestamp()}] [EXEC] {symbol} flat after P1")
            return

        # -----------------
        # PHASE 2 (Passive)
        # -----------------
        print(f"[{self._timestamp()}] [EXEC][P2] Passive LIMIT working remaining={remaining_inventory}")
        reprices = 0
        last_posted_price = None

        while remaining_inventory > 0:
            remaining_time = self.get_remaining_time()
            if remaining_time <= self.phase3_time_cutoff_sec:
                print(f"[{self._timestamp()}] [EXEC][P2] Time cutoff hit (t_remain={remaining_time}s) -> P3")
                break
            if reprices >= int(self.phase2_max_reprices):
                print(f"[{self._timestamp()}] [EXEC][P2] Max reprices hit ({reprices}) -> P3")
                break

            best_bid, best_ask, _ = self._top_of_book(symbol)
            if best_bid is None or best_ask is None:
                print(f"[{self._timestamp()}] [EXEC][P2] Missing book -> P3")
                break

            spread = float(best_ask - best_bid)
            if spread > float(self.phase2_max_spread):
                print(f"[{self._timestamp()}] [EXEC][P2] Spread too wide ({spread:.4f}) -> P3")
                break

            # Passive price at/near top-of-book on our side:
            # - SELL: post at best_ask
            # - BUY: post at best_bid
            if side == 'SELL':
                price = float(best_ask)
            else:
                price = float(best_bid)

            # Avoid re-posting at the exact same price repeatedly.
            if last_posted_price is not None and abs(price - last_posted_price) < 1e-9:
                if side == 'SELL':
                    # Nudge one tick more aggressive (still usually inside/top)
                    price = max(float(best_bid), price - float(self.phase2_min_tick))
                else:
                    price = min(float(best_ask), price + float(self.phase2_min_tick))

            last_posted_price = price

            # Cancel any stale working orders before reposting.
            if reprices > 0:
                self.cancel_all_orders()

            slice_qty = min(int(remaining_inventory), max_slice)
            print(f"[{self._timestamp()}] [EXEC][P2] Post LIMIT {side} {slice_qty} {symbol} @ {price:.2f} (reprice={reprices})")
            order = self.place_order(symbol, side, slice_qty, price=price, order_type="LIMIT")
            if not order:
                # If the server is unhappy, don't hammer it.
                if self._last_order_status_code == 500:
                    self._sleep_with_backoff(2)

            # Give some time for passive fill; then re-check position.
            time.sleep(float(self.phase2_check_sleep))
            pos_now = int(self.get_all_positions().get(symbol, 0))
            remaining_inventory = abs(pos_now)

            if remaining_inventory == 0:
                print(f"[{self._timestamp()}] [EXEC] {symbol} flat after P2")
                return

            reprices += 1

        # -----------------
        # PHASE 3 (Failsafe)
        # -----------------
        remaining_inventory = abs(int(self.get_all_positions().get(symbol, 0)))
        if remaining_inventory <= 0:
            print(f"[{self._timestamp()}] [EXEC] {symbol} already flat before P3")
            return

        print(f"[{self._timestamp()}] [EXEC][P3] Failsafe MARKET exit remaining={remaining_inventory}")
        # Cancel first to avoid crossing ourselves
        self.cancel_all_orders()
        time.sleep(float(self.position_settle_sleep))

        consecutive_failures = 0
        while remaining_inventory > 0:
            pos_before = int(self.get_all_positions().get(symbol, 0))
            if pos_before == 0:
                break
            side = 'SELL' if pos_before > 0 else 'BUY'

            slice_qty = min(int(abs(pos_before)), int(remaining_inventory), int(max_slice))
            if slice_qty <= 0:
                break

            order = self.place_order(symbol, side, slice_qty, order_type="MARKET")
            if not order:
                consecutive_failures += 1
                if self._last_order_status_code == 500:
                    print(f"[{self._timestamp()}] [EXEC][P3] HTTP 500 placing MARKET; backing off ({consecutive_failures})")
                if consecutive_failures >= int(self.max_consecutive_order_failures):
                    # Don't tight-loop forever; back off more aggressively.
                    self._sleep_with_backoff(consecutive_failures)
                else:
                    self._sleep_with_backoff(consecutive_failures)
                remaining_inventory = abs(int(self.get_all_positions().get(symbol, 0)))
                continue

            consecutive_failures = 0
            time.sleep(float(self.position_settle_sleep))
            remaining_inventory = abs(int(self.get_all_positions().get(symbol, 0)))

            time.sleep(float(self.sleep_between_orders))

        print(f"[{self._timestamp()}] [EXEC] {symbol} flat after P3")

    def execute_unwind_plan(self, ticker, unwind_plan, total_qty):
        """Execute the unwind plan immediately

        NOTE: tender acceptance logic and plan generation remain unchanged.
        This method now delegates the actual unwind execution to `execute_unwind()`.
        """
        positions_before = self.get_all_positions()
        pos_before = positions_before.get(ticker, 0)
        print(f"[{self._timestamp()}] MARKET UNWIND start {ticker}: pos_before={pos_before}, tender_qty={total_qty:+}")

        # Determine unwind direction from current position (do not change tender semantics)
        side = 'SELL' if int(pos_before) > 0 else 'BUY'
        self.execute_unwind(ticker, side, abs(int(total_qty)))

        positions_after = self.get_all_positions()
        pos_after = positions_after.get(ticker, 0)
        print(f"[{self._timestamp()}] MARKET UNWIND end {ticker}: pos_after={pos_after}")

    def manage_positions(self):
        """Manage and unwind existing positions with time awareness

        Refactor: execution uses the same hybrid 3-phase unwind strategy.
        """
        remaining = self.get_remaining_time()
        positions = self.get_all_positions()

        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] Managing {ticker}: {position} (Time left: {remaining}s)")

                side = 'SELL' if int(position) > 0 else 'BUY'
                self.execute_unwind(ticker, side, abs(int(position)))

    def run(self):
        """Main trading loop with guaranteed position closure"""
        print(f"[{self._timestamp()}] Starting Liability Trader...")
        print("Press Ctrl+C to stop")

        # Get initial case info
        self.get_case_info()

        try:
            while True:
                # Check time and adjust strategy
                remaining = self.get_remaining_time()
                if remaining <= 0:
                    print(f"[{self._timestamp()}] ⚠ TIME'S UP! Closing all positions...")
                    self.force_close_all_positions()
                    break

                # Show time remaining every 30 seconds
                if int(remaining) % 30 == 0 and remaining > 0:
                    print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")

                # 1. Check for tenders
                tenders = self.get_tender_offers()

                for tender in tenders:
                    self.tenders_received += 1
                    ticker = tender['ticker']
                    qty = tender['quantity']
                    price = tender['price']
                    tender_id = tender['tender_id']

                    print(f"\n[{self._timestamp()}] {'='*50}")
                    print(f"[{self._timestamp()}] TENDER #{self.tenders_received}")
                    print(f"[{self._timestamp()}] {ticker}: {qty:+} shares @ ${price:.2f}")
                    print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")

                    # Evaluate
                    accept, profit, reason, unwind_plan = self.evaluate_tender(tender)

                    print(f"[{self._timestamp()}] Analysis: {reason}")
                    print(f"[{self._timestamp()}] Decision: {'ACCEPT' if accept else 'DECLINE'}")

                    if accept:
                        if self.accept_tender(tender_id):
                            self.tenders_accepted += 1
                            self.total_pnl += profit

                            time.sleep(0.5)  # Wait for position update
                            self.execute_unwind_plan(ticker, unwind_plan, qty)
                    else:
                        self.decline_tender(tender_id)

                    print(f"[{self._timestamp()}] Accepted: {self.tenders_accepted}/{self.tenders_received}")
                    print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")

                # 2. Manage positions (more frequent near the end)
                if remaining <= 120:  # Last 2 minutes: manage every 5 seconds
                    if int(time.time()) % 5 == 0:
                        self.manage_positions()
                else:  # Normal: manage every 15 seconds
                    if int(time.time()) % 15 == 0:
                        self.manage_positions()

                # 3. Brief sleep
                time.sleep(0.5)

        except KeyboardInterrupt:
            print(f"\n[{self._timestamp()}] Manual shutdown requested...")

        finally:
            # GUARANTEED CLEANUP - RUNS NO MATTER WHAT
            print(f"\n[{self._timestamp()}] =========================================")
            print(f"[{self._timestamp()}] FINAL CLEANUP - ENSURING ZERO POSITIONS")
            print(f"[{self._timestamp()}] =========================================")

            # Force close everything
            self.force_close_all_positions()

            # Final check
            time.sleep(2)
            positions = self.get_all_positions()
            print(f"\n[{self._timestamp()}] FINAL POSITION CHECK:")
            all_closed = True
            for ticker, position in positions.items():
                print(f"[{self._timestamp()}] {ticker}: {position}")
                if position != 0:
                    all_closed = False
                    print(f"[{self._timestamp()}] ⚠ WARNING: {ticker} still has position {position}")

            if all_closed:
                print(f"[{self._timestamp()}] ✓ SUCCESS: All positions closed!")
            else:
                print(f"[{self._timestamp()}] ⚠ FAILED: Some positions remain open")
                # One more aggressive try
                self.force_close_all_positions()

            # Final stats
            print(f"\n[{self._timestamp()}] FINAL STATS:")
            print(f"[{self._timestamp()}] Tenders: {self.tenders_accepted}/{self.tenders_received}")
            print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")
            print(f"[{self._timestamp()}] Runtime: {time.time() - self.start_time:.1f}s")

            # Check if any positions remain (critical warning)
            positions = self.get_all_positions()
            for ticker, position in positions.items():
                if position != 0:
                    print(f"\n[{self._timestamp()}] ❗❗❗ CRITICAL WARNING ❗❗❗")
                    print(f"[{self._timestamp()}] {ticker} position: {position} NOT CLOSED")
                    print(f"[{self._timestamp()}] YOU WILL BE PENALIZED!")
                    print(f"[{self._timestamp()}] Manually close position in RIT client NOW!")


def main():
    """Main entry point"""
    print("=== RCFA Liability Trading Bot ===")
    print("IMPORTANT: This bot will FORCE CLOSE all positions at the end")
    print("Make sure RIT client is running and connected!")
    print()

    API_KEY = input("Enter your RIT API Key (get from RIT client): ").strip()

    if not API_KEY:
        print("No API key provided. Using default 'YOUR_API_KEY'")
        API_KEY = "YOUR_API_KEY"

    trader = LiabilityTrader(api_key=API_KEY)
    trader.run()


if __name__ == "__main__":
    main()
