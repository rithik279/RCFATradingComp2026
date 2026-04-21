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
        self.end_time = None
        self.time_warning_issued = False
        self.aggressive_mode = False
        
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
        
        if remaining <= 30 and not self.time_warning_issued:
            print(f"[{self._timestamp()}] ⚠ CRITICAL: {remaining}s remaining")
            print(f"[{self._timestamp()}] Forcing position closure")
            self.time_warning_issued = True
            self.aggressive_mode = True
            self.force_close_all_positions()
            return 'aggressive'
            
        elif remaining <= 60:
            print(f"[{self._timestamp()}] ⚠ LAST MINUTE: {remaining}s remaining")
            print(f"[{self._timestamp()}] Switching to aggressive closing mode")
            self.aggressive_mode = True
            return 'aggressive'
        
        elif remaining <= 120:
            # Last 2 minutes - be MORE ACCEPTING of tenders
            self.aggressive_mode = True
            return 'aggressive'
        
        return 'normal'
    
    def _analyze_buy_tender(self, ticker, block_qty, block_price, asks, time_mode, remaining):
        """Analyze tender where we BUY from client"""
        if not asks:
            return False, 0, "No ask prices available", []
        
        # **FIXED: More accepting near the end, not less**
        if time_mode == 'aggressive' or remaining < 120:
            # Last 2 minutes: accept MORE tenders to maximize profit
            min_profit = 500     # Lower threshold
            min_per_share = 0.02  # Lower threshold
            min_liquidity = 0.4   # Lower threshold - accept less liquid offers
        else:
            min_profit = 1000
            min_per_share = 0.04
            min_liquidity = 0.6
        
        remaining_qty = block_qty
        profit = 0
        immediate_liquidity = 0
        unwind_plan = []
        
        # Look deeper into order book for aggressive mode
        levels_to_check = 30 if self.aggressive_mode else 20
        
        for level in asks[:levels_to_check]:
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
        
        # **FIXED: Accept even if not fully liquid in aggressive mode**
        if self.aggressive_mode and remaining <= 60:
            # Last minute: accept if profitable overall
            accept = profit > 0 and avg_profit > 0
        else:
            accept = (
                profit > min_profit and
                avg_profit >= min_per_share and
                liquidity_coverage >= min_liquidity
            )
        
        # LAST 30 SECONDS: NO NEW TENDERS
        if remaining <= 30:
            accept = False
            reason = f"REJECTED: Last 30 seconds - closing positions only"
        else:
            reason = f"Profit: ${profit:.2f}, Avg: ${avg_profit:.4f}/share, Liquidity: {liquidity_coverage:.1%}"
            if not accept and profit > 0:
                reason += f" (Rejected: thresholds not met)"
        
        return accept, profit, reason, unwind_plan
    
    def _analyze_sell_tender(self, ticker, block_qty, block_price, bids, time_mode, remaining):
        """Analyze tender where we SELL to client"""
        if not bids:
            return False, 0, "No bid prices available", []
        
        # **FIXED: Same logic as buy tenders**
        if time_mode == 'aggressive' or remaining < 120:
            min_profit = 500
            min_per_share = 0.02
            min_liquidity = 0.4
        else:
            min_profit = 1000
            min_per_share = 0.04
            min_liquidity = 0.6
        
        remaining_qty = block_qty
        profit = 0
        immediate_liquidity = 0
        unwind_plan = []
        
        levels_to_check = 30 if self.aggressive_mode else 20
        
        for level in bids[:levels_to_check]:
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
        
        if self.aggressive_mode and remaining <= 60:
            accept = profit > 0 and avg_profit > 0
        else:
            accept = (
                profit > min_profit and
                avg_profit >= min_per_share and
                liquidity_coverage >= min_liquidity
            )
        
        if remaining <= 30:
            accept = False
            reason = f"REJECTED: Last 30 seconds - closing positions only"
        else:
            reason = f"Profit: ${profit:.2f}, Avg: ${avg_profit:.4f}/share, Liquidity: {liquidity_coverage:.1%}"
            if not accept and profit > 0:
                reason += f" (Rejected: thresholds not met)"
        
        return accept, profit, reason, unwind_plan
    
    def manage_positions(self):
        """Manage and unwind existing positions with time awareness"""
        remaining = self.get_remaining_time()
        positions = self.get_all_positions()
        
        for ticker, position in positions.items():
            if position != 0:
                print(f"[{self._timestamp()}] Managing {ticker}: {position} (Time left: {remaining}s)")
                
                # More aggressive unwinding as time runs out
                if remaining <= 30:
                    # Last 30 seconds: use larger slices
                    qty = min(abs(position), self.max_order_sizes[ticker] * 2)
                elif remaining <= 60:
                    # Last minute: slightly larger slices
                    qty = min(abs(position), int(self.max_order_sizes[ticker] * 1.5))
                else:
                    qty = min(abs(position), self.max_order_sizes[ticker])
                
                if qty <= 0:
                    continue
                
                if position > 0:
                    action = "SELL"
                else:
                    action = "BUY"
                
                print(f"[{self._timestamp()}] MARKET UNWIND manage {ticker}: pos_before={position}, {action} {qty}")
                self.place_order(ticker, action, qty, order_type="MARKET")
                
                time.sleep(0.1)
    
    def run(self):
        """Main trading loop with guaranteed position closure"""
        print(f"[{self._timestamp()}] Starting Liability Trader...")
        print("Press Ctrl+C to stop")
        
        self.get_case_info()
        
        try:
            while True:
                remaining = self.get_remaining_time()
                if remaining <= 0:
                    print(f"[{self._timestamp()}] ⚠ TIME'S UP! Closing all positions...")
                    self.force_close_all_positions()
                    break
                
                # Show time remaining every 30 seconds
                if int(remaining) % 30 == 0 and remaining > 0:
                    print(f"[{self._timestamp()}] Time remaining: {remaining:.0f}s")
                
                # Check for tenders
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
                    
                    # Check time mode BEFORE evaluating
                    time_mode = self.check_time_warning()
                    
                    # Evaluate tender
                    accept, profit, reason, unwind_plan = self.evaluate_tender(tender)
                    
                    print(f"[{self._timestamp()}] Analysis: {reason}")
                    print(f"[{self._timestamp()}] Decision: {'ACCEPT' if accept else 'DECLINE'}")
                    
                    if accept:
                        if self.accept_tender(tender_id):
                            self.tenders_accepted += 1
                            self.total_pnl += profit
                            
                            time.sleep(0.3)
                            self.execute_unwind_plan(ticker, unwind_plan, qty)
                    else:
                        self.decline_tender(tender_id)
                    
                    print(f"[{self._timestamp()}] Accepted: {self.tenders_accepted}/{self.tenders_received}")
                    print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")
                
                # Manage positions more frequently in aggressive mode
                if self.aggressive_mode:
                    if int(time.time()) % 3 == 0:  # Every 3 seconds
                        self.manage_positions()
                else:
                    if int(time.time()) % 10 == 0:  # Every 10 seconds
                        self.manage_positions()
                
                time.sleep(0.5)
                
        except KeyboardInterrupt:
            print(f"\n[{self._timestamp()}] Manual shutdown requested...")
        
        finally:
            # Final cleanup
            print(f"\n[{self._timestamp()}] =========================================")
            print(f"[{self._timestamp()}] FINAL CLEANUP")
            print(f"[{self._timestamp()}] =========================================")
            
            self.force_close_all_positions()
            
            time.sleep(1)
            positions = self.get_all_positions()
            for ticker, position in positions.items():
                if position != 0:
                    print(f"[{self._timestamp()}] ⚠ WARNING: {ticker} still has position {position}")
                    # Emergency close
                    if position > 0:
                        self.place_order(ticker, "SELL", abs(position), order_type="MARKET")
                    else:
                        self.place_order(ticker, "BUY", abs(position), order_type="MARKET")
            
            print(f"\n[{self._timestamp()}] FINAL STATS:")
            print(f"[{self._timestamp()}] Tenders: {self.tenders_accepted}/{self.tenders_received}")
            print(f"[{self._timestamp()}] Total P&L: ${self.total_pnl:.2f}")

