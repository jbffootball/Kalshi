#!/usr/bin/env python3
"""
Kalshi KXBTC15M 1-minute candle downloader + reversal grid backtester.

Uses ONLY Kalshi public market data.
No external BTC feed. No Kalshi account credentials required.

Core outcome rule:
    successive Kalshi strikes determine the just-ended 15-minute session:
    next_strike > strike -> YES
    next_strike < strike -> NO
    equal -> TIE

For a new market:
    prior YES streak -> test buying NO
    prior NO streak  -> test buying YES

Backtest fill assumption:
    a resting limit order is considered touched when the 1-minute candle's
    reversal-side ask LOW <= configured limit.
    Fill price is conservatively recorded at the configured limit.
    Trade is held to the successive-strike outcome.
"""

import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("SERIES_TICKER", "KXBTC15M")

MAX_MARKETS = int(os.getenv("MAX_MARKETS", "5000"))
MIN_STREAK = int(os.getenv("MIN_STREAK", "2"))
MAX_STREAK = int(os.getenv("MAX_STREAK", "5"))
MIN_ENTRY_CENTS = int(os.getenv("MIN_ENTRY_CENTS", "20"))
MAX_ENTRY_CENTS = int(os.getenv("MAX_ENTRY_CENTS", "60"))
MIN_WINDOW_MIN = int(os.getenv("MIN_WINDOW_MIN", "1"))
MAX_WINDOW_MIN = int(os.getenv("MAX_WINDOW_MIN", "6"))

# If true, save 1-minute candles for every selected market (~15 rows/market).
# For 5,000 markets this is roughly 75,000 rows when Kalshi has all minutes.
FETCH_CANDLES_ALL_MARKETS = os.getenv("FETCH_CANDLES_ALL_MARKETS", "true").lower() in ("1","true","yes","y")

OUT_DIR = Path(os.getenv("OUT_DIR", "/data/kalshi_reversal_backtest"))
if not OUT_DIR.parent.exists():
    OUT_DIR = Path("./kalshi_reversal_backtest")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-public-research/1.0"})

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

def get_json(path: str, params: Optional[dict] = None, tries: int = 8) -> dict:
    url = BASE + path
    delay = 1.0
    last = None
    for attempt in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code}: {r.text[:300]}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise RuntimeError(f"GET {url} failed: {last}")
        except requests.RequestException as e:
            last = repr(e)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"GET {url} failed after retries: {last}")

def get_cutoff() -> Optional[int]:
    try:
        d = get_json("/historical/cutoff")
        # Docs use market_settled_ts. Be defensive about nesting/type.
        v = d.get("market_settled_ts")
        if v is None and isinstance(d.get("cutoff"), dict):
            v = d["cutoff"].get("market_settled_ts")
        if isinstance(v, str):
            if v.isdigit():
                return int(v)
            return int(parse_time(v).timestamp())
        return int(v) if v is not None else None
    except Exception:
        return None

def fetch_market_pages(path: str, series: str) -> List[dict]:
    out = []
    cursor = None
    while True:
        params = {"limit": 1000, "series_ticker": series}
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

def fetch_all_series_markets() -> List[dict]:
    # Historical endpoint contains archived markets; live endpoint contains current/recent.
    hist = fetch_market_pages("/historical/markets", SERIES)
    live = fetch_market_pages("/markets", SERIES)

    by_ticker = {}
    for m in hist + live:
        t = m.get("ticker")
        if t:
            by_ticker[t] = m

    rows = list(by_ticker.values())
    rows = [m for m in rows if m.get("close_time") and m.get("floor_strike") is not None]
    rows.sort(key=lambda m: parse_time(m["close_time"]))

    # Need one extra strike after the newest traded session to derive that session's result.
    if len(rows) > MAX_MARKETS + 1:
        rows = rows[-(MAX_MARKETS + 1):]
    return rows

