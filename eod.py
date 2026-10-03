"""End-of-day job: flatten 5 minutes before the close, log the day, send one short summary."""
import csv
import os
import time as _time
from datetime import datetime, time, timedelta

from ifvg import NY, SYMBOL, client_id, clients, fetch_qqq, notify, now_ny, session

LOG = "trades.csv"
FIELDS = ["date", "outcome", "side", "qty", "entry", "exit", "stop", "r", "pnl", "mfe_r",
          "minutes", "equity"]


def loss_reason(side, outcome, mfe_r, minutes):
    move = "up" if side == "short" else "down"
    if outcome == "eod":
        return "Never hit the target or the stop. Closed at the end of the day."
    if mfe_r < 0.25:
        return (f"The sweep didn't reverse. Price kept going {move} and hit the stop "
                f"{minutes} min after entry without ever being in profit.")
    return (f"Was up {mfe_r:.1f}R, then reversed and hit the stop after {minutes} min. "
            f"The move ran out before the 2R target.")


def main():
    from alpaca.common.exceptions import APIError
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    tc, dc = clients()
    now = now_ny()
    today = now.date()
    s = session(tc, today)
    if not s:
        print("Market closed today.")
        return
    flat_at = s[1] - timedelta(minutes=5)
    if not (flat_at - timedelta(minutes=30) <= now <= flat_at + timedelta(minutes=4)):
        print("Not this run's close window, exiting.")
        return
    if now < flat_at:
        _time.sleep((flat_at - now).total_seconds())

    tc.close_all_positions(cancel_orders=True)
    _time.sleep(15)

    row = {"date": today.isoformat(), "outcome": "no_trade", "pnl": 0}
    try:
        entry = tc.get_order_by_client_id(client_id(today))
    except APIError:
        entry = None

    if entry is not None and entry.filled_avg_price:
        side = 1 if entry.side.value == "buy" else -1
        e_px, qty = float(entry.filled_avg_price), int(float(entry.filled_qty))
        orders = tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=[SYMBOL], nested=True, limit=100,
            after=datetime.combine(today, time(9, 0), NY)))
        stop_px, exit_px, exit_t, outcome = None, None, None, "eod"
        for o in orders:
            if o.id == entry.id:
                for leg in o.legs or []:
                    if leg.stop_price:
                        stop_px = float(leg.stop_price)
                    if leg.filled_avg_price:
                        exit_px, exit_t = float(leg.filled_avg_price), leg.filled_at
                        outcome = "stop" if leg.stop_price else "target"
        if exit_px is None:
            for o in orders:
                if o.id != entry.id and o.filled_avg_price and o.side != entry.side:
                    exit_px, exit_t = float(o.filled_avg_price), o.filled_at
        if exit_px is not None:
            risk = abs(e_px - stop_px) if stop_px else None
            entry_t = entry.filled_at.astimezone(NY)
            exit_t = (exit_t or now_ny()).astimezone(NY)
            minutes = max(1, round((exit_t - entry_t).total_seconds() / 60))
            mfe_r = 0.0
            try:
                bars = fetch_qqq(dc, entry_t, exit_t)
                if bars and risk:
                    best = max(b.h for b in bars) if side == 1 else min(b.l for b in bars)
                    mfe_r = max(0.0, (best - e_px) * side / risk)
            except Exception as e:
                print("MFE data unavailable:", e)
            row.update(outcome=outcome, side="long" if side == 1 else "short", qty=qty,
                       entry=e_px, exit=exit_px, stop=stop_px, minutes=minutes,
                       pnl=round((exit_px - e_px) * qty * side, 2), mfe_r=round(mfe_r, 2),
                       r=round((exit_px - e_px) * side / risk, 2) if risk else "")
    row["equity"] = round(float(tc.get_account().equity), 2)

    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)

    # ── One glanceable notification ──
    day = f"{today:%a %-d %b}"
    pnl = row["pnl"]
    r_txt = f"{row['r']:+}R" if isinstance(row.get("r"), float) else "R n/a"
    if row["outcome"] == "no_trade":
        notify(f"{day}: no setup today.", title="$0 · No trade")
    elif pnl >= 0:
        how = {"target": "target hit", "stop": "stopped out", "eod": "closed at end of day"}
        notify(f"{day}: {row['side']} QQQ, {how[row['outcome']]} ({r_txt}).",
               title=f"+${pnl:,.0f} ✅")
    else:
        why = loss_reason(row["side"], row["outcome"], row["mfe_r"], row["minutes"])
        notify(f"{day}: {row['side']} QQQ ({r_txt}).\nWhy: {why}",
               title=f"−${abs(pnl):,.0f} ❌")


if __name__ == "__main__":
    main()
