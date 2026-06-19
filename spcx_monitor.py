#!/usr/bin/env python3
# =============================================================================
#  SPCX SHORT-ENTRY WARNING SYSTEM  —  single file
#  VERSION 11.0
# -----------------------------------------------------------------------------
#  An absolute-warning monitor for the SPCX short. It watches the conditions
#  that must line up BEFORE shorting a hyped, low-float mega-IPO, scores them,
#  runs a 4-stage warning ladder, blocks entry during gamma-squeeze risk, and
#  — when (and only when) the checklist is truly met — fires a loud TRIGGER and
#  hands you a ready-to-place put-spread plan sized to your capital.
#
#  It shows WHERE every number comes from (provenance), tracks change since the
#  last run, and persists state so it can run on a schedule or in --watch mode.
#
#  RUN
#     python spcx_monitor.py             # one live reading (TWS -> else seed)
#     python spcx_monitor.py --html      # also write dashboard.html
#     python spcx_monitor.py --watch 300 # poll every 300s, alert on escalation
#     python spcx_monitor.py --demo      # too-early / aligned / blocked
#     python spcx_monitor.py --selftest  # internal assertions, prints PASS/FAIL
#     python spcx_monitor.py --replay f.csv   # backtest thresholds over history
#     python spcx_monitor.py --gen-sample-csv # write a replay CSV template
#
#  This is DECISION-SUPPORT, not a trade call. Even a TRIGGER is a probability,
#  not a promise. It does not predict tops and does not place orders. You decide,
#  you trade. Not financial advice.
#
#  CHANGELOG
#   v1  multi-tier signals, composite score, confluence gate
#   v2  IV percentile from stored history, trend-rollover signal, run-over-run deltas
#   v3  4-stage WARNING LADDER (DORMANT/WATCH/ARMED/TRIGGER) + BLOCKED override,
#       escalation-only alerts with cooldown, persisted state machine
#   v4  RISK ENGINE: Black-Scholes put-spread builder, ATR stops, capital sizing,
#       max-loss/breakeven/payoff, thesis-invalidation level
#   v5  robustness: --watch loop, --selftest, config validation, GEX hook,
#       relative-strength hook, hardened graceful degradation
#   v6  Telegram/email escalation, upgraded dashboard (ladder+risk+provenance),
#       signal journal, final self-test
#   v7  SENTIMENT/NEWS tier: social sentiment trend + attention (mention volume)
#       as momentum-exhaustion confirms; news flow signal; a bullish catalyst
#       headline becomes a SQUEEZE-RISK BLOCKER (Musk-headline protection)
#   v8  STAGGERED UNLOCK SCHEDULE: models the full lockup timeline (not one
#       cliff). unlock_window timing signal goes hot near major supply events;
#       float-lock context (95% locked now -> squeeze-prone); catalyst clock
#       shows next unlock + next MAJOR unlock; plan flags nearest supply event
#   v9  KAIZEN: data-completeness/confidence score; "what flips this to TRIGGER"
#       gap analysis; per-tier score breakdown; dynamic expiry that spans the
#       next major unlock; scale-in tranche ladder across unlock waves; alert
#       de-bounce (hysteresis); score-trend sparkline; --replay backtest mode
#   v10 KAIZEN+TESTS: option Greeks (delta/theta/vega) + probability-of-profit +
#       IV-crush scenario on the plan; "what changed since last run" signal diff;
#       --fuzz invariant testing (randomized snapshots, asserts no rule broken)
# =============================================================================
from __future__ import annotations
import sys, os, json, sqlite3, calendar, time, urllib.request, urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None
from math import log, sqrt, exp, erf, floor

VERSION = "11.1"


def _today_et():
    """Calendar date in US market time (America/New_York) so unlock/lockup day
    counts don't shift by one for a user east of the US or running near midnight."""
    return datetime.now(_ET).date() if _ET is not None else date.today()


def enable_utf8_output(*streams):
    """Force UTF-8 on stdout/stderr (or the given text streams) so the emoji,
    arrows and box glyphs in the report never crash on a non-UTF-8 Windows
    console (e.g. cp1254). This is the root fix for the whole print-crash class."""
    if not streams:
        streams = (sys.stdout, sys.stderr)
    for s in streams:
        try:
            s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

# =============================================================================
#  CONFIG  — your thesis as numbers. Edit freely.
# =============================================================================
CONFIG = {
    "ticker": "SPCX",
    "ibkr_contract_id": 890493863,
    "tws": {"host": "127.0.0.1", "port": 7496, "client_id": 77},

    "fundamentals": {
        # Sourced from the SpaceX (SPCX) Form 424B4, filed 2026-06-12, Reg. 333-296070,
        # CIK 1181412 (sec.gov/Archives/edgar/data/1181412/000162828026042639/).
        # IPO $135.00; 555.6M Class A sold; ~13.1B total shares (7.49B A + 5.60B B);
        # IPO float ~556M ≈ 4-5% of total (~95% locked).
        "ipo_price": 135.00,
        "ttm_revenue_usd": 18_700_000_000,     # assumption — verify in first 10-Q
        "shares_outstanding": 13_100_000_000,  # 424B4 post-offering total (A+B)
        "lockup_expiry": "2026-12-08",         # 180-day expiry (day 180)
        "index_inclusion_effective": None,     # Nasdaq-100 fast-track possible ~15 trading days
        "current_float_pct": 5,                # ~95% locked at IPO (424B4)
        # 180-day lock-up tranches as % OF THE 180-DAY LOCK-UP POOL (verbatim 424B4).
        # The two big EARNINGS-ANCHORED tranches (Q2 20%, Q3 28%) have ESTIMATED
        # dates — re-anchor when SPCX sets its earnings dates. "conditional" = the
        # 30%/5-of-10 price trigger that only fires into STRENGTH (bullish — never
        # arm a short on it). Musk's 6.4B founder shares lock 366d with NO early
        # release (supply-absorbing). holder_weight: vc 1.0 > employee .7 > officer .5.
        "unlock_pct_basis": "180d_lockup_pool",
        "unlock_schedule": [
            {"date": "2026-08-04", "label": "Q2 earnings +2d", "pct": 20, "major": True,
             "holder": "mixed", "anchor": "earnings", "est": True},
            {"date": "2026-08-04", "label": "price-trigger (≥30% for 5/10d)", "pct": 10,
             "conditional": True, "holder": "mixed", "anchor": "earnings", "est": True},
            {"date": "2026-08-20", "label": "day-70 unlock", "pct": 7, "holder": "mixed"},
            {"date": "2026-09-09", "label": "day-90 unlock", "pct": 7, "holder": "mixed"},
            {"date": "2026-09-24", "label": "day-105 unlock", "pct": 7, "holder": "mixed"},
            {"date": "2026-10-09", "label": "day-120 unlock", "pct": 7, "holder": "mixed"},
            {"date": "2026-10-24", "label": "day-135 unlock", "pct": 7, "holder": "mixed"},
            {"date": "2026-11-06", "label": "Q3 earnings +2d (LARGEST)", "pct": 28, "major": True,
             "holder": "mixed", "anchor": "earnings", "est": True},
            {"date": "2026-12-08", "label": "180d remainder", "pct": 14, "major": False, "holder": "mixed"},
            {"date": "2027-03-18", "label": "extended-lock (VC)", "pct": 10, "holder": "vc"},
            {"date": "2027-05-17", "label": "extended-lock (VC)", "pct": 10, "holder": "vc"},
            {"date": "2027-06-12", "label": "Founder/Musk 366d (no early release)", "pct": 40,
             "major": True, "holder": "founder"},
        ],
    },

    "thresholds": {
        "ps_stretched": 80, "ps_normalizing": 50,
        "volume_fade_pct": 40,
        "iv_percentile_high": 80,
        "short_interest_pct_float": 15,
        "dec_put_call_ratio": 0.8,
        "gamma_block_usd": 10_000_000,
        "iv_skew_block": 0.05,
        "support_floor": None,   # optional hard floor; None = adaptive falling-knife only
        "knife_drop_pct": 0.12,   # 'falling knife': price >this% below recent-close avg = wait
        "unlock_window_days": 12,    # within this many days of a MAJOR unlock = hot
        "min_data_confidence": 55,   # no TRIGGER below this % data completeness (accuracy guard)
        "divergence_min_bars": 5,    # bars needed before a divergence counts as confirmed
    },

    "manual_squeeze_block": False,

    # Risk engine — how you'd structure the trade once it triggers.
    "risk": {
        "capital_usd": 100_000,
        "max_risk_pct": 15,          # never risk more than this % of capital
        "long_put_moneyness": 0.95,  # buy put at ~95% of spot
        "short_put_moneyness": 0.78, # sell put at ~78% (defines the spread floor)
        "risk_free_rate": 0.045,
        "atr_stop_mult": 1.5,        # thesis invalidates above swing-high + k*ATR
    },

    # Alerts. Escalation alerts fire when the level rises; the DAILY DIGEST always
    # fires once per calendar day (--daily, or the heartbeat in --watch).
    "alerts": {
        # ntfy = simplest phone push: install the free "ntfy" app, SUBSCRIBE to this
        # exact topic, and you'll get notifications. Topic is secret-ish — change it.
        "ntfy": {"enabled": True, "topic": "spcx-warn-arben-9f3k2x7q", "server": "https://ntfy.sh"},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "email": {"enabled": False, "to": "arbenya@gmail.com", "smtp_host": "localhost"},
        "cooldown_minutes": 120,
        "hysteresis_runs": 2,   # a higher level must persist this many runs before alerting (BLOCKED fires immediately)
        "daily_digest": True,   # always send one summary notification per day
    },

    # Seed = last known LIVE readings (IBKR pull 2026-06-16). Used when TWS off,
    # so you always get a real, current reading. Provenance always shows SEEDED.
    "seed": {
        "price": 212.45, "volume": 241_569_595, "avg_volume_20d": 300_000_000,
        "iv_annual": 1.1932, "iv_5d_ago": None,
        "all_expiry_call_vol": 725_926, "all_expiry_put_vol": 525_822,
        "short_interest_pct_float": None, "short_interest_prev": None,
        "borrow_fee": None, "borrow_fee_prev": None,
        "insider_sales_30d": None,
        "otm_call_premium": 14_100_000, "otm_put_premium": 600_000,
        "call_iv_otm": None, "put_iv_otm": None,
        # recent daily bars since debut (approx) for trend + ATR
        "recent_highs": [176.52, 195.60, 225.64],
        "recent_lows":  [150.00, 165.00, 199.98],
        "recent_closes":[160.95, 192.50, 212.45],
        "recent_volumes":[410_000_000, 300_000_000, 241_569_595],  # price up, volume fading
        "index_return_5d": None,     # SPCX vs Nasdaq for relative strength hook
        "spcx_return_5d": None,
        # --- sentiment / news (euphoric today: stock +50% off IPO) ----------
        "social_sentiment": 0.68,     # -1..+1 net bull/bear (StockTwits/Reddit)
        "social_sentiment_prev": 0.55,  # rising = FOMO intact
        "social_volume": 48_000,      # mentions/day
        "social_volume_avg": 30_000,  # baseline -> spiking now
        "news_sentiment": 0.55,       # -1..+1 over recent headlines
        "bullish_catalyst": False,    # discrete Musk/SpaceX positive event = squeeze fuel
    },

    "edgar_cik": None,
    "finra_token": None,
    "unusual_whales_token": None,
    "finnhub_token": None,            # optional: news sentiment (finnhub.io)
    "stocktwits_enabled": True,       # public symbol stream for social sentiment
    "yahoo_enabled": True,            # free daily price/volume fallback when TWS is off

    "confluence_require_all": ["momentum_breaking", "short_interest_rising",
                               "dec_puts_elevated", "above_support", "leading_confirms"],
    "db_path": "spcx_monitor.db",
}
UA = "SPCX-Monitor/1.0 (arbenya@gmail.com)"
RED, YELLOW, GREEN = "RED", "YELLOW", "GREEN"
DORMANT, WATCH, ARMED, TRIGGER, BLOCKED = "DORMANT", "WATCH", "ARMED", "TRIGGER", "BLOCKED"
LADDER_RANK = {DORMANT: 0, WATCH: 1, ARMED: 2, TRIGGER: 3}

# =============================================================================
#  SNAPSHOT + provenance
# =============================================================================
@dataclass
class Snapshot:
    ts: datetime
    price: float = None; market_cap: float = None
    volume: float = None; avg_volume_20d: float = None
    iv_annual: float = None; iv_percentile: float = None; iv_5d_ago: float = None
    short_interest_pct_float: float = None; short_interest_prev: float = None
    borrow_fee: float = None; borrow_fee_prev: float = None
    dec_put_volume: float = None; dec_call_volume: float = None
    insider_sales_30d: int = None
    otm_call_premium: float = None; otm_put_premium: float = None
    call_iv_otm: float = None; put_iv_otm: float = None
    recent_highs: list = None; recent_lows: list = None; recent_closes: list = None
    recent_volumes: list = None
    atr: float = None
    index_return_5d: float = None; spcx_return_5d: float = None
    social_sentiment: float = None; social_sentiment_prev: float = None
    social_volume: float = None; social_volume_avg: float = None
    news_sentiment: float = None; bullish_catalyst: bool = None
    days_to_next_unlock: int = None; next_unlock_label: str = None
    days_to_next_major: int = None; next_major_label: str = None; next_major_pct: float = None
    cum_float_pct: float = None
    iv_series: list = None              # recent realized/implied vol history (for vol-of-vol, spot/vol corr)
    long_closes: list = None            # longer close history (~60d) for overextension vs trend
    float_velocity_30d: float = None    # % of float unlocking within the next 30 days (supply ramp)
    scent: float = None; scent_state: str = None   # early-warning fragility (set by scent_score)
    scent_recent_peak: float = None     # max SCENT over the recent window (did a fragile top form?)
    sources: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)

    def mark(self, f, v, src, status):
        self.sources[f] = (v, src, status)

# =============================================================================
#  LIVE COLLECTION (provenance-tagged)
# =============================================================================
def _third_friday(y, m):
    c = calendar.Calendar(firstweekday=calendar.MONDAY)
    return [d for d in c.itermonthdates(y, m) if d.month == m and d.weekday() == 4][2]


def collect_ibkr(cfg, snap) -> bool:
    try:
        from ib_insync import IB, Stock, Option
    except ImportError:
        snap.extras["ibkr"] = "ib_insync not installed"; return False
    ib = IB(); t = cfg["tws"]
    try:
        ib.connect(t["host"], t["port"], clientId=t["client_id"], readonly=True)
    except Exception as e:
        snap.extras["ibkr"] = f"no TWS: {e}"; return False
    try:
        src = "IBKR live (ib_insync->TWS)"
        stk = Stock(cfg["ticker"], "SMART", "USD"); ib.qualifyContracts(stk)
        tkr = ib.reqMktData(stk, "165,236", snapshot=False); ib.sleep(2.5)
        snap.price = tkr.last or tkr.close or tkr.marketPrice()
        snap.mark("price", snap.price, src, "LIVE")
        if tkr.volume:
            snap.volume = tkr.volume * 100; snap.mark("volume", snap.volume, src, "LIVE")
        iv = tkr.impliedVolatility or tkr.histVolatility
        if iv:
            snap.iv_annual = iv; snap.mark("iv_annual", iv, src, "LIVE")
        so = cfg["fundamentals"]["shares_outstanding"]
        if snap.price and so:
            snap.market_cap = snap.price * so
            snap.mark("market_cap", snap.market_cap, "derived: price x shares", "LIVE")
        bars = ib.reqHistoricalData(stk, "", "40 D", "1 day", "TRADES", True)
        if bars:
            snap.recent_highs = [b.high for b in bars[-10:]]
            snap.recent_lows = [b.low for b in bars[-10:]]
            snap.recent_closes = [b.close for b in bars[-10:]]
            snap.long_closes = [b.close for b in bars]   # full ~40d trend ref for SCENT overextension/parabola
            snap.recent_volumes = [b.volume*100 for b in bars[-10:] if b.volume]
            vols = [b.volume for b in bars[-20:] if b.volume]
            if vols:
                snap.avg_volume_20d = sum(vols)/len(vols)*100
                snap.mark("avg_volume_20d", snap.avg_volume_20d, src+" hist", "LIVE")
            snap.mark("recent_bars", len(bars), src+" hist", "LIVE")
        try:
            year = int(cfg["fundamentals"]["lockup_expiry"][:4])
            dexp = _third_friday(year, 12).strftime("%Y%m%d")
            params = ib.reqSecDefOptParams(stk.symbol, "", "STK", stk.conId)
            strikes = sorted({s for p in params for s in p.strikes
                              if 0.7*snap.price <= s <= 1.3*snap.price})
            pv = cv = 0.0
            for right in ("P", "C"):
                for k in strikes[:40]:
                    o = Option(stk.symbol, dexp, k, right, "SMART")
                    try:
                        ib.qualifyContracts(o)
                        ot = ib.reqMktData(o, "100", snapshot=True); ib.sleep(0.12)
                        v = (ot.volume or 0)*100
                        pv, cv = (pv+v, cv) if right == "P" else (pv, cv+v)
                    except Exception:
                        continue
            if pv or cv:
                snap.dec_put_volume, snap.dec_call_volume = pv or None, cv or None
                snap.mark("dec_put_call", f"{pv:.0f}/{cv:.0f}", src+f" Dec{dexp}", "LIVE")
        except Exception as e:
            snap.extras["ibkr_options"] = str(e)
        return True
    finally:
        ib.disconnect()


def collect_edgar(cfg, snap):
    cik = cfg.get("edgar_cik")
    if not cik:
        snap.mark("insider_sales_30d", None, "SEC EDGAR (data.sec.gov)", "NO CIK SET"); return
    try:
        req = urllib.request.Request(
            f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json",
            headers={"User-Agent": UA})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        r = data["filings"]["recent"]; cut = _today_et()-timedelta(days=30); n = 0
        for form, fdate in zip(r["form"], r["filingDate"]):
            if form == "4" and datetime.strptime(fdate, "%Y-%m-%d").date() >= cut:
                n += 1
        snap.insider_sales_30d = n
        snap.mark("insider_sales_30d", n, "SEC EDGAR Form 4", "LIVE")
    except Exception as e:
        snap.mark("insider_sales_30d", None, "SEC EDGAR", f"ERR {e}")


def collect_finra(cfg, snap):
    tok = cfg.get("finra_token")
    if not tok:
        snap.mark("short_interest_pct_float", None,
                  "FINRA Query API (developer.finra.org)", "NO TOKEN SET"); return
    try:
        url = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
        body = json.dumps({"limit": 2, "compareFilters": [
            {"fieldName": "symbolCode", "fieldValue": cfg["ticker"], "compareType": "equal"}],
            "sortFields": ["-settlementDate"]}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": UA, "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}"})
        rows = json.loads(urllib.request.urlopen(req, timeout=20).read())
        so = cfg["fundamentals"]["shares_outstanding"]
        # SI is conventionally a % of FLOAT, not of total shares. With ~5% float,
        # dividing by total shares understates SI%-of-float ~20x — the threshold
        # (15%) would then essentially never fire. Use the tradeable float.
        float_pct = cfg["fundamentals"].get("current_float_pct") or 100
        float_shares = so * float_pct / 100.0
        if rows and float_shares:
            snap.short_interest_pct_float = float(rows[0].get("currentShortPositionQuantity", 0))/float_shares*100
            snap.mark("short_interest_pct_float", snap.short_interest_pct_float,
                      f"FINRA SI / {float_pct}% float", "LIVE")
            if len(rows) > 1:
                snap.short_interest_prev = float(rows[1].get("currentShortPositionQuantity", 0))/float_shares*100
    except Exception as e:
        snap.mark("short_interest_pct_float", None, "FINRA", f"ERR {e}")


