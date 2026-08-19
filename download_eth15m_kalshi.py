#!/usr/bin/env python3
"""
download_eth15m_kalshi_v2.py

Robust downloader for up to 15,000 completed Kalshi ETH 15-minute markets
(KXETH15M) plus 1-minute candlesticks.

Designed for Railway:
- short candle request timeouts
- max 2 candle attempts per endpoint
- skips bad/slow markets instead of hanging
- progress every 25 markets
- checkpoint/resume support
- records skipped candle markets
- uses Kalshi's current documented live candlestick endpoint
- uses historical/archive endpoint for archived markets

Outputs under ./eth_data:
  eth_15m_sessions_15k.csv
  eth_15m_candles_15k.csv
  eth_15m_market_raw_15k.jsonl
  eth_15m_checkpoint.jsonl
  eth_15m_candle_failures.csv
  eth_15m_download_report.txt

Run:
  python download_eth15m_kalshi_v2.py
"""

import argparse
import csv
import json
import math
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXETH15M"
DEFAULT_MAX_MARKETS = 15000
LIST_TIMEOUT = 20
LIST_RETRIES = 5
CANDLE_TIMEOUT = 8
CANDLE_ATTEMPTS_PER_ENDPOINT = 2
PROGRESS_EVERY = 25
REQUEST_PAUSE = 0.02


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
                print(f"  rate limited{(' on ' + label) if label else ''}; waiting {wait}s (attempt {attempt}/{attempts})", flush=True)
                time.sleep(wait)
                last_error = RuntimeError("HTTP 429 rate limit")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_error = e
            if attempt < attempts:
                print(f"  retry {attempt + 1}/{attempts}{(' for ' + label) if label else ''}: {type(e).__name__}", flush=True)
                time.sleep(0.5)
    raise RuntimeError(str(last_error))


def paginate_markets(session, historical: bool, max_markets: int):
    path = "/historical/markets" if historical else "/markets"
    url = BASE + path
    cursor = None
    out = []
    tier = "historical" if historical else "recent"
    while len(out) < max_markets:
        params = {"limit": min(1000, max_markets - len(out)), "series_ticker": SERIES}
        if cursor:
            params["cursor"] = cursor
        data = request_json(session, url, params=params, timeout=LIST_TIMEOUT, attempts=LIST_RETRIES, label=f"{tier} market list")
        batch = data.get("markets") or []
        out.extend(batch)
        print(f"{tier} markets fetched: {len(out):,}", flush=True)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return out[:max_markets]


def parse_numeric_from_text(text: str):
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
    preferred = ["strike", "strike_price", "strike_price_dollars", "target_price", "target_price_dollars", "floor_strike", "cap_strike"]
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
    for field in ["title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary"]:
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
    for k in ["close_time", "expiration_time", "expected_expiration_time", "latest_expiration_time"]:
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
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1}
    data = request_json(session, url, params=params, timeout=CANDLE_TIMEOUT, attempts=CANDLE_ATTEMPTS_PER_ENDPOINT, label=ticker)
    return data.get("candlesticks") or []


def fetch_candles_with_fallback(session, ticker, start_ts, end_ts, preferred_tier):
    tiers = [preferred_tier, "recent" if preferred_tier == "historical" else "historical"]
    errors = []
    for tier in tiers:
        try:
            candles = fetch_candles_one_endpoint(session, ticker, start_ts, end_ts, tier)
            if candles:
                return candles, tier, ""
            errors.append(f"{tier}: empty candle response")
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


def read_checkpoint(path):
    done = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("ticker"):
                    done.add(rec["ticker"])
            except Exception:
                pass
    return done


