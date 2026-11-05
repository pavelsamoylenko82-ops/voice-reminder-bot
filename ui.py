"""Тексты, карточки и клавиатуры бота.

Всё HTML-экранирование пользовательских данных происходит здесь, чтобы
handlers и scheduler не дублировали одно и то же.
"""

from __future__ import annotations

import html as html_lib
import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

import config
import money

BUTTON_MY_REMINDERS = "📋 Мои напоминания"
BUTTON_SHOPPING = "🛒 Список покупок"
BUTTON_FAMILY = "👨‍👩‍👧‍👦 Семья и роли"
BUTTON_HELP = "💡 Примеры и помощь"


def esc(value: Any) -> str:
    return html_lib.escape(str(value or ""), quote=False)


def shorten(value: str, limit: int = 80) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = config.parse_db_dt(value)
        if dt is None:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(config.MINSK_TZ).replace(tzinfo=None)
    return dt


def format_human_dt(value: Any) -> str:
    dt = _as_datetime(value)
    if dt is None:
        return str(value or "")
    now = config.now_minsk()
    days_diff = (dt.date() - now.date()).days
    prefixes = {
        0: "Сегодня",
        1: "Завтра",
        2: "Послезавтра",
        -1: "Вчера",
        -2: "Позавчера",
    }
    prefix = prefixes.get(days_diff)
    if prefix:
        return f"{prefix} в {dt.strftime('%H:%M')}"
    weekday = config.WEEKDAYS_RU_SHORT[dt.weekday()]
    return f"{weekday}, {dt.strftime('%d.%m.%Y')} в {dt.strftime('%H:%M')}"


def format_delta_hint(value: Any) -> str:
    dt = _as_datetime(value)
    if dt is None:
        return ""
    diff_seconds = (dt - config.now_minsk()).total_seconds()
    if diff_seconds <= 0:
        return "уже наступило"
    if diff_seconds < 60:
        return "меньше минуты"
    minutes = max(1, math.ceil(diff_seconds / 60))
    if minutes < 60:
        return f"через {minutes} мин."
    hours, rest_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"через {hours} ч. {rest_minutes} мин." if rest_minutes else f"через {hours} ч."
    days, rest_hours = divmod(hours, 24)
    return f"через {days} дн. {rest_hours} ч." if rest_hours else f"через {days} дн."


def status_label(status: str) -> str:
    return config.STATUS_LABELS.get((status or "").upper(), status or "—")


def user_label(user: Optional[Dict[str, Any]]) -> str:
    if not user:
        return "Участник"
    role_key = user.get("role_key")
    if role_key in config.PREDEFINED_ROLES:
        return config.PREDEFINED_ROLES[role_key]["label"]
    return user.get("display_name") or "Участник"


def user_gender(user: Optional[Dict[str, Any]]) -> Optional[str]:
    if not user:
        return None
    return config.role_gender(str(user.get("role_key") or ""))


def verb_for_user(user: Optional[Dict[str, Any]], male: str, female: str) -> str:
    gender = user_gender(user)
    if gender == "f":
        return female
    if gender == "m":
        return male
    return f"{male}(а)"


def reminder_target_label(reminder: Dict[str, Any]) -> str:
    if reminder.get("target_id") == -1 or reminder.get("target_role_key") == config.ALL_ROLE_KEY:
        return config.ALL_LABEL
    if reminder.get("target_id") == reminder.get("creator_id") or reminder.get("target_role_key") in {
        None,
        "",
        config.SELF_ROLE_KEY,
    }:
        return config.SELF_LABEL
    return config.role_label(str(reminder.get("target_role_key") or config.SELF_ROLE_KEY))


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------
def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_MY_REMINDERS), KeyboardButton(text=BUTTON_SHOPPING)],
            [KeyboardButton(text=BUTTON_FAMILY), KeyboardButton(text=BUTTON_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши напоминание...",
    )


def roles_inline_kb(connected: Optional[Iterable[str]] = None) -> InlineKeyboardMarkup:
    connected_set = set(connected or ())
    rows = [
        [
            InlineKeyboardButton(
                text=f"{role['emoji']} {role['label']}" + (" ✅" if key in connected_set else ""),
                callback_data=f"reg:{key}",
            )
        ]
        for key, role in config.PREDEFINED_ROLES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def created_kb(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏳ -10 мин", callback_data=f"rem_shift:{reminder_id}:-10"),
                InlineKeyboardButton(text="⏳ +10 мин", callback_data=f"rem_shift:{reminder_id}:10"),
                InlineKeyboardButton(text="⏳ +1 час", callback_data=f"rem_shift:{reminder_id}:60"),
            ],
            [
                InlineKeyboardButton(text="🗑 Отменить", callback_data=f"rem_cancel:{reminder_id}"),
                InlineKeyboardButton(text="📋 Все активные", callback_data="list:refresh"),
            ],
        ]
    )


