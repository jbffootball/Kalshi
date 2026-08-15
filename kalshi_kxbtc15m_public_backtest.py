#!/usr/bin/env python3
"""
Kalshi KXBTC15M universal-exit backtester.

Purpose:
- Reuse Kalshi-only public 1-minute candle data.
- Test candidate entry streams with one UNIVERSAL stop-loss and one UNIVERSAL sell-early rule.
- Hold-to-close remains the baseline.
- Because candles are 1-minute, same-candle stop/target ordering is ambiguous. We report both:
    * conservative: if both touched in same candle, STOP is assumed first
    * optimistic:   if both touched in same candle, TARGET is assumed first

Default entry streams:
    S2 <= 25c / 2 min
    S3 <= 29c / 2 min
    S5 <= 33c / 3 min
These can be changed with environment variables.

Outputs:
    /data/kalshi_exit_backtest/exit_grid_results.csv
    /data/kalshi_exit_backtest/exit_trade_details.csv
    /data/kalshi_exit_backtest/run_summary.json

No trading. No private key. Public Kalshi data only.
"""

import csv
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("SERIES_TICKER", "KXBTC15M")
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "5000"))

# Three entry streams, configurable.
STREAMS = [
    {"name": "A", "streak": int(os.getenv("A_STREAK", "2")), "entry": int(os.getenv("A_ENTRY_CENTS", "25")), "window": int(os.getenv("A_WINDOW_MIN", "2"))},
    {"name": "B", "streak": int(os.getenv("B_STREAK", "3")), "entry": int(os.getenv("B_ENTRY_CENTS", "29")), "window": int(os.getenv("B_WINDOW_MIN", "2"))},
    {"name": "C", "streak": int(os.getenv("C_STREAK", "5")), "entry": int(os.getenv("C_ENTRY_CENTS", "33")), "window": int(os.getenv("C_WINDOW_MIN", "3"))},
]

# Universal exits. 0 means OFF for stop, 100 means OFF for target.
STOP_LEVELS = [0, 5, 10, 15, 20]
TARGET_LEVELS = [100, 70, 75, 80, 85, 90]

OUT_DIR = Path(os.getenv("OUT_DIR", "/data/kalshi_exit_backtest"))
if not OUT_DIR.parent.exists():
    OUT_DIR = Path("./kalshi_exit_backtest")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-public-research-exit/1.0"})

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_time(s):
    return datetime.fromisoformat(str(s).replace("Z","+00:00")).astimezone(timezone.utc)

def get_json(path, params=None, tries=8):
    url = BASE + path
    delay = 1.0
    last = None
    for _ in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code}: {r.text[:250]}"
            if r.status_code in (429,500,502,503,504):
                time.sleep(delay)
                delay = min(delay*2, 30)
                continue
            raise RuntimeError(last)
        except requests.RequestException as e:
            last = repr(e)
            time.sleep(delay)
            delay = min(delay*2, 30)
    raise RuntimeError(f"{url}: {last}")

def fetch_pages(path):
    out = []
    cursor = None
    while True:
        params = {"limit":1000, "series_ticker":SERIES}
        if cursor:
            params["cursor"] = cursor
        d = get_json(path, params)
        rows = d.get("markets", [])
        out.extend(rows)
        cursor = d.get("cursor")
        print(f"{path}: {len(out):,} markets", flush=True)
        if not cursor or not rows:
            break
    return out

def fetch_markets():
    by = {}
    for m in fetch_pages("/historical/markets") + fetch_pages("/markets"):
        if m.get("ticker"):
            by[m["ticker"]] = m
    rows = [m for m in by.values() if m.get("close_time") and m.get("floor_strike") is not None]
    rows.sort(key=lambda x: parse_time(x["close_time"]))
    if len(rows) > MAX_MARKETS + 1:
        rows = rows[-(MAX_MARKETS+1):]
    return rows

def derive_sessions(markets):
    base = []
    run_side = None
    run_len = 0
    for i in range(len(markets)-1):
        m, nxt = markets[i], markets[i+1]
        a, b = float(m["floor_strike"]), float(nxt["floor_strike"])
        result = "YES" if b>a else ("NO" if b<a else "TIE")
        if result == "TIE":
            run_side, run_len = None, 0
        elif result == run_side:
            run_len += 1
        else:
            run_side, run_len = result, 1
        close_dt = parse_time(m["close_time"])
        start_ts = int(close_dt.timestamp()) - 15*60
        base.append({
            "ticker":m["ticker"],
            "close_time":m["close_time"],
            "session_start_ts":start_ts,
            "session_start_utc":iso(datetime.fromtimestamp(start_ts,tz=timezone.utc)),
            "result":result,
            "streak_here":run_len,
        })
    by = {x["ticker"]:x for x in base}
    out = []
    for i in range(1, len(markets)-1):
        cur = by.get(markets[i]["ticker"])
        prev = by.get(markets[i-1]["ticker"])
        if not cur or not prev or prev["result"] not in ("YES","NO"):
            continue
        out.append({
            **cur,
            "prior_result":prev["result"],
            "prior_streak":prev["streak_here"],
            "reversal_side":"NO" if prev["result"]=="YES" else "YES",
            "actual_reversal": int(cur["result"] in ("YES","NO") and cur["result"] != prev["result"]),
        })
    return out

