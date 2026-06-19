#!/usr/bin/env python3
# =============================================================================
#  SPCX BACKTEST  —  validate the early-warning method on REAL historical IPOs
# -----------------------------------------------------------------------------
#  The SPCX thesis ("hyped, low-float IPO tops when locked supply finally hits")
#  is not testable on SPCX alone (n=1, weeks old). So we test the METHOD on
#  documented analogs that already played out: Beyond Meat, Meta/FB, Rivian,
#  Snowflake, Robinhood — each a hyped IPO with a known lockup schedule and a
#  subsequent top. We pull real daily OHLCV (Yahoo), walk the engine forward one
#  day at a time exactly as it would have run live, and measure:
#     • did the early-warning ladder (SCENT/WATCH/ARMED) light up BEFORE the
#       price peak (positive lead time)?
#     • what was the forward return after the first bearish flag (would a put
#       have worked)?
#     • how did it behave around the actual lockup-expiry date?
#
#  HONESTY: historical OHLCV gives price/volume/realized-vol/timing only. The
#  options-flow / borrow / skew leading signals CANNOT be backtested without that
#  data, so this validates the PRICE+VOLUME+VOLATILITY+CATALYST-CLOCK spine — the
#  part that determines the early WATCH/ARMED/SCENT escalation. Realized vol is
#  used as the IV proxy and is labelled as such.
#
#  RUN:  python spcx_backtest.py            # all analogs, table + report
#        python spcx_backtest.py BYND       # one analog, verbose day log
# =============================================================================
from __future__ import annotations
import sys, os, csv, json, math, time, bisect, urllib.request
from datetime import datetime, date, timedelta

import spcx_monitor as eng

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# -----------------------------------------------------------------------------
#  Analog catalog. Lockup dates are the documented expiries (primary 180-day and
#  notable early/secondary releases). %s are approximate where disclosed; the
#  evaluation anchors on the DATA-derived price peak, so small date error is ok.
# -----------------------------------------------------------------------------
ANALOGS = [
    {"sym": "BYND", "name": "Beyond Meat", "ipo": "2019-05-02", "ipo_px": 25,
     "start": "2019-05-02", "end": "2020-02-28",
     "lockups": [{"date": "2019-07-29", "label": "secondary", "pct": 8},
                 {"date": "2019-10-29", "label": "180d lockup", "pct": 80}]},
    {"sym": "META", "name": "Meta (Facebook)", "ipo": "2012-05-18", "ipo_px": 38,
     "start": "2012-05-18", "end": "2013-02-28",
     "lockups": [{"date": "2012-08-16", "label": "lockup wave 1", "pct": 14},
                 {"date": "2012-10-15", "label": "lockup wave 2", "pct": 13},
                 {"date": "2012-11-14", "label": "lockup wave 3", "pct": 40},
                 {"date": "2012-12-14", "label": "lockup wave 4", "pct": 8}]},
    {"sym": "RIVN", "name": "Rivian", "ipo": "2021-11-10", "ipo_px": 78,
     "start": "2021-11-10", "end": "2022-08-31",
     "lockups": [{"date": "2022-05-08", "label": "180d lockup", "pct": 85}]},
    {"sym": "SNOW", "name": "Snowflake", "ipo": "2020-09-16", "ipo_px": 120,
     "start": "2020-09-16", "end": "2021-06-30",
     "lockups": [{"date": "2020-12-14", "label": "early release", "pct": 25},
                 {"date": "2021-03-05", "label": "180d lockup", "pct": 60}]},
    {"sym": "HOOD", "name": "Robinhood", "ipo": "2021-07-29", "ipo_px": 38,
     "start": "2021-07-29", "end": "2022-03-31",
     "lockups": [{"date": "2021-08-25", "label": "early release", "pct": 15},
                 {"date": "2021-12-01", "label": "lockup expiry", "pct": 60}]},
]

