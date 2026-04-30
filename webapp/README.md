# TradeBackTest WebApp (MVP)

## 1. Backend

```bash
cd webapp/backend
pip install -r requirements.txt
python -m scripts.init_db
python -m scripts.import_example_data
uvicorn app.main:app --reload --port 8000
```

## 2. Frontend

```bash
cd webapp/frontend
npm install
npm run dev
```

Open: http://127.0.0.1:5173

## 3. Core API

- `GET /api/v1/health`
- `GET /api/v1/strategies`
- `POST /api/v1/strategies`
- `GET /api/v1/strategies/{id}`
- `GET /api/v1/strategies/{id}/metrics`
- `GET /api/v1/strategies/{id}/equity`
- `GET /api/v1/strategies/{id}/equity/drawdown`
- `GET /api/v1/strategies/{id}/trades`
- `POST /api/v1/ingest/equity`
- `POST /api/v1/ingest/trades`

## 4. Import OKX Kline To Dashboard

```bash
cd webapp/backend
python -m scripts.import_okx_kline_to_webapp --strategy-name "OKX BTC 1H" --symbol BTC-USDT --bar 1H --days 10 --reset
```

Optional proxy:

```bash
python -m scripts.import_okx_kline_to_webapp --strategy-name "OKX BTC 1H" --symbol BTC-USDT --bar 1H --days 10 --proxy http://127.0.0.1:7897 --reset
```