def v(d, key):
    if not isinstance(d, dict):
        return None
    for k in (key+"_dollars", key):
        if d.get(k) is not None:
            try:
                return float(d[k])
            except:
                return None
    return None

def fetch_candles(ticker, start_ts, end_ts):
    params = {"start_ts":start_ts, "end_ts":end_ts, "period_interval":1}
    paths = [
        f"/series/{SERIES}/markets/{ticker}/candlesticks",
        f"/historical/markets/{ticker}/candlesticks",
    ]
    for p in paths:
        try:
            d = get_json(p, params, tries=4)
            cs = d.get("candlesticks", [])
            if cs:
                return cs
        except Exception:
            pass
    return []

def convert_candle(s, c):
    end_ts = int(c["end_period_ts"])
    minute = int(math.ceil((end_ts - s["session_start_ts"])/60.0))
    yb = c.get("yes_bid") or {}
    ya = c.get("yes_ask") or {}
    yb_low, yb_high, yb_close = v(yb,"low"), v(yb,"high"), v(yb,"close")
    ya_low, ya_high, ya_close = v(ya,"low"), v(ya,"high"), v(ya,"close")

    if s["reversal_side"] == "YES":
        ask_low = ya_low
        bid_low = yb_low
        bid_high = yb_high
        bid_close = yb_close
    else:
        # NO ask = 1 - YES bid
        ask_low = None if yb_high is None else 1-yb_high
        # NO bid = 1 - YES ask
        # lowest NO bid occurs when YES ask is highest
        bid_low = None if ya_high is None else 1-ya_high
        # highest NO bid occurs when YES ask is lowest
        bid_high = None if ya_low is None else 1-ya_low
        bid_close = None if ya_close is None else 1-ya_close

    cents = lambda x: None if x is None else round(x*100.0,4)
    return {
        "minute":minute,
        "end_ts":end_ts,
        "ask_low":cents(ask_low),
        "bid_low":cents(bid_low),
        "bid_high":cents(bid_high),
        "bid_close":cents(bid_close),
    }

def write_csv(path, rows):
    if not rows:
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def choose_stream(streak):
    # one stream per exact streak
    for s in STREAMS:
        if s["streak"] == streak:
            return s
    return None

def simulate_trade(session, rows, stream, stop_level, target_level, same_candle_policy):
    entry = stream["entry"]
    window = stream["window"]

    touch_idx = None
    for i,r in enumerate(rows):
        if r["minute"] > window:
            break
        if r["ask_low"] is not None and r["ask_low"] <= entry:
            touch_idx = i
            break
    if touch_idx is None:
        return None

    # Conservative fill at configured limit.
    entry_price = entry

    # Exit checks begin with the entry candle itself because after a fill sometime
    # within that 1-minute candle, the exit level may also have been touched.
    # Exact within-candle ordering is unknowable from 1-minute OHLC.
    for r in rows[touch_idx:]:
        hit_stop = bool(stop_level and r["bid_low"] is not None and r["bid_low"] <= stop_level)
        hit_target = bool(target_level < 100 and r["bid_high"] is not None and r["bid_high"] >= target_level)

        if hit_stop and hit_target:
            if same_candle_policy == "stop_first":
                exit_price, reason = stop_level, "STOP"
            else:
                exit_price, reason = target_level, "TARGET"
            return entry_price, exit_price, reason, r["minute"]

        if hit_stop:
            return entry_price, stop_level, "STOP", r["minute"]
        if hit_target:
            return entry_price, target_level, "TARGET", r["minute"]

    # Held to close. Settlement proxy from successive strikes.
    exit_price = 100 if session["actual_reversal"] else 0
    return entry_price, exit_price, "CLOSE", 15

