"""Конфигурация семейного Telegram-бота «Семейный ИИ-Напоминатор».

Модуль отвечает только за:
* чтение `.env` (сначала `/app/.env`, затем локальный `.env`);
* переменные окружения и безопасные значения по умолчанию;
* часовой пояс Минска (UTC+3);
* предустановленные семейные роли и их синонимы;
* русские названия дней недели/месяцев и константы планировщика.

Никаких сетевых вызовов и обращений к базе данных здесь нет.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

try:  # На slim-образах zoneinfo может не найти базу часовых поясов.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - зависит от сборки Python
    ZoneInfo = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    # Комментарий в конце строки: KEY=value  # comment
    value = re.split(r"\s+#", value, maxsplit=1)[0]
    return value.strip()


def _load_dotenv_file(path: Path) -> None:
    """Загружает переменные из файла, не перетирая уже заданное окружение."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    except OSError as exc:
        logging.getLogger(__name__).warning("Не удалось прочитать %s: %s", path, exc)
        return

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = _unquote_env_value(value)
        # Приоритет у реального окружения контейнера.
        os.environ.setdefault(key, value)


def load_dotenv() -> None:
    candidates = [
        Path("/app/.env"),
        BASE_DIR / ".env",
        Path.cwd() / ".env",
    ]
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        _load_dotenv_file(path)


load_dotenv()


# ---------------------------------------------------------------------------
# Основные настройки
# ---------------------------------------------------------------------------
APP_NAME = "Семейный ИИ-Напоминатор"
APP_VERSION = "3.0"

DEFAULT_TOKEN = "8406940174:AAGDxk-G6NQg_BRCYiErIbUeFqraUIr1eVQ"
DEFAULT_API_KEY = "sk-fc-brUkPKHb4iXRDA7N4mrkVtD1I-DpCQt6-lAJkGGjflA"

TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or DEFAULT_TOKEN).strip()
API_KEY = (os.getenv("OPENAI_API_KEY") or DEFAULT_API_KEY).strip()
BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://freecodex.fun/v1").strip().rstrip("/")
OPENAI_MODELS: Tuple[str, ...] = tuple(
    model.strip()
    for model in (
        os.getenv("OPENAI_MODELS")
        or "gemini-3.8-flash,gemini-3-flash,gemini-3.1-pro"
    ).split(",")
    if model.strip()
) or ("gemini-3.8-flash",)

AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "60") or 60)
AI_ALWAYS = (os.getenv("AI_ALWAYS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}

# --- Список покупок и обмен с «Ботом Финансов» ---
CURRENCY = (os.getenv("CURRENCY") or "BYN").strip().upper()
FINANCE_API_URL = (os.getenv("FINANCE_API_URL") or "").strip()
FINANCE_API_TOKEN = (os.getenv("FINANCE_API_TOKEN") or "").strip()
FINANCE_CATEGORY = (os.getenv("FINANCE_CATEGORY") or "Продукты").strip()
FINANCE_API_TIMEOUT = float(os.getenv("FINANCE_API_TIMEOUT", "15") or 15)
SHOP_DISPLAY_LIMIT = min(45, max(5, int(os.getenv("SHOP_DISPLAY_LIMIT", "45") or 45)))
SHOP_BUTTON_LIMIT = min(45, max(5, int(os.getenv("SHOP_BUTTON_LIMIT", "45") or 45)))

# В Docker рабочая директория /app, база лежит на volume /app/data.
_default_db = "/app/data/reminders.db" if Path("/app").is_dir() else str(BASE_DIR / "data" / "reminders.db")
DB_PATH = Path(os.getenv("DB_PATH", _default_db)).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
DB_PATH = DB_PATH.resolve()

RETRY_INTERVAL_MINUTES = max(1, int(os.getenv("REMINDER_RETRY_MINUTES", "10") or 10))
MAX_RETRY_COUNT = max(1, int(os.getenv("REMINDER_MAX_RETRIES", "3") or 3))
SCHEDULER_TICK_SECONDS = max(3, int(os.getenv("SCHEDULER_TICK_SECONDS", "10") or 10))
TASK_MAX_LENGTH = max(100, int(os.getenv("TASK_MAX_LENGTH", "700") or 700))

LOG_LEVEL = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # Не логируем тело запросов к LLM и токены.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Время (Минск, UTC+3, без перехода на летнее время с 2011 года)
# ---------------------------------------------------------------------------
def _make_minsk_tz() -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Europe/Minsk")  # type: ignore[return-value]
        except Exception as exc:  # ZoneInfoNotFoundError и любые проблемы tzdata
            logging.getLogger(__name__).debug("ZoneInfo Europe/Minsk недоступен: %s", exc)
    return timezone(timedelta(hours=3), name="Europe/Minsk")


MINSK_TZ = _make_minsk_tz()
DB_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_minsk() -> datetime:
    """Текущее локальное время Минска как naive datetime (для SQLite)."""
    return datetime.now(MINSK_TZ).replace(tzinfo=None)


# Совместимость со старым именем.
get_now_minsk = now_minsk


def format_db_dt(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(DB_DT_FORMAT)


def parse_db_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), DB_DT_FORMAT)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(MINSK_TZ).replace(tzinfo=None)
        return parsed.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Семейные роли
# ---------------------------------------------------------------------------
# ВАЖНО: порядок словаря используется в интерфейсе.
PREDEFINED_ROLES: Dict[str, dict] = {
    "pasha": {
        "name": "Паша",
        "label": "Паша (Сын)",
        "emoji": "🙋‍♂️",
        "gender": "m",
        "synonyms": (
            "паша", "паши", "паше", "пашу", "пашей",
            "павел", "павла", "павлу", "павлом", "павле",
            "сын", "сына", "сыну", "сыном", "сыне",
            "брат", "брата", "брату", "братом", "брате",
            "парень", "парня", "парню", "парнем",
        ),
    },
    "denis": {
        "name": "Денис",
        "label": "Денис (Папа)",
        "emoji": "👨",
        "gender": "m",
        "synonyms": (
            "денис", "дениса", "денису", "денисом", "денисе",
            "папа", "папы", "папе", "папу", "папой", "папе",
            "отец", "отца", "отцу", "отцом", "отце",
            "батя", "бати", "бате", "батю", "батей",
        ),
    },
    "yulya": {
        "name": "Юля",
        "label": "Юля (Мама)",
        "emoji": "👩",
        "gender": "f",
        "synonyms": (
            "юля", "юли", "юле", "юлю", "юлей",
            "мама", "мамы", "маме", "маму", "мамой",
            "мамуля", "мамули", "мамуле", "мамулю", "мамулей",
            "мать", "матери", "матерью",
        ),
    },
    "vera": {
        "name": "Вера",
        "label": "Вера (Дочь / Сестра)",
        "emoji": "👧",
        "gender": "f",
        "synonyms": (
            "вера", "веры", "вере", "веру", "верой",
            "дочь", "дочери", "дочерью", "дочка", "дочки", "дочке", "дочку", "дочкой",
            "сестра", "сестры", "сестре", "сестру", "сестрой",
            "сестренка", "сестренки", "сестренке", "сестренку", "сестренкой",
            "мелкая", "мелкой", "мелкую",
            "малая", "малой", "малую",
        ),
    },
    "verochka": {
        "name": "Верочка",
        "label": "Верочка (Девушка)",
        "emoji": "❤️",
        "gender": "f",
        "synonyms": (
            "верочка", "верочки", "верочке", "верочку", "верочкой",
            "девушка", "девушки", "девушке", "девушку", "девушкой",
            "веста", "весты", "весте", "весту", "вестой",
            "любимая", "любимой", "любимую",
            "зайка", "зайки", "зайке", "зайку", "зайкой",
        ),
    },
}

SELF_ROLE_KEY = "self"
ALL_ROLE_KEY = "all"
SELF_LABEL = "🙋 Лично (себе)"
ALL_LABEL = "👨‍👩‍👧‍👦 Всей семье"

ALL_SYNONYMS: Tuple[str, ...] = (
    "всем", "всех", "всей семье", "всей семьей", "всей семьёй",
    "для всех", "для всей семьи", "семье", "семьей", "семьёй",
)
SELF_SYNONYMS: Tuple[str, ...] = ("мне", "себе", "себя")

SYNONYM_TO_ROLE: Dict[str, str] = {}
for _role_key, _role in PREDEFINED_ROLES.items():
    for _synonym in _role["synonyms"]:
        SYNONYM_TO_ROLE[_synonym.lower()] = _role_key
for _synonym in ALL_SYNONYMS:
    SYNONYM_TO_ROLE[_synonym.lower()] = ALL_ROLE_KEY
for _synonym in SELF_SYNONYMS:
    SYNONYM_TO_ROLE[_synonym.lower()] = SELF_ROLE_KEY

ROLE_KEYS: Tuple[str, ...] = tuple(PREDEFINED_ROLES.keys())


def full_roles_description() -> str:
    lines = []
    for key, role in PREDEFINED_ROLES.items():
        lines.append(f"- '{key}': {role['label']} — {', '.join(role['synonyms'])}")
    lines.append(f"- '{SELF_ROLE_KEY}': напоминание самому себе (по умолчанию)")
    lines.append(f"- '{ALL_ROLE_KEY}': напоминание всей семье (всем)")
    return "\n".join(lines)


def role_label(role_key: str) -> str:
    if role_key == ALL_ROLE_KEY:
        return ALL_LABEL
    if role_key == SELF_ROLE_KEY:
        return SELF_LABEL
    role = PREDEFINED_ROLES.get(role_key)
    if role:
        return f"{role['emoji']} {role['label']}"
    return SELF_LABEL


def role_gender(role_key: str) -> Optional[str]:
    role = PREDEFINED_ROLES.get(role_key or "")
    return role.get("gender") if role else None


# ---------------------------------------------------------------------------
# Даты и дни недели
# ---------------------------------------------------------------------------
WEEKDAYS_RU_SHORT: Tuple[str, ...] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEKDAYS_RU_FULL: Tuple[str, ...] = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)
WEEKDAY_FORMS: Dict[str, int] = {
    "понедельник": 0, "понедельника": 0, "понедельнику": 0, "понедельником": 0, "понедельнике": 0,
    "вторник": 1, "вторника": 1, "вторнику": 1, "вторником": 1, "вторнике": 1,
    "среда": 2, "среды": 2, "среде": 2, "среду": 2, "средой": 2,
    "четверг": 3, "четверга": 3, "четвергу": 3, "четвергом": 3, "четверге": 3,
    "пятница": 4, "пятницы": 4, "пятнице": 4, "пятницу": 4, "пятницей": 4,
    "суббота": 5, "субботы": 5, "субботе": 5, "субботу": 5, "субботой": 5,
    "воскресенье": 6, "воскресенья": 6, "воскресенью": 6, "воскресеньем": 6,
}
WEEKDAY_ALTERNATION = "|".join(sorted((re.escape(k) for k in WEEKDAY_FORMS), key=len, reverse=True))

