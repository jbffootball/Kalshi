#!/usr/bin/env python3
"""
Kalshi KXBTC15M 10,000-market non-overlapping validation backtest.

Purpose
-------
Pull the 10,000 KXBTC15M markets immediately BEFORE the most-recent
5,000-market discovery block and evaluate the full practical entry grid.

Kalshi-only public data. No external BTC source. No trading.

Outcome rule
------------
Successive Kalshi strikes define each completed 15-minute session:
    next strike > current strike  -> YES
    next strike < current strike  -> NO
    equal                         -> TIE

Reversal trade
--------------
If the prior completed session/streak ended YES, test buying NO.
If it ended NO, test buying YES.

Grid
----
Streaks: 2, 3, 4, 5
Entry limits: 20c through 60c
Entry windows: 2 through 6 minutes
Exit: hold to close
Stop: OFF
Sell early: OFF

Outputs
-------
/data/kalshi_validation_10000/
    sessions_with_streaks.csv
    candles_1min.csv                  # appended incrementally; survives restart
    candle_progress.csv                 # one row per processed market
    grid_results.csv
    grid_ranked_by_avg_pnl.csv
    grid_ranked_min100_trades.csv
    grid_ranked_min250_trades.csv
    daily_trade_results.csv
    monthly_strategy_summary.csv
    run_summary.json
"""

import csv
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("SERIES_TICKER", "KXBTC15M")

# IMPORTANT: skip the newest 5,000-market discovery block, then take 10,000 older markets.
MARKET_OFFSET = int(os.getenv("MARKET_OFFSET", "5000"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "10000"))

MIN_STREAK = int(os.getenv("MIN_STREAK", "2"))
MAX_STREAK = int(os.getenv("MAX_STREAK", "5"))
MIN_ENTRY_CENTS = int(os.getenv("MIN_ENTRY_CENTS", "20"))
MAX_ENTRY_CENTS = int(os.getenv("MAX_ENTRY_CENTS", "60"))
MIN_WINDOW_MIN = int(os.getenv("MIN_WINDOW_MIN", "2"))
MAX_WINDOW_MIN = int(os.getenv("MAX_WINDOW_MIN", "6"))

OUT_DIR = Path(os.getenv("OUT_DIR", "/data/kalshi_validation_10000"))
if not OUT_DIR.parent.exists():
    OUT_DIR = Path("./kalshi_validation_10000")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Progress is written to the Railway volume after every market so an
# interruption/redeploy can resume instead of restarting candle collection.
CANDLE_FILE = OUT_DIR / "candles_1min.csv"
PROGRESS_FILE = OUT_DIR / "candle_progress.csv"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-public-validation/1.0"})

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)

def get_json(path: str, params: Optional[dict] = None, tries: int = 8) -> dict:
    url = BASE + path
    delay = 1.0
    last = None
    for _ in range(tries):
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

def fetch_market_pages(path: str) -> List[dict]:
    out = []
    cursor = None
    while True:
        params = {"limit": 1000, "series_ticker": SERIES}
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

def fetch_selected_markets() -> List[dict]:
    by_ticker = {}
    for m in fetch_market_pages("/historical/markets") + fetch_market_pages("/markets"):
        t = m.get("ticker")
        if t:
            by_ticker[t] = m

    rows = [
        m for m in by_ticker.values()
        if m.get("close_time") and m.get("floor_strike") is not None
    ]
    rows.sort(key=lambda m: parse_time(m["close_time"]))

    # Need MAX_MARKETS + 1 strike rows so successive strikes can derive outcomes.
    end = len(rows) - MARKET_OFFSET
    start = max(0, end - (MAX_MARKETS + 1))
    selected = rows[start:end]

    if len(selected) < 100:
        raise RuntimeError(
            f"Not enough markets after applying offset={MARKET_OFFSET}. "
            f"Found only {len(selected)} selected strike rows."
        )
    return selected