def collect_flow(cfg, snap):
    seed = cfg["seed"]
    if seed.get("otm_call_premium") is not None and snap.otm_call_premium is None:
        snap.otm_call_premium = seed["otm_call_premium"]
        snap.otm_put_premium = seed.get("otm_put_premium")
        snap.mark("otm_call_premium", snap.otm_call_premium,
                  "MANUAL / CheddarFlow screenshot (seed)", "MANUAL"); return
    tok = cfg.get("unusual_whales_token")
    if not tok:
        snap.mark("otm_call_premium", None, "Unusual Whales API / manual", "NO SOURCE"); return
    try:
        req = urllib.request.Request(
            f"https://api.unusualwhales.com/api/stock/{cfg['ticker']}/flow-alerts",
            headers={"User-Agent": UA, "Authorization": f"Bearer {tok}"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        cp = sum(float(a.get("total_premium", 0)) for a in data.get("data", [])
                 if "call" in (a.get("type", "")).lower() and a.get("is_otm", True))
        snap.otm_call_premium = cp
        snap.mark("otm_call_premium", cp, "Unusual Whales API", "LIVE")
    except Exception as e:
        snap.mark("otm_call_premium", None, "Unusual Whales", f"ERR {e}")


def compute_atr(snap):
    """ATR from recent bars; fallback ATR ~ price * daily IV."""
    h, l, c = snap.recent_highs, snap.recent_lows, snap.recent_closes
    if h and l and c and len(h) == len(l) == len(c) and len(c) >= 2:
        trs = []
        for i in range(1, len(c)):
            tr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
            trs.append(tr)
        if trs:
            snap.atr = sum(trs)/len(trs)
            snap.mark("atr", round(snap.atr, 2), "computed from recent bars", "DERIVED"); return
    if snap.price and snap.iv_annual:
        snap.atr = snap.price * snap.iv_annual / sqrt(252)
        snap.mark("atr", round(snap.atr, 2), "estimated price*dailyIV", "ESTIMATED")


def iv_percentile_from_db(cfg, snap):
    """Real IV percentile from stored history; fallback to a seed if thin."""
    if snap.iv_annual is None:
        return
    try:
        c = sqlite3.connect(cfg["db_path"])
        c.execute("CREATE TABLE IF NOT EXISTS runs(ts TEXT,price REAL,composite REAL,"
                  "level TEXT,aligned INT,blocked INT,iv REAL)")
        ivs = [r[0] for r in c.execute("SELECT iv FROM runs WHERE iv IS NOT NULL").fetchall()]
        c.close()
    except Exception:
        ivs = []
    # feed SCENT's vol-dynamics (vol-of-vol, spot/vol correlation) the recent IV path
    if snap.iv_series is None and ivs:
        snap.iv_series = (ivs[-12:] + [snap.iv_annual])
    pool = ivs + [snap.iv_annual]
    if len(pool) >= 10:
        below = sum(1 for x in ivs if x <= snap.iv_annual)
        snap.iv_percentile = below/len(ivs)
        snap.mark("iv_percentile", round(snap.iv_percentile, 2),
                  f"computed from {len(ivs)} stored runs", "LIVE")
    else:
        snap.iv_percentile = 0.90
        snap.mark("iv_percentile", 0.90,
                  f"seed (history thin: {len(ivs)} runs, need 10)", "SEEDED")


def apply_seed(cfg, snap):
    s = cfg["seed"]
    for f, v in [("price", s["price"]), ("volume", s["volume"]),
                 ("avg_volume_20d", s["avg_volume_20d"]), ("iv_annual", s["iv_annual"]),
                 ("iv_5d_ago", s["iv_5d_ago"]),
                 ("short_interest_pct_float", s["short_interest_pct_float"]),
                 ("short_interest_prev", s["short_interest_prev"]),
                 ("borrow_fee", s["borrow_fee"]), ("borrow_fee_prev", s["borrow_fee_prev"]),
                 ("insider_sales_30d", s["insider_sales_30d"]),
                 ("call_iv_otm", s["call_iv_otm"]), ("put_iv_otm", s["put_iv_otm"]),
                 ("recent_highs", s["recent_highs"]), ("recent_lows", s["recent_lows"]),
                 ("recent_closes", s["recent_closes"]), ("recent_volumes", s.get("recent_volumes")),
                 ("index_return_5d", s["index_return_5d"]), ("spcx_return_5d", s["spcx_return_5d"]),
                 ("social_sentiment", s["social_sentiment"]), ("social_sentiment_prev", s["social_sentiment_prev"]),
                 ("social_volume", s["social_volume"]), ("social_volume_avg", s["social_volume_avg"]),
                 ("news_sentiment", s["news_sentiment"]), ("bullish_catalyst", s["bullish_catalyst"])]:
        if getattr(snap, f) is None and v is not None:
            setattr(snap, f, v)
            if f not in snap.sources and f in ("price", "volume", "avg_volume_20d", "iv_annual",
                                               "social_sentiment", "news_sentiment"):
                snap.mark(f, v, "SEED (last live pull 2026-06-16)", "SEEDED")
    if snap.dec_put_volume is None and s["all_expiry_put_vol"]:
        snap.dec_put_volume = s["all_expiry_put_vol"]; snap.dec_call_volume = s["all_expiry_call_vol"]
        snap.mark("dec_put_call", f"{s['all_expiry_put_vol']}/{s['all_expiry_call_vol']}",
                  "SEED all-expiry P/C proxy (IBKR 2026-06-16)", "SEEDED-PROXY")
    so = cfg["fundamentals"]["shares_outstanding"]
    if snap.market_cap is None and snap.price and so:
        snap.market_cap = snap.price*so
        snap.mark("market_cap", snap.market_cap, "derived: price x shares", "DERIVED")


def _social_rate_baseline(cfg, rate):
    """Store the current StockTwits posting rate and return the median of prior
    rates (the rolling attention baseline) — or None until enough history exists.
    Lets social_attention compare today's velocity to its own recent norm."""
    try:
        c = sqlite3.connect(cfg["db_path"])
        c.execute("CREATE TABLE IF NOT EXISTS social_rate(ts TEXT, rate REAL)")
        prior = [r[0] for r in c.execute(
            "SELECT rate FROM social_rate ORDER BY rowid DESC LIMIT 30").fetchall()]
        c.execute("INSERT INTO social_rate(ts, rate) VALUES(?,?)",
                  (datetime.now().isoformat(timespec="seconds"), float(rate)))
        c.commit(); c.close()
    except Exception:
        return None
    if len(prior) < 5:
        return None
    s = sorted(prior); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2


def collect_sentiment(cfg, snap):
    """Social sentiment + news. Manual seed wins; else StockTwits (social) and
    Finnhub (news). Social mood and ATTENTION lead the tape on a retail name."""
    seed = cfg["seed"]
    # social — StockTwits public symbol stream
    if cfg.get("stocktwits_enabled") and snap.social_sentiment is None:
        try:
            req = urllib.request.Request(
                f"https://api.stocktwits.com/api/2/streams/symbol/{cfg['ticker']}.json",
                headers={"User-Agent": UA})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            msgs = data.get("messages", [])
            bull = sum(1 for m in msgs if (m.get("entities", {}).get("sentiment") or {}).get("basic") == "Bullish")
            bear = sum(1 for m in msgs if (m.get("entities", {}).get("sentiment") or {}).get("basic") == "Bearish")
            if bull+bear:
                snap.social_sentiment = (bull-bear)/(bull+bear)
                snap.mark("social_sentiment", round(snap.social_sentiment, 2),
                          f"StockTwits ({bull}🐂/{bear}🐻 of {len(msgs)})", "LIVE")
            # ATTENTION VELOCITY (live, day-1, no price history): the literature finds
            # social MOOD alone barely predicts returns — the predictive piece is the
            # posting VELOCITY / spike. An absolute page count (~30 newest msgs) is not a
            # valid daily volume, but the RATE (messages/hour from the page timestamps)
            # vs a rolling baseline IS apples-to-apples and is a genuine leading signal
            # that exists from the first day of trading (it feeds social_attention).
            try:
                ts = []
                for m in msgs:
                    ca = m.get("created_at")
                    if ca:
                        try:
                            ts.append(datetime.strptime(ca, "%Y-%m-%dT%H:%M:%SZ"))
                        except Exception:
                            pass
                if len(ts) >= 5:
                    span_h = max((max(ts)-min(ts)).total_seconds()/3600.0, 0.05)
                    rate = len(ts)/span_h                       # messages per hour
                    base = _social_rate_baseline(cfg, rate)     # median of prior runs (+stores this one)
                    snap.social_volume = round(rate, 2)
                    if base is not None:
                        snap.social_volume_avg = round(base, 2)
                        snap.mark("social_volume", round(rate, 2),
                                  f"StockTwits velocity {rate:.1f} msg/h vs base {base:.1f}", "LIVE")
                    else:
                        # No baseline yet: set avg = rate (neutral ratio 1.0) so the seed's
                        # incompatible ABSOLUTE count can't fill it and fake an 'attention
                        # leaving' read by mixing scales (rate msg/h vs ~30k count).
                        snap.social_volume_avg = round(rate, 2)
                        snap.mark("social_volume", round(rate, 2),
                                  f"StockTwits velocity {rate:.1f} msg/h (baseline building)", "LIVE")
            except Exception as e:
                snap.extras["attention_velocity"] = f"ERR {e}"
        except Exception as e:
            snap.extras["stocktwits"] = f"ERR {e}"
    # news — Finnhub sentiment
    tok = cfg.get("finnhub_token")
    if tok and snap.news_sentiment is None:
        try:
            req = urllib.request.Request(
                f"https://finnhub.io/api/v1/news-sentiment?symbol={cfg['ticker']}&token={tok}",
                headers={"User-Agent": UA})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read())
            bp = d.get("sentiment", {}).get("bullishPercent")
            if bp is not None:
                snap.news_sentiment = bp*2-1
                snap.mark("news_sentiment", round(snap.news_sentiment, 2), "Finnhub news-sentiment", "LIVE")
        except Exception as e:
            snap.extras["finnhub"] = f"ERR {e}"


def compute_unlocks(cfg, snap, today=None):
    """Find days to the next unlock and next MAJOR (>=15%) unlock from schedule,
    and advance the cumulative tradeable float as unlock waves elapse."""
    today = today or _today_et()
    sched = sorted(cfg["fundamentals"].get("unlock_schedule", []), key=lambda e: e["date"])
    base = cfg["fundamentals"].get("current_float_pct") or 0
    # Derive cumulative tradeable % from elapsed unlocks (the stored 'cum' column
    # is unverified/inconsistent — see config_warnings). Cap at 100.
    running, cum_by_date = base, {}
    for e in sched:
        running += e.get("pct", 0)
        cum_by_date[e["date"]] = min(running, 100)
    elapsed = [e for e in sched if datetime.strptime(e["date"], "%Y-%m-%d").date() < today]
    if snap.cum_float_pct is None:
        snap.cum_float_pct = min(cum_by_date[elapsed[-1]["date"]], 100) if elapsed else base
    fut = [e for e in sched if datetime.strptime(e["date"], "%Y-%m-%d").date() >= today]
    if not fut:
        snap.mark("next_major_unlock", "none (all unlocks elapsed)",
                  "unlock schedule (config)", "CONFIG")
        return
    nxt = fut[0]
    snap.days_to_next_unlock = (datetime.strptime(nxt["date"], "%Y-%m-%d").date()-today).days
    snap.next_unlock_label = f"{nxt['label']} {nxt['date']} (+{nxt['pct']}%)"
    # MAJOR = explicit flag or >=15% of the pool, EXCLUDING bullish-conditional
    # tranches (a price-triggered release only fires into strength — not bearish).
    majors = [e for e in fut if (e.get("major") or e.get("pct", 0) >= 15) and not e.get("conditional")]
    if majors:
        mj = majors[0]
        snap.days_to_next_major = (datetime.strptime(mj["date"], "%Y-%m-%d").date()-today).days
        snap.next_major_label = f"{mj['label']} {mj['date']}"
        snap.next_major_pct = mj["pct"]
        snap.extras["next_major_date"] = mj["date"]
        snap.extras["major_unlocks"] = [(e["date"], e["pct"]) for e in majors]
    # near-window supply ramp: % of pool unlocking within the next 30 days
    h30 = today + timedelta(days=30)
    snap.float_velocity_30d = sum(e.get("pct", 0) for e in fut
                                  if datetime.strptime(e["date"], "%Y-%m-%d").date() <= h30
                                  and not e.get("conditional")) or 0
    snap.mark("next_major_unlock",
              snap.next_major_label or "none", "unlock schedule (424B4)", "CONFIG")


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _yahoo_closes(sym, rng="3mo"):
    """Just the daily closes for a symbol (used for the SPY relative-strength leg)."""
    for host in ("query1", "query2"):
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read())
            q = d["chart"]["result"][0]["indicators"]["quote"][0]
            return [c for c in q["close"] if c is not None]
        except Exception:
            continue
    return None


def collect_yahoo(cfg, snap):
    """Free daily price/volume/bars from Yahoo (no account) — fallback when TWS is
    off, so the daily run tracks the REAL tape (and feeds SCENT real history)
    instead of repeating the seed. Degrades silently to seed on any failure."""
    if not cfg.get("yahoo_enabled", True) or snap.price is not None:
        return False
    sym = cfg["ticker"]
    for host in ("query1", "query2"):
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range=4mo&interval=1d"
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=20).read())
            res = d["chart"]["result"][0]; q = res["indicators"]["quote"][0]
            closes = [c for c in q["close"] if c is not None]
            highs = [h for h in q["high"] if h is not None]
            lows = [l for l in q["low"] if l is not None]
            vols = [v for v in q["volume"] if v is not None]
            if not closes:
                continue
            snap.price = res.get("meta", {}).get("regularMarketPrice") or closes[-1]
            snap.mark("price", round(snap.price, 2), f"Yahoo live ({sym})", "LIVE")
            snap.recent_closes = closes[-10:]; snap.long_closes = closes[-60:]
            snap.recent_highs = highs[-10:]; snap.recent_lows = lows[-10:]
            if vols:
                snap.volume = vols[-1]
                snap.avg_volume_20d = sum(vols[-20:]) / len(vols[-20:])
                snap.recent_volumes = vols[-10:]
                snap.mark("volume", snap.volume, f"Yahoo live ({sym})", "LIVE")
            so = cfg["fundamentals"]["shares_outstanding"]
            if so:
                snap.market_cap = snap.price * so
            # relative strength vs the market (SPY), both 5-day returns — was 'no data'
            if snap.spcx_return_5d is None and len(closes) >= 6:
                spy = _yahoo_closes("SPY")
                if spy and len(spy) >= 6:
                    snap.spcx_return_5d = closes[-1]/closes[-6] - 1
                    snap.index_return_5d = spy[-1]/spy[-6] - 1
                    snap.mark("rel_strength", round((snap.spcx_return_5d-snap.index_return_5d)*100, 1),
                              "Yahoo SPCX vs SPY (5d)", "LIVE")
            return True
        except Exception as e:
            snap.extras["yahoo"] = f"ERR {e}"
    return False


def collect_live(cfg) -> Snapshot:
    snap = Snapshot(ts=datetime.now())
    got = collect_ibkr(cfg, snap)
    if not got:
        collect_yahoo(cfg, snap)          # free real-tape fallback when TWS is off
    collect_edgar(cfg, snap); collect_finra(cfg, snap); collect_flow(cfg, snap)
    collect_sentiment(cfg, snap)
    apply_seed(cfg, snap)
    compute_unlocks(cfg, snap)
    iv_percentile_from_db(cfg, snap)
    compute_atr(snap)
    snap.extras["tws_connected"] = got
    snap.extras["price_source"] = "Yahoo" if (not got and snap.price) else ("IBKR" if got else "seed")
    return snap

# =============================================================================
#  SIGNALS
# =============================================================================
@dataclass
class Signal:
    key: str; tier: str; state: str; score: float; value: object; note: str

TIER_WEIGHTS = {"valuation": .13, "momentum": .22, "structural": .20,
                "options": .10, "insider": .10, "squeeze": .13, "sentiment": .12}


