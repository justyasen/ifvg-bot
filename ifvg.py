"""Shared pieces for the NQ-levels / QQQ-execution sweep + inverse FVG bot.

Levels (Asia 20:00-23:59, London 02:00-05:00, New York time) come from NQ futures
(Yahoo Finance, free). The live trigger runs on QQQ 1-minute bars from Alpaca's free
IEX feed, and orders go to an Alpaca paper account.
"""
import os
from collections import namedtuple
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
SYMBOL = "QQQ"
RR = float(os.environ.get("RR", "2.0"))              # target in R
RISK_PCT = float(os.environ.get("RISK_PCT", "0.005"))  # 0.5% of equity risked per trade
STOP_BUFFER = 0.02                                   # dollars beyond the sweep extreme
WIN_START, WIN_END = time(9, 30), time(11, 0)

Bar = namedtuple("Bar", "t o h l c")


# ── Clients / helpers ────────────────────────────────────
def clients():
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    key, secret = os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
    return TradingClient(key, secret, paper=True), StockHistoricalDataClient(key, secret)


def now_ny():
    return datetime.now(NY)


def notify(msg, title="IFVG bot"):
    print(f"[{now_ny():%H:%M:%S}] {title}: {msg}", flush=True)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post("https://ntfy.sh/", timeout=10,
                          json={"topic": topic, "title": title, "message": msg})
        except Exception as e:
            print("ntfy failed:", e)


def session(tc, d):
    """Return (open, close) as NY datetimes if d is a trading day, else None."""
    from alpaca.trading.requests import GetCalendarRequest
    cal = tc.get_calendar(GetCalendarRequest(start=d, end=d))
    if not cal or cal[0].date != d:
        return None
    o, c = cal[0].open, cal[0].close
    o = o.replace(tzinfo=NY) if o.tzinfo is None else o.astimezone(NY)
    c = c.replace(tzinfo=NY) if c.tzinfo is None else c.astimezone(NY)
    return o, c


def client_id(d):
    return f"ifvg-{d:%Y%m%d}"


def fetch_qqq(dc, start, end):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Minute,
                           start=start, end=end, feed=DataFeed.IEX)
    raw = dc.get_stock_bars(req).data.get(SYMBOL, [])
    return [Bar(b.timestamp.astimezone(NY), b.open, b.high, b.low, b.close) for b in raw]


# ── Levels ───────────────────────────────────────────────
def build_levels(dc, d):
    """Asia/London highs and lows from NQ, converted to QQQ prices.
    A level is None if price already traded through it before 09:30."""
    import yfinance as yf
    nq = yf.Ticker("NQ=F").history(period="5d", interval="1m", prepost=True)
    if nq.empty:
        raise RuntimeError("no NQ data from Yahoo")
    nq.index = nq.index.tz_convert(NY)

    d0 = datetime.combine(d, time(0), NY)
    cut = d0 + timedelta(hours=9, minutes=30)
    asia = nq[(nq.index >= d0 - timedelta(hours=4)) & (nq.index < d0)]
    lon = nq[(nq.index >= d0 + timedelta(hours=2)) & (nq.index < d0 + timedelta(hours=5))]
    after_asia = nq[(nq.index >= d0) & (nq.index < cut)]
    after_lon = nq[(nq.index >= d0 + timedelta(hours=5)) & (nq.index < cut)]
    if asia.empty or lon.empty or after_lon.empty:
        raise RuntimeError("missing Asia/London NQ data")

    # NQ -> QQQ conversion from the latest minute both have before 09:30
    q = fetch_qqq(dc, d0 + timedelta(hours=4), cut)
    qmap = {b.t: b for b in q}
    common = [t for t in after_lon.index if t in qmap]
    if not common:
        raise RuntimeError("no overlapping NQ/QQQ premarket minute")
    t_ref = max(common)
    ratio = float(after_lon.loc[t_ref, "Close"]) / qmap[t_ref].c
    q_after = [b for b in q if b.t > t_ref]
    q_hi = max((b.h for b in q_after), default=float("-inf"))
    q_lo = min((b.l for b in q_after), default=float("inf"))

    def lvl(level, after, is_high):
        qlevel = level / ratio
        if is_high:
            ok = after["High"].max() < level and q_hi < qlevel
        else:
            ok = after["Low"].min() > level and q_lo > qlevel
        return round(qlevel, 2) if ok else None

    raw = {"asia_hi": float(asia["High"].max()), "asia_lo": float(asia["Low"].min()),
           "lon_hi": float(lon["High"].max()), "lon_lo": float(lon["Low"].min())}
    return {
        "asia_hi": lvl(raw["asia_hi"], after_asia, True),
        "asia_lo": lvl(raw["asia_lo"], after_asia, False),
        "lon_hi": lvl(raw["lon_hi"], after_lon, True),
        "lon_lo": lvl(raw["lon_lo"], after_lon, False),
        "nq": raw, "ratio": round(ratio, 4),
    }


def levels_text(lv):
    def f(k):
        return f"{lv['nq'][k]:.2f}" + ("" if lv[k] is not None else " (taken)")
    return (f"NQ levels  Asia H {f('asia_hi')} / L {f('asia_lo')}  "
            f"London H {f('lon_hi')} / L {f('lon_lo')}")


# ── Rule engine (same logic as the Pine Script) ──────────
def evaluate(bars, lv):
    """bars: completed 1-min QQQ bars, oldest first, starting a few minutes before 09:30.
    Returns {'status': 'waiting'|'signal'|'no_trade', ...}."""
    highs = [lv[k] for k in ("asia_hi", "lon_hi") if lv[k] is not None]
    lows = [lv[k] for k in ("asia_lo", "lon_lo") if lv[k] is not None]
    direction, ext, bull_lo, bear_hi = 0, None, None, None

    for i, b in enumerate(bars):
        if not (WIN_START <= b.t.time() < WIN_END):
            continue
        if direction == 0:
            hi = any(b.h > x for x in highs)
            lo = any(b.l < x for x in lows)
            if hi and lo:
                return {"status": "no_trade", "reason": "both sides swept in one bar", "t": b.t}
            if hi:
                direction, ext = -1, b.h
            elif lo:
                direction, ext = 1, b.l
        else:
            ext = max(ext, b.h) if direction == -1 else min(ext, b.l)

        if bull_lo is not None and b.c < bull_lo:
            if direction == -1:
                stop = ext + STOP_BUFFER
                return {"status": "signal", "side": "short", "t": b.t,
                        "entry": b.c, "stop": stop, "risk": stop - b.c}
            bull_lo = None
        if bear_hi is not None and b.c > bear_hi:
            if direction == 1:
                stop = ext - STOP_BUFFER
                return {"status": "signal", "side": "long", "t": b.t,
                        "entry": b.c, "stop": stop, "risk": b.c - stop}
            bear_hi = None

        if i >= 2:
            if b.l > bars[i - 2].h:
                bull_lo = bars[i - 2].h
            if b.h < bars[i - 2].l:
                bear_hi = bars[i - 2].l

    return {"status": "waiting", "direction": direction}