def fired_kb(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выполнено", callback_data=f"rem_done:{reminder_id}"),
                InlineKeyboardButton(text="⏳ +10 мин", callback_data=f"rem_snooze:{reminder_id}:10"),
            ],
            [
                InlineKeyboardButton(text="⏳ +30 мин", callback_data=f"rem_snooze:{reminder_id}:30"),
                InlineKeyboardButton(text="⏳ +1 час", callback_data=f"rem_snooze:{reminder_id}:60"),
            ],
            [
                InlineKeyboardButton(text="🌅 Завтра в 09:00", callback_data=f"rem_tomorrow:{reminder_id}"),
            ],
        ]
    )


def list_kb(reminders: Sequence[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for index, reminder in enumerate(reminders, start=1):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить #{index}",
                    callback_data=f"rem_cancel:{int(reminder['id'])}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="list:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_kb(items: Sequence[Dict[str, Any]]) -> Optional[InlineKeyboardMarkup]:
    rows: List[List[InlineKeyboardButton]] = []
    for index, item in enumerate(items[: config.SHOP_BUTTON_LIMIT], start=1):
        pending = bool(item.get("finance_batch_id") and not item.get("finance_exported_at"))
        mark = "⏳" if pending else ("✅" if item.get("is_bought") else "⬜️")
        item_id = int(item["id"])
        name = shorten(str(item.get("item_text") or "Покупка"), 26)
        price_cents = int(item.get("price_cents") or 0)
        price_text = money.format_money_plain(price_cents) if price_cents else "+"
        price_mark = "⏳" if pending else "💰"
        rows.append(
            [
                InlineKeyboardButton(
                    text=shorten(f"{mark} {index}. {name}", 40),
                    callback_data=f"shop_tg:{item_id}",
                ),
                InlineKeyboardButton(
                    text=f"{price_mark} {price_text}",
                    callback_data=f"shop_price:{item_id}",
                ),
            ]
        )
    if not rows:
        return None

    pending_total = sum(
        int(item.get("price_cents") or 0)
        for item in items
        if item.get("finance_batch_id")
        and not item.get("finance_exported_at")
        and int(item.get("price_cents") or 0) > 0
    )
    ready_total = sum(
        int(item.get("price_cents") or 0)
        for item in items
        if item.get("is_bought")
        and int(item.get("price_cents") or 0) > 0
        and not item.get("finance_exported_at")
        and not item.get("finance_batch_id")
    )
    if pending_total > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📤 Повторить передачу ({money.format_money_plain(pending_total)})",
                    callback_data="shop_finance",
                )
            ]
        )
    elif ready_total > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📤 В Финансы ({money.format_money_plain(ready_total)})",
                    callback_data="shop_finance",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🧹 Очистить купленное", callback_data="shop_clr")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------
def welcome_text(user: Optional[Dict[str, Any]]) -> str:
    if user:
        label = user_label(user)
        return (
            f"⏰ <b>{config.APP_NAME}</b> <i>v{config.APP_VERSION}</i>\n"
            f"Профиль: <b>{esc(label)}</b>\n\n"
            "Пиши или диктуй задачи как есть:\n"
            "• <code>в 9:40 позвонить</code>\n"
            "• <code>Стоматолог в 10 утра послезавтра</code>\n"
            "• <code>Напомни папе сегодня в 19:00 забрать ключи</code>\n"
            "• <code>через 20 минут выключить духовку</code>\n\n"
            "Можно отправить фото записки или голосовое — я разберу.\n"
            "🛒 В списке покупок можно ставить флажки, указывать цены и передавать расходы в Бот Финансов."
        )
    return (
        f"👋 <b>Привет! Это {config.APP_NAME}.</b>\n\n"
        "Я помогаю всей семье не забывать о важном.\n"
        "👇 Нажми на кнопку со своим именем, чтобы подключиться:"
    )


