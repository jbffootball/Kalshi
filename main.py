import os
import time
import uuid
import base64
import csv
import json
import re
import datetime as dt
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation

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
MIN_ENTRY_CENTS = int(os.getenv("MIN_ENTRY_CENTS", "1"))
ENTRY_WINDOW_MINUTES = float(os.getenv("ENTRY_WINDOW_MINUTES", "3"))
CONTRACTS = float(os.getenv("CONTRACTS", "1"))
POLL_ACTIVE_SECONDS = float(os.getenv("POLL_ACTIVE_SECONDS", "2"))
POLL_IDLE_SECONDS = float(os.getenv("POLL_IDLE_SECONDS", "60"))
POLL_ORDER_SECONDS = float(os.getenv("POLL_ORDER_SECONDS", "10"))
ORDER_SUBMIT_RETRIES = int(os.getenv("ORDER_SUBMIT_RETRIES", "3"))
ORDER_RETRY_SECONDS = float(os.getenv("ORDER_RETRY_SECONDS", "2"))
RESULT_POLL_SECONDS = float(os.getenv("RESULT_POLL_SECONDS", "1"))
WAKE_BEFORE_BOUNDARY_SECONDS = float(os.getenv("WAKE_BEFORE_BOUNDARY_SECONDS", "3"))
MAX_CLOSE_CAPTURE_LAG_SECONDS = float(os.getenv("MAX_CLOSE_CAPTURE_LAG_SECONDS", "3"))
DEBUG_LIVE_DATA = os.getenv("DEBUG_LIVE_DATA", "true").strip().lower() == "true"

# Persistent paper-trade log. If a Railway volume is attached, Railway supplies
# RAILWAY_VOLUME_MOUNT_PATH automatically. Otherwise, fall back to ./data.
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.getenv("DATA_DIR", "./data"))
TRADE_LOG = os.path.join(DATA_DIR, "paper_trades.csv")
SESSION_LOG = os.path.join(DATA_DIR, "btc_session_results.csv")

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

