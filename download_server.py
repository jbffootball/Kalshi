#!/usr/bin/env python3
"""
eth15m_download_and_serve.py

ONE-DEPLOY Railway workflow:
1. Downloads up to 15,000 completed KXETH15M markets + 1-minute candles.
2. Writes files under ./eth_data.
3. Starts a small web server so the files can be downloaded from an iPhone.
4. No redeploy is needed between downloading and serving.

Railway Start Command:
    python eth15m_download_and_serve.py
"""

import argparse
import csv
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, send_from_directory, render_template_string, abort

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXETH15M"
MAX_MARKETS = 15000

LIST_TIMEOUT = 20
LIST_RETRIES = 5
CANDLE_TIMEOUT = 8
CANDLE_ATTEMPTS_PER_ENDPOINT = 2
PROGRESS_EVERY = 25
REQUEST_PAUSE = 0.02

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "eth_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSIONS_PATH = DATA_DIR / "eth_15m_sessions_15k.csv"
CANDLES_PATH = DATA_DIR / "eth_15m_candles_15k.csv"
RAW_PATH = DATA_DIR / "eth_15m_market_raw_15k.jsonl"
CHECKPOINT_PATH = DATA_DIR / "eth_15m_checkpoint.jsonl"
FAILURES_PATH = DATA_DIR / "eth_15m_candle_failures.csv"
REPORT_PATH = DATA_DIR / "eth_15m_download_report.txt"

download_status = {
    "state": "starting",
    "processed": 0,
    "total": MAX_MARKETS,
    "success": 0,
    "skipped": 0,
    "rows": 0,
    "message": "Starting downloader..."
}

app = Flask(__name__)

