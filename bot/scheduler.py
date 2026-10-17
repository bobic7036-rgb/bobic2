"""Фонові задачі: генерація зделок, моніторинг TP/SL, денний PnL-звіт."""
import asyncio
import time
from aiogram import Bot
from bot import db, analyzer
from bot.formatting import signal_text, result_text, fmt_price

last_gen = {"ts": 0}


async def send_signals_round(bot: Bot):
    """Генерує новий раунд зделок і розсилає підписникам."""
    count = await db.get_int("signals_count")
    min_score = await db.get_int("min_score")
    try:
        signals = await analyzer.generate_signals(count=count, min_score=min_score)
    except Exception as e:
        print("generate_signals error:", e)
        return
    if not signals:
        print("no signals this round")
        return

    # БАЗОВІ підписники — отримують самі сигнали
    base_users = await db.active_subscribers("base")
    for u in base_users:
        for s in signals:
            try:
                await bot.send_message(u["user_id"], signal_text(s), parse_mode="HTML")
                await asyncio.sleep(0.1)
            except Exception:
                pass
        # створюємо трейди для базових (без плеча, маржа умовна = 100 USDT депозит за замовчуванням)
        for s in signals:
            await db.add_trade({
                "user_id": u["user_id"], "symbol": s["symbol"], "side": s["side"],
                "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                "score": s["score"], "reason": s["reason"],
                "leverage": 1, "margin": 0,
            })

    # ПРО підписники — отримують сигнали + плече під їх депозит
    pro_users = await db.active_subscribers("pro")
    for u in pro_users:
        row = await db.get_user(u["user_id"])
        deposit = row["deposit"] or 0
        for s in signals:
            if deposit > 0:
                lev, margin = analyzer.pick_leverage(deposit, s["score"])
                await bot.send_message(u["user_id"], signal_text(s, lev, margin), parse_mode="HTML")
                await db.add_trade({
                    "user_id": u["user_id"], "symbol": s["symbol"], "side": s["side"],
                    "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                    "score": s["score"], "reason": s["reason"],
                    "leverage": lev, "margin": margin,
                })
            else:
                await bot.send_message(u["user_id"], signal_text(s), parse_mode="HTML")
                await db.add_trade({
                    "user_id": u["user_id"], "symbol": s["symbol"], "side": s["side"],
                    "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                    "score": s["score"], "reason": s["reason"],
                    "leverage": 1, "margin": 0,
                })
            await asyncio.sleep(0.1)


async def monitor_trades(bot: Bot):
    """Перевіряє відкриті зделки проти поточної ціни Binance."""
    trades = await db.open_trades()
    if not trades:
        return
    symbols = {t["symbol"] for t in trades}
    prices = {}
    for sym in symbols:
        try:
            d = await analyzer.fetch_json(
                f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}")
            prices[sym] = float(d["price"])
        except Exception:
            continue

    closed = []  # (trade_row_after_update, status, price)
    for t in trades:
        price = prices.get(t["symbol"])
        if not price:
            continue
        hit = None
        if t["side"] == "LONG":
            if price >= t["tp2"]:
                hit = ("TP2", t["tp2"])
            elif price >= t["tp1"] and t["status"] == "OPEN":
                hit = ("TP1", t["tp1"])
            elif price <= t["sl"]:
                hit = ("SL", t["sl"])
        else:
            if price <= t["tp2"]:
                hit = ("TP2", t["tp2"])
            elif price <= t["tp1"] and t["status"] == "OPEN":
                hit = ("TP1", t["tp1"])
            elif price >= t["sl"]:
                hit = ("SL", t["sl"])
        if hit:
            await db.close_trade(t["id"], hit[0], hit[1])
            cur = await db._db.execute("SELECT * FROM trades WHERE id=?", (t["id"],))
            row = await cur.fetchone()
            closed.append((row, hit[0], hit[1]))

    for row, status, price in closed:
        if not row:
            continue
        try:
            await bot.send_message(row["user_id"], result_text(row, status, price), parse_mode="HTML")
        except Exception:
            pass


async def daily_report(bot: Bot):
    """Ввівечері надсилає PRO-користувачам підсумок дня."""
    users = await db.active_subscribers("pro")
    for u in users:
        row = await db.get_user(u["user_id"])
        dep = row["deposit"] or 0
        day = await db.day_pnl(u["user_id"])
        tot = await db.total_pnl(u["user_id"])
        pnl = day["s"] or 0
        pct = (pnl / dep * 100) if dep else 0
        text = (
            f"🌙 <b>Підсумок дня</b>\n\n"
            f"Депозит: <b>{fmt_price(dep)} USDT</b>\n"
            f"PnL за 24 год: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USDT</b> ({'+' if pct >= 0 else ''}{pct:.2f}%)\n"
            f"Зделок закрито за добу: <b>{day['c']}</b>\n"
            f"Загальний PnL: <b>{'+' if (tot['s'] or 0) >= 0 else ''}{(tot['s'] or 0):.2f} USDT</b>"
        )
        try:
            await bot.send_message(u["user_id"], text, parse_mode="HTML")
        except Exception:
            pass


async def scheduler_loop(bot: Bot):
    """Головний цикл фонових задач."""
    last_report_day = None
    while True:
        now = time.time()
        interval = await db.get_int("signals_interval_min") * 60
        if now - last_gen["ts"] >= interval:
            last_gen["ts"] = now
            asyncio.create_task(send_signals_round(bot))
        await asyncio.sleep(60)   # моніторинг щохвилини
        try:
            await monitor_trades(bot)
        except Exception as e:
            print("monitor error:", e)
        # звіт о 21:00
        lt = time.localtime(now)
        if lt.tm_hour == 21 and lt.tm_min < 5 and last_report_day != lt.tm_yday:
            last_report_day = lt.tm_yday
            asyncio.create_task(daily_report(bot))
