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
    
    def execute_unwind_plan(self, ticker, unwind_plan, total_qty):
        """Execute the unwind plan immediately"""
        positions_before = self.get_all_positions()
        pos_before = positions_before.get(ticker, 0)
        print(f"[{self._timestamp()}] MARKET UNWIND start {ticker}: pos_before={pos_before}, tender_qty={total_qty:+}")

        placed_orders = 0
        placed_qty = 0
        for item in unwind_plan:
            qty = int(item.get('quantity', 0))
            if qty <= 0:
                continue

            side = item.get('action')
            print(f"[{self._timestamp()}] MARKET UNWIND {ticker}: {side} {qty}")
            order = self.place_order(
                ticker=ticker,
                action=side,
                qty=qty,
                order_type="MARKET"
            )
            if order:
                placed_orders += 1
                placed_qty += qty
                time.sleep(0.08)  # Small delay between orders

        if placed_orders == 0:
            print(f"[{self._timestamp()}] ⚠ MARKET UNWIND: no orders placed - trying backup strategy")
            positions = self.get_all_positions()
            current_pos = positions.get(ticker, 0)
            if abs(current_pos) > 0:
                action = 'SELL' if current_pos > 0 else 'BUY'
                qty = min(abs(current_pos), self.max_order_sizes[ticker])
                print(f"[{self._timestamp()}] MARKET UNWIND backup {ticker}: pos_before={current_pos}, {action} {qty}")
                self.place_order(ticker, action, qty, order_type="MARKET")
        else:
            print(f"[{self._timestamp()}] ✓ MARKET UNWIND placed {placed_orders} orders, total_qty={placed_qty}")

        time.sleep(0.5)
        positions_after = self.get_all_positions()
        pos_after = positions_after.get(ticker, 0)
        print(f"[{self._timestamp()}] MARKET UNWIND end {ticker}: pos_after={pos_after}")
    
    def manage_positions(self):
        """Manage and unwind existing positions with time awareness"""
        remaining = self.get_remaining_time()
        positions = self.get_all_positions()
        
        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] Managing {ticker}: {position} (Time left: {remaining}s)")

                qty = min(abs(position), self.max_order_sizes[ticker])
                if qty <= 0:
                    continue

                if position > 0:  # Long, need to SELL
                    action = "SELL"
                else:  # Short, need to BUY
                    action = "BUY"

                print(f"[{self._timestamp()}] MARKET UNWIND manage {ticker}: pos_before={position}, {action} {qty}")
                self.place_order(ticker, action, qty, order_type="MARKET")

                time.sleep(0.2)
                pos_after = self.get_all_positions().get(ticker, 0)
                print(f"[{self._timestamp()}] MARKET UNWIND manage {ticker}: pos_after={pos_after}")
    
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
