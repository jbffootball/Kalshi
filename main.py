import os
from pathlib import Path
import time
import threading
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

BUILD_VERSION = "FAST_BOUNDARY_SOURCE_GUARD_V4_2026-08-17"

MODE = os.getenv("MODE", "paper").strip().lower()
if MODE not in {"paper", "demo", "live"}:
    raise RuntimeError("MODE must be 'paper', 'demo', or 'live'.")

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
LIVE_TRADING_CONFIRM = os.getenv("LIVE_TRADING_CONFIRM", "").strip()
LIVE_MAX_CONTRACTS = float(os.getenv("LIVE_MAX_CONTRACTS", "1"))

if MODE == "paper":
    # Production market data, simulated orders only.
    BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
    PLACE_ORDERS = False
elif MODE == "demo":
    BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
    PLACE_ORDERS = os.getenv("PLACE_DEMO_ORDERS", "false").strip().lower() == "true"
else:
    # Production market data + production authenticated orders.
    BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
    if not LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE safety lock: MODE=live requires LIVE_TRADING_ENABLED=true."
        )
    if LIVE_TRADING_CONFIRM != "YES_REAL_MONEY":
        raise RuntimeError(
            "LIVE safety lock: set LIVE_TRADING_CONFIRM=YES_REAL_MONEY to enable real orders."
        )
    PLACE_ORDERS = True

if LIVE_MAX_CONTRACTS <= 0:
    raise RuntimeError("LIVE_MAX_CONTRACTS must be > 0.")