# Broader, mixed-outcome analog set — IPO date / price / lock-up expiry verified
# by a research+verify pass (sec/news sources; de-SPACs & direct listings excluded).
# These remove the "famous crashers only" selection bias of the five above.
_VERIFIED = [
    ("ABNB", "Airbnb", "2020-12-10", 68, "2021-05-17", []),
    ("DASH", "DoorDash", "2020-12-09", 102, "2021-06-07", ["2021-03-09"]),
    ("U", "Unity", "2020-09-18", 52, "2021-03-17", []),
    ("PATH", "UiPath", "2021-04-21", 56, "2021-10-18", []),
    ("AFRM", "Affirm", "2021-01-13", 49, "2021-07-12", ["2021-03-03"]),
    ("CPNG", "Coupang", "2021-03-11", 35, "2021-09-07", ["2021-03-18"]),
    ("NET", "Cloudflare", "2019-09-13", 15, "2020-02-19", []),
    ("CRWD", "CrowdStrike", "2019-06-12", 34, "2019-12-09", []),
    ("ZM", "Zoom", "2019-04-18", 36, "2019-10-15", []),
    ("DDOG", "Datadog", "2019-09-19", 27, "2020-03-16", ["2019-12-10"]),
    ("ARM", "Arm Holdings", "2023-09-14", 51, "2024-03-12", []),
    ("CART", "Instacart", "2023-09-19", 30, "2024-02-15", []),
    ("KVYO", "Klaviyo", "2023-09-20", 30, "2024-02-29", []),
    ("CAVA", "CAVA Group", "2023-06-15", 22, "2023-12-12", []),
    ("RDDT", "Reddit", "2024-03-21", 34, "2024-09-17", ["2024-08-08"]),
    ("DUOL", "Duolingo", "2021-07-28", 102, "2022-01-26", ["2021-11-15"]),
    ("S", "SentinelOne", "2021-06-30", 35, "2021-12-09", ["2021-09-28"]),
    ("BMBL", "Bumble", "2021-02-11", 43, "2021-08-10", []),
    ("NU", "Nu Holdings", "2021-12-09", 9, "2022-05-17", []),
    ("TOST", "Toast", "2021-09-22", 40, "2022-03-21", ["2021-11-12"]),
    ("BIRK", "Birkenstock", "2023-10-11", 46, "2024-04-08", []),
    ("MNDY", "monday.com", "2021-06-10", 155, "2021-12-06", ["2021-09-07"]),
    ("CFLT", "Confluent", "2021-06-24", 36, "2021-12-21", []),
    ("FROG", "JFrog", "2020-09-16", 44, "2021-03-14", ["2020-11-25"]),
    ("GTLB", "GitLab", "2021-10-14", 77, "2022-04-12", []),
    ("LYFT", "Lyft", "2019-03-29", 72, "2019-08-19", []),
    ("UBER", "Uber", "2019-05-10", 45, "2019-11-06", []),
    ("PINS", "Pinterest", "2019-04-18", 19, "2019-10-15", []),
]


def _expand(sym, name, ipo, ipo_px, lockup, earlies, end_pad=90):
    lk = []
    for er in earlies:
        if er not in (ipo, lockup):                     # drop degenerate/dup dates
            lk.append({"date": er, "label": "early release", "pct": 20})
    lk.append({"date": lockup, "label": "180d lockup", "pct": 80})
    lk.sort(key=lambda e: e["date"])
    end = (datetime.strptime(lockup, "%Y-%m-%d") + timedelta(days=end_pad)).strftime("%Y-%m-%d")
    return {"sym": sym, "name": name, "ipo": ipo, "ipo_px": ipo_px,
            "start": ipo, "end": end, "lockups": lk}


ANALOGS = ANALOGS + [_expand(*r) for r in _VERIFIED]

# 2024-and-later IPOs — a FRESH out-of-sample cohort (the set above is mostly
# 2019-2023). Lockup anchored at the standard IPO + 180 calendar days; the actual
# first-trade date in the data confirms the IPO anchor. Run with: --recent
_RECENT_2024 = [
    ("ALAB", "Astera Labs", "2024-03-20", 36),
    ("AS",   "Amer Sports", "2024-02-01", 13),
    ("ULS",  "UL Solutions", "2024-04-12", 28),
    ("RBRK", "Rubrik", "2024-04-25", 32),
    ("IBTA", "Ibotta", "2024-04-18", 88),
    ("PACS", "PACS Group", "2024-04-11", 21),
    ("VIK",  "Viking Holdings", "2024-05-01", 24),
    ("TEM",  "Tempus AI", "2024-06-14", 37),
    ("WAY",  "Waystar", "2024-06-07", 21.5),
    ("OS",   "OneStream", "2024-07-24", 20),
    ("LINE", "Lineage", "2024-07-25", 78),
    ("SARO", "StandardAero", "2024-10-01", 24),
    ("VG",   "Venture Global", "2025-01-24", 25),
    ("CRWV", "CoreWeave", "2025-03-28", 40),
    ("ETOR", "eToro", "2025-05-14", 52),
    ("CRCL", "Circle", "2025-06-05", 31),
]


def _recent_analog(t, name, ipo, px):
    lockup = (datetime.strptime(ipo, "%Y-%m-%d") + timedelta(days=180)).strftime("%Y-%m-%d")
    a = _expand(t, name, ipo, px, lockup, [])
    a["cohort"] = "2024+"
    return a


ANALOGS = ANALOGS + [_recent_analog(*r) for r in _RECENT_2024]