def help_text(user: Optional[Dict[str, Any]]) -> str:
    profile = f"\n\nТвой профиль: <b>{esc(user_label(user))}</b>" if user else ""
    return (
        f"💡 <b>{config.APP_NAME} — примеры и помощь</b>{profile}\n\n"
        "<b>Время:</b>\n"
        "• <code>в 9:40</code>, <code>в 09.40</code>, <code>в 18:30</code>\n"
        "• <code>в 10 утра</code>, <code>в 7 вечера</code>, <code>в 2 часа дня</code>\n"
        "• <code>через 5 минут</code>, <code>через полчаса</code>, <code>через полтора часа</code>\n"
        "• <code>сегодня</code>, <code>завтра</code>, <code>послезавтра</code>, <code>в понедельник</code>\n"
        "• <code>29.10</code>, <code>29 октября</code>\n\n"
        "<b>Адресат:</b>\n"
        "• <code>напомни папе ...</code>, <code>напомни маме ...</code>\n"
        "• <code>напомни Вере ...</code>, <code>напомни Верочке ...</code>\n"
        "• <code>напомни всем ...</code> — всей семье\n\n"
        "<b>Списки покупок:</b>\n"
        "• <code>Купи молоко и хлеб</code> — добавит два пункта\n"
        "• <code>в покупки: сыр, кофе</code>\n"
        "• Голосом: «Купи молоко и хлеб» или «Молоко, хлеб, сыр»\n"
        "• Нажми на позицию — поставить/снять флажок «в корзине»\n"
        "• Нажми <b>💰</b> — указать стоимость товара\n"
        "• <b>📤 В Финансы</b> — передать отмеченные товары с ценой\n\n"
        "Если время не указано, я не угадываю: просто напиши его в сообщении."
    )


def roles_text(connected: Optional[Iterable[str]] = None) -> str:
    connected_set = set(connected or ())
    lines = [
        "👨‍👩‍👧‍👦 <b>Семья и роли</b>",
        "",
    ]
    for key, role in config.PREDEFINED_ROLES.items():
        status = "🟢 Подключен(а)" if key in connected_set else "⚪️ Еще не заходил(а)"
        lines.append(f"{role['emoji']} <b>{esc(role['label'])}</b> — {status}")
    lines.extend(
        [
            "",
            "👇 <i>Выбери свою роль. Напоминания, адресованные роли, привяжутся автоматически.</i>",
        ]
    )
    return "\n".join(lines)


def clarify_text() -> str:
    return (
        "🤔 <b>Не понял время или дату.</b>\n\n"
        "Напиши конкретнее, например:\n"
        "• <code>в 9:40 позвонить</code>\n"
        "• <code>завтра в 10:00 стоматолог</code>\n"
        "• <code>через 20 минут выключить духовку</code>\n"
        "• <code>напомни папе сегодня в 19:00 забрать ключи</code>"
    )


def creation_card(
    task_text: str,
    target_label: str,
    scheduled_at: Any,
    warning: Optional[str] = None,
) -> str:
    lines = [
        "✅ <b>Напоминание записано!</b>",
        f"📌 Задача: <b>{esc(task_text)}</b>",
        f"👤 Кому: {esc(target_label)}",
        f"🕒 Когда: {esc(format_human_dt(scheduled_at))} "
        f"(<i>{esc(format_delta_hint(scheduled_at))}</i>)",
    ]
    if warning:
        lines.extend(["", warning])
    return "\n".join(lines)


def fired_card(
    task_text: str,
    scheduled_at: Any,
    from_label: Optional[str] = None,
) -> str:
    lines = [
        "🔔 <b>НАПОМИНАНИЕ!</b>",
        f"📌 <b>{esc(task_text)}</b>",
        f"🕒 {esc(format_human_dt(scheduled_at))}",
    ]
    if from_label:
        lines.append(f"👤 <i>От: {esc(from_label)}</i>")
    return "\n".join(lines)