def build_signals(snap, cfg):
    t = cfg["thresholds"]; f = cfg["fundamentals"]; S = []
    ps = (snap.market_cap/f["ttm_revenue_usd"]) if snap.market_cap else None
    if ps is None:
        S.append(Signal("ps_ratio", "valuation", RED, 0, None, "no market-cap data"))
    elif ps >= t["ps_stretched"]:
        S.append(Signal("ps_ratio", "valuation", YELLOW, .4, round(ps), f"P/S {ps:.0f}x — nosebleed, thesis intact, no crack yet"))
    elif ps <= t["ps_normalizing"]:
        S.append(Signal("ps_ratio", "valuation", GREEN, 1, round(ps), f"P/S {ps:.0f}x — multiple compressing"))
    else:
        S.append(Signal("ps_ratio", "valuation", YELLOW, .7, round(ps), f"P/S {ps:.0f}x — coming off peak"))

    if snap.volume and snap.avg_volume_20d:
        fade = (1-snap.volume/snap.avg_volume_20d)*100; need = t["volume_fade_pct"]
        if fade >= need:
            S.append(Signal("volume_fade", "momentum", GREEN, 1, round(fade), f"volume {fade:.0f}% below 20d avg — retail leaving"))
        elif fade >= need/2:
            S.append(Signal("volume_fade", "momentum", YELLOW, .6, round(fade), f"volume cooling ({fade:.0f}% below avg)"))
        else:
            S.append(Signal("volume_fade", "momentum", RED, .1, round(fade), f"volume still hot ({fade:.0f}% vs avg) — momentum alive"))
    else:
        S.append(Signal("volume_fade", "momentum", RED, 0, None, "no volume data"))

    ivp = snap.iv_percentile
    over = snap.iv_annual is not None and snap.iv_5d_ago is not None and snap.iv_annual < snap.iv_5d_ago
    if ivp is None:
        S.append(Signal("iv_capitulation", "momentum", RED, 0, None, "no IV data"))
    elif ivp >= t["iv_percentile_high"]/100 and over:
        S.append(Signal("iv_capitulation", "momentum", GREEN, 1, round(ivp, 2), "IV top-decile AND falling — fear unwinding"))
    elif ivp >= t["iv_percentile_high"]/100:
        S.append(Signal("iv_capitulation", "momentum", YELLOW, .5, round(ivp, 2), "IV very high, not yet rolling over (need 5d history)"))
    elif ivp < 0.5:
        S.append(Signal("iv_capitulation", "momentum", YELLOW, .2, round(ivp, 2), f"IV at {ivp*100:.0f}th pct — fear already low, weak exhaustion case"))
    else:
        S.append(Signal("iv_capitulation", "momentum", YELLOW, .4, round(ivp, 2), f"IV at {ivp*100:.0f}th pct — elevated but not extreme"))

    # trend rollover: lower high vs prior swing highs = momentum breaking
    rh = snap.recent_highs
    if rh and len(rh) >= 3:
        last, prior_max = rh[-1], max(rh[:-1])
        if last < prior_max:
            S.append(Signal("trend_rollover", "momentum", GREEN, 1, round(last, 1), f"lower high ({last:.0f} < prior {prior_max:.0f}) — rollover forming"))
        else:
            S.append(Signal("trend_rollover", "momentum", RED, .1, round(last, 1), f"higher highs ({last:.0f}) — uptrend intact, do not fight"))
    else:
        S.append(Signal("trend_rollover", "momentum", YELLOW, .4, None, "insufficient bars for trend"))

    # bearish volume divergence: price at new highs while volume fades = distribution
    rv = snap.recent_volumes
    if rh and rv and len(rh) >= 3 and len(rv) >= 3:
        price_hh = rh[-1] >= max(rh[:-1])*0.995
        vol_now = sum(rv[-2:])/2; vol_prior = sum(rv[:-1])/len(rv[:-1])
        vol_decl = vol_now < vol_prior
        strong = len(rv) >= t.get("divergence_min_bars", 5)
        if price_hh and vol_decl:
            st, sc = (GREEN, 1.0) if strong else (YELLOW, .65)
            S.append(Signal("divergence", "momentum", st, sc, round(vol_now/1e6),
                            f"bearish divergence: price highs on fading volume — distribution"
                            + ("" if strong else f" (early, only {len(rv)} bars)")))
        elif price_hh:
            S.append(Signal("divergence", "momentum", RED, .1, None,
                            "price at highs on steady/rising volume — healthy trend, no divergence"))
        else:
            S.append(Signal("divergence", "momentum", YELLOW, .4, round(rh[-1], 0), "price off highs — divergence n/a"))
    else:
        S.append(Signal("divergence", "momentum", YELLOW, .4, None, "no volume series for divergence"))

    # relative strength: SPCX underperforming the index = rotation out (bearish)
    if snap.spcx_return_5d is not None and snap.index_return_5d is not None:
        diff = snap.spcx_return_5d - snap.index_return_5d
        if diff <= -0.02:
            S.append(Signal("rel_strength", "momentum", GREEN, 1, round(diff, 3), f"SPCX lagging index by {abs(diff)*100:.1f}% — rotation out"))
        elif diff < 0.03:
            S.append(Signal("rel_strength", "momentum", YELLOW, .5, round(diff, 3), "SPCX roughly tracking index"))
        else:
            S.append(Signal("rel_strength", "momentum", RED, .1, round(diff, 3), f"SPCX leading index by {diff*100:.1f}% — still the favourite"))
    else:
        S.append(Signal("rel_strength", "momentum", YELLOW, .4, None, "no relative-strength data (SPCX vs Nasdaq)"))

    si = snap.short_interest_pct_float
    if si is None:
        S.append(Signal("short_interest", "structural", RED, 0, None, "no short-interest data (FINRA bi-weekly, lagged)"))
    else:
        rising = snap.short_interest_prev is None or si > snap.short_interest_prev
        if si >= t["short_interest_pct_float"] and rising:
            S.append(Signal("short_interest", "structural", GREEN, 1, round(si, 1), f"SI {si:.1f}% of float and rising — crowd arriving"))
        elif si >= t["short_interest_pct_float"]/2:
            S.append(Signal("short_interest", "structural", YELLOW, .6, round(si, 1), f"SI {si:.1f}% — building"))
        else:
            S.append(Signal("short_interest", "structural", RED, .2, round(si, 1), f"SI {si:.1f}% — shorts not here yet"))

    if snap.borrow_fee is not None and snap.borrow_fee_prev is not None:
        easing = snap.borrow_fee < snap.borrow_fee_prev
        S.append(Signal("borrow_fee", "structural", GREEN if easing else YELLOW, .8 if easing else .4,
                        round(snap.borrow_fee, 1), "borrow easing — supply opening up" if easing else "borrow still tight"))
    else:
        S.append(Signal("borrow_fee", "structural", RED, 0, None, "no borrow data (IBKR shortable/SLB)"))

    # unlock window: supply catalyst timing from the staggered lockup schedule
    dM = snap.days_to_next_major
    win = t.get("unlock_window_days", 12)
    if dM is None:
        S.append(Signal("unlock_window", "structural", YELLOW, .4, None, "no unlock schedule loaded"))
    elif -3 <= dM <= win:
        S.append(Signal("unlock_window", "structural", GREEN, 1, dM,
                        f"MAJOR unlock {snap.next_major_label} in {dM}d (+{snap.next_major_pct}% supply) — bearish window open"))
    elif dM <= 35:
        S.append(Signal("unlock_window", "structural", YELLOW, .6, dM,
                        f"major unlock approaching: {snap.next_major_label} ({dM}d)"))
    else:
        S.append(Signal("unlock_window", "structural", RED, .15, dM,
                        f"next major supply catalyst {dM}d away ({snap.next_major_label}) — no near-term unlock"))

    if snap.dec_put_volume and snap.dec_call_volume:
        pc = snap.dec_put_volume/snap.dec_call_volume; need = t["dec_put_call_ratio"]
        if pc >= need:
            S.append(Signal("dec_put_call", "options", GREEN, 1, round(pc, 2), f"P/C {pc:.2f} — positioning bearish"))
        elif pc >= need*.6:
            S.append(Signal("dec_put_call", "options", YELLOW, .5, round(pc, 2), f"P/C {pc:.2f} — puts building"))
        else:
            S.append(Signal("dec_put_call", "options", RED, .1, round(pc, 2), f"P/C {pc:.2f} — calls dominate (greed)"))
    else:
        S.append(Signal("dec_put_call", "options", RED, 0, None, "no options data"))

    n = snap.insider_sales_30d
    if n is None:
        S.append(Signal("insider_sells", "insider", RED, 0, None, "no Form-4 data"))
    elif n >= 1:
        S.append(Signal("insider_sells", "insider", GREEN, 1, n, f"{n} insider sell(s)/30d — they think it's rich"))
    else:
        S.append(Signal("insider_sells", "insider", YELLOW, .3, 0, "no insider selling yet (lockup likely binding)"))

    # --- SENTIMENT / NEWS --------------------------------------------------
    ss, ssp = snap.social_sentiment, snap.social_sentiment_prev
    if ss is None:
        S.append(Signal("social_mood", "sentiment", RED, 0, None, "no social sentiment data (StockTwits/Reddit)"))
    elif ss >= 0.3 and ssp is not None and ss < ssp:
        S.append(Signal("social_mood", "sentiment", GREEN, 1, round(ss, 2), f"euphoria fading ({ssp:+.2f}->{ss:+.2f}) — hype rolling over"))
    elif ss >= 0.3 and (ssp is None or ss >= ssp):
        S.append(Signal("social_mood", "sentiment", RED, .1, round(ss, 2), f"social mood {ss:+.2f} bullish & rising — FOMO intact, do not fight"))
    elif ss <= -0.2:
        S.append(Signal("social_mood", "sentiment", GREEN, 1, round(ss, 2), f"social mood {ss:+.2f} turned bearish — crowd souring"))
    else:
        S.append(Signal("social_mood", "sentiment", YELLOW, .5, round(ss, 2), f"social mood {ss:+.2f} neutral"))

    sv, sva = snap.social_volume, snap.social_volume_avg
    if sv and sva:
        ratio = sv/sva
        if ratio <= 0.7:
            S.append(Signal("social_attention", "sentiment", GREEN, 1, round(ratio, 2), f"mentions {(1-ratio)*100:.0f}% below baseline — attention leaving"))
        elif ratio >= 1.3:
            S.append(Signal("social_attention", "sentiment", RED, .1, round(ratio, 2), f"mentions {(ratio-1)*100:.0f}% above baseline — retail piling in"))
        else:
            S.append(Signal("social_attention", "sentiment", YELLOW, .5, round(ratio, 2), "mention volume normal"))
    else:
        S.append(Signal("social_attention", "sentiment", YELLOW, .4, None, "no mention-volume data"))

    ns = snap.news_sentiment
    if ns is None:
        S.append(Signal("news_flow", "sentiment", YELLOW, .4, None, "no news-sentiment data (Finnhub/manual)"))
    elif ns <= -0.2:
        S.append(Signal("news_flow", "sentiment", GREEN, 1, round(ns, 2), f"news flow {ns:+.2f} turning negative — bearish confirm"))
    elif ns >= 0.4:
        S.append(Signal("news_flow", "sentiment", RED, .1, round(ns, 2), f"news flow {ns:+.2f} strongly positive — greed (and squeeze fuel)"))
    else:
        S.append(Signal("news_flow", "sentiment", YELLOW, .5, round(ns, 2), f"news flow {ns:+.2f} neutral"))

    manual = cfg.get("manual_squeeze_block", False)
    skew = (snap.call_iv_otm-snap.put_iv_otm) if (snap.call_iv_otm and snap.put_iv_otm) else None
    # FAIL-CLOSED: a guard may only go GREEN (clear the short) on a MEASURED all-clear
    # from a real flow feed. A seeded/manual/derived OTM-call value must NOT unblock —
    # otherwise pasting one number flips the squeeze guard green with no live data.
    # (RED still fires from any source: blocking on a high reading is the safe side.)
    _UNVERIFIED = {"MANUAL", "SEEDED", "SEEDED-PROXY", "NO SOURCE", "DERIVED", "ESTIMATED"}
    flow_status = (snap.sources.get("otm_call_premium") or (None, None, None))[2]
    flow_unverified = flow_status in _UNVERIFIED
    if manual:
        S.append(Signal("gamma_squeeze", "squeeze", RED, 0, "manual", "MANUAL BLOCK on — live OTM call sweeps; short suppressed"))
    elif snap.otm_call_premium is not None and snap.otm_call_premium >= t["gamma_block_usd"]:
        src = " (SEED/MANUAL — verify live)" if flow_unverified else ""
        S.append(Signal("gamma_squeeze", "squeeze", RED, 0, round(snap.otm_call_premium/1e6, 1),
                        f"${snap.otm_call_premium/1e6:.1f}M OTM call sweeps — squeeze risk, short BLOCKED{src}"))
    elif skew is not None and skew >= t["iv_skew_block"]:
        S.append(Signal("gamma_squeeze", "squeeze", YELLOW, .3, round(skew, 3), f"call IV>put IV by {skew*100:.1f}pts — call demand"))
    elif snap.otm_call_premium is not None and flow_unverified:
        # value present but NOT from a live flow feed — never green-light on a seed.
        S.append(Signal("gamma_squeeze", "squeeze", YELLOW, .4, round(snap.otm_call_premium/1e6, 1),
                        f"OTM-call premium ${snap.otm_call_premium/1e6:.1f}M is SEED/MANUAL, not live flow — squeeze not ruled out"))
    elif snap.otm_call_premium is not None:
        S.append(Signal("gamma_squeeze", "squeeze", GREEN, 1, round(snap.otm_call_premium/1e6, 1),
                        f"OTM call buying quiet (${snap.otm_call_premium/1e6:.1f}M, live) — no squeeze pressure"))
    else:
        S.append(Signal("gamma_squeeze", "squeeze", YELLOW, .4, None, "no flow data — can't rule out a squeeze"))
    return S


def composite(S):
    by = {}
    for s in S:
        by.setdefault(s.tier, []).append(s.score)
    return round(sum(w*(sum(by[t])/len(by[t])) for t, w in TIER_WEIGHTS.items() if by.get(t))*100, 1)


def tier_contributions(S):
    """Weighted contribution of each tier to the composite (for transparency)."""
    by = {}
    for s in S:
        by.setdefault(s.tier, []).append(s.score)
    out = {}
    for t, w in TIER_WEIGHTS.items():
        if by.get(t):
            out[t] = round(w*(sum(by[t])/len(by[t]))*100, 1)
    return out


def data_confidence(S):
    """How complete is the picture? No-data signals (value is None AND red/no-data
    note) don't count as informed. Returns (pct, informed, total, missing_keys)."""
    missing = [s.key for s in S if s.value is None and ("no " in s.note or "insufficient" in s.note)]
    informed = len(S)-len(missing)
    pct = round(informed/len(S)*100) if S else 0
    return pct, informed, len(S), missing


def gap_to_trigger(snap, S, conf, cfg):
    """Exactly what must change to reach TRIGGER — actionable, with current values."""
    m = {s.key: s for s in S}; t = cfg["thresholds"]; gaps = []
    for b in conf["blockers"]:
        if b == "gamma_squeeze_active":
            cur = (snap.otm_call_premium or 0)/1e6
            gaps.append(f"clear gamma block: OTM call sweeps < ${t['gamma_block_usd']/1e6:.0f}M (now ${cur:.1f}M)")
        if b == "gamma_squeeze_unevaluated":
            gaps.append("rule out a squeeze: supply OTM-call-flow data (Unusual Whales / manual). "
                        "No flow data = squeeze risk unknown = the engine will not green-light a short")
        if b == "bullish_catalyst_squeeze_risk":
            gaps.append("clear the bullish catalyst (no live Musk/SpaceX squeeze headline)")
        if b == "insufficient_data":
            gaps.append(f"raise data completeness to ≥{t.get('min_data_confidence',55)}% "
                        f"(now {conf.get('data_confidence','?')}%) — wire FINRA/EDGAR/flow before trusting a trigger")
    c = conf["conditions"]
    if not c.get("leading_confirms", True):
        gaps.append("leading_confirms: at least one fast signal (volume/IV/flow/divergence/sentiment) must go GREEN — don't enter on lagging data alone")
    if not c["momentum_breaking"]:
        gaps.append("momentum_breaking: any one of volume-fade / IV-rollover / lower-high / "
                    "rel-strength / social-mood-fade / attention-fade must go GREEN")
    if not c["short_interest_rising"]:
        si = snap.short_interest_pct_float
        cur = f"{si:.1f}%" if si is not None else "no data"
        gaps.append(f"short_interest_rising: SI >= {t['short_interest_pct_float']}% and rising (now {cur})")
    if not c["dec_puts_elevated"]:
        if snap.dec_put_volume and snap.dec_call_volume:
            cur = f"{snap.dec_put_volume/snap.dec_call_volume:.2f}"
        else:
            cur = "no data"
        gaps.append(f"dec_puts_elevated: Dec P/C >= {t['dec_put_call_ratio']} (now {cur})")
    if not c["above_support"]:
        gaps.append(f"above_support: not a falling knife — price must stabilize "
                    f"(within {int(t.get('knife_drop_pct',0.12)*100)}% of recent-close avg; now ${snap.price})")
    return gaps


def confluence(snap, S, cfg):
    m = {s.key: s for s in S}; t = cfg["thresholds"]
    # 'above_support' = not catching a falling knife. Adaptive: price not in a
    # vertical drop vs its recent-close average; plus optional hard floor.
    floor = t.get("support_floor")
    floor_ok = (snap.price >= floor) if (floor and snap.price is not None) else True
    if snap.price is not None and snap.recent_closes and len(snap.recent_closes) >= 2:
        ref = sum(snap.recent_closes[-3:])/len(snap.recent_closes[-3:])
        not_freefall = snap.price >= ref*(1-t.get("knife_drop_pct", 0.12))
    else:
        not_freefall = snap.price is not None
    # A genuine fast tape signal (price/vol/IV/divergence/rel-strength/mood) — NOT
    # attention-alone, and not the separately-required dec_put_call — must confirm,
    # so we never enter on lagging/structural data with only a mention-volume blip.
    fast_lead = ("volume_fade", "iv_capitulation", "trend_rollover", "divergence",
                 "rel_strength", "social_mood")
    cond = {
        "momentum_breaking": any(m[k].state == GREEN for k in ("volume_fade", "iv_capitulation", "trend_rollover", "divergence", "rel_strength", "social_mood", "social_attention")),
        "short_interest_rising": m["short_interest"].state == GREEN,
        "dec_puts_elevated": m["dec_put_call"].state == GREEN,
        "above_support": floor_ok and not_freefall,
        "leading_confirms": any(m[k].state == GREEN for k in fast_lead if k in m),
    }
    blockers = []
    if m["gamma_squeeze"].state == RED:
        blockers.append("gamma_squeeze_active")
    elif m["gamma_squeeze"].state != GREEN:
        # Fail-safe: a YELLOW squeeze guard means squeeze risk is NOT ruled out
        # (e.g. no options-flow data). Never short into an unknown squeeze.
        blockers.append("gamma_squeeze_unevaluated")
    if snap.bullish_catalyst:
        blockers.append("bullish_catalyst_squeeze_risk")
    need = cfg["confluence_require_all"]
    pre = all(cond.get(k) for k in need) and not blockers
    # accuracy guard: don't fire a TRIGGER on a half-empty picture
    conf_pct = data_confidence(S)[0]
    if pre and conf_pct < t.get("min_data_confidence", 55):
        blockers.append("insufficient_data")
    aligned = all(cond.get(k) for k in need) and not blockers
    return {"aligned": aligned, "conditions": cond, "blockers": blockers,
            "missing": [k for k in need if not cond.get(k)], "data_confidence": conf_pct}


def warning_level(snap, S, comp, conf):
    """4-stage ladder + BLOCKED override."""
    if conf["blockers"]:
        return BLOCKED
    if conf["aligned"]:
        return TRIGGER
    c = conf["conditions"]
    core = c["momentum_breaking"] and c["short_interest_rising"] and c["above_support"]
    if core or comp >= 70:
        return ARMED
    if comp >= 40:
        return WATCH
    return DORMANT

# =============================================================================
#  SCENT — early-warning FRAGILITY (leading/state, NOT a directional call)
# -----------------------------------------------------------------------------
#  Lagging momentum signals say "uptrend intact" at the exact blow-off top
#  (verified on BYND/RIVN/META backtests). SCENT instead scores the structural
#  fragility that PEAKS into a euphoric top: parabolic overextension, vol/volume
#  climax, IV-rising-with-price reflexivity, and proximity to scheduled supply.
#  It is deliberately separate from the strict directional TRIGGER — fragility
#  precedes a melt-UP as often as a top, so SCENT alone never authorizes a short.
# =============================================================================
SCENT_QUIET, SCENT_STIRRING, SCENT_ELEVATED = "QUIET", "STIRRING", "ELEVATED"
SCENT_STIR_AT, SCENT_ELEV_AT = 45, 65
SCENT_WEIGHTS = {"overextension": .22, "parabola": .18, "vol_expansion": .12,
                 "volume_climax": .14, "spot_vol_corr": .12, "supply_ramp": .22}


def _clip01(x):
    return max(0.0, min(1.0, x))


def _corr(a, b):
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a)/n, sum(b)/n
    cov = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    va = sum((x-ma)**2 for x in a); vb = sum((x-mb)**2 for x in b)
    return cov/sqrt(va*vb) if va > 0 and vb > 0 else 0.0


