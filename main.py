import os
import time
import uuid
import base64
import csv
import datetime as dt
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

MODE = os.getenv("MODE", "paper").strip().lower()
if MODE not in {"paper", "demo"}:
    raise RuntimeError("MODE must be 'paper' or 'demo'.")

if MODE == "paper":
    BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
    PLACE_ORDERS = False
else:
    BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
    PLACE_ORDERS = os.getenv("PLACE_DEMO_ORDERS", "false").lower() == "true"

if PLACE_ORDERS and "demo.kalshi.co" not in BASE_URL:
    raise RuntimeError("Safety lock: orders are only allowed in Kalshi Demo.")

SERIES = os.getenv("SERIES", "KXBTC15M")
STREAK_TRIGGER = int(os.getenv("STREAK_TRIGGER", "2"))
MAX_ENTRY_CENTS = int(os.getenv("MAX_ENTRY_CENTS", "40"))
ENTRY_WINDOW_MINUTES = float(os.getenv("ENTRY_WINDOW_MINUTES", "3"))
CONTRACTS = float(os.getenv("CONTRACTS", "1"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))

# Persistent paper-trade log. If a Railway volume is attached, Railway supplies
# RAILWAY_VOLUME_MOUNT_PATH automatically. Otherwise, fall back to ./data.
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.getenv("DATA_DIR", "./data"))
TRADE_LOG = os.path.join(DATA_DIR, "paper_trades.csv")

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY", "")
_private_key = None

TRADE_FIELDS = [
    "signal_time_utc",
    "ticker",
    "trigger_result",
    "trigger_streak",
    "side",
    "entry_cents",
    "market_age_minutes",
    "contracts",
    "status",
    "settlement",
    "won",
    "gross_pnl_cents",
    "settled_time_utc",
]


def load_private_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    if not API_KEY_ID or not PRIVATE_KEY_TEXT:
        raise RuntimeError("Demo order placement requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY.")
    pem = PRIVATE_KEY_TEXT.replace("\\n", "\n").encode("utf-8")
    _private_key = serialization.load_pem_private_key(pem, password=None)
    return _private_key


def auth_headers(method, path):
    timestamp = str(int(time.time() * 1000))
    sign_path = urlparse(BASE_URL + path).path
    message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")
    signature = load_private_key().sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
    }


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def dollars_to_cents(value):
    return None if value in (None, "") else round(float(value) * 100, 4)


