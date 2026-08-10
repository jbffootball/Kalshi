[README.md](https://github.com/user-attachments/files/30911644/README.md)
# Kalshi BTC 15-Minute Streak Bot — Railway Starter

## Modes
- `MODE=paper`: public production market data, **never places an order**.
- `MODE=demo`: Kalshi Demo data; demo orders only when `PLACE_DEMO_ORDERS=true`.
- There is intentionally no production real-money order mode in this starter.

## Current strategy
- `KXBTC15M`
- Trigger after 2 identical settled outcomes
- Buy the opposite outcome
- Maximum entry 40 cents
- Entry only during first 3 minutes after `open_time`
- 1 contract by default
- Hold to settlement
- One filled trade per market
- Immediate-or-cancel order so an unfilled order cannot remain resting after the 3-minute entry window

## Railway
1. Create a GitHub repository.
2. Upload `main.py` and `requirements.txt`.
3. Railway -> New Project -> Deploy from GitHub.
4. Start command: `python main.py`
5. Add the variables from `.env.example`.

Start with `MODE=paper`. No API key is needed for public market data.

For demo orders, set `MODE=demo`, `PLACE_DEMO_ORDERS=true`, and add your **demo** API key ID and RSA private key as Railway variables.

If your private key is pasted on one line, escaped `\n` characters are accepted.

Demo has separate credentials and its own market environment. If `KXBTC15M` is unavailable there, use `MODE=paper` to test live signal logic without sending orders.
