"""Morning job: build levels, watch 09:30-11:00 NY, place one bracket order on QQQ.

python bot.py           live run (used by GitHub Actions)
python bot.py --check   no orders: replays the most recent session and reports what it would have done
"""
import sys
import time as _time
from datetime import datetime, time, timedelta

from ifvg import (NY, RR, RISK_PCT, SYMBOL, WIN_END, build_levels, client_id, clients,
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
    if not session(tc, today):
        print("Market closed today.")
        return
    if not (time(9, 0) <= now.time() <= time(10, 0)):
        print("Outside start window (other DST cron), exiting.")
        return
    cid = client_id(today)
    if order_exists(tc, cid):
        print("Already traded today.")
        return

    try:
        lv = build_levels(dc, today)
    except Exception as e:
        notify(f"No trading today: couldn't load market data ({e})", title="⚠️ Bot needs attention")
        return
    print(levels_text(lv))
    if not any(lv[k] for k in ("asia_hi", "asia_lo", "lon_hi", "lon_lo")):
        print("All levels already taken before the open, no trade today.")
        return

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
