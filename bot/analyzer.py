"""Аналіз монет на Binance Futures + генерація зделок.

Використовуємо публічні REST-ендпоінти Binance Futures (без API-ключа):
 - /fapi/v1/ticker/24hr          — зміна ціни за 24 год, обсяг
 - /fapi/v1/klines               — свічки (1h) для RSI / EMA / тренду
 - /fapi/v1/premiumIndex         — funding rate (настрій ринку)

Логіка оцінки "шансу" (score 0-100): комбінуємо кілька індикаторів,
щоб визначити напрямок (LONG/SHORT) та впевненість. На основі ATR
ставимо вхід, стоп-лос і два тейк-профіти.
"""
import asyncio
import math
import random
import urllib.request
import json
from bot.config import BINANCE_FUTURES_URL

_cache = {"tickers": None, "ts": 0}


def _get(url: str, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


async def fetch_json(url: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get, url)


async def get_top_symbols(count: int = 40):
    """Монети з найбільшим обсягом за 24 год (USDT-перпетуали)."""
    now = asyncio.get_running_loop().time()
    if _cache["tickers"] and now - _cache["ts"] < 60:
        tickers = _cache["tickers"]
    else:
        tickers = await fetch_json(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr")
        _cache["tickers"] = tickers
        _cache["ts"] = now
    usdt = [t for t in tickers
            if t["symbol"].endswith("USDT")
            and not any(x in t["symbol"] for x in ("UP", "DOWN", "BULL", "BEAR"))]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return usdt[:count]


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / period, sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        period = max(len(trs), 1)
    return sum(trs[-period:]) / period


async def analyze_symbol(sym: str):
    """Повертає dict з аналізом або None."""
    try:
        kl = await fetch_json(
            f"{BINANCE_FUTURES_URL}/fapi/v1/klines?symbol={sym}&interval=1h&limit=100")
    except Exception:
        return None
    if not kl or len(kl) < 30:
        return None
    opens = [float(k[1]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    vols = [float(k[5]) for k in kl]
    price = closes[-1]

    rsi = _rsi(closes)
    ema20 = _ema(closes[-40:], 20)
    ema50 = _ema(closes, 50)
    atr = _atr(highs, lows, closes)
    chg24 = None

    # тренд останніх 24 годин
    chg24 = (price - closes[-24]) / closes[-24] * 100 if len(closes) >= 24 else 0
    vol_ratio = sum(vols[-6:]) / 6 / (sum(vols[-24:]) / 24 + 1e-9)

    # funding
    funding = 0.0
    try:
        p = await fetch_json(f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex?symbol={sym}")
        funding = float(p.get("lastFundingRate", 0))
    except Exception:
        pass

    score_long = 50.0
    reasons = []

    # 1. Перетин EMA — сила тренду
    if ema20 > ema50:
        score_long += 10
        reasons.append("EMA20 вище EMA50 — висхідний тренд")
    else:
        score_long -= 10
        reasons.append("EMA20 нижче EMA50 — низхідний тренд")

    # 2. RSI
    if rsi < 30:
        score_long += 15
        reasons.append(f"RSI={rsi:.0f} — перепроданість, можливе відскочування")
    elif rsi > 70:
        score_long -= 15
        reasons.append(f"RSI={rsi:.0f} — перекупленість, можливий відкат")
    elif 45 <= rsi <= 55 and ema20 > ema50:
        score_long += 5
        reasons.append(f"RSI={rsi:.0f} — нейтральний, тренд триває")

    # 3. Обсяги
    if vol_ratio > 1.5 and chg24 > 0:
        score_long += 10
        reasons.append(f"Обсяг виріс у {vol_ratio:.1f}x разом з ціною")
    elif vol_ratio > 1.5 and chg24 < 0:
        score_long -= 10
        reasons.append(f"Обсяг виріс у {vol_ratio:.1f}x при падінні ціни")

    # 4. Funding
    if funding < -0.0003:
        score_long += 8
        reasons.append(f"Funding negative ({funding*100:.3f}%) — шортисти платять, тиск вниз вичерпано")
    elif funding > 0.0005:
        score_long -= 8
        reasons.append(f"Funding високий ({funding*100:.3f}%) — ринок перевантажений лонгами")

    # 5. Моментум 24ч
    if 1 < chg24 < 8 and ema20 > ema50:
        score_long += 7
        reasons.append(f"+{chg24:.1f}% за 24г — помірний моментум без перегріву")
    elif chg24 > 12:
        score_long -= 7
        reasons.append(f"+{chg24:.1f}% за 24г — перегрів, ризик корекції")
    elif -8 < chg24 < -1 and ema20 < ema50:
        score_long -= 7
        reasons.append(f"{chg24:.1f}% за 24г — сильний тиск продавців")

    score_long = max(5, min(95, score_long))

    if score_long >= 50:
        side = "LONG"
        score = round(score_long)
    else:
        side = "SHORT"
        score = round(100 - score_long)

    # Рівні на базі ATR
    a = atr if atr > 0 else price * 0.01
    if side == "LONG":
        entry = price
        sl = price - 1.2 * a
        tp1 = price + 1.5 * a
        tp2 = price + 3.0 * a
    else:
        entry = price
        sl = price + 1.2 * a
        tp1 = price - 1.5 * a
        tp2 = price - 3.0 * a

    return {
        "symbol": sym,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "score": score,
        "reason": "; ".join(reasons[:3]),
        "atr": a,
        "price": price,
    }


def pick_leverage(deposit: float, score: int):
    """Підбір плеча: чим менший депозит — тим обережніше; чим вищий шанс — можна більше."""
    if deposit <= 0:
        return 1, 0
    risk_pct = 0.05 if score >= 80 else 0.03     # ризик на зделку від депозиту
    margin = max(deposit * 0.2, 5)               # маржа на зделку ~20% депозиту
    lev = int(min(max(risk_pct * deposit / (margin * 0.02), 3), 20))
    if deposit < 50:
        lev = min(lev, 5)
    elif deposit < 200:
        lev = min(lev, 10)
    return lev, round(margin, 2)


async def generate_signals(count: int = 5, min_score: int = 65):
    """Аналізує топ-монет, повертає найкращі `count` зделок."""
    syms = await get_top_symbols(40)
    results = []
    queue = [s["symbol"] for s in syms]
    random.shuffle(queue)
    tasks = []
    for s in queue[:20]:
        tasks.append(analyze_symbol(s))
    analyzed = await asyncio.gather(*tasks)
    for r in analyzed:
        if r and r["score"] >= min_score:
            results.append(r)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:count]