def main():
    print("KALSHI UNIVERSAL EXIT BACKTEST")
    print("Streams:")
    for s in STREAMS:
        print(f"  {s['name']}: S{s['streak']} <= {s['entry']}c / {s['window']}m")
    print("Stops:", STOP_LEVELS)
    print("Targets:", TARGET_LEVELS)
    print()

    markets = fetch_markets()
    sessions = derive_sessions(markets)
    relevant = [s for s in sessions if choose_stream(s["prior_streak"]) is not None]
    print(f"Relevant sessions: {len(relevant):,}")

    candle_map = {}
    raw_count = 0
    for i,s in enumerate(relevant,1):
        cs = fetch_candles(s["ticker"], s["session_start_ts"], s["session_start_ts"]+15*60)
        rows = [convert_candle(s,c) for c in cs]
        rows = [r for r in rows if 1 <= r["minute"] <= 15]
        rows.sort(key=lambda r:(r["minute"],r["end_ts"]))
        candle_map[s["ticker"]] = rows
        raw_count += len(rows)
        if i % 100 == 0 or i == len(relevant):
            print(f"Candles {i:,}/{len(relevant):,}; rows={raw_count:,}", flush=True)
        time.sleep(0.02)

    result_rows = []
    detail_rows = []

    for policy in ("stop_first","target_first"):
        for stop in STOP_LEVELS:
            for target in TARGET_LEVELS:
                total_n = wins = 0
                pnl = 0.0
                stops = targets = closes = 0

                per_stream = {s["name"]: {"n":0,"pnl":0.0} for s in STREAMS}

                for sess in relevant:
                    stream = choose_stream(sess["prior_streak"])
                    rows = candle_map.get(sess["ticker"], [])
                    sim = simulate_trade(sess, rows, stream, stop, target, policy)
                    if sim is None:
                        continue
                    entry, exitp, reason, exit_min = sim
                    trade_pnl = exitp - entry
                    pnl += trade_pnl
                    total_n += 1
                    if trade_pnl > 0:
                        wins += 1
                    if reason == "STOP":
                        stops += 1
                    elif reason == "TARGET":
                        targets += 1
                    else:
                        closes += 1

                    per_stream[stream["name"]]["n"] += 1
                    per_stream[stream["name"]]["pnl"] += trade_pnl

                    detail_rows.append({
                        "same_candle_policy":policy,
                        "stop_cents":stop,
                        "target_cents":target,
                        "stream":stream["name"],
                        "streak":stream["streak"],
                        "entry_limit_cents":entry,
                        "entry_window_min":stream["window"],
                        "ticker":sess["ticker"],
                        "prior_result":sess["prior_result"],
                        "reversal_side":sess["reversal_side"],
                        "actual_reversal":sess["actual_reversal"],
                        "exit_reason":reason,
                        "exit_minute":exit_min,
                        "exit_price_cents":exitp,
                        "pnl_cents":trade_pnl,
                    })

                row = {
                    "same_candle_policy":policy,
                    "stop_cents":stop,
                    "target_cents":target,
                    "trades":total_n,
                    "profitable_trades":wins,
                    "profitable_trade_pct":round(100*wins/total_n,3) if total_n else "",
                    "stop_exits":stops,
                    "target_exits":targets,
                    "close_exits":closes,
                    "total_pnl_cents":round(pnl,2),
                    "avg_pnl_per_trade_cents":round(pnl/total_n,3) if total_n else "",
                }
                for s in STREAMS:
                    d = per_stream[s["name"]]
                    row[f"{s['name']}_trades"] = d["n"]
                    row[f"{s['name']}_avg_pnl_cents"] = round(d["pnl"]/d["n"],3) if d["n"] else ""
                result_rows.append(row)

    # Rank within each same-candle assumption.
    result_rows.sort(key=lambda r: (r["same_candle_policy"], -(r["avg_pnl_per_trade_cents"] if isinstance(r["avg_pnl_per_trade_cents"],(int,float)) else -999)))
    write_csv(OUT_DIR/"exit_grid_results.csv", result_rows)
    write_csv(OUT_DIR/"exit_trade_details.csv", detail_rows)

    summary = {
        "series":SERIES,
        "streams":STREAMS,
        "stop_levels":STOP_LEVELS,
        "target_levels":TARGET_LEVELS,
        "relevant_sessions":len(relevant),
        "candle_rows":raw_count,
        "notes":[
            "Kalshi-only public data.",
            "Entry fill = 1-minute ask low touch, filled at configured limit.",
            "Stop uses reversal bid LOW; target uses reversal bid HIGH.",
            "1-minute candles cannot identify same-candle stop/target ordering.",
            "Results are reported under both stop-first and target-first assumptions.",
            "Fees/slippage excluded."
        ],
    }
    (OUT_DIR/"run_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")

    print("\nDONE")
    print(f"Result rows: {len(result_rows):,}")
    print(f"Trade detail rows: {len(detail_rows):,}")
    print(f"Saved to: {OUT_DIR.resolve()}")
    print("\nTop 10 conservative (stop_first):")
    cons = [r for r in result_rows if r["same_candle_policy"]=="stop_first"]
    cons.sort(key=lambda r:r["avg_pnl_per_trade_cents"] if isinstance(r["avg_pnl_per_trade_cents"],(int,float)) else -999, reverse=True)
    for r in cons[:10]:
        print(f"stop={r['stop_cents']} target={r['target_cents']} n={r['trades']} avg={r['avg_pnl_per_trade_cents']}c total={r['total_pnl_cents']}c")

if __name__ == '__main__':
    main()