def derive_sessions(markets: List[dict]) -> List[dict]:
    """
    Each market's strike is the start/reference price for that 15-minute session.
    Outcome for market i is determined by strike[i+1] vs strike[i].
    """
    sessions = []
    running_side = None
    running_len = 0

    for i in range(len(markets) - 1):
        m = markets[i]
        nxt = markets[i + 1]
        strike = float(m["floor_strike"])
        next_strike = float(nxt["floor_strike"])

        if next_strike > strike:
            result = "YES"
        elif next_strike < strike:
            result = "NO"
        else:
            result = "TIE"

        if result == "TIE":
            running_side = None
            running_len = 0
        elif result == running_side:
            running_len += 1
        else:
            running_side = result
            running_len = 1

        close_dt = parse_time(m["close_time"])
        start_dt = close_dt.timestamp() - 15 * 60

        sessions.append({
            "ticker": m["ticker"],
            "close_time": m["close_time"],
            "session_start_ts": int(start_dt),
            "session_start_utc": iso(datetime.fromtimestamp(start_dt, tz=timezone.utc)),
            "strike": strike,
            "next_strike": next_strike,
            "result": result,
            "streak_ending_here": running_len,
            "streak_side_ending_here": running_side or "",
            "settlement_ts": m.get("settlement_ts") or "",
        })

    # Attach PRIOR result/streak to each market to be traded.
    by_ticker = {s["ticker"]: s for s in sessions}
    enriched = []
    for i in range(1, len(markets) - 1):
        cur = markets[i]
        prev_s = by_ticker.get(markets[i-1]["ticker"])
        cur_s = by_ticker.get(cur["ticker"])
        if not prev_s or not cur_s:
            continue
        enriched.append({
            **cur_s,
            "prior_result": prev_s["result"],
            "prior_streak": prev_s["streak_ending_here"],
            "prior_streak_side": prev_s["streak_side_ending_here"],
            "reversal_side": "NO" if prev_s["result"] == "YES" else ("YES" if prev_s["result"] == "NO" else ""),
            "actual_reversal": int(
                prev_s["result"] in ("YES","NO")
                and cur_s["result"] in ("YES","NO")
                and prev_s["result"] != cur_s["result"]
            ),
        })
    return enriched

def val(d: Optional[dict], key: str) -> Optional[float]:
    if not isinstance(d, dict):
        return None
    # Current endpoint uses *_dollars; historical may use plain keys.
    for k in (key + "_dollars", key):
        x = d.get(k)
        if x is not None:
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
    return None

def fetch_candles(ticker: str, start_ts: int, end_ts: int, prefer_historical: bool) -> List[dict]:
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1}
    paths = (
        [f"/historical/markets/{ticker}/candlesticks",
         f"/series/{SERIES}/markets/{ticker}/candlesticks"]
        if prefer_historical else
        [f"/series/{SERIES}/markets/{ticker}/candlesticks",
         f"/historical/markets/{ticker}/candlesticks"]
    )
    last_err = None
    for path in paths:
        try:
            d = get_json(path, params, tries=4)
            cs = d.get("candlesticks", [])
            if cs:
                return cs
        except Exception as e:
            last_err = e
    if last_err:
        print(f"WARNING candles unavailable {ticker}: {last_err}", flush=True)
    return []

def candle_row(session: dict, c: dict) -> dict:
    end_ts = int(c.get("end_period_ts"))
    elapsed_sec = end_ts - session["session_start_ts"]
    minute = int(math.ceil(elapsed_sec / 60.0))

    yb = c.get("yes_bid") or {}
    ya = c.get("yes_ask") or {}

    yes_ask_low = val(ya, "low")
    yes_ask_close = val(ya, "close")
    yes_bid_high = val(yb, "high")
    yes_bid_close = val(yb, "close")

    if session["reversal_side"] == "YES":
        rev_ask_low = yes_ask_low
        rev_ask_close = yes_ask_close
        rev_bid_high = yes_bid_high
        rev_bid_close = yes_bid_close
    else:
        # NO ask = 1 - YES bid; for the LOWEST NO ask in a candle,
        # use the HIGHEST YES bid in that candle.
        rev_ask_low = None if yes_bid_high is None else 1.0 - yes_bid_high
        rev_ask_close = None if yes_bid_close is None else 1.0 - yes_bid_close
        # NO bid = 1 - YES ask; highest NO bid uses lowest YES ask.
        rev_bid_high = None if yes_ask_low is None else 1.0 - yes_ask_low
        rev_bid_close = None if yes_ask_close is None else 1.0 - yes_ask_close

    def cents(x):
        return None if x is None else round(x * 100.0, 4)

    return {
        "ticker": session["ticker"],
        "close_time": session["close_time"],
        "session_start_utc": session["session_start_utc"],
        "result": session["result"],
        "prior_result": session["prior_result"],
        "prior_streak": session["prior_streak"],
        "reversal_side": session["reversal_side"],
        "actual_reversal": session["actual_reversal"],
        "minute": minute,
        "candle_end_ts": end_ts,
        "yes_ask_low_cents": cents(yes_ask_low),
        "yes_ask_close_cents": cents(yes_ask_close),
        "yes_bid_high_cents": cents(yes_bid_high),
        "yes_bid_close_cents": cents(yes_bid_close),
        "reversal_ask_low_cents": cents(rev_ask_low),
        "reversal_ask_close_cents": cents(rev_ask_close),
        "reversal_bid_high_cents": cents(rev_bid_high),
        "reversal_bid_close_cents": cents(rev_bid_close),
        "volume": c.get("volume_fp", c.get("volume")),
        "open_interest": c.get("open_interest_fp", c.get("open_interest")),
    }