# -----------------------------------------------------------------------------
#  Data: Yahoo v8 chart with on-disk CSV cache (so re-runs are deterministic
#  and don't hammer the endpoint).
# -----------------------------------------------------------------------------
def _to_ts(d):
    return int(time.mktime(datetime.strptime(d, "%Y-%m-%d").timetuple()))


def fetch_ohlcv(sym, start, end):
    cache = os.path.join(DATA_DIR, f"{sym}_{start}_{end}.csv")
    if os.path.exists(cache):
        return _read_cache(cache)
    p1, p2 = _to_ts(start), _to_ts(end) + 86400
    last = ""
    for host in ("query1", "query2"):
        for k in range(4):
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}"
                   f"?period1={p1}&period2={p2}&interval=1d")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                raw = urllib.request.urlopen(req, timeout=30).read()
                d = json.loads(raw)
                res = d["chart"]["result"][0]
                ts = res["timestamp"]; q = res["indicators"]["quote"][0]
                from datetime import timezone as _tz
                rows = []
                for i, t in enumerate(ts):
                    c = q["close"][i]
                    if c is None:
                        continue
                    rows.append({
                        "date": datetime.fromtimestamp(t, _tz.utc).strftime("%Y-%m-%d"),
                        "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
                        "close": c, "volume": q["volume"][i] or 0})
                _write_cache(cache, rows)
                return rows
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"fetch {sym} failed: {last}")


def _write_cache(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _read_cache(path):
    with open(path, encoding="utf-8") as f:
        out = []
        for r in csv.DictReader(f):
            out.append({"date": r["date"], "open": float(r["open"] or 0),
                        "high": float(r["high"] or 0), "low": float(r["low"] or 0),
                        "close": float(r["close"]), "volume": float(r["volume"] or 0)})
        return out


# -----------------------------------------------------------------------------
#  Build an engine Snapshot from the trailing OHLCV window as of bar i.
#  No look-ahead: only data up to and including bar i is used.
# -----------------------------------------------------------------------------
def _ann_realized_vol(closes):
    if len(closes) < 6:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes)) if closes[k - 1] > 0]
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def build_snapshot(rows, i, analog, cfg):
    today = datetime.strptime(rows[i]["date"], "%Y-%m-%d").date()
    win = rows[max(0, i - 19): i + 1]
    closes = [r["close"] for r in win]
    highs = [r["high"] for r in win]
    lows = [r["low"] for r in win]
    vols = [r["volume"] for r in win]
    s = eng.Snapshot(ts=datetime.strptime(rows[i]["date"], "%Y-%m-%d"))
    s.price = rows[i]["close"]
    s.volume = rows[i]["volume"]
    s.avg_volume_20d = sum(vols) / len(vols) if vols else None
    s.recent_highs = highs[-10:]
    s.recent_lows = lows[-10:]
    s.recent_closes = closes[-10:]
    s.recent_volumes = vols[-10:]
    s.long_closes = [r["close"] for r in rows[max(0, i - 59): i + 1]]  # ~60d trend ref
    # realized vol as the IV proxy (no historical options data); label downstream.
    rv = _ann_realized_vol(closes)
    s.iv_annual = rv
    rv5 = _ann_realized_vol([r["close"] for r in rows[max(0, i - 24): i - 4]]) if i >= 10 else None
    s.iv_5d_ago = rv5
    # iv percentile vs the run's own realized-vol history so far
    hist_rv = []
    for j in range(20, i + 1, 2):
        v = _ann_realized_vol([r["close"] for r in rows[max(0, j - 19): j + 1]])
        if v is not None:
            hist_rv.append(v)
    if rv is not None and len(hist_rv) >= 8:
        below = sum(1 for x in hist_rv if x <= rv)
        s.iv_percentile = below / len(hist_rv)
    elif rv is not None:
        s.iv_percentile = 0.7
    # realized-vol series for the new vol-dynamics signals (most-recent last)
    s.iv_series = [v for v in hist_rv][-12:] if hist_rv else None
    # Catalyst Clock: days to next/major unlock from the analog's lockup schedule
    sched = sorted(analog["lockups"], key=lambda e: e["date"])
    fut = [e for e in sched if datetime.strptime(e["date"], "%Y-%m-%d").date() >= today]
    base = 5
    running, cum_by = base, {}
    for e in sched:
        running += e.get("pct", 0)
        cum_by[e["date"]] = min(running, 100)
    elapsed = [e for e in sched if datetime.strptime(e["date"], "%Y-%m-%d").date() < today]
    s.cum_float_pct = min(cum_by[elapsed[-1]["date"]], 100) if elapsed else base
    if fut:
        nx = fut[0]
        s.days_to_next_unlock = (datetime.strptime(nx["date"], "%Y-%m-%d").date() - today).days
        s.next_unlock_label = f"{nx['label']} {nx['date']} (+{nx['pct']}%)"
        majors = [e for e in fut if e["pct"] >= 15]
        if majors:
            mj = majors[0]
            s.days_to_next_major = (datetime.strptime(mj["date"], "%Y-%m-%d").date() - today).days
            s.next_major_label = f"{mj['label']} {mj['date']}"
            s.next_major_pct = mj["pct"]
            s.extras["next_major_date"] = mj["date"]
            s.extras["major_unlocks"] = [(e["date"], e["pct"]) for e in majors]
    # near-window cumulative float velocity (supply ramp): %float added in next 30d
    horizon = today + timedelta(days=30)
    s.float_velocity_30d = sum(e["pct"] for e in fut
                               if datetime.strptime(e["date"], "%Y-%m-%d").date() <= horizon) or 0
    eng.compute_atr(s)
    return s