SESSION_FIELDS = [
    "captured_time_utc",
    "ticker",
    "event_ticker",
    "open_time_utc",
    "close_time_utc",
    "kalshi_start_btc",
    "kalshi_close_btc",
    "close_distance",
    "immediate_result",
    "close_capture_lag_seconds",
    "close_price_source",
    "live_price_path",
    "milestone_id",
    "note",
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


def authenticated_request(method, path, json_body=None):
    """Authenticated Kalshi Demo request using the bot's existing auth_headers()."""
    method = method.upper()
    headers = auth_headers(method, path)
    url = BASE_URL + path

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    elif method == "POST":
        r = requests.post(url, headers=headers, json=json_body, timeout=15)
    elif method == "DELETE":
        r = requests.delete(url, headers=headers, timeout=15)
    else:
        raise ValueError(f"Unsupported authenticated method: {method}")

    print(f"AUTH {method} {path}: {r.status_code} {r.text[:500]}")
    r.raise_for_status()
    return r.json() if r.text else {}


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def seconds_to_next_15m_boundary(now=None):
    now = now or utc_now()
    seconds_into_block = (now.minute % 15) * 60 + now.second + now.microsecond / 1_000_000
    return 900 - seconds_into_block


def idle_sleep_seconds():
    # Never sleep past the next 15-minute market opening.
    wake_in = max(1.0, seconds_to_next_15m_boundary() - WAKE_BEFORE_BOUNDARY_SECONDS)
    return min(POLL_IDLE_SECONDS, wake_in)


def sleep_active():
    time.sleep(POLL_ACTIVE_SECONDS)


def sleep_idle():
    time.sleep(idle_sleep_seconds())


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def dollars_to_cents(value):
    return None if value in (None, "") else round(float(value) * 100, 4)


def get_json(path, params=None):
    r = requests.get(BASE_URL + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()




PRICE_TEXT_RE = re.compile(
    r"(?:price\s+to\s+beat|target\s+price|target)\s*[:Â·-]?\s*\$?\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float)):
            return Decimal(str(value))
        cleaned = str(value).strip().replace("$", "").replace(",", "")
        return Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError):
        return None


def format_decimal(value, places=None):
    if value is None:
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if places is not None:
        q = Decimal("1").scaleb(-places)
        value = value.quantize(q)
    return format(value, "f")


def extract_kalshi_start_btc(market):
    """
    Kalshi-only start/strike price.

    Prefer the human-facing market text ("Price to beat" / "Target Price"),
    because that is the number Kalshi displays to the trader. Fall back to
    floor_strike only if the display text is unavailable.
    """
    for field in ("yes_sub_title", "no_sub_title", "subtitle", "sub_title", "title"):
        raw = market.get(field)
        if not raw:
            continue
        match = PRICE_TEXT_RE.search(str(raw))
        if match:
            return decimal_or_none(match.group(1)), f"market.{field}"

    floor = decimal_or_none(market.get("floor_strike"))
    if floor is not None and floor > 1000:
        return floor, "market.floor_strike"

    custom = market.get("custom_strike")
    if isinstance(custom, dict):
        for key in ("price", "value", "strike", "target"):
            candidate = decimal_or_none(custom.get(key))
            if candidate is not None and candidate > 1000:
                return candidate, f"market.custom_strike.{key}"

    return None, ""


def ensure_session_log():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SESSION_LOG):
        with open(SESSION_LOG, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=SESSION_FIELDS).writeheader()


def read_session_log():
    ensure_session_log()
    with open(SESSION_LOG, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_session_log(rows):
    ensure_session_log()
    tmp = SESSION_LOG + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SESSION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, SESSION_LOG)


def logged_session_tickers():
    return {r.get("ticker") for r in read_session_log() if r.get("ticker")}


def recursive_numeric_candidates(obj, path="details"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"
            if isinstance(value, (dict, list)):
                yield from recursive_numeric_candidates(value, child_path)
            else:
                number = decimal_or_none(value)
                if number is not None:
                    yield child_path, number
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from recursive_numeric_candidates(value, f"{path}[{i}]")


def extract_btc_price_from_live_data(live_data, start_btc):
    """
    Extract the BTC price from Kalshi's own live-data payload.

    The live-data `details` schema is milestone-type specific, so use a
    conservative scored extractor. A candidate must look like a price field
    and be near the session's displayed BTC target. Target/strike fields are
    explicitly excluded so we never mistake the starting price for the close.
    """
    details = (live_data or {}).get("details", {})
    if not isinstance(details, (dict, list)):
        return None, ""

    preferred = ("btc", "underlying", "index", "spot", "current", "last", "price", "value")
    excluded = (
        "target", "strike", "threshold", "yes_", "no_", "odds",
        "probab", "contract", "volume", "count", "fee", "payout"
    )

    scored = []
    for path, number in recursive_numeric_candidates(details):
        low = path.lower()
        if any(token in low for token in excluded):
            continue
        if not any(token in low for token in preferred):
            continue

        if start_btc is not None:
            if number < start_btc * Decimal("0.5") or number > start_btc * Decimal("1.5"):
                continue
        elif number < Decimal("1000"):
            continue

        score = 0
        weights = {
            "btc": 12, "underlying": 10, "index": 9, "spot": 8,
            "current": 7, "last": 5, "price": 5, "value": 1,
        }
        for token, weight in weights.items():
            if token in low:
                score += weight
        scored.append((score, path, number))

    if not scored:
        return None, ""

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, path, number = scored[0]
    return number, path


def get_crypto_milestones(event_ticker):
    """
    Milestones are Kalshi metadata objects. For live charts, the milestone ID
    is also the key used by Kalshi's /live_data API.
    """
    params = {
        "limit": 50,
        "category": "Crypto",
        "related_event_ticker": event_ticker,
    }
    data = get_json("/milestones", params)
    milestones = data.get("milestones", [])
    if not milestones:
        # Some milestones may list the event as primary rather than related;
        # fall back to a broader Crypto query and filter locally.
        data = get_json("/milestones", {"limit": 200, "category": "Crypto"})
        milestones = []
        for m in data.get("milestones", []):
            related = set(m.get("related_event_tickers") or [])
            primary = set(m.get("primary_event_tickers") or [])
            if event_ticker in related or event_ticker in primary:
                milestones.append(m)
    return milestones


def fetch_kalshi_displayed_btc(market, start_btc):
    """
    Kalshi-only live BTC display.

    We intentionally do not call Coinbase, Binance, TradingView, or any other
    external BTC source. If Kalshi does not expose a usable live BTC value for
    this session through its live-data API, return None and flag the session.
    """
    event_ticker = market.get("event_ticker")
    if not event_ticker:
        return None, "", "", ""

    milestones = get_crypto_milestones(event_ticker)
    for milestone in milestones:
        milestone_id = milestone.get("id")
        if not milestone_id:
            continue
        try:
            payload = get_json(f"/live_data/milestone/{milestone_id}")
        except Exception as exc:
            print(f"KALSHI LIVE DATA WARNING: milestone={milestone_id} {exc!r}")
            continue
        live_data = payload.get("live_data", payload)
        price, path = extract_btc_price_from_live_data(live_data, start_btc)
        if price is not None:
            return price, "kalshi_live_data", path, milestone_id

    return None, "", "", ""




def choose_current_open_session():
    """
    Pick the currently active KXBTC15M session by its scheduled open/close
    times. This happens BEFORE close so we do not need to rediscover the
    market afterward.
    """
    now = utc_now()
    candidates = []
    for market in get_open_markets():
        opened = parse_time(market.get("open_time"))
        closes = parse_time(market.get("close_time"))
        if opened is None or closes is None:
            continue
        if opened <= now < closes:
            candidates.append((closes, market))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def prepare_current_session():
    """
    Cache everything we can before the boundary:
      - exact ticker
      - Kalshi-displayed start/target BTC price
      - close timestamp
      - crypto milestone IDs used by Kalshi live_data

    No outside data source is used.
    """
    market = choose_current_open_session()
    if market is None:
        return None

    ticker = market.get("ticker")
    if not ticker or ticker in logged_session_tickers():
        return None

    close_time = parse_time(market.get("close_time"))
    start_btc, start_source = extract_kalshi_start_btc(market)

    milestones = get_crypto_milestones(market.get("event_ticker")) if market.get("event_ticker") else []
    milestone_ids = [m.get("id") for m in milestones if m.get("id")]

    prepared = {
        "ticker": ticker,
        "market": market,
        "close_time": close_time,
        "start_btc": start_btc,
        "start_source": start_source,
        "milestone_ids": milestone_ids,
        "prepared_at": utc_now(),
    }

    print(
        f"PREPARED CLOSE: {ticker} "
        f"start={format_decimal(start_btc) or '?'} "
        f"close_time={market.get('close_time')} "
        f"milestones={len(milestone_ids)}"
    )
    return prepared


def fetch_prepared_kalshi_close(prepared):
    """
    Read Kalshi live_data at the boundary using milestone IDs cached before
    close. This deliberately avoids rediscovering the market after it closes.
    """
    start_btc = prepared.get("start_btc")
    for milestone_id in prepared.get("milestone_ids") or []:
        try:
            payload = get_json(f"/live_data/milestone/{milestone_id}")
        except Exception as exc:
            print(
                f"KALSHI LIVE DATA WARNING: milestone={milestone_id} "
                f"{exc!r}"
            )
            continue

        live_data = payload.get("live_data", payload)

        if DEBUG_LIVE_DATA:
            try:
                print(
                    "LIVE DATA RAW: "
                    + json.dumps(
                        {
                            "milestone_id": milestone_id,
                            "live_data": live_data,
                        },
                        sort_keys=True,
                        default=str,
                    )
                )
            except Exception as exc:
                print(f"LIVE DATA RAW LOG ERROR: {exc!r}")

        price, path = extract_btc_price_from_live_data(live_data, start_btc)
        if price is not None:
            print(
                f"LIVE DATA EXTRACTED: milestone={milestone_id} "
                f"path={path} value={format_decimal(price)}"
            )
            return price, "kalshi_live_data_cached_milestone", path, milestone_id

    return None, "", "", ""


def capture_prepared_session(prepared):
    """
    Capture the prepared session at/just after its scheduled close.

    We use the exact market and milestone IDs cached while the session was
    still open. If the boundary is missed by more than the safety tolerance,
    flag it rather than substituting a later value.
    """
    if not prepared:
        return False

    market = prepared["market"]
    ticker = prepared["ticker"]

    if ticker in logged_session_tickers():
        return False

    close_time = prepared.get("close_time")
    if close_time is None:
        return append_session_result(
            market, prepared.get("start_btc"), None, "", "", "", 9999,
            note="prepared session missing close_time"
        )

    now = utc_now()
    if now < close_time:
        return False

    capture_lag = (now - close_time).total_seconds()
    start_btc = prepared.get("start_btc")

    if capture_lag > MAX_CLOSE_CAPTURE_LAG_SECONDS:
        return append_session_result(
            market,
            start_btc,
            None,
            "",
            "",
            "",
            capture_lag,
            note=(
                f"prepared boundary capture was {capture_lag:.3f}s late; "
                "no external/history substitution allowed"
            ),
        )

    if start_btc is None:
        return append_session_result(
            market, None, None, "", "", "", capture_lag,
            note="Kalshi displayed starting/target BTC price unavailable before close"
        )

    close_btc, source, live_path, milestone_id = fetch_prepared_kalshi_close(prepared)

    if close_btc is None:
        return append_session_result(
            market,
            start_btc,
            None,
            "",
            "",
            milestone_id,
            capture_lag,
            note=(
                "Kalshi live BTC display unavailable from cached live_data "
                "at boundary; session flagged"
            ),
        )

    return append_session_result(
        market,
        start_btc,
        close_btc,
        source,
        live_path,
        milestone_id,
        capture_lag,
        note=f"start_source={prepared.get('start_source','')}; prepared_before_close=true",
    )


def capture_prepared_if_due(prepared):
    """
    When we are within WAKE_BEFORE_BOUNDARY_SECONDS of the prepared close,
    wait precisely for the boundary and capture immediately.

    Returns (captured, prepared_after).
    """
    if not prepared:
        return False, None

    close_time = prepared.get("close_time")
    if close_time is None:
        return False, prepared

    seconds_left = (close_time - utc_now()).total_seconds()

    if 0 < seconds_left <= WAKE_BEFORE_BOUNDARY_SECONDS:
        print(
            f"BOUNDARY ARMED: {prepared['ticker']} "
            f"closing in {seconds_left:.3f}s"
        )
        time.sleep(seconds_left)
        captured = capture_prepared_session(prepared)
        return captured, None if captured else prepared

    if seconds_left <= 0:
        captured = capture_prepared_session(prepared)
        return captured, None if captured else prepared

    return False, prepared

def get_latest_market_closed_by_time():
    """
    Find the newest KXBTC15M session whose scheduled close_time has passed.
    No result/status filter is used because we do not wait for determination.
    """
    now = utc_now()
    now_ts = int(now.timestamp())
    data = get_json(
        "/markets",
        {
            "series_ticker": SERIES,
            "min_close_ts": now_ts - 3600,
            "max_close_ts": now_ts,
            "limit": 100,
        },
    )
    markets = []
    for market in data.get("markets", []):
        close_time = parse_time(market.get("close_time"))
        if close_time is not None and close_time <= now:
            markets.append(market)
    if not markets:
        return None
    markets.sort(key=lambda m: m.get("close_time") or "")
    return markets[-1]


def append_session_result(market, start_btc, close_btc, source, live_path, milestone_id,
                          capture_lag, note=""):
    rows = read_session_log()
    ticker = market.get("ticker")
    if any(r.get("ticker") == ticker for r in rows):
        return False

    close_distance = None
    immediate_result = "flag"
    if start_btc is not None and close_btc is not None:
        close_distance = close_btc - start_btc
        if close_distance > 0:
            immediate_result = "yes"
        elif close_distance < 0:
            immediate_result = "no"
        else:
            immediate_result = "tie"

    rows.append({
        "captured_time_utc": utc_now().isoformat(timespec="microseconds"),
        "ticker": ticker or "",
        "event_ticker": market.get("event_ticker") or "",
        "open_time_utc": market.get("open_time") or "",
        "close_time_utc": market.get("close_time") or "",
        "kalshi_start_btc": format_decimal(start_btc),
        "kalshi_close_btc": format_decimal(close_btc),
        "close_distance": format_decimal(close_distance),
        "immediate_result": immediate_result,
        "close_capture_lag_seconds": f"{float(capture_lag):.3f}",
        "close_price_source": source,
        "live_price_path": live_path,
        "milestone_id": milestone_id,
        "note": note,
    })
    write_session_log(rows)

    if immediate_result in {"yes", "no", "tie"}:
        print(
            f"IMMEDIATE CLOSE: {ticker} start={format_decimal(start_btc)} "
            f"close={format_decimal(close_btc)} distance={format_decimal(close_distance)} "
            f"=> {immediate_result.upper()} lag={capture_lag:.3f}s"
        )
    else:
        print(f"IMMEDIATE CLOSE FLAGGED: {ticker} | {note}")

    return True


def capture_just_closed_session():
    """
    Capture the Kalshi-displayed start and close prices as close to the
    scheduled session boundary as possible.

    This is the only source used for the immediate YES/NO streak. Official
    Kalshi determination and the Past page are not required.
    """
    market = get_latest_market_closed_by_time()
    if market is None:
        return False

    ticker = market.get("ticker")
    if not ticker or ticker in logged_session_tickers():
        return False

    close_time = parse_time(market.get("close_time"))
    if close_time is None:
        return append_session_result(
            market, None, None, "", "", "", 9999,
            note="missing close_time"
        )

    capture_lag = (utc_now() - close_time).total_seconds()

    # Never use a current BTC display value as though it were the historical
    # close if the bot woke up too late (e.g. after a restart).
    if capture_lag > MAX_CLOSE_CAPTURE_LAG_SECONDS:
        return append_session_result(
            market, *extract_kalshi_start_btc(market)[:1], None,
            "", "", "", capture_lag,
            note=(
                f"close capture was {capture_lag:.3f}s late; "
                "no external/history substitution allowed"
            )
        )

    start_btc, start_source = extract_kalshi_start_btc(market)
    if start_btc is None:
        return append_session_result(
            market, None, None, "", "", "", capture_lag,
            note="Kalshi displayed starting/target BTC price unavailable"
        )

    close_btc, close_source, live_path, milestone_id = fetch_kalshi_displayed_btc(
        market, start_btc
    )
    if close_btc is None:
        return append_session_result(
            market, start_btc, None, "", "", milestone_id, capture_lag,
            note=(
                "Kalshi live BTC display unavailable from live_data; "
                "session flagged and no external source used"
            )
        )

    return append_session_result(
        market,
        start_btc,
        close_btc,
        close_source,
        live_path,
        milestone_id,
        capture_lag,
        note=f"start_source={start_source}",
    )


def session_rows_as_markets():
    """
    Convert our immediate session observations to the minimal market-like shape
    already used by the streak and trade-settlement helpers.
    """
    rows = read_session_log()
    markets = []
    for row in rows:
        result = (row.get("immediate_result") or "").lower()
        markets.append({
            "ticker": row.get("ticker"),
            "close_time": row.get("close_time_utc"),
            "result": result,
            "kalshi_start_btc": row.get("kalshi_start_btc"),
            "kalshi_close_btc": row.get("kalshi_close_btc"),
            "close_distance": row.get("close_distance"),
            "close_capture_lag_seconds": row.get("close_capture_lag_seconds"),
        })
    markets.sort(key=lambda m: m.get("close_time") or "")
    return markets


def print_last_five_immediate(markets):
    recent = markets[-5:]
    print("LAST 5 IMMEDIATE (Kalshi displayed start -> close):")
    if not recent:
        print("  none")
        return
    for m in recent:
        print(
            f"  {m.get('close_time','?')} | {m.get('ticker','?')} | "
            f"start={m.get('kalshi_start_btc','')} "
            f"close={m.get('kalshi_close_btc','')} "
            f"distance={m.get('close_distance','')} | "
            f"{(m.get('result') or '?').upper()}"
        )
    print("  SEQUENCE: " + " | ".join((m.get("result") or "?").upper() for m in recent))

def get_recent_settled_markets():
    # IMPORTANT: Recent settlements belong on the live /markets endpoint.
    # /historical/markets is only for markets older than Kalshi's historical cutoff.
    # Restrict to the last 24 hours so a stale historical page can never drive the streak.
    since_ts = int((utc_now() - dt.timedelta(hours=24)).timestamp())
    data = get_json(
        "/markets",
        {
            "series_ticker": SERIES,
            "status": "settled",
            "min_settled_ts": since_ts,
            "limit": 1000,
        },
    )
    markets = [m for m in data.get("markets", []) if m.get("result") in {"yes", "no"}]

    # settlement_ts is the preferred chronology for settled markets. Fall back to
    # close_time only if settlement_ts is absent.
    def settlement_sort_key(m):
        return m.get("settlement_ts") or m.get("close_time") or ""

    markets.sort(key=settlement_sort_key)
    return markets


def get_open_markets():
    data = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    return data.get("markets", [])


def last_five_settled(markets):
    """Return only the five most recent settled markets, oldest -> newest."""
    return markets[-5:]


def print_last_five(markets):
    recent = last_five_settled(markets)
    print("LAST 5 SETTLED (oldest -> newest):")
    if not recent:
        print("  none")
        return
    for m in recent:
        close_time = m.get("close_time", "?")
        ticker = m.get("ticker", "?")
        result = (m.get("result") or "?").upper()
        print(f"  {close_time} | {ticker} | {result}")
    sequence = " | ".join((m.get("result") or "?").upper() for m in recent)
    print(f"  SEQUENCE: {sequence}")


def calculate_streak(markets):
    # Decision streak uses only our Kalshi start-vs-close immediate outcomes.
    # TIE or FLAG intentionally breaks the streak and prevents a trade.
    recent = markets[-5:]
    if not recent:
        return None, 0
    latest = (recent[-1].get("result") or "").lower()
    if latest not in {"yes", "no"}:
        return latest or None, 0
    count = 0
    for m in reversed(recent):
        if (m.get("result") or "").lower() == latest:
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


def actual_fill_cents(resp, side, fallback_cents):
    """Use Kalshi's returned average fill price when available."""
    raw = resp.get("average_price") or resp.get("price")
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return float(fallback_cents)

    # For YES, returned price is the YES contract price. For NO orders the
    # current order payload uses the complementary book price, so convert back.
    if side == "yes":
        return round(px * 100.0, 4)
    return round((1.0 - px) * 100.0, 4)


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



def place_demo_resting_limit_order(ticker, side, limit_cents, expiration_ts):
    """
    Place one resting limit order that expires automatically at the end of
    the configured entry window.
    """
    if MODE != "demo":
        raise RuntimeError("Resting demo orders require MODE=demo")
    if not PLACE_ORDERS:
        raise RuntimeError("PLACE_DEMO_ORDERS is false")

    client_order_id = str(uuid.uuid4())

    # V2 event-market book quotes everything from the YES side.
    # Buy YES = bid at YES price.
    # Buy NO at X cents = ask YES at (1-X) dollars.
    if side == "yes":
        book_side = "bid"
        price_dollars = limit_cents / 100.0
    else:
        book_side = "ask"
        price_dollars = 1.0 - (limit_cents / 100.0)

    payload = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": book_side,
        "count": f"{CONTRACTS:.2f}",
        "price": f"{price_dollars:.4f}",
        "time_in_force": "good_till_canceled",
        "expiration_time": int(expiration_ts),
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }

    resp = authenticated_request("POST", "/portfolio/events/orders", json_body=payload)
    order = resp.get("order", resp)
    print(f"RESTING LIMIT ORDER: {order}")
    return order


def get_demo_order(order_id):
    resp = authenticated_request("GET", f"/portfolio/orders/{order_id}")
    return resp.get("order", resp)


def cancel_demo_order(order_id):
    try:
        resp = authenticated_request("DELETE", f"/portfolio/events/orders/{order_id}")
        print(f"CANCEL RESULT: {resp}")
        return resp
    except Exception as e:
        print(f"CANCEL WARNING: {e!r}")
        return None


def market_entry_expiration_ts(market):
    """
    Expiration = market open time + ENTRY_WINDOW_MINUTES.
    """
    open_time = parse_time(market.get("open_time"))
    if open_time is None:
        raise ValueError("market missing open_time")
    return int((open_time + dt.timedelta(minutes=ENTRY_WINDOW_MINUTES)).timestamp())


def order_fill_count(order):
    for key in ("fill_count_fp", "fill_count"):
        try:
            return float(order.get(key, 0) or 0)
        except (TypeError, ValueError):
            pass
    return 0.0


def order_remaining_count(order):
    for key in ("remaining_count_fp", "remaining_count"):
        try:
            return float(order.get(key, 0) or 0)
        except (TypeError, ValueError):
            pass
    return 0.0


def submit_resting_order_with_retry(ticker, side, limit_cents, expiration_ts):
    """
    Try to submit the resting limit order up to ORDER_SUBMIT_RETRIES times.
    Stop immediately once Kalshi accepts an order. Never retry after the
    configured entry window has expired.
    """
    last_error = None

    for attempt in range(1, ORDER_SUBMIT_RETRIES + 1):
        now_ts = utc_now().timestamp()
        if now_ts >= expiration_ts:
            print(
                f"SUBMIT STOP: entry window expired before attempt "
                f"{attempt}/{ORDER_SUBMIT_RETRIES}."
            )
            break

        try:
            print(
                f"SUBMIT ATTEMPT {attempt}/{ORDER_SUBMIT_RETRIES}: "
                f"{ticker} {side.upper()} @ {limit_cents}c"
            )
            order = place_demo_resting_limit_order(
                ticker, side, limit_cents, expiration_ts
            )
            print(
                f"SUBMIT ACCEPTED on attempt {attempt}: "
                f"order_id={order.get('order_id')}"
            )
            return order

        except Exception as e:
            last_error = e
            print(
                f"SUBMIT REJECTED attempt {attempt}/{ORDER_SUBMIT_RETRIES}: "
                f"{e!r}"
            )

            if attempt >= ORDER_SUBMIT_RETRIES:
                break

            seconds_left = expiration_ts - utc_now().timestamp()
            if seconds_left <= 0:
                break

            sleep_for = min(ORDER_RETRY_SECONDS, max(0.0, seconds_left))
            if sleep_for <= 0:
                break

            print(f"Retrying in {sleep_for:g}s...")
            time.sleep(sleep_for)

    print(
        f"SUBMIT FAILED: no resting order accepted for {ticker} "
        f"after up to {ORDER_SUBMIT_RETRIES} attempts."
    )
    if last_error is not None:
        print(f"LAST SUBMIT ERROR: {last_error!r}")
    return None


def get_market_by_ticker(ticker):
    data = get_json(f"/markets/{ticker}")
    return data.get("market", data)


def get_most_recent_closed_market():
    now_ts = int(utc_now().timestamp())
    data = get_json("/markets", {
        "series_ticker": SERIES,
        "status": "closed",
        "min_close_ts": now_ts - 3600,
        "max_close_ts": now_ts,
        "limit": 50,
    })
    markets = data.get("markets", [])
    if not markets:
        return None
    markets.sort(key=lambda m: m.get("close_time") or "")
    return markets[-1]


def current_entry_deadline_ts():
    opens = get_open_markets()
    if opens:
        opens.sort(key=lambda m: m.get("open_time") or "")
        open_time = parse_time(opens[-1].get("open_time"))
        if open_time:
            return (open_time + dt.timedelta(minutes=ENTRY_WINDOW_MINUTES)).timestamp()
    return (utc_now() + dt.timedelta(minutes=ENTRY_WINDOW_MINUTES)).timestamp()


def wait_for_previous_result():
    previous = get_most_recent_closed_market()
    if not previous:
        print("FAST RESULT: no recently closed market found.")
        return None
    ticker = previous.get("ticker")
    result = (previous.get("result") or "").lower()
    if result in {"yes", "no"}:
        observed_at = utc_now()
        print(f"PREVIOUS RESULT ALREADY AVAILABLE: {ticker} = {result.upper()}")
        log_determination_delay(previous, observed_at)
        return previous

    started = utc_now()
    deadline_ts = current_entry_deadline_ts()
    print(f"WAITING FOR PREVIOUS RESULT: {ticker}")

    while utc_now().timestamp() < deadline_ts:
        market = get_market_by_ticker(ticker)
        result = (market.get("result") or "").lower()
        status = (market.get("status") or "").lower()
        if result in {"yes", "no"}:
            observed_at = utc_now()
            wait_delay = (observed_at - started).total_seconds()
            print(f"PREVIOUS RESULT AVAILABLE: {ticker} = {result.upper()} status={status} after {wait_delay:.2f}s of polling")
            log_determination_delay(market, observed_at)
            return market
        time.sleep(RESULT_POLL_SECONDS)

    print(f"PREVIOUS RESULT TIMEOUT FOR TRADING: {ticker}. Continuing to measure determination delay only.")
    measurement_deadline = utc_now() + dt.timedelta(minutes=10)
    while utc_now() < measurement_deadline:
        market = get_market_by_ticker(ticker)
        result = (market.get("result") or "").lower()
        status = (market.get("status") or "").lower()
        if result in {"yes", "no"}:
            observed_at = utc_now()
            print(f"LATE PREVIOUS RESULT: {ticker} = {result.upper()} status={status}")
            log_determination_delay(market, observed_at)
            return None
        time.sleep(RESULT_POLL_SECONDS)

    print(f"DETERMINATION MEASUREMENT TIMEOUT: {ticker} still has no yes/no after 10 additional minutes.")
    return None


def merge_fast_result(settled_markets, fast_market):
    by_ticker = {}
    for m in settled_markets:
        if m.get("ticker") and (m.get("result") or "").lower() in {"yes", "no"}:
            by_ticker[m["ticker"]] = m
    if fast_market and fast_market.get("ticker") and (fast_market.get("result") or "").lower() in {"yes", "no"}:
        by_ticker[fast_market["ticker"]] = fast_market
    combined = list(by_ticker.values())
    combined.sort(key=lambda m: m.get("close_time") or "")
    return combined



def log_determination_delay(market, observed_at=None):
    if not market:
        return
    observed_at = observed_at or utc_now()
    ticker = market.get("ticker", "?")
    close_raw = market.get("close_time")
    close_time = parse_time(close_raw) if close_raw else None
    result = (market.get("result") or "").upper()
    status = market.get("status") or "?"
    if close_time is None:
        print(f"DETERMINATION TIMING: {ticker} result={result or '?'} status={status} close_time=unknown observed={observed_at.isoformat()}")
        return
    delay = (observed_at - close_time).total_seconds()
    print(f"SESSION CLOSED: {ticker} at {close_time.isoformat()}")
    print(f"RESULT AVAILABLE: {ticker} = {result or '?'} status={status} observed={observed_at.isoformat()}")
    print(f"DETERMINATION DELAY: {delay:.3f} seconds")


def main():
    ensure_trade_log()
    ensure_session_log()

    print("KALSHI BTC 15-MINUTE BOT")
    print(
        f"MODE={MODE} SERIES={SERIES} STREAK={STREAK_TRIGGER} "
        f"CAP={MAX_ENTRY_CENTS}c WINDOW={ENTRY_WINDOW_MINUTES}m "
        f"ORDER_POLL={POLL_ORDER_SECONDS}s IDLE_POLL={POLL_IDLE_SECONDS}s "
        f"RETRIES={ORDER_SUBMIT_RETRIES} RETRY_WAIT={ORDER_RETRY_SECONDS}s"
    )
    print("OUTCOME SOURCE=Kalshi displayed BTC start/target vs Kalshi live BTC at close")
    print("OFFICIAL DETERMINATION=not used for trading decision")
    print("BOUNDARY CAPTURE=pre-cache open session, then read Kalshi live_data at close")
    print(f"DEBUG_LIVE_DATA={DEBUG_LIVE_DATA}")
    print(f"TRADE LOG={TRADE_LOG}")
    print(f"SESSION LOG={SESSION_LOG}")

    completed_markets = logged_tickers()
    current_order = None
    last_sequence_ticker = None
    prepared_session = None

    print_summary()

    while True:
        try:
            # Prepare the currently open session BEFORE it closes so ticker,
            # start price, and live-data milestone IDs are already cached.
            if prepared_session is None:
                prepared_session = prepare_current_session()

            # As the boundary approaches, block only for the final few seconds
            # and capture from the cached session immediately at close.
            captured, prepared_session = capture_prepared_if_due(prepared_session)

            # If a capture just completed, prepare the newly opened session on
            # the next loop. We intentionally do not rediscover the old one.
            immediate_markets = session_rows_as_markets()
            # Settle our tracker from the same immediate decision outcome.
            # FLAG/TIE sessions never settle a pending trade as a win/loss.
            settle_pending_trades([
                m for m in immediate_markets if m.get("result") in {"yes", "no"}
            ])

            newest_ticker = immediate_markets[-1].get("ticker") if immediate_markets else None
            if captured or newest_ticker != last_sequence_ticker:
                print_last_five_immediate(immediate_markets)
                last_sequence_ticker = newest_ticker
                if captured:
                    print("BOUNDARY CAPTURE COMPLETE: preparing next session.")

            last_result, streak = calculate_streak(immediate_markets)
            print(f"CALCULATED IMMEDIATE: last={last_result} streak={streak}")

            # Existing resting-order safety/monitoring stays intact.
            if current_order is not None:
                order_id = current_order["order_id"]
                ticker = current_order["ticker"]
                side = current_order["side"]
                entry_cents = current_order["entry_cents"]

                order = get_demo_order(order_id)
                status = (order.get("status") or "").lower()
                filled = order_fill_count(order)
                remaining = order_remaining_count(order)

                print(
                    f"ORDER STATUS: {ticker} {side.upper()} status={status} "
                    f"filled={filled:g} remaining={remaining:g}"
                )

                if filled > 0:
                    fill_cents = actual_fill_cents(order, side, entry_cents)
                    print(
                        f"FILLED: {ticker} {side.upper()} qty={filled:g} "
                        f"at ~{fill_cents:.1f}c. MARKET LOCKED."
                    )
                    log_signal(
                        ticker,
                        current_order["trigger_result"],
                        current_order["trigger_streak"],
                        side,
                        fill_cents,
                        current_order["age_at_submit"],
                    )
                    completed_markets.add(ticker)
                    current_order = None
                    sleep_idle()
                    continue

                if status in {"canceled", "executed"} or remaining <= 0:
                    print(f"ORDER CLOSED WITHOUT FILL: {ticker}")
                    current_order = None
                    sleep_idle()
                    continue

                if utc_now().timestamp() >= current_order["expiration_ts"]:
                    print(f"ENTRY WINDOW ENDED: canceling {ticker}")
                    cancel_demo_order(order_id)
                    current_order = None
                    sleep_idle()
                    continue

                time.sleep(POLL_ORDER_SECONDS)
                continue

            # A TIE/FLAG or insufficient immediate streak means no order.
            if streak < STREAK_TRIGGER:
                sleep_idle()
                continue

            market = qualifying_current_market(get_open_markets())
            if market is None:
                print(
                    "No qualifying market inside entry window. "
                    "Will wake before next 15-minute boundary."
                )
                sleep_idle()
                continue

            ticker = market["ticker"]
            age = market_age_minutes(market)
            side = opposite_side(last_result)

            if ticker in completed_markets:
                print(f"SKIP: {ticker} already completed by this bot.")
                sleep_idle()
                continue

            # Locked execution decision: immediately rest at the adjustable
            # Railway MAX_ENTRY_CENTS price; do not chase bid/ask.
            limit_cents = MAX_ENTRY_CENTS
            if limit_cents < MIN_ENTRY_CENTS:
                print(f"SKIP: configured limit {limit_cents}c is invalid.")
                sleep_idle()
                continue

            expiration_ts = market_entry_expiration_ts(market)
            if utc_now().timestamp() >= expiration_ts:
                print(f"SKIP: entry window already expired for {ticker}.")
                sleep_idle()
                continue

            print(
                f"PLACE LIMIT: {ticker} buy {side.upper()} "
                f"{CONTRACTS:g} @ {limit_cents}c age={age:.2f}m "
                f"expires at entry-window end."
            )

            if MODE == "paper":
                print("PAPER LIMIT SIGNAL ONLY â no order sent.")
                log_signal(ticker, last_result, streak, side, limit_cents, age)
                completed_markets.add(ticker)
                sleep_idle()
                continue

            if not PLACE_ORDERS:
                print("DEMO limit signal only â PLACE_DEMO_ORDERS=false.")
                log_signal(ticker, last_result, streak, side, limit_cents, age)
                completed_markets.add(ticker)
                sleep_idle()
                continue

            order = submit_resting_order_with_retry(
                ticker, side, limit_cents, expiration_ts
            )
            if order is None:
                sleep_active()
                continue

            order_id = order.get("order_id")
            if not order_id:
                raise RuntimeError(f"Accepted order response missing order_id: {order}")

            filled = order_fill_count(order)
            if filled > 0:
                fill_cents = actual_fill_cents(order, side, limit_cents)
                print(
                    f"IMMEDIATE FILL: {ticker} {side.upper()} qty={filled:g} "
                    f"at ~{fill_cents:.1f}c. MARKET LOCKED."
                )
                log_signal(ticker, last_result, streak, side, fill_cents, age)
                completed_markets.add(ticker)
                sleep_idle()
                continue

            current_order = {
                "order_id": order_id,
                "ticker": ticker,
                "side": side,
                "entry_cents": limit_cents,
                "expiration_ts": expiration_ts,
                "trigger_result": last_result,
                "trigger_streak": streak,
                "age_at_submit": age,
            }

            print(
                f"ORDER RESTING: {ticker} {side.upper()} @ {limit_cents}c. "
                f"Monitoring every {POLL_ORDER_SECONDS:g}s until fill/expiration."
            )
            time.sleep(POLL_ORDER_SECONDS)

        except KeyboardInterrupt:
            if current_order is not None:
                cancel_demo_order(current_order["order_id"])
            break
        except Exception as e:
            print("ERROR:", repr(e))
            time.sleep(min(10.0, POLL_IDLE_SECONDS))


if __name__ == "__main__":
    main()