def write_csv(path: Path, rows: List[dict]):
    if not rows:
        print(f"No rows for {path.name}")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

def run_grid(sessions: List[dict], candles_by_ticker: Dict[str, List[dict]]) -> Tuple[List[dict], List[dict]]:
    grid = []
    trades = []

    eligible = [s for s in sessions if MIN_STREAK <= s["prior_streak"] <= MAX_STREAK and s["reversal_side"]]
    print(f"Eligible streak {MIN_STREAK}-{MAX_STREAK} markets: {len(eligible):,}", flush=True)

    # Pre-sort rows and keep only the 15-minute session.
    usable = {}
    for s in eligible:
        rows = [
            r for r in candles_by_ticker.get(s["ticker"], [])
            if 1 <= r["minute"] <= 15 and r["reversal_ask_low_cents"] is not None
        ]
        rows.sort(key=lambda r: (r["minute"], r["candle_end_ts"]))
        usable[s["ticker"]] = rows

    for streak in range(MIN_STREAK, MAX_STREAK + 1):
        ss = [s for s in eligible if s["prior_streak"] == streak]
        for entry in range(MIN_ENTRY_CENTS, MAX_ENTRY_CENTS + 1):
            for window in range(MIN_WINDOW_MIN, MAX_WINDOW_MIN + 1):
                n = wins = losses = 0
                pnl = 0.0
                for s in ss:
                    touch = None
                    for r in usable.get(s["ticker"], []):
                        if r["minute"] > window:
                            break
                        if r["reversal_ask_low_cents"] <= entry:
                            touch = r
                            break
                    if touch is None:
                        continue

                    n += 1
                    win = bool(s["actual_reversal"])
                    if win:
                        wins += 1
                        trade_pnl = 100 - entry
                    else:
                        losses += 1
                        trade_pnl = -entry
                    pnl += trade_pnl
                    trades.append({
                        "streak": streak,
                        "entry_cents": entry,
                        "window_minutes": window,
                        "ticker": s["ticker"],
                        "prior_result": s["prior_result"],
                        "reversal_side": s["reversal_side"],
                        "entry_touch_minute": touch["minute"],
                        "actual_reversal": int(win),
                        "pnl_cents": trade_pnl,
                    })

                grid.append({
                    "streak": streak,
                    "entry_cents": entry,
                    "window_minutes": window,
                    "trades": n,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": round(100.0 * wins / n, 3) if n else "",
                    "break_even_win_rate_pct": entry,
                    "edge_vs_break_even_pct_points": round((100.0 * wins / n) - entry, 3) if n else "",
                    "total_pnl_cents": round(pnl, 2),
                    "avg_pnl_per_trade_cents": round(pnl / n, 3) if n else "",
                })

    return grid, trades

