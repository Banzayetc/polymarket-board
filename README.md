# Polymarket Sports Board

Live dashboard: Polymarket odds vs bookmakers across Soccer, NBA, MLB, NHL, Tennis, MMA.

## Features
- Live data from Polymarket Gamma API
- Bookmaker comparison via Claude API (web search)
- Diff column: how far PM is from Pinnacle/Bet365
- Links to each market on polymarket.com
- Caches market data 10 min, bookmaker odds 10 min

## Deploy on Render

1. Push repo to GitHub (public repo)
2. render.com → New → Web Service → connect repo
3. Render reads `render.yaml` automatically
4. Add env var: `ANTHROPIC_API_KEY` = your key
5. Deploy → done

## Local run

```bash
pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-ant-... python app.py
```

Open http://localhost:5000

## ENV vars

| Variable | Default | Description |
|---|---|---|
| ANTHROPIC_API_KEY | — | Required for bookmaker fetch |
| DB_PATH | /tmp/board.db | SQLite cache path |
| CACHE_TTL_MIN | 10 | Cache lifetime in minutes |