def derive_sessions(markets: List[dict]) -> List[dict]:
    base = []
    running_side = None
    running_len = 0

    for i in range(len(markets) - 1):
        m, nxt = markets[i], markets[i + 1]
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
        start_ts = int(close_dt.timestamp()) - 15 * 60

        base.append({
            "ticker": m["ticker"],
            "close_time": m["close_time"],
            "session_start_ts": start_ts,
            "session_start_utc": iso(datetime.fromtimestamp(start_ts, tz=timezone.utc)),
            "strike": strike,
            "next_strike": next_strike,
            "result": result,
            "streak_ending_here": running_len,
            "streak_side_ending_here": running_side or "",
        })

    by_ticker = {x["ticker"]: x for x in base}
    enriched = []

    # Need a prior completed session to know the streak entering the current market.
    for i in range(1, len(markets) - 1):
        cur = by_ticker.get(markets[i]["ticker"])
        prev = by_ticker.get(markets[i - 1]["ticker"])
        if not cur or not prev:
            continue

        reversal_side = ""
        if prev["result"] == "YES":
            reversal_side = "NO"
        elif prev["result"] == "NO":
            reversal_side = "YES"

        enriched.append({
            **cur,
            "prior_result": prev["result"],
            "prior_streak": prev["streak_ending_here"],
            "prior_streak_side": prev["streak_side_ending_here"],
            "reversal_side": reversal_side,
            "actual_reversal": int(
                prev["result"] in ("YES", "NO")
                and cur["result"] in ("YES", "NO")
                and prev["result"] != cur["result"]
            ),
        })

    return enriched

def val(d: Optional[dict], key: str) -> Optional[float]:
    if not isinstance(d, dict):
        return None
    for k in (key + "_dollars", key):
        x = d.get(k)
        if x is not None:
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
    return None

def fetch_candles(ticker: str, start_ts: int, end_ts: int) -> List[dict]:
    params = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": 1,
    }

    # Try both public candle locations. One will cover active/recent, the other archived.
    paths = [
        f"/series/{SERIES}/markets/{ticker}/candlesticks",
        f"/historical/markets/{ticker}/candlesticks",
    ]

    for path in paths:
        try:
            d = get_json(path, params, tries=4)
            cs = d.get("candlesticks", [])
            if cs:
                return cs
        except Exception:
            pass

    print(f"WARNING candles unavailable {ticker}", flush=True)
    return []

def candle_row(session: dict, c: dict) -> dict:
    end_ts = int(c["end_period_ts"])
    minute = int(math.ceil((end_ts - session["session_start_ts"]) / 60.0))

    yb = c.get("yes_bid") or {}
    ya = c.get("yes_ask") or {}

    yes_ask_low = val(ya, "low")
    yes_ask_close = val(ya, "close")
    yes_bid_high = val(yb, "high")
    yes_bid_close = val(yb, "close")

    if session["reversal_side"] == "YES":
        rev_ask_low = yes_ask_low
        rev_ask_close = yes_ask_close
    else:
        # NO ask = 1 - YES bid.
        # Lowest NO ask in candle uses highest YES bid.
        rev_ask_low = None if yes_bid_high is None else 1.0 - yes_bid_high
        rev_ask_close = None if yes_bid_close is None else 1.0 - yes_bid_close

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

