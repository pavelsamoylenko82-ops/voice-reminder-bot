"""Передача расходов из списка покупок в «Бот Финансов».

Обмен идёт по HTTP API финансового бота:
POST {FINANCE_API_URL}
{
  "source": "family_reminder_bot",
  "author": "Паша (Сын)",
  "external_id": "shop-...",
  "items": [
    {"description": "Молоко", "amount": 2.5, "currency": "BYN",
     "category": "Продукты", "tx_type": "expense"}
  ]
}

`external_id` детерминирован для набора позиций, поэтому повторная отправка
после сетевого таймаута не создаёт дубль: финансовый бот возвращает уже
существующую партию.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

import httpx

import config

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.FINANCE_API_TIMEOUT, connect=5.0),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass(frozen=True)
class FinanceResult:
    ok: bool
    batch_id: str = ""
    total_cents: int = 0
    error: str = ""
    duplicate: bool = False
    retriable: bool = True


def build_export_id(items: Sequence[Dict[str, Any]]) -> str:
    # Только ID позиций: повторная отправка того же набора остаётся тем же
    # ключом даже если пользователь успел поправить цену. Новые позиции
    # получают новый ID и, соответственно, новую партию.
    raw = "|".join(str(int(item["id"])) for item in items)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"shop-{digest}"


def _payload(items: Sequence[Dict[str, Any]], author: str, external_id: str) -> Dict[str, Any]:
    return {
        "source": "family_reminder_bot",
        "author": author or "Семья",
        "external_id": external_id,
        "items": [
            {
                "description": str(item.get("item_text") or "Покупка")[:200],
                "amount": round(int(item.get("price_cents") or 0) / 100.0, 2),
                "currency": config.CURRENCY,
                "category": config.FINANCE_CATEGORY,
                "tx_type": "expense",
            }
            for item in items
        ],
    }


async def send_expenses(
    items: Sequence[Dict[str, Any]],
    author: str = "",
    external_id: Optional[str] = None,
) -> FinanceResult:
    if not items:
        return FinanceResult(ok=False, error="Нет позиций для передачи", retriable=False)
    if not config.FINANCE_API_URL:
        return FinanceResult(
            ok=False,
            error=(
                "Финансовый бот не настроен: укажи FINANCE_API_URL "
                "(например, http://finance_tracker_bot:8081/api/expenses)."
            ),
            retriable=False,
        )

    external_id = external_id or build_export_id(items)
    total_cents = sum(int(item.get("price_cents") or 0) for item in items)
    headers = {"Content-Type": "application/json"}
    if config.FINANCE_API_TOKEN:
        headers["X-API-Token"] = config.FINANCE_API_TOKEN

    try:
        response = await get_client().post(
            config.FINANCE_API_URL,
            json=_payload(items, author, external_id),
            headers=headers,
        )
    except httpx.TimeoutException:
        logger.warning("Финансовый API не ответил вовремя")
        return FinanceResult(
            ok=False,
            total_cents=total_cents,
            error="Финансовый бот не ответил вовремя. Попробуй ещё раз.",
            retriable=True,
        )
    except httpx.HTTPError as exc:
        logger.warning("Ошибка сети при передаче расходов: %s", exc)
        return FinanceResult(
            ok=False,
            total_cents=total_cents,
            error="Нет связи с финансовым ботом. Попробуй ещё раз.",
            retriable=True,
        )

    if response.status_code >= 400:
        body = (response.text or "").strip()[:300]
        logger.warning("Финансовый API HTTP %s: %s", response.status_code, body)
        if response.status_code in {401, 403}:
            detail = "неверный FINANCE_API_TOKEN"
        else:
            detail = body or f"HTTP {response.status_code}"
        retriable = response.status_code in {408, 429} or response.status_code >= 500
        return FinanceResult(
            ok=False,
            total_cents=total_cents,
            error=f"Финансовый бот отклонил расходы: {detail}",
            retriable=retriable,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("ok") is False:
        return FinanceResult(
            ok=False,
            total_cents=total_cents,
            error=str(data.get("error") or "финансовый бот вернул ошибку"),
            retriable=False,
        )
    batch_id = str(data.get("batch_id") or external_id)
    duplicate = bool(data.get("duplicate"))
    logger.info(
        "В финансовый бот передано %s позиций на %s коп. batch=%s duplicate=%s",
        len(items),
        total_cents,
        batch_id,
        duplicate,
    )
    return FinanceResult(
        ok=True,
        batch_id=batch_id,
        total_cents=total_cents,
        duplicate=duplicate,
    )