SERIES = os.getenv("SERIES", "KXBTC15M")
STREAK_TRIGGER = int(os.getenv("STREAK_TRIGGER", "2"))
MAX_ENTRY_CENTS = int(os.getenv("MAX_ENTRY_CENTS", "40"))
MIN_ENTRY_CENTS = int(os.getenv("MIN_ENTRY_CENTS", "1"))
ENTRY_WINDOW_MINUTES = float(os.getenv("ENTRY_WINDOW_MINUTES", "3"))
CONTRACTS = float(os.getenv("CONTRACTS", "1"))
POLL_ACTIVE_SECONDS = float(os.getenv("POLL_ACTIVE_SECONDS", "2"))
POLL_IDLE_SECONDS = float(os.getenv("POLL_IDLE_SECONDS", "60"))
OUTAGE_BACKOFF_START_SECONDS = float(os.getenv("OUTAGE_BACKOFF_START_SECONDS", "10"))
OUTAGE_BACKOFF_MAX_SECONDS = float(os.getenv("OUTAGE_BACKOFF_MAX_SECONDS", "60"))
POLL_ORDER_SECONDS = float(os.getenv("POLL_ORDER_SECONDS", "10"))
ORDER_SUBMIT_RETRIES = int(os.getenv("ORDER_SUBMIT_RETRIES", "3"))
ORDER_RETRY_SECONDS = float(os.getenv("ORDER_RETRY_SECONDS", "2"))
RESULT_POLL_SECONDS = float(os.getenv("RESULT_POLL_SECONDS", "1"))
WAKE_BEFORE_BOUNDARY_SECONDS = float(os.getenv("WAKE_BEFORE_BOUNDARY_SECONDS", "3"))
MAX_CLOSE_CAPTURE_LAG_SECONDS = float(os.getenv("MAX_CLOSE_CAPTURE_LAG_SECONDS", "3"))
FAST_BOUNDARY_ENABLED = os.getenv("FAST_BOUNDARY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
FAST_DIRECT_RETRY_SECONDS = float(os.getenv("FAST_DIRECT_RETRY_SECONDS", "0.25"))
FAST_DIRECT_RETRY_MAX_SECONDS = float(os.getenv("FAST_DIRECT_RETRY_MAX_SECONDS", "20"))
DEBUG_LIVE_DATA = os.getenv("DEBUG_LIVE_DATA", "true").strip().lower() in {"1", "true", "yes", "on"}
LIVE_DATA_DIAGNOSTIC_ENABLED = os.getenv("LIVE_DATA_DIAGNOSTIC_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
LIVE_DATA_DIAGNOSTIC_INTERVAL_SECONDS = float(os.getenv("LIVE_DATA_DIAGNOSTIC_INTERVAL_SECONDS", "60"))
LIVE_DATA_DIAGNOSTIC_BOUNDARY_GUARD_SECONDS = float(os.getenv("LIVE_DATA_DIAGNOSTIC_BOUNDARY_GUARD_SECONDS", "8"))
STOP_LOSS_CENTS = float(os.getenv("STOP_LOSS_CENTS", "0"))
SELL_EARLY_CENTS = float(os.getenv("SELL_EARLY_CENTS", "100"))
POLL_POSITION_SECONDS = float(os.getenv("POLL_POSITION_SECONDS", "2"))

EXIT_LOG_POLL_SECONDS = float(os.getenv("EXIT_LOG_POLL_SECONDS", "1"))
POSITION_PRICE_LOG = os.getenv(
    "POSITION_PRICE_LOG", "/data/position_price_log.csv"
)
POSITION_SUMMARY_LOG = os.getenv(
    "POSITION_SUMMARY_LOG", "/data/position_summary_log.csv"
)

PERFORMANCE_LOG = os.getenv(
    "PERFORMANCE_LOG", "/data/current_strategy_performance.csv"
)

TAKE_PROFIT_GT3_CENTS = float(os.getenv("TAKE_PROFIT_GT3_CENTS", "95"))
TAKE_PROFIT_2_TO_3_CENTS = float(os.getenv("TAKE_PROFIT_2_TO_3_CENTS", "90"))
TAKE_PROFIT_1_TO_2_CENTS = float(os.getenv("TAKE_PROFIT_1_TO_2_CENTS", "85"))
TAKE_PROFIT_LT1_CENTS = float(os.getenv("TAKE_PROFIT_LT1_CENTS", "80"))

STOP_LOSS_GT3_CENTS = float(os.getenv("STOP_LOSS_GT3_CENTS", "5"))
STOP_LOSS_2_TO_3_CENTS = float(os.getenv("STOP_LOSS_2_TO_3_CENTS", "10"))
STOP_LOSS_1_TO_2_CENTS = float(os.getenv("STOP_LOSS_1_TO_2_CENTS", "15"))
STOP_LOSS_LT1_CENTS = float(os.getenv("STOP_LOSS_LT1_CENTS", "20"))

for _name, _value in {
    "TAKE_PROFIT_GT3_CENTS": TAKE_PROFIT_GT3_CENTS,
    "TAKE_PROFIT_2_TO_3_CENTS": TAKE_PROFIT_2_TO_3_CENTS,
    "TAKE_PROFIT_1_TO_2_CENTS": TAKE_PROFIT_1_TO_2_CENTS,
    "TAKE_PROFIT_LT1_CENTS": TAKE_PROFIT_LT1_CENTS,
    "STOP_LOSS_GT3_CENTS": STOP_LOSS_GT3_CENTS,
    "STOP_LOSS_2_TO_3_CENTS": STOP_LOSS_2_TO_3_CENTS,
    "STOP_LOSS_1_TO_2_CENTS": STOP_LOSS_1_TO_2_CENTS,
    "STOP_LOSS_LT1_CENTS": STOP_LOSS_LT1_CENTS,
}.items():
    if not 0 <= _value <= 100:
        raise RuntimeError(f"{_name} must be between 0 and 100.")

def env_bool(name, default=False):
    return os.getenv(name, "true" if default else "false").strip().lower() in {
        "1", "true", "yes", "on"
    }

TIME_EXIT_ENABLED = env_bool("TIME_EXIT_ENABLED", True)

EXIT_LOGGING_ENABLED = env_bool("EXIT_LOGGING_ENABLED", True)

STRATEGY_SLOTS = [
    {
        "name": "A",
        "enabled": env_bool("STREAK_A_ENABLED", True),
        "streak": int(os.getenv("STREAK_A_LENGTH", "2")),
        "max_entry_cents": int(os.getenv("STREAK_A_MAX_ENTRY_CENTS", "35")),
        "entry_window_minutes": float(os.getenv("STREAK_A_ENTRY_WINDOW_MINUTES", "3")),
        "contracts": float(os.getenv("STREAK_A_CONTRACTS", "1")),
    },
    {
        "name": "B",
        "enabled": env_bool("STREAK_B_ENABLED", True),
        "streak": int(os.getenv("STREAK_B_LENGTH", "3")),
        "max_entry_cents": int(os.getenv("STREAK_B_MAX_ENTRY_CENTS", "40")),
        "entry_window_minutes": float(os.getenv("STREAK_B_ENTRY_WINDOW_MINUTES", "4")),
        "contracts": float(os.getenv("STREAK_B_CONTRACTS", "1")),
    },
    {
        "name": "C",
        "enabled": env_bool("STREAK_C_ENABLED", True),
        "streak": int(os.getenv("STREAK_C_LENGTH", "5")),
        "max_entry_cents": int(os.getenv("STREAK_C_MAX_ENTRY_CENTS", "45")),
        "entry_window_minutes": float(os.getenv("STREAK_C_ENTRY_WINDOW_MINUTES", "5")),
        "contracts": float(os.getenv("STREAK_C_CONTRACTS", "1")),
    },
]

for slot in STRATEGY_SLOTS:
    if slot["streak"] < 1:
        raise RuntimeError(f'STREAK_{slot["name"]}_LENGTH must be >= 1')
    if not 1 <= slot["max_entry_cents"] <= 99:
        raise RuntimeError(
            f'STREAK_{slot["name"]}_MAX_ENTRY_CENTS must be between 1 and 99'
        )
    if not 0 < slot["entry_window_minutes"] <= 15:
        raise RuntimeError(
            f'STREAK_{slot["name"]}_ENTRY_WINDOW_MINUTES must be >0 and <=15'
        )
    if slot["contracts"] <= 0:
        raise RuntimeError(f'STREAK_{slot["name"]}_CONTRACTS must be > 0')

# Multiple enabled slots may intentionally use the same streak length in paper mode
# so different price/window hypotheses can be A/B tested on the same real market.
# Demo/Live API mode intentionally places only the first matching slot per streak.

if not 0 <= STOP_LOSS_CENTS <= 99:
    raise RuntimeError("STOP_LOSS_CENTS must be between 0 and 99; use 0 to disable.")
if not 1 <= SELL_EARLY_CENTS <= 100:
    raise RuntimeError("SELL_EARLY_CENTS must be between 1 and 100; use 100 to disable.")

# Persistent paper-trade log. If a Railway volume is attached, Railway supplies
# RAILWAY_VOLUME_MOUNT_PATH automatically. Otherwise, fall back to ./data.
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.getenv("DATA_DIR", "./data"))
TRADE_LOG = os.path.join(DATA_DIR, "paper_trades.csv")
SESSION_LOG = os.path.join(DATA_DIR, "btc_session_results.csv")

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY", "")
_private_key = None

if MODE == "live":
    if not API_KEY_ID or not PRIVATE_KEY_TEXT.strip():
        raise RuntimeError(
            "LIVE safety lock: production KALSHI_API_KEY_ID and "
            "KALSHI_PRIVATE_KEY are required for MODE=live."
        )
    for slot in STRATEGY_SLOTS:
        if slot["enabled"] and float(slot["contracts"]) > LIVE_MAX_CONTRACTS:
            raise RuntimeError(
                f"LIVE safety lock: SLOT {slot['name']} contracts={slot['contracts']:g} "
                f"exceeds LIVE_MAX_CONTRACTS={LIVE_MAX_CONTRACTS:g}."
            )

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
    "next_ticker",
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
    """Authenticated Kalshi request against the currently selected environment."""
    method = method.upper()
    headers = auth_headers(method, path)
    url = BASE_URL + path

    is_entry_post = (
        method == "POST"
        and path == "/portfolio/events/orders"
        and isinstance(json_body, dict)
        and not json_body.get("reduce_only", False)
    )
    if is_entry_post:
        latency_mark(
            "POST_START",
            detail=(
                f"ticker={json_body.get('ticker','?')} "
                f"side={json_body.get('side','?')} "
                f"price={json_body.get('price','?')}"
            ),
        )

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    elif method == "POST":
        r = requests.post(url, headers=headers, json=json_body, timeout=15)
    elif method == "DELETE":
        r = requests.delete(url, headers=headers, timeout=15)
    else:
        raise ValueError(f"Unsupported authenticated method: {method}")

    if is_entry_post:
        latency_mark("POST_RESPONSE", detail=f"http={r.status_code}")

    print(f"AUTH {method} {path}: {r.status_code} {r.text[:500]}")
    r.raise_for_status()
    return r.json() if r.text else {}



def http_error_code(exc):
    """Best-effort extraction of Kalshi's structured API error code."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("code")
    return None


def is_market_not_found_error(exc):
    return http_error_code(exc) == "market_not_found"


def http_status_code(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_temporary_service_error(exc):
    return http_status_code(exc) in {502, 503, 504}


def demo_market_preflight(ticker):
    """
    Confirm the exact ticker is still visible on the Demo market-data endpoint
    immediately before attempting an order. This is diagnostic: Demo's public
    market catalog and matching engine can still disagree, so a successful
    preflight does not guarantee order acceptance.
    """
    if MODE != "demo":
        return None
    try:
        data = get_json(f"/markets/{ticker}")
        market = data.get("market", data)
        print(
            f"DEMO PREFLIGHT OK: {ticker} "
            f"status={market.get('status','?')} "
            f"close_time={market.get('close_time','?')}"
        )
        return market
    except requests.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        print(f"DEMO PREFLIGHT FAILED: {ticker} HTTP {status}")
        return None
    except Exception as e:
        print(f"DEMO PREFLIGHT WARNING: {ticker} {e!r}")
        return None


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


# LATENCY DIAGNOSTICS ONLY -- no trading-rule changes.
_latency_boundary_key = None
_latency_boundary_dt = None
_latency_seen = set()


def latency_reset_if_new_boundary(boundary_dt):
    global _latency_boundary_key, _latency_boundary_dt, _latency_seen
    key = boundary_dt.isoformat()
    if key != _latency_boundary_key:
        _latency_boundary_key = key
        _latency_boundary_dt = boundary_dt
        _latency_seen = set()
        latency_mark("BOUNDARY", boundary_dt=boundary_dt, force=True)


def latency_mark(stage, boundary_dt=None, detail="", force=False):
    global _latency_seen
    boundary_dt = boundary_dt or _latency_boundary_dt
    if boundary_dt is None:
        return
    token = (boundary_dt.isoformat(), stage)
    if token in _latency_seen and not force:
        return
    _latency_seen.add(token)
    now = utc_now()
    delta = (now - boundary_dt).total_seconds()
    suffix = f" | {detail}" if detail else ""
    print(
        f"LATENCY | {stage:<18} | +{delta:8.3f}s | "
        f"utc={now.isoformat(timespec='milliseconds')}{suffix}"
    )


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
    r"(?:price\s+to\s+beat|target\s+price|target)\s*[:ÃÂ·-]?\s*\$?\s*"
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
    ensure_performance_log()
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


def _short_json(obj, limit=1800):
    try:
        text = json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def get_milestone_by_id(milestone_id):
    """Fetch the authoritative milestone metadata, including its type."""
    try:
        payload = get_json(f"/milestones/{milestone_id}")
        return payload.get("milestone", payload)
    except Exception as exc:
        print(f"MILESTONE DETAIL WARNING: id={milestone_id} {exc!r}")
        return None


def fetch_live_data_for_milestone(milestone, start_btc, context="boundary"):
    """
    Diagnostic Kalshi-only live-data reader.

    Try the current documented endpoint first. If it returns 404, fetch and
    print the milestone metadata and also try Kalshi's documented legacy
    type-specific endpoint. Finally try the documented batch endpoint.
    No external BTC source is ever used.
    """
    milestone_id = (milestone or {}).get("id")
    if not milestone_id:
        return None, "", "", ""

    milestone_type = (milestone or {}).get("type") or ""
    if DEBUG_LIVE_DATA:
        print(
            f"LIVE DATA PROBE: context={context} id={milestone_id} "
            f"type={milestone_type or '?'} title={(milestone or {}).get('title','?')}"
        )

    # 1) Preferred/current documented endpoint.
    try:
        payload = get_json(f"/live_data/milestone/{milestone_id}")
        live_data = payload.get("live_data", payload)
        if DEBUG_LIVE_DATA:
            print(f"LIVE DATA CURRENT OK: id={milestone_id} payload={_short_json(live_data)}")
        price, path = extract_btc_price_from_live_data(live_data, start_btc)
        if price is not None:
            return price, "kalshi_live_data", path, milestone_id
        print(f"LIVE DATA CURRENT NO BTC CANDIDATE: id={milestone_id} payload={_short_json(live_data)}")
    except requests.HTTPError as exc:
        status = http_status_code(exc)
        body = getattr(getattr(exc, "response", None), "text", "")
        print(
            f"LIVE DATA CURRENT HTTP ERROR: id={milestone_id} status={status} "
            f"body={body[:800]!r}"
        )
    except Exception as exc:
        print(f"LIVE DATA CURRENT ERROR: id={milestone_id} {exc!r}")

    # Refresh metadata so we can see the exact category/type/source ids Kalshi
    # associates with the milestone that failed above.
    detail = get_milestone_by_id(milestone_id)
    if detail:
        milestone = detail
        milestone_type = detail.get("type") or milestone_type
        print(f"MILESTONE DETAIL: {_short_json(detail)}")

    # 2) Legacy documented endpoint that includes the milestone type.
    if milestone_type:
        try:
            payload = get_json(f"/live_data/{milestone_type}/milestone/{milestone_id}")
            live_data = payload.get("live_data", payload)
            if DEBUG_LIVE_DATA:
                print(
                    f"LIVE DATA LEGACY OK: id={milestone_id} type={milestone_type} "
                    f"payload={_short_json(live_data)}"
                )
            price, path = extract_btc_price_from_live_data(live_data, start_btc)
            if price is not None:
                return price, "kalshi_live_data_legacy_typed", path, milestone_id
            print(
                f"LIVE DATA LEGACY NO BTC CANDIDATE: id={milestone_id} "
                f"type={milestone_type} payload={_short_json(live_data)}"
            )
        except requests.HTTPError as exc:
            status = http_status_code(exc)
            body = getattr(getattr(exc, "response", None), "text", "")
            print(
                f"LIVE DATA LEGACY HTTP ERROR: id={milestone_id} type={milestone_type} "
                f"status={status} body={body[:800]!r}"
            )
        except Exception as exc:
            print(
                f"LIVE DATA LEGACY ERROR: id={milestone_id} "
                f"type={milestone_type} {exc!r}"
            )

    # 3) Documented batch endpoint. Diagnostic V3 deliberately logs the raw
    # response shape before attempting to parse it. Kalshi may return a list,
    # a dict keyed by milestone id, or a nested object depending on endpoint
    # behavior/version. Do not assume iterability here.
    try:
        data = get_json("/live_data/batch", {"milestone_ids": [milestone_id]})
        print(
            f"LIVE DATA BATCH RAW: id={milestone_id} "
            f"python_type={type(data).__name__} payload={_short_json(data, limit=4000)}"
        )

        # Collect plausible live-data objects without assuming one response shape.
        candidates = []
        if isinstance(data, list):
            candidates.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            # Common wrappers first.
            for key in ("live_datas", "live_data", "data", "results", "milestones"):
                value = data.get(key)
                if isinstance(value, list):
                    candidates.extend(x for x in value if isinstance(x, dict))
                elif isinstance(value, dict):
                    # Could itself be one live-data object or a mapping by id.
                    candidates.append(value)
                    candidates.extend(x for x in value.values() if isinstance(x, dict))

            # Also inspect top-level values in case the payload is keyed by milestone id.
            candidates.extend(x for x in data.values() if isinstance(x, dict))

            # Finally include the whole dict itself; the extractor is conservative.
            candidates.append(data)

        # De-duplicate dict objects by serialized form for cleaner diagnostics.
        unique = []
        seen = set()
        for candidate in candidates:
            try:
                marker = json.dumps(candidate, sort_keys=True, default=str)
            except Exception:
                marker = repr(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(candidate)

        print(
            f"LIVE DATA BATCH PARSE: id={milestone_id} "
            f"candidate_dicts={len(unique)}"
        )

        for idx, live_data in enumerate(unique):
            print(
                f"LIVE DATA BATCH CANDIDATE[{idx}]: id={milestone_id} "
                f"payload={_short_json(live_data, limit=2500)}"
            )
            price, path = extract_btc_price_from_live_data(live_data, start_btc)
            if price is not None:
                print(
                    f"LIVE DATA BATCH BTC FOUND: id={milestone_id} "
                    f"candidate={idx} path={path} value={format_decimal(price)}"
                )
                return price, "kalshi_live_data_batch", path, milestone_id

        print(f"LIVE DATA BATCH NO BTC CANDIDATE: id={milestone_id}")
    except requests.HTTPError as exc:
        status = http_status_code(exc)
        body = getattr(getattr(exc, "response", None), "text", "")
        print(
            f"LIVE DATA BATCH HTTP ERROR: id={milestone_id} status={status} "
            f"body={body[:1200]!r}"
        )
    except Exception as exc:
        print(f"LIVE DATA BATCH ERROR: id={milestone_id} {type(exc).__name__}: {exc!r}")

    return None, "", "", milestone_id


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
        price, source, path, milestone_id = fetch_live_data_for_milestone(
            milestone, start_btc, context="post_close_lookup"
        )
        if price is not None:
            return price, source, path, milestone_id

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
        "milestones": milestones,
        "milestone_ids": milestone_ids,
        "prepared_at": utc_now(),
    }

    print(
        f"PREPARED CLOSE: {ticker} "
        f"start={format_decimal(start_btc) or '?'} "
        f"close_time={market.get('close_time')} "
        f"milestones={len(milestone_ids)}"
    )
    if DEBUG_LIVE_DATA:
        for m in milestones:
            print(
                "PREPARED MILESTONE: "
                f"id={m.get('id','?')} category={m.get('category','?')} "
                f"type={m.get('type','?')} source_id={m.get('source_id','?')} "
                f"title={m.get('title','?')}"
            )
    return prepared


def fetch_prepared_kalshi_close(prepared):
    """
    Read Kalshi live_data at the boundary using milestone metadata cached before
    close. The probe logs enough information to diagnose 404s and tries only
    documented Kalshi live-data endpoints; no external BTC source is used.
    """
    start_btc = prepared.get("start_btc")
    milestones = prepared.get("milestones") or []

    # Backward compatibility if an older prepared object contains IDs only.
    if not milestones:
        milestones = [{"id": mid} for mid in (prepared.get("milestone_ids") or [])]

    last_mid = ""
    for milestone in milestones:
        last_mid = milestone.get("id") or last_mid
        price, source, path, milestone_id = fetch_live_data_for_milestone(
            milestone, start_btc, context="exact_boundary"
        )
        if price is not None:
            print(
                f"LIVE DATA EXTRACTED: milestone={milestone_id} "
                f"path={path} value={format_decimal(price)} source={source}"
            )
            return price, source, path, milestone_id

    return None, "", "", last_mid


def diagnostic_probe_prepared_session(prepared):
    print("*** ONE-MINUTE KALSHI LIVE DATA TEST ***", flush=True)
    """
    Probe Kalshi live-data for the currently prepared BTC session during the
    session, without changing any trading state. This is diagnostic only.

    It uses the same Kalshi-only endpoint chain as the exact-boundary capture,
    so repeated 404s during the session tell us the failure is not boundary-only.
    """
    if not prepared:
        return None

    start_btc = prepared.get("start_btc")
    milestones = prepared.get("milestones") or []
    if not milestones:
        milestones = [{"id": mid} for mid in (prepared.get("milestone_ids") or [])]

    now = utc_now()
    print(
        f"LIVE TEST {now.isoformat(timespec='seconds')} "
        f"ticker={prepared.get('ticker','?')} "
        f"start={format_decimal(start_btc) or '?'} "
        f"milestones={len(milestones)}"
    )

    found = None
    for milestone in milestones:
        price, source, path, milestone_id = fetch_live_data_for_milestone(
            milestone, start_btc, context="one_minute_diagnostic"
        )
        if price is not None:
            found = {
                "price": price,
                "source": source,
                "path": path,
                "milestone_id": milestone_id,
            }
            print(
                f"LIVE BTC FOUND: id={milestone_id} "
                f"value={format_decimal(price)} source={source} path={path}"
            )
            break

    if found is None:
        print("LIVE TEST RESULT: no usable Kalshi BTC live value found", flush=True)
    print("*** ONE-MINUTE KALSHI LIVE DATA TEST COMPLETE ***", flush=True)
    return found


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



def get_recent_kxbtc15m_markets_for_strikes():
    """
    Kalshi-only market list around the current time. No settlement/result fields
    are required for the trading decision.
    """
    now_ts = int(utc_now().timestamp())
    data = get_json(
        "/markets",
        {
            "series_ticker": SERIES,
            "min_close_ts": now_ts - 4 * 3600,
            "max_close_ts": now_ts + 3600,
            "limit": 200,
        },
    )
    markets = data.get("markets", [])
    markets.sort(key=lambda m: m.get("open_time") or "")
    return markets



def btc_session_start(market):
    """
    Actual KXBTC15M BTC measurement interval starts 15 minutes before close_time.
    Kalshi's API open_time can be earlier because a market may be listed before
    its 15-minute BTC interval begins.
    """
    close_dt = parse_time(market.get("close_time"))
    if close_dt is None:
        return None
    return close_dt - dt.timedelta(minutes=15)


def market_strike_record(market):
    strike, source = extract_kalshi_start_btc(market)
    if strike is None:
        return None
    return {
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "strike": strike,
        "source": source,
    }


def derive_successive_strike_results():
    """
    Derive each completed 15-minute session outcome from adjacent Kalshi strikes:

        next_strike > prior_strike -> prior session YES
        next_strike < prior_strike -> prior session NO
        equal                    -> TIE

    No official Kalshi result, Past page, settlement average, or external BTC
    source is used.
    """
    markets = get_recent_kxbtc15m_markets_for_strikes()
    strikes = []
    now = utc_now()

    for market in markets:
        rec = market_strike_record(market)
        if rec is None or not rec.get("ticker"):
            continue

        # A Kalshi strike is usable for this method only once the actual
        # 15-minute BTC session has begun: close_time - 15 minutes.
        session_start = btc_session_start(market)
        if session_start is None or session_start > now:
            continue

        strikes.append(rec)

    # Deduplicate by ticker and keep chronological order.
    by_ticker = {r["ticker"]: r for r in strikes}
    strikes = list(by_ticker.values())
    strikes.sort(key=lambda r: r.get("close_time") or "")

    derived = []
    for prior, nxt in zip(strikes, strikes[1:]):
        prior_strike = prior["strike"]
        next_strike = nxt["strike"]
        distance = next_strike - prior_strike

        if distance > 0:
            result = "yes"
        elif distance < 0:
            result = "no"
        else:
            result = "tie"

        derived.append({
            "ticker": prior["ticker"],
            "next_ticker": nxt["ticker"],
            "event_ticker": prior.get("event_ticker") or "",
            "open_time": prior.get("open_time") or "",
            "close_time": prior.get("close_time") or "",
            "kalshi_start_btc": prior_strike,
            "kalshi_close_btc": next_strike,
            "close_distance": distance,
            "result": result,
            "start_source": prior.get("source") or "",
            "next_source": nxt.get("source") or "",
        })

    return derived


def persist_successive_strike_results(derived):
    """
    Persist newly inferred session results to btc_session_results.csv.
    Existing rows are replaced for matching tickers so old FLAG rows from the
    abandoned live-data method do not poison the streak.
    """
    rows = read_session_log()
    existing = {r.get("ticker"): r for r in rows if r.get("ticker")}

    for d in derived:
        ticker = d["ticker"]
        row = {
            "captured_time_utc": utc_now().isoformat(timespec="seconds"),
            "ticker": ticker,
            "next_ticker": d.get("next_ticker") or "",
            "event_ticker": d.get("event_ticker") or "",
            "open_time_utc": d.get("open_time") or "",
            "close_time_utc": d.get("close_time") or "",
            "kalshi_start_btc": format_decimal(d.get("kalshi_start_btc")),
            "kalshi_close_btc": format_decimal(d.get("kalshi_close_btc")),
            "close_distance": format_decimal(d.get("close_distance")),
            "immediate_result": d.get("result") or "flag",
            "close_capture_lag_seconds": "",
            "close_price_source": "next_kalshi_session_strike",
            "live_price_path": "",
            "milestone_id": "",
            "note": (
                f"start_source={d.get('start_source','')}; "
                f"next_source={d.get('next_source','')}; "
                "outcome=successive_strike"
            ),
        }
        existing[ticker] = row

    merged = list(existing.values())
    merged.sort(key=lambda r: r.get("close_time_utc") or "")
    write_session_log(merged)


def successive_strike_markets():
    """
    Return market-like records consumed by existing streak and P&L helpers.
    """
    derived = derive_successive_strike_results()
    persist_successive_strike_results(derived)

    markets = []
    for d in derived:
        markets.append({
            "ticker": d["ticker"],
            "close_time": d.get("close_time") or "",
            "result": d.get("result") or "flag",
            "kalshi_start_btc": format_decimal(d.get("kalshi_start_btc")),
            "kalshi_close_btc": format_decimal(d.get("kalshi_close_btc")),
            "close_distance": format_decimal(d.get("close_distance")),
            "next_ticker": d.get("next_ticker") or "",
        })
    markets.sort(key=lambda m: m.get("close_time") or "")
    return markets


def print_last_five_successive(markets):
    recent = markets[-5:]
    print("LAST 5 SUCCESSIVE-STRIKE RESULTS:")
    if not recent:
        print("  none")
        return

    for m in recent:
        print(
            f"  {m.get('close_time','?')} | {m.get('ticker','?')} -> "
            f"{m.get('next_ticker','?')} | "
            f"start={m.get('kalshi_start_btc','')} "
            f"next_strike={m.get('kalshi_close_btc','')} "
            f"distance={m.get('close_distance','')} | "
            f"{(m.get('result') or '?').upper()}"
        )

    print(
        "  SEQUENCE: "
        + " | ".join((m.get("result") or "?").upper() for m in recent)
    )

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




PERFORMANCE_FIELDS = [
    "fill_time_utc",
    "ticker",
    "slot",
    "trigger_streak",
    "side",
    "entry_cents",
    "contracts",
    "status",
    "exit_reason",
    "exit_cents",
    "settlement_result",
    "pnl_cents",
    "outcome",
    "closed_time_utc",
]


def ensure_performance_log():
    p = Path(PERFORMANCE_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists() or p.stat().st_size == 0:
        with p.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=PERFORMANCE_FIELDS).writeheader()


def read_performance_log():
    ensure_performance_log()
    with Path(PERFORMANCE_LOG).open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_performance_log(rows):
    ensure_performance_log()
    with Path(PERFORMANCE_LOG).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PERFORMANCE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def performance_start_trade(
    ticker, slot, trigger_streak, side, entry_cents, contracts
):
    rows = read_performance_log()
    if any(r.get("ticker") == ticker and r.get("slot") == (slot or "") for r in rows):
        return

    rows.append({
        "fill_time_utc": utc_now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "slot": slot or "",
        "trigger_streak": str(trigger_streak),
        "side": side,
        "entry_cents": f"{float(entry_cents):.4f}",
        "contracts": f"{float(contracts):.4f}",
        "status": "OPEN",
        "exit_reason": "",
        "exit_cents": "",
        "settlement_result": "",
        "pnl_cents": "",
        "outcome": "",
        "closed_time_utc": "",
    })
    write_performance_log(rows)
    print(
        f"PERFORMANCE TRACKED: {ticker} slot={slot or '-'} "
        f"{side.upper()} entry={float(entry_cents):.1f}c"
    )


def performance_close_exit(ticker, exit_reason, exit_cents):
    rows = read_performance_log()
    changed = False

    for r in rows:
        if r.get("ticker") != ticker or r.get("status") != "OPEN":
            continue

        entry = float(r.get("entry_cents") or 0)
        qty = float(r.get("contracts") or 0)
        exit_price = float(exit_cents)
        pnl = (exit_price - entry) * qty

        r["status"] = "CLOSED"
        r["exit_reason"] = exit_reason
        r["exit_cents"] = f"{exit_price:.4f}"
        r["pnl_cents"] = f"{pnl:.4f}"
        r["outcome"] = (
            "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
        )
        r["closed_time_utc"] = utc_now().isoformat(timespec="seconds")
        changed = True
        break

    if changed:
        write_performance_log(rows)
        print_current_strategy_summary(rows)


def performance_settle_held_trades(immediate_markets):
    """
    Close trades still held at market end using our Kalshi successive-strike
    result, not Kalshi's official result.
    """
    result_by_ticker = {
        m.get("ticker"): (m.get("result") or "").lower()
        for m in immediate_markets
        if m.get("ticker") and (m.get("result") or "").lower() in {"yes", "no"}
    }

    rows = read_performance_log()
    changed = False

    for r in rows:
        if r.get("status") != "OPEN":
            continue

        settlement = result_by_ticker.get(r.get("ticker"))
        if settlement not in {"yes", "no"}:
            continue

        side = (r.get("side") or "").lower()
        entry = float(r.get("entry_cents") or 0)
        qty = float(r.get("contracts") or 0)
        won = settlement == side
        pnl = ((100.0 - entry) if won else -entry) * qty

        r["status"] = "CLOSED"
        r["exit_reason"] = "held_to_close"
        r["exit_cents"] = "100.0000" if won else "0.0000"
        r["settlement_result"] = settlement
        r["pnl_cents"] = f"{pnl:.4f}"
        r["outcome"] = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
        r["closed_time_utc"] = utc_now().isoformat(timespec="seconds")
        changed = True

        print(
            f"PERFORMANCE CLOSED: {r['ticker']} held_to_close "
            f"{r['outcome']} pnl={pnl:.1f}c"
        )

    if changed:
        write_performance_log(rows)
        print_current_strategy_summary(rows)


def _summary_stats(rows):
    closed = [r for r in rows if r.get("status") == "CLOSED"]
    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    wins = sum(1 for r in closed if r.get("outcome") == "WIN")
    losses = sum(1 for r in closed if r.get("outcome") == "LOSS")
    breakeven = sum(1 for r in closed if r.get("outcome") == "BREAKEVEN")
    pnl = sum(float(r.get("pnl_cents") or 0) for r in closed)
    decided = wins + losses
    win_rate = (100.0 * wins / decided) if decided else None
    avg_pnl = (pnl / len(closed)) if closed else None

    return {
        "closed": len(closed),
        "open": len(open_rows),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": win_rate,
        "pnl": pnl,
        "avg_pnl": avg_pnl,
    }


def print_current_strategy_summary(rows=None):
    if rows is None:
        rows = read_performance_log()

    s = _summary_stats(rows)
    wr = f"{s['win_rate']:.1f}%" if s["win_rate"] is not None else "--"
    avg = f"{s['avg_pnl']:.1f}c" if s["avg_pnl"] is not None else "--"

    print(
        "CURRENT STRATEGY SUMMARY: "
        f"closed={s['closed']} open={s['open']} "
        f"wins={s['wins']} losses={s['losses']} "
        f"win_rate={wr} net_pnl={s['pnl']:.1f}c "
        f"avg_pnl_per_trade={avg}"
    )

    for slot in ("A", "B", "C"):
        slot_rows = [r for r in rows if r.get("slot") == slot]
        if not slot_rows:
            continue
        ss = _summary_stats(slot_rows)
        swr = f"{ss['win_rate']:.1f}%" if ss["win_rate"] is not None else "--"
        savg = f"{ss['avg_pnl']:.1f}c" if ss["avg_pnl"] is not None else "--"
        print(
            f"  SLOT {slot}: closed={ss['closed']} open={ss['open']} "
            f"W={ss['wins']} L={ss['losses']} win_rate={swr} "
            f"net_pnl={ss['pnl']:.1f}c avg={savg}"
        )


def strategies_for_streak(streak):
    """Return every enabled exact-match slot for this streak.

    Paper mode may intentionally return multiple slots with the same streak so
    they can be tested independently on the same production market.
    """
    return [
        slot for slot in STRATEGY_SLOTS
        if slot["enabled"] and streak == slot["streak"]
    ]


def strategy_for_streak(streak):
    """Single-slot selector used by Demo/Live API order placement."""
    matches = strategies_for_streak(streak)
    return matches[0] if matches else None


def opposite_side(result):
    return "no" if result == "yes" else "yes"


def market_age_minutes(market):
    """
    Minutes since the actual BTC 15-minute session began.
    Future sessions return a negative age so they cannot qualify for entry.
    """
    session_start = btc_session_start(market)
    if session_start is None:
        return None
    return (utc_now() - session_start).total_seconds() / 60.0

def qualifying_current_market(open_markets, entry_window_minutes):
    candidates = []
    for m in open_markets:
        age = market_age_minutes(m)
        if age is not None and 0 <= age <= entry_window_minutes:
            candidates.append((age, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def current_15m_session_start(now=None):
    """UTC start of the 15-minute session containing *now*."""
    now = now or utc_now()
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)


def market_for_session_start(open_markets, session_start):
    """Return the KXBTC15M market whose true session start matches exactly."""
    for m in open_markets:
        start = btc_session_start(m)
        if start is not None and start == session_start:
            return m
    return None


def preidentify_market_for_session_start(session_start):
    """Try to cache the exact next-session ticker before its session begins."""
    queries = [
        {"series_ticker": SERIES, "status": "unopened", "limit": 100},
        {"series_ticker": SERIES, "limit": 100},
    ]
    for params in queries:
        try:
            data = get_json("/markets", params)
        except Exception as exc:
            print(f"FAST PREIDENTIFY WARNING: {exc!r}")
            continue

        for market in data.get("markets", []):
            start = btc_session_start(market)
            if start is not None and start == session_start and market.get("ticker"):
                print(
                    f"FAST PREIDENTIFIED: target_start={session_start.isoformat()} "
                    f"ticker={market.get('ticker')} status={market.get('status','?')}"
                )
                return market
    return None


FAST_LIVE_SOURCES = {
    "kalshi_live_data",
    "kalshi_live_data_cached_milestone",
    "kalshi_live_data_legacy_typed",
    "kalshi_live_data_batch",
}


def latest_fast_boundary_row(target_start):
    """Return ONLY a true Kalshi live-data capture for target_start.

    Successive-strike history rows share the same session log, but they are
    delayed observations and must never masquerade as an instantaneous fast
    boundary result.
    """
    for row in reversed(read_session_log()):
        close_dt = parse_time(row.get("close_time_utc"))
        if close_dt != target_start:
            continue

        source = (row.get("close_price_source") or "").strip()
        if source in FAST_LIVE_SOURCES:
            return row

        if source:
            print(
                f"FAST SOURCE GUARD: rejecting {row.get('ticker','?')} "
                f"source={source} for target_start={target_start.isoformat()}",
                flush=True,
            )
    return None


def fast_streak_view(immediate_markets, target_start):
    """Append the instantaneous boundary outcome if the formal strike is not ready."""
    fast_row = latest_fast_boundary_row(target_start)
    if not fast_row:
        return immediate_markets, None

    result = (fast_row.get("immediate_result") or "").lower()
    if result not in {"yes", "no", "tie"}:
        return immediate_markets, fast_row

    if latest_result_confirms_boundary(immediate_markets, target_start):
        return immediate_markets, fast_row

    synthetic = {
        "ticker": fast_row.get("ticker") or "",
        "close_time": target_start.isoformat(),
        "result": result,
        "source": "instant_kalshi_boundary_price",
        "kalshi_start_btc": fast_row.get("kalshi_start_btc") or "",
        "kalshi_close_btc": fast_row.get("kalshi_close_btc") or "",
    }
    return list(immediate_markets) + [synthetic], fast_row


def direct_submit_preidentified(pending):
    """Try the cached ticker directly, without waiting for open-market visibility."""
    ticker = pending.get("ticker_hint")
    if not ticker:
        return "no_hint", None

    deadline = min(
        pending["expiration_ts"],
        utc_now().timestamp() + FAST_DIRECT_RETRY_MAX_SECONDS,
    )
    attempt = 0
    while utc_now().timestamp() < deadline:
        attempt += 1
        age = (utc_now() - pending["target_start"]).total_seconds() / 60.0
        latency_mark(
            "DIRECT_POST_TRY",
            boundary_dt=pending["target_start"],
            detail=f"ticker={ticker} attempt={attempt} age={age:.3f}m",
            force=True,
        )
        status, order = submit_resting_order_with_retry(
            ticker,
            pending["side"],
            pending["entry_cents"],
            pending["expiration_ts"],
            pending["contracts"],
        )
        if status == "accepted" and order:
            return "accepted", order
        if status not in {"market_not_found", "failed"}:
            return status, order

        remaining = deadline - utc_now().timestamp()
        if remaining <= 0:
            break
        time.sleep(min(FAST_DIRECT_RETRY_SECONDS, remaining))

    return "not_ready", None


def latest_result_confirms_boundary(immediate_markets, target_start):
    """
    True only after the successive-strike dataset contains the result for the
    session that JUST ended at target_start.

    Example: for the 3:00-3:15 entry market, do not trade until the latest
    derived result has close_time == 3:00. That proves the 3:00 Kalshi strike
    is available and the 2:45-3:00 result has been incorporated.
    """
    if not immediate_markets:
        return False
    latest_close = parse_time(immediate_markets[-1].get("close_time"))
    if latest_close is None:
        return False
    return latest_close == target_start


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


def log_signal(ticker, trigger_result, streak, side, entry_cents, age_minutes, contracts=None):
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
        "contracts": f"{float(CONTRACTS if contracts is None else contracts):.4f}",
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



def place_resting_limit_order(ticker, side, limit_cents, expiration_ts, contracts):
    """
    Place one resting limit order that expires automatically at the end of
    the configured entry window.
    """
    if MODE not in {"demo", "live"}:
        raise RuntimeError("Real API orders require MODE=demo or MODE=live")
    if not PLACE_ORDERS:
        raise RuntimeError("Order placement is disabled")
    if MODE == "live" and float(contracts) > LIVE_MAX_CONTRACTS:
        raise RuntimeError(
            f"LIVE safety lock: requested {float(contracts):g} contracts exceeds "
            f"LIVE_MAX_CONTRACTS={LIVE_MAX_CONTRACTS:g}"
        )

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
        "count": f"{contracts:.2f}",
        "price": f"{price_dollars:.4f}",
        "time_in_force": "good_till_canceled",
        "expiration_time": int(expiration_ts),
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }

    resp = authenticated_request("POST", "/portfolio/events/orders", json_body=payload)
    order = resp.get("order", resp)
    latency_mark(
        "ORDER_ACCEPTED",
        detail=f"ticker={ticker} order_id={order.get('order_id','?')}",
    )
    print(f"{MODE.upper()} RESTING LIMIT ORDER ACCEPTED: {order}")
    return order


def get_order(order_id):
    resp = authenticated_request("GET", f"/portfolio/orders/{order_id}")
    return resp.get("order", resp)


def cancel_order(order_id):
    try:
        resp = authenticated_request("DELETE", f"/portfolio/events/orders/{order_id}")
        print(f"CANCEL RESULT: {resp}")
        return resp
    except Exception as e:
        print(f"CANCEL WARNING: {e!r}")
        return None


def market_entry_expiration_ts(market, entry_window_minutes):
    session_start = btc_session_start(market)
    if session_start is None:
        return 0.0
    return (
        session_start + dt.timedelta(minutes=entry_window_minutes)
    ).timestamp()

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


def submit_resting_order_with_retry(ticker, side, limit_cents, expiration_ts, contracts):
    """
    Submit a resting API order.

    Returns:
      ("accepted", order_dict)
      ("market_not_found", None)
      ("failed", None)

    MARKET_NOT_FOUND is terminal for that ticker. Retrying the same unknown
    symbol seconds later only creates repeated 404s, so stop immediately.
    """
    last_error = None

    # Demo-only diagnostic preflight; production/live skips this.
    demo_market_preflight(ticker)

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
            order = place_resting_limit_order(
                ticker, side, limit_cents, expiration_ts, contracts
            )
            print(
                f"SUBMIT ACCEPTED on attempt {attempt}: "
                f"order_id={order.get('order_id')}"
            )
            return "accepted", order

        except requests.HTTPError as e:
            last_error = e

            if is_market_not_found_error(e):
                env_name = "DEMO" if MODE == "demo" else "LIVE"
                print(
                    f"{env_name} MARKET UNAVAILABLE: {ticker} returned market_not_found. "
                    f"Skipping this ticker; no further retries."
                )
                return "market_not_found", None

            print(
                f"SUBMIT REJECTED attempt {attempt}/{ORDER_SUBMIT_RETRIES}: "
                f"{e!r}"
            )

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
    return "failed", None


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




EXIT_TRACKERS = {}
EXIT_LOG_LOCK = threading.Lock()

POSITION_PRICE_FIELDS = [
    "timestamp_utc",
    "ticker",
    "slot",
    "side",
    "entry_cents",
    "bid_cents",
    "seconds_since_fill",
    "stop_loss_setting_cents",
    "sell_early_setting_cents",
]

POSITION_SUMMARY_FIELDS = [
    "ticker",
    "slot",
    "side",
    "fill_time_utc",
    "entry_cents",
    "contracts",
    "min_bid_cents",
    "max_bid_cents",
    "last_bid_cents",
    "actual_exit_reason",
    "actual_exit_cents",
    "actual_exit_time_utc",
    "market_close_time_utc",
]


def append_exit_csv(path, fields, row):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with EXIT_LOG_LOCK:
        new_file = not p.exists() or p.stat().st_size == 0
        with p.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)


def record_actual_exit(ticker, reason, exit_cents):
    with EXIT_LOG_LOCK:
        tracker = EXIT_TRACKERS.get(ticker)
        if tracker is not None:
            tracker["actual_exit_reason"] = reason
            tracker["actual_exit_cents"] = exit_cents
            tracker["actual_exit_time_utc"] = utc_now().isoformat(timespec="seconds")


def exit_logging_worker(ticker):
    with EXIT_LOG_LOCK:
        tracker = EXIT_TRACKERS.get(ticker)
        if tracker is None:
            return
        tracker = dict(tracker)

    fill_dt = parse_time(tracker["fill_time_utc"])
    min_bid = None
    max_bid = None
    last_bid = None

    while True:
        try:
            market = get_market_by_ticker(ticker)
            now = utc_now()

            if market:
                close_dt = parse_time(market.get("close_time"))
                bid = side_bid_cents(market, tracker["side"])

                if bid is not None:
                    min_bid = bid if min_bid is None else min(min_bid, bid)
                    max_bid = bid if max_bid is None else max(max_bid, bid)
                    last_bid = bid

                    elapsed = (
                        (now - fill_dt).total_seconds()
                        if fill_dt is not None else ""
                    )

                    append_exit_csv(
                        POSITION_PRICE_LOG,
                        POSITION_PRICE_FIELDS,
                        {
                            "timestamp_utc": now.isoformat(timespec="seconds"),
                            "ticker": ticker,
                            "slot": tracker.get("slot", ""),
                            "side": tracker["side"],
                            "entry_cents": tracker["entry_cents"],
                            "bid_cents": bid,
                            "seconds_since_fill": elapsed,
                            "stop_loss_setting_cents": STOP_LOSS_CENTS,
                            "sell_early_setting_cents": SELL_EARLY_CENTS,
                        },
                    )

                if close_dt is not None and now >= close_dt:
                    with EXIT_LOG_LOCK:
                        live = EXIT_TRACKERS.get(ticker, {})
                        actual_reason = live.get("actual_exit_reason", "")
                        actual_cents = live.get("actual_exit_cents", "")
                        actual_time = live.get("actual_exit_time_utc", "")

                    append_exit_csv(
                        POSITION_SUMMARY_LOG,
                        POSITION_SUMMARY_FIELDS,
                        {
                            "ticker": ticker,
                            "slot": tracker.get("slot", ""),
                            "side": tracker["side"],
                            "fill_time_utc": tracker["fill_time_utc"],
                            "entry_cents": tracker["entry_cents"],
                            "contracts": tracker["contracts"],
                            "min_bid_cents": "" if min_bid is None else min_bid,
                            "max_bid_cents": "" if max_bid is None else max_bid,
                            "last_bid_cents": "" if last_bid is None else last_bid,
                            "actual_exit_reason": actual_reason,
                            "actual_exit_cents": actual_cents,
                            "actual_exit_time_utc": actual_time,
                            "market_close_time_utc": market.get("close_time", ""),
                        },
                    )

                    print(
                        f"EXIT LOG COMPLETE: {ticker} "
                        f"min_bid={min_bid}c max_bid={max_bid}c"
                    )
                    break

        except Exception as exc:
            print(f"EXIT LOG WARNING {ticker}: {exc!r}")

        time.sleep(max(0.5, EXIT_LOG_POLL_SECONDS))

    with EXIT_LOG_LOCK:
        EXIT_TRACKERS.pop(ticker, None)


def start_exit_logging(ticker, slot, side, entry_cents, contracts):
    if not EXIT_LOGGING_ENABLED:
        return

    tracker = {
        "ticker": ticker,
        "slot": slot or "",
        "side": side,
        "entry_cents": float(entry_cents),
        "contracts": float(contracts),
        "fill_time_utc": utc_now().isoformat(timespec="seconds"),
        "actual_exit_reason": "",
        "actual_exit_cents": "",
        "actual_exit_time_utc": "",
    }

    with EXIT_LOG_LOCK:
        if ticker in EXIT_TRACKERS:
            return
        EXIT_TRACKERS[ticker] = tracker

    print(
        f"EXIT LOG STARTED: {ticker} slot={slot or '-'} "
        f"{side.upper()} entry={float(entry_cents):.1f}c"
    )

    thread = threading.Thread(
        target=exit_logging_worker,
        args=(ticker,),
        daemon=True,
        name=f"exit-log-{ticker}",
    )
    thread.start()



def time_exit_thresholds(market):
    """
    Return (stop_loss_cents, take_profit_cents, minutes_remaining, band_name)
    for the current 15-minute contract.
    """
    close_dt = parse_time(market.get("close_time"))
    if close_dt is None:
        return None, None, None, "unknown"

    minutes_remaining = (close_dt - utc_now()).total_seconds() / 60.0

    if minutes_remaining > 3:
        return (
            STOP_LOSS_GT3_CENTS,
            TAKE_PROFIT_GT3_CENTS,
            minutes_remaining,
            ">3m",
        )
    if minutes_remaining > 2:
        return (
            STOP_LOSS_2_TO_3_CENTS,
            TAKE_PROFIT_2_TO_3_CENTS,
            minutes_remaining,
            "2-3m",
        )
    if minutes_remaining > 1:
        return (
            STOP_LOSS_1_TO_2_CENTS,
            TAKE_PROFIT_1_TO_2_CENTS,
            minutes_remaining,
            "1-2m",
        )
    return (
        STOP_LOSS_LT1_CENTS,
        TAKE_PROFIT_LT1_CENTS,
        minutes_remaining,
        "<1m",
    )


def side_bid_cents(market, held_side):
    """
    Executable value of the position: the best displayed bid for the exact
    contract we hold (YES or NO).
    """
    field = "yes_bid_dollars" if held_side == "yes" else "no_bid_dollars"
    return dollars_to_cents(market.get(field))


def submit_exit_ioc(ticker, held_side, contracts, side_bid):
    """
    Exit an existing position using an immediate-or-cancel reduce-only order
    priced at the current executable bid for the held contract.

    V2 event orders are represented on the YES book:
      - Sell YES -> ask YES at YES bid.
      - Sell NO  -> buy YES at 1 - NO bid.
    """
    if MODE not in {"demo", "live"}:
        raise RuntimeError("API exit orders require MODE=demo or MODE=live")
    if not PLACE_ORDERS:
        raise RuntimeError("Order placement is disabled")
    if MODE == "live" and float(contracts) > LIVE_MAX_CONTRACTS:
        raise RuntimeError(
            f"LIVE safety lock: requested exit {float(contracts):g} contracts exceeds "
            f"LIVE_MAX_CONTRACTS={LIVE_MAX_CONTRACTS:g}"
        )

    if held_side == "yes":
        book_side = "ask"
        yes_book_price = side_bid / 100.0
    elif held_side == "no":
        book_side = "bid"
        yes_book_price = 1.0 - (side_bid / 100.0)
    else:
        raise ValueError(f"Unknown held side: {held_side}")

    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": book_side,
        "count": f"{float(contracts):.2f}",
        "price": f"{yes_book_price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "reduce_only": True,
        "subaccount": 0,
        "exchange_index": 0,
    }

    resp = authenticated_request(
        "POST", "/portfolio/events/orders", json_body=payload
    )
    return resp.get("order", resp)


def monitor_position_once(position):
    """
    Check one filled position against the Railway-configured stop-loss and
    early-sell thresholds. Returns:
      ("hold", position)
      ("closed", None)
      ("partial", updated_position)
      ("expired", None)
    """
    ticker = position["ticker"]
    held_side = position["side"]
    qty = float(position["contracts"])

    market = get_market_by_ticker(ticker)
    if not market:
        print(f"POSITION WARNING: could not load market {ticker}; holding.")
        return "hold", position

    # Once the 15-minute market itself is over, do not submit a late exit.
    close_dt = parse_time(market.get("close_time"))
    if close_dt is not None and utc_now() >= close_dt:
        print(f"POSITION WINDOW CLOSED: {ticker}; no early exit submitted.")
        return "expired", None

    bid = side_bid_cents(market, held_side)
    if bid is None:
        print(f"POSITION: {ticker} {held_side.upper()} no executable bid; holding.")
        return "hold", position

    entry = float(position["entry_cents"])

    if TIME_EXIT_ENABLED:
        stop_threshold, early_threshold, minutes_remaining, time_band = (
            time_exit_thresholds(market)
        )
        stop_hit = (
            stop_threshold is not None
            and stop_threshold > 0
            and bid <= stop_threshold
        )
        early_hit = (
            early_threshold is not None
            and early_threshold < 100
            and bid >= early_threshold
        )
    else:
        stop_threshold = STOP_LOSS_CENTS
        early_threshold = SELL_EARLY_CENTS
        minutes_remaining = (
            (close_dt - utc_now()).total_seconds() / 60.0
            if close_dt is not None else None
        )
        time_band = "fixed"
        stop_hit = STOP_LOSS_CENTS > 0 and bid <= STOP_LOSS_CENTS
        early_hit = SELL_EARLY_CENTS < 100 and bid >= SELL_EARLY_CENTS

    remaining_text = (
        f"{minutes_remaining:.2f}m"
        if minutes_remaining is not None else "?"
    )

    print(
        f"POSITION: {ticker} {held_side.upper()} qty={qty:g} "
        f"entry={entry:.1f}c bid={bid:.1f}c remaining={remaining_text} "
        f"band={time_band} stop={stop_threshold:g}c "
        f"take={early_threshold:g}c"
    )

    if not stop_hit and not early_hit:
        return "hold", position

    reason = "STOP LOSS" if stop_hit else "SELL EARLY"
    print(
        f"{reason} TRIGGERED: {ticker} {held_side.upper()} "
        f"bid={bid:.1f}c qty={qty:g}"
    )

    if MODE == "paper":
        # Real-market paper mode: no API order is ever sent. Treat the currently
        # displayed bid as the executable paper exit price.
        exited = qty
        exit_cents = float(bid)
        print(
            f"REAL MARKET PAPER EXIT: {ticker} {held_side.upper()} "
            f"qty={exited:g} at observed bid={exit_cents:.1f}c Ã¢ÂÂ NO ORDER SENT"
        )
    else:
        exit_order = submit_exit_ioc(ticker, held_side, qty, bid)
        exited = order_fill_count(exit_order)

        if exited <= 0:
            print(
                f"{reason} EXIT NOT FILLED: {ticker}; "
                f"will re-check on next position poll."
            )
            return "hold", position

        exit_cents = actual_fill_cents(exit_order, held_side, bid)

    remaining = max(0.0, qty - exited)
    pnl_cents = (exit_cents - entry) * exited

    print(
        f"{reason} EXIT FILLED: {ticker} {held_side.upper()} "
        f"qty={exited:g} at ~{exit_cents:.1f}c "
        f"trade_pnl={pnl_cents:.1f}c remaining={remaining:g}"
    )

    reason_key = "stop_loss" if stop_hit else "sell_early"
    record_actual_exit(ticker, reason_key, exit_cents)
    performance_close_exit(ticker, reason_key, exit_cents)

    if remaining <= 0.000001:
        return "closed", None

    updated = dict(position)
    updated["contracts"] = remaining
    return "partial", updated


def main():
    print(f"BUILD VERSION={BUILD_VERSION}", flush=True)
    print("*** THIS BUILD MUST PRINT ONE-MINUTE LIVE-DATA PROBE HEARTBEATS ***", flush=True)
    ensure_trade_log()
    ensure_session_log()

    print("KALSHI BTC 15-MINUTE STREAK BOT")
    if MODE == "paper":
        print("*** REAL KALSHI MARKET DATA / PAPER TRADING ONLY / ZERO LIVE ORDERS ***")
        print("*** MULTI-SLOT A/B TESTING ENABLED: duplicate streak lengths allowed ***")
        print(f"PRODUCTION MARKET DATA API={BASE_URL}")
    elif MODE == "demo":
        print("*** KALSHI DEMO ENVIRONMENT / MOCK MONEY ***")
    else:
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!! LIVE KALSHI PRODUCTION TRADING Ã¢ÂÂ REAL MONEY ENABLED !!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"PRODUCTION TRADE API={BASE_URL}")
        print(
            f"LIVE SAFETY: enabled={LIVE_TRADING_ENABLED} "
            f"confirmation=OK max_contracts_per_order={LIVE_MAX_CONTRACTS:g}"
        )
    print(
        f"MODE={MODE} SERIES={SERIES} "
        f"ORDER_POLL={POLL_ORDER_SECONDS}s IDLE_POLL={POLL_IDLE_SECONDS}s "
        f"RETRIES={ORDER_SUBMIT_RETRIES} RETRY_WAIT={ORDER_RETRY_SECONDS}s"
    )
    for slot in STRATEGY_SLOTS:
        state = "ON" if slot["enabled"] else "OFF"
        print(
            f"SLOT {slot['name']}={state} streak={slot['streak']} "
            f"max_entry={slot['max_entry_cents']}c "
            f"window={slot['entry_window_minutes']:g}m "
            f"contracts={slot['contracts']:g}"
        )
    print("OUTCOME METHOD=instant Kalshi boundary BTC for live decision; successive strikes retained for history/diagnostics")
    print("FAST BOUNDARY=ON; no wait for next formal strike when Kalshi live BTC capture is available")
    print("SESSION CLOCK=close_time - 15 minutes")
    print("FUTURE STRIKE GUARD=enabled; future sessions excluded")
    print("OFFICIAL RESULT=not used")
    print("EXTERNAL BTC SOURCES=not used")
    print(
        f"LIVE DATA DIAGNOSTIC={'ON' if LIVE_DATA_DIAGNOSTIC_ENABLED else 'OFF'} "
        f"interval={LIVE_DATA_DIAGNOSTIC_INTERVAL_SECONDS:g}s "
        f"boundary_guard={LIVE_DATA_DIAGNOSTIC_BOUNDARY_GUARD_SECONDS:g}s"
    )
    print(
        f"EXITS: STOP_LOSS_CENTS={STOP_LOSS_CENTS:g} "
        f"SELL_EARLY_CENTS={SELL_EARLY_CENTS:g} "
        f"POSITION_POLL={POLL_POSITION_SECONDS:g}s "
        "(STOP=0 disables stop; EARLY=100 disables early sell)"
    )
    print(f"TRADE LOG={TRADE_LOG}")
    print(f"SESSION LOG={SESSION_LOG}")
    print(
        f"EXIT LOGGING={'ON' if EXIT_LOGGING_ENABLED else 'OFF'} "
        f"poll={EXIT_LOG_POLL_SECONDS:g}s"
    )
    print(
        f"TIME EXITS={'ON' if TIME_EXIT_ENABLED else 'OFF'} | "
        f">3m stop/take={STOP_LOSS_GT3_CENTS:g}/{TAKE_PROFIT_GT3_CENTS:g}c | "
        f"2-3m={STOP_LOSS_2_TO_3_CENTS:g}/{TAKE_PROFIT_2_TO_3_CENTS:g}c | "
        f"1-2m={STOP_LOSS_1_TO_2_CENTS:g}/{TAKE_PROFIT_1_TO_2_CENTS:g}c | "
        f"<1m={STOP_LOSS_LT1_CENTS:g}/{TAKE_PROFIT_LT1_CENTS:g}c"
    )
    print(f"POSITION PRICE LOG={POSITION_PRICE_LOG}")
    print(f"POSITION SUMMARY LOG={POSITION_SUMMARY_LOG}")
    print(f"PERFORMANCE LOG={PERFORMANCE_LOG}")
    print(
        f"TEMP OUTAGE BACKOFF={OUTAGE_BACKOFF_START_SECONDS:g}s"
        f"->{OUTAGE_BACKOFF_MAX_SECONDS:g}s for HTTP 502/503/504"
    )

    completed_markets = logged_tickers()
    demo_unavailable_markets = set()
    outage_backoff_seconds = OUTAGE_BACKOFF_START_SECONDS
    current_order = None
    current_position = None
    # Demo/live signal latch. Once a qualifying streak is seen, keep that exact
    # signal alive through its entry window even if the next successive strike
    # arrives before Kalshi's current market becomes discoverable.
    pending_api_signal = None
    # Real-market paper mode can carry several independent simulated resting
    # orders at once, including multiple slots on the same ticker/streak.
    paper_orders = {}  # key=(ticker, slot) -> simulated order
    paper_completed = {
        (r.get("ticker"), r.get("slot"))
        for r in read_performance_log()
        if r.get("ticker") and r.get("slot")
    }
    last_sequence_ticker = None
    prepared_fast_session = None
    preidentified_next_market = None
    next_live_data_diagnostic_ts = 0.0

    print_current_strategy_summary()

    while True:
        try:
            _latency_boundary = current_15m_session_start()
            latency_reset_if_new_boundary(_latency_boundary)

            # SPEED-FIRST path: prepare before close and capture Kalshi's own
            # live BTC value exactly at the boundary.
            if FAST_BOUNDARY_ENABLED:
                if prepared_fast_session is None:
                    prepared_fast_session = prepare_current_session()
                    if prepared_fast_session is not None:
                        target = prepared_fast_session.get("close_time")
                        if target is not None:
                            preidentified_next_market = preidentify_market_for_session_start(target)

                if prepared_fast_session is not None:
                    target = prepared_fast_session.get("close_time")
                    if target is not None and (
                        preidentified_next_market is None
                        or btc_session_start(preidentified_next_market) != target
                    ):
                        preidentified_next_market = preidentify_market_for_session_start(target)

                    # Diagnostic-only one-minute live-data probe. Avoid the final
                    # few seconds before a boundary so it cannot compete with the
                    # exact-boundary capture request.
                    if LIVE_DATA_DIAGNOSTIC_ENABLED:
                        _now_ts = utc_now().timestamp()
                        _seconds_to_close = (target - utc_now()).total_seconds() if target else 9999
                        if (
                            _now_ts >= next_live_data_diagnostic_ts
                            and _seconds_to_close > LIVE_DATA_DIAGNOSTIC_BOUNDARY_GUARD_SECONDS
                        ):
                            diagnostic_probe_prepared_session(prepared_fast_session)
                            next_live_data_diagnostic_ts = (
                                utc_now().timestamp() + LIVE_DATA_DIAGNOSTIC_INTERVAL_SECONDS
                            )

                    captured, prepared_fast_session = capture_prepared_if_due(
                        prepared_fast_session
                    )
                    if captured:
                        boundary_dt = current_15m_session_start()
                        latency_reset_if_new_boundary(boundary_dt)
                        row = latest_fast_boundary_row(boundary_dt)
                        if row:
                            latency_mark(
                                "FAST_PRICE_CAPTURE",
                                boundary_dt=boundary_dt,
                                detail=(
                                    f"start={row.get('kalshi_start_btc','?')} "
                                    f"close={row.get('kalshi_close_btc','?')} "
                                    f"result={row.get('immediate_result','?')} "
                                    f"lag={row.get('close_capture_lag_seconds','?')}s"
                                ),
                            )

            # Keep formal successive strikes for prior history and diagnostics.
            immediate_markets = successive_strike_markets()
            if outage_backoff_seconds != OUTAGE_BACKOFF_START_SECONDS:
                print("KALSHI API RECOVERED: normal polling resumed.")
            outage_backoff_seconds = OUTAGE_BACKOFF_START_SECONDS
            performance_settle_held_trades(immediate_markets)

            newest_ticker = (
                immediate_markets[-1].get("ticker") if immediate_markets else None
            )
            if newest_ticker != last_sequence_ticker:
                print_last_five_successive(immediate_markets)
                last_sequence_ticker = newest_ticker

            last_result, streak = calculate_streak(immediate_markets)
            print(f"CALCULATED IMMEDIATE: last={last_result} streak={streak}")

            # --------------------------------------------------------------
            # REAL-MARKET PAPER MULTI-SLOT ENGINE
            # --------------------------------------------------------------
            if MODE == "paper":
                # 1) Update every simulated resting order independently.
                market_cache = {}
                for key, po in list(paper_orders.items()):
                    ticker_po, slot_po = key
                    if utc_now().timestamp() >= po["expiration_ts"]:
                        print(
                            f"PAPER SLOT {slot_po} EXPIRED: {ticker_po} "
                            f"{po['side'].upper()} limit={po['entry_cents']:.1f}c"
                        )
                        paper_orders.pop(key, None)
                        continue

                    try:
                        live_market = market_cache.get(ticker_po)
                        if live_market is None:
                            live_market = get_market_by_ticker(ticker_po)
                            market_cache[ticker_po] = live_market
                        ask = entry_ask_cents(live_market, po["side"])
                        age_now = market_age_minutes(live_market)
                    except Exception as e:
                        print(f"PAPER SLOT {slot_po} quote read failed for {ticker_po}: {e!r}")
                        continue

                    age_text = f"{age_now:.2f}m" if age_now is not None else "?"
                    if ask is not None and ask <= po["entry_cents"]:
                        fill_cents = float(po["entry_cents"])
                        print(
                            f"REAL MARKET PAPER FILL SLOT {slot_po}: {ticker_po} "
                            f"{po['side'].upper()} qty={po['contracts']:g} "
                            f"limit={fill_cents:.1f}c observed_ask={ask:.1f}c "
                            f"age={age_text} Ã¢ÂÂ NO ORDER SENT"
                        )
                        log_signal(
                            ticker_po, po["trigger_result"], po["trigger_streak"],
                            po["side"], fill_cents,
                            age_now if age_now is not None else po["age_at_submit"],
                            po["contracts"],
                        )
                        performance_start_trade(
                            ticker_po, slot_po, po["trigger_streak"], po["side"],
                            fill_cents, po["contracts"],
                        )
                        paper_completed.add(key)
                        paper_orders.pop(key, None)
                    else:
                        ask_text = f"{ask:.1f}c" if ask is not None else "NONE"
                        print(
                            f"PAPER SLOT {slot_po} RESTING: {ticker_po} "
                            f"limit={po['entry_cents']:.1f}c ask={ask_text} age={age_text}"
                        )

                # 2) Create new simulated orders only AFTER the new boundary strike
                # has produced the just-ended session result.
                paper_target_start = current_15m_session_start()
                paper_boundary_ready = latest_result_confirms_boundary(
                    immediate_markets, paper_target_start
                )
                if not paper_boundary_ready:
                    latest_close = (
                        immediate_markets[-1].get("close_time")
                        if immediate_markets else "none"
                    )
                    print(
                        f"BOUNDARY WAIT (PAPER): target_start={paper_target_start.isoformat()} "
                        f"latest_derived_close={latest_close}; no new entry yet."
                    )
                elif last_result in {"yes", "no"}:
                    matches = strategies_for_streak(streak)
                    if not matches:
                        print(f"NO SLOT MATCH: streak={streak}; no paper orders.")
                    else:
                        open_markets = get_open_markets()
                        for strategy in matches:
                            slot_name = strategy["name"]
                            entry_window = strategy["entry_window_minutes"]
                            market = market_for_session_start(
                                open_markets, paper_target_start
                            )
                            if market is None:
                                print(
                                    f"PAPER SLOT {slot_name}: target market for "
                                    f"{paper_target_start.isoformat()} not visible yet."
                                )
                                continue
                            age_check = market_age_minutes(market)
                            if (
                                age_check is None
                                or age_check < 0
                                or age_check > entry_window
                            ):
                                print(
                                    f"PAPER SLOT {slot_name}: target market age "
                                    f"{age_check} outside {entry_window:g}m window."
                                )
                                continue
                            ticker = market["ticker"]
                            key = (ticker, slot_name)
                            if key in paper_completed or key in paper_orders:
                                continue
                            expiration_ts = market_entry_expiration_ts(market, entry_window)
                            if utc_now().timestamp() >= expiration_ts:
                                continue
                            age = market_age_minutes(market)
                            side = opposite_side(last_result)
                            limit_cents = float(strategy["max_entry_cents"])
                            contracts = float(strategy["contracts"])
                            ask = entry_ask_cents(market, side)
                            ask_text = f"{ask:.1f}c" if ask is not None else "NONE"
                            paper_orders[key] = {
                                "ticker": ticker, "slot": slot_name, "side": side,
                                "entry_cents": limit_cents,
                                "expiration_ts": expiration_ts,
                                "trigger_result": last_result,
                                "trigger_streak": streak,
                                "age_at_submit": age,
                                "contracts": contracts,
                            }
                            print(
                                f"REAL MARKET PAPER LIMIT SLOT {slot_name}: {ticker} "
                                f"buy {side.upper()} {contracts:g} @ {limit_cents:.0f}c "
                                f"ask={ask_text} age={age:.2f}m window={entry_window:g}m "
                                f"Ã¢ÂÂ NO ORDER SENT"
                            )

                # All paper fills are tracked independently and settled from the
                # successive-strike result. Demo's single-order engine is skipped.
                time.sleep(POLL_ORDER_SECONDS if paper_orders else POLL_ACTIVE_SECONDS)
                continue

            # Manage a filled position before looking for any new entry.
            if current_position is not None:
                action, current_position = monitor_position_once(current_position)
                if action in {"closed", "expired"}:
                    sleep_idle()
                else:
                    time.sleep(POLL_POSITION_SECONDS)
                continue

            # Manage any already accepted/simulated resting order.
            if current_order is not None:
                ticker = current_order["ticker"]
                side = current_order["side"]
                entry_cents = current_order["entry_cents"]

                if MODE == "paper":
                    if utc_now().timestamp() >= current_order["expiration_ts"]:
                        print(
                            f"REAL MARKET PAPER ORDER EXPIRED WITHOUT FILL: {ticker} "
                            f"{side.upper()} limit={entry_cents:.1f}c"
                        )
                        current_order = None
                        sleep_idle()
                        continue

                    live_market = get_market_by_ticker(ticker)
                    observed_ask = entry_ask_cents(live_market, side)
                    age_now = market_age_minutes(live_market)
                    age_text = f"{age_now:.2f}m" if age_now is not None else "?"

                    if observed_ask is None:
                        print(
                            f"REAL MARKET PAPER ORDER: {ticker} {side.upper()} "
                            f"limit={entry_cents:.1f}c ask=NONE age={age_text}; waiting"
                        )
                        time.sleep(POLL_ORDER_SECONDS)
                        continue

                    if observed_ask <= entry_cents:
                        # Conservative fill convention, matching the historical
                        # backtest: credit the configured limit, not a better ask.
                        fill_cents = float(entry_cents)
                        filled = float(current_order["contracts"])
                        print(
                            f"REAL MARKET PAPER FILL: {ticker} {side.upper()} "
                            f"qty={filled:g} limit={fill_cents:.1f}c "
                            f"observed_ask={observed_ask:.1f}c age={age_text} Ã¢ÂÂ NO ORDER SENT"
                        )
                        log_signal(
                            ticker,
                            current_order["trigger_result"],
                            current_order["trigger_streak"],
                            side,
                            fill_cents,
                            age_now if age_now is not None else current_order["age_at_submit"],
                            current_order["contracts"],
                        )
                        performance_start_trade(
                            ticker,
                            current_order.get("slot", ""),
                            current_order["trigger_streak"],
                            side,
                            fill_cents,
                            filled,
                        )
                        completed_markets.add(ticker)
                        current_position = {
                            "ticker": ticker,
                            "side": side,
                            "entry_cents": fill_cents,
                            "contracts": filled,
                        }
                        start_exit_logging(
                            ticker,
                            current_order.get("slot", ""),
                            side,
                            fill_cents,
                            filled,
                        )
                        current_order = None
                        if TIME_EXIT_ENABLED or STOP_LOSS_CENTS > 0 or SELL_EARLY_CENTS < 100:
                            print(
                                f"PAPER POSITION MONITOR STARTED: {ticker} {side.upper()} "
                                f"qty={filled:g} entry={fill_cents:.1f}c"
                            )
                            time.sleep(POLL_POSITION_SECONDS)
                        else:
                            print("PAPER POSITION EXITS DISABLED; holding to market close.")
                            current_position = None
                            sleep_idle()
                        continue

                    print(
                        f"REAL MARKET PAPER ORDER RESTING: {ticker} {side.upper()} "
                        f"limit={entry_cents:.1f}c observed_ask={observed_ask:.1f}c "
                        f"age={age_text}"
                    )
                    time.sleep(POLL_ORDER_SECONDS)
                    continue

                order_id = current_order["order_id"]
                order = get_order(order_id)
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
                        current_order["contracts"],
                    )
                    performance_start_trade(
                        ticker,
                        current_order.get("slot", ""),
                        current_order["trigger_streak"],
                        side,
                        fill_cents,
                        filled,
                    )
                    completed_markets.add(ticker)
                    current_position = {
                        "ticker": ticker,
                        "side": side,
                        "entry_cents": fill_cents,
                        "contracts": filled,
                    }
                    start_exit_logging(
                        ticker,
                        current_order.get("slot", ""),
                        side,
                        fill_cents,
                        filled,
                    )
                    current_order = None
                    if TIME_EXIT_ENABLED or STOP_LOSS_CENTS > 0 or SELL_EARLY_CENTS < 100:
                        print(
                            f"POSITION MONITOR STARTED: {ticker} {side.upper()} "
                            f"qty={filled:g} entry={fill_cents:.1f}c"
                        )
                        time.sleep(POLL_POSITION_SECONDS)
                    else:
                        print("POSITION EXITS DISABLED; holding to market close.")
                        current_position = None
                        sleep_idle()
                    continue

                if status in {"canceled", "executed"} or remaining <= 0:
                    print(f"ORDER CLOSED WITHOUT FILL: {ticker}")
                    current_order = None
                    sleep_idle()
                    continue

                if utc_now().timestamp() >= current_order["expiration_ts"]:
                    print(f"ENTRY WINDOW ENDED: canceling {ticker}")
                    cancel_order(order_id)
                    current_order = None
                    sleep_idle()
                    continue

                time.sleep(POLL_ORDER_SECONDS)
                continue

            # --------------------------------------------------------------
            # DEMO/LIVE SIGNAL LOCK
            # --------------------------------------------------------------
            # A streak signal can exist exactly at a 15-minute boundary before
            # Kalshi's new market appears in the open-market listing. Latch the
            # signal and retry that SAME target session until its window ends.
            if pending_api_signal is not None:
                now_ts = utc_now().timestamp()
                if now_ts >= pending_api_signal["expiration_ts"]:
                    print(
                        f"SIGNAL LOCK EXPIRED SLOT {pending_api_signal['slot']}: "
                        f"target_start={pending_api_signal['target_start'].isoformat()} "
                        f"without finding/placing the target market."
                    )
                    pending_api_signal = None
                else:
                    # First choice: submit straight to the ticker cached before
                    # the boundary. Do not wait for the open-market list.
                    if pending_api_signal.get("ticker_hint"):
                        ticker_hint = pending_api_signal["ticker_hint"]
                        status, order = direct_submit_preidentified(pending_api_signal)
                        if status == "accepted" and order:
                            ticker = ticker_hint
                            age = (
                                utc_now() - pending_api_signal["target_start"]
                            ).total_seconds() / 60.0
                            side = pending_api_signal["side"]
                            slot_name = pending_api_signal["slot"]
                            limit_cents = pending_api_signal["entry_cents"]
                            contracts = pending_api_signal["contracts"]
                            expiration_ts = pending_api_signal["expiration_ts"]
                            trigger_result = pending_api_signal["trigger_result"]
                            trigger_streak = pending_api_signal["trigger_streak"]
                            order_id = order.get("order_id")
                            current_order = {
                                "order_id": order_id,
                                "ticker": ticker,
                                "side": side,
                                "entry_cents": limit_cents,
                                "expiration_ts": expiration_ts,
                                "trigger_result": trigger_result,
                                "trigger_streak": trigger_streak,
                                "age_at_submit": age,
                                "contracts": contracts,
                                "slot": slot_name,
                            }
                            _order_target_start = pending_api_signal["target_start"]
                            pending_api_signal = None
                            latency_mark(
                                "DIRECT_ACCEPTED",
                                boundary_dt=_order_target_start,
                                detail=f"ticker={ticker} order_id={order_id}",
                            )
                            latency_mark(
                                "ORDER_RESTING",
                                boundary_dt=_order_target_start,
                                detail=f"ticker={ticker} order_id={order_id}",
                            )
                            print(
                                f"FAST DIRECT ORDER RESTING SLOT {slot_name}: "
                                f"{ticker} {side.upper()} @ {limit_cents}c"
                            )
                            time.sleep(POLL_ORDER_SECONDS)
                            continue
                        if status == "not_ready":
                            print(
                                f"FAST DIRECT TICKER NOT READY: {ticker_hint}; "
                                "falling back to normal market discovery."
                            )

                    open_markets = get_open_markets()
                    market = market_for_session_start(
                        open_markets, pending_api_signal["target_start"]
                    )
                    if market is None:
                        remaining = pending_api_signal["expiration_ts"] - now_ts
                        print(
                            f"SIGNAL LOCK WAITING SLOT {pending_api_signal['slot']}: "
                            f"streak={pending_api_signal['trigger_streak']} "
                            f"target_start={pending_api_signal['target_start'].isoformat()} "
                            f"market_not_visible_yet remaining={remaining:.1f}s"
                        )
                        time.sleep(POLL_ACTIVE_SECONDS)
                        continue

                    ticker = market["ticker"]
                    age = market_age_minutes(market)
                    latency_mark(
                        "MARKET_VISIBLE",
                        boundary_dt=pending_api_signal["target_start"],
                        detail=f"ticker={ticker} age={age:.3f}m",
                    )
                    side = pending_api_signal["side"]
                    slot_name = pending_api_signal["slot"]
                    limit_cents = pending_api_signal["entry_cents"]
                    contracts = pending_api_signal["contracts"]
                    expiration_ts = pending_api_signal["expiration_ts"]
                    trigger_result = pending_api_signal["trigger_result"]
                    trigger_streak = pending_api_signal["trigger_streak"]

                    if ticker in completed_markets:
                        print(
                            f"SIGNAL LOCK CLEAR: {ticker} already completed by this bot."
                        )
                        pending_api_signal = None
                        sleep_idle()
                        continue

                    print(
                        f"SIGNAL LOCK FOUND SLOT {slot_name}: {ticker} buy {side.upper()} "
                        f"{contracts:g} @ {limit_cents}c age={age:.2f}m "
                        f"latched_streak={trigger_streak}"
                    )

                    status, order = submit_resting_order_with_retry(
                        ticker, side, limit_cents, expiration_ts, contracts
                    )
                    if status == "market_not_found":
                        # Do NOT throw away the latched signal. The listing and
                        # matching engine can become consistent seconds later.
                        remaining = expiration_ts - utc_now().timestamp()
                        print(
                            f"SIGNAL LOCK RETAINED SLOT {slot_name}: order endpoint "
                            f"does not know {ticker} yet; remaining={remaining:.1f}s"
                        )
                        time.sleep(POLL_ACTIVE_SECONDS)
                        continue
                    if status != "accepted" or not order:
                        remaining = expiration_ts - utc_now().timestamp()
                        print(
                            f"SIGNAL LOCK RETRY SLOT {slot_name}: submission not accepted; "
                            f"remaining={remaining:.1f}s"
                        )
                        time.sleep(POLL_ACTIVE_SECONDS)
                        continue

                    order_id = order.get("order_id")
                    current_order = {
                        "order_id": order_id,
                        "ticker": ticker,
                        "side": side,
                        "entry_cents": limit_cents,
                        "expiration_ts": expiration_ts,
                        "trigger_result": trigger_result,
                        "trigger_streak": trigger_streak,
                        "age_at_submit": age,
                        "contracts": contracts,
                        "slot": slot_name,
                    }
                    _order_target_start = pending_api_signal["target_start"]
                    pending_api_signal = None
                    latency_mark(
                        "ORDER_RESTING",
                        boundary_dt=_order_target_start,
                        detail=f"ticker={ticker} order_id={order_id}",
                    )
                    print(
                        f"ORDER RESTING SLOT {slot_name}: {ticker} {side.upper()} "
                        f"@ {limit_cents}c expires={dt.datetime.fromtimestamp(expiration_ts, dt.timezone.utc).isoformat()}"
                    )
                    time.sleep(POLL_ORDER_SECONDS)
                    continue

            # --------------------------------------------------------------
            # DEMO/LIVE SPEED-FIRST BOUNDARY DECISION
            # --------------------------------------------------------------
            target_start = current_15m_session_start()
            latency_reset_if_new_boundary(target_start)

            decision_markets, fast_row = fast_streak_view(
                immediate_markets, target_start
            )
            fast_ready = (
                fast_row is not None
                and (fast_row.get("immediate_result") or "").lower()
                in {"yes", "no", "tie"}
            )

            if FAST_BOUNDARY_ENABLED and fast_ready:
                last_result, streak = calculate_streak(decision_markets)
                latency_mark(
                    "FAST_RESULT_READY",
                    boundary_dt=target_start,
                    detail=(
                        f"last={last_result} streak={streak} "
                        f"start={fast_row.get('kalshi_start_btc','?')} "
                        f"boundary={fast_row.get('kalshi_close_btc','?')}"
                    ),
                )
                print(
                    f"FAST BOUNDARY RESULT: last={last_result} streak={streak}; "
                    "formal next strike NOT awaited."
                )
            else:
                print(
                    f"FAST BOUNDARY SKIP: target_start={target_start.isoformat()} "
                    "instantaneous Kalshi boundary price unavailable; "
                    "not waiting for a formal strike."
                )
                sleep_idle()
                continue

            latency_mark(
                "STREAK_READY",
                boundary_dt=target_start,
                detail=f"last={last_result} streak={streak}",
            )

            # TIE/FLAG deliberately breaks the streak.
            if last_result not in {"yes", "no"}:
                sleep_idle()
                continue

            strategy = strategy_for_streak(streak)
            if strategy is None:
                print(f"NO SLOT MATCH: streak={streak}; no order.")
                sleep_idle()
                continue

            latency_mark(
                "SLOT_SELECTED",
                boundary_dt=target_start,
                detail=(
                    f"slot={strategy['name']} streak={streak} "
                    f"limit={strategy['max_entry_cents']}c "
                    f"window={strategy['entry_window_minutes']:g}m"
                ),
            )

            entry_window = strategy["entry_window_minutes"]
            contracts = strategy["contracts"]
            limit_cents = strategy["max_entry_cents"]
            slot_name = strategy["name"]

            # The boundary above is confirmed. Lock this validated streak to
            # that exact new 15-minute session and keep retrying market discovery.
            expiration_ts = (
                target_start + dt.timedelta(minutes=entry_window)
            ).timestamp()
            side = opposite_side(last_result)

            if utc_now().timestamp() >= expiration_ts:
                print(
                    f"SKIP: SLOT {slot_name} signal arrived after its "
                    f"{entry_window:g}m entry window."
                )
                sleep_idle()
                continue

            pending_api_signal = {
                "slot": slot_name,
                "trigger_result": last_result,
                "trigger_streak": streak,
                "side": side,
                "entry_cents": float(limit_cents),
                "contracts": float(contracts),
                "target_start": target_start,
                "expiration_ts": expiration_ts,
                "ticker_hint": (
                    preidentified_next_market.get("ticker")
                    if preidentified_next_market is not None
                    and btc_session_start(preidentified_next_market) == target_start
                    else None
                ),
            }
            latency_mark(
                "SIGNAL_LOCKED",
                boundary_dt=target_start,
                detail=(
                    f"slot={slot_name} last={last_result} streak={streak} "
                    f"side={side.upper()} limit={limit_cents}c"
                ),
            )
            print(
                f"SIGNAL LOCKED SLOT {slot_name}: last={last_result} streak={streak} "
                f"buy={side.upper()} limit={limit_cents}c "
                f"target_start={target_start.isoformat()} "
                f"expires={dt.datetime.fromtimestamp(expiration_ts, dt.timezone.utc).isoformat()}"
            )
            # Re-enter the loop immediately; the SIGNAL LOCK handler above will
            # find the exact target market and place the order when it appears.
            time.sleep(min(POLL_ACTIVE_SECONDS, 0.5))
            continue


        except requests.HTTPError as e:
            if is_temporary_service_error(e):
                status = http_status_code(e)
                wait_s = min(
                    max(outage_backoff_seconds, OUTAGE_BACKOFF_START_SECONDS),
                    OUTAGE_BACKOFF_MAX_SECONDS,
                )
                print(
                    f"KALSHI TEMPORARILY UNAVAILABLE: HTTP {status}. "
                    f"Backing off for {wait_s:g}s."
                )
                time.sleep(wait_s)
                outage_backoff_seconds = min(
                    wait_s * 2,
                    OUTAGE_BACKOFF_MAX_SECONDS,
                )
                continue

            print("ERROR:", repr(e))
            time.sleep(min(10.0, POLL_IDLE_SECONDS))

        except Exception as e:
            print("ERROR:", repr(e))
            time.sleep(min(10.0, POLL_IDLE_SECONDS))


if __name__ == "__main__":
    main()