MONTHS_RU: Dict[str, int] = {
    "января": 1, "январь": 1, "январе": 1,
    "февраля": 2, "февраль": 2, "феврале": 2,
    "марта": 3, "март": 3, "марте": 3,
    "апреля": 4, "апрель": 4, "апреле": 4,
    "мая": 5, "май": 5, "мае": 5,
    "июня": 6, "июнь": 6, "июне": 6,
    "июля": 7, "июль": 7, "июле": 7,
    "августа": 8, "август": 8, "августе": 8,
    "сентября": 9, "сентябрь": 9, "сентябре": 9,
    "октября": 10, "октябрь": 10, "октябре": 10,
    "ноября": 11, "ноябрь": 11, "ноябре": 11,
    "декабря": 12, "декабрь": 12, "декабре": 12,
}
MONTH_ALTERNATION = "|".join(sorted((re.escape(k) for k in MONTHS_RU), key=len, reverse=True))

STATUS_LABELS: Dict[str, str] = {
    "PENDING": "⏳ Ожидает",
    "SENT": "🔔 Отправлено",
    "COMPLETED": "✅ Выполнено",
    "EXPIRED": "⌛️ Истекло",
    "CANCELLED": "🗑 Отменено",
}
ACTIVE_STATUSES: Tuple[str, ...] = ("PENDING", "SENT")