PAGE = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>ETH Kalshi Downloader</title>
<style>
body { font-family:-apple-system,BlinkMacSystemFont,sans-serif; margin:24px; }
h1 { font-size:26px; }
.card { border:1px solid #ddd; border-radius:12px; padding:16px; margin:14px 0; }
a { display:block; padding:16px; margin:12px 0; border:1px solid #bbb; border-radius:10px; text-decoration:none; font-size:18px; }
.small { color:#666; font-size:14px; }
.ready { font-weight:600; }
</style>
</head>
<body>
<h1>ETH Kalshi 15-Minute Data</h1>

<div class="card">
  <div><b>Status:</b> {{ status.state }}</div>
  <div><b>Markets:</b> {{ status.processed }} / {{ status.total }}</div>
  <div><b>Successful candle markets:</b> {{ status.success }}</div>
  <div><b>Skipped:</b> {{ status.skipped }}</div>
  <div><b>Candle rows:</b> {{ status.rows }}</div>
  <div class="small">{{ status.message }}</div>
</div>

{% if status.state == "DONE" %}
  <p class="ready">Download complete. Tap each file below.</p>
  {% for item in files %}
    {% if item.exists %}
      <a href="/download/{{ item.name }}">{{ item.label }}<br><small>{{ item.name }}</small></a>
    {% else %}
      <div>{{ item.label }} â missing</div>
    {% endif %}
  {% endfor %}
{% else %}
  <p>This page refreshes automatically every 15 seconds. Leave Railway running.</p>
{% endif %}
</body>
</html>
"""

FILES = [
    ("eth_15m_sessions_15k.csv", "ETH sessions"),
    ("eth_15m_candles_15k.csv", "ETH 1-minute candles"),
    ("eth_15m_candle_failures.csv", "Skipped candle markets"),
    ("eth_15m_download_report.txt", "Download report"),
    ("eth_15m_market_raw_15k.jsonl", "Raw market metadata"),
]

@app.route("/")
def index():
    items = []
    for name, label in FILES:
        p = DATA_DIR / name
        items.append({"name": name, "label": label, "exists": p.exists()})
    return render_template_string(PAGE, status=download_status, files=items)

@app.route("/download/<path:filename>")
def download(filename):
    allowed = {name for name, _ in FILES}
    if filename not in allowed:
        abort(404)
    path = DATA_DIR / filename
    if not path.exists():
        abort(404)
    return send_from_directory(DATA_DIR, filename, as_attachment=True)

@app.route("/health")
def health():
    return {"ok": True, **download_status}


def iso_to_dt(x: Any) -> Optional[datetime]:
    if not x:
        return None
    try:
        s = str(x).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def safe_float(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def request_json(session, url, params=None, timeout=10, attempts=2, label=""):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)

            if r.status_code in (400, 401, 403, 404):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

            if r.status_code == 429:
                wait = min(2 * attempt, 6)
                print(f"rate limited on {label}; wait {wait}s", flush=True)
                time.sleep(wait)
                last_error = RuntimeError("HTTP 429")
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(0.5)

    raise RuntimeError(str(last_error))


def paginate_markets(session, historical, max_markets):
    path = "/historical/markets" if historical else "/markets"
    url = BASE + path
    cursor = None
    out = []
    tier = "historical" if historical else "recent"

    while len(out) < max_markets:
        params = {
            "limit": min(1000, max_markets - len(out)),
            "series_ticker": SERIES,
        }
        if cursor:
            params["cursor"] = cursor

        data = request_json(
            session, url, params,
            timeout=LIST_TIMEOUT,
            attempts=LIST_RETRIES,
            label=f"{tier} market list"
        )

        batch = data.get("markets") or []
        out.extend(batch)
        print(f"{tier} markets fetched: {len(out):,}", flush=True)

        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    return out[:max_markets]


def parse_numeric_from_text(text):
    vals = []
    if not text:
        return vals
    for m in re.finditer(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except Exception:
            pass
    return vals


def infer_target_price(m):
    preferred = [
        "strike", "strike_price", "strike_price_dollars",
        "target_price", "target_price_dollars",
        "floor_strike", "cap_strike"
    ]
    for k in preferred:
        v = safe_float(m.get(k))
        if v is not None and 100 <= v <= 100000:
            return v, k

    custom = m.get("custom_strike")
    if isinstance(custom, dict):
        for k, v in custom.items():
            f = safe_float(v)
            if f is not None and 100 <= f <= 100000:
                return f, f"custom_strike.{k}"

    for field in [
        "title", "subtitle", "yes_sub_title", "no_sub_title",
        "rules_primary", "rules_secondary"
    ]:
        text = str(m.get(field) or "")
        candidates = [x for x in parse_numeric_from_text(text) if 100 <= x <= 100000]
        if candidates:
            return candidates[0], f"text:{field}"

    return None, ""


def infer_settlement_value(m):
    for k in ["settlement_value_dollars", "expiration_value", "settlement_value"]:
        v = safe_float(m.get(k))
        if v is not None:
            return v, k
    return None, ""


def normalize_result(m, target, close_value):
    if target is not None and close_value is not None:
        if close_value > target:
            return "YES"
        if close_value < target:
            return "NO"
        return "TIE_INVALID"

    result = str(m.get("result") or "").strip().lower()
    if result == "yes":
        return "YES"
    if result == "no":
        return "NO"
    return "TIE_INVALID"


def market_end_dt(m):
    for k in [
        "close_time", "expiration_time",
        "expected_expiration_time", "latest_expiration_time"
    ]:
        dt = iso_to_dt(m.get(k))
        if dt:
            return dt
    return None


def market_start_dt(m):
    end = market_end_dt(m)
    if end:
        return end - timedelta(minutes=15)
    return iso_to_dt(m.get("open_time"))


def fetch_candles_one_endpoint(session, ticker, start_ts, end_ts, tier):
    if tier == "historical":
        url = BASE + f"/historical/markets/{ticker}/candlesticks"
    else:
        url = BASE + f"/series/{SERIES}/markets/{ticker}/candlesticks"

    params = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": 1,
    }

    data = request_json(
        session, url, params,
        timeout=CANDLE_TIMEOUT,
        attempts=CANDLE_ATTEMPTS_PER_ENDPOINT,
        label=ticker
    )
    return data.get("candlesticks") or []


def fetch_candles_with_fallback(session, ticker, start_ts, end_ts, preferred_tier):
    tiers = [preferred_tier, "recent" if preferred_tier == "historical" else "historical"]
    errors = []

    for tier in tiers:
        try:
            candles = fetch_candles_one_endpoint(
                session, ticker, start_ts, end_ts, tier
            )
            if candles:
                return candles, tier, ""
            errors.append(f"{tier}: empty")
        except Exception as e:
            errors.append(f"{tier}: {e}")

    return [], "", " | ".join(errors)


def flatten_price(prefix, obj, row):
    if not isinstance(obj, dict):
        return
    for k in ["open", "high", "low", "close"]:
        if k in obj:
            row[f"{prefix}_{k}"] = obj.get(k)
    for k in ["open_dollars", "high_dollars", "low_dollars", "close_dollars"]:
        if k in obj:
            row[f"{prefix}_{k}"] = obj.get(k)


def run_download():
    try:
        download_status.update({
            "state": "DOWNLOADING",
            "processed": 0,
            "total": MAX_MARKETS,
            "success": 0,
            "skipped": 0,
            "rows": 0,
            "message": "Fetching market metadata..."
        })

        # Start fresh every deployment.
        for p in [
            SESSIONS_PATH, CANDLES_PATH, RAW_PATH,
            CHECKPOINT_PATH, FAILURES_PATH, REPORT_PATH
        ]:
            if p.exists():
                p.unlink()

        session = requests.Session()
        session.headers.update({
            "User-Agent": "kalshi-eth15m-research/3.0",
            "Accept": "application/json",
            "Connection": "keep-alive"
        })

        recent = paginate_markets(session, False, MAX_MARKETS)
        historical = paginate_markets(session, True, MAX_MARKETS)

        by_ticker = {}
        source = {}

        for m in recent:
            t = m.get("ticker")
            if t:
                by_ticker[t] = m
                source[t] = "recent"

        for m in historical:
            t = m.get("ticker")
            if t:
                by_ticker[t] = m
                source[t] = "historical"

        all_markets = list(by_ticker.values())
        completed = []

        for m in all_markets:
            status = str(m.get("status") or "").lower()
            result = str(m.get("result") or "").lower()
            end = market_end_dt(m)
            if end and (
                status in {"closed", "settled", "finalized"}
                or result in {"yes", "no"}
            ):
                completed.append(m)

        completed.sort(key=lambda m: market_end_dt(m) or datetime.max.replace(tzinfo=timezone.utc))

        if len(completed) > MAX_MARKETS:
            completed = completed[-MAX_MARKETS:]

        download_status["total"] = len(completed)
        download_status["message"] = f"Selected {len(completed):,} completed markets."

        with RAW_PATH.open("w", encoding="utf-8") as f:
            for m in completed:
                t = m.get("ticker")
                f.write(json.dumps({"_source_tier": source.get(t, ""), **m}, ensure_ascii=False) + "\n")

        session_rows = []
        target_sources = {}
        settlement_sources = {}

        for m in completed:
            ticker = m.get("ticker")
            target, target_src = infer_target_price(m)
            close_value, close_src = infer_settlement_value(m)
            result = normalize_result(m, target, close_value)
            start = market_start_dt(m)
            end = market_end_dt(m)

            if target_src:
                target_sources[target_src] = target_sources.get(target_src, 0) + 1
            if close_src:
                settlement_sources[close_src] = settlement_sources.get(close_src, 0) + 1

            session_rows.append({
                "ticker": ticker,
                "session_start_utc": start.isoformat() if start else "",
                "session_end_utc": end.isoformat() if end else "",
                "status": m.get("status"),
                "official_result": m.get("result"),
                "target_price": target,
                "target_price_source": target_src,
                "closing_value": close_value,
                "closing_value_source": close_src,
                "normalized_result": result,
                "volume": m.get("volume"),
                "volume_fp": m.get("volume_fp"),
                "open_interest": m.get("open_interest"),
                "open_interest_fp": m.get("open_interest_fp"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "no_bid": m.get("no_bid"),
                "no_ask": m.get("no_ask"),
                "last_price": m.get("last_price"),
                "source_tier": source.get(ticker, ""),
                "title": m.get("title"),
                "subtitle": m.get("subtitle"),
            })

        if session_rows:
            with SESSIONS_PATH.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(session_rows[0].keys()))
                writer.writeheader()
                writer.writerows(session_rows)

        candle_fields = [
            "ticker", "session_start_utc", "session_end_utc",
            "candle_end_period", "minute_number", "source_tier",
            "price_open", "price_high", "price_low", "price_close",
            "price_open_dollars", "price_high_dollars", "price_low_dollars", "price_close_dollars",
            "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
            "yes_bid_open_dollars", "yes_bid_high_dollars", "yes_bid_low_dollars", "yes_bid_close_dollars",
            "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close",
            "yes_ask_open_dollars", "yes_ask_high_dollars", "yes_ask_low_dollars", "yes_ask_close_dollars",
            "volume", "open_interest"
        ]

        failure_fields = ["ticker", "preferred_tier", "error"]

        with CANDLES_PATH.open("w", newline="", encoding="utf-8") as candle_f, \
             FAILURES_PATH.open("w", newline="", encoding="utf-8") as fail_f:

            cw = csv.DictWriter(candle_f, fieldnames=candle_fields)
            fw = csv.DictWriter(fail_f, fieldnames=failure_fields)
            cw.writeheader()
            fw.writeheader()

            for idx, m in enumerate(completed, 1):
                ticker = m.get("ticker")
                start = market_start_dt(m)
                end = market_end_dt(m)

                if not ticker or not start or not end:
                    download_status["skipped"] += 1
                    download_status["processed"] = idx
                    continue

                start_ts = int(start.timestamp()) - 60
                end_ts = int(end.timestamp()) + 60
                preferred = source.get(ticker, "historical")

                candles, used_tier, error = fetch_candles_with_fallback(
                    session, ticker, start_ts, end_ts, preferred
                )

                market_rows = 0

                if candles:
                    for c in candles:
                        row = {
                            "ticker": ticker,
                            "session_start_utc": start.isoformat(),
                            "session_end_utc": end.isoformat(),
                            "candle_end_period": c.get("end_period_ts"),
                            "minute_number": "",
                            "source_tier": used_tier,
                            "volume": c.get("volume"),
                            "open_interest": c.get("open_interest"),
                        }

                        ep = c.get("end_period_ts")
                        try:
                            ep_dt = datetime.fromtimestamp(int(ep), tz=timezone.utc)
                            row["minute_number"] = int(round((ep_dt - start).total_seconds() / 60))
                        except Exception:
                            pass

                        flatten_price("price", c.get("price"), row)
                        flatten_price("yes_bid", c.get("yes_bid"), row)
                        flatten_price("yes_ask", c.get("yes_ask"), row)

                        cw.writerow(row)
                        market_rows += 1

                    candle_f.flush()
                    download_status["success"] += 1
                    download_status["rows"] += market_rows
                else:
                    fw.writerow({
                        "ticker": ticker,
                        "preferred_tier": preferred,
                        "error": error
                    })
                    fail_f.flush()
                    download_status["skipped"] += 1

                download_status["processed"] = idx
                download_status["message"] = f"Processing {ticker}"

                if idx % PROGRESS_EVERY == 0:
                    print(
                        f"Candles: {idx:,}/{len(completed):,} | "
                        f"success {download_status['success']:,} | "
                        f"skipped {download_status['skipped']:,} | "
                        f"rows {download_status['rows']:,}",
                        flush=True
                    )

                time.sleep(REQUEST_PAUSE)

        first_end = market_end_dt(completed[0]) if completed else None
        last_end = market_end_dt(completed[-1]) if completed else None

        report = [
            "ETH 15-MINUTE KALSHI DOWNLOAD REPORT",
            "=" * 50,
            f"Series: {SERIES}",
            f"Completed markets selected: {len(completed):,}",
            f"First selected market end: {first_end.isoformat() if first_end else 'n/a'}",
            f"Last selected market end: {last_end.isoformat() if last_end else 'n/a'}",
            f"Candle markets successful: {download_status['success']:,}",
            f"Candle markets skipped: {download_status['skipped']:,}",
            f"Total candle rows: {download_status['rows']:,}",
            "",
            "Target-price extraction sources:",
        ]
        for k, v in sorted(target_sources.items(), key=lambda kv: -kv[1]):
            report.append(f"  {k}: {v:,}")

        report.append("")
        report.append("Settlement-value extraction sources:")
        for k, v in sorted(settlement_sources.items(), key=lambda kv: -kv[1]):
            report.append(f"  {k}: {v:,}")

        REPORT_PATH.write_text("\n".join(report), encoding="utf-8")

        download_status["state"] = "DONE"
        download_status["message"] = "Download finished. Files are ready below."

        print("=" * 70, flush=True)
        print("DONE - FILES ARE NOW AVAILABLE FROM THE WEB PAGE", flush=True)
        print("=" * 70, flush=True)

    except Exception as e:
        download_status["state"] = "ERROR"
        download_status["message"] = str(e)
        print(f"DOWNLOAD ERROR: {e}", flush=True)


if __name__ == "__main__":
    # Start the download in the background while Flask serves the status page.
    worker = threading.Thread(target=run_download, daemon=True)
    worker.start()

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
