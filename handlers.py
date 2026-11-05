"""Все Telegram-хендлеры бота.

Порядок регистрации важен:
1. команды и кнопки нижнего Reply-меню;
2. callback-кнопки;
3. добавление покупок;
4. медиа;
5. общий текстовый хендлер `F.text` — всегда последним.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from PIL import Image, ImageOps, UnidentifiedImageError

import config
import db
import finance
import llm
import money
import parser
import ui
from states import ShoppingStates

logger = logging.getLogger(__name__)
router = Router(name="family_reminders")

_SHOP_ADD_RE = re.compile(
    r"^(?:купи(?:ть)?|в\s+покупки|добавь(?:те)?\s+в\s+покупки|"
    r"добавить\s+в\s+покупки|запиши\s+в\s+покупки)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Служебные помощники
# ---------------------------------------------------------------------------
async def _answer(
    call: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> None:
    """Гарантированно останавливает спиннер inline-кнопки."""
    try:
        await call.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        logger.debug("Не удалось ответить на callback: %s", exc)
    except TelegramAPIError as exc:
        logger.warning("Ошибка ответа на callback: %s", exc)


async def _safe_delete(message: Optional[Message]) -> None:
    if message is None or not hasattr(message, "delete"):
        return
    try:
        await message.delete()
    except TelegramBadRequest as exc:
        logger.debug("Сообщение уже удалено: %s", exc)
    except TelegramAPIError as exc:
        logger.warning("Не удалось удалить сообщение: %s", exc)


async def _safe_edit(
    message: Optional[Message],
    text: str,
    reply_markup: Any = None,
) -> None:
    if message is None or not hasattr(message, "edit_text"):
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Не удалось отредактировать сообщение: %s", exc)
    except TelegramAPIError as exc:
        logger.warning("Ошибка редактирования сообщения: %s", exc)


async def _callback_reply(call: CallbackQuery, text: str) -> None:
    try:
        if call.message is not None and hasattr(call.message, "answer"):
            await call.message.answer(text)
        else:
            await call.bot.send_message(call.from_user.id, text)
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить сообщение после callback: %s", exc)


# Один экспорт за раз в пределах процесса; от повторной сети/клика дополнительно
# защищает детерминированный external_id на стороне Finance API.
_finance_export_lock = asyncio.Lock()


def _pending_export_batch(items: Sequence[Dict[str, Any]]) -> tuple[Optional[str], List[Dict[str, Any]]]:
    pending = [
        item
        for item in items
        if item.get("finance_batch_id")
        and not item.get("finance_exported_at")
        and int(item.get("price_cents") or 0) > 0
    ]
    if not pending:
        return None, []
    batch_id = str(pending[0]["finance_batch_id"])
    return batch_id, [
        item for item in pending if str(item.get("finance_batch_id")) == batch_id
    ]


def _ready_export_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in items
        if item.get("is_bought")
        and int(item.get("price_cents") or 0) > 0
        and not item.get("finance_exported_at")
        and not item.get("finance_batch_id")
    ]


async def _require_user(message: Message) -> Optional[Dict[str, Any]]:
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(
            ui.welcome_text(None),
            reply_markup=ui.roles_inline_kb(db.connected_roles()),
        )
        return None
    return user


def _is_shopping_message(message: Message) -> bool:
    return bool(message.text and _SHOP_ADD_RE.match(message.text.strip()))


def _parse_shop_items(raw: str, strip_command: bool = True) -> List[str]:
    body = _SHOP_ADD_RE.sub("", raw, count=1) if strip_command else raw
    body = re.sub(r"^\s*[:,\-–—]\s*", "", body)
    chunks = re.split(r"[,;\n]+|\s+и\s+", body, flags=re.IGNORECASE)
    result: List[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        item = re.sub(r"^(?:и|а)\s+", "", chunk.strip(), flags=re.IGNORECASE)
        item = item.strip(" \t.,;:!?—-")
        if len(item) < 2:
            continue
        item = item[:1].upper() + item[1:]
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _looks_like_voice_shopping(text: str) -> bool:
    """Эвристика для голосового ввода: «молоко и хлеб» без слова «купи»."""
    text = (text or "").strip()
    if not text:
        return False
    if _SHOP_ADD_RE.match(text):
        return True
    local = parser.local_parse(text)
    if local.scheduled_at is not None:
        return False
    lowered = text.lower()
    if re.search(
        r"\b(?:напомни|напомнить|позвон|позвонить|встрет|забер|отвез|"
        r"оплат|выключ|включ|запиши|добавь|купи)\b",
        lowered,
    ):
        return False
    chunks = [chunk.strip() for chunk in re.split(r"[,;]+|\s+и\s+", text) if chunk.strip()]
    return len(chunks) >= 2 and all(len(chunk) >= 2 for chunk in chunks)


async def _add_shopping_from_text(
    message: Message,
    user: Dict[str, Any],
    text: str,
    *,
    strip_command: bool = True,
) -> bool:
    items = _parse_shop_items(text, strip_command=strip_command)
    if not items:
        await message.answer(
            "🛒 Что добавить? Например: <code>Купи молоко и хлеб</code>"
        )
        return False
    db.add_shopping_items(items, str(user.get("display_name") or "Участник"))
    logger.info("Пользователь %s добавил покупок: %s", user["tg_id"], len(items))
    await _send_shop(message)
    return True


def _actor_role_key(tg_id: int) -> Optional[str]:
    user = db.get_user(tg_id)
    return str(user.get("role_key") or "") if user else None


def _can_manage(reminder: Dict[str, Any], tg_id: int, role_key: Optional[str]) -> bool:
    creator_id = int(reminder.get("creator_id") or 0)
    target_id = int(reminder.get("target_id") or 0)
    if tg_id in {creator_id, target_id} or target_id == -1:
        return True
    if target_id == 0 and role_key and role_key == reminder.get("target_role_key"):
        return True
    return False


def _can_act_on_fired(reminder: Dict[str, Any], tg_id: int, role_key: Optional[str]) -> bool:
    creator_id = int(reminder.get("creator_id") or 0)
    target_id = int(reminder.get("target_id") or 0)
    if target_id == -1 or target_id == tg_id:
        return True
    if target_id == 0:
        return tg_id == creator_id or (bool(role_key) and role_key == reminder.get("target_role_key"))
    return False


def _target_warning(target: db.TargetInfo) -> Optional[str]:
    if target.target_id == 0 and target.role_key not in {
        config.SELF_ROLE_KEY,
        config.ALL_ROLE_KEY,
    }:
        return (
            f"⚠️ <i>{ui.esc(target.label)} ещё не подключен(а) к боту. "
            "Напоминание привяжется после /start; до этого пинг получит автор.</i>"
        )
    return None


def _warning_for_reminder(reminder: Dict[str, Any]) -> Optional[str]:
    if int(reminder.get("target_id") or 0) != 0:
        return None
    role_key = str(reminder.get("target_role_key") or config.SELF_ROLE_KEY)
    if role_key in {config.SELF_ROLE_KEY, config.ALL_ROLE_KEY}:
        return None
    return (
        f"⚠️ <i>{ui.esc(config.role_label(role_key))} ещё не подключен(а) к боту. "
        "Напоминание привяжется после /start; до этого пинг получит автор.</i>"
    )


async def _download_file(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    if not file.file_path:
        raise ValueError("Telegram не вернул file_path")
    buffer = io.BytesIO()
    await bot.download_file(file.file_path, buffer)
    return buffer.getvalue()


def _compress_photo(raw: bytes) -> bytes:
    """Сжимает фото до 1024x1024 и JPEG quality=78 перед base64."""
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA", "P"}:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=78, optimize=True)
            compressed = output.getvalue()
            logger.info("Фото сжато: %s -> %s байт", len(raw), len(compressed))
            return compressed
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("Не удалось сжать фото (%s), отправляю исходный файл", exc)
        return raw


async def _send_active_list(message: Message, user: Dict[str, Any]) -> None:
    reminders = db.get_active_for_user(int(user["tg_id"]), str(user.get("role_key") or "self"))
    await message.answer(
        ui.active_list_text(reminders),
        reply_markup=ui.list_kb(reminders),
    )


async def _send_shop(message: Message) -> None:
    items = db.get_shopping_items()
    await message.answer(ui.shop_view(items), reply_markup=ui.shop_kb(items))


# ---------------------------------------------------------------------------
# 1. Команды и Reply-меню (регистрируются первыми)
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user:
        await message.answer(ui.welcome_text(user), reply_markup=ui.main_reply_kb())
    else:
        await message.answer(
            ui.welcome_text(None),
            reply_markup=ui.roles_inline_kb(db.connected_roles()),
        )


@router.message(Command("help"))
@router.message(F.text == ui.BUTTON_HELP)
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)
    await message.answer(
        ui.help_text(user),
        reply_markup=ui.main_reply_kb() if user else ui.roles_inline_kb(db.connected_roles()),
    )


@router.message(Command("roles"))
@router.message(F.text == ui.BUTTON_FAMILY)
async def cmd_roles(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        ui.roles_text(db.connected_roles()),
        reply_markup=ui.roles_inline_kb(db.connected_roles()),
    )


@router.message(Command("list"))
@router.message(Command("reminders"))
@router.message(F.text == ui.BUTTON_MY_REMINDERS)
async def cmd_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _require_user(message)
    if user is None:
        return
    await _send_active_list(message, user)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current == ShoppingStates.waiting_price.state:
        await message.answer("✖️ Ввод цены отменён.")
    else:
        await message.answer("Нечего отменять.")


@router.message(Command("shop"))
@router.message(Command("shopping"))
@router.message(F.text == ui.BUTTON_SHOPPING)
async def cmd_shop(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _require_user(message)
    if user is None:
        return
    await _send_shop(message)


# ---------------------------------------------------------------------------
# 2. Callback-кнопки
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("reg:"))
async def cb_register(call: CallbackQuery) -> None:
    key = (call.data or "").split(":", 1)[1] if ":" in (call.data or "") else ""
    role = config.PREDEFINED_ROLES.get(key)
    if role is None:
        await _answer(call, "Неизвестная роль", show_alert=True)
        return
    await _answer(call, "✅ Роль сохранена")
    db.upsert_user(
        call.from_user.id,
        key,
        role["name"],
        ", ".join(role["synonyms"]),
    )
    user = db.get_user(call.from_user.id)
    await _safe_edit(
        call.message,
        "✅ <b>Профиль сохранен!</b>\n\n" + ui.welcome_text(user),
        reply_markup=None,
    )
    if call.message is not None and hasattr(call.message, "answer"):
        try:
            await call.message.answer(
                "Нижнее меню активно 👇",
                reply_markup=ui.main_reply_kb(),
            )
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить Reply-меню: %s", exc)


@router.callback_query(F.data.startswith("shop_tg:"))
async def cb_shop_toggle(call: CallbackQuery) -> None:
    try:
        item_id = int((call.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return await _answer(call, "Позиция не найдена", show_alert=True)
    item = db.get_shopping_item(item_id)
    if item is None:
        return await _answer(call, "Позиция не найдена", show_alert=True)
    if item.get("finance_batch_id") and not item.get("finance_exported_at"):
        return await _answer(
            call,
            "⏳ Передача этой партии уже начата. Нажми «📤 Повторить передачу».",
            show_alert=True,
        )
    if item.get("finance_exported_at"):
        return await _answer(
            call,
            "💸 Расход уже передан в Бот Финансов. Флажок этой позиции зафиксирован.",
            show_alert=True,
        )
    await _answer(call)
    db.toggle_shopping_item(item_id)
    items = db.get_shopping_items()
    await _safe_edit(call.message, ui.shop_view(items), ui.shop_kb(items))


@router.callback_query(F.data.startswith("shop_price:"))
async def cb_shop_price(call: CallbackQuery, state: FSMContext) -> None:
    try:
        item_id = int((call.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return await _answer(call, "Позиция не найдена", show_alert=True)
    item = db.get_shopping_item(item_id)
    if item is None:
        return await _answer(call, "Позиция не найдена", show_alert=True)
    if item.get("finance_batch_id") and not item.get("finance_exported_at"):
        return await _answer(
            call,
            "⏳ Передача этой партии уже начата. Дождись результата или нажми «📤 Повторить передачу».",
            show_alert=True,
        )
    if item.get("finance_exported_at"):
        return await _answer(
            call,
            "💸 Расход по этой позиции уже передан в Бот Финансов. Цену менять нельзя.",
            show_alert=True,
        )

    await state.set_state(ShoppingStates.waiting_price)
    await state.update_data(shop_item_id=item_id)
    await _answer(call)
    text = ui.price_prompt_text(item)
    try:
        if call.message is not None and hasattr(call.message, "answer"):
            await call.message.answer(text, reply_markup=ui.price_prompt_kb())
        else:
            await call.bot.send_message(
                call.from_user.id,
                text,
                reply_markup=ui.price_prompt_kb(),
            )
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить запрос цены: %s", exc)


@router.callback_query(F.data == "shop_price_cancel")
async def cb_shop_price_cancel(call: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current != ShoppingStates.waiting_price.state:
        return await _answer(call, "Ввод цены уже завершён.")
    await state.clear()
    await _answer(call)
    await _safe_edit(call.message, "✖️ Ввод цены отменён.", reply_markup=None)


@router.callback_query(F.data == "shop_finance")
async def cb_shop_finance(call: CallbackQuery) -> None:
    user = db.get_user(call.from_user.id)
    if user is None:
        return await _answer(call, "Сначала нажми /start и выбери роль.", show_alert=True)
    if _finance_export_lock.locked():
        return await _answer(
            call,
            "⏳ Передача в Бот Финансов уже выполняется. Подожди пару секунд.",
            show_alert=True,
        )

    all_items = db.get_shopping_items()
    pending_batch_id, pending_items = _pending_export_batch(all_items)
    ready_items = _ready_export_items(all_items)
    if not pending_items and not ready_items:
        already_exported = any(
            item.get("is_bought")
            and int(item.get("price_cents") or 0) > 0
            and item.get("finance_exported_at")
            for item in all_items
        )
        message = (
            "✅ Все купленные позиции с ценой уже переданы в Бот Финансов."
            if already_exported
            else "Нет купленных позиций с ценой. Поставь ✅ и укажи 💰 стоимость."
        )
        return await _answer(call, message, show_alert=True)

    await _answer(call)

    async with _finance_export_lock:
        # Повторно читаем БД под замком: второй клик мог прийти одновременно.
        all_items = db.get_shopping_items()
        pending_batch_id, pending_items = _pending_export_batch(all_items)
        ready_items = _ready_export_items(all_items)

        if pending_items and pending_batch_id:
            items = pending_items
            external_id = pending_batch_id
            retried_pending = True
        elif ready_items:
            items = ready_items
            external_id = finance.build_export_id(items)
            retried_pending = False
        else:
            already_exported = any(
                int(item.get("price_cents") or 0) > 0 and item.get("finance_exported_at")
                for item in all_items
            )
            await _callback_reply(
                call,
                "✅ Все купленные позиции с ценой уже переданы в Бот Финансов."
                if already_exported
                else "Нет купленных позиций с ценой.",
            )
            return

        if not config.FINANCE_API_URL:
            await _callback_reply(
                call,
                "⚠️ Финансовый бот не настроен: укажи <code>FINANCE_API_URL</code>.",
            )
            return

        if not retried_pending:
            # Застолбить партию ДО запроса: при потере ответа повтор пойдёт
            # с тем же batch_id и тем же набором позиций.
            claimed = db.begin_shopping_export([int(item["id"]) for item in items], external_id)
            if claimed != len(items):
                all_items = db.get_shopping_items()
                pending_batch_id, pending_items = _pending_export_batch(all_items)
                if not pending_items or not pending_batch_id:
                    await _callback_reply(
                        call,
                        "⚠️ Не удалось зафиксировать партию для передачи. Обнови список и попробуй снова.",
                    )
                    return
                items = pending_items
                external_id = pending_batch_id
                retried_pending = True

        bought_unpriced = sum(
            1
            for item in all_items
            if item.get("is_bought") and int(item.get("price_cents") or 0) <= 0
        )

        try:
            result = await finance.send_expenses(
                items,
                ui.user_label(user),
                external_id=external_id,
            )
        except Exception:
            logger.exception("Неожиданная ошибка передачи расходов в финансовый бот")
            result = finance.FinanceResult(
                ok=False,
                error="Внутренняя ошибка при передаче расходов. Попробуй ещё раз.",
                retriable=True,
            )

        if not result.ok:
            if not result.retriable:
                db.clear_shopping_export_batch(external_id)
            note = (
                "\n⏳ Партия сохранена. Нажми «📤 Повторить передачу»."
                if result.retriable
                else "\nМожно исправить данные и попробовать снова."
            )
            await _callback_reply(call, f"⚠️ {ui.esc(result.error)}{note}")
            refreshed = db.get_shopping_items()
            await _safe_edit(call.message, ui.shop_view(refreshed), ui.shop_kb(refreshed))
            return

        exported_at = config.format_db_dt(config.now_minsk())
        changed = db.mark_shopping_exported(
            [int(item["id"]) for item in items],
            result.batch_id,
            exported_at,
        )
        logger.info(
            "В Финансы передано %s позиций, отмечено %s, batch=%s, exported_at=%s",
            len(items),
            changed,
            result.batch_id,
            exported_at,
        )
        refreshed = db.get_shopping_items()
        remaining_ready = len(_ready_export_items(refreshed))
        await _safe_edit(call.message, ui.shop_view(refreshed), ui.shop_kb(refreshed))
        confirmation = ui.finance_result_text(
            len(items),
            result.total_cents,
            duplicate=result.duplicate,
            skipped_unpriced=bought_unpriced,
            retried_pending=retried_pending,
            remaining_ready=remaining_ready,
        )
        await _callback_reply(call, confirmation)


@router.callback_query(F.data == "shop_clr")
async def cb_shop_clear(call: CallbackQuery) -> None:
    if db.get_pending_shopping_items():
        return await _answer(
            call,
            "⏳ Сначала заверши передачу в Бот Финансов. Пока есть незавершённая партия, очистка недоступна.",
            show_alert=True,
        )
    await _answer(call)
    db.clear_bought_items()
    items = db.get_shopping_items()
    await _safe_edit(call.message, ui.shop_view(items), ui.shop_kb(items))


@router.callback_query(F.data == "list:refresh")
async def cb_list_refresh(call: CallbackQuery) -> None:
    await _answer(call)
    user = db.get_user(call.from_user.id)
    if user is None:
        await _safe_edit(call.message, "Сначала нажми /start и выбери роль.")
        return
    reminders = db.get_active_for_user(int(user["tg_id"]), str(user.get("role_key") or "self"))
    await _safe_edit(
        call.message,
        ui.active_list_text(reminders),
        ui.list_kb(reminders),
    )


@router.callback_query(F.data.startswith("rem_shift:"))
async def cb_shift_created(call: CallbackQuery) -> None:
    await _answer(call)
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        return
    try:
        reminder_id = int(parts[1])
        delta_minutes = int(parts[2])
    except ValueError:
        return
    if delta_minutes not in {-10, 10, 60}:
        return

    reminder = db.get_reminder(reminder_id)
    if reminder is None:
        await _safe_edit(call.message, "Напоминание не найдено.")
        return
    if int(reminder.get("creator_id") or 0) != call.from_user.id:
        await _safe_edit(call.message, "⛔️ Менять время может только автор напоминания.")
        return
    status = str(reminder.get("status") or "")
    if status in {"COMPLETED", "CANCELLED"}:
        await _safe_edit(call.message, ui.status_label(status))
        return

    old_dt = config.parse_db_dt(str(reminder.get("scheduled_at") or ""))
    now = config.now_minsk()
    new_dt = (old_dt or now) + timedelta(minutes=delta_minutes)
    if new_dt <= now:
        new_dt = now + timedelta(minutes=1)
    db.reschedule_reminder(reminder_id, config.format_db_dt(new_dt))
    target_label = db.target_label_for_reminder(reminder)
    await _safe_edit(
        call.message,
        ui.creation_card(
            str(reminder.get("parsed_text") or ""),
            target_label,
            new_dt,
            _warning_for_reminder(reminder),
        ),
        ui.created_kb(reminder_id),
    )


@router.callback_query(F.data.startswith("rem_cancel:"))
async def cb_cancel(call: CallbackQuery) -> None:
    await _answer(call)
    try:
        reminder_id = int((call.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return
    reminder = db.get_reminder(reminder_id)
    if reminder is None:
        await _safe_edit(call.message, "Напоминание не найдено.")
        return
    role_key = _actor_role_key(call.from_user.id)
    if not _can_manage(reminder, call.from_user.id, role_key):
        await _safe_edit(call.message, "⛔️ Это напоминание не для тебя.")
        return
    status = str(reminder.get("status") or "")
    if status in {"COMPLETED", "CANCELLED"}:
        await _safe_edit(call.message, ui.status_label(status))
        return
    db.cancel_reminder(reminder_id)
    await _safe_edit(
        call.message,
        ui.cancelled_card(str(reminder.get("parsed_text") or "")),
        reply_markup=None,
    )


@router.callback_query(F.data.startswith("rem_done:"))
async def cb_done(call: CallbackQuery) -> None:
    await _answer(call)
    try:
        reminder_id = int((call.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return
    reminder = db.get_reminder(reminder_id)
    if reminder is None:
        await _safe_edit(call.message, "Напоминание не найдено.")
        return
    role_key = _actor_role_key(call.from_user.id)
    if not _can_act_on_fired(reminder, call.from_user.id, role_key):
        await _safe_edit(call.message, "⛔️ Это напоминание не для тебя.")
        return
    status = str(reminder.get("status") or "")
    if status == "COMPLETED":
        await _safe_edit(call.message, ui.completed_card(str(reminder.get("parsed_text") or "")))
        return
    if status == "CANCELLED":
        await _safe_edit(call.message, ui.cancelled_card(str(reminder.get("parsed_text") or "")))
        return

    db.complete_reminder(reminder_id)
    task_text = str(reminder.get("parsed_text") or "")
    await _safe_edit(call.message, ui.completed_card(task_text), reply_markup=None)

    creator_id = int(reminder.get("creator_id") or 0)
    if creator_id and creator_id != call.from_user.id:
        actor = db.get_user(call.from_user.id)
        if actor is None:
            # cb_done больше не падает на незарегистрированном пользователе.
            actor = {
                "tg_id": call.from_user.id,
                "display_name": call.from_user.full_name,
                "role_key": "",
            }
        try:
            await call.bot.send_message(
                creator_id,
                ui.completed_by_text(actor, task_text),
            )
        except TelegramForbiddenError:
            logger.warning("Автор %s заблокировал бота, отбивка не доставлена", creator_id)
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить отбивку автору %s: %s", creator_id, exc)


@router.callback_query(F.data.startswith("rem_snooze:"))
async def cb_snooze(call: CallbackQuery) -> None:
    await _answer(call)
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        return
    try:
        reminder_id = int(parts[1])
        minutes = int(parts[2])
    except ValueError:
        return
    if minutes not in {10, 30, 60}:
        return

    reminder = db.get_reminder(reminder_id)
    if reminder is None:
        await _safe_edit(call.message, "Напоминание не найдено.")
        return
    role_key = _actor_role_key(call.from_user.id)
    if not _can_act_on_fired(reminder, call.from_user.id, role_key):
        await _safe_edit(call.message, "⛔️ Это напоминание не для тебя.")
        return
    status = str(reminder.get("status") or "")
    if status == "COMPLETED":
        await _safe_edit(call.message, ui.completed_card(str(reminder.get("parsed_text") or "")))
        return
    if status == "CANCELLED":
        await _safe_edit(call.message, ui.cancelled_card(str(reminder.get("parsed_text") or "")))
        return

    new_dt = config.now_minsk() + timedelta(minutes=minutes)
    db.reschedule_reminder(reminder_id, config.format_db_dt(new_dt))
    await _safe_edit(
        call.message,
        ui.postponed_card(str(reminder.get("parsed_text") or ""), new_dt),
        reply_markup=None,
    )


@router.callback_query(F.data.startswith("rem_tomorrow:"))
async def cb_tomorrow(call: CallbackQuery) -> None:
    await _answer(call)
    try:
        reminder_id = int((call.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return
    reminder = db.get_reminder(reminder_id)
    if reminder is None:
        await _safe_edit(call.message, "Напоминание не найдено.")
        return
    role_key = _actor_role_key(call.from_user.id)
    if not _can_act_on_fired(reminder, call.from_user.id, role_key):
        await _safe_edit(call.message, "⛔️ Это напоминание не для тебя.")
        return
    status = str(reminder.get("status") or "")
    if status == "COMPLETED":
        await _safe_edit(call.message, ui.completed_card(str(reminder.get("parsed_text") or "")))
        return
    if status == "CANCELLED":
        await _safe_edit(call.message, ui.cancelled_card(str(reminder.get("parsed_text") or "")))
        return

    now = config.now_minsk()
    new_dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    db.reschedule_reminder(reminder_id, config.format_db_dt(new_dt))
    await _safe_edit(
        call.message,
        ui.postponed_card(str(reminder.get("parsed_text") or ""), new_dt),
        reply_markup=None,
    )


# ---------------------------------------------------------------------------
# 3. Ввод цены (FSM) и покупки
# ---------------------------------------------------------------------------
@router.message(StateFilter(ShoppingStates.waiting_price), F.text)
async def on_shop_price_input(message: Message, state: FSMContext) -> None:
    await _apply_price_text(message, state, message.text or "")


async def _apply_price_text(message: Message, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    try:
        item_id = int(data.get("shop_item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    item = db.get_shopping_item(item_id) if item_id else None
    if item is None:
        await state.clear()
        await message.answer("Позиция уже удалена. Открой список покупок заново.")
        return
    if item.get("finance_batch_id") and not item.get("finance_exported_at"):
        await state.clear()
        await message.answer("⏳ Передача этой партии уже начата. Изменять цену нельзя.")
        return
    if item.get("finance_exported_at"):
        await state.clear()
        await message.answer("💸 Расход уже передан в Бот Финансов. Цену менять нельзя.")
        return

    price_cents = money.parse_price_to_cents(text or "")
    if price_cents is None:
        await message.answer(ui.price_invalid_text())
        return

    db.set_shopping_price(item_id, price_cents)
    updated = db.get_shopping_item(item_id)
    await state.clear()
    await message.answer(ui.price_saved_text(updated or item))
    await _send_shop(message)


# ---------------------------------------------------------------------------
# 4. Добавление покупок (до общего текстового хендлера)
# ---------------------------------------------------------------------------
@router.message(F.text, _is_shopping_message)
async def on_shopping_text(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    await _add_shopping_from_text(message, user, message.text or "")


# ---------------------------------------------------------------------------
# 5. Медиа
# ---------------------------------------------------------------------------
@router.message(F.photo)
async def on_photo(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    try:
        raw = await _download_file(message.bot, message.photo[-1].file_id)
        compressed = await asyncio.to_thread(_compress_photo, raw)
        media = {
            "type": "image",
            "data": base64.b64encode(compressed).decode("ascii"),
            "mime": "image/jpeg",
        }
        await _process_input(
            message,
            user,
            (message.caption or "").strip(),
            media=media,
            original_text=(message.caption or "").strip() or "[Фото]",
        )
    except TelegramAPIError as exc:
        logger.exception("Не удалось скачать фото: %s", exc)
        await message.answer("📷 Не удалось скачать фото. Попробуй отправить ещё раз.")
    except Exception:
        logger.exception("Не удалось обработать фото")
        await message.answer("📷 Не получилось разобрать фото. Попробуй текстом.")


@router.message(F.voice)
async def on_voice(message: Message, state: FSMContext) -> None:
    user = await _require_user(message)
    if user is None:
        return
    status_message: Optional[Message] = None
    try:
        status_message = await message.answer("🎙 <i>Распознаю голосовое...</i>")
        raw = await _download_file(message.bot, message.voice.file_id)
        media = {
            "type": "audio",
            "data": base64.b64encode(raw).decode("ascii"),
            "mime": "audio/ogg",
        }
        is_price_input = await state.get_state() == ShoppingStates.waiting_price.state
        transcript = await llm.transcribe_voice(
            media,
            purpose="price" if is_price_input else "reminder",
        )
        await _safe_delete(status_message)
        status_message = None

        if not transcript:
            await message.answer(
                "🎙 Не расслышал голосовое. Попробуй ещё раз или напиши текстом."
            )
            return

        logger.info("Голосовое распознано как: %s", transcript[:200])
        if is_price_input:
            if transcript.strip(" .!").lower() in {"отмена", "отмени", "отменить", "cancel"}:
                await state.clear()
                await message.answer("✖️ Ввод цены отменён.")
                return
            await _apply_price_text(message, state, transcript)
            return
        if _looks_like_voice_shopping(transcript):
            strip_command = bool(_SHOP_ADD_RE.match(transcript))
            await _add_shopping_from_text(
                message,
                user,
                transcript,
                strip_command=strip_command,
            )
            return
        await _process_input(
            message,
            user,
            transcript,
            media=None,
            original_text=transcript,
        )
    except TelegramAPIError as exc:
        logger.exception("Не удалось скачать/обработать голосовое: %s", exc)
        await message.answer("🎙 Не удалось скачать голосовое. Попробуй ещё раз.")
    except Exception:
        logger.exception("Не удалось обработать голосовое")
        await message.answer("🎙 Не получилось разобрать голосовое. Попробуй текстом.")
    finally:
        await _safe_delete(status_message)


# ---------------------------------------------------------------------------
# 6. Общий текстовый хендлер — строго последний
# ---------------------------------------------------------------------------
@router.message(F.text)
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        await message.answer("🤔 Неизвестная команда. Напиши /help или просто задачу текстом.")
        return
    user = await _require_user(message)
    if user is None:
        return
    await _process_input(message, user, text, media=None, original_text=text)


# ---------------------------------------------------------------------------
# Создание напоминаний
# ---------------------------------------------------------------------------
async def _notify_planned(
    bot: Bot,
    creator: Dict[str, Any],
    target: db.TargetInfo,
    item: parser.ParsedReminder,
) -> None:
    if target.target_id == -1:
        for member in db.get_user_list():
            if int(member["tg_id"]) == int(creator["tg_id"]):
                continue
            try:
                await bot.send_message(
                    int(member["tg_id"]),
                    ui.planned_for_you_text(
                        creator,
                        item.task_text,
                        item.scheduled_at,
                        for_all=True,
                    ),
                )
            except TelegramForbiddenError:
                logger.info("Участник %s заблокировал бота", member["tg_id"])
            except TelegramAPIError as exc:
                logger.warning("Не удалось уведомить участника %s: %s", member["tg_id"], exc)
        return

    if target.target_id <= 0 or target.target_id == int(creator["tg_id"]):
        return
    try:
        await bot.send_message(
            target.target_id,
            ui.planned_for_you_text(
                creator,
                item.task_text,
                item.scheduled_at,
                for_all=False,
            ),
        )
    except TelegramForbiddenError:
        logger.info("Получатель %s заблокировал бота", target.target_id)
    except TelegramAPIError as exc:
        logger.warning("Не удалось уведомить получателя %s: %s", target.target_id, exc)


async def _create_reminder_from_item(
    message: Message,
    user: Dict[str, Any],
    item: parser.ParsedReminder,
    original_text: str,
) -> int:
    target = db.resolve_target(user, item.target_role_key)
    reminder_id = db.create_reminder(
        creator_id=int(user["tg_id"]),
        target_id=target.target_id,
        target_role_key=target.role_key,
        original_text=original_text,
        parsed_text=item.task_text,
        scheduled_at=config.format_db_dt(item.scheduled_at),
    )
    logger.info(
        "Создано напоминание #%s: %s -> %s (%s, source=%s)",
        reminder_id,
        item.task_text,
        config.format_db_dt(item.scheduled_at),
        target.label,
        item.source,
    )
    await message.answer(
        ui.creation_card(
            item.task_text,
            target.label,
            item.scheduled_at,
            _target_warning(target),
        ),
        reply_markup=ui.created_kb(reminder_id),
    )
    await _notify_planned(message.bot, user, target, item)
    return reminder_id


async def _process_input(
    message: Message,
    user: Dict[str, Any],
    raw_text: str,
    *,
    media: Optional[Dict[str, Any]] = None,
    original_text: Optional[str] = None,
) -> None:
    now = config.now_minsk()
    local = parser.local_parse(raw_text, now) if raw_text.strip() else parser.LocalParse()
    status_message: Optional[Message] = None
    try:
        if llm.should_use_ai(local, raw_text, media):
            status_message = await message.answer("⏳ <i>Анализирую...</i>")
            items, source = await llm.parse_reminders(
                user,
                raw_text,
                media=media,
                local=local,
                now=now,
            )
            await _safe_delete(status_message)
            status_message = None
        else:
            items, source = parser.items_from_local(local), "local"

        items = [item for item in items if item.task_text and item.scheduled_at]
        if not items:
            await message.answer(ui.clarify_text())
            return

        if len(items) > 5:
            logger.info("Обрезаю количество напоминаний с %s до 5", len(items))
            items = items[:5]

        stored_original = (original_text or raw_text or "").strip()
        for item in items:
            await _create_reminder_from_item(message, user, item, stored_original)
        logger.info(
            "Обработано %s напоминаний пользователя %s (source=%s)",
            len(items),
            user["tg_id"],
            source,
        )
    except Exception:
        logger.exception("Не удалось обработать входящее сообщение")
        try:
            await message.answer(
                "⚠️ Не получилось сохранить напоминание. Попробуй ещё раз или напиши /help."
            )
        except TelegramAPIError:
            logger.warning("Не удалось сообщить пользователю об ошибке")
    finally:
        await _safe_delete(status_message)
