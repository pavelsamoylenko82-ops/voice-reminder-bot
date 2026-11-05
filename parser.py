"""Детерминированный парсер русского времени и текста задачи.

Парсер намеренно не делает сетевых вызовов — его можно тестировать без
Telegram, базы и LLM. Он решает две задачи:

1. Пре-парсер: вытащить точное время, дату, получателя и очистить задачу.
2. Пост-валидатор: если в исходном тексте был явный шаблон `в ЧЧ:ММ` или
   `через X минут`, именно локальное время побеждает ответ LLM.

Ключевой принцип: никаких «кулдаунов +1 час». Если сейчас 09:38:20, а
пользователь пишет «в 9:40», напоминание ставится на сегодня 09:40:00.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import config

BOUND_L = r"(?<![\w-])"
BOUND_R = r"(?![\w-])"


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedReminder:
    target_role_key: str
    scheduled_at: datetime
    task_text: str
    source: str = "local"
    explicit_time: bool = False


@dataclass
class LocalParse:
    scheduled_at: Optional[datetime] = None
    task_text: str = ""
    target_role_key: str = config.SELF_ROLE_KEY
    target_explicit: bool = False
    has_explicit_time: bool = False
    has_explicit_day: bool = False
    confidence: str = "none"
    notes: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Словари и общие регулярки
# ---------------------------------------------------------------------------
def _alternation(values: Iterable[str]) -> str:
    return "|".join(sorted({re.escape(value) for value in values}, key=len, reverse=True))


_TARGET_ALTERNATION = _alternation(config.SYNONYM_TO_ROLE.keys())

_VERB_PATTERN = (
    r"(?:напомни(?:ть)?|напоминание|напоминания|"
    r"поставь(?:те)?(?:\s+напоминание)?|"
    r"запланируй(?:те)?(?:\s+напоминание)?|"
    r"создай(?:те)?(?:\s+напоминание)?)"
)

_TARGET_AFTER_VERB_RE = re.compile(
    rf"{BOUND_L}{_VERB_PATTERN}\s*,?\s*(?:пожалуйста\s*,?\s*)?(?:для\s+)?"
    rf"({_TARGET_ALTERNATION}){BOUND_R}",
    re.IGNORECASE,
)
_TARGET_AT_START_RE = re.compile(
    rf"^\s*(?:для\s+)?({_TARGET_ALTERNATION}){BOUND_R}\s*[,:]?\s*",
    re.IGNORECASE,
)

_REMINDER_WORD_RE = re.compile(
    rf"{BOUND_L}(?:напомни(?:ть)?|напоминание|напоминания|напоминанием|"
    rf"пожалуйста|плиз|будь\s+добр(?:а)?|не\s+забудь(?:те)?){BOUND_R}",
    re.IGNORECASE,
)
_SETUP_PHRASE_RE = re.compile(
    rf"{BOUND_L}(?:поставь(?:те)?|создай(?:те)?|запланируй(?:те)?|сделай(?:те)?)"
    rf"\s+(?:напоминание|задачу|напоминание){BOUND_R}",
    re.IGNORECASE,
)

# --- Относительное время ---
_REL_DECIMAL_HOURS_RE = re.compile(
    rf"{BOUND_L}через\s+(\d+)[.,](\d+)\s*(?:час(?:а|ов)?|ч){BOUND_R}", re.IGNORECASE
)
_REL_HOURS_MINUTES_RE = re.compile(
    rf"{BOUND_L}через\s+(\d+)\s*(?:час(?:а|ов)?|ч)\s*(?:и\s*)?"
    rf"(\d+)\s*(?:минут(?:у|ы)?|мин){BOUND_R}",
    re.IGNORECASE,
)
_REL_HALF_HOUR_RE = re.compile(
    rf"{BOUND_L}через\s+пол(?:\s*часа|часа){BOUND_R}", re.IGNORECASE
)
_REL_ONE_HALF_HOUR_RE = re.compile(
    rf"{BOUND_L}через\s+(?:полтора|полторы)\s+час(?:а|ов)?{BOUND_R}", re.IGNORECASE
)
_REL_HOUR_RE = re.compile(
    rf"{BOUND_L}через\s+(?:один\s+)?час{BOUND_R}", re.IGNORECASE
)
_REL_HOURS_RE = re.compile(
    rf"{BOUND_L}через\s+(\d+)\s*(?:час(?:а|ов)?|ч){BOUND_R}", re.IGNORECASE
)
_REL_MINUTES_RE = re.compile(
    rf"{BOUND_L}через\s+(\d+)\s*(?:минут(?:у|ы)?|мин\.?){BOUND_R}", re.IGNORECASE
)
_REL_SECONDS_RE = re.compile(
    rf"{BOUND_L}через\s+(\d+)\s*(?:секунд(?:у|ы)?|сек\.?){BOUND_R}", re.IGNORECASE
)
_REL_PAIR_MINUTES_RE = re.compile(
    rf"{BOUND_L}через\s+пару\s+минут{BOUND_R}", re.IGNORECASE
)

_REL_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("decimal_hours", _REL_DECIMAL_HOURS_RE),
    ("hours_minutes", _REL_HOURS_MINUTES_RE),
    ("half_hour", _REL_HALF_HOUR_RE),
    ("one_half_hours", _REL_ONE_HALF_HOUR_RE),
    ("hour", _REL_HOUR_RE),
    ("hours", _REL_HOURS_RE),
    ("minutes", _REL_MINUTES_RE),
    ("seconds", _REL_SECONDS_RE),
    ("pair_minutes", _REL_PAIR_MINUTES_RE),
)

# --- Абсолютное время ---
_DAYPART_WORD = r"(утра|вечера|дня|ночи)"

_ABS_HM_RE = re.compile(
    rf"{BOUND_L}(?:в|во|к)\s+(\d{{1,2}})\s*[:.]\s*(\d{{2}})"
    rf"(?:\s*{_DAYPART_WORD})?{BOUND_R}",
    re.IGNORECASE,
)
_ABS_HOUR_MINUTES_DAYPART_RE = re.compile(
    rf"{BOUND_L}(?:в|во|к)\s+(\d{{1,2}})\s*час(?:а|ов)?\s+"
    rf"(\d{{1,2}})\s*минут(?:у|ы)?\s*({_DAYPART_WORD}){BOUND_R}",
    re.IGNORECASE,
)
_ABS_HOUR_MINUTES_CHAS_RE = re.compile(
    rf"{BOUND_L}(?:в|во|к)\s+(\d{{1,2}})\s*час(?:а|ов)?\s+"
    rf"(\d{{1,2}})\s*минут(?:у|ы)?{BOUND_R}",
    re.IGNORECASE,
)
_ABS_HOUR_DAYPART_RE = re.compile(
    rf"{BOUND_L}(?:в|во|к)\s+(\d{{1,2}})\s*(?:час(?:а|ов)?\s*)?"
    rf"({_DAYPART_WORD}){BOUND_R}",
    re.IGNORECASE,
)
_ABS_HOUR_CHAS_RE = re.compile(
    rf"{BOUND_L}(?:в|во|к)\s+(\d{{1,2}})\s*час(?:а|ов)?{BOUND_R}",
    re.IGNORECASE,
)
# «завтра в 10», «сегодня к 19» — час без минут. Исключаем «в 10 раз», «в 2 года».
_ABS_HOUR_BARE_RE = re.compile(
    rf"{BOUND_L}(?:в|во|к)\s+(\d{{1,2}})(?![\d:.,])"
    rf"(?!\s*(?:раз|раза|руб|рублей|процент|%|год|года|лет|месяц|месяца|"
    rf"месяцев|недел|день|дня|дней|минут|секунд)){BOUND_R}",
    re.IGNORECASE,
)
_BARE_HM_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")

_TIME_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("hm", _ABS_HM_RE),
    ("hour_minutes_daypart", _ABS_HOUR_MINUTES_DAYPART_RE),
    ("hour_minutes", _ABS_HOUR_MINUTES_CHAS_RE),
    ("hour_daypart", _ABS_HOUR_DAYPART_RE),
    ("hour", _ABS_HOUR_CHAS_RE),
    ("bare_hm", _BARE_HM_RE),
    ("bare_hour", _ABS_HOUR_BARE_RE),
)

# --- Дни, дни недели, даты ---
_DAY_RE = re.compile(
    rf"{BOUND_L}(?:(?:на|к|до|со)\s+)?(послезавтра|завтра|сегодня){BOUND_R}",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    rf"{BOUND_L}(?:(?:в|во|на|к|до)\s+)?"
    rf"(?:(?:следующий|следующую|след\.?)\s+)?"
    rf"({config.WEEKDAY_ALTERNATION}){BOUND_R}",
    re.IGNORECASE,
)
_DATE_NUM_RE = re.compile(
    rf"{BOUND_L}(\d{{1,2}})[./](\d{{1,2}})(?:[./](\d{{2,4}}))?{BOUND_R}"
)
_DATE_MONTH_RE = re.compile(
    rf"{BOUND_L}(?:на\s+|к\s+|до\s+)?(\d{{1,2}})\s+"
    rf"({config.MONTH_ALTERNATION})(?:\s+(\d{{4}}))?{BOUND_R}",
    re.IGNORECASE,
)

_DAYPART_ONLY_RE = re.compile(
    rf"{BOUND_L}(утром|днём|днем|вечером|ночью|после\s+обеда|к\s+вечеру|в\s+обед){BOUND_R}",
    re.IGNORECASE,
)
_DAYPART_DEFAULTS: dict[str, Tuple[int, int]] = {
    "утром": (9, 0),
    "днем": (13, 0),
    "вечером": (19, 0),
    "ночью": (22, 0),
    "после обеда": (14, 0),
    "к вечеру": (19, 0),
    "в обед": (13, 0),
}

_DAY_WORDS = {
    "сегодня": "today",
    "завтра": "tomorrow",
    "послезавтра": "day_after",
}

# Частые опечатки, которые встречаются в семейных сообщениях.
_TYPO_MAP = {
    "стомотолог": "стоматолог",
    "стомотолога": "стоматолога",
    "стомотологу": "стоматологу",
    "стомотологом": "стоматологом",
    "стомотологе": "стоматологе",
    "позванить": "позвонить",
    "пазванить": "позвонить",
    "позваню": "позвоню",
    "паликлиника": "поликлиника",
    "поликлиники": "поликлиники",
    "тренировка": "тренировка",
}
_TYPO_RE = re.compile(
    rf"{BOUND_L}({_alternation(_TYPO_MAP.keys())}){BOUND_R}", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Вспомогательные структуры
# ---------------------------------------------------------------------------
Span = Tuple[int, int]


@dataclass(frozen=True)
class _TimeCandidate:
    start: int
    end: int
    hour: int
    minute: int
    kind: str


@dataclass(frozen=True)
class _RelativeCandidate:
    start: int
    end: int
    delta: timedelta
    kind: str


@dataclass(frozen=True)
class _DayInfo:
    kind: str
    spans: Tuple[Span, ...]


@dataclass(frozen=True)
class _WeekdayInfo:
    index: int
    next_week: bool
    spans: Tuple[Span, ...]


@dataclass(frozen=True)
class _DateInfo:
    year: Optional[int]
    month: int
    day: int
    spans: Tuple[Span, ...]


def _overlaps(span: Span, spans: Sequence[Span]) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def _merge_spans(spans: Iterable[Span]) -> List[Span]:
    ordered = sorted((start, end) for start, end in spans if end > start)
    merged: List[Span] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _remove_spans(text: str, spans: Iterable[Span]) -> str:
    merged = _merge_spans(spans)
    if not merged:
        return text
    parts: List[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(text[cursor:start])
        parts.append(" ")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Разбор времени
# ---------------------------------------------------------------------------
def _apply_daypart(hour: int, daypart: Optional[str]) -> int:
    if not daypart:
        return hour
    daypart = daypart.lower()
    if daypart in {"дня"}:
        if hour < 6:
            hour += 12
    elif daypart in {"вечера"}:
        if 1 <= hour < 12:
            hour += 12
    elif daypart in {"ночи"}:
        if hour == 12:
            hour = 0
    elif daypart in {"утра"}:
        if hour == 12:
            hour = 0
    return hour


def _parse_time_match(kind: str, match: re.Match[str]) -> Optional[Tuple[int, int]]:
    try:
        if kind == "hm":
            hour, minute = int(match.group(1)), int(match.group(2))
            daypart = match.group(3) if match.lastindex and match.lastindex >= 3 else None
            hour = _apply_daypart(hour, daypart)
        elif kind == "bare_hm":
            hour, minute = int(match.group(1)), int(match.group(2))
        elif kind == "hour_minutes":
            hour, minute = int(match.group(1)), int(match.group(2))
        elif kind == "hour_minutes_daypart":
            hour, minute = int(match.group(1)), int(match.group(2))
            hour = _apply_daypart(hour, match.group(3))
        elif kind == "hour_daypart":
            hour, minute = int(match.group(1)), 0
            hour = _apply_daypart(hour, match.group(2))
        elif kind == "hour":
            hour, minute = int(match.group(1)), 0
        elif kind == "bare_hour":
            hour, minute = int(match.group(1)), 0
        else:
            return None
    except (TypeError, ValueError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _time_candidates(text: str) -> List[_TimeCandidate]:
    candidates: List[_TimeCandidate] = []
    for order, (kind, pattern) in enumerate(_TIME_PATTERNS):
        for match in pattern.finditer(text):
            parsed = _parse_time_match(kind, match)
            if parsed is None:
                continue
            hour, minute = parsed
            candidates.append(
                _TimeCandidate(match.start(), match.end(), hour, minute, f"{order}:{kind}")
            )
    return candidates


def _relative_candidates(text: str) -> List[_RelativeCandidate]:
    candidates: List[_RelativeCandidate] = []
    for order, (kind, pattern) in enumerate(_REL_PATTERNS):
        for match in pattern.finditer(text):
            try:
                if kind == "decimal_hours":
                    whole = int(match.group(1))
                    fraction_text = match.group(2)
                    fraction = int(fraction_text) / (10 ** len(fraction_text))
                    delta = timedelta(minutes=round((whole + fraction) * 60))
                elif kind == "hours_minutes":
                    delta = timedelta(hours=int(match.group(1)), minutes=int(match.group(2)))
                elif kind == "half_hour":
                    delta = timedelta(minutes=30)
                elif kind == "one_half_hours":
                    delta = timedelta(minutes=90)
                elif kind == "hour":
                    delta = timedelta(hours=1)
                elif kind == "hours":
                    delta = timedelta(hours=int(match.group(1)))
                elif kind == "minutes":
                    delta = timedelta(minutes=int(match.group(1)))
                elif kind == "seconds":
                    delta = timedelta(seconds=int(match.group(1)))
                elif kind == "pair_minutes":
                    delta = timedelta(minutes=2)
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if delta.total_seconds() <= 0:
                continue
            candidates.append(
                _RelativeCandidate(match.start(), match.end(), delta, f"{order}:{kind}")
            )
    return candidates


def _extract_day(text: str) -> Optional[_DayInfo]:
    matches = list(_DAY_RE.finditer(text))
    if not matches:
        return None
    first = matches[0]
    kind = _DAY_WORDS.get(first.group(1).lower())
    if not kind:
        return None
    return _DayInfo(kind=kind, spans=tuple(match.span() for match in matches))


def _extract_weekday(text: str) -> Optional[_WeekdayInfo]:
    matches = list(_WEEKDAY_RE.finditer(text))
    if not matches:
        return None
    first = matches[0]
    index = config.WEEKDAY_FORMS.get(first.group(1).lower())
    if index is None:
        return None
    next_week = bool(re.search(r"след", first.group(0), re.IGNORECASE))
    return _WeekdayInfo(
        index=index,
        next_week=next_week,
        spans=tuple(match.span() for match in matches),
    )


def _make_date(
    now: datetime,
    year_value: Optional[str],
    month: int,
    day: int,
) -> Optional[Tuple[int, int, int]]:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if year_value:
        try:
            year = int(year_value)
        except ValueError:
            return None
        if year < 100:
            year += 2000
    else:
        year = now.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if not year_value and candidate < now.date():
        year += 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
    return candidate.year, candidate.month, candidate.day


def _numeric_date_allowed(
    text: str,
    match: re.Match[str],
    day_value: int,
    year_value: Optional[str],
) -> bool:
    if year_value:
        return True
    raw = match.group(0)
    if "/" in raw or day_value > 12:
        return True
    # «1.5 кг», «0.5 л» — это не дата, а количество.
    tail = text[match.end() : match.end() + 16].lower()
    if re.match(
        r"\s*(?:кг|г|гр|л|литр|мл|км|м|см|мм|руб|₽|%|шт|раз|года|лет|час|мин|сек)\b",
        tail,
    ):
        return False
    return True


def _extract_date(text: str, excluded_spans: Sequence[Span], now: datetime) -> Optional[_DateInfo]:
    candidates: List[_DateInfo] = []

    for match in _DATE_MONTH_RE.finditer(text):
        if _overlaps(match.span(), excluded_spans):
            continue
        month = config.MONTHS_RU.get(match.group(2).lower())
        if not month:
            continue
        parsed = _make_date(now, match.group(3), month, int(match.group(1)))
        if parsed is None:
            continue
        candidates.append(_DateInfo(*parsed, spans=(match.span(),)))

    for match in _DATE_NUM_RE.finditer(text):
        if _overlaps(match.span(), excluded_spans):
            continue
        day_value, month_value = int(match.group(1)), int(match.group(2))
        year_value = match.group(3)
        if not _numeric_date_allowed(text, match, day_value, year_value):
            continue
        parsed = _make_date(now, year_value, month_value, day_value)
        if parsed is None:
            continue
        candidates.append(_DateInfo(*parsed, spans=(match.span(),)))

    if not candidates:
        return None
    return min(candidates, key=lambda item: item.spans[0][0])


def _extract_daypart_only(
    text: str, excluded_spans: Sequence[Span]
) -> Optional[Tuple[int, int, Span]]:
    for match in _DAYPART_ONLY_RE.finditer(text):
        if _overlaps(match.span(), excluded_spans):
            continue
        key = re.sub(r"\s+", " ", match.group(1).lower()).replace("днём", "днем")
        default = _DAYPART_DEFAULTS.get(key)
        if default:
            return default[0], default[1], match.span()
    return None


# ---------------------------------------------------------------------------
# Расчёт даты
# ---------------------------------------------------------------------------
def _resolve_datetime(
    now: datetime,
    hour: int,
    minute: int,
    day_kind: Optional[str],
    weekday: Optional[_WeekdayInfo],
    date_info: Optional[_DateInfo],
) -> datetime:
    if date_info is not None:
        return datetime(
            year=date_info.year or now.year,
            month=date_info.month,
            day=date_info.day,
            hour=hour,
            minute=minute,
        )

    if weekday is not None:
        days_ahead = (weekday.index - now.weekday()) % 7
        base_date = now.date() + timedelta(days=days_ahead)
        candidate = datetime.combine(base_date, time(hour, minute))
        if days_ahead == 0 and (weekday.next_week or candidate <= now):
            candidate += timedelta(days=7)
        return candidate

    base_date = now.date()
    if day_kind == "tomorrow":
        base_date += timedelta(days=1)
    elif day_kind == "day_after":
        base_date += timedelta(days=2)

    candidate = datetime.combine(base_date, time(hour, minute))
    # Ключевое правило: перенос на завтра только если время уже прошло
    # и пользователь НЕ сказал «сегодня».
    if day_kind is None and candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Очистка текста
# ---------------------------------------------------------------------------
def _fix_typos(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        fixed = _TYPO_MAP.get(word.lower(), word)
        if word[:1].isupper():
            fixed = fixed[:1].upper() + fixed[1:]
        return fixed

    return _TYPO_RE.sub(replace, text)


_PREPOSITION = r"(?:в|во|на|о|об|обо|про|для|к|ко|с|со)"


def _clean_task_text(raw: str) -> str:
    text = _fix_typos(raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" \t\n,.;:!?—–-")
    text = re.sub(r"^(?:мне|пожалуйста|плиз|и|а)\b[\s,;:.-]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:мне|пожалуйста|плиз|и|а)\b[\s,;:.-]*", "", text, flags=re.IGNORECASE)

    # «в про звонок» -> «про звонок», «на в …» -> «в …».
    for _ in range(4):
        updated = re.sub(
            rf"^{_PREPOSITION}\s+(?={_PREPOSITION}{BOUND_R})",
            "",
            text,
            flags=re.IGNORECASE,
        )
        if updated == text:
            break
        text = updated

    # Иногда после времени остаётся «в позвонить …».
    text = re.sub(
        r"^(?:в|во)\s+(?=(?:позвонить|позвони|купить|забрать|выключить|включить|"
        r"записаться|сходить|заехать|оплатить|проверить|отвезти)\b)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" \t\n,.;:!?—–-")
    text = re.sub(
        r"\s+(?:в|во|на|о|об|про|для|к|ко|с|со)$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) > config.TASK_MAX_LENGTH:
        text = text[: config.TASK_MAX_LENGTH].rstrip(" ,.;") + "…"
    return text[:1].upper() + text[1:]


def _base_spans(
    text: str,
    *,
    include_target: bool = True,
    include_reminder_words: bool = True,
) -> Tuple[List[Span], Optional[str], bool]:
    spans: List[Span] = []
    target_key: Optional[str] = None
    target_explicit = False

    if include_target:
        after_verb = _TARGET_AFTER_VERB_RE.search(text)
        at_start = _TARGET_AT_START_RE.search(text)
        chosen: Optional[re.Match[str]] = None
        if after_verb and at_start:
            chosen = after_verb if after_verb.start() <= at_start.start() else at_start
        elif after_verb:
            chosen = after_verb
        elif at_start:
            chosen = at_start
        if chosen is not None:
            spans.append(chosen.span())
            target_key = config.SYNONYM_TO_ROLE.get(chosen.group(1).lower())
            target_explicit = target_key is not None

    if include_reminder_words:
        for pattern in (_SETUP_PHRASE_RE, _REMINDER_WORD_RE):
            spans.extend(match.span() for match in pattern.finditer(text))

    return spans, target_key, target_explicit


def sanitize_task_text(raw: str, now: Optional[datetime] = None) -> str:
    """Очищает текст, полученный от LLM, от служебных слов и маркеров времени."""
    if not raw:
        return ""
    now = now or config.now_minsk()
    text = str(raw).strip()
    spans, _, _ = _base_spans(text)
    for _, pattern in _REL_PATTERNS:
        spans.extend(match.span() for match in pattern.finditer(text))
    for candidate in _time_candidates(text):
        spans.append((candidate.start, candidate.end))
    day_info = _extract_day(text)
    if day_info:
        spans.extend(day_info.spans)
    weekday_info = _extract_weekday(text)
    if weekday_info:
        spans.extend(weekday_info.spans)
    date_info = _extract_date(text, [], now)
    if date_info:
        spans.extend(date_info.spans)
    for match in _DAYPART_ONLY_RE.finditer(text):
        spans.append(match.span())
    return _clean_task_text(_remove_spans(text, spans))


# ---------------------------------------------------------------------------
# Публичный локальный парсер
# ---------------------------------------------------------------------------
def local_parse(raw_text: str, now: Optional[datetime] = None) -> LocalParse:
    """Разбирает русский текст без сети. Возвращает максимально точный результат."""
    now = now or config.now_minsk()
    original = (raw_text or "").strip()
    if not original:
        return LocalParse()

    spans: List[Span] = []
    notes: List[str] = []

    target_spans, target_key, target_explicit = _base_spans(original)
    spans.extend(target_spans)
    if target_explicit:
        notes.append("target")

    # 1. Относительное время («через 20 минут», «через полтора часа»).
    relative_candidates = _relative_candidates(original)
    for candidate in relative_candidates:
        spans.append((candidate.start, candidate.end))
    relative = min(relative_candidates, key=lambda item: (item.start, item.kind)) if relative_candidates else None
    if relative:
        notes.append(f"relative:{relative.kind}")

    # 2. Абсолютное время («в 9:40», «в 10 утра», «в 2 часа дня»).
    time_candidates = _time_candidates(original)
    for candidate in time_candidates:
        spans.append((candidate.start, candidate.end))
    absolute_time = min(time_candidates, key=lambda item: (item.start, item.kind)) if time_candidates else None
    if absolute_time:
        notes.append(f"time:{absolute_time.kind}")

    # 3. День/день недели/дата.
    day_info = _extract_day(original)
    if day_info:
        spans.extend(day_info.spans)
        notes.append(f"day:{day_info.kind}")

    weekday_info = _extract_weekday(original)
    if weekday_info:
        spans.extend(weekday_info.spans)
        notes.append(f"weekday:{weekday_info.index}")

    excluded_for_date = [span for span in spans]
    date_info = _extract_date(original, excluded_for_date, now)
    if date_info:
        spans.extend(date_info.spans)
        notes.append("date")

    # 4. Время суток без часа («завтра вечером»).
    daypart: Optional[Tuple[int, int]] = None
    if relative is None and absolute_time is None:
        extracted = _extract_daypart_only(original, spans)
        if extracted:
            hour, minute, span = extracted
            daypart = (hour, minute)
            spans.append(span)
            for match in _DAYPART_ONLY_RE.finditer(original):
                if not _overlaps(match.span(), [(span[0], span[1])]):
                    spans.append(match.span())
            notes.append("daypart")

    # 5. Итоговое время.
    scheduled_at: Optional[datetime] = None
    has_explicit_time = False
    if relative is not None:
        scheduled_at = (now + relative.delta).replace(microsecond=0)
        has_explicit_time = True
    elif absolute_time is not None:
        scheduled_at = _resolve_datetime(
            now,
            absolute_time.hour,
            absolute_time.minute,
            day_info.kind if day_info else None,
            weekday_info,
            date_info,
        )
        has_explicit_time = True
    elif daypart is not None:
        scheduled_at = _resolve_datetime(
            now,
            daypart[0],
            daypart[1],
            day_info.kind if day_info else None,
            weekday_info,
            date_info,
        )
        has_explicit_time = True
    elif day_info or weekday_info or date_info:
        scheduled_at = _resolve_datetime(
            now,
            9,
            0,
            day_info.kind if day_info else None,
            weekday_info,
            date_info,
        )

    task_text = _clean_task_text(_remove_spans(original, spans))
    if scheduled_at is None:
        confidence = "none"
    elif has_explicit_time:
        confidence = "high"
    elif day_info or weekday_info or date_info:
        confidence = "medium"
    else:
        confidence = "low"

    return LocalParse(
        scheduled_at=scheduled_at,
        task_text=task_text,
        target_role_key=target_key or config.SELF_ROLE_KEY,
        target_explicit=target_explicit,
        has_explicit_time=has_explicit_time,
        has_explicit_day=bool(day_info or weekday_info or date_info),
        confidence=confidence,
        notes=tuple(notes),
    )


def items_from_local(local: LocalParse) -> List[ParsedReminder]:
    if local is None or local.scheduled_at is None:
        return []
    return [
        ParsedReminder(
            target_role_key=local.target_role_key,
            scheduled_at=local.scheduled_at,
            task_text=local.task_text,
            source="local",
            explicit_time=local.has_explicit_time,
        )
    ]


def has_multiple_time_markers(text: str) -> bool:
    """Есть ли в сообщении больше одного независимого времени (для маршрута в LLM)."""
    spans: List[Span] = []
    for candidate in _time_candidates(text):
        spans.append((candidate.start, candidate.end))
    for candidate in _relative_candidates(text):
        spans.append((candidate.start, candidate.end))
    return len(_merge_spans(spans)) >= 2


def parse_datetime_flexible(value: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    """Приводит дату из ответа LLM к naive-времени Минска."""
    now = now or config.now_minsk()
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        parsed = None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%d.%m.%Y %H:%M:%S",
                "%d.%m.%Y %H:%M",
                "%d.%m.%Y",
                "%Y-%m-%d",
            ):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            if re.fullmatch(r"\d{1,2}:\d{2}", raw):
                try:
                    hour, minute = (int(part) for part in raw.split(":"))
                    parsed = datetime.combine(now.date(), time(hour, minute))
                    if parsed <= now:
                        parsed += timedelta(days=1)
                except ValueError:
                    return None
            else:
                return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(config.MINSK_TZ).replace(tzinfo=None)
    # Дата без времени от LLM трактуется как 09:00, как в системном промпте.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()) or re.fullmatch(
        r"\d{1,2}\.\d{1,2}\.\d{4}", str(value).strip()
    ):
        parsed = parsed.replace(hour=9, minute=0, second=0)
    return parsed.replace(microsecond=0)


def merge_ai_with_local(
    ai_items: Sequence[ParsedReminder],
    local: Optional[LocalParse],
    now: Optional[datetime] = None,
) -> List[ParsedReminder]:
    """Пост-валидация ответа LLM детерминированным парсером.

    Если в исходнике было явное `в 9:40` / `через 20 минут`, локальное время
    принудительно заменяет ответ модели — защита от галлюцинаций.
    """
    now = now or config.now_minsk()
    if not ai_items:
        return items_from_local(local) if local else []

    single = len(ai_items) == 1
    result: List[ParsedReminder] = []

    for item in ai_items:
        task_text = sanitize_task_text(item.task_text)
        target_key = item.target_role_key if item.target_role_key in config.PREDEFINED_ROLES or item.target_role_key in {
            config.SELF_ROLE_KEY,
            config.ALL_ROLE_KEY,
        } else config.SELF_ROLE_KEY

        scheduled_at = item.scheduled_at
        source = "ai"
        explicit_time = item.explicit_time

        if single and local is not None:
            overridden = False
            if local.has_explicit_time and local.scheduled_at is not None:
                if scheduled_at != local.scheduled_at:
                    overridden = True
                scheduled_at = local.scheduled_at
                explicit_time = True
            elif (
                scheduled_at is not None
                and scheduled_at < now - timedelta(minutes=2)
                and local.scheduled_at is not None
                and local.scheduled_at >= now - timedelta(minutes=1)
            ):
                scheduled_at = local.scheduled_at
                overridden = True
            if local.target_explicit and local.target_role_key != target_key:
                target_key = local.target_role_key
                overridden = True
            if not task_text and local.task_text:
                task_text = local.task_text
                overridden = True
            if overridden:
                source = "hybrid"

        if scheduled_at is None:
            continue
        if not task_text:
            if single and local and local.task_text:
                task_text = local.task_text
            else:
                continue

        result.append(
            ParsedReminder(
                target_role_key=target_key,
                scheduled_at=scheduled_at,
                task_text=task_text,
                source=source,
                explicit_time=explicit_time,
            )
        )

    if not result:
        return items_from_local(local) if local else []
    return result