def retry_card(
    task_text: str,
    scheduled_at: Any,
    attempt: int,
    max_attempts: int,
    from_label: Optional[str] = None,
) -> str:
    lines = [
        f"🔔 <b>Повторное напоминание ({attempt}/{max_attempts})!</b>",
        f"Не забудь: <b>{esc(task_text)}</b>",
        f"🕒 {esc(format_human_dt(scheduled_at))}",
    ]
    if from_label:
        lines.append(f"👤 <i>От: {esc(from_label)}</i>")
    return "\n".join(lines)


def planned_for_you_text(
    creator: Dict[str, Any],
    task_text: str,
    scheduled_at: Any,
    for_all: bool = False,
) -> str:
    who = user_label(creator)
    verb = verb_for_user(creator, "запланировал", "запланировала")
    audience = "для всей семьи" if for_all else "для тебя"
    return (
        f"📨 <b>{esc(who)}</b> {verb} {audience} напоминание "
        f"на <b>{esc(format_human_dt(scheduled_at))}</b>:\n"
        f"📌 {esc(task_text)}"
    )


def completed_by_text(actor: Optional[Dict[str, Any]], task_text: str) -> str:
    who = user_label(actor)
    verb = verb_for_user(actor, "выполнил", "выполнила")
    return (
        f"👍 <b>{esc(who)}</b> {verb} твоё напоминание:\n"
        f"✔️ <s>{esc(task_text)}</s>"
    )


def postponed_card(task_text: str, scheduled_at: Any) -> str:
    return (
        "⏳ <b>Отложено</b>\n"
        f"📌 {esc(task_text)}\n"
        f"🕒 {esc(format_human_dt(scheduled_at))} "
        f"(<i>{esc(format_delta_hint(scheduled_at))}</i>)"
    )


def cancelled_card(task_text: str) -> str:
    return f"🗑 <b>Отменено</b>\n<s>{esc(task_text)}</s>"


def completed_card(task_text: str) -> str:
    return f"✅ <b>Выполнено:</b>\n<s>{esc(task_text)}</s>"


def active_list_text(reminders: Sequence[Dict[str, Any]]) -> str:
    if not reminders:
        return "📭 <b>Активных напоминаний нет.</b>"
    lines = [f"📋 <b>Активные напоминания</b> · {len(reminders)}", ""]
    for index, reminder in enumerate(reminders, start=1):
        target = reminder_target_label(reminder)
        status = status_label(str(reminder.get("status") or ""))
        lines.append(f"<b>{index}. {esc(reminder.get('parsed_text') or '')}</b>")
        lines.append(
            f"🕒 {esc(format_human_dt(reminder.get('scheduled_at')))} · "
            f"👤 {esc(target)} · {status}"
        )
        lines.append("")
    return "\n".join(lines).strip()