def append_csv_rows(path: Path, rows: List[dict]):
    """Append rows immediately, writing the header only for a new file."""
    if not rows:
        return
    new_file = not path.exists() or path.stat().st_size == 0
    fields = list(rows[0].keys())
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def load_existing_candles() -> Tuple[Dict[str, List[dict]], List[dict]]:
    """Load already checkpointed candle rows from a prior interrupted run."""
    by_ticker = defaultdict(list)
    rows = []
    if not CANDLE_FILE.exists() or CANDLE_FILE.stat().st_size == 0:
        return dict(by_ticker), rows

    with CANDLE_FILE.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Restore numeric fields needed by the grid.
            for key in (
                "prior_streak", "actual_reversal", "minute", "candle_end_ts",
                "yes_ask_low_cents", "yes_ask_close_cents",
                "yes_bid_high_cents", "yes_bid_close_cents",
                "reversal_ask_low_cents", "reversal_ask_close_cents",
            ):
                if row.get(key) not in (None, ""):
                    try:
                        row[key] = float(row[key])
                    except ValueError:
                        pass
            if isinstance(row.get("prior_streak"), float):
                row["prior_streak"] = int(row["prior_streak"])
            if isinstance(row.get("actual_reversal"), float):
                row["actual_reversal"] = int(row["actual_reversal"])
            if isinstance(row.get("minute"), float):
                row["minute"] = int(row["minute"])
            if isinstance(row.get("candle_end_ts"), float):
                row["candle_end_ts"] = int(row["candle_end_ts"])

            ticker = row.get("ticker")
            if ticker:
                by_ticker[ticker].append(row)
            rows.append(row)

    return dict(by_ticker), rows


def load_processed_tickers() -> set:
    """Tickers in this file are complete, even if Kalshi returned zero candles."""
    done = set()
    if not PROGRESS_FILE.exists() or PROGRESS_FILE.stat().st_size == 0:
        return done
    with PROGRESS_FILE.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = row.get("ticker")
            if ticker:
                done.add(ticker)
    return done


def checkpoint_market(ticker: str, candle_count: int):
    append_csv_rows(
        PROGRESS_FILE,
        [{
            "ticker": ticker,
            "candle_count": candle_count,
            "processed_time_utc": iso(datetime.now(timezone.utc)),
        }],
    )


