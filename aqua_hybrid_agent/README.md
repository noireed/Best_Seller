# AQuA Hybrid Agent — starter scaffold

This is an **incomplete safe starter**, not a running autonomous trading bot.
It includes the documented API client, basic technical indicators, typed decision
schema, order guard, and dry-run executor. No real orders are placed by default.

## Setup
```bash
python -m venv .venv
pip install -r requirements.txt
cp .env.example .env
python main.py
```
Fill in the official AQuA base URL and team code. The provided assignment PDF
specifies API paths but **does not supply a verified API host**.

## Before live mode
- Implement news/Web Search MCP ingestion and timestamped provenance.
- Implement Gemini structured-output integration and deterministic fallback.
- Obtain the portfolio, order-status, and order-history endpoint specifications.
- Persist fills, reserved cash, cooldowns, rate limits, and idempotency keys.
- Add 30-minute cooldown and per-minute/hour token buckets with server reconciliation.
- Test 1m/1d candle JSON schema and order status lifecycle against AQuA.
- Backtest without lookahead, then paper test end-to-end.
- Never send orders on a network error or missing risk input.
