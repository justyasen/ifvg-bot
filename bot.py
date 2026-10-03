"""Morning job: build levels, watch 09:30-11:00 NY, place one bracket order on QQQ.

python bot.py           live run (used by GitHub Actions)
python bot.py --check   no orders: replays the most recent session and reports what it would have done
"""
import sys
import time as _time
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SOFIA = ZoneInfo("Europe/Sofia")

from ifvg import (NY, RR, RISK_PCT, SYMBOL, WIN_END, WIN_START, build_levels, client_id, clients,
                  evaluate, fetch_qqq, levels_text, notify, now_ny, session)


def order_exists(tc, cid):
    from alpaca.common.exceptions import APIError
    try:
        tc.get_order_by_client_id(cid)
        return True
    except APIError:
        return False


def place(tc, sig, cid):
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    equity = float(tc.get_account().equity)
    qty = min(int(equity * RISK_PCT / sig["risk"]), int(equity * 2 / sig["entry"]))
    if qty < 1:
        print(f"Signal {sig['side']} but size rounds to 0, skipped.")
        return
    long = sig["side"] == "long"
    target = sig["entry"] + (RR * sig["risk"] if long else -RR * sig["risk"])
    tc.submit_order(MarketOrderRequest(
        symbol=SYMBOL, qty=qty, side=OrderSide.BUY if long else OrderSide.SELL,
        time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(target, 2)),
        stop_loss=StopLossRequest(stop_price=round(sig["stop"], 2)),
        client_order_id=cid))
    print(f"{sig['side'].upper()} {qty} {SYMBOL} @ ~{sig['entry']:.2f}  "
          f"stop {sig['stop']:.2f}  target {target:.2f}")


def check_mode(tc, dc):
    """Replay the latest completed session with no orders."""
    now = now_ny()
    d = now.date()
    for _ in range(10):
        s = session(tc, d)
        if s and s[0] + timedelta(minutes=95) <= now:
            break
        d -= timedelta(days=1)
    lv = build_levels(dc, d)
    d0 = datetime.combine(d, time(9, 25), NY)
    bars = fetch_qqq(dc, d0, d0 + timedelta(minutes=95))
    res = evaluate(bars, lv)
    print(levels_text(lv), res)
    if res["status"] == "signal":
        out = f"would have gone {res['side']} at {res['t']:%H:%M}"
    else:
        out = "no trade"
    notify(f"{d:%a %-d %b}: {out}", title="Test OK ✅")


def main():
    tc, dc = clients()
    if "--check" in sys.argv:
        return check_mode(tc, dc)

    now = now_ny()
    today = now.date()
    if not (time(9, 0) <= now.time() <= time(10, 0)):
        print("Outside start window (other DST cron), exiting.")
        return
    day = f"{today:%a %-d %b}"
    if not session(tc, today):
        notify(f"{day}: US market holiday.", title="⚪ No trading today")
        return
    cid = client_id(today)
    if order_exists(tc, cid):
        print("Already traded today.")
        return

    # ── Pre-flight: can it trade today? ──
    acct = tc.get_account()
    if acct.trading_blocked or acct.account_blocked or str(acct.status.value) != "ACTIVE":
        notify(f"{day}: Alpaca account can't trade (status {acct.status.value}).",
               title="⚠️ Bot needs attention")
        return
    try:
        lv = build_levels(dc, today)
    except Exception as e:
        notify(f"{day}: couldn't load market data, so no trading today ({e}).",
               title="⚠️ Bot needs attention")
        return
    print(levels_text(lv))
    names = {"asia_hi": "Asia high", "asia_lo": "Asia low",
             "lon_hi": "London high", "lon_lo": "London low"}
    live = [names[k] for k in names if lv[k] is not None]
    if not live:
        notify(f"{day}: all overnight levels were already taken before the open.",
               title="⚪ No trade today")
        return
    w0 = datetime.combine(today, WIN_START, NY).astimezone(SOFIA)
    w1 = datetime.combine(today, WIN_END, NY).astimezone(SOFIA)
    notify(f"{day}: watching {', '.join(live)} from {w0:%H:%M} to {w1:%H:%M}.",
           title=f"🟢 Ready to trade · {len(live)} level{'s' if len(live) > 1 else ''}")

    start = datetime.combine(today, time(9, 31, 8), NY)
    if now_ny() < start:
        _time.sleep((start - now_ny()).total_seconds())

    while True:
        now = now_ny()
        if now.time() >= time(WIN_END.hour, WIN_END.minute, 30):
            print("No setup by 11:00, no trade today.")
            return
        try:
            bars = fetch_qqq(dc, datetime.combine(today, time(9, 25), NY), now)
            bars = [b for b in bars if b.t + timedelta(minutes=1) <= now]
            res = evaluate(bars, lv)
        except Exception as e:
            print("Data error, retrying:", e)
            res = {"status": "waiting"}

        if res["status"] == "no_trade":
            print(f"No trade today: {res['reason']}.")
            return
        if res["status"] == "signal":
            age = now - (res["t"] + timedelta(minutes=1))
            if age > timedelta(minutes=3):
                notify(f"Missed today's signal at {res['t']:%H:%M} because GitHub started the bot late. No trade.", title="⚠️ Bot needs attention")
                return
            try:
                place(tc, res, cid)
            except Exception as e:
                notify(f"Order was rejected by Alpaca: {e}", title="⚠️ Bot needs attention")
            return

        nxt = (now + timedelta(minutes=1)).replace(second=8, microsecond=0)
        _time.sleep(max(1, (nxt - now_ny()).total_seconds()))


if __name__ == "__main__":
    main()
