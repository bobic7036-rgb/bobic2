"""Юзерські команди: /help, /signals, /deposit, /status, /pnl + адмін-команди."""
import asyncio
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
from bot import db, analyzer
from bot.config import ADMIN_ID
from bot.formatting import signal_text, fmt_price
from bot.scheduler import send_signals_round

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def has_any(user_id: int) -> bool:
    if is_admin(user_id):        # 👑 адміну все безкоштовно
        return True
    u = await db.get_user(user_id)
    now = db.time.time()
    return (u["base_until"] or 0) > now or (u["pro_until"] or 0) > now


async def is_pro(user_id: int) -> bool:
    if is_admin(user_id):        # 👑 адміну все безкоштовно
        return True
    u = await db.get_user(user_id)
    return (u["pro_until"] or 0) > db.time.time()


HELP_TEXT = """📖 <b>Довідка</b>

Я отримую дані з <b>Binance Futures</b>, аналізую монети (RSI, EMA, обсяги, funding, моментум) і видаю готові зделки.

<b>Кожна зделка містить:</b>
• Напрямок — 🟢 LONG / 🔴 SHORT
• Точку входу
• Стоп-лос
• Два тейк-профіти
• % шансу та пояснення «чому так піде»

<b>Підписки (оплата зірками ⭐ Telegram Stars):</b>
📦 <b>БАЗОВА</b> — сигнали за розкладом (кожні N хв), бот сам повідомляє, чи спрацювала зделка: дійшла до ✅ ТЕЙКу або 🛑 СТОПУ.
💎 <b>ПРОФЕСІЙНА</b> — все з базової + напиши команду /deposit (свій депозит у USDT), і бот підбере плече та розмір позиції під кожну зделку, а в кінці дня надішле звіт: скільки заробив від депозиту та PnL.
Оплата — натисни кнопку в /start, Telegram сам спише зірки, підписка активується миттєво.

<b>Команди:</b>
/start — головне меню та купівля підписки
/subscribe — обрати підписку
/signals — отримати актуальні зделки зараз
/deposit 500 — вказати депозит (для PRO, щоб підбирати плече)
/status — термін моїх підписок
/pnl — мій PnL за 24 год і загальний
/mytrades — останні зделки та їх результат
/help — ця довідка

⚠️ <i>Трейдинг — ризикований. Сигнали не є фінансовою порадою.</i>"""


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message):
    u = await db.get_user(message.from_user.id)
    now = db.time.time()

    def until(ts):
        if message.from_user.id == ADMIN_ID:
            return "♾️ безкоштовно (адмін)"
        d = ts and (ts - now) // 86400
        return f"✅ діє ще {d} дн." if d and d >= 1 else "❌ немає"

    txt = (
        f"📊 <b>Твій статус</b>\n\n"
        f"📦 Базова: {until(u['base_until'])}\n"
        f"💎 Професійна: {until(u['pro_until'])}\n"
        f"💰 Депозит: {fmt_price(u['deposit'] or 0)} USDT"
    )
    await message.answer(txt, parse_mode="HTML")


@router.message(Command("deposit"))
async def cmd_deposit(message: Message, command: CommandObject):
    if not await is_pro(message.from_user.id):
        await message.answer("💎 Ця функція доступна лише з ПРОФЕСІЙНОЮ підпискою.\n/subscribe — оформити.")
        return
    try:
        dep = float((command.args or "").replace(",", "."))
        assert dep > 0
    except (ValueError, AssertionError):
        await message.answer("Формат: <code>/deposit 500</code> (у USDT).", parse_mode="HTML")
        return
    await db.set_deposit(message.from_user.id, dep)
    await message.answer(
        f"✅ Депозит {fmt_price(dep)} USDT збережено.\n"
        f"Тепер у кожній новій зделці бот підбере плече та маржу автоматично.\n"
        f"Щоб отримати зделки просто зараз — /signals"
    )


@router.message(Command("signals"))
async def cmd_signals(message: Message):
    if not await has_any(message.from_user.id):
        await message.answer("🔒 Сигнали доступні тільки по підписці.\n/subscribe — обрати план.")
        return
    count = await db.get_int("signals_count")
    min_score = await db.get_int("min_score")
    await message.answer("🔎 Аналізую Binance Futures, зачекай...")
    try:
        signals = await analyzer.generate_signals(count=count, min_score=min_score)
    except Exception as e:
        await message.answer(f"⚠️ Не вдалось отримати дані з Binance: {e}")
        return
    if not signals:
        await message.answer("Зараз немає зделок з достатнім шансом (мінімум "
                             f"{min_score}%). Спробуй пізніше.")
        return
    u = await db.get_user(message.from_user.id)
    pro = await is_pro(message.from_user.id)
    deposit = u["deposit"] or 0
    for s in signals:
        if pro and deposit > 0:
            lev, margin = analyzer.pick_leverage(deposit, s["score"])
            await message.answer(signal_text(s, lev, margin), parse_mode="HTML")
            await db.add_trade({
                "user_id": message.from_user.id, "symbol": s["symbol"], "side": s["side"],
                "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                "score": s["score"], "reason": s["reason"], "leverage": lev, "margin": margin,
            })
        else:
            await message.answer(signal_text(s), parse_mode="HTML")
            await db.add_trade({
                "user_id": message.from_user.id, "symbol": s["symbol"], "side": s["side"],
                "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                "score": s["score"], "reason": s["reason"], "leverage": 1, "margin": 0,
            })
        await asyncio.sleep(0.1)


