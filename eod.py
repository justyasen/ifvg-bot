"""End-of-day job: flatten 5 minutes before the close and log the day to trades.csv."""
import csv
import os
import time as _time
from datetime import datetime, time, timedelta

from ifvg import NY, SYMBOL, client_id, clients, notify, now_ny, session

LOG = "trades.csv"
FIELDS = ["date", "outcome", "side", "qty", "entry", "exit", "stop", "r", "pnl", "equity"]


def main():
    from alpaca.common.exceptions import APIError
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    tc, _ = clients()
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

    row = {"date": today.isoformat(), "outcome": "no_trade"}
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
        stop_px, exit_px, outcome = None, None, "eod"
        for o in orders:
            if o.id == entry.id:
                for leg in o.legs or []:
                    if leg.stop_price:
                        stop_px = float(leg.stop_price)
                    if leg.filled_avg_price:
                        exit_px = float(leg.filled_avg_price)
                        outcome = "stop" if leg.stop_price else "target"
        if exit_px is None:
            for o in orders:
                if o.id != entry.id and o.filled_avg_price and o.side != entry.side:
                    exit_px = float(o.filled_avg_price)
        if exit_px is not None:
            risk = abs(e_px - stop_px) if stop_px else None
            pnl = (exit_px - e_px) * qty * side
            row.update(outcome=outcome, side="long" if side == 1 else "short", qty=qty,
                       entry=e_px, exit=exit_px, stop=stop_px, pnl=round(pnl, 2),
                       r=round((exit_px - e_px) * side / risk, 2) if risk else "")
    row["equity"] = round(float(tc.get_account().equity), 2)

    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)

    if row["outcome"] == "no_trade":
        notify(f"{today}: no trade. Equity ${row['equity']:,.2f}", title="IFVG daily")
    else:
        notify(f"{today}: {row['side']} closed by {row['outcome']}, {row['r']}R, "
               f"P&L ${row['pnl']:,.2f}. Equity ${row['equity']:,.2f}", title="IFVG daily")


if __name__ == "__main__":
    main()