def main():
    print("KALSHI KXBTC15M PUBLIC-DATA BACKTEST")
    print(f"series={SERIES} max_markets={MAX_MARKETS:,}")
    print(f"streaks={MIN_STREAK}-{MAX_STREAK} entries={MIN_ENTRY_CENTS}-{MAX_ENTRY_CENTS}c windows={MIN_WINDOW_MIN}-{MAX_WINDOW_MIN}m")
    print(f"output={OUT_DIR.resolve()}")
    print()

    cutoff = get_cutoff()
    print(f"historical market cutoff ts={cutoff}", flush=True)

    markets = fetch_all_series_markets()
    if len(markets) < 10:
        raise RuntimeError(f"Only found {len(markets)} {SERIES} markets; aborting.")

    print(f"Selected {len(markets):,} strike rows spanning {markets[0]['close_time']} -> {markets[-1]['close_time']}")
    sessions = derive_sessions(markets)
    print(f"Derived {len(sessions):,} tradable sessions")

    write_csv(OUT_DIR / "sessions_with_streaks.csv", sessions)

    # Fetch candles. Default is all selected sessions so we preserve the ~75k-row research table.
    targets = sessions if FETCH_CANDLES_ALL_MARKETS else [
        s for s in sessions if MIN_STREAK <= s["prior_streak"] <= MAX_STREAK
    ]

    candles_by_ticker = {}
    all_candle_rows = []
    total = len(targets)

    for idx, s in enumerate(targets, 1):
        # One extra minute on each side is harmless; we filter exact minute 1..15 later.
        start_ts = s["session_start_ts"]
        end_ts = start_ts + 15 * 60
        settlement_ts = s.get("settlement_ts")
        prefer_hist = False
        if cutoff and settlement_ts:
            try:
                st = parse_time(settlement_ts).timestamp() if not str(settlement_ts).isdigit() else int(settlement_ts)
                prefer_hist = st < cutoff
            except Exception:
                pass

        cs = fetch_candles(s["ticker"], start_ts, end_ts, prefer_hist)
        rows = [candle_row(s, c) for c in cs]
        rows = [r for r in rows if 1 <= r["minute"] <= 15]
        candles_by_ticker[s["ticker"]] = rows
        all_candle_rows.extend(rows)

        if idx % 100 == 0 or idx == total:
            print(f"Candles: {idx:,}/{total:,} markets; {len(all_candle_rows):,} rows", flush=True)

        # Small courtesy delay; 429 handling above adds exponential backoff if needed.
        time.sleep(0.02)

    write_csv(OUT_DIR / "candles_1min.csv", all_candle_rows)

    grid, trades = run_grid(sessions, candles_by_ticker)
    write_csv(OUT_DIR / "grid_results.csv", grid)
    write_csv(OUT_DIR / "trade_details_all_grid_cells.csv", trades)

    ranked = [r for r in grid if isinstance(r["avg_pnl_per_trade_cents"], (int,float))]
    ranked.sort(key=lambda r: (r["avg_pnl_per_trade_cents"], r["trades"], r["total_pnl_cents"]), reverse=True)
    write_csv(OUT_DIR / "grid_ranked_by_avg_pnl.csv", ranked)

    robust = [r for r in ranked if r["trades"] >= 100]
    write_csv(OUT_DIR / "grid_ranked_min100_trades.csv", robust)

    summary = {
        "series": SERIES,
        "selected_market_strike_rows": len(markets),
        "derived_sessions": len(sessions),
        "candle_rows": len(all_candle_rows),
        "candle_markets": len(candles_by_ticker),
        "date_start": sessions[0]["session_start_utc"] if sessions else "",
        "date_end": sessions[-1]["close_time"] if sessions else "",
        "streak_range": [MIN_STREAK, MAX_STREAK],
        "entry_cents_range": [MIN_ENTRY_CENTS, MAX_ENTRY_CENTS],
        "window_minutes_range": [MIN_WINDOW_MIN, MAX_WINDOW_MIN],
        "grid_cells": len(grid),
        "top_20_min100_trades": robust[:20],
        "notes": [
            "Kalshi-only public data.",
            "Outcome uses successive Kalshi strikes, not official settlement.",
            "Touch means 1-minute reversal ask low <= resting limit.",
            "Fill assumed at configured limit; fees/slippage not included.",
            "1-minute candles cannot resolve exact 30-second timing."
        ],
    }
    (OUT_DIR / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\nDONE")
    print(f"1-minute candle rows: {len(all_candle_rows):,}")
    print(f"Grid cells: {len(grid):,}")
    print(f"Files saved in: {OUT_DIR.resolve()}")
    if robust:
        print("\nTop 10 with >=100 trades:")
        for r in robust[:10]:
            print(
                f"s{r['streak']} {r['entry_cents']}c/{r['window_minutes']}m "
                f"n={r['trades']} win={r['win_rate_pct']}% "
                f"avg={r['avg_pnl_per_trade_cents']}c total={r['total_pnl_cents']}c"
            )

if __name__ == "__main__":
    main()