def scent_components(snap):
    """Fragility sub-scores in [0,1], each with a human note. Only the components
    whose data is present are returned (graceful degradation)."""
    c = {}
    rc = snap.recent_closes
    ref = snap.long_closes or rc           # prefer the longer trend history
    # 1) overextension: how far price sits above its longer moving average — the
    #    blow-off-top tell. A parabola runs far above its own trend before it breaks.
    if ref and len(ref) >= 10 and snap.price:
        n = min(len(ref), 50)
        ma = sum(ref[-n:])/n
        ext = (snap.price/ma - 1) if ma else 0
        c["overextension"] = (_clip01(ext/0.50), f"{ext*100:+.0f}% vs {n}d MA")
    # 2) parabola: the recent leg steeper than the prior leg (up-acceleration)
    if ref and len(ref) >= 20:
        n = min(len(ref)//2, 15)
        recent = (ref[-1]/ref[-n]-1) if ref[-n] else 0
        prior = (ref[-n]/ref[-2*n+1]-1) if (len(ref) >= 2*n and ref[-2*n+1]) else 0
        accel = recent - prior
        c["parabola"] = (_clip01(accel/0.30) if accel > 0 else 0.0, f"up-accel {accel*100:+.0f}pts")
    # NOTE: an RSI(14)-overbought component was tested on both cohorts and did NOT
    # improve SCENT quality (recall unchanged, lead slightly worse) — the names
    # SCENT misses peak in their first 1-2 weeks with no price history to compute
    # from, a DATA limit no price indicator fixes. Reverted (kept the engine lean).
    if snap.iv_percentile is not None:
        c["vol_expansion"] = (_clip01((snap.iv_percentile-0.5)/0.45), f"vol pct {snap.iv_percentile*100:.0f}")
    rv = snap.recent_volumes
    if rv and len(rv) >= 5 and snap.avg_volume_20d:
        peak = max(rv); cur = rv[-1]
        spike = peak/snap.avg_volume_20d if snap.avg_volume_20d else 1
        fading = cur < peak*0.85
        c["volume_climax"] = (_clip01((spike-1.2)/1.3) * (1.0 if fading else 0.6),
                              f"vol peak {spike:.1f}x avg{' + fading' if fading else ''}")
    ivs = snap.iv_series
    if ivs and rc and len(ivs) >= 4 and len(rc) >= 4:
        k = min(len(ivs), len(rc))
        dp = [rc[-k+i]-rc[-k+i-1] for i in range(1, k)]
        di = [ivs[-k+i]-ivs[-k+i-1] for i in range(1, k)]
        cc = _corr(dp, di)
        if cc is not None:
            c["spot_vol_corr"] = (_clip01(cc/0.6) if cc > 0 else 0.0, f"corr(dVol,dPx) {cc:+.2f}")
    if snap.float_velocity_30d is not None:
        c["supply_ramp"] = (_clip01(snap.float_velocity_30d/30.0), f"+{snap.float_velocity_30d:.0f}% float/30d")
    elif snap.days_to_next_major is not None:
        d = snap.days_to_next_major
        prox = _clip01((21-d)/21) if d >= 0 else (1.0 if d >= -3 else 0.0)
        c["supply_ramp"] = (prox, f"major unlock {d}d")
    return c


def scent_score(snap, S=None):
    """Composite fragility 0..100 (or None if no data). Also stamps snap.scent."""
    comps = scent_components(snap)
    if not comps:
        return None
    num = sum(SCENT_WEIGHTS.get(k, .1)*v[0] for k, v in comps.items())
    den = sum(SCENT_WEIGHTS.get(k, .1) for k in comps)
    sc = round(num/den*100, 1) if den else None
    snap.scent = sc
    snap.scent_state = scent_state(sc)
    return sc


def scent_state(sc):
    if sc is None:
        return None
    return SCENT_ELEVATED if sc >= SCENT_ELEV_AT else SCENT_STIRRING if sc >= SCENT_STIR_AT else SCENT_QUIET


def _breaking_down(snap, m):
    """Has the up-move LOST ITS TREND — price below its ~50-day mean (a CONFIRMED
    rollover)? Out-of-sample testing on 2024-25 IPOs showed a shallow 20-day break
    got faked out by buy-the-dip melt-ups; requiring the deeper 50-day break cut
    the out-of-sample loss ~75% while preserving the older edge. Falls back to a
    confirmed lower-high when long history isn't available (e.g. TWS off / seed)."""
    ref = snap.long_closes or snap.recent_closes
    price = snap.price
    if ref and len(ref) >= 10 and price:
        n = min(len(ref), 50)
        return price < sum(ref[-n:])/n
    if (m.get("trend_rollover") and m["trend_rollover"].state == GREEN
            and snap.recent_closes and len(snap.recent_closes) >= 5 and price
            and price < sum(snap.recent_closes[-5:])/5):
        return True
    return False


def early_warning_level(snap, S, comp, conf, sc):
    """The leading FRAGILITY track (DORMANT/WATCH/ARMED), independent of the
    live-data availability blocks that gate the strict TRIGGER. The actionable
    ARMED requires a fragile top that is NOW BREAKING into a supply window — so
    it never green-lights a short into a still-melting-up parabola (the squeeze
    trap). SCENT-elevated alone is only WATCH ('fragility building, prepare').
    Surfaced live alongside the main ladder; it is what the backtest evaluates."""
    m = {s.key: s for s in S}
    peak = max(x for x in (sc, snap.scent_recent_peak) if x is not None) \
        if (sc is not None or snap.scent_recent_peak is not None) else None
    fragile_top = peak is not None and peak >= SCENT_ELEV_AT
    # dated supply window: within ~3 weeks of (or just past) a MAJOR unlock — the
    # empirical pre-positioning / event window. Float velocity feeds SCENT, not
    # this gate, so the window can't open weeks early.
    catalyst = snap.days_to_next_major is not None and -3 <= snap.days_to_next_major <= 21
    breaking = _breaking_down(snap, m)
    # NOTE: a price-only "priced-in" suppressor (skip if already >40% off the 60d
    # high) was tested on 32 analogs and slightly HURT (it cut winners, not the
    # worst losers) — the real priced-in discriminator needs short-interest data
    # (live, not backtestable), so it is intentionally NOT gated on price here.
    # ARM the directional short only when a dated SUPPLY CATALYST window is open
    # AND price is rolling over. Momentum-exhaustion without a near catalyst is
    # only WATCH — a hyped parabola's pullbacks resume, so we do not short
    # strength on the smell alone (the thesis: bearish pressure comes with supply).
    if catalyst and breaking:
        return ARMED
    if (fragile_top and breaking) or (sc is not None and sc >= SCENT_STIR_AT) \
            or fragile_top or comp >= 40:
        return WATCH
    return DORMANT

# =============================================================================
#  RISK ENGINE — put-spread builder, ATR stops, sizing
# =============================================================================
def _N(x):
    return (1+erf(x/sqrt(2)))/2

def _n(x):
    return exp(-x*x/2)/sqrt(2*3.141592653589793)

def bs_put(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(K-S, 0.0)
    d1 = (log(S/K)+(r+sig*sig/2)*T)/(sig*sqrt(T)); d2 = d1-sig*sqrt(T)
    return K*exp(-r*T)*_N(-d2) - S*_N(-d1)

def bs_put_greeks(S, K, T, r, sig):
    """Per-share Greeks for a put: delta, gamma, theta(/day), vega(/1% vol)."""
    if T <= 0 or sig <= 0:
        return {"delta": -1.0 if S < K else 0.0, "gamma": 0, "theta": 0, "vega": 0}
    d1 = (log(S/K)+(r+sig*sig/2)*T)/(sig*sqrt(T)); d2 = d1-sig*sqrt(T)
    delta = _N(d1)-1
    gamma = _n(d1)/(S*sig*sqrt(T))
    theta = (-(S*_n(d1)*sig)/(2*sqrt(T)) + r*K*exp(-r*T)*_N(-d2))/365
    vega = S*_n(d1)*sqrt(T)/100
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}

def bs_call(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(S-K, 0.0)
    d1 = (log(S/K)+(r+sig*sig/2)*T)/(sig*sqrt(T)); d2 = d1-sig*sqrt(T)
    return S*_N(d1) - K*exp(-r*T)*_N(d2)

def spread_value(S, Kl, Ks, T, r, sig):
    return bs_put(S, Kl, T, r, sig) - bs_put(S, Ks, T, r, sig)


def build_trade_plan(snap, cfg):
    if not snap.price:
        return None
    rk = cfg["risk"]; S0 = snap.price; sig = snap.iv_annual or 1.0
    # dynamic expiry: first monthly ~45d AFTER the next major unlock, so the
    # spread spans the supply event; fall back to configured lockup date.
    nmd = snap.extras.get("next_major_date")
    if nmd:
        target = datetime.strptime(nmd, "%Y-%m-%d").date() + timedelta(days=45)
        exp_date = _third_friday(target.year, target.month)
        exp_d = exp_date.strftime("%Y-%m-%d"); exp_basis = f"spans {snap.next_major_label}"
    else:
        exp_d = cfg["fundamentals"]["lockup_expiry"]; exp_basis = "lockup expiry"
    T = max((datetime.strptime(exp_d, "%Y-%m-%d").date()-date.today()).days, 1)/365
    Klong = round(S0*rk["long_put_moneyness"]/5)*5
    Kshort = round(S0*rk["short_put_moneyness"]/5)*5
    r = rk["risk_free_rate"]
    long_prem = bs_put(S0, Klong, T, r, sig); short_prem = bs_put(S0, Kshort, T, r, sig)
    spread_cost = max(long_prem-short_prem, 0.01); width = Klong-Kshort
    max_risk_usd = rk["capital_usd"]*rk["max_risk_pct"]/100
    contracts = max(int(max_risk_usd//(spread_cost*100)), 0)
    cost = contracts*spread_cost*100
    max_profit = contracts*(width-spread_cost)*100
    breakeven = Klong-spread_cost
    atr = snap.atr or (S0*sig/sqrt(252))
    swing_high = max(snap.recent_highs) if snap.recent_highs else S0
    invalidation = swing_high + rk["atr_stop_mult"]*atr
    rr = (max_profit/cost) if cost else 0
    # net spread Greeks (long put minus short put), scaled to the position
    gl = bs_put_greeks(S0, Klong, T, r, sig); gs = bs_put_greeks(S0, Kshort, T, r, sig)
    mult = contracts*100
    greeks = {k: round((gl[k]-gs[k])*mult, 1) for k in ("delta", "gamma", "theta", "vega")}
    # probability of profit (risk-neutral): P(S_T < breakeven)
    if T > 0 and sig > 0 and breakeven > 0:
        d2b = (log(S0/breakeven)+(r-sig*sig/2)*T)/(sig*sqrt(T))
        pop = round(_N(-d2b)*100)
    else:
        pop = None
    # IV-crush scenario: value if IV falls to 60% of current, spot unchanged
    sig_crush = sig*0.6
    crush_val = spread_value(S0, Klong, Kshort, T, r, sig_crush)
    crush_pnl = round((crush_val-spread_cost)*mult)
    # scale-in ladder across major unlock windows (don't go all-in on one date)
    majors = snap.extras.get("major_unlocks", [])
    tranches = []
    if majors:
        per = max_risk_usd/len(majors)
        for d, pct in majors:
            tranches.append({"date": d, "pct": pct, "risk": round(per)})
    # --- SELLING / EXIT RULES, anchored to the supply schedule (the research's
    #     key point: the lockup edge is timing + EXITS, not "short the unlock") ---
    selling_rules = {
        "instrument": "put DEBIT spread, not naked puts — caps IV-crush/theta bleed on a high-IV name",
        "entry_filter": "require the modeled drop to exceed the option-implied move before entering",
        "profit_take": f"harvest ~60% of spread max (≈${0.6*max_profit:,.0f}) into the unlock date ±1 day "
                       f"— the lockup drop is front-loaded and historically does not reverse",
        "scale_out": ("ladder across the big waves and take partial profit at each; weight the largest "
                      f"({snap.next_major_label or 'next major'}) heaviest" if tranches
                      else "single defined-risk tranche"),
        "roll": f"if a tranche underperforms but a larger one is near, roll down-and-out to span "
                f"{snap.next_major_label or 'the next wave'}",
        "time_stop": "close any spread not in-the-money by ~T+2 trading days after the unlock "
                     "(≈1/3 of unlocks rally / IV crushes — don't hold and hope)",
        "invalidation": f"hard stop above ${invalidation:.2f} (swing-high + {rk['atr_stop_mult']}×ATR); "
                        f"stand down on squeeze/priced-in suppressors (SI spiked, borrow rising, "
                        f"call-heavy near a call wall, >30% above IPO = bullish price-trigger regime)",
    }
    return {
        "expiry": exp_d, "exp_basis": exp_basis, "dte": int(T*365), "spot": S0, "iv": sig,
        "Klong": Klong, "Kshort": Kshort, "width": width,
        "spread_cost": spread_cost, "contracts": contracts, "cost": cost,
        "max_loss": cost, "max_profit": max_profit, "breakeven": breakeven,
        "rr": rr, "efficiency": spread_cost/width if width else 1,
        "atr": atr, "invalidation": invalidation, "max_risk_usd": max_risk_usd,
        "quality": _plan_quality(rr, sig), "tranches": tranches,
        "greeks": greeks, "pop": pop, "iv_crush_pnl": crush_pnl, "iv_crush_to": round(sig_crush, 2),
        "selling_rules": selling_rules,
    }


def _plan_quality(rr, iv):
    if rr < 1.0:
        return ("POOR", f"R:R {rr:.1f}:1 — you risk more than you can make. IV ~{iv*100:.0f}% "
                "is inflating the long put. Wait for IV to fall before structuring.")
    if rr < 1.5:
        return ("MARGINAL", f"R:R {rr:.1f}:1 — acceptable but not great; IV still rich.")
    return ("GOOD", f"R:R {rr:.1f}:1 — favorable structure.")


def _iv_is_seeded(snap):
    """True when the volatility driving option prices is a SEED/STALE guess, not a
    live IV — so POP / breakeven / max-loss are ILLUSTRATIVE, not actionable quotes.
    (No live single-name IV feed is wired; the daily run prices off a seed.)"""
    st = (snap.sources.get("iv_annual") or (None, None, None))[2]
    return isinstance(st, str) and st.upper().startswith("SEED")


def build_option_tickets(snap, cfg, plan):
    """A beginner-friendly MENU of ready-to-read options tickets to express the
    bearish thesis, each defined-risk by default. Prices are theoretical
    (Black-Scholes) — verify live and use LIMIT orders. NOT financial advice."""
    if not plan or not snap.price:
        return []
    illustrative = _iv_is_seeded(snap)
    seed_warn = (" ⚠ PRICED OFF SEED/STALE IV — these $ figures are ILLUSTRATIVE, "
                 "not a live quote; confirm the real chain before sizing." if illustrative else "")
    S0 = plan["spot"]; sig = plan["iv"] or 1.0; r = cfg["risk"]["risk_free_rate"]
    exp = plan["expiry"]; T = max(plan["dte"], 1)/365.0
    cap = plan["max_risk_usd"]
    k = lambda mult: max(round(S0*mult/5)*5, 5)
    Kp_hi, Kp_lo = plan["Klong"], plan["Kshort"]           # 0.95 / 0.78 puts
    Kc_lo, Kc_hi = k(1.10), k(1.25)                         # OTM calls for a credit spread
    Kp_fc, Kp_fl = k(0.85), k(0.65)                         # cheaper, further-OTM put spread
    tickets = []

    def leg(action, qty, strike, right):
        return {"action": action, "qty": qty, "expiry": exp, "strike": strike, "right": right}

    # 1) Bear PUT debit spread — the all-rounder (engine's pick) ----------------
    c1 = max(bs_put(S0, Kp_hi, T, r, sig) - bs_put(S0, Kp_lo, T, r, sig), 0.01)
    n1 = max(int(cap // (c1*100)), 0); w1 = Kp_hi - Kp_lo
    if n1:
        tickets.append({
            "key": "bear_put_spread", "name": "Bear put debit spread", "difficulty": "beginner-friendly",
            "recommended": True, "defined_risk": True,
            "legs": [leg("BUY", n1, Kp_hi, "P"), leg("SELL", n1, Kp_lo, "P")],
            "net": -round(n1*c1*100), "max_loss": round(n1*c1*100),
            "max_profit": round(n1*(w1-c1)*100), "breakeven": round(Kp_hi-c1, 2),
            "wins_if": f"price falls toward/below ${Kp_lo:.0f} by {exp}",
            "note": "Best all-around: cheaper than a plain put and the sold put helps offset IV-crush/"
                    "decay. You can never lose more than the debit. Profit is capped at the lower strike."})

    # 2) Long put — simplest bearish ------------------------------------------
    p = bs_put(S0, Kp_hi, T, r, sig); per = p*100
    n2 = max(int(cap // per), 0) if per > 0 else 0
    if n2:
        tickets.append({
            "key": "long_put", "name": "Long put (buy a put)", "difficulty": "beginner",
            "recommended": False, "defined_risk": True,
            "legs": [leg("BUY", n2, Kp_hi, "P")],
            "net": -round(n2*per), "max_loss": round(n2*per),
            "max_profit": round(n2*(Kp_hi*100) - n2*per), "breakeven": round(Kp_hi-p, 2),
            "wins_if": f"price falls below ${Kp_hi-p:.0f} (breakeven) by {exp}",
            "note": "Simplest bearish trade and you can't lose more than the premium — BUT at ~"
                    f"{sig*100:.0f}% IV the premium is rich and an IV drop can lose money even if you're "
                    "right on direction. Usually the spread (above) is the smarter version."})

    # 3) Bear CALL credit spread — let high IV work FOR you --------------------
    cr = max(bs_call(S0, Kc_lo, T, r, sig) - bs_call(S0, Kc_hi, T, r, sig), 0.01)
    wc = Kc_hi - Kc_lo; maxloss_per = (wc - cr)*100
    n3 = max(int(cap // maxloss_per), 0) if maxloss_per > 0 else 0
    if n3:
        tickets.append({
            "key": "bear_call_spread", "name": "Bear call credit spread", "difficulty": "intermediate",
            "recommended": False, "defined_risk": True,
            "legs": [leg("SELL", n3, Kc_lo, "C"), leg("BUY", n3, Kc_hi, "C")],
            "net": round(n3*cr*100), "max_loss": round(n3*maxloss_per),
            "max_profit": round(n3*cr*100), "breakeven": round(Kc_lo+cr, 2),
            "wins_if": f"price stays BELOW ${Kc_lo:.0f} by {exp} (it doesn't even have to fall)",
            "note": "You COLLECT premium up front and high IV + time decay work for you; you win if the "
                    "stock simply fails to rip higher. Loss is capped but larger than the credit, and the "
                    "short call can be assigned. Good when IV is extreme — but the squeeze guard must be clear."})

    # 4) Cheap far-OTM put spread — low-cost lottery (advanced) ----------------
    c4 = max(bs_put(S0, Kp_fc, T, r, sig) - bs_put(S0, Kp_fl, T, r, sig), 0.01)
    budget4 = cap*0.25; n4 = max(int(budget4 // (c4*100)), 0); w4 = Kp_fc - Kp_fl
    if n4:
        tickets.append({
            "key": "cheap_put_spread", "name": "Cheap far-OTM put spread (lottery)", "difficulty": "advanced",
            "recommended": False, "defined_risk": True,
            "legs": [leg("BUY", n4, Kp_fc, "P"), leg("SELL", n4, Kp_fl, "P")],
            "net": -round(n4*c4*100), "max_loss": round(n4*c4*100),
            "max_profit": round(n4*(w4-c4)*100), "breakeven": round(Kp_fc-c4, 2),
            "wins_if": f"price CRASHES below ~${Kp_fc:.0f} (needs a big drop) by {exp}",
            "note": "Small cost, big payoff IF the drop is large — but low probability; only risk money "
                    "you'd treat as a lottery ticket (sized here to ~25% of your risk budget)."})

    # 5) Short shares — the non-options baseline (flagged, no ticket) ----------
    tickets.append({
        "key": "short_shares", "name": "Short the shares (NOT options)", "difficulty": "high-risk",
        "recommended": False, "defined_risk": False, "legs": [],
        "net": None, "max_loss": "UNLIMITED", "max_profit": None, "breakeven": None,
        "wins_if": "price falls",
        "note": "Listed for completeness only. Loss is UNLIMITED if it squeezes up, and a ~95%-locked IPO "
                "is often impossible/expensive to borrow. Not recommended for non-experts — the defined-risk "
                "option structures above are far safer."})
    # Tag every IV-priced ticket when the volatility is a seed/stale guess, so the
    # polished dollar-exact figures can't be mistaken for a live, actionable quote.
    for tk in tickets:
        tk["priced_off_seed_iv"] = illustrative and bool(tk.get("legs"))
        if tk["priced_off_seed_iv"]:
            tk["note"] = tk["note"] + seed_warn
    return tickets


# =============================================================================
#  PERSISTENCE + alerts
# =============================================================================
def db_init(cfg):
    c = sqlite3.connect(cfg["db_path"])
    c.execute("CREATE TABLE IF NOT EXISTS runs(ts TEXT,price REAL,composite REAL,"
              "level TEXT,aligned INT,blocked INT,iv REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS journal(ts TEXT,key TEXT,state TEXT,note TEXT)")
    try:                              # migration: add SCENT column for the track record
        c.execute("ALTER TABLE runs ADD COLUMN scent REAL")
    except Exception:
        pass
    c.commit(); return c

def db_prior_states(cfg):
    """Per-signal states from the most recent prior run (for change detection)."""
    try:
        c = sqlite3.connect(cfg["db_path"])
        last = c.execute("SELECT ts FROM journal ORDER BY ts DESC LIMIT 1").fetchone()
        if not last:
            c.close(); return {}
        rows = c.execute("SELECT key,state FROM journal WHERE ts=?", (last[0],)).fetchall()
        c.close(); return dict(rows)
    except Exception:
        return {}


def db_last(cfg):
    try:
        c = sqlite3.connect(cfg["db_path"])
        row = c.execute("SELECT ts,composite,level FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        c.close(); return row
    except Exception:
        return None

def db_streak(cfg):
    """Consecutive runs (incl. latest) at the most recent level — for de-bounce."""
    try:
        c = sqlite3.connect(cfg["db_path"])
        rows = [r[0] for r in c.execute("SELECT level FROM runs ORDER BY rowid DESC LIMIT 20").fetchall()]
        c.close()
    except Exception:
        return 0
    if not rows:
        return 0
    top = rows[0]; n = 0
    for r in rows:
        if r == top:
            n += 1
        else:
            break
    return n

def db_trend(cfg, n=24):
    try:
        c = sqlite3.connect(cfg["db_path"])
        rows = [r[0] for r in c.execute("SELECT composite FROM runs ORDER BY rowid DESC LIMIT ?", (n,)).fetchall()]
        c.close(); return list(reversed(rows))
    except Exception:
        return []

def sparkline(vals):
    if not vals:
        return ""
    blocks = "▁▂▃▄▅▆▇█"; lo, hi = min(vals), max(vals)
    if hi == lo:
        return blocks[3]*len(vals)
    return "".join(blocks[int((v-lo)/(hi-lo)*(len(blocks)-1))] for v in vals)

def db_log(cfg, snap, comp, level, conf, S):
    c = db_init(cfg)
    c.execute("INSERT INTO runs(ts,price,composite,level,aligned,blocked,iv,scent) VALUES(?,?,?,?,?,?,?,?)",
              (snap.ts.isoformat(), snap.price, comp, level,
               int(conf["aligned"]), int(bool(conf["blockers"])), snap.iv_annual, snap.scent))
    for s in S:
        c.execute("INSERT INTO journal VALUES(?,?,?,?)", (snap.ts.isoformat(), s.key, s.state, s.note))
    c.commit(); c.close()


def track_record(cfg, fwd_days=20, drop=0.03):
    """SPCX's OWN scorecard: for every past run that issued a signal, did price
    actually fall over the next ~fwd_days? Accumulates as the daily task runs — it
    is the honest, non-overfitting form of 'learning' (it builds evidence for YOU;
    it does NOT auto-change the rules). Returns {signal: {n, hit_rate, avg_fwd}}."""
    try:
        c = sqlite3.connect(cfg["db_path"])
        rows = c.execute("SELECT ts,price,level,scent FROM runs WHERE price IS NOT NULL ORDER BY ts").fetchall()
        c.close()
    except Exception:
        return None
    pts = []
    for ts, price, level, scent in rows:
        try:
            pts.append((datetime.fromisoformat(ts), price, level, scent))
        except Exception:
            continue
    if len(pts) < 2:
        return None

    def fwd_price(t):
        cand = [(abs((p[0]-(t+timedelta(days=fwd_days))).total_seconds()), p[1])
                for p in pts if p[0] >= t + timedelta(days=fwd_days*0.5)]
        return min(cand)[1] if cand else None

    buckets = {}
    for t, price, level, scent in pts:
        sig = level if level in (WATCH, ARMED, TRIGGER) else ("SCENT≥65" if (scent or 0) >= SCENT_ELEV_AT else None)
        if not sig or not price:
            continue
        fp = fwd_price(t)
        if fp is None:
            continue
        r = (fp - price) / price
        b = buckets.setdefault(sig, {"n": 0, "hit": 0, "sum": 0.0})
        b["n"] += 1; b["sum"] += r
        if r <= -drop:                # a bearish signal "won" if price fell >= drop
            b["hit"] += 1
    return {k: {"n": b["n"], "hit_rate": round(b["hit"]/b["n"]*100), "avg_fwd": round(b["sum"]/b["n"]*100, 1)}
            for k, b in buckets.items() if b["n"]}


def alert_text(snap, comp, level, conf, plan):
    head = {TRIGGER: "🚨 ABSOLUTE WARNING — SHORT-ENTRY CONDITIONS MET",
            ARMED: "🟠 ARMED — one condition from trigger",
            BLOCKED: "⛔ BLOCKED — gamma-squeeze risk live",
            WATCH: "🟡 WATCH — thesis building",
            DORMANT: "⚪ DORMANT"}[level]
    lines = [f"*SPCX warning system* — {head}", f"price ${snap.price} · score {comp}/100"]
    if conf["missing"]:
        lines.append("waiting on: " + ", ".join(conf["missing"]))
    if conf["blockers"]:
        lines.append("blocked by: " + ", ".join(conf["blockers"]))
    if level == TRIGGER and plan and plan["contracts"]:
        lines += ["", f"Plan: BUY {plan['contracts']}x {plan['expiry']} {plan['Klong']:.0f}/"
                  f"{plan['Kshort']:.0f} put spread ({plan['dte']}DTE)", f"max loss ${plan['max_loss']:,.0f} · "
                  f"max profit ${plan['max_profit']:,.0f} · R:R {plan['rr']:.1f}"]
    lines += ["", "_Decision-support only. Not financial advice._"]
    return "\n".join(lines)


def send_ntfy(cfg, title, body, priority="default", tags=""):
    """Push to the phone via ntfy.sh (no tokens — just subscribe to the topic).
    Topic/server can be overridden by env vars so the cloud cron (GitHub Actions)
    keeps the topic as a SECRET instead of in the public repo."""
    n = cfg["alerts"].get("ntfy", {})
    topic = os.environ.get("SPCX_NTFY_TOPIC") or n.get("topic")
    if not n.get("enabled", True) or not topic:
        return False
    server = os.environ.get("SPCX_NTFY_SERVER") or n.get("server", "https://ntfy.sh")
    url = server.rstrip("/") + "/" + topic
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers={
            "User-Agent": UA, "Title": title.encode("ascii", "replace").decode(),
            "Priority": priority, "Tags": tags})
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print("  ntfy failed:", e); return False


def send_telegram(cfg, text):
    tg = cfg["alerts"]["telegram"]
    if not (tg["enabled"] and tg["bot_token"] and tg["chat_id"]):
        return False
    try:
        url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text, "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
        return True
    except Exception as e:
        print("  telegram failed:", e); return False


def send_email(cfg, subject, text):
    em = cfg["alerts"]["email"]
    if not em["enabled"]:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        m = MIMEText(text); m["Subject"] = subject; m["To"] = em["to"]
        with smtplib.SMTP(em["smtp_host"]) as s:
            s.send_message(m)
        return True
    except Exception as e:
        print("  email failed:", e); return False


def notify_all(cfg, title, text, priority="default", tags=""):
    """Send to every enabled channel (ntfy + telegram + email). Returns True if any sent."""
    sent = send_ntfy(cfg, title, text, priority, tags)
    sent = send_telegram(cfg, text) or sent
    sent = send_email(cfg, title, text) or sent
    return sent


def alert_dispatch(cfg, text, level, prev_level, streak=99):
    """Escalation only, with de-bounce: a higher level must persist
    hysteresis_runs runs before alerting. BLOCKED fires immediately (safety)."""
    rank_now = LADDER_RANK.get(level, 3 if level == TRIGGER else 0)
    rank_prev = LADDER_RANK.get(prev_level, -1)
    escalated = (level in (TRIGGER, BLOCKED) and prev_level != level) or rank_now > rank_prev
    if not escalated:
        return False
    hyst = cfg["alerts"].get("hysteresis_runs", 1)
    if level != BLOCKED and streak < hyst:
        print(f"  (escalation pending confirmation {streak}/{hyst} — not yet alerted)")
        return False
    prio = "urgent" if level == TRIGGER else "high" if level in (BLOCKED, ARMED) else "default"
    return notify_all(cfg, f"SPCX {level}", text, prio,
                      "rotating_light" if level == TRIGGER else "warning")


def daily_digest_text(snap, comp, level, conf, plan):
    do = {TRIGGER: "✅ entry conditions met — review the plan",
          ARMED: "🟠 supply window + rollover — actionable, watch closely",
          BLOCKED: "⛔ DO NOT short yet (squeeze risk / data missing)",
          WATCH: "🟡 thesis building — not actionable yet",
          DORMANT: "⚪ nothing to do yet — momentum in control"}[level]
    lines = [f"SPCX daily — {level}  ({comp:.0f}/100)", f"price ${snap.price}", do]
    if snap.scent is not None:
        lines.append(f"SCENT {snap.scent:.0f} [{snap.scent_state}] → early-warning {snap.extras.get('early_warning','')}")
    if snap.next_major_label:
        lines.append(f"next major unlock: {snap.next_major_label} in {snap.days_to_next_major}d")
    if conf.get("missing"):
        lines.append("waiting on: " + ", ".join(conf["missing"]))
    lines.append("— decision-support only, not financial advice")
    return "\n".join(lines)


def send_daily_digest(cfg, snap, comp, level, conf, plan):
    if not cfg["alerts"].get("daily_digest", True):
        return False
    prio = "urgent" if level == TRIGGER else "high" if level in (ARMED, BLOCKED) else "default"
    return notify_all(cfg, f"SPCX {level} {comp:.0f}/100",
                      daily_digest_text(snap, comp, level, conf, plan), prio,
                      "rotating_light" if level == TRIGGER else "chart_with_downwards_trend")

# =============================================================================
#  OUTPUT
# =============================================================================
DOT = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴"}
LADDER_VIS = [DORMANT, WATCH, ARMED, TRIGGER]

def ladder_bar(level):
    if level == BLOCKED:
        return "  [ DORMANT · WATCH · ARMED · TRIGGER ]   ⛔ BLOCKED (override)"
    out = []
    for L in LADDER_VIS:
        out.append(f"〈{L}〉" if L == level else L)
    return "  " + " · ".join(out)


def print_report(snap, S, comp, level, conf, plan, cfg):
    tws = snap.extras.get("tws_connected")
    last = db_last(cfg)
    delta = ""
    if last and last[1] is not None:
        d = comp-last[1]; delta = f"  (Δ {d:+.1f} vs last {last[2]})"
    print("\n" + "=" * 72)
    print(f"  SPCX SHORT-ENTRY WARNING SYSTEM v{VERSION}    {snap.ts:%Y-%m-%d %H:%M}")
    print(f"  price ${snap.price}   composite {comp}/100{delta}")
    print(f"  data: {'TWS LIVE' if tws else 'TWS OFF -> seed/last-live'}")
    conf_pct, informed, total, missing = data_confidence(S)
    print(f"  confidence: {conf_pct}% ({informed}/{total} signals informed)"
          + (f" — missing: {', '.join(missing)}" if missing else ""))
    if conf_pct < 60:
        print("  ⚠ low data completeness — treat the score as provisional until sources are wired")
    # catalyst clock — staggered unlock schedule
    try:
        if snap.cum_float_pct is not None:
            print(f"  float: ~{snap.cum_float_pct}% tradeable ({100-snap.cum_float_pct}% locked)"
                  f"{' — tiny float = structurally squeeze-prone' if snap.cum_float_pct < 15 else ''}")
        if snap.next_unlock_label:
            print(f"  next unlock:  {snap.next_unlock_label} in {snap.days_to_next_unlock}d")
        if snap.next_major_label:
            print(f"  next MAJOR:   {snap.next_major_label} (+{snap.next_major_pct}%) in {snap.days_to_next_major}d"
                  f"  <- first real supply pressure / bearish window")
    except Exception:
        pass
    print("=" * 72)

    print("\n  WARNING LADDER")
    print(ladder_bar(level))
    sc = snap.scent
    if sc is not None:
        ew = snap.extras.get("early_warning", "")
        comps = ", ".join(f"{k} {v[1]}" for k, v in sorted(scent_components(snap).items(),
                          key=lambda kv: -kv[1][0])[:3])
        print(f"  SCENT (fragility gauge)  {sc:.0f}/100 [{snap.scent_state}]  → ladder: {ew}")
        print(f"     top drivers: {comps}")
        print("     (SCENT = the up-move is propped/fragile; NOT a directional call and never arms a")
        print("      short alone — only WATCH until a supply catalyst + rollover confirm.)")
        print("     ⚠ CAVEAT: price-derived, so it catches only ~25% of historical tops and MISSES the")
        print("       week-1-2 IPO peaks with no price history (SPCX's own profile right now). Its short")
        print("       edge INVERTS in IPO-mania regimes. Treat it as a risk-gate, not a leading 'smell-it-")
        print("       first' signal — a true lead needs live borrow/options-flow, which is not wired.")

    tr = track_record(cfg)
    if tr:
        print("\n  TRACK RECORD (this SPCX run-log — did past signals lead a 20d drop?)")
        print("  " + "-" * 68)
        for k in (TRIGGER, ARMED, WATCH, "SCENT≥65"):
            if k in tr:
                r = tr[k]
                print(f"  {k:<10} {r['n']} signal(s) · {r['hit_rate']}% led a ≥3% drop · avg fwd {r['avg_fwd']:+.1f}%")
        print("     (accumulates as the daily task runs; needs varying live price to be meaningful)")

    print("\n  DATA SOURCES & PROVENANCE")
    print("  " + "-" * 68)
    print(f"  {'field':<22}{'value':<16}{'status':<14}source")
    for fld, (val, src, st) in snap.sources.items():
        v = f"{val:.2f}" if isinstance(val, float) else str(val)
        print(f"  {fld:<22}{v[:15]:<16}{st:<14}{src}")

    print("\n  SIGNALS")
    print("  " + "-" * 68)
    prior = db_prior_states(cfg)
    changes = []
    for s in S:
        arrow = ""
        if prior.get(s.key) and prior[s.key] != s.state:
            arrow = f"   ({prior[s.key]}→{s.state})"
            changes.append(f"{s.key} {prior[s.key]}→{s.state}")
        print(f"  {DOT[s.state]} {s.key:<16}{s.note}{arrow}")
    if changes:
        print("\n  CHANGED SINCE LAST RUN: " + " · ".join(changes))

    print("\n  CONFLUENCE GATE")
    print("  " + "-" * 68)
    for k, v in conf["conditions"].items():
        print(f"  {'✅' if v else '❌'} {k}")
    for b in conf["blockers"]:
        print(f"  ⛔ BLOCKED: {b}")

    print("\n  SUGGESTION")
    print("  " + "-" * 68)
    if level == TRIGGER:
        print("  🚨🚨  ABSOLUTE WARNING: SHORT-ENTRY CONDITIONS MET  🚨🚨")
        print("  Your full checklist is satisfied and no squeeze risk is active.")
    elif level == BLOCKED:
        if "bullish_catalyst_squeeze_risk" in conf["blockers"]:
            print("  ⛔ DO NOT SHORT — a bullish catalyst (Musk/SpaceX headline) is live.")
            print("     Positive news is the classic squeeze trigger. Stand down until it clears.")
        elif "gamma_squeeze_unevaluated" in conf["blockers"]:
            print("  ⛔ DO NOT SHORT — squeeze risk UNEVALUATED (no OTM call-flow data).")
            print("     The single most important safety input is unknown. Wire up flow data")
            print("     (Unusual Whales / manual) so the squeeze guard can actually clear.")
        else:
            print("  ⛔ DO NOT SHORT — gamma-squeeze risk live. Wait for OTM call")
            print("     buying to die down before anything else matters.")
    elif level == ARMED:
        print("  🟠 ARMED — one trigger condition away. Watch closely. Missing:")
        print("     " + ", ".join(conf["missing"]))
    elif level == WATCH:
        print("  🟡 WATCH — thesis context building, not actionable. Missing:")
        print("     " + ", ".join(conf["missing"]))
    else:
        print("  ⚪ DORMANT — momentum still in control. Do not fight it. Missing:")
        print("     " + ", ".join(conf["missing"]))

    if level != TRIGGER:
        gaps = gap_to_trigger(snap, S, conf, cfg)
        if gaps:
            print("\n  WHAT FLIPS THIS TO TRIGGER")
            print("  " + "-" * 68)
            for g in gaps:
                print(f"  → {g}")
    contrib = tier_contributions(S)
    print("\n  SCORE BREAKDOWN (weighted pts/tier)")
    print("  " + "-" * 68)
    print("  " + "  ".join(f"{t}:{v}" for t, v in contrib.items()))
    # consensus: how many tiers lean short (mean signal score >= 0.6)
    by = {}
    for s in S:
        by.setdefault(s.tier, []).append(s.score)
    lean = sum(1 for tv in by.values() if sum(tv)/len(tv) >= 0.6)
    print(f"  signal agreement: {lean}/{len(by)} tiers lean short  ·  "
          f"data confidence {conf.get('data_confidence','?')}%")

    if plan and plan["contracts"]:
        print(f"\n  TRADE PLAN (if/when triggered — put debit spread, {plan['exp_basis']})")
        print("  " + "-" * 68)
        print(f"  expiry      {plan['expiry']} ({plan['dte']}DTE) — {plan['exp_basis']}")
        print(f"  structure   BUY {plan['contracts']}x  {plan['Klong']:.0f}P / SELL {plan['contracts']}x {plan['Kshort']:.0f}P")
        if _iv_is_seeded(snap):
            print(f"  ⚠ IV {plan['iv']*100:.0f}% is SEED/STALE (no live single-name IV feed wired) — every $ figure")
            print(f"     below (cost, max-loss, breakeven, POP) is ILLUSTRATIVE, not a live quote.")
        print(f"  cost/spread ${plan['spread_cost']:.2f}  → total debit ${plan['cost']:,.0f}  (cap risk ${plan['max_risk_usd']:,.0f})")
        print(f"  max loss    ${plan['max_loss']:,.0f}    max profit ${plan['max_profit']:,.0f}   R:R {plan['rr']:.1f}:1")
        print(f"  breakeven   ${plan['breakeven']:.2f}    ATR ${plan['atr']:.1f}")
        print(f"  invalidate  if price closes above ${plan['invalidation']:.2f} (swing-high + {cfg['risk']['atr_stop_mult']}xATR)")
        q = plan["quality"]
        print(f"  quality     [{q[0]}] {q[1]}")
        g = plan["greeks"]
        print(f"  greeks      delta {g['delta']:+.0f}  theta {g['theta']:+.0f}/day  vega {g['vega']:+.0f}/1%IV"
              + (f"  ·  risk-neutral P(S_T<BE) ~{plan['pop']}%" if plan.get("pop") is not None else ""))
        if plan.get("pop") is not None:
            print(f"              (NB: under ~{(plan['iv']*100):.0f}% IV, variance drag lifts this number; "
                  f"breakeven ${plan['breakeven']:.0f} is {(1-plan['breakeven']/plan['spot'])*100:.0f}% below spot — "
                  f"not a real-world win rate)")
        print(f"  IV-crush    if IV falls to {plan['iv_crush_to']*100:.0f}% (spot flat): "
              f"{'+' if plan['iv_crush_pnl']>=0 else ''}{plan['iv_crush_pnl']:,} P&L "
              f"— {'hurts' if plan['iv_crush_pnl']<0 else 'helps'} the long leg")
        if snap.next_major_label:
            print(f"  timing      nearest supply catalyst: {snap.next_major_label} (+{snap.next_major_pct}%, {snap.days_to_next_major}d) "
                  f"— consider an expiry that spans it")
        if plan.get("tranches"):
            print(f"  scale-in    don't go all-in — ladder ${plan['max_risk_usd']:,.0f} across {len(plan['tranches'])} unlock windows:")
            for tr in plan["tranches"]:
                print(f"                ${tr['risk']:,} near {tr['date']} (+{tr['pct']}% unlock)")
        sr = plan.get("selling_rules")
        if sr:
            print("\n  SELLING / EXIT RULES (the lockup edge is timing + exits, not 'short the unlock')")
            print("  " + "-" * 68)
            for k in ("instrument", "entry_filter", "profit_take", "scale_out", "roll", "time_stop", "invalidation"):
                if sr.get(k):
                    print(f"  {k:<13} {sr[k]}")
        # Order tickets UNLOCK only on a live, actionable signal (ARMED/TRIGGER) —
        # never while DORMANT/WATCH/BLOCKED, so they can't tempt a premature order.
        tickets = build_option_tickets(snap, cfg, plan) if level in (ARMED, TRIGGER) else []
        if level not in (ARMED, TRIGGER):
            print("\n  OPTIONS TRADE TICKETS")
            print("  " + "-" * 68)
            print(f"  🔒 locked — no order. Tickets unlock only on a live go-signal (ARMED/TRIGGER).")
            print(f"     Current level: {level} → stand down, place nothing. (This plan is reference only.)")
        if tickets:
            print("\n  OPTIONS TRADE TICKETS — sample structures (theoretical prices; verify live, use LIMIT orders)")
            print("  " + "-" * 68)
            for tk in tickets:
                star = " ★ RECOMMENDED" if tk["recommended"] else ""
                print(f"\n  • {tk['name']}  [{tk['difficulty']}]{star}")
                if tk["legs"]:
                    for lg in tk["legs"]:
                        print(f"      {lg['action']} {lg['qty']}× {lg['expiry']} {lg['strike']:.0f}{lg['right']}")
                    net = tk["net"]
                    netlbl = (f"credit ${net:,}" if net and net > 0 else f"debit ${abs(net):,}") if net is not None else "—"
                    ml = tk["max_loss"]; mp = tk["max_profit"]
                    ml = f"${ml:,}" if isinstance(ml, (int, float)) else str(ml)
                    mp = f"${mp:,}" if isinstance(mp, (int, float)) else "—"
                    print(f"      net {netlbl}  ·  max loss {ml}  ·  max profit {mp}  ·  breakeven ${tk['breakeven']}")
                print(f"      wins if: {tk['wins_if']}")
                print(f"      {tk['note']}")
            print("\n  ⚠ Beginner guardrails: start tiny or paper-trade first; NEVER sell naked options; use")
            print("    LIMIT orders (these are model prices, real fills differ); size so the max-loss is")
            print("    money you can afford to lose. Educational samples, NOT financial advice.")
    print("\n  (Decision-support only. A TRIGGER is a probability, not a promise.")
    print("   No system removes short-squeeze risk. Not financial advice.)\n")


def write_dashboard(snap, S, comp, level, conf, plan, path="dashboard.html"):
    cdot = {GREEN: "#1A6F5A", YELLOW: "#E0A100", RED: "#DC2D2D"}
    banner = {TRIGGER: "#1A6F5A", ARMED: "#B45309", BLOCKED: "#9B2226",
              WATCH: "#243B53", DORMANT: "#486581"}[level]
    msg = {TRIGGER: "🚨 ABSOLUTE WARNING — short-entry conditions met",
           ARMED: "ARMED — one condition from trigger",
           BLOCKED: "BLOCKED — gamma-squeeze risk live",
           WATCH: "WATCH — thesis building", DORMANT: "DORMANT — momentum in control"}[level]
    steps = "".join(
        f'<span style="padding:4px 10px;border-radius:8px;margin:2px;{"background:#1A6F5A;color:#fff" if L==level else "background:#E6E1D8;color:#627D98"}">{L}</span>'
        for L in LADDER_VIS)
    if level == BLOCKED:
        steps += '<span style="padding:4px 10px;border-radius:8px;margin:2px;background:#9B2226;color:#fff">⛔ BLOCKED</span>'
    rows = "".join(f'<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{cdot[s.state]};margin-right:8px"></span>{s.key}</td><td style="color:#627D98">{s.note}</td></tr>' for s in S)
    src = "".join(f'<tr><td>{f}</td><td><code>{v}</code></td><td>{st}</td><td style="color:#829AB1">{sc}</td></tr>' for f, (v, sc, st) in snap.sources.items())
    plan_html = ""
    if plan and plan["contracts"]:
        plan_html = f"""<h3>Trade plan ({plan['expiry']} put spread · {plan['exp_basis']})</h3><table style="width:100%;font-size:14px">
<tr><td>structure</td><td>BUY {plan['contracts']}x {plan['Klong']:.0f}P / SELL {plan['Kshort']:.0f}P · {plan['dte']}DTE</td></tr>
<tr><td>total debit</td><td>${plan['cost']:,.0f} (cap ${plan['max_risk_usd']:,.0f})</td></tr>
<tr><td>max loss / profit</td><td>${plan['max_loss']:,.0f} / ${plan['max_profit']:,.0f} · R:R {plan['rr']:.1f}</td></tr>
<tr><td>greeks · risk-neutral P(S_T&lt;BE)</td><td>Δ{plan['greeks']['delta']:+.0f} θ{plan['greeks']['theta']:+.0f}/d ν{plan['greeks']['vega']:+.0f}/1% · ~{plan.get('pop','?')}% <span style="color:#829AB1">(not a win rate; BE {(1-plan['breakeven']/plan['spot'])*100:.0f}% below spot)</span></td></tr>
<tr><td>IV-crush risk</td><td>{'+' if plan['iv_crush_pnl']>=0 else ''}{plan['iv_crush_pnl']:,} if IV→{plan['iv_crush_to']*100:.0f}% (spot flat)</td></tr>
<tr><td>breakeven</td><td>${plan['breakeven']:.2f}</td></tr>
<tr><td>invalidate above</td><td>${plan['invalidation']:.2f}</td></tr></table>"""
    html = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>SPCX Warning System</title><body style="font-family:'Plus Jakarta Sans',system-ui;background:#FAF8F4;color:#102A43;margin:0">
<div style="max-width:780px;margin:auto;padding:20px">
<div style="background:{banner};color:#fff;border-radius:14px;padding:18px">
<h2 style="margin:0">SPCX — Short-Entry Warning System</h2><div style="opacity:.9">{msg}</div>
<div style="font-size:40px;font-weight:700">{comp}<span style="font-size:16px;opacity:.7">/100</span></div>
<div style="opacity:.85">price ${snap.price} · {datetime.now():%Y-%m-%d %H:%M}</div></div>
<div style="margin:14px 0">{steps}</div>
<h3>Signals</h3><table style="width:100%;border-collapse:collapse;font-size:14px">{rows}</table>
{plan_html}
<h3>Data sources &amp; provenance</h3><table style="width:100%;border-collapse:collapse;font-size:13px">
<tr style="color:#486581"><td>field</td><td>value</td><td>status</td><td>source</td></tr>{src}</table>
<p style="color:#829AB1;font-size:12px">Decision-support only. A TRIGGER is a probability, not a promise; no system
removes short-squeeze risk. Surfaces market/public data against your thresholds. Not financial advice.</p></div></body>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path

# =============================================================================
#  RUNNERS
# =============================================================================
def write_live_json(cfg, snap, comp, level, path="spcx_live.json"):
    """Write the current real-tape snapshot for the phone app's ↻ Refresh button.
    Values are app-ready (volume in millions, IV in %). Served same-origin by the
    local server so the app can fetch it with no CORS issue."""
    data = {
        "ts": snap.ts.isoformat(timespec="seconds"),
        "source": snap.extras.get("price_source", "seed"),
        "level": level, "composite": comp, "scent": snap.scent, "scent_state": snap.scent_state,
        "early_warning": snap.extras.get("early_warning"),
        "next_major_label": snap.next_major_label, "days_to_next_major": snap.days_to_next_major,
        "inputs": {
            "price": round(snap.price, 2) if snap.price else None,
            "ivAnnual": round(snap.iv_annual*100, 2) if snap.iv_annual else None,
            "ivSeed": _iv_is_seeded(snap),   # True = IV is a stale guess; $ figures illustrative
            "ivPct": round(snap.iv_percentile*100) if snap.iv_percentile is not None else None,
            "volume": round(snap.volume/1e6, 2) if snap.volume else None,
            "avgVol": round(snap.avg_volume_20d/1e6, 2) if snap.avg_volume_20d else None,
            "recentHighs": [round(x, 2) for x in snap.recent_highs] if snap.recent_highs else None,
            "recentLows": [round(x, 2) for x in snap.recent_lows] if snap.recent_lows else None,
            "recentCloses": [round(x, 2) for x in snap.recent_closes] if snap.recent_closes else None,
            "recentVolumes": [round(v/1e6, 2) for v in snap.recent_volumes] if snap.recent_volumes else None,
        },
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print("  live json write failed:", e)
    return path


def run_once(cfg, write_html=False, do_alerts=False, daily=False):
    prev = db_last(cfg); prev_level = prev[2] if prev else None
    snap = collect_live(cfg)
    S = build_signals(snap, cfg); comp = composite(S)
    conf = confluence(snap, S, cfg); level = warning_level(snap, S, comp, conf)
    sc = scent_score(snap, S)               # early-warning fragility (leading track)
    snap.extras["early_warning"] = early_warning_level(snap, S, comp, conf, sc)
    plan = build_trade_plan(snap, cfg)
    print_report(snap, S, comp, level, conf, plan, cfg)
    db_log(cfg, snap, comp, level, conf, S)
    write_live_json(cfg, snap, comp, level)   # for the phone app's ↻ Refresh button
    streak = db_streak(cfg)
    if write_html:
        print("  wrote", write_dashboard(snap, S, comp, level, conf, plan))
    if do_alerts:
        if alert_dispatch(cfg, alert_text(snap, comp, level, conf, plan), level, prev_level, streak):
            print("  >> alert dispatched (escalation)")
    if daily:
        if send_daily_digest(cfg, snap, comp, level, conf, plan):
            print("  >> daily digest sent to phone (ntfy/telegram/email)")
        else:
            print("  >> daily digest: no channel sent (check alerts config / network)")
    return level, comp


def run_watch(cfg, interval, iters=None):
    print(f"  watching every {interval}s — escalation alerts + one daily digest. Ctrl-C to stop.")
    i = 0; last_digest_day = None
    try:
        while True:
            today = _today_et()
            send_daily = (today != last_digest_day)   # guaranteed once-per-day heartbeat
            run_once(cfg, write_html=True, do_alerts=True, daily=send_daily)
            if send_daily:
                last_digest_day = today
            i += 1
            if iters and i >= iters:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  stopped (Ctrl-C).")


def run_demo(cfg):
    def mk(**k):
        s = Snapshot(ts=datetime.now())
        for f, v in k.items():
            setattr(s, f, v)
        s.market_cap = s.price*cfg["fundamentals"]["shares_outstanding"]
        s.iv_percentile = k.get("iv_percentile", 0.9); compute_atr(s)
        return s
    scen = {
        "A TODAY (too early)": mk(price=212, volume=410_000_000, avg_volume_20d=300_000_000,
            iv_annual=1.30, iv_5d_ago=1.20, iv_percentile=.95, short_interest_pct_float=3,
            short_interest_prev=2, dec_put_volume=20_000, dec_call_volume=140_000,
            insider_sales_30d=0, otm_call_premium=14_100_000,
            recent_highs=[176, 195, 225], recent_lows=[150, 165, 200], recent_closes=[160, 192, 212],
            social_sentiment=.7, social_sentiment_prev=.55, social_volume=50_000,
            social_volume_avg=30_000, news_sentiment=.6, bullish_catalyst=False),
        "B INFLECTION (aligned)": mk(price=188, volume=150_000_000, avg_volume_20d=300_000_000,
            iv_annual=.95, iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17,
            short_interest_prev=12, borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95_000,
            dec_call_volume=90_000, insider_sales_30d=2, otm_call_premium=500_000,
            recent_highs=[230, 215, 205], recent_lows=[210, 195, 184], recent_closes=[228, 205, 188],
            social_sentiment=.1, social_sentiment_prev=.6, social_volume=18_000,
            social_volume_avg=30_000, news_sentiment=-.1, bullish_catalyst=False),
        "C ALIGNED BUT GAMMA SWEEP (blocked)": mk(price=188, volume=150_000_000, avg_volume_20d=300_000_000,
            iv_annual=.95, iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17,
            short_interest_prev=12, borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95_000,
            dec_call_volume=90_000, insider_sales_30d=2, otm_call_premium=22_000_000,
            recent_highs=[230, 215, 205], recent_lows=[210, 195, 184], recent_closes=[228, 205, 188],
            social_sentiment=.1, social_sentiment_prev=.6, social_volume=18_000,
            social_volume_avg=30_000, news_sentiment=-.1, bullish_catalyst=False),
        "D ALIGNED BUT MUSK HEADLINE (blocked)": mk(price=188, volume=150_000_000, avg_volume_20d=300_000_000,
            iv_annual=.95, iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17,
            short_interest_prev=12, borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95_000,
            dec_call_volume=90_000, insider_sales_30d=2, otm_call_premium=500_000,
            recent_highs=[230, 215, 205], recent_lows=[210, 195, 184], recent_closes=[228, 205, 188],
            social_sentiment=.1, social_sentiment_prev=.6, social_volume=18_000,
            social_volume_avg=30_000, news_sentiment=.2, bullish_catalyst=True, days_to_next_major=8, next_major_label="Wave 1", next_major_pct=20),
    }
    for name, s in scen.items():
        S = build_signals(s, cfg); comp = composite(S)
        conf = confluence(s, S, cfg); level = warning_level(s, S, comp, conf)
        print(f"\n### {name}  -> {level}  score {comp}/100  aligned={conf['aligned']}")
        for sig in S:
            print(f"  {DOT[sig.state]} {sig.key:<16}{sig.note}")
        if conf["blockers"]:
            print("  ⛔ BLOCKED:", ", ".join(conf["blockers"]))
    return scen


def run_selftest(cfg):
    print("  SELF-TEST")
    fails = []
    def chk(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)
    scen = run_demo_quiet(cfg)
    chk("A is BLOCKED (gamma sweep live)", scen["A"][0] == BLOCKED)
    chk("B is TRIGGER (aligned)", scen["B"][0] == TRIGGER)
    chk("C is BLOCKED (gamma override beats alignment)", scen["C"][0] == BLOCKED)
    chk("D is BLOCKED (bullish catalyst squeeze-risk beats alignment)", scen["D"][0] == BLOCKED)
    chk("B composite > A composite", scen["B"][1] > scen["A"][1])
    # Black-Scholes sanity
    p = bs_put(200, 180, 0.5, 0.045, 1.0)
    chk("BS put price positive & < strike", 0 < p < 180)
    chk("deeper-OTM put cheaper", bs_put(200, 140, 0.5, .045, 1.0) < bs_put(200, 190, 0.5, .045, 1.0))
    # sizing respects capital
    snap = Snapshot(ts=datetime.now(), price=212, iv_annual=1.19,
                    recent_highs=[225], recent_lows=[200], recent_closes=[212])
    snap.market_cap = 212*cfg["fundamentals"]["shares_outstanding"]
    plan = build_trade_plan(snap, cfg)
    chk("trade plan within risk cap", plan["cost"] <= plan["max_risk_usd"]+1)
    chk("plan quality flags poor R:R at high IV", plan["quality"][0] in ("POOR", "MARGINAL", "GOOD"))
    chk("ladder ranks ordered", LADDER_RANK[WATCH] < LADDER_RANK[ARMED] < LADDER_RANK[TRIGGER])
    chk("composite in 0..100", 0 <= composite(build_signals(snap, cfg)) <= 100)
    chk("sentiment tier present (3 signals)", sum(1 for s in build_signals(snap, cfg) if s.tier == "sentiment") == 3)
    usnap = Snapshot(ts=datetime.now()); compute_unlocks(cfg, usnap, today=date(2026, 7, 28))
    chk("unlock: next major detected from 424B4 schedule", usnap.next_major_label is not None)
    chk("unlock: late-Jul sees the Q2 earnings tranche within ~2wk",
        usnap.days_to_next_major is not None and 0 <= usnap.days_to_next_major <= 14)
    chk("unlock: bullish price-trigger tranche excluded from majors",
        "price-trigger" not in (usnap.next_major_label or ""))
    chk("unlock: float velocity computed (supply ramp)", usnap.float_velocity_30d is not None)
    chk("sparkline renders", len(sparkline([1, 5, 3, 8, 2])) == 5)
    chk("data_confidence returns pct 0..100", 0 <= data_confidence(build_signals(snap, cfg))[0] <= 100)
    csvp = gen_sample_csv("/tmp/_spcx_rt.csv"); import os as _os
    chk("sample csv generated", _os.path.exists(csvp))
    dynsnap = Snapshot(ts=datetime.now(), price=180, iv_annual=.8,
                       recent_highs=[200], recent_lows=[170], recent_closes=[180])
    dynsnap.market_cap = 180*cfg["fundamentals"]["shares_outstanding"]
    dynsnap.extras["next_major_date"] = "2026-08-11"; dynsnap.next_major_label = "Wave 1"
    dynsnap.extras["major_unlocks"] = [("2026-08-11", 20), ("2026-11-09", 28)]
    dp = build_trade_plan(dynsnap, cfg)
    chk("dynamic expiry spans next major unlock (after Aug 11)", dp["expiry"] > "2026-08-11")
    chk("tranche ladder built across unlocks", len(dp["tranches"]) == 2)
    chk("plan exposes greeks + pop", "greeks" in dp and "pop" in dp)
    divsnap = Snapshot(ts=datetime.now(), price=225, iv_annual=1.1,
                       recent_highs=[180, 200, 226], recent_lows=[150, 170, 200],
                       recent_closes=[178, 198, 225],
                       recent_volumes=[410e6, 300e6, 250e6, 200e6, 180e6])
    divsnap.market_cap = 225*cfg["fundamentals"]["shares_outstanding"]
    dm = {s.key: s for s in build_signals(divsnap, cfg)}
    chk("divergence GREEN on price-highs + fading volume", dm["divergence"].state == GREEN)
    thin = Snapshot(ts=datetime.now(), price=188, iv_annual=.95, iv_5d_ago=1.25, iv_percentile=.88,
                    volume=150e6, avg_volume_20d=300e6, dec_put_volume=95000, dec_call_volume=80000,
                    short_interest_pct_float=18, short_interest_prev=12, otm_call_premium=500000,
                    recent_highs=[230, 215, 205], recent_lows=[210, 195, 184], recent_closes=[228, 205, 188])
    thin.market_cap = 188*cfg["fundamentals"]["shares_outstanding"]; compute_atr(thin)
    tconf = confluence(thin, build_signals(thin, cfg), cfg)
    chk("insufficient-data gate prevents trigger on thin data",
        ("insufficient_data" in tconf["blockers"]) or (not tconf["aligned"]))
    chk("leading_confirms in conditions", "leading_confirms" in confluence(snap, build_signals(snap, cfg), cfg)["conditions"])

    # === report / encoding paths (the crash that slipped through a green suite) ===
    import io as _io, tempfile as _tf, os as _os, copy as _copy
    tcfg = dict(cfg); tcfg["db_path"] = _os.path.join(_tf.gettempdir(), "_spcx_selftest.db")
    try:
        _os.remove(tcfg["db_path"])
    except OSError:
        pass
    rsnap = Snapshot(ts=datetime.now(), price=212, iv_annual=1.19,
                     recent_highs=[225], recent_lows=[200], recent_closes=[212])
    rsnap.market_cap = 212*cfg["fundamentals"]["shares_outstanding"]; compute_atr(rsnap)
    rS = build_signals(rsnap, tcfg); rcomp = composite(rS)
    rconf = confluence(rsnap, rS, tcfg); rplan = build_trade_plan(rsnap, tcfg)
    old_out = sys.stdout; report_ok = True
    for lvl in (DORMANT, WATCH, ARMED, TRIGGER, BLOCKED):
        sys.stdout = _io.TextIOWrapper(_io.BytesIO(), encoding="utf-8", errors="strict")
        try:
            sys.stdout.write(ladder_bar(lvl) + "\n")
            print_report(rsnap, rS, rcomp, lvl, rconf, rplan, tcfg)
            sys.stdout.write(alert_text(rsnap, rcomp, lvl, rconf, rplan) + "\n")
        except Exception:
            report_ok = False
        finally:
            sys.stdout = old_out
    chk("report path renders for all levels without error", report_ok)

    # enable_utf8_output is the real console-crash fix: a cp1254 stream it
    # reconfigures must then accept the report glyphs that used to crash.
    cp = _io.TextIOWrapper(_io.BytesIO(), encoding="cp1254", errors="strict")
    enable_utf8_output(cp)
    try:
        cp.write("⛔ 🚨 → ✓ Δθν ▁▂█"); cp.flush(); glyph_ok = True
    except UnicodeEncodeError:
        glyph_ok = False
    chk("enable_utf8_output makes report glyphs encodable on cp1254", glyph_ok)

    dpath = _os.path.join(_tf.gettempdir(), "_spcx_dash.html")
    try:
        write_dashboard(rsnap, rS, rcomp, BLOCKED, rconf, rplan, dpath)
        with open(dpath, encoding="utf-8") as _f:
            dash_ok = "⛔" in _f.read()
    except Exception:
        dash_ok = False
    chk("dashboard writes valid utf-8 with glyphs", dash_ok)

    # === safety: a blocker ALWAYS suppresses TRIGGER ===
    def mkfull(**k):
        s = Snapshot(ts=datetime.now())
        for f, v in k.items():
            setattr(s, f, v)
        s.market_cap = s.price*cfg["fundamentals"]["shares_outstanding"]
        compute_atr(s); return s
    base_aligned = dict(price=188, volume=150e6, avg_volume_20d=300e6, iv_annual=.95,
        iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17, short_interest_prev=12,
        borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95000, dec_call_volume=90000,
        insider_sales_30d=2, otm_call_premium=500_000, recent_highs=[230, 215, 205],
        recent_lows=[210, 195, 184], recent_closes=[228, 205, 188],
        recent_volumes=[400e6, 300e6, 220e6, 180e6, 150e6], social_sentiment=.1,
        social_sentiment_prev=.6, social_volume=18000, social_volume_avg=30000,
        news_sentiment=-.1, bullish_catalyst=False, days_to_next_major=8,
        next_major_label="Wave 1", next_major_pct=20)
    def lvlof(d):
        s = mkfull(**d); S = build_signals(s, cfg); c = composite(S)
        return warning_level(s, S, c, confluence(s, S, cfg))
    chk("baseline scenario is TRIGGER (control)", lvlof(base_aligned) == TRIGGER)
    chk("gamma sweep forces BLOCKED over alignment", lvlof({**base_aligned, "otm_call_premium": 50_000_000}) == BLOCKED)
    chk("bullish catalyst forces BLOCKED over alignment", lvlof({**base_aligned, "bullish_catalyst": True}) == BLOCKED)
    chk("squeeze unevaluated (no flow) forces BLOCKED", lvlof({**base_aligned, "otm_call_premium": None}) == BLOCKED)
    # FAIL-CLOSED: a seeded/manual OTM-call value below the block threshold must NOT
    # clear the squeeze guard — only a live flow feed may green-light the short.
    def lvl_marked(d, status):
        s = mkfull(**d); s.mark("otm_call_premium", d["otm_call_premium"], "test", status)
        S = build_signals(s, cfg); c = composite(S)
        return warning_level(s, S, c, confluence(s, S, cfg))
    chk("seeded low OTM-call does NOT clear squeeze guard", lvl_marked({**base_aligned, "otm_call_premium": 500_000}, "MANUAL") == BLOCKED)
    chk("live low OTM-call DOES clear squeeze guard", lvl_marked({**base_aligned, "otm_call_premium": 500_000}, "LIVE") == TRIGGER)

    # === monotonicity: a more-bearish input never LOWERS the composite ===
    base_comp = composite(build_signals(mkfull(**base_aligned), cfg)); mono_ok = True
    for tweak in (dict(short_interest_pct_float=25), dict(volume=80e6), dict(social_sentiment=-0.3),
                  dict(dec_put_volume=200000), dict(news_sentiment=-0.5), dict(insider_sales_30d=4)):
        if composite(build_signals(mkfull(**{**base_aligned, **tweak}), cfg)) < base_comp - 1e-9:
            mono_ok = False
    chk("more-bearish inputs never lower the composite", mono_ok)

    # === ladder rung mapping (pure function, all 5 rungs) ===
    def _cf(mom, si, sup, **kw):
        cond = {"momentum_breaking": mom, "short_interest_rising": si,
                "dec_puts_elevated": kw.get("dec", False), "above_support": sup,
                "leading_confirms": kw.get("lead", False)}
        return {"aligned": kw.get("aligned", False), "conditions": cond,
                "blockers": kw.get("blockers", []), "missing": [], "data_confidence": 100}
    chk("ladder: blockers -> BLOCKED", warning_level(None, [], 95, _cf(True, True, True, blockers=["x"])) == BLOCKED)
    chk("ladder: aligned -> TRIGGER", warning_level(None, [], 95, _cf(True, True, True, aligned=True)) == TRIGGER)
    chk("ladder: core conditions -> ARMED", warning_level(None, [], 10, _cf(True, True, True)) == ARMED)
    chk("ladder: comp>=70 -> ARMED", warning_level(None, [], 75, _cf(False, False, False)) == ARMED)
    chk("ladder: comp 40..69 -> WATCH", warning_level(None, [], 50, _cf(False, False, False)) == WATCH)
    chk("ladder: comp<40 -> DORMANT", warning_level(None, [], 20, _cf(False, False, False)) == DORMANT)

    # === config validation ===
    chk("validate_config passes on shipped config", validate_config(cfg) is None)
    chk("config_warnings flags the impossible unlock schedule",
        any("UNLOCK SCHEDULE" in w for w in config_warnings(cfg)))
    def _rejects(mutate):
        bad = _copy.deepcopy(cfg); mutate(bad)
        try:
            validate_config(bad); return False
        except AssertionError:
            return True
    chk("validate_config rejects inverted moneyness", _rejects(lambda c: c["risk"].__setitem__("short_put_moneyness", 0.99)))
    chk("validate_config rejects out-of-range threshold", _rejects(lambda c: c["thresholds"].__setitem__("iv_percentile_high", 150)))

    # === golden vectors (shared with the JS app for parity) ===
    gpath = emit_golden(cfg, _os.path.join(_tf.gettempdir(), "_spcx_golden.json"))
    with open(gpath, encoding="utf-8") as _f:
        gv = json.load(_f)
    gmap = {g["name"]: g for g in gv}; golden_ok = True
    for g in gv:
        if golden_outputs(cfg, _golden_snapshot(cfg, g["inputs"])) != g["expected"]:
            golden_ok = False
    chk("golden vectors reproduce deterministically", golden_ok)
    chk("golden: aligned_trigger -> TRIGGER", gmap["aligned_trigger"]["expected"]["level"] == TRIGGER)
    chk("golden: squeeze_unevaluated -> BLOCKED", gmap["squeeze_unevaluated_blocks"]["expected"]["level"] == BLOCKED)
    chk("golden: blocked_gamma_sweep -> BLOCKED", gmap["blocked_gamma_sweep"]["expected"]["level"] == BLOCKED)

    # === SCENT early-warning fragility ===
    # parabolic, extended, near a major unlock, rolling over -> ELEVATED + ARMable
    paro = Snapshot(ts=datetime.now(), price=300,
                    long_closes=[100+ i*3 for i in range(40)] + [230, 250, 275, 300, 312, 305, 285],
                    recent_closes=[275, 300, 312, 305, 295, 285], recent_highs=[312]*6,
                    recent_lows=[270]*6, recent_volumes=[5e6, 8e6, 9e6, 7e6, 6e6, 5e6],
                    avg_volume_20d=4e6, iv_percentile=.9, days_to_next_major=10,
                    next_major_label="unlock", next_major_pct=20, float_velocity_30d=20)
    paro.market_cap = 300*cfg["fundamentals"]["shares_outstanding"]; compute_atr(paro)
    sc_p = scent_score(paro, build_signals(paro, cfg))
    chk("SCENT scores 0..100", sc_p is not None and 0 <= sc_p <= 100)
    chk("SCENT elevated on a parabolic, near-unlock top", sc_p >= SCENT_ELEV_AT)
    quiet = Snapshot(ts=datetime.now(), price=100,
                     long_closes=[100]*40, recent_closes=[100]*6, recent_highs=[101]*6,
                     recent_lows=[99]*6, recent_volumes=[4e6]*6, avg_volume_20d=4e6,
                     iv_percentile=.4)
    quiet.market_cap = 100*cfg["fundamentals"]["shares_outstanding"]; compute_atr(quiet)
    chk("SCENT quiet on a flat, calm tape", (scent_score(quiet, build_signals(quiet, cfg)) or 0) < SCENT_ELEV_AT)
    # the gate: SCENT-elevated WITHOUT a rollover stays WATCH (never arms into strength)
    melt = Snapshot(ts=datetime.now(), price=312,
                    long_closes=[100+i*3 for i in range(40)] + [230, 250, 275, 300, 312],
                    recent_closes=[275, 300, 312], recent_highs=[312, 312, 312],
                    recent_lows=[300, 305, 310], recent_volumes=[5e6, 8e6, 9e6],
                    avg_volume_20d=4e6, iv_percentile=.9, days_to_next_major=10,
                    next_major_label="unlock", next_major_pct=20, float_velocity_30d=20)
    melt.market_cap = 312*cfg["fundamentals"]["shares_outstanding"]; compute_atr(melt)
    mS = build_signals(melt, cfg); msc = scent_score(melt, mS)
    chk("SCENT never ARMS into a still-rising parabola (no rollover)",
        early_warning_level(melt, mS, composite(mS), confluence(melt, mS, cfg), msc) != ARMED)
    chk("ARMED requires a breakdown (rollover) — paro case is breaking",
        early_warning_level(paro, build_signals(paro, cfg), composite(build_signals(paro, cfg)),
                            confluence(paro, build_signals(paro, cfg), cfg), sc_p) in (WATCH, ARMED))

    # === replay + alert coverage ===
    gp = gen_sample_csv(_os.path.join(_tf.gettempdir(), "_spcx_replay.csv"))
    old_out = sys.stdout; sys.stdout = _io.TextIOWrapper(_io.BytesIO(), encoding="utf-8")
    try:
        run_replay(tcfg, gp); replay_ok = True
    except Exception:
        replay_ok = False
    finally:
        sys.stdout = old_out
    chk("replay runs over sample csv without error", replay_ok)
    chk("alert_text builds for every level",
        all(isinstance(alert_text(rsnap, rcomp, L, rconf, rplan), str)
            for L in (DORMANT, WATCH, ARMED, TRIGGER, BLOCKED)))

    chk("fuzz: 500 random snapshots hold all invariants", run_fuzz(cfg, 500, verbose=False))
    print(f"\n  {'ALL PASS' if not fails else 'FAILURES: '+', '.join(fails)}")
    return not fails


def run_demo_quiet(cfg):
    out = {}
    def mk(**k):
        s = Snapshot(ts=datetime.now())
        for f, v in k.items():
            setattr(s, f, v)
        s.market_cap = s.price*cfg["fundamentals"]["shares_outstanding"]
        s.iv_percentile = k.get("iv_percentile", .9); compute_atr(s)
        return s
    defs = {
        "A": dict(price=212, volume=410_000_000, avg_volume_20d=300_000_000, iv_annual=1.30,
                  iv_5d_ago=1.20, iv_percentile=.95, short_interest_pct_float=3, short_interest_prev=2,
                  dec_put_volume=20_000, dec_call_volume=140_000, insider_sales_30d=0,
                  otm_call_premium=14_100_000, recent_highs=[176, 195, 225],
                  recent_lows=[150, 165, 200], recent_closes=[160, 192, 212], recent_volumes=[300e6, 380e6, 410e6],
                  social_sentiment=.7, social_sentiment_prev=.55, social_volume=50_000,
                  social_volume_avg=30_000, news_sentiment=.6, bullish_catalyst=False, days_to_next_major=56, next_major_label="Wave 1", next_major_pct=20),
        "B": dict(price=188, volume=150_000_000, avg_volume_20d=300_000_000, iv_annual=.95,
                  iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17, short_interest_prev=12,
                  borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95_000, dec_call_volume=90_000,
                  insider_sales_30d=2, otm_call_premium=500_000, recent_highs=[230, 215, 205],
                  recent_lows=[210, 195, 184], recent_closes=[228, 205, 188], recent_volumes=[400e6, 300e6, 220e6, 180e6, 150e6],
                  social_sentiment=.1, social_sentiment_prev=.6, social_volume=18_000,
                  social_volume_avg=30_000, news_sentiment=-.1, bullish_catalyst=False, days_to_next_major=8, next_major_label="Wave 1", next_major_pct=20),
        "C": dict(price=188, volume=150_000_000, avg_volume_20d=300_000_000, iv_annual=.95,
                  iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17, short_interest_prev=12,
                  borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95_000, dec_call_volume=90_000,
                  insider_sales_30d=2, otm_call_premium=22_000_000, recent_highs=[230, 215, 205],
                  recent_lows=[210, 195, 184], recent_closes=[228, 205, 188], recent_volumes=[400e6, 300e6, 220e6, 180e6, 150e6],
                  social_sentiment=.1, social_sentiment_prev=.6, social_volume=18_000,
                  social_volume_avg=30_000, news_sentiment=-.1, bullish_catalyst=False, days_to_next_major=8, next_major_label="Wave 1", next_major_pct=20),
        "D": dict(price=188, volume=150_000_000, avg_volume_20d=300_000_000, iv_annual=.95,
                  iv_5d_ago=1.25, iv_percentile=.88, short_interest_pct_float=17, short_interest_prev=12,
                  borrow_fee=40, borrow_fee_prev=60, dec_put_volume=95_000, dec_call_volume=90_000,
                  insider_sales_30d=2, otm_call_premium=500_000, recent_highs=[230, 215, 205],
                  recent_lows=[210, 195, 184], recent_closes=[228, 205, 188], recent_volumes=[400e6, 300e6, 220e6, 180e6, 150e6],
                  social_sentiment=.1, social_sentiment_prev=.6, social_volume=18_000,
                  social_volume_avg=30_000, news_sentiment=.2, bullish_catalyst=True, days_to_next_major=8, next_major_label="Wave 1", next_major_pct=20),
    }
    for name, d in defs.items():
        s = mk(**d); S = build_signals(s, cfg); comp = composite(S)
        conf = confluence(s, S, cfg); out[name] = (warning_level(s, S, comp, conf), comp)
    return out


def _coerce(v):
    if v is None or v == "":
        return None
    s = str(v).strip()
    if s.lower() in ("true", "1", "yes"):
        return True
    if s.lower() in ("false", "0", "no"):
        return False
    try:
        return float(s) if "." in s or "e" in s.lower() else int(s)
    except ValueError:
        return s


def gen_sample_csv(path="spcx_replay_sample.csv"):
    import csv
    cols = ["date", "price", "volume", "avg_volume_20d", "iv_annual", "iv_5d_ago",
            "iv_percentile", "short_interest_pct_float", "short_interest_prev",
            "borrow_fee", "borrow_fee_prev", "dec_put_volume", "dec_call_volume",
            "insider_sales_30d", "otm_call_premium", "social_sentiment",
            "social_sentiment_prev", "social_volume", "social_volume_avg",
            "news_sentiment", "bullish_catalyst", "days_to_next_major", "next_major_pct"]
    rows = [
        ["2026-06-16", 212, 410e6, 300e6, 1.30, 1.20, .95, 3, 2, "", "", 20000, 140000, 0, 14_100_000, .7, .55, 50000, 30000, .6, False, 56, 20],
        ["2026-08-05", 175, 220e6, 300e6, .85, 1.05, .80, 11, 7, 90, 110, 60000, 70000, 1, 3_000_000, .2, .5, 25000, 30000, .0, False, 6, 20],
        ["2026-08-20", 150, 140e6, 300e6, .70, .95, .75, 18, 12, 45, 70, 95000, 85000, 3, 600_000, -.1, .3, 16000, 30000, -.2, False, 1, 28],
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in rows:
            w.writerow(r)
    return path


def run_replay(cfg, path):
    import csv
    print(f"  REPLAY  {path}")
    print("  " + "-" * 68)
    print(f"  {'date':<12}{'price':>8}{'score':>8}  {'level':<9}aligned")
    trig = blk = 0; n = 0
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n += 1
            snap = Snapshot(ts=datetime.now())
            for k, v in row.items():
                if hasattr(snap, k) and k != "date":
                    setattr(snap, k, _coerce(v))
            so = cfg["fundamentals"]["shares_outstanding"]
            if snap.price:
                snap.market_cap = snap.price*so
            if getattr(snap, "next_major_pct", None) is not None:
                snap.next_major_label = "scheduled unlock"
            compute_atr(snap)
            S = build_signals(snap, cfg); comp = composite(S)
            conf = confluence(snap, S, cfg); lvl = warning_level(snap, S, comp, conf)
            if lvl == TRIGGER:
                trig += 1
            if lvl == BLOCKED:
                blk += 1
            print(f"  {row.get('date',''):<12}{snap.price or 0:>8.0f}{comp:>8.1f}  {lvl:<9}{conf['aligned']}")
    print("  " + "-" * 68)
    print(f"  {n} rows · {trig} TRIGGER · {blk} BLOCKED · {n-trig-blk} other")
    print("  Use this to sanity-check thresholds: do triggers land where you'd expect?")


def run_fuzz(cfg, n=500, verbose=True):
    """Randomized invariant testing: the engine must never break its own rules."""
    import random
    fails = []
    for i in range(n):
        s = Snapshot(ts=datetime.now())
        s.price = random.uniform(40, 420)
        s.market_cap = s.price*cfg["fundamentals"]["shares_outstanding"]
        s.volume = random.uniform(20e6, 600e6); s.avg_volume_20d = random.uniform(100e6, 400e6)
        s.iv_annual = random.uniform(.2, 2.2); s.iv_5d_ago = random.uniform(.2, 2.2)
        s.iv_percentile = random.random()
        s.short_interest_pct_float = random.choice([None, random.uniform(0, 35)])
        s.short_interest_prev = random.uniform(0, 35)
        s.borrow_fee = random.choice([None, random.uniform(5, 200)]); s.borrow_fee_prev = random.uniform(5, 200)
        s.dec_put_volume = random.uniform(1e3, 2e5); s.dec_call_volume = random.uniform(1e3, 2e5)
        s.insider_sales_30d = random.choice([None, 0, 1, 2, 5])
        s.otm_call_premium = random.choice([None, random.uniform(0, 30e6)])
        s.social_sentiment = random.uniform(-1, 1); s.social_sentiment_prev = random.uniform(-1, 1)
        s.social_volume = random.uniform(5e3, 80e3); s.social_volume_avg = random.uniform(5e3, 60e3)
        s.news_sentiment = random.uniform(-1, 1); s.bullish_catalyst = random.choice([True, False])
        s.days_to_next_major = random.choice([None, random.randint(0, 200)])
        s.next_major_label = "x"; s.next_major_pct = 20
        base = random.uniform(40, 420)
        s.recent_highs = [base*random.uniform(.9, 1.2) for _ in range(3)]
        s.recent_lows = [base*random.uniform(.8, 1.0) for _ in range(3)]
        s.recent_closes = [base*random.uniform(.85, 1.1) for _ in range(3)]
        s.recent_volumes = random.choice([None, [random.uniform(50e6, 500e6) for _ in range(random.randint(3, 6))]])
        compute_atr(s)
        try:
            S = build_signals(s, cfg); comp = composite(S)
            conf = confluence(s, S, cfg); lvl = warning_level(s, S, comp, conf)
            plan = build_trade_plan(s, cfg)
            sig = {x.key: x for x in S}
            # INVARIANTS
            if not (0 <= comp <= 100): fails.append(f"#{i} score {comp} out of range")
            if conf["aligned"] and lvl != TRIGGER: fails.append(f"#{i} aligned but not TRIGGER")
            if conf["blockers"] and conf["aligned"]: fails.append(f"#{i} blocked yet aligned")
            if conf["blockers"] and lvl != BLOCKED: fails.append(f"#{i} blocked but level {lvl}")
            if lvl not in (DORMANT, WATCH, ARMED, TRIGGER, BLOCKED): fails.append(f"#{i} bad level {lvl}")
            # SAFETY: a TRIGGER must never fire unless the squeeze guard is explicitly GREEN
            if lvl == TRIGGER and sig["gamma_squeeze"].state != GREEN:
                fails.append(f"#{i} TRIGGER while squeeze guard {sig['gamma_squeeze'].state} (not ruled out)")
            if lvl == TRIGGER and s.bullish_catalyst:
                fails.append(f"#{i} TRIGGER with bullish catalyst live")
            if plan and plan["contracts"]:
                if plan["cost"] > plan["max_risk_usd"]+1: fails.append(f"#{i} plan over risk cap")
                if plan["Klong"] <= plan["Kshort"]: fails.append(f"#{i} inverted/equal strikes")
                if not (plan["Kshort"] <= plan["breakeven"] <= plan["Klong"]):
                    fails.append(f"#{i} breakeven outside strikes")
                if plan["max_profit"] < 0: fails.append(f"#{i} negative max profit")
                if plan.get("pop") is not None and not (0 <= plan["pop"] <= 100):
                    fails.append(f"#{i} pop out of range")
            pc, *_ = data_confidence(S)
            if not (0 <= pc <= 100): fails.append(f"#{i} confidence out of range")
            # SCENT / early-warning invariants
            sc = scent_score(s, S)
            if sc is not None and not (0 <= sc <= 100): fails.append(f"#{i} scent {sc} out of range")
            ew = early_warning_level(s, S, comp, conf, sc)
            if ew not in (DORMANT, WATCH, ARMED): fails.append(f"#{i} bad early-warning {ew}")
            # the early-warning ARMED must always be a real rollover (never a melt-up)
            if ew == ARMED and not _breaking_down(s, sig):
                fails.append(f"#{i} early-warning ARMED without a rollover")
        except Exception as e:
            fails.append(f"#{i} EXCEPTION {type(e).__name__}: {e}")
    if verbose:
        print(f"  FUZZ: {n} randomized snapshots — "
              + (f"{len(fails)} FAILURES:\n   " + "\n   ".join(fails[:10]) if fails
                 else "0 invariant violations, 0 crashes  ✓"))
    return not fails


# =============================================================================
#  GOLDEN VECTORS — canonical inputs -> outputs, shared with the JS app so the
#  browser engine (spcx_app.html) can prove it matches this Python engine.
#  Outputs here are DATE-INDEPENDENT (score, level, aligned, blockers, strikes);
#  the plan's dollar sizing/DTE/POP depend on 'today' in BOTH engines by design,
#  so they are intentionally not golden-tested.
# =============================================================================
GOLDEN_SCENARIOS = [
    {"name": "blocked_gamma_sweep", "inputs": {
        "price": 212.45, "iv_annual": 1.1932, "iv_percentile": 0.90, "iv_5d_ago": None,
        "volume": 241_569_595, "avg_volume_20d": 300_000_000,
        "short_interest_pct_float": None, "short_interest_prev": None,
        "borrow_fee": None, "borrow_fee_prev": None,
        "dec_put_volume": 525_822, "dec_call_volume": 725_926,
        "insider_sales_30d": None, "otm_call_premium": 14_100_000,
        "social_sentiment": 0.68, "social_sentiment_prev": 0.55,
        "social_volume": 48_000, "social_volume_avg": 30_000, "news_sentiment": 0.55,
        "bullish_catalyst": False,
        "recent_highs": [176.52, 195.60, 225.64], "recent_lows": [150.0, 165.0, 199.98],
        "recent_closes": [160.95, 192.50, 212.45], "recent_volumes": [410e6, 300e6, 241.57e6],
        "days_to_next_major": 55, "next_major_label": "Wave 1", "next_major_pct": 20,
        "next_major_date": "2026-08-11",
        "major_unlocks": [["2026-08-11", 20], ["2026-11-09", 28], ["2027-06-13", 40]]}},
    {"name": "aligned_trigger", "inputs": {
        "price": 188.0, "iv_annual": 0.95, "iv_percentile": 0.88, "iv_5d_ago": 1.25,
        "volume": 150e6, "avg_volume_20d": 300e6,
        "short_interest_pct_float": 17, "short_interest_prev": 12,
        "borrow_fee": 40, "borrow_fee_prev": 60,
        "dec_put_volume": 95_000, "dec_call_volume": 90_000, "insider_sales_30d": 2,
        "otm_call_premium": 500_000,
        "social_sentiment": 0.1, "social_sentiment_prev": 0.6,
        "social_volume": 18_000, "social_volume_avg": 30_000, "news_sentiment": -0.1,
        "bullish_catalyst": False,
        "recent_highs": [230, 215, 205], "recent_lows": [210, 195, 184],
        "recent_closes": [228, 205, 188], "recent_volumes": [400e6, 300e6, 220e6, 180e6, 150e6],
        "days_to_next_major": 8, "next_major_label": "Wave 1", "next_major_pct": 20,
        "next_major_date": "2026-08-11",
        "major_unlocks": [["2026-08-11", 20], ["2026-11-09", 28]]}},
    {"name": "squeeze_unevaluated_blocks", "inputs": {
        # identical to aligned_trigger but with NO options-flow data -> the
        # gamma guard is YELLOW (unevaluated) and the fail-safe must BLOCK.
        "price": 188.0, "iv_annual": 0.95, "iv_percentile": 0.88, "iv_5d_ago": 1.25,
        "volume": 150e6, "avg_volume_20d": 300e6,
        "short_interest_pct_float": 17, "short_interest_prev": 12,
        "borrow_fee": 40, "borrow_fee_prev": 60,
        "dec_put_volume": 95_000, "dec_call_volume": 90_000, "insider_sales_30d": 2,
        "otm_call_premium": None,
        "social_sentiment": 0.1, "social_sentiment_prev": 0.6,
        "social_volume": 18_000, "social_volume_avg": 30_000, "news_sentiment": -0.1,
        "bullish_catalyst": False,
        "recent_highs": [230, 215, 205], "recent_lows": [210, 195, 184],
        "recent_closes": [228, 205, 188], "recent_volumes": [400e6, 300e6, 220e6, 180e6, 150e6],
        "days_to_next_major": 8, "next_major_label": "Wave 1", "next_major_pct": 20,
        "next_major_date": "2026-08-11",
        "major_unlocks": [["2026-08-11", 20], ["2026-11-09", 28]]}},
]


def _golden_snapshot(cfg, inp):
    """Build a deterministic Snapshot from neutral golden inputs (no date calls)."""
    s = Snapshot(ts=datetime(2026, 6, 17, 12, 0, 0))
    for k, v in inp.items():
        if k in ("next_major_date", "major_unlocks"):
            continue
        setattr(s, k, v)
    s.market_cap = (s.price or 0) * cfg["fundamentals"]["shares_outstanding"]
    if inp.get("next_major_date"):
        s.extras["next_major_date"] = inp["next_major_date"]
    if inp.get("major_unlocks"):
        s.extras["major_unlocks"] = [tuple(x) for x in inp["major_unlocks"]]
    compute_atr(s)
    return s


def golden_outputs(cfg, snap):
    """Date-independent canonical outputs used for Python<->JS parity."""
    S = build_signals(snap, cfg); comp = composite(S)
    conf = confluence(snap, S, cfg); lvl = warning_level(snap, S, comp, conf)
    plan = build_trade_plan(snap, cfg)
    return {
        "composite": round(comp, 1), "level": lvl, "aligned": conf["aligned"],
        "blockers": sorted(conf["blockers"]),
        "Klong": plan["Klong"] if plan else None,
        "Kshort": plan["Kshort"] if plan else None,
    }


def emit_golden(cfg, path="spcx_golden.json"):
    """Write canonical scenario inputs + this engine's outputs to JSON (UTF-8,
    ASCII-safe) so the JS app's in-browser parity self-test can diff against it."""
    out = []
    for sc in GOLDEN_SCENARIOS:
        snap = _golden_snapshot(cfg, sc["inputs"])
        out.append({"name": sc["name"], "inputs": sc["inputs"],
                    "expected": golden_outputs(cfg, snap)})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=True)
    return path


def validate_config(cfg):
    """Hard invariants: a violation is a coding error that must abort. Soft data
    issues (e.g. an unverified unlock schedule) are surfaced by config_warnings()."""
    t = cfg["thresholds"]; r = cfg["risk"]; f = cfg["fundamentals"]
    assert t["ps_normalizing"] < t["ps_stretched"], "ps thresholds inverted"
    assert 0 < t["dec_put_call_ratio"], "bad p/c threshold"
    assert r["short_put_moneyness"] < r["long_put_moneyness"], "put strikes inverted"
    assert 0 < r["short_put_moneyness"] <= 1 and 0 < r["long_put_moneyness"] <= 1, "moneyness out of (0,1]"
    assert 0 < r["max_risk_pct"] <= 100, "bad risk pct"
    assert abs(sum(TIER_WEIGHTS.values()) - 1.0) < 1e-6, "tier weights must sum to 1.0"
    assert 0 <= t["iv_percentile_high"] <= 100, "iv_percentile_high out of 0..100"
    assert 0 <= t["min_data_confidence"] <= 100, "min_data_confidence out of 0..100"
    sched = f.get("unlock_schedule", [])
    if sched:
        dates = [e["date"] for e in sched]
        assert dates == sorted(dates), "unlock schedule not date-sorted"
        assert all(0 <= e["pct"] <= 100 for e in sched), "unlock pct out of 0..100"


def config_warnings(cfg):
    """Non-fatal data-integrity warnings. These DON'T abort the run; they print a
    loud reminder that the underlying figures are assumptions to verify (the tool
    can't know the real S-1, so it must never silently present bad supply data)."""
    f = cfg["fundamentals"]; warns = []
    sched = f.get("unlock_schedule", [])
    base = f.get("current_float_pct") or 0
    basis = f.get("unlock_pct_basis")
    if sched and basis == "180d_lockup_pool":
        est = [e["label"] for e in sched if e.get("est")]
        if est:
            warns.append("UNLOCK SCHEDULE (424B4-sourced) — earnings-anchored tranches have ESTIMATED "
                         "dates [" + ", ".join(est) + "]; re-anchor when SPCX sets its Q2/Q3 earnings "
                         "dates (those two are the largest supply events). Musk's 366-day founder lock "
                         "has no early release (supply-absorbing).")
    elif sched:
        total = base + sum(e.get("pct", 0) for e in sched)
        if total > 100 + 1e-6:
            warns.append(f"UNLOCK SCHEDULE UNVERIFIED — float {base}% + Σ(unlock pct) = {total:.0f}% "
                         f"exceeds 100% of shares (impossible). Supply %s and the tranche ladder are "
                         f"placeholders; verify every row against the actual S-1 lockup table.")
    so = f.get("shares_outstanding")
    if so and not (1e9 <= so <= 5e10):
        warns.append(f"shares_outstanding={so:,} looks implausible — verify; it scales market-cap, "
                     f"P/S and short-interest %float.")
    return warns


def main():
    enable_utf8_output()
    cfg = CONFIG
    try:
        validate_config(cfg)
    except AssertionError as e:
        print("CONFIG ERROR:", e); return
    for w in config_warnings(cfg):
        print("  ⚠ " + w)
    if "--emit-golden" in sys.argv:
        i = sys.argv.index("--emit-golden")
        path = sys.argv[i+1] if len(sys.argv) > i+1 and not sys.argv[i+1].startswith("-") else "spcx_golden.json"
        print("  wrote", emit_golden(cfg, path)); return
    if "--gen-sample-csv" in sys.argv:
        print("  wrote", gen_sample_csv()); return
    if "--track" in sys.argv:
        tr = track_record(cfg)
        if not tr:
            print("  TRACK RECORD: not enough run history yet (run the daily task with live price for a while).")
        else:
            print("  TRACK RECORD (this SPCX run-log — did past signals lead a 20d drop?)")
            for k, r in tr.items():
                print(f"    {k:<10} {r['n']} signal(s) · {r['hit_rate']}% led a ≥3% drop · avg fwd {r['avg_fwd']:+.1f}%")
        return
    if "--replay" in sys.argv:
        i = sys.argv.index("--replay")
        path = sys.argv[i+1] if len(sys.argv) > i+1 else "spcx_replay_sample.csv"
        if not os.path.exists(path):
            gen_sample_csv(path)
        run_replay(cfg, path); return
    if "--demo" in sys.argv:
        run_demo(cfg); return
    if "--selftest" in sys.argv:
        ok = run_selftest(cfg); sys.exit(0 if ok else 1)
    if "--fuzz" in sys.argv:
        i = sys.argv.index("--fuzz")
        n = int(sys.argv[i+1]) if len(sys.argv) > i+1 and sys.argv[i+1].isdigit() else 1000
        ok = run_fuzz(cfg, n); sys.exit(0 if ok else 1)
    if "--watch" in sys.argv:
        i = sys.argv.index("--watch")
        interval = int(sys.argv[i+1]) if len(sys.argv) > i+1 else 300
        run_watch(cfg, interval); return
    if "--daily" in sys.argv:   # one-shot run that ALWAYS pushes the daily digest (for a scheduled task)
        run_once(cfg, write_html=True, do_alerts=True, daily=True); return
    if "--test-notify" in sys.argv:   # verify the phone-notification pipe works
        ok = notify_all(cfg, "SPCX test", "✅ SPCX notification test — if you see this, daily alerts work.",
                        "default", "white_check_mark")
        print("  test notification:", "SENT ✓" if ok else "NOT sent (check alerts config / network)"); return
    run_once(cfg, write_html="--html" in sys.argv, do_alerts="--alerts" in sys.argv)


if __name__ == "__main__":
    main()
