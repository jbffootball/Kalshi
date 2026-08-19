#!/usr/bin/env python3
"""
download_eth15m_kalshi.py

Download up to 15,000 completed Kalshi ETH 15-minute markets (KXETH15M)
plus 1-minute candlesticks into one continuous research dataset.

Outputs:
  eth_15m_sessions_15k.csv
  eth_15m_candles_15k.csv
  eth_15m_market_raw_15k.jsonl
  eth_15m_download_report.txt

Usage:
  python download_eth15m_kalshi.py

Optional:
  python download_eth15m_kalshi.py --outdir eth_data
  python download_eth15m_kalshi.py --max-markets 15000
  python download_eth15m_kalshi.py --skip-candles
"""

import argparse
import csv
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXETH15M"
TIMEOUT = 30
MAX_RETRIES = 6
DEFAULT_MAX_MARKETS = 15000


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


def get_json(session, path, params=None):
    url = BASE + path
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(min(2 ** attempt, 20))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(min(1.5 * (attempt + 1), 8))
    raise RuntimeError(f"GET failed: {url} params={params}: {last}")


def paginate_markets(session, historical: bool, max_markets: int) -> List[Dict[str, Any]]:
    path = "/historical/markets" if historical else "/markets"
    cursor = None
    out = []

    while len(out) < max_markets:
        params = {"limit": min(1000, max_markets - len(out)), "series_ticker": SERIES}
        if cursor:
            params["cursor"] = cursor

        data = get_json(session, path, params)
        batch = data.get("markets") or []
        out.extend(batch)

        print(f"{'historical' if historical else 'recent'} markets fetched: {len(out):,}", flush=True)

        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    return out[:max_markets]


def parse_numeric_from_text(text: str) -> List[float]:
    vals = []
    if not text:
        return vals
    for m in re.finditer(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except Exception:
            pass
    return vals


def infer_target_price(m: Dict[str, Any]) -> Tuple[Optional[float], str]:
    # Prefer explicit strike-like fields.
    preferred = [
        "strike", "strike_price", "strike_price_dollars",
        "target_price", "target_price_dollars",
        "floor_strike", "cap_strike",
    ]
    for k in preferred:
        v = safe_float(m.get(k))
        if v is not None and 100 <= v <= 100000:
            return v, k

    cs = m.get("custom_strike")
    if isinstance(cs, dict):
        for k, v in cs.items():
            f = safe_float(v)
            if f is not None and 100 <= f <= 100000:
                return f, f"custom_strike.{k}"

    # Fall back to title/subtitle/rules text.
    for field in ["title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary"]:
        text = str(m.get(field) or "")
        candidates = [x for x in parse_numeric_from_text(text) if 100 <= x <= 100000]
        if candidates:
            return candidates[0], f"text:{field}"

    return None, ""


def infer_settlement_value(m: Dict[str, Any]) -> Tuple[Optional[float], str]:
    for k in ["settlement_value_dollars", "expiration_value", "settlement_value"]:
        v = safe_float(m.get(k))
        if v is not None:
            # Some older APIs may encode cents; avoid auto-converting unless obviously huge/small.
            return v, k
    return None, ""


def normalize_result(m: Dict[str, Any], target: Optional[float], close_value: Optional[float]) -> str:
    # Prefer direct numeric comparison if both exist.
    if target is not None and close_value is not None:
        if close_value > target:
            return "YES"
        if close_value < target:
            return "NO"
        return "TIE_INVALID"

    # Fall back to official result only if available.
    r = str(m.get("result") or "").strip().lower()
    if r == "yes":
        return "YES"
    if r == "no":
        return "NO"
    return "TIE_INVALID"


def market_end_dt(m: Dict[str, Any]) -> Optional[datetime]:
    for k in ["close_time", "expiration_time", "expected_expiration_time", "latest_expiration_time"]:
        dt = iso_to_dt(m.get(k))
        if dt:
            return dt
    return None


def market_start_dt(m: Dict[str, Any]) -> Optional[datetime]:
    for k in ["open_time", "expected_expiration_time"]:
        dt = iso_to_dt(m.get(k))
        if dt:
            return dt
    end = market_end_dt(m)
    if end:
        from datetime import timedelta
        return end - timedelta(minutes=15)
    return None


def candle_endpoint(historical: bool, ticker: str) -> str:
    if historical:
        return f"/historical/markets/{ticker}/candlesticks"
    return f"/markets/{ticker}/candlesticks"


def fetch_candles(session, ticker: str, start_ts: int, end_ts: int, historical: bool):
    path = candle_endpoint(historical, ticker)
    params = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": 1,
    }
    data = get_json(session, path, params)
    return data.get("candlesticks") or []


