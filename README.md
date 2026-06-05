# Balfund Renko Trading System v2.6

Renko-based stop-and-reverse trading system with Dhan API.

## Features
- Renko bricks from live WebSocket ticks (spot index for options, futures for MCX)
- ITM options trading for NIFTY / BANKNIFTY / SENSEX
- Direct futures trading for GOLDPETAL / SILVERMICRO / CRUDEOILM
- Target-based exit with wait-for-reversal
- Daily profit target limit
- Auto-squareoff at configurable IST time
- CustomTkinter dark-themed GUI with Token Manager

## Instruments
| Key | Signal Source | Trade Type | Default Brick |
|-----|--------------|------------|---------------|
| NIFTY | Spot Index (IDX_I:13) | ITM Options | 30 pts |
| BANKNIFTY | Spot Index (IDX_I:25) | ITM Options | 50 pts |
| SENSEX | Spot Index (IDX_I:51) | ITM Options | 100 pts |
| GOLDPETAL | MCX Futures | Futures | 5 pts |
| SILVERMICRO | MCX Futures | Futures | 50 pts |
| CRUDEOILM | MCX Futures | Futures | 5 pts |

## Setup
1. Download EXE from GitHub Actions artifacts
2. Run the EXE
3. Enter Dhan credentials in Token Manager tab
4. Configure strategy in Config tab
5. Click START

## Requirements (for running from source)
```
pip install -r requirements.txt
python app.py
```

## Build
Automatic via GitHub Actions on every push. Downloads EXE from Actions > Artifacts.

---
Balfund Trading Pvt Ltd | info@balfund.com
