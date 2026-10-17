"""Підписки та оплата Telegram Stars (зірками).

Для адміністратора (ADMIN_ID) ВСЕ безкоштовно: підписка видається
автоматично без оплати. Для всіх інших — реальна оплата зірками через
send_invoice (валюта XTR) + pre_checkout_query + successful_payment.
"""
import time
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, SuccessfulPayment,
)
from aiogram.filters import Command, CommandStart
from bot import db
from bot.config import ADMIN_ID, STAR

router = Router()

PLAN_NAMES = {"base": "БАЗОВА", "pro": "ПРОФЕСІЙНА"}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def subscription_kb(user_id: int):
    if is_admin(user_id):
        btn = InlineKeyboardButton(
            text="👑 Адмін — доступ безкоштовний (активний)", callback_data="adm_free")
        return InlineKeyboardMarkup(inline_keyboard=[[btn]])
    base_stars = await db.get_int("stars_base")
    pro_stars = await db.get_int("stars_pro")
    base_days = await db.get_int("base_days")
    pro_days = await db.get_int("pro_days")
    rows = [
        [InlineKeyboardButton(text=f"📦 БАЗОВА — ⭐ {base_stars} / {base_days} дн.",
                              callback_data="buy:base")],
        [InlineKeyboardButton(text=f"💎 ПРОФЕСІЙНА — ⭐ {pro_stars} / {pro_days} дн.",
                              callback_data="buy:pro")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm_free")
async def cb_adm_free(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Тільки для адміна 😉", show_alert=True)
        return
    await cb.answer("👑 Тобі все безкоштовно!")


@router.message(Command("subscribe"))
@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    await db.ensure_user(uid, message.from_user.username or "")
    # 👑 адміну — авто-підписка назавжди при кожному /start
    if is_admin(uid):
        for p in ("base", "pro"):
            await db.set_subscription(uid, p, 36500)  # ~100 років
    u = await db.get_user(uid)
    now = time.time()

    def until(ts):
        if is_admin(uid):
            return "♾️ безкоштовно (адмін)"
        d = (ts - now) // 86400 if ts else -1
        return f"ще {int(d)} дн." if d >= 0 else "❌ немає"

    text = (
        "🤖 <b>Crypto Signals Bot</b>\n"
        "Готові зделки з Binance Futures: вхід, стоп, 2 тейки, шанс і пояснення.\n\n"
        f"📦 Базова підписка: {until(u['base_until'])}\n"
        f"💎 Професійна підписка: {until(u['pro_until'])}\n\n"
        + ("👑 Ти адмін — усі функції БЕЗКОШТОВНО та назавжди.\n"
           if is_admin(uid) else
           "Оплата зірками (⭐ Telegram Stars) — миттєво і безпечно.\n"
           "Обери підписку 👇 або напиши /help")
    )
    await message.answer(text, parse_mode="HTML", reply_markup=await subscription_kb(uid))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery):
    plan = cb.data.split(":")[1]
    if plan not in PLAN_NAMES:
        await cb.answer()
        return

    # 👑 АДМІН — все безкоштовно, активація без оплати
    if is_admin(cb.from_user.id):
        await db.set_subscription(cb.from_user.id, plan, 36500)
        name = PLAN_NAMES[plan]
        await cb.message.answer(
            f"👑 <b>{name}</b> підписка активована БЕЗКОШТОВНО (ти адмін). ♾️",
            parse_mode="HTML",
        )
        await cb.answer("Безкоштовно для адміна 👑")
        return

    stars = max(await db.get_int(f"stars_{plan}"), 1)   # кількість зірок
    days = await db.get_int(f"{plan}_days")
    amount = stars * STAR                                # у тонах (1 star = 10^6 tons)
    title = f"Підписка {PLAN_NAMES[plan]} ({days} днів)"
    payload = f"sub:{plan}:{cb.from_user.id}"

    try:
        await cb.bot.send_invoice(
            chat_id=cb.from_user.id,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
            title=title,
            description=f"Доступ до сигналів. Оплата ⭐ Telegram Stars.",
            payload=payload,
            provider_payload=payload,
        )
    except Exception as e:
        await cb.message.answer(f"⚠️ Не вдалося створити рахунок: {e}")
    await cb.answer()


# ------------------- ЕТАПИ ОПЛАТИ ЗІРКАМИ -------------------

@router.pre_checkout_query()
async def process_pre_checkout(pc):
    # Валідація: валюта XTR (зірки) та мінімальна сума (50 тон)
    ok = pc.currency == "XTR" and pc.total_amount >= 50
    if ok:
        await pc.answer(success=True)
    else:
        await pc.answer(success=False, error_message="⚠️ Невірні параметри оплати.")


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    pay: SuccessfulPayment = message.successful_payment
    try:
        _, plan, uid_s = pay.invoice_payload.split(":")
        uid = int(uid_s)
    except (ValueError, AttributeError):
        return
    if plan not in PLAN_NAMES:
        return
    days = await db.get_int(f"{plan}_days")
    await db.set_subscription(uid, plan, days)
    stars = max(pay.total_amount // STAR, 1)
    name = PLAN_NAMES[plan]
    await message.bot.send_message(
        uid,
        f"🎉 <b>Оплата зірками пройшла успішно!</b>\n"
        f"Активовано <b>{name}</b> підписку на {days} днів (⭐ {stars}).\n\n"
        f"/signals — отримати актуальні зделки просто зараз.",
        parse_mode="HTML",
    )
    # сповіщення адміну про продаж
    try:
        await message.bot.send_message(
            ADMIN_ID,
            f"💰 Продаж! @{message.from_user.username or ''} (id {message.from_user.id})\n"
            f"План: {name} — ⭐ {stars} за {days} днів",
        )
    except Exception:
        pass