# -----------------------------------------------------------------------------
#  Run the engine over the whole series (no look-ahead).
# -----------------------------------------------------------------------------
def _mkt_up(bench, dstr):
    """Is the broad market (SPY) in a risk-on uptrend (above its 50-day MA)?"""
    bd, bc = bench
    if not bd:
        return False
    i = bisect.bisect_right(bd, dstr) - 1
    if i < 50:
        return False
    return bc[i] > sum(bc[i-49:i+1]) / 50


def bt_level(snap, S, comp, conf, sc, variant="base", mkt_up=False):
    """Experimental early-warning variants for the regime study. 'base' is the
    shipped logic; the others add a stricter trend break and/or a market-regime
    veto, to test what generalizes out-of-sample."""
    if variant == "base":
        return eng.early_warning_level(snap, S, comp, conf, sc)
    m = {x.key: x for x in S}
    peakv = max([v for v in (sc, snap.scent_recent_peak) if v is not None], default=None)
    fragile = peakv is not None and peakv >= eng.SCENT_ELEV_AT
    catalyst = snap.days_to_next_major is not None and -3 <= snap.days_to_next_major <= 21
    ref = snap.long_closes or snap.recent_closes
    price = snap.price
    if "trend50" in variant and ref and len(ref) >= 10 and price:
        kk = min(len(ref), 50); breaking = price < sum(ref[-kk:]) / kk   # deeper trend break
    else:
        breaking = eng._breaking_down(snap, m)
    armed = catalyst and breaking
    if "regime" in variant and mkt_up:
        armed = False                                                    # don't short into a risk-on tape
    if armed:
        return "ARMED"
    if (fragile and breaking) or (sc is not None and sc >= eng.SCENT_STIR_AT) or fragile or comp >= 40:
        return "WATCH"
    return "DORMANT"


def run_series(analog, cfg, warmup=15, variant="base", bench=(None, None)):
    rows = fetch_ohlcv(analog["sym"], analog["start"], analog["end"])
    series = []
    scent_hist = []   # backward-looking SCENT history for "fragile top formed?"
    for i in range(len(rows)):
        if i < warmup:
            continue
        snap = build_snapshot(rows, i, analog, cfg)
        S = eng.build_signals(snap, cfg)
        comp = eng.composite(S)
        conf = eng.confluence(snap, S, cfg)
        sc = eng.scent_score(snap, S)
        snap.scent_recent_peak = max(scent_hist[-15:]) if scent_hist else None  # prior bars only
        scent_hist.append(sc if sc is not None else 0)
        lvl = bt_level(snap, S, comp, conf, sc, variant, _mkt_up(bench, rows[i]["date"]))
        series.append({"date": rows[i]["date"], "px": rows[i]["close"], "level": lvl,
                       "comp": comp, "scent": sc})
    return rows, series


# Levels we treat as an actionable early bearish flag in the backtest. (TRIGGER
# needs live options-flow to clear the squeeze guard, which history lacks, so the
# backtest measures the early-warning rungs that price/vol/timing CAN drive.)
BEARISH = {"WATCH", "ARMED", "TRIGGER"}


def _trading_idx(rows, dstr):
    for k, r in enumerate(rows):
        if r["date"] >= dstr:
            return k
    return None