def get_json(path, params=None):
    r = requests.get(BASE_URL + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_recent_settled_markets():
    data = get_json("/historical/markets", {"series_ticker": SERIES, "limit": 1000})
    markets = [m for m in data.get("markets", []) if m.get("result") in {"yes", "no"}]
    markets.sort(key=lambda m: m.get("close_time", ""))
    return markets


def get_open_markets():
    data = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    return data.get("markets", [])


def calculate_streak(markets):
    if not markets:
        return None, 0
    latest = markets[-1]["result"]
    count = 0
    for m in reversed(markets):
        if m.get("result") == latest:
            count += 1
        else:
            break
    return latest, count


def opposite_side(result):
    return "no" if result == "yes" else "yes"


def market_age_minutes(market):
    opened = parse_time(market.get("open_time"))
    return None if opened is None else (utc_now() - opened).total_seconds() / 60.0


def qualifying_current_market(open_markets):
    candidates = []
    for m in open_markets:
        age = market_age_minutes(m)
        if age is not None and 0 <= age <= ENTRY_WINDOW_MINUTES:
            candidates.append((age, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def entry_ask_cents(market, outcome_side):
    field = "yes_ask_dollars" if outcome_side == "yes" else "no_ask_dollars"
    return dollars_to_cents(market.get(field))


def place_demo_ioc_order(ticker, outcome_side, entry_cents):
    path = "/portfolio/events/orders"
    q = entry_cents / 100.0
    if outcome_side == "yes":
        book_side, price = "bid", q
    else:
        book_side, price = "ask", 1.0 - q
    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": book_side,
        "count": f"{CONTRACTS:.2f}",
        "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "reduce_only": False,
        "subaccount": 0,
        "exchange_index": 0,
    }
    r = requests.post(BASE_URL + path, headers=auth_headers("POST", path), json=payload, timeout=15)
    print("DEMO ORDER:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()


def filled_quantity(resp):
    try:
        return float(resp.get("fill_count", "0"))
    except (TypeError, ValueError):
        return 0.0


def ensure_trade_log():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            writer.writeheader()


def read_trade_log():
    ensure_trade_log()
    with open(TRADE_LOG, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_trade_log(rows):
    ensure_trade_log()
    temp_path = TRADE_LOG + ".tmp"
    with open(temp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, TRADE_LOG)


def logged_tickers():
    return {r["ticker"] for r in read_trade_log() if r.get("ticker")}


def log_signal(ticker, trigger_result, streak, side, entry_cents, age_minutes):
    rows = read_trade_log()
    if any(r.get("ticker") == ticker for r in rows):
        return
    rows.append({
        "signal_time_utc": utc_now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "trigger_result": trigger_result,
        "trigger_streak": str(streak),
        "side": side,
        "entry_cents": f"{float(entry_cents):.4f}",
        "market_age_minutes": f"{float(age_minutes):.4f}",
        "contracts": f"{CONTRACTS:.4f}",
        "status": "PENDING",
        "settlement": "",
        "won": "",
        "gross_pnl_cents": "",
        "settled_time_utc": "",
    })
    write_trade_log(rows)
    print(f"TRACKED: {ticker} {side.upper()} at {entry_cents}c -> {TRADE_LOG}")


def settle_pending_trades(settled_markets):
    rows = read_trade_log()
    settled_by_ticker = {m.get("ticker"): m for m in settled_markets if m.get("ticker")}
    changed = False
    newly_settled = 0

    for r in rows:
        if r.get("status") != "PENDING":
            continue
        market = settled_by_ticker.get(r.get("ticker"))
        if not market:
            continue

        settlement = market.get("result")
        side = r.get("side")
        entry = float(r.get("entry_cents") or 0)
        contracts = float(r.get("contracts") or 1)
        won = settlement == side
        gross_pnl = ((100.0 - entry) if won else -entry) * contracts

        r["status"] = "SETTLED"
        r["settlement"] = settlement or ""
        r["won"] = "TRUE" if won else "FALSE"
        r["gross_pnl_cents"] = f"{gross_pnl:.4f}"
        r["settled_time_utc"] = market.get("close_time") or utc_now().isoformat(timespec="seconds")
        changed = True
        newly_settled += 1
        print(
            f"SETTLED: {r['ticker']} bought {side.upper()} at {entry:.1f}c -> "
            f"{settlement.upper()} | {'WIN' if won else 'LOSS'} | gross P/L={gross_pnl:.1f}c"
        )

    if changed:
        write_trade_log(rows)
        print_summary(rows)
    return newly_settled


def print_summary(rows=None):
    if rows is None:
        rows = read_trade_log()
    settled = [r for r in rows if r.get("status") == "SETTLED"]
    pending = [r for r in rows if r.get("status") == "PENDING"]
    if not settled:
        print(f"SUMMARY: settled=0 pending={len(pending)}")
        return
    wins = sum(1 for r in settled if r.get("won") == "TRUE")
    pnl = sum(float(r.get("gross_pnl_cents") or 0) for r in settled)
    avg_entry = sum(float(r.get("entry_cents") or 0) for r in settled) / len(settled)
    win_rate = 100.0 * wins / len(settled)
    print(
        f"SUMMARY: settled={len(settled)} wins={wins} win_rate={win_rate:.1f}% "
        f"avg_entry={avg_entry:.1f}c gross_pnl={pnl:.1f}c pending={len(pending)}"
    )


def main():
    ensure_trade_log()
    print("KALSHI BTC STREAK BOT")
    print(f"MODE={MODE} SERIES={SERIES} STREAK={STREAK_TRIGGER} CAP={MAX_ENTRY_CENTS}c WINDOW={ENTRY_WINDOW_MINUTES}m")
    print(f"TRACKING FILE={TRADE_LOG}")
    traded_markets = logged_tickers()
    print_summary()

    while True:
        try:
            settled = get_recent_settled_markets()
            settle_pending_trades(settled)

            last_result, streak = calculate_streak(settled)
            print(f"{utc_now().isoformat(timespec='seconds')} last={last_result} streak={streak}")
            if streak < STREAK_TRIGGER:
                time.sleep(POLL_SECONDS)
                continue

            market = qualifying_current_market(get_open_markets())
            if market is None:
                print("No qualifying market inside entry window.")
                time.sleep(POLL_SECONDS)
                continue

            ticker = market["ticker"]
            age = market_age_minutes(market)
            side = opposite_side(last_result)
            ask = entry_ask_cents(market, side)
            print(f"Watch {ticker}: buy {side.upper()} age={age:.2f}m ask={ask}c cap={MAX_ENTRY_CENTS}c")

            if ticker in traded_markets or ask is None or ask > MAX_ENTRY_CENTS:
                time.sleep(POLL_SECONDS)
                continue

            print(f"QUALIFIED: {side.upper()} at {ask}c")
            if MODE == "paper":
                print("PAPER SIGNAL ONLY â no order sent.")
                log_signal(ticker, last_result, streak, side, ask, age)
                traded_markets.add(ticker)
            elif not PLACE_ORDERS:
                print("DEMO signal only â PLACE_DEMO_ORDERS=false.")
                log_signal(ticker, last_result, streak, side, ask, age)
                traded_markets.add(ticker)
            else:
                resp = place_demo_ioc_order(ticker, side, ask)
                if filled_quantity(resp) > 0:
                    log_signal(ticker, last_result, streak, side, ask, age)
                    traded_markets.add(ticker)
                else:
                    print("No fill; can retry while still inside the entry window.")

            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("ERROR:", repr(e))
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

