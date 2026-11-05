"""Деньги для списка покупок: разбор ввода и форматирование.

Цены хранятся в копейках (`price_cents`), чтобы избежать ошибок округления
float. На ввод принимаем «2,50», «2.5», «2,50 руб», «2,50 BYN».
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

import config

_CURRENCY_WORDS_RE = re.compile(
    r"(?iu)(?:byn|br|бел\.?\s*руб(?:\.|лей|ля|ль)?|руб(?:\.|лей|ля|ль)?|р\.?|₽)"
)


def parse_price_to_cents(raw: str) -> Optional[int]:
    """Возвращает цену в копейках или None, если ввод не похож на цену.

    Ноль разрешён: так пользователь может сбросить цену позиции.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    text = text.replace("\u00a0", " ").replace(" ", "")
    text = _CURRENCY_WORDS_RE.sub("", text)
    text = text.strip(".,;:")
    text = text.replace(",", ".")
    if not re.fullmatch(r"\d{1,7}(?:\.\d{1,2})?", text):
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if amount < 0 or amount > Decimal("1000000"):
        return None
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents


def format_money(cents: int, currency: Optional[str] = None) -> str:
    """Форматирует копейки: 250 -> «2,50 BYN», 0 -> «0,00 BYN»."""
    try:
        cents_value = int(cents or 0)
    except (TypeError, ValueError):
        cents_value = 0
    if cents_value < 0:
        cents_value = 0
    currency = (currency or config.CURRENCY or "BYN").upper()
    rubles, kopecks = divmod(cents_value, 100)
    grouped = f"{rubles:,}".replace(",", " ")
    return f"{grouped},{kopecks:02d} {currency}"


def format_money_plain(cents: int, currency: Optional[str] = None) -> str:
    """Без символа валюты — для callback-текста, если нужно."""
    value = format_money(cents, currency)
    return value.rsplit(" ", 1)[0]
