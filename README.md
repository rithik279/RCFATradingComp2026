# RCFATradingComp2026

Algorithmic trading strategies developed for the **Rotman Finance Lab Interactive Trader (RIT) Trading Case Competition**, where our team placed **4th overall** out of 26 teams.

---

## Competition Overview

The RIT Trading Case Competition involved 26 teams, each running algorithmic trading strategies on a simulated market platform. Final rankings were based on aggregate and average performance across multiple simulation runs.

**Team Members:**
- Rithik Singh
- Michael Xu

---

## Getting Started

### 1. Platform Setup

Before running these algorithms, you need to set up the RIT Market Simulator:

1. **Install RIT Client** — Download from the [Rotman Finance Lab](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/rit-market-simulator/rit-demo--tutorials/)
2. **Connect to Server** — Set Server IP to `flserver.rotman.utoronto.ca` (port 10000)
3. **Configure API** — Enable the Client REST API in the RIT client and note your API key

### 2. Install Dependencies

```bash
pip install requests
```

### 3. Configure Scripts

Edit the `CONFIG` dict at the top of each script with your setup:

```python
CONFIG = {
    "base_url": "http://localhost:9999/v1",  # Default local API URL
    "api_key": "your-api-key",               # Your RIT API key
    "case": "LT3"                            # LT3 or ALGO2e depending on case
}
```

### 4. Run

```bash
# Liability Trading Case
python liability_trading/scripts/liability_trader.py

# Market Making Case
python market_making/scripts/algo2e_trader_v1.py
```

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

### 1. Liability Trading Case (LTB/LT3)

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

## Repository Structure

```
RCFATradingComp2026/
├── docs/                          # API contracts, case documentation
│   ├── RIT_API_CONTRACT           # REST API endpoint reference
│   ├── LT3_BEHAVIOUR_CONTRACT     # Case-specific rules
│   └── roughCaseNotes.txt         # Development notes
│
├── liability_trading/             # Liability Trading Case
│   ├── base/                      # Base algorithm template
│   ├── scripts/                   # Production-ready versions (v1-v5)
│   ├── experiments/               # Prototype/iteration versions
│   └── utils/                     # Shared utilities
│
├── market_making/                  # Market Making Case
│   └── scripts/                   # Algorithm versions
│
├── reflection/                     # Competition reflection
└── workspace/                      # RIT workspace files
```

---

## Key Learnings

1. **Latency matters** — continuous polling outperforms fixed sleep intervals
2. **Position flattening before session end** is critical to avoid catastrophic fines
3. **Wait the full 30-second window** before declining tender offers — conditions can shift into profitability
4. **Separate algorithms per case** to avoid market maker vs. liability trader mode confusion
5. **Average aggregate performance** across multiple runs matters more than maximizing any single run

---

## License

This project is licensed under the MIT License - see [LICENSE](LICENSE) for details.

---

## Acknowledgments

Developed with support from Rotman Finance Lab and the RIT platform.