@router.message(Command("mytrades"))
async def cmd_mytrades(message: Message):
    trades = await db.recent_trades(message.from_user.id, 10)
    if not trades:
        await message.answer("У тебе ще немає зделок. /signals — отримати перші.")
        return
    lines = ["📜 <b>Останні зделки:</b>", ""]
    label = {"OPEN": "⏳ відкрита", "TP1": "✅ TP1 взято", "TP2": "🏆 TP2", "SL": "🛑 стоп"}
    for t in trades:
        coin = t["symbol"].replace("USDT", "")
        pnl = t["pnl"] or 0
        pnl_s = f" ({'+' if pnl >= 0 else ''}{pnl:.2f}$)" if pnl else ""
        lines.append(
            f"• {coin} {t['side']} @ {fmt_price(t['entry'])} — {label.get(t['status'], t['status'])}{pnl_s}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("pnl"))
async def cmd_pnl(message: Message):
    u = await db.get_user(message.from_user.id)
    dep = u["deposit"] or 0
    day = await db.day_pnl(message.from_user.id)
    tot = await db.total_pnl(message.from_user.id)
    pnl = day["s"] or 0
    pct = (pnl / dep * 100) if dep else 0
    txt = (
        f"💹 <b>PnL</b>\n\n"
        f"За 24 год: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USDT</b>"
        + (f" ({'+' if pct >= 0 else ''}{pct:.2f}% від депозиту)" if dep else "") + "\n"
        f"Всього: <b>{'+' if (tot['s'] or 0) >= 0 else ''}{(tot['s'] or 0):.2f} USDT</b>"
    )
    await message.answer(txt, parse_mode="HTML")


# ========================= АДМІН =========================

SETTINGS_MAP = {
    "interval": ("signals_interval_min", "інтервал зделок (хв)"),
    "count": ("signals_count", "кількість зделок за прохід"),
    "minscore": ("min_score", "мінімальний шанс (%)"),
    "starsbase": ("stars_base", "ціна БАЗОВОЇ (зірок ⭐)"),
    "basedays": ("base_days", "тривалість БАЗОВОЇ (днів)"),
    "starspro": ("stars_pro", "ціна ПРО (зірок ⭐)"),
    "prodays": ("pro_days", "тривалість ПРО (днів)"),
}


@router.message(Command("settings"))
async def cmd_settings(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    args = (command.args or "").split()
    if len(args) == 2 and args[0].lower() in SETTINGS_MAP:
        key, desc = SETTINGS_MAP[args[0].lower()]
        try:
            val = int(float(args[1]))
            assert val > 0
        except (ValueError, AssertionError):
            await message.answer("Значення має бути додатним числом.")
            return
        await db.set_setting(key, val)
        await message.answer(f"✅ {desc} = {val}")
        return
    cur = await db._db.execute("SELECT key, value FROM settings")
    rows = await cur.fetchall()
    lines = ["⚙️ <b>Налаштування бота</b> (редагуються без коду):\n"]
    for r in rows:
        lines.append(f"• <code>{r['key']}</code> = <b>{r['value']}</b>")
    lines.append("\n<b>Змінити:</b>\n<code>/settings interval 30</code> — кожні 30 хв давати зделки\n"
                 "<code>/settings count 5</code> — 5 зделок за прохід\n"
                 "<code>/settings minscore 70</code> — мінімальний шанс 70%\n"
                 "<code>/settings starsbase 3</code> / <code>/settings basedays 30</code> — ціна (⭐) і тривалість БАЗОВОЇ\n"
                 "<code>/settings starspro 7</code> / <code>/settings prodays 30</code> — ціна (⭐) і тривалість ПРО")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    now = db.time.time()
    cur = await db._db.execute("SELECT * FROM users ORDER BY user_id")
    rows = await cur.fetchall()
    lines = [f"👥 <b>Користувачі ({len(rows)}):</b>\n"]
    for r in rows[:50]:
        b = "📦" if (r["base_until"] or 0) > now else "▫️"
        p = "💎" if (r["pro_until"] or 0) > now else "▫️"
        lines.append(f"{b}{p} <code>{r['user_id']}</code> @{r['username'] or '-'} dep={r['deposit'] or 0}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    """Адмін може вручну видати/забрати підписку: /give <user_id> base|pro [днів]"""
    if message.from_user.id != ADMIN_ID:
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("<code>/give 123456 base 30</code> — видати на 30 днів\n"
                             "<code>/give 123456 pro off</code> — забрати", parse_mode="HTML")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await message.answer("user_id має бути числом.")
        return
    plan = args[1]
    if plan not in ("base", "pro"):
        return
    if len(args) > 2 and args[2] == "off":
        await db.revoke_subscription(uid, plan)
        await message.answer(f"❌ Підписку {plan} для {uid} забрано.")
        return
    days = int(args[2]) if len(args) > 2 else await db.get_int(f"{plan}_days")
    await db.set_subscription(uid, plan, days)
    await message.answer(f"✅ Видано {plan} на {days} днів для {uid}.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args:
        return
    subs = set()
    for plan in ("base", "pro"):
        for u in await db.active_subscribers(plan):
            subs.add(u["user_id"])
    n = 0
    for uid in subs:
        try:
            await message.bot.send_message(uid, command.args)
            n += 1
        except Exception:
            pass
    await message.answer(f"📣 Розіслано {n} активним підписникам.")


@router.message(Command("gennow"))
async def cmd_gennow(message: Message):
    """Примусово запустити раунд розсилки зделок зараз."""
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🚀 Запускаю раунд...")
    asyncio.create_task(send_signals_round(message.bot))
