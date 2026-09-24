# AlgoEdge

AlgoEdge is a Python trading-automation starter project. It currently downloads market data, evaluates a moving-average signal, and executes it against a paper broker. Live trading is disabled by default.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .
Copy-Item .env.example .env
```

In VS Code, select `.venv\Scripts\python.exe` as the Python interpreter.

## Run

```powershell
python -m algoedge.main
```

The first run fetches data from Yahoo Finance, so it needs network access. The default result is a paper-trading signal for AAPL.

## Live dashboard

Start the dashboard server with:

```powershell
python -m algoedge.web_server
```

Open `http://127.0.0.1:5173`. The server binds to localhost only and has no login of its own; anyone with local access to the machine can reach it. It keeps Groww credentials on the backend and exposes only normalized position and open-order data at `GET /api/grids`; credentials and raw account responses are never sent to the browser. Original grid levels are shown only when AlgoEdge has recorded them locally in its ignored order ledger.

The Groww account used by the server must have access to the relevant live-data API. If Groww denies quote access, mark price, unrealized P&L, and liquidation price remain unavailable rather than falling back to paper values.

## Connect Groww

Groww API access requires a Groww Trading API subscription. Create an access token or API key and secret in the [Groww Trading API portal](https://groww.in/trade-api/api-keys), then add credentials to `.env`:

```text
ALGOEDGE_BROKER=groww
ALGOEDGE_GROWW_ACCESS_TOKEN=your_token
```

Alternatively, use `ALGOEDGE_GROWW_API_KEY` and `ALGOEDGE_GROWW_API_SECRET`. Do not commit `.env` or share these values. The access-token flow expires daily; API-key and secret authentication requires the daily approval required by Groww.

Check the authenticated connection without placing an order:

```powershell
python -m algoedge.connect_groww
```

The Groww adapter supports NSE cash orders, but live execution is blocked unless `ALGOEDGE_LIVE_TRADING=true` is explicitly set. Validate symbols, quantities, risk limits, and paper results before enabling it. Groww symbols use Indian exchange conventions such as `RELIANCE`, not Yahoo Finance symbols such as `AAPL`.

## Test and lint

```powershell
pytest
ruff check .
```

## Project layout

- `src/algoedge/`: application code
- `config/`: non-secret configuration reference
- `tests/`: automated tests
- `data/`: local market-data exports, ignored by Git
- `logs/`: runtime logs, ignored by Git
- `.env`: local settings and secrets, ignored by Git

Before adding a real broker, implement authentication, order validation, position sizing, risk limits, retries, audit logging, and a paper-trading validation period. Never commit API keys or enable live trading without explicit safeguards.
