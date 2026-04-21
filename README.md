# RCFATradingComp2026

Algorithmic trading strategies developed for the **Rotman Finance Lab Interactive Trader (RIT) Trading Case Competition**, where our team placed **4th overall**.

---

## Competition Overview

The RIT Trading Case Competition involved 26 teams, each running algorithmic trading strategies on a simulated market platform. Final rankings were based on aggregate and average performance across multiple simulation runs.

**Team Members:**
- Rithik Singh
- Michael Xu

---

## Strategy Framework

Every strategy follows a mandatory top-down framework:

| Question | Focus |
|----------|-------|
| Who am I in this market? | Role definition |
| What is my objective? | Execution goal, not price prediction |
| Where does profit come from? | Spread, arbitrage, or fee differentials |
| What are the risks? | Liquidity, inventory, fines, execution |
| How are risks mitigated? | Rules, limits, automation logic |

**Core constraint:** No speculation, no technical analysis, no directional prediction. Market prices follow a random walk — edge comes from structure, execution, and risk control.

---

## Cases Implemented

### 1. Liability Trading Case (LTB)

**Role:** Market taker executing arbitrage.

**Profit Source:** Arbitrage between institutional tender offer prices and current market prices.

**Execution Logic:**
- Evaluate tender price vs. VWAP and order book liquidity
- Accept only when market can absorb liquidation profitably
- Liquidate immediately upon acceptance — no inventory warehousing
- Trailing stop-loss logic to maximize profit within accepted position
- Position flattening function to close all outstanding positions before session end (avoiding end-of-session uncovered position fines)

**Fines Avoided:**
- Front-running (trading before tender offer acceptance)
- Market maker activity detection
- Speculative trades
- End-of-session uncovered positions ($1/share penalty)

### 2. Algorithmic Trading / Market Making Case (ALGO2e)

**Role:** Algorithmic market maker providing liquidity.

**Profit Source:**
- Bid-ask spread capture (buy lower, sell higher)
- Market maker fees for providing liquidity

**Core Requirements:**
- Continuous quoting on both sides of the order book
- Balanced inventory (targeting near-zero net position)
- Dynamic quote adjustment for inventory rebalancing
- Fee-aware execution

**Key Challenge:** Maintaining market neutrality while accounting for competitor behavior and liquidity variations.

---

## Technical Architecture

### API Integration
- **Client REST API** for order execution and market data retrieval
- Continuous polling for low-latency execution (vs. fixed sleep intervals)
- Separate scripts for liability trading and market making to prevent mode confusion

### Key Files

| File | Description |
|------|-------------|
| `liability_trader*.py` | Liability trading case — iterative versions showing strategy refinement |
| `lt3_liability_trader*.py` | Early liability trading implementations with various strategy approaches |
| `algo2e_trader*.py` | Market making case — VWAP-hybrid and edge-weighted strategies |
| `lt3_market_utils.py` | Shared market utility functions |
| `RIT_API_CONTRACT` | API endpoint documentation |
| `LT3_BEHAVIOUR_CONTRACT` | Case-specific behavior rules |

### Dependencies
```
requests
```

---

## Key Learnings

1. **Latency matters** — continuous polling outperforms fixed sleep intervals
2. **Position flattening before session end** is critical to avoid catastrophic fines
3. **Wait the full 30-second window** before declining tender offers — conditions can shift into profitability
4. **Separate algorithms per case** to avoid market maker vs. liability trader mode confusion
5. **Average aggregate performance** across multiple runs matters more than maximizing any single run

---

## Acknowledgments

Developed with support from Rotman Finance Lab and the RIT platform.
