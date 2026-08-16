#!/usr/bin/env python3
import csv, json
from collections import defaultdict
from pathlib import Path

DATASETS = [
    ("first_5k", Path("/data/kalshi_reversal_backtest")),
    ("new_10k", Path("/data/kalshi_validation_10000")),
]
STREAMS = [
    {"name":"S2","streak":2,"entry_cents":39,"window_minutes":2},
    {"name":"S3","streak":3,"entry_cents":27,"window_minutes":2},
    {"name":"S4","streak":4,"entry_cents":27,"window_minutes":5},
    {"name":"S5","streak":5,"entry_cents":28,"window_minutes":3},
]
TARGETS = [100,70,75,80,85,90]
OUT_DIR = Path("/data/kalshi_early_sell_validation")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def fnum(v):
    try:
        return None if v in (None,"") else float(v)
    except Exception:
        return None

def inum(v):
    x=fnum(v)
    return None if x is None else int(x)

def write_csv(path, rows):
    if not rows: return
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys()),extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

def load_candles(path):
    rows=[]
    with path.open("r",newline="",encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in ("prior_streak","actual_reversal","minute","candle_end_ts"):
                r[k]=inum(r.get(k))
            for k in ("yes_ask_low_cents","yes_ask_close_cents","yes_bid_high_cents",
                      "yes_bid_close_cents","reversal_ask_low_cents","reversal_ask_close_cents"):
                r[k]=fnum(r.get(k))
            rows.append(r)
    return rows

def reversal_bid_high(r):
    side=(r.get("reversal_side") or "").upper()
    if side=="YES":
        return r.get("yes_bid_high_cents")
    if side=="NO":
        yal=r.get("yes_ask_low_cents")
        return None if yal is None else 100.0-yal
    return None

def evaluate_dataset(name, folder):
    p=folder/"candles_1min.csv"
    if not p.exists():
        print(f"SKIP {name}: missing {p}")
        return [],[]
    candles=load_candles(p)
    print(f"{name}: {len(candles):,} candle rows")

    by=defaultdict(list); meta={}
    for r in candles:
        t=r.get("ticker")
        if t:
            by[t].append(r); meta[t]=r
    for rs in by.values():
        rs.sort(key=lambda x:((x.get("minute") or 99),(x.get("candle_end_ts") or 0)))

    summary=[]; details=[]
    for s in STREAMS:
        entries=[]
        for t,m in meta.items():
            if m.get("prior_streak")!=s["streak"]: continue
            if (m.get("reversal_side") or "").upper() not in {"YES","NO"}: continue
            rs=by[t]
            touch_i=touch=None
            for i,r in enumerate(rs):
                minute=r.get("minute"); ask=r.get("reversal_ask_low_cents")
                if minute is None or ask is None: continue
                if minute>s["window_minutes"]: break
                if ask<=s["entry_cents"]:
                    touch_i=i; touch=r; break
            if touch is not None:
                entries.append((t,rs,touch_i,touch))

        for target in TARGETS:
            n=wins=early=close=0; pnl_sum=0.0
            for t,rs,touch_i,touch in entries:
                n+=1
                won=bool(touch.get("actual_reversal"))
                reason="held_to_close"
                exit_c=100.0 if won else 0.0
                exit_min=""
                if target<100:
                    for r in rs[touch_i:]:
                        bid_hi=reversal_bid_high(r)
                        if bid_hi is not None and bid_hi>=target:
                            reason="sell_early"; exit_c=float(target); exit_min=r.get("minute"); break
                pnl=exit_c-s["entry_cents"]
                pnl_sum+=pnl; wins+=int(pnl>0); early+=int(reason=="sell_early"); close+=int(reason=="held_to_close")
                details.append({
                    "dataset":name,"stream":s["name"],"streak":s["streak"],
                    "entry_cents":s["entry_cents"],"window_minutes":s["window_minutes"],
                    "target_cents":target,"ticker":t,"reversal_side":touch.get("reversal_side"),
                    "entry_touch_minute":touch.get("minute"),"exit_reason":reason,
                    "exit_minute":exit_min,"exit_cents":exit_c,"actual_reversal":int(won),"pnl_cents":pnl
                })
            summary.append({
                "dataset":name,"stream":s["name"],"streak":s["streak"],
                "entry_cents":s["entry_cents"],"window_minutes":s["window_minutes"],
                "target_cents":target,"exit_rule":"hold_to_close" if target==100 else f"sell_at_{target}c",
                "trades":n,"profitable_trades":wins,
                "profitable_trade_pct":round(100*wins/n,3) if n else "",
                "sell_early_exits":early,"held_to_close_exits":close,
                "total_pnl_cents":round(pnl_sum,2),
                "avg_pnl_per_trade_cents":round(pnl_sum/n,3) if n else ""
            })
    return summary,details

def combine_streams(rows):
    agg=defaultdict(lambda:{"n":0,"wins":0,"early":0,"close":0,"pnl":0.0})
    for r in rows:
        k=(r["stream"],r["streak"],r["entry_cents"],r["window_minutes"],r["target_cents"])
        d=agg[k]
        d["n"]+=int(r["trades"]); d["wins"]+=int(r["profitable_trades"])
        d["early"]+=int(r["sell_early_exits"]); d["close"]+=int(r["held_to_close_exits"])
        d["pnl"]+=float(r["total_pnl_cents"])
    out=[]
    for (stream,streak,entry,window,target),d in agg.items():
        n=d["n"]
        out.append({
            "stream":stream,"streak":streak,"entry_cents":entry,"window_minutes":window,
            "target_cents":target,"exit_rule":"hold_to_close" if target==100 else f"sell_at_{target}c",
            "combined_trades":n,"profitable_trades":d["wins"],
            "profitable_trade_pct":round(100*d["wins"]/n,3) if n else "",
            "sell_early_exits":d["early"],"held_to_close_exits":d["close"],
            "total_pnl_cents":round(d["pnl"],2),
            "avg_pnl_per_trade_cents":round(d["pnl"]/n,3) if n else ""
        })
    return out

def combine_portfolio(details):
    agg=defaultdict(lambda:{"n":0,"wins":0,"early":0,"close":0,"pnl":0.0})
    for r in details:
        t=int(r["target_cents"]); d=agg[t]
        d["n"]+=1; d["wins"]+=int(float(r["pnl_cents"])>0)
        d["early"]+=int(r["exit_reason"]=="sell_early")
        d["close"]+=int(r["exit_reason"]=="held_to_close")
        d["pnl"]+=float(r["pnl_cents"])
    out=[]
    for target,d in agg.items():
        n=d["n"]
        out.append({
            "target_cents":target,"exit_rule":"hold_to_close" if target==100 else f"sell_at_{target}c",
            "trades":n,"profitable_trades":d["wins"],
            "profitable_trade_pct":round(100*d["wins"]/n,3) if n else "",
            "sell_early_exits":d["early"],"held_to_close_exits":d["close"],
            "total_pnl_cents":round(d["pnl"],2),
            "avg_pnl_per_trade_cents":round(d["pnl"]/n,3) if n else ""
        })
    out.sort(key=lambda r:float(r["avg_pnl_per_trade_cents"] or -999),reverse=True)
    return out

def main():
    all_s=[]; all_d=[]; found=[]
    for name,folder in DATASETS:
        s,d=evaluate_dataset(name,folder)
        if s:
            found.append(name); all_s.extend(s); all_d.extend(d)
    if not all_s:
        raise RuntimeError("No saved candle datasets found.")
    combined=combine_streams(all_s)
    portfolio=combine_portfolio(all_d)

    write_csv(OUT_DIR/"early_sell_by_dataset_and_stream.csv",all_s)
    write_csv(OUT_DIR/"early_sell_combined_by_stream.csv",combined)
    write_csv(OUT_DIR/"early_sell_combined_all_streams.csv",portfolio)
    write_csv(OUT_DIR/"early_sell_trade_details.csv",all_d)

    (OUT_DIR/"run_summary.json").write_text(json.dumps({
        "datasets_found":found,"streams":STREAMS,"targets_tested":TARGETS,
        "stop_loss":"off","portfolio_ranking":portfolio
    },indent=2),encoding="utf-8")

    print("\nDONE")
    print("Datasets:",", ".join(found))
    print("Saved to:",OUT_DIR)
    print("\nALL FOUR STREAMS COMBINED:")
    for r in portfolio:
        print(f"{r['exit_rule']:>15} | n={r['trades']:4d} | avg={r['avg_pnl_per_trade_cents']:7.3f}c | total={r['total_pnl_cents']:9.1f}c")
    print("\nBEST EXIT BY STREAM:")
    for stream in [x["name"] for x in STREAMS]:
        rs=[r for r in combined if r["stream"]==stream]
        rs.sort(key=lambda r:float(r["avg_pnl_per_trade_cents"] or -999),reverse=True)
        if rs:
            r=rs[0]
            print(f"{stream}: {r['exit_rule']} | n={r['combined_trades']} | avg={r['avg_pnl_per_trade_cents']}c")

if __name__=="__main__":
    main()