def evaluate(analog, rows, series):
    closes = [r["close"] for r in rows]
    dates = [r["date"] for r in rows]
    # data-derived peak over the studied window (after warmup)
    start_k = _trading_idx(rows, series[0]["date"]) if series else 0
    peak_k = max(range(start_k, len(closes)), key=lambda k: closes[k])
    peak_px, peak_date = closes[peak_k], dates[peak_k]
    trough_k = max(range(peak_k, len(closes)), key=lambda k: -closes[k]) if peak_k < len(closes) - 1 else peak_k
    max_dd = (closes[trough_k] / peak_px - 1) if peak_px else 0
    # first bearish flag
    first = next((p for p in series if p["level"] in BEARISH), None)
    first_armed = next((p for p in series if p["level"] in ("ARMED", "TRIGGER")), None)

    def fwd(dstr, horizon):
        k = _trading_idx(rows, dstr)
        if k is None or k + horizon >= len(closes):
            j = len(closes) - 1
        else:
            j = k + horizon
        base = closes[k] if k is not None else None
        return (closes[j] / base - 1) if base else None

    def lead(dstr):
        k = _trading_idx(rows, dstr)
        return (peak_k - k) if k is not None else None  # +ve = flagged before peak

    out = {
        "sym": analog["sym"], "name": analog["name"],
        "ipo_px": analog["ipo_px"], "peak_px": round(peak_px, 2), "peak_date": peak_date,
        "max_drawdown_from_peak": round(max_dd * 100, 1),
        "first_flag": first["level"] if first else None,
        "first_flag_date": first["date"] if first else None,
        "first_flag_lead_days": lead(first["date"]) if first else None,
        "first_flag_fwd20": round(fwd(first["date"], 20) * 100, 1) if first else None,
        "first_flag_fwd40": round(fwd(first["date"], 40) * 100, 1) if first else None,
        "first_armed_date": first_armed["date"] if first_armed else None,
        "first_armed_lead_days": lead(first_armed["date"]) if first_armed else None,
        "first_armed_fwd40": round(fwd(first_armed["date"], 40) * 100, 1) if first_armed else None,
    }
    # level at each lockup date
    out["at_lockups"] = []
    for e in analog["lockups"]:
        p = next((x for x in series if x["date"] >= e["date"]), None)
        out["at_lockups"].append({"date": e["date"], "label": e["label"],
                                  "level": p["level"] if p else None})
    return out


def _fmt(v, suff="", w=8):
    if v is None:
        return "—".rjust(w)
    if isinstance(v, float):
        return (f"{v:+.1f}{suff}").rjust(w)
    return (f"{v}{suff}").rjust(w)


def bench_series():
    """SPY closes for market-adjustment (separating lockup alpha from 2022-bear beta)."""
    try:
        rows = fetch_ohlcv("SPY", "2012-01-01", "2025-06-30")
        return [r["date"] for r in rows], [r["close"] for r in rows]
    except Exception:
        return None, None


def _bench_close(bench, dstr):
    bd, bc = bench
    if not bd:
        return None
    i = bisect.bisect_right(bd, dstr) - 1
    return bc[i] if i >= 0 else None


def simulate_trades(series, bench=(None, None), horizon=20, stop=0.20, target=0.25, hurdle=0.03):
    """Simulate the actual short/put trades the method would take, to measure a
    per-trade success rate. Each FRESH ARMED onset opens a trade; it exits at the
    first of: +stop adverse move (squeeze cut), -target favorable move (take
    profit), or `horizon` trading days (the unlock window). Short P&L on the
    underlying = (entry-exit)/entry. A 'win' beats a small `hurdle` (a crude proxy
    for the premium/IV a real put spread must overcome — so it is conservative).
    Re-arming requires leaving ARMED and re-entering (no overlapping trades)."""
    px = [p["px"] for p in series]; lv = [p["level"] for p in series]; dt = [p["date"] for p in series]
    sc = [p.get("scent") for p in series]
    trades = []; i, n = 0, len(series); armed_prev = False
    while i < n:
        onset = (lv[i] == "ARMED" and not armed_prev)
        armed_prev = (lv[i] == "ARMED")
        if onset:
            entry = px[i]; exit_i = min(i + horizon, n - 1); reason = "horizon"
            for j in range(i + 1, min(i + horizon, n - 1) + 1):
                if (px[j] - entry) / entry >= stop:
                    exit_i, reason = j, "stop"; break
                if (entry - px[j]) / entry >= target:
                    exit_i, reason = j, "target"; break
            pnl = (entry - px[exit_i]) / entry
            hold_i = min(i + horizon, n - 1)
            pnl_hold = (entry - px[hold_i]) / entry      # pure fixed-horizon, no stop/target
            # market-adjusted (alpha): short-stock P&L minus short-SPY P&L over the same window.
            # >0 means the stock fell MORE than the market — real idiosyncratic downside, not beta.
            be, bx = _bench_close(bench, dt[i]), _bench_close(bench, dt[exit_i])
            alpha = (pnl - (be - bx) / be) if (be and bx) else None
            trades.append({"entry_date": dt[i], "entry_px": round(entry, 2), "exit_date": dt[exit_i],
                           "exit_px": round(px[exit_i], 2), "reason": reason,
                           "pnl_pct": round(pnl * 100, 1), "win": pnl > hurdle,
                           "pnl_hold_pct": round(pnl_hold * 100, 1), "win_hold": pnl_hold > hurdle,
                           "alpha_pct": round(alpha * 100, 1) if alpha is not None else None,
                           "win_alpha": (alpha > hurdle) if alpha is not None else None,
                           "scent_entry": sc[i], "scent_peak": max([x for x in sc[max(0, i-15):i+1] if x is not None] or [0])})
            i = exit_i; armed_prev = (lv[i] == "ARMED")
        i += 1
    return trades