def flatten_price(prefix: str, obj: Any, row: Dict[str, Any]):
    if not isinstance(obj, dict):
        return
    for k in ["open", "high", "low", "close"]:
        if k in obj:
            row[f"{prefix}_{k}"] = obj.get(k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="eth_data")
    ap.add_argument("--max-markets", type=int, default=DEFAULT_MAX_MARKETS)
    ap.add_argument("--skip-candles", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    s = requests.Session()
    s.headers.update({"User-Agent": "kalshi-eth15m-research/1.0"})

    print(f"Fetching up to {args.max_markets:,} KXETH15M markets...", flush=True)

    recent = paginate_markets(s, historical=False, max_markets=args.max_markets)
    hist = paginate_markets(s, historical=True, max_markets=args.max_markets)

    # De-duplicate by ticker; historical version wins for older settled markets.
    by_ticker = {}
    source = {}
    for m in recent:
        t = m.get("ticker")
        if t:
            by_ticker[t] = m
            source[t] = "recent"
    for m in hist:
        t = m.get("ticker")
        if t:
            by_ticker[t] = m
            source[t] = "historical"

    markets = list(by_ticker.values())

    # Keep completed/settled-looking markets, then sort chronologically.
    completed = []
    for m in markets:
        status = str(m.get("status") or "").lower()
        result = str(m.get("result") or "").lower()
        end = market_end_dt(m)
        if end and (status in {"closed", "settled", "finalized"} or result in {"yes", "no"}):
            completed.append(m)

    completed.sort(key=lambda m: market_end_dt(m) or datetime.max.replace(tzinfo=timezone.utc))

    if len(completed) > args.max_markets:
        # Keep the most recent continuous block of N completed markets.
        completed = completed[-args.max_markets:]

    raw_path = outdir / "eth_15m_market_raw_15k.jsonl"
    sessions_path = outdir / "eth_15m_sessions_15k.csv"
    candles_path = outdir / "eth_15m_candles_15k.csv"
    report_path = outdir / "eth_15m_download_report.txt"

    with raw_path.open("w", encoding="utf-8") as f:
        for m in completed:
            rec = {"_source_tier": source.get(m.get("ticker"), ""), **m}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    session_rows = []
    target_sources = {}
    settle_sources = {}

    for i, m in enumerate(completed, 1):
        target, target_src = infer_target_price(m)
        close_val, close_src = infer_settlement_value(m)
        result = normalize_result(m, target, close_val)

        if target_src:
            target_sources[target_src] = target_sources.get(target_src, 0) + 1
        if close_src:
            settle_sources[close_src] = settle_sources.get(close_src, 0) + 1

        start = market_start_dt(m)
        end = market_end_dt(m)

        row = {
            "ticker": m.get("ticker"),
            "session_start_utc": start.isoformat() if start else "",
            "session_end_utc": end.isoformat() if end else "",
            "status": m.get("status"),
            "official_result": m.get("result"),
            "target_price": target,
            "target_price_source": target_src,
            "closing_value": close_val,
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
            "source_tier": source.get(m.get("ticker"), ""),
            "title": m.get("title"),
            "subtitle": m.get("subtitle"),
        }
        session_rows.append(row)

    session_fields = list(session_rows[0].keys()) if session_rows else []
    with sessions_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=session_fields)
        w.writeheader()
        w.writerows(session_rows)

    candle_count = 0
    candle_failures = []
    if not args.skip_candles:
        candle_fields = [
            "ticker", "session_start_utc", "session_end_utc", "candle_end_period",
            "minute_number", "source_tier",
            "price_open", "price_high", "price_low", "price_close",
            "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
            "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close",
            "volume", "open_interest",
        ]

        with candles_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=candle_fields)
            w.writeheader()

            for idx, m in enumerate(completed, 1):
                ticker = m.get("ticker")
                start = market_start_dt(m)
                end = market_end_dt(m)
                if not ticker or not start or not end:
                    candle_failures.append((ticker, "missing start/end"))
                    continue

                # Add a tiny buffer around the 15m window; we'll retain rows returned by Kalshi.
                start_ts = int(start.timestamp()) - 60
                end_ts = int(end.timestamp()) + 60

                tiers = [source.get(ticker, "historical")]
                if tiers[0] == "historical":
                    tiers.append("recent")
                else:
                    tiers.append("historical")

                candles = None
                err = None
                used_tier = None
                for tier in tiers:
                    try:
                        candles = fetch_candles(s, ticker, start_ts, end_ts, historical=(tier == "historical"))
                        if candles is not None:
                            used_tier = tier
                            break
                    except Exception as e:
                        err = str(e)

                if candles is None:
                    candle_failures.append((ticker, err or "unknown error"))
                    continue

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
                        mins = int(round((ep_dt - start).total_seconds() / 60))
                        row["minute_number"] = mins
                    except Exception:
                        pass

                    flatten_price("price", c.get("price"), row)
                    flatten_price("yes_bid", c.get("yes_bid"), row)
                    flatten_price("yes_ask", c.get("yes_ask"), row)

                    w.writerow(row)
                    candle_count += 1

                if idx % 100 == 0:
                    print(f"Candles: {idx:,}/{len(completed):,} markets processed; {candle_count:,} rows", flush=True)

                # Be polite to API and reduce 429s.
                time.sleep(0.03)

    first_end = market_end_dt(completed[0]).isoformat() if completed else "n/a"
    last_end = market_end_dt(completed[-1]).isoformat() if completed else "n/a"

    report = [
        f"Series: {SERIES}",
        f"Requested max completed markets: {args.max_markets:,}",
        f"Unique markets retrieved before completion filter: {len(markets):,}",
        f"Completed markets saved: {len(completed):,}",
        f"First saved market end: {first_end}",
        f"Last saved market end: {last_end}",
        f"1-minute candle rows saved: {candle_count:,}",
        f"Candle failures: {len(candle_failures):,}",
        "",
        "Target-price extraction sources:",
    ]
    for k, v in sorted(target_sources.items(), key=lambda kv: -kv[1]):
        report.append(f"  {k}: {v:,}")

    report += ["", "Settlement-value extraction sources:"]
    for k, v in sorted(settle_sources.items(), key=lambda kv: -kv[1]):
        report.append(f"  {k}: {v:,}")

    if candle_failures:
        report += ["", "First 25 candle failures:"]
        for t, e in candle_failures[:25]:
            report.append(f"  {t}: {e}")

    report_path.write_text("\n".join(report), encoding="utf-8")

    print("\nDONE")
    print(f"Sessions: {sessions_path}")
    print(f"Candles:  {candles_path}")
    print(f"Raw JSON: {raw_path}")
    print(f"Report:   {report_path}")


if __name__ == "__main__":
    main()