def shop_view(items: Sequence[Dict[str, Any]]) -> str:
    if not items:
        return (
            "🛒 <b>Список покупок пуст.</b>\n"
            "Напиши: <code>Купи молоко и хлеб</code>"
        )

    limit = config.SHOP_DISPLAY_LIMIT
    visible = list(items[:limit])
    lines = [f"🛒 <b>Список покупок</b> · {len(items)}", ""]

    list_total = sum(int(item.get("price_cents") or 0) for item in items)
    cart_total = sum(
        int(item.get("price_cents") or 0)
        for item in items
        if item.get("is_bought")
    )
    priced_count = sum(1 for item in items if int(item.get("price_cents") or 0) > 0)
    bought_count = sum(1 for item in items if item.get("is_bought"))
    exported_total = sum(
        int(item.get("price_cents") or 0)
        for item in items
        if item.get("finance_exported_at")
    )
    pending_total = sum(
        int(item.get("price_cents") or 0)
        for item in items
        if item.get("finance_batch_id") and not item.get("finance_exported_at")
    )
    unchecked_total = sum(
        int(item.get("price_cents") or 0)
        for item in items
        if not item.get("is_bought")
    )

    for index, item in enumerate(visible, start=1):
        text = esc(item.get("item_text") or "Покупка")
        pending = bool(item.get("finance_batch_id") and not item.get("finance_exported_at"))
        if pending:
            mark = "⏳"
        elif item.get("is_bought"):
            mark = "✅"
        else:
            mark = "⬜️"
        name = f"<s>{text}</s>" if item.get("is_bought") else f"<b>{text}</b>"
        price_cents = int(item.get("price_cents") or 0)
        price = money.format_money(price_cents) if price_cents else "<i>цена не указана</i>"
        if item.get("finance_exported_at"):
            state_mark = " · 📤"
        elif pending:
            state_mark = " · ⏳ передача"
        else:
            state_mark = ""
        lines.append(f"{index}. {mark} {name} — {price}{state_mark}")

    if len(items) > limit:
        lines.append(
            f"… и ещё {len(items) - limit} поз. "
            f"(показаны первые {limit})"
        )

    lines.extend(["", "━━━━━━━━━━━━"])
    if priced_count:
        lines.append(
            f"💰 <b>Сумма всех цен:</b> {money.format_money(list_total)} "
            f"<i>(по {priced_count} из {len(items)} поз.)</i>"
        )
        lines.append(f"🧺 <b>В корзине:</b> {money.format_money(cart_total)}")
        if pending_total:
            lines.append(f"⏳ <b>В передаче:</b> {money.format_money(pending_total)}")
        if exported_total:
            lines.append(f"📤 <b>Передано в Финансы:</b> {money.format_money(exported_total)}")
        if unchecked_total:
            lines.append(
                f"⚪️ <b>Не в корзине:</b> {money.format_money(unchecked_total)} "
                "<i>(в Финансы не уйдёт)</i>"
            )
    else:
        lines.append("💰 Цены пока не указаны. Нажми <b>💰</b> напротив позиции.")

    lines.append(f"🧺 Отмечено в корзине: {bought_count} из {len(items)}")
    return "\n".join(lines)


def price_prompt_text(item: Dict[str, Any]) -> str:
    name = esc(item.get("item_text") or "Покупка")
    current = int(item.get("price_cents") or 0)
    current_line = (
        f"\nСейчас: <b>{money.format_money(current)}</b>"
        if current
        else "\nСейчас цена не указана."
    )
    return (
        f"💰 <b>Стоимость позиции</b>\n"
        f"📌 {name}{current_line}\n\n"
        f"Введи цену в {config.CURRENCY or 'BYN'} одним сообщением.\n"
        "Например: <code>2,50</code> или <code>2.50</code>.\n"
        "Чтобы убрать цену — введи <code>0</code>.\n\n"
        "Отмена — /cancel или кнопка ниже."
    )


def price_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="shop_price_cancel")]
        ]
    )


def price_saved_text(item: Dict[str, Any]) -> str:
    name = esc(item.get("item_text") or "Покупка")
    price_cents = int(item.get("price_cents") or 0)
    if price_cents:
        return f"✅ Цена «{name}»: <b>{money.format_money(price_cents)}</b>"
    return f"✅ Цена «{name}» убрана."


def price_invalid_text() -> str:
    return (
        "🤔 Не понял цену.\n"
        "Введи число, например <code>2,50</code> или <code>12</code>.\n"
        "Для отмены — /cancel."
    )


def finance_result_text(
    count: int,
    total_cents: int,
    *,
    duplicate: bool = False,
    skipped_unpriced: int = 0,
    retried_pending: bool = False,
    remaining_ready: int = 0,
) -> str:
    if duplicate:
        prefix = "ℹ️ Эти расходы уже были переданы ранее."
    elif retried_pending:
        prefix = "✅ Ранее начатая передача завершена."
    else:
        prefix = "✅ Расходы переданы в Бот Финансов."
    lines = [
        prefix,
        f"🧾 Позиций: <b>{count}</b>",
        f"💰 Сумма: <b>{money.format_money(total_cents)}</b>",
    ]
    if skipped_unpriced:
        lines.append(
            f"⚠️ Без цены пропущено: <b>{skipped_unpriced}</b>. "
            "Укажи цену и нажми «📤 В Финансы» ещё раз."
        )
    if remaining_ready:
        lines.append(
            f"📦 Осталось новых позиций: <b>{remaining_ready}</b>. "
            "Нажми «📤 В Финансы» ещё раз."
        )
    return "\n".join(lines)