def run_grid(
    sessions: List[dict],
    candles_by_ticker: Dict[str, List[dict]]
) -> Tuple[List[dict], List[dict]]:
    grid = []
    trade_details = []

    eligible = [
        s for s in sessions
        if MIN_STREAK <= s["prior_streak"] <= MAX_STREAK
        and s["reversal_side"]
    ]
    print(
        f"Eligible streak {MIN_STREAK}-{MAX_STREAK} markets: {len(eligible):,}",
        flush=True
    )

    usable = {}
    for s in eligible:
        rows = [
            r for r in candles_by_ticker.get(s["ticker"], [])
            if 1 <= r["minute"] <= 15
            and r["reversal_ask_low_cents"] is not None
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

                    win = bool(s["actual_reversal"])
                    trade_pnl = (100 - entry) if win else -entry

                    n += 1
                    wins += int(win)
                    losses += int(not win)
                    pnl += trade_pnl

                    trade_details.append({
                        "streak": streak,
                        "entry_cents": entry,
                        "window_minutes": window,
                        "ticker": s["ticker"],
                        "session_start_utc": s["session_start_utc"],
                        "date_utc": s["session_start_utc"][:10],
                        "month_utc": s["session_start_utc"][:7],
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
                    "edge_vs_break_even_pct_points": round(
                        (100.0 * wins / n) - entry, 3
                    ) if n else "",
                    "total_pnl_cents": round(pnl, 2),
                    "avg_pnl_per_trade_cents": round(pnl / n, 3) if n else "",
                })

    return grid, trade_details

def build_daily_and_monthly(trades: List[dict]):
    # These rows summarize each grid cell by month.
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    daily = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

    for t in trades:
        key_base = (t["streak"], t["entry_cents"], t["window_minutes"])

        mk = key_base + (t["month_utc"],)
        monthly[mk]["trades"] += 1
        monthly[mk]["wins"] += t["actual_reversal"]
        monthly[mk]["pnl"] += t["pnl_cents"]

        dk = key_base + (t["date_utc"],)
        daily[dk]["trades"] += 1
        daily[dk]["wins"] += t["actual_reversal"]
        daily[dk]["pnl"] += t["pnl_cents"]

    monthly_rows = []
    for (streak, entry, window, month), d in monthly.items():
        monthly_rows.append({
            "streak": streak,
            "entry_cents": entry,
            "window_minutes": window,
            "month_utc": month,
            "trades": d["trades"],
            "wins": d["wins"],
            "win_rate_pct": round(100 * d["wins"] / d["trades"], 3),
            "total_pnl_cents": round(d["pnl"], 2),
            "avg_pnl_per_trade_cents": round(d["pnl"] / d["trades"], 3),
        })

    daily_rows = []
    for (streak, entry, window, day), d in daily.items():
        daily_rows.append({
            "streak": streak,
            "entry_cents": entry,
            "window_minutes": window,
            "date_utc": day,
            "trades": d["trades"],
            "wins": d["wins"],
            "win_rate_pct": round(100 * d["wins"] / d["trades"], 3),
            "total_pnl_cents": round(d["pnl"], 2),
            "avg_pnl_per_trade_cents": round(d["pnl"] / d["trades"], 3),
        })

    return daily_rows, monthly_rows

def main():
    print("KALSHI KXBTC15M 10,000-MARKET VALIDATION BACKTEST")
    print(f"series={SERIES}")
    print(f"skip newest={MARKET_OFFSET:,} markets")
    print(f"validation block={MAX_MARKETS:,} older markets")
    print(
        f"streaks={MIN_STREAK}-{MAX_STREAK} "
        f"entries={MIN_ENTRY_CENTS}-{MAX_ENTRY_CENTS}c "
        f"windows={MIN_WINDOW_MIN}-{MAX_WINDOW_MIN}m"
    )
    print("exit=HOLD TO CLOSE; stop=OFF; sell early=OFF")
    print(f"output={OUT_DIR.resolve()}")
    print()

    markets = fetch_selected_markets()

    print(
        f"Selected {len(markets):,} strike rows spanning "
        f"{markets[0]['close_time']} -> {markets[-1]['close_time']}"
    )

    sessions = derive_sessions(markets)
    print(f"Derived {len(sessions):,} tradable sessions")
    write_csv(OUT_DIR / "sessions_with_streaks.csv", sessions)

    # RESUME SUPPORT ---------------------------------------------------------
    # Load checkpointed data from the Railway volume first.
    candles_by_ticker, all_candle_rows = load_existing_candles()
    processed_tickers = load_processed_tickers()

    # Backward-compatible recovery: if candles exist but the progress file was
    # lost, treat candle-bearing tickers as processed.
    processed_tickers.update(candles_by_ticker.keys())

    total = len(sessions)
    already_done = sum(1 for s in sessions if s["ticker"] in processed_tickers)
    if already_done:
        print(
            f"RESUME: found {already_done:,}/{total:,} already processed markets "
            f"and {len(all_candle_rows):,} saved candle rows.",
            flush=True,
        )
    else:
        print("RESUME: no prior candle checkpoint found; starting candle collection.")

    newly_processed = 0

    for idx, s in enumerate(sessions, 1):
        ticker = s["ticker"]

        if ticker in processed_tickers:
            if idx % 100 == 0 or idx == total:
                print(
                    f"Candles: {idx:,}/{total:,} markets scanned; "
                    f"{len(all_candle_rows):,} rows saved "
                    f"(resume skipped {already_done:,})",
                    flush=True,
                )
            continue

        cs = fetch_candles(
            ticker,
            s["session_start_ts"],
            s["session_start_ts"] + 15 * 60
        )
        rows = [candle_row(s, c) for c in cs]
        rows = [r for r in rows if 1 <= r["minute"] <= 15]

        # Write the candle rows FIRST, then mark the ticker complete.
        # If the container dies between these two writes, a few duplicate rows
        # are possible on restart; they are deduplicated below before analysis.
        if rows:
            append_csv_rows(CANDLE_FILE, rows)

        checkpoint_market(ticker, len(rows))
        processed_tickers.add(ticker)
        candles_by_ticker[ticker] = rows
        all_candle_rows.extend(rows)
        newly_processed += 1

        if idx % 100 == 0 or idx == total:
            print(
                f"Candles: {idx:,}/{total:,} markets scanned; "
                f"{len(all_candle_rows):,} rows saved; "
                f"new this run={newly_processed:,}",
                flush=True
            )

        time.sleep(0.02)

    # Deduplicate in memory in case a restart occurred between candle append
    # and progress checkpoint for one market.
    deduped = {}
    for row in all_candle_rows:
        key = (row.get("ticker"), row.get("candle_end_ts"))
        deduped[key] = row
    all_candle_rows = list(deduped.values())
    all_candle_rows.sort(
        key=lambda r: (r.get("ticker", ""), int(r.get("candle_end_ts") or 0))
    )

    candles_by_ticker = defaultdict(list)
    for row in all_candle_rows:
        candles_by_ticker[row["ticker"]].append(row)
    candles_by_ticker = dict(candles_by_ticker)

    grid, trades = run_grid(sessions, candles_by_ticker)
    write_csv(OUT_DIR / "grid_results.csv", grid)

    ranked = [
        r for r in grid
        if isinstance(r["avg_pnl_per_trade_cents"], (int, float))
    ]
    ranked.sort(
        key=lambda r: (
            r["avg_pnl_per_trade_cents"],
            r["trades"],
            r["total_pnl_cents"]
        ),
        reverse=True
    )

    write_csv(OUT_DIR / "grid_ranked_by_avg_pnl.csv", ranked)
    write_csv(
        OUT_DIR / "grid_ranked_min100_trades.csv",
        [r for r in ranked if r["trades"] >= 100]
    )
    write_csv(
        OUT_DIR / "grid_ranked_min250_trades.csv",
        [r for r in ranked if r["trades"] >= 250]
    )

    daily_rows, monthly_rows = build_daily_and_monthly(trades)
    write_csv(OUT_DIR / "daily_trade_results.csv", daily_rows)
    write_csv(OUT_DIR / "monthly_strategy_summary.csv", monthly_rows)

    summary = {
        "series": SERIES,
        "market_offset": MARKET_OFFSET,
        "requested_validation_markets": MAX_MARKETS,
        "selected_strike_rows": len(markets),
        "derived_sessions": len(sessions),
        "candle_rows": len(all_candle_rows),
        "candle_progress_markets": len(processed_tickers),
        "date_start": sessions[0]["session_start_utc"] if sessions else "",
        "date_end": sessions[-1]["close_time"] if sessions else "",
        "streak_range": [MIN_STREAK, MAX_STREAK],
        "entry_cents_range": [MIN_ENTRY_CENTS, MAX_ENTRY_CENTS],
        "window_minutes_range": [MIN_WINDOW_MIN, MAX_WINDOW_MIN],
        "grid_cells": len(grid),
        "exit_rule": "hold_to_close",
        "stop_loss": "off",
        "sell_early": "off",
        "top_20_min250_trades": [
            r for r in ranked if r["trades"] >= 250
        ][:20],
        "notes": "Kalshi-only; successive strikes; hold to close; no stop/early sell; checkpointed resume enabled."
    }

    (OUT_DIR / "run_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8"
    )

    print("\nDONE")
    print(f"1-minute candle rows: {len(all_candle_rows):,}")
    print(f"Grid cells: {len(grid):,}")
    print(f"Saved to: {OUT_DIR.resolve()}")

    robust = [r for r in ranked if r["trades"] >= 250]
    if robust:
        print("\nTop 15 with >=250 trades:")
        for r in robust[:15]:
            print(
                f"s{r['streak']} {r['entry_cents']}c/{r['window_minutes']}m "
                f"n={r['trades']} win={r['win_rate_pct']}% "
                f"avg={r['avg_pnl_per_trade_cents']}c "
                f"total={r['total_pnl_cents']}c"
            )

if __name__ == "__main__":
    main()
