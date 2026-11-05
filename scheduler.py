"""Фоновый планировщик и система «анти-игнор».

Логика:
* PENDING + scheduled_at <= now -> отправляем карточку, status=SENT,
  retry_count=1, last_sent_at=now;
* пока status=SENT и пользователь не нажал «Выполнено»/«Отложить»,
  каждые REMINDER_RETRY_MINUTES минут отправляем повторный пинг;
* всего 3 уведомления (1/3, 2/3, 3/3), после чего status=EXPIRED;
* перед повторным пингом старое сообщение с кнопками удаляется, если известен
  его message_id (для прямого получателя; для рассылки «всем» не удаляем,
  чтобы не задеть чужие сообщения).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message

import config
import db
import ui

logger = logging.getLogger(__name__)


async def _send(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any = None,
) -> Optional[Message]:
    for attempt in range(2):
        try:
            return await bot.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramRetryAfter as exc:
            if attempt == 0:
                logger.warning("Telegram flood control, ждём %s сек.", exc.retry_after)
                await asyncio.sleep(float(exc.retry_after))
                continue
            logger.error("Telegram flood control не отпустил chat_id=%s", chat_id)
        except TelegramForbiddenError:
            logger.warning("chat_id=%s заблокировал бота", chat_id)
        except TelegramBadRequest as exc:
            logger.warning("Некорректный запрос для chat_id=%s: %s", chat_id, exc)
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить сообщение chat_id=%s: %s", chat_id, exc)
        return None
    return None


async def _delete_old_message(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest as exc:
        logger.debug("Старое сообщение %s уже недоступно: %s", message_id, exc)
    except TelegramForbiddenError:
        logger.warning("Нет доступа удалить сообщение в chat_id=%s", chat_id)
    except TelegramAPIError as exc:
        logger.warning("Не удалось удалить сообщение %s: %s", message_id, exc)


def _from_label(reminder: Dict[str, Any], chat_id: int) -> Optional[str]:
    creator_id = int(reminder.get("creator_id") or 0)
    if chat_id == creator_id:
        return None
    creator = db.get_user(creator_id)
    return ui.user_label(creator) if creator else None


def _recipients(reminder: Dict[str, Any]) -> tuple[List[int], Optional[str]]:
    chat_ids, warning = db.resolve_chat_ids_for_reminder(reminder)
    # Убираем дубли, сохраняя порядок.
    return list(dict.fromkeys(int(chat_id) for chat_id in chat_ids if chat_id)), warning


async def _deliver_initial(bot: Bot, reminder: Dict[str, Any]) -> None:
    reminder_id = int(reminder["id"])
    task_text = str(reminder.get("parsed_text") or "")
    scheduled_at = reminder.get("scheduled_at")
    target_id = int(reminder.get("target_id") or 0)
    creator_id = int(reminder.get("creator_id") or 0)
    chat_ids, warning = _recipients(reminder)
    if not chat_ids:
        logger.error("Напоминание #%s не имеет получателей", reminder_id)
        return

    for chat_id in chat_ids:
        text = ui.fired_card(
            task_text,
            scheduled_at,
            from_label=_from_label(reminder, chat_id),
        )
        if warning and chat_id == creator_id:
            text += f"\n\n⚠️ <i>{ui.esc(warning)}</i>"
        sent = await _send(bot, chat_id, text, ui.fired_kb(reminder_id))
        if sent is not None and target_id > 0:
            db.set_sent_message_id(reminder_id, sent.message_id)


async def _deliver_retry(
    bot: Bot,
    reminder: Dict[str, Any],
    attempt: int,
) -> None:
    reminder_id = int(reminder["id"])
    task_text = str(reminder.get("parsed_text") or "")
    scheduled_at = reminder.get("scheduled_at")
    target_id = int(reminder.get("target_id") or 0)
    creator_id = int(reminder.get("creator_id") or 0)
    chat_ids, warning = _recipients(reminder)
    if not chat_ids:
        logger.error("Повтор #%s: нет получателей", reminder_id)
        return

    for chat_id in chat_ids:
        text = ui.retry_card(
            task_text,
            scheduled_at,
            attempt,
            config.MAX_RETRY_COUNT,
            from_label=_from_label(reminder, chat_id),
        )
        if warning and chat_id == creator_id:
            text += f"\n\n⚠️ <i>{ui.esc(warning)}</i>"
        sent = await _send(bot, chat_id, text, ui.fired_kb(reminder_id))
        if sent is not None and target_id > 0:
            db.set_sent_message_id(reminder_id, sent.message_id)


async def _process_due(bot: Bot) -> None:
    now = config.now_minsk()
    now_str = config.format_db_dt(now)
    due = db.get_due_pending(now_str)
    for reminder in due:
        reminder_id = int(reminder["id"])
        # Сначала фиксируем факт отправки, чтобы при падении не отправить дважды.
        db.mark_sent_started(reminder_id, now_str)
        logger.info(
            "Сработало напоминание #%s: %s в %s",
            reminder_id,
            reminder.get("parsed_text"),
            reminder.get("scheduled_at"),
        )
        await _deliver_initial(bot, reminder)


async def _process_retries(bot: Bot) -> None:
    now = config.now_minsk()
    now_str = config.format_db_dt(now)
    cutoff = config.format_db_dt(now - timedelta(minutes=config.RETRY_INTERVAL_MINUTES))
    due = db.get_due_retries(cutoff)
    for reminder in due:
        reminder_id = int(reminder["id"])
        current_retry = int(reminder.get("retry_count") or 0)
        next_retry = current_retry + 1
        if next_retry > config.MAX_RETRY_COUNT:
            db.update_reminder(reminder_id, status="EXPIRED")
            continue

        target_id = int(reminder.get("target_id") or 0)
        old_message_id = int(reminder.get("sent_message_id") or 0)
        if target_id > 0 and old_message_id:
            await _delete_old_message(bot, target_id, old_message_id)

        status = "SENT"
        db.set_retry_state(reminder_id, next_retry, now_str, status)
        logger.info(
            "Повторное напоминание #%s (%s/%s)",
            reminder_id,
            next_retry,
            config.MAX_RETRY_COUNT,
        )
        await _deliver_retry(bot, reminder, next_retry)
        if next_retry >= config.MAX_RETRY_COUNT:
            db.update_reminder(reminder_id, status="EXPIRED")


async def scheduler_loop(bot: Bot) -> None:
    logger.info(
        "Планировщик запущен: тик %s сек., повтор каждые %s мин., максимум %s",
        config.SCHEDULER_TICK_SECONDS,
        config.RETRY_INTERVAL_MINUTES,
        config.MAX_RETRY_COUNT,
    )
    while True:
        try:
            db.expire_finished_retries()
            await _process_due(bot)
            await _process_retries(bot)
        except asyncio.CancelledError:
            logger.info("Планировщик остановлен")
            raise
        except Exception:
            # Цикл не должен умирать из-за одной ошибки БД/сети.
            logger.exception("Ошибка в цикле планировщика")
        await asyncio.sleep(config.SCHEDULER_TICK_SECONDS)


# Обратная совместимость со старым именем из main.py.
task_loop = scheduler_loop
