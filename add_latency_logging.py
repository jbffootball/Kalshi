#!/usr/bin/env python3
from pathlib import Path
import sys

target = Path(sys.argv[1] if len(sys.argv) > 1 else "main.py")
if not target.exists():
    raise SystemExit(f"Could not find {target}")

text = target.read_text(encoding="utf-8")
original = text

def replace_once(old, new, label):
    global text
    if old not in text:
        raise SystemExit(f"PATCH STOPPED: could not find expected section: {label}")
    text = text.replace(old, new, 1)

replace_once(
"""def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def seconds_to_next_15m_boundary(now=None):
""",
"""def utc_now():
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
""",
"latency helper insertion"
)

replace_once(
"""def authenticated_request(method, path, json_body=None):
    """Authenticated Kalshi request against the currently selected environment."""
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
""",
"""def authenticated_request(method, path, json_body=None):
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
""",
"authenticated_request timing"
)

replace_once(
"""    resp = authenticated_request("POST", "/portfolio/events/orders", json_body=payload)
    order = resp.get("order", resp)
    print(f"{MODE.upper()} RESTING LIMIT ORDER ACCEPTED: {order}")
    return order
""",
"""    resp = authenticated_request("POST", "/portfolio/events/orders", json_body=payload)
    order = resp.get("order", resp)
    latency_mark(
        "ORDER_ACCEPTED",
        detail=f"ticker={ticker} order_id={order.get('order_id','?')}",
    )
    print(f"{MODE.upper()} RESTING LIMIT ORDER ACCEPTED: {order}")
    return order
""",
"accepted marker"
)

replace_once(
"""    while True:
        try:
            # Build the immediate streak strictly from adjacent Kalshi strikes.
            immediate_markets = successive_strike_markets()
""",
"""    while True:
        try:
            _boundary_now = current_15m_session_start()
            latency_reset_if_new_boundary(_boundary_now)

            # Build the immediate streak strictly from adjacent Kalshi strikes.
            immediate_markets = successive_strike_markets()
""",
"main boundary marker"
)

replace_once(
"""            last_result, streak = calculate_streak(immediate_markets)
            print(f"CALCULATED IMMEDIATE: last={last_result} streak={streak}")
""",
"""            last_result, streak = calculate_streak(immediate_markets)
            print(f"CALCULATED IMMEDIATE: last={last_result} streak={streak}")

            _latency_target = current_15m_session_start()
            if latest_result_confirms_boundary(immediate_markets, _latency_target):
                _latest = immediate_markets[-1] if immediate_markets else {}
                latency_mark(
                    "NEW_STRIKE_SEEN",
                    boundary_dt=_latency_target,
                    detail=(
                        f"derived_close={_latest.get('close_time','?')} "
                        f"next_ticker={_latest.get('next_ticker','?')}"
                    ),
                )
                latency_mark(
                    "STREAK_READY",
                    boundary_dt=_latency_target,
                    detail=f"last={last_result} streak={streak}",
                )
""",
"strike marker"
)

replace_once(
"""            strategy = strategy_for_streak(streak)
            if strategy is None:
                print(f"NO SLOT MATCH: streak={streak}; no order.")
                sleep_idle()
                continue

            entry_window = strategy["entry_window_minutes"]
""",
"""            strategy = strategy_for_streak(streak)
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
""",
"slot marker"
)

replace_once(
"""            print(
                f"SIGNAL LOCKED SLOT {slot_name}: last={last_result} streak={streak} "
                f"buy={side.upper()} limit={limit_cents}c "
                f"target_start={target_start.isoformat()} "
                f"expires={dt.datetime.fromtimestamp(expiration_ts, dt.timezone.utc).isoformat()}"
            )
""",
"""            latency_mark(
                "SIGNAL_LOCKED",
                boundary_dt=target_start,
                detail=f"slot={slot_name} buy={side.upper()} limit={limit_cents}c",
            )
            print(
                f"SIGNAL LOCKED SLOT {slot_name}: last={last_result} streak={streak} "
                f"buy={side.upper()} limit={limit_cents}c "
                f"target_start={target_start.isoformat()} "
                f"expires={dt.datetime.fromtimestamp(expiration_ts, dt.timezone.utc).isoformat()}"
            )
""",
"signal marker"
)

replace_once(
"""                    print(
                        f"SIGNAL LOCK FOUND SLOT {slot_name}: {ticker} buy {side.upper()} "
                        f"{contracts:g} @ {limit_cents}c age={age:.2f}m "
                        f"latched_streak={trigger_streak}"
                    )

                    status, order = submit_resting_order_with_retry(
""",
"""                    latency_mark(
                        "MARKET_VISIBLE",
                        boundary_dt=pending_api_signal["target_start"],
                        detail=f"ticker={ticker} age={age:.3f}m",
                    )
                    print(
                        f"SIGNAL LOCK FOUND SLOT {slot_name}: {ticker} buy {side.upper()} "
                        f"{contracts:g} @ {limit_cents}c age={age:.2f}m "
                        f"latched_streak={trigger_streak}"
                    )

                    status, order = submit_resting_order_with_retry(
""",
"market visible marker"
)

replace_once(
"""                    print(
                        f"ORDER RESTING SLOT {slot_name}: {ticker} {side.upper()} "
                        f"@ {limit_cents}c expires={dt.datetime.fromtimestamp(expiration_ts, dt.timezone.utc).isoformat()}"
                    )
""",
"""                    latency_mark(
                        "ORDER_RESTING",
                        boundary_dt=current_15m_session_start(),
                        detail=f"ticker={ticker} order_id={order_id}",
                    )
                    print(
                        f"ORDER RESTING SLOT {slot_name}: {ticker} {side.upper()} "
                        f"@ {limit_cents}c expires={dt.datetime.fromtimestamp(expiration_ts, dt.timezone.utc).isoformat()}"
                    )
""",
"resting marker"
)

backup = target.with_name(target.name + ".pre_latency_backup")
backup.write_text(original, encoding="utf-8")
target.write_text(text, encoding="utf-8")
print(f"Patched: {target}")
print(f"Backup:  {backup}")
print("Only LATENCY logging was added.")