def append_checkpoint(path, ticker, status, rows, error=""):
    rec = {"ticker": ticker, "status": status, "candle_rows": rows, "error": error, "time_utc": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="eth_data")
    ap.add_argument("--max-markets", type=int, default=DEFAULT_MAX_MARKETS)
    ap.add_argument("--fresh", action="store_true", help="Delete old candle/checkpoint/failure outputs and start fresh.")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    sessions_path = outdir / "eth_15m_sessions_15k.csv"
    candles_path = outdir / "eth_15m_candles_15k.csv"
    raw_path = outdir / "eth_15m_market_raw_15k.jsonl"
    checkpoint_path = outdir / "eth_15m_checkpoint.jsonl"
    failures_path = outdir / "eth_15m_candle_failures.csv"
    report_path = outdir / "eth_15m_download_report.txt"

    if args.fresh:
        for p in [candles_path, checkpoint_path, failures_path, report_path]:
            if p.exists():
                p.unlink()

    session = requests.Session()
    session.headers.update({"User-Agent": "kalshi-eth15m-research/2.0", "Accept": "application/json", "Connection": "keep-alive"})

    print("=" * 70, flush=True)
    print("ETH 15-MINUTE KALSHI DOWNLOADER v2", flush=True)
    print(f"Series: {SERIES}", flush=True)
    print(f"Target completed markets: {args.max_markets:,}", flush=True)
    print(f"Candle timeout: {CANDLE_TIMEOUT}s; {CANDLE_ATTEMPTS_PER_ENDPOINT} attempts per endpoint", flush=True)
    print("=" * 70, flush=True)

    recent = paginate_markets(session, historical=False, max_markets=args.max_markets)
    historical = paginate_markets(session, historical=True, max_markets=args.max_markets)

    by_ticker, source = {}, {}
    for m in recent:
        ticker = m.get("ticker")
        if ticker:
            by_ticker[ticker] = m
            source[ticker] = "recent"
    for m in historical:
        ticker = m.get("ticker")
        if ticker:
            by_ticker[ticker] = m
            source[ticker] = "historical"

    all_markets = list(by_ticker.values())
    completed = []
    for m in all_markets:
        status = str(m.get("status") or "").lower()
        result = str(m.get("result") or "").lower()
        end = market_end_dt(m)
        if end and (status in {"closed", "settled", "finalized"} or result in {"yes", "no"}):
            completed.append(m)

    completed.sort(key=lambda m: market_end_dt(m) or datetime.max.replace(tzinfo=timezone.utc))
    if len(completed) > args.max_markets:
        completed = completed[-args.max_markets:]

    print(f"\nUnique markets found: {len(all_markets):,}\nCompleted markets selected: {len(completed):,}", flush=True)
    if not completed:
        raise RuntimeError("No completed KXETH15M markets found.")

    with raw_path.open("w", encoding="utf-8") as f:
        for m in completed:
            ticker = m.get("ticker")
            f.write(json.dumps({"_source_tier": source.get(ticker, ""), **m}, ensure_ascii=False) + "\n")

    session_rows = []
    target_sources, settlement_sources = {}, {}
    for m in completed:
        ticker = m.get("ticker")
        target, target_src = infer_target_price(m)
        close_value, close_src = infer_settlement_value(m)
        result = normalize_result(m, target, close_value)
        start, end = market_start_dt(m), market_end_dt(m)
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

    with sessions_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(session_rows[0].keys()))
        writer.writeheader()
        writer.writerows(session_rows)

    candle_fields = [
        "ticker", "session_start_utc", "session_end_utc", "candle_end_period", "minute_number", "source_tier",
        "price_open", "price_high", "price_low", "price_close",
        "price_open_dollars", "price_high_dollars", "price_low_dollars", "price_close_dollars",
        "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
        "yes_bid_open_dollars", "yes_bid_high_dollars", "yes_bid_low_dollars", "yes_bid_close_dollars",
        "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close",
        "yes_ask_open_dollars", "yes_ask_high_dollars", "yes_ask_low_dollars", "yes_ask_close_dollars",
        "volume", "open_interest",
    ]

    done_tickers = read_checkpoint(checkpoint_path)
    print(f"\nCheckpoint contains {len(done_tickers):,} already-processed markets.", flush=True)

    candle_exists = candles_path.exists() and candles_path.stat().st_size > 0
    fail_exists = failures_path.exists() and failures_path.stat().st_size > 0
    candle_mode = "a" if candle_exists else "w"
    fail_mode = "a" if fail_exists else "w"

    processed_this_run = success_this_run = skipped_this_run = candle_rows_this_run = 0
    start_clock = time.time()

    with candles_path.open(candle_mode, newline="", encoding="utf-8") as candle_f, failures_path.open(fail_mode, newline="", encoding="utf-8") as fail_f:
        candle_writer = csv.DictWriter(candle_f, fieldnames=candle_fields)
        if not candle_exists:
            candle_writer.writeheader()
        failure_fields = ["ticker", "preferred_tier", "error"]
        failure_writer = csv.DictWriter(fail_f, fieldnames=failure_fields)
        if not fail_exists:
            failure_writer.writeheader()

        for m in completed:
            ticker = m.get("ticker")
            if not ticker or ticker in done_tickers:
                continue
            start, end = market_start_dt(m), market_end_dt(m)
            if not start or not end:
                err = "missing session start/end"
                failure_writer.writerow({"ticker": ticker, "preferred_tier": source.get(ticker, ""), "error": err})
                fail_f.flush()
                append_checkpoint(checkpoint_path, ticker, "skipped", 0, err)
                skipped_this_run += 1
                processed_this_run += 1
                continue

            start_ts = int(start.timestamp()) - 60
            end_ts = int(end.timestamp()) + 60
            preferred = source.get(ticker, "historical")
            candles, used_tier, error = fetch_candles_with_fallback(session, ticker, start_ts, end_ts, preferred)
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
                    candle_writer.writerow(row)
                    market_rows += 1
                candle_f.flush()
                append_checkpoint(checkpoint_path, ticker, "success", market_rows, "")
                success_this_run += 1
                candle_rows_this_run += market_rows
            else:
                failure_writer.writerow({"ticker": ticker, "preferred_tier": preferred, "error": error})
                fail_f.flush()
                append_checkpoint(checkpoint_path, ticker, "skipped", 0, error)
                skipped_this_run += 1

            processed_this_run += 1
            if processed_this_run % PROGRESS_EVERY == 0:
                total_done = len(done_tickers) + processed_this_run
                elapsed = max(time.time() - start_clock, 0.001)
                rate = processed_this_run / elapsed
                remaining = max(len(completed) - total_done, 0)
                eta_min = (remaining / rate / 60) if rate > 0 else 0
                print(f"Candles: {total_done:,}/{len(completed):,} markets processed | success {success_this_run:,} | skipped {skipped_this_run:,} | rows {candle_rows_this_run:,} | ETA ~{eta_min:.1f} min", flush=True)
            time.sleep(REQUEST_PAUSE)

    checkpoint_records = checkpoint_success = checkpoint_skipped = total_candle_rows = 0
    if checkpoint_path.exists():
        with checkpoint_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    checkpoint_records += 1
                    if rec.get("status") == "success":
                        checkpoint_success += 1
                    elif rec.get("status") == "skipped":
                        checkpoint_skipped += 1
                    total_candle_rows += int(rec.get("candle_rows") or 0)
                except Exception:
                    pass

    first_end, last_end = market_end_dt(completed[0]), market_end_dt(completed[-1])
    report_lines = [
        "ETH 15-MINUTE KALSHI DOWNLOAD REPORT", "=" * 50,
        f"Series: {SERIES}",
        f"Requested completed markets: {args.max_markets:,}",
        f"Unique markets retrieved: {len(all_markets):,}",
        f"Completed markets selected: {len(completed):,}",
        f"First selected market end: {first_end.isoformat() if first_end else 'n/a'}",
        f"Last selected market end: {last_end.isoformat() if last_end else 'n/a'}",
        "", f"Checkpoint records: {checkpoint_records:,}",
        f"Candle markets successful: {checkpoint_success:,}",
        f"Candle markets skipped: {checkpoint_skipped:,}",
        f"Total candle rows recorded: {total_candle_rows:,}",
        "", "Target-price extraction sources:",
    ]
    for k, v in sorted(target_sources.items(), key=lambda kv: -kv[1]):
        report_lines.append(f"  {k}: {v:,}")
    report_lines += ["", "Settlement-value extraction sources:"]
    for k, v in sorted(settlement_sources.items(), key=lambda kv: -kv[1]):
        report_lines.append(f"  {k}: {v:,}")
    report_lines += ["", f"Sessions file: {sessions_path}", f"Candles file: {candles_path}", f"Failures file: {failures_path}", f"Raw market file: {raw_path}", f"Checkpoint file: {checkpoint_path}"]
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    print("\n" + "=" * 70, flush=True)
    print("DONE", flush=True)
    print(f"Sessions:   {sessions_path}", flush=True)
    print(f"Candles:    {candles_path}", flush=True)
    print(f"Failures:   {failures_path}", flush=True)
    print(f"Raw JSON:   {raw_path}", flush=True)
    print(f"Checkpoint: {checkpoint_path}", flush=True)
    print(f"Report:     {report_path}", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