def agg_trades(trades):
    if not trades:
        return None
    p = [t["pnl_pct"] for t in trades]
    wins = [x for x in p if x > 0]; losses = [x for x in p if x <= 0]
    gw = sum(wins); gl = -sum(losses)
    h = [t["pnl_hold_pct"] for t in trades]
    al = [t["alpha_pct"] for t in trades if t.get("alpha_pct") is not None]
    alpha_block = {}
    if al:
        alpha_block = {"alpha_win_rate": round(sum(1 for t in trades if t.get("win_alpha")) / len(al) * 100, 1),
                       "avg_alpha": round(sum(al) / len(al), 1), "median_alpha": round(sorted(al)[len(al) // 2], 1)}
    # SCENT split: do entries with an ELEVATED recent SCENT peak do better?
    hi = [t for t in trades if (t.get("scent_peak") or 0) >= 65]
    lo = [t for t in trades if (t.get("scent_peak") or 0) < 65]
    def _wr(ts):
        return round(sum(1 for t in ts if t["win"]) / len(ts) * 100, 1) if ts else None
    def _ap(ts):
        return round(sum(t["pnl_pct"] for t in ts) / len(ts), 1) if ts else None
    scent_block = {"n_scent_hi": len(hi), "n_scent_lo": len(lo),
                   "win_scent_hi": _wr(hi), "win_scent_lo": _wr(lo),
                   "avg_scent_hi": _ap(hi), "avg_scent_lo": _ap(lo)}
    return {**alpha_block, **scent_block, "n": len(trades),
            "win_rate": round(sum(1 for t in trades if t["win"]) / len(trades) * 100, 1),
            "hold_win_rate": round(sum(1 for t in trades if t["win_hold"]) / len(trades) * 100, 1),
            "hold_avg_pnl": round(sum(h) / len(h), 1),
            "dir_win_rate": round(len(wins) / len(trades) * 100, 1),
            "avg_pnl": round(sum(p) / len(p), 1), "median_pnl": round(sorted(p)[len(p) // 2], 1),
            "avg_win": round(sum(wins) / len(wins), 1) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 1) if losses else 0,
            "profit_factor": round(gw / gl, 2) if gl > 0 else None,
            "best": max(p), "worst": min(p)}


def scent_quality(rows, series, elev=65, fwd=20):
    """SCENT-as-early-warning quality for ONE analog: did SCENT reach ELEVATED
    BEFORE the price peak (recall + lead)? Plus a 'specificity' check: how much
    of the run was spent elevated far from the top (lower = sharper)."""
    if not series:
        return None
    closes = [r["close"] for r in rows]; dates = [r["date"] for r in rows]
    start_k = _trading_idx(rows, series[0]["date"]) or 0
    peak_k = max(range(start_k, len(closes)), key=lambda k: closes[k])
    peak_date = dates[peak_k]
    elevated = [p for p in series if (p.get("scent") or 0) >= elev]
    before = [p for p in elevated if p["date"] < peak_date]
    lead = None
    if before:
        fk = _trading_idx(rows, before[0]["date"])
        lead = (peak_k - fk) if fk is not None else None
    return {"elev_before_peak": bool(before), "lead": lead,
            "frac_elevated": round(len(elevated) / len(series), 2)}


def run_scentq(cfg):
    """Measure SCENT quality on both cohorts so each improvement can be scored.
    A change is kept ONLY if it raises elevated-before-peak / lead WITHOUT
    regressing the other cohort (the user's bar: must genuinely improve SCENT)."""
    eng.enable_utf8_output()
    cohorts = {"2019-23": [a for a in ANALOGS if a.get("cohort") != "2024+"],
               "2024+": [a for a in ANALOGS if a.get("cohort") == "2024+"]}
    print("\n  SCENT QUALITY — does SCENT elevate BEFORE the peak? (higher recall + lead = better)\n")
    print(f"  {'cohort':<9}{'names':>6}{'elev-before-peak':>18}{'median lead':>13}{'avg %elevated':>15}")
    print("  " + "-" * 62)
    for cname, calist in cohorts.items():
        n = 0; rec = 0; leads = []; fracs = []
        for a in calist:
            try:
                rows, series = run_series(a, cfg)
            except Exception:
                continue
            q = scent_quality(rows, series)
            if not q:
                continue
            n += 1; fracs.append(q["frac_elevated"])
            if q["elev_before_peak"]:
                rec += 1
                if q["lead"] is not None:
                    leads.append(q["lead"])
        recpct = round(rec / n * 100) if n else 0
        med = sorted(leads)[len(leads) // 2] if leads else None
        af = round(sum(fracs) / len(fracs) * 100) if fracs else 0
        print(f"  {cname:<9}{n:>6}{f'{rec}/{n} ({recpct}%)':>18}{(str(med)+'d') if med is not None else '—':>13}{f'{af}%':>15}")
    print("\n  (elev-before-peak = SCENT smelled the top early; median lead = days early; "
          "avg %elevated = how often it cried wolf — lower is sharper.)")


def run_experiment(cfg):
    """Test candidate improvements (stricter trend break + market-regime veto) on
    BOTH cohorts at once — the honest way to tell a real improvement from overfit:
    it must keep 2019-23 healthy AND lift the out-of-sample 2024+ above breakeven."""
    eng.enable_utf8_output()
    bench = bench_series()
    cohorts = {"2019-23": [a for a in ANALOGS if a.get("cohort") != "2024+"],
               "2024+": [a for a in ANALOGS if a.get("cohort") == "2024+"]}
    variants = ["base", "trend50", "regime", "trend50+regime"]
    print("\n  REGIME EXPERIMENT — can a trend/regime filter rescue 2024+ without breaking 2019-23?\n")
    print(f"  {'variant':<15}{'cohort':<9}{'trades':>7}{'win%':>7}{'avg%':>7}{'PF':>6}{'alphaWin%':>10}{'avgAlpha':>9}")
    print("  " + "-" * 70)
    for v in variants:
        for cname, calist in cohorts.items():
            alltr = []
            for a in calist:
                try:
                    _, series = run_series(a, cfg, variant=v, bench=bench)
                    alltr += simulate_trades(series, bench)
                except Exception:
                    continue
            ta = agg_trades(alltr)
            if ta:
                pf = ta["profit_factor"] if ta["profit_factor"] is not None else 0
                print(f"  {v:<15}{cname:<9}{ta['n']:>7}{ta['win_rate']:>7.0f}{ta['avg_pnl']:>7.1f}"
                      f"{pf:>6.2f}{ta.get('alpha_win_rate', 0):>10}{ta.get('avg_alpha', 0):>9}")
            else:
                print(f"  {v:<15}{cname:<9}{0:>7}   (no trades)")
        print()
    print("  A variant only 'wins' if 2019-23 stays strong AND 2024+ turns positive (real, not overfit).")


def main():
    cfg = eng.CONFIG
    eng.enable_utf8_output()
    if "--experiment" in sys.argv:
        run_experiment(cfg); return
    if "--scentq" in sys.argv:
        run_scentq(cfg); return
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    recent = "--recent" in sys.argv
    if recent:
        analogs = [a for a in ANALOGS if a.get("cohort") == "2024+"]
    elif only:
        analogs = [a for a in ANALOGS if a["sym"] in only]
    else:
        analogs = [a for a in ANALOGS if a.get("cohort") != "2024+"]   # 2019-23 in-sample set
    results = []; all_trades = []
    bench = bench_series()   # SPY, for market-adjusted (alpha) trade returns
    print("\n  SPCX METHOD — HISTORICAL BACKTEST ON ANALOG IPOs")
    print("  (early-warning spine: price/volume/realized-vol/catalyst-clock; "
          "options-flow signals not backtestable)\n")
    for a in analogs:
        try:
            rows, series = run_series(a, cfg)
        except Exception as ex:
            print(f"  {a['sym']}: data error — {ex}")
            continue
        ev = evaluate(a, rows, series)
        trades = simulate_trades(series, bench); ev["trades"] = trades; ev["tstats"] = agg_trades(trades)
        all_trades += trades
        results.append(ev)
        print(f"  ── {ev['sym']} ({ev['name']}) ── IPO ${ev['ipo_px']} → peak "
              f"${ev['peak_px']} on {ev['peak_date']} → drawdown {ev['max_drawdown_from_peak']}%")
        print(f"     first flag: {ev['first_flag']} on {ev['first_flag_date']} "
              f"(lead {ev['first_flag_lead_days']}d to peak)  fwd40 {ev['first_flag_fwd40']}%")
        ts = ev["tstats"]
        if ts:
            print(f"     trades: {ts['n']}  win {ts['win_rate']}%  avg {ts['avg_pnl']:+.1f}%  "
                  f"PF {ts['profit_factor']}  (best {ts['best']:+.0f} / worst {ts['worst']:+.0f})")
        else:
            print("     trades: 0 (never ARMED)")
        if len(analogs) == 1:
            print("\n     day-by-day (every 5th bar):")
            for p in series[::5]:
                print(f"       {p['date']}  ${p['px']:.2f}  {p['level']:<8} comp={p['comp']:.0f}"
                      + (f" scent={p['scent']:.0f}" if p['scent'] is not None else ""))
    # aggregate
    if results:
        leads = [r["first_flag_lead_days"] for r in results if r["first_flag_lead_days"] is not None]
        f40 = [r["first_flag_fwd40"] for r in results if r["first_flag_fwd40"] is not None]
        led = sum(1 for x in leads if x > 0)
        print("\n  ── AGGREGATE ──")
        print(f"     analogs: {len(results)}  |  flagged before peak: {led}/{len(leads)}")
        if leads:
            print(f"     median lead to peak: {sorted(leads)[len(leads)//2]}d  "
                  f"(range {min(leads)}..{max(leads)})")
        ta = agg_trades(all_trades)
        if ta:
            print(f"\n  ── TRADE SUCCESS RATE (every ARMED entry, {len(results)} names) ──")
            print(f"     trades:            {ta['n']}")
            print(f"     win rate (managed): {ta['win_rate']}%   avg P&L {ta['avg_pnl']:+.1f}%   "
                  f"median {ta['median_pnl']:+.1f}%   PF {ta['profit_factor']}")
            print(f"     win rate (20d hold): {ta['hold_win_rate']}%   avg P&L {ta['hold_avg_pnl']:+.1f}%   "
                  f"(parameter-free — no stop/target)")
            if "alpha_win_rate" in ta:
                print(f"     MARKET-ADJUSTED (vs SPY): {ta['alpha_win_rate']}% beat the market down   "
                      f"avg alpha {ta['avg_alpha']:+.1f}%  median {ta['median_alpha']:+.1f}%")
                print("       ^ this is the real edge — strips out the 2022-bear beta that flatters the raw win rate")
            print(f"     avg win {ta['avg_win']:+.1f}%  |  avg loss {ta['avg_loss']:+.1f}%  |  "
                  f"best {ta['best']:+.0f}%  worst {ta['worst']:+.0f}%")
            print(f"     SCENT split — elevated entry ({ta['n_scent_hi']}): win {ta['win_scent_hi']}% avg "
                  f"{ta['avg_scent_hi']}%   vs  quiet ({ta['n_scent_lo']}): win {ta['win_scent_lo']}% avg {ta['avg_scent_lo']}%")
            print("     (short P&L on the underlying; a 'win' clears a 3% hurdle for option cost. "
                  "Live squeeze/IV suppressors — not backtestable — would cut the worst losers.)")
        rpath = "backtest_report_2024plus.md" if recent else "backtest_report.md"
        _write_report(results, ta, rpath)
        print(f"\n     wrote {rpath}\n")
    return results


def _write_report(results, ta=None, path="backtest_report.md"):
    lines = ["# SPCX early-warning method — historical backtest", "",
             "Method run forward (no look-ahead) on real OHLCV of analogous hyped IPOs with",
             "known lockup schedules. Validates the price/volume/realized-vol/catalyst-clock",
             "spine; options-flow/borrow/skew leading signals require data history lacks.", ""]
    if ta:
        lines += [f"## Trade success rate ({ta['n']} simulated ARMED entries)", "",
                  f"- **Win rate: {ta['win_rate']}%** (directional {ta['dir_win_rate']}%)",
                  f"- Avg P&L/trade **{ta['avg_pnl']:+.1f}%** (median {ta['median_pnl']:+.1f}%)",
                  f"- Avg win {ta['avg_win']:+.1f}% | avg loss {ta['avg_loss']:+.1f}% | "
                  f"profit factor {ta['profit_factor']}",
                  f"- Best {ta['best']:+.0f}% | worst {ta['worst']:+.0f}%",
                  "- Short P&L on the underlying; win clears a 3% option-cost hurdle. "
                  "Live squeeze/IV suppressors (not backtestable) would cut the worst losers.", ""]
    lines += ["## Per-analog", "",
              "| Analog | IPO→Peak | Drawdown | First flag (lead) | Trades | Win% | Avg P&L |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        t = r.get("tstats")
        avg = f"{t['avg_pnl']:+.1f}%" if t else "—"
        winr = t["win_rate"] if t else "—"
        lines.append(f"| {r['sym']} {r['name']} | ${r['ipo_px']}→${r['peak_px']} | "
                     f"{r['max_drawdown_from_peak']}% | {r['first_flag']} {r['first_flag_date']} "
                     f"({r['first_flag_lead_days']}d) | {t['n'] if t else 0} | {winr} | {avg} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
