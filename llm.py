"""Клиент OpenAI-совместимого шлюза и гибридная AI-ветка парсера.

Модуль:
* формирует подробный системный промпт с текущим временем Минска;
* умеет отправлять текст, изображения и голосовые;
* для голосовых сначала пробует `image_url` с data:audio, а при HTTP 400/422
  повторяет запрос с `input_audio` (требование v3.0);
* любой ответ модели проходит детерминированную пост-валидацию в `parser.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

import config
import parser

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.AI_TIMEOUT_SECONDS, connect=15.0),
            headers={
                "Authorization": f"Bearer {config.API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "OpenAI/Python 1.54.0",
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _system_prompt(user: Dict[str, Any], local: Optional[parser.LocalParse], now: datetime) -> str:
    role_key = user.get("role_key") or "self"
    creator_label = config.role_label(role_key) if role_key in config.PREDEFINED_ROLES else (
        user.get("display_name") or "Участник"
    )
    local_hint = {
        "scheduled_at": config.format_db_dt(local.scheduled_at) if local and local.scheduled_at else None,
        "task_text": local.task_text if local else "",
        "target_role_key": local.target_role_key if local else "self",
        "has_explicit_time": bool(local and local.has_explicit_time),
        "has_explicit_day": bool(local and local.has_explicit_day),
        "confidence": local.confidence if local else "none",
    }
    local_hint_json = json.dumps(local_hint, ensure_ascii=False)

    return f"""Ты — семейный ИИ-планировщик напоминаний. Отвечай ТОЛЬКО валидным JSON без markdown.
Сейчас: {now.strftime('%Y-%m-%d %H:%M:%S')} ({config.WEEKDAYS_RU_FULL[now.weekday()]}), часовой пояс Europe/Minsk (UTC+3).
Сегодня: {now.strftime('%Y-%m-%d')} ({config.WEEKDAYS_RU_FULL[now.weekday()]}).
Завтра: {(now + timedelta(days=1)).strftime('%Y-%m-%d')} ({config.WEEKDAYS_RU_FULL[(now + timedelta(days=1)).weekday()]}).
Послезавтра: {(now + timedelta(days=2)).strftime('%Y-%m-%d')} ({config.WEEKDAYS_RU_FULL[(now + timedelta(days=2)).weekday()]}).
Отправитель: {user.get('display_name') or 'Участник'} (role_key='{role_key}', {creator_label}).

ДОСТУПНЫЕ РОЛИ:
{config.full_roles_description()}

ПРАВИЛА ВЫЧИСЛЕНИЯ ВРЕМЕНИ (критично):
1. Если пользователь пишет «в 9:40», «в 09.40», «к 18:30» — это конкретное время сегодня, если оно ещё не прошло. Если сейчас 09:38:20, то «в 9:40» = СЕГОДНЯ 09:40:00, даже если до него 1–2 минуты. НИКОГДА не прибавляй час и не округляй.
2. Переноси время на завтра (+1 день) ТОЛЬКО если указанное ЧЧ:ММ уже строго прошло относительно текущего момента и в тексте нет слова «сегодня». Если пользователь сказал «сегодня», оставляй сегодняшнюю дату (даже если время уже прошло).
3. «через 5 минут», «через 10 мин», «через полчаса», «через час», «через 2 часа», «через полтора часа» — считай ТОЧНО от текущего времени, не округляй до часа.
4. «завтра», «послезавтра», дни недели и даты («29.10», «29 октября») — конкретная дата. Если указан только день без времени — ставь 09:00:00.
5. День недели: ближайший будущий. Если сегодня тот же день и время уже прошло — следующий такой день.
6. Не выдумывай время, которого нет в тексте. Не используй «сейчас + 1 час» как значение по умолчанию.
7. Все даты в scheduled_at — локальное время Минска, формат строго "YYYY-MM-DD HH:MM:SS".

ПРАВИЛА task_text:
- Полностью удали маркеры времени и даты: «в 9:40», «сегодня», «завтра», «послезавтра», «через 15 минут», «в 10 утра», «29 октября» и т.п.
- Удали служебные слова: «напомни», «напомнить», «напоминание», «пожалуйста», «плиз», «мне», «не забудь», «будь добр».
- Удали адресата из начала фразы и служебные слова: «папе», «маме», «всем», «для папы» и т.п. НО сохрани адресата внутри самой задачи, если это смысл действия: «позвонить маме» -> «Позвонить маме».
- Исправь очевидные опечатки («стомотолог» -> «Стоматолог»), сделай первую букву заглавной.
- Не переписывай задачу и не теряй важные детали (что именно, куда, зачем). Убирай только служебное.
- Если после очистки остался предлог-мусор вроде «в про звонок» — нормализуй до «Про звонок».

ПРАВИЛА target_role_key:
- Если явно указан член семьи — верни его ключ из списка выше.
- «всем», «всей семье» -> "all".
- Если адресат не указан или это сам отправитель -> "self".
- Не придумывай роль, если её нет в тексте.

ФОРМАТ ОТВЕТА (строго объект):
{{"reminders": [{{"target_role_key": "self", "scheduled_at": "YYYY-MM-DD HH:MM:SS", "task_text": "Текст"}}]}}
Если в сообщении несколько задач с разным временем — верни несколько элементов массива. Максимум 5.

ПРИМЕРЫ:
- Пример (если сейчас 09:38:20): текст «в 9:40 напомни в про звонок» -> {{"reminders":[{{"target_role_key":"self","scheduled_at":"{now.strftime('%Y-%m-%d')} 09:40:00","task_text":"Про звонок"}}]}}
- Текст «напомни папе сегодня в 19:00 забрать ключи» -> target_role_key "denis", время сегодня 19:00:00, task_text «Забрать ключи».
- Текст «стомотолог в 10 утра послезавтра» -> время послезавтра 10:00:00, task_text «Стоматолог».
- Текст «через 20 минут выключить духовку» -> время ровно через 20 минут от текущего, task_text «Выключить духовку».

ЛОКАЛЬНЫЙ ПРЕДПАРСЕР (может ошибаться, проверь по тексту): {local_hint_json}
"""


def should_use_ai(
    local: Optional[parser.LocalParse],
    text: str,
    media: Optional[Dict[str, Any]] = None,
) -> bool:
    if media:
        return True
    if config.AI_ALWAYS:
        return True
    # Локальный парсер уже дал полноценный результат (время + задача) —
    # отвечаем мгновенно, без сетевой задержки и без риска галлюцинаций.
    if (
        local is not None
        and local.scheduled_at is not None
        and local.task_text
        and not parser.has_multiple_time_markers(text or "")
    ):
        return False
    # Нет времени/задачи, несколько задач или неоднозначная фраза — в LLM.
    return True


def _media_instruction(media: Dict[str, Any], text: str) -> str:
    if media.get("type") == "image":
        base = (
            "На изображении может быть записка, скриншот, расписание, чек или фото. "
            "Извлеки из изображения задачу, дату и время. "
        )
    else:
        base = (
            "Это голосовое сообщение. Расшифруй речь и извлеки напоминание: "
            "задачу, дату, время и адресата. "
        )
    if text:
        base += f"Подпись/текст пользователя: {text}"
    return base


def _build_user_content(
    text: str,
    media: Optional[Dict[str, Any]],
    audio_variant: str,
) -> Union[str, List[Dict[str, Any]]]:
    if not media:
        return text

    instruction = _media_instruction(media, text)
    data = str(media.get("data") or "")
    mime = str(media.get("mime") or ("image/jpeg" if media.get("type") == "image" else "audio/ogg"))

    if media.get("type") == "image":
        return [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
        ]

    if audio_variant == "input_audio":
        return [
            {"type": "text", "text": instruction},
            {"type": "input_audio", "input_audio": {"data": data, "format": "ogg"}},
        ]

    # Первый вариант для голосового — image_url с data:audio/ogg.
    return [
        {"type": "text", "text": instruction},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
    ]


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
        return "\n".join(parts)
    return ""


def _audio_content(
    media: Dict[str, Any],
    audio_variant: str,
    instruction: str,
) -> List[Dict[str, Any]]:
    data = str(media.get("data") or "")
    mime = str(media.get("mime") or "audio/ogg")
    if audio_variant == "input_audio":
        audio_part: Dict[str, Any] = {
            "type": "input_audio",
            "input_audio": {"data": data, "format": "ogg"},
        }
    else:
        audio_part = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}"},
        }
    return [{"type": "text", "text": instruction}, audio_part]


def _clean_transcript(raw: str) -> str:
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^```(?:text)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^(?:расшифровка|текст)\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    return text.strip().strip('"').strip("'").strip()


async def transcribe_voice(media: Dict[str, Any], purpose: str = "reminder") -> str:
    """Распознаёт голосовое в текст через тот же OpenAI-совместимый шлюз."""
    if not media or media.get("type") != "audio" or not media.get("data"):
        return ""

    if purpose == "price":
        instruction = (
            "Это голосовое сообщение с ценой товара. Распознай речь и верни ТОЛЬКО "
            "число цифрами без слов: например 2.50, 12, 150. Если сказано "
            "«два пятьдесят» — верни 2.50."
        )
    else:
        instruction = (
            "Распознай голосовое сообщение дословно на русском языке. Верни только "
            "текст речи без пояснений, кавычек и markdown. Сохрани команды вроде "
            "«купи молоко и хлеб», «напомни папе», цены и даты. Числа по возможности "
            "записывай цифрами."
        )

    system_prompt = (
        "Ты — точный распознаватель русской речи для семейного Telegram-бота. "
        "Не пересказывай и не добавляй ничего от себя."
    )
    variants: Sequence[str] = ("image_url", "input_audio")

    for model in config.OPENAI_MODELS:
        for variant in variants:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _audio_content(media, variant, instruction)},
            ]
            try:
                raw, status, error_body = await _post_chat(model, messages)
            except httpx.TimeoutException:
                logger.warning("STT timeout: model=%s variant=%s", model, variant)
                break
            except httpx.HTTPError as exc:
                logger.warning("STT HTTP-ошибка: model=%s variant=%s: %s", model, variant, exc)
                break

            should_try_input_audio = variant == "image_url" and (
                (raw is None and status in {400, 422})
                or (raw is not None and not raw.strip())
            )
            if should_try_input_audio:
                logger.info(
                    "STT: image_url не дал текст (HTTP %s), пробую input_audio",
                    status,
                )
                continue
            if raw is None:
                logger.warning(
                    "STT отказал: model=%s variant=%s HTTP=%s body=%s",
                    model,
                    variant,
                    status,
                    error_body,
                )
                break

            transcript = _clean_transcript(raw)
            if transcript:
                logger.info("Голосовое распознано (%s символов, model=%s)", len(transcript), model)
                return transcript
            break

    return ""


def _extract_json_payload(raw: str) -> Optional[Any]:
    if not raw:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("LLM вернула невалидный JSON: %s", cleaned[:300])
        return None


def _payload_to_items(payload: Any, now: datetime) -> List[parser.ParsedReminder]:
    if not payload:
        return []
    raw_items: Any = payload
    if isinstance(payload, dict):
        raw_items = payload.get("reminders") or payload.get("items") or []
        if not raw_items and payload.get("scheduled_at"):
            raw_items = [payload]
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return []

    result: List[parser.ParsedReminder] = []
    for raw_item in raw_items[:5]:
        if not isinstance(raw_item, dict):
            continue
        target_key = str(raw_item.get("target_role_key") or "self").strip().lower()
        target_key = config.SYNONYM_TO_ROLE.get(target_key, target_key)
        if target_key not in config.PREDEFINED_ROLES and target_key not in {
            config.SELF_ROLE_KEY,
            config.ALL_ROLE_KEY,
        }:
            target_key = config.SELF_ROLE_KEY
        scheduled = parser.parse_datetime_flexible(raw_item.get("scheduled_at"), now)
        task_text = parser.sanitize_task_text(str(raw_item.get("task_text") or ""), now)
        if scheduled is None or not task_text:
            continue
        result.append(
            parser.ParsedReminder(
                target_role_key=target_key,
                scheduled_at=scheduled,
                task_text=task_text,
                source="ai",
                explicit_time=False,
            )
        )
    return result


async def _post_chat(
    model: str,
    messages: List[Dict[str, Any]],
) -> Tuple[Optional[str], int, str]:
    client = get_http_client()
    response = await client.post(
        f"{config.BASE_URL}/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 900,
        },
    )
    status = response.status_code
    if status >= 400:
        body = (response.text or "")[:400].replace("\n", " ")
        return None, status, body
    try:
        data = response.json()
    except ValueError:
        logger.warning("LLM вернула не-JSON HTTP-ответ (model=%s)", model)
        return None, status, "invalid json response"
    choices = data.get("choices") or []
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    return _flatten_content(content), status, ""


async def _ask_ai(
    user: Dict[str, Any],
    text: str,
    media: Optional[Dict[str, Any]],
    local: Optional[parser.LocalParse],
    now: datetime,
) -> List[parser.ParsedReminder]:
    system_prompt = _system_prompt(user, local, now)
    is_audio = bool(media and media.get("type") == "audio")
    variants: Sequence[str] = ("image_url", "input_audio") if is_audio else ("text",)

    for model in config.OPENAI_MODELS:
        for variant in variants:
            user_content = _build_user_content(text, media, variant)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            try:
                raw, status, error_body = await _post_chat(model, messages)
            except httpx.TimeoutException:
                logger.warning("LLM timeout: model=%s variant=%s", model, variant)
                break
            except httpx.HTTPError as exc:
                logger.warning("LLM HTTP-ошибка: model=%s variant=%s: %s", model, variant, exc)
                break

            if raw is None and is_audio and variant == "image_url" and status in {400, 422}:
                logger.info(
                    "Голосовое не принято как image_url (HTTP %s), пробую input_audio",
                    status,
                )
                continue
            if raw is None:
                logger.warning(
                    "LLM отказала: model=%s variant=%s HTTP=%s body=%s",
                    model,
                    variant,
                    status,
                    error_body,
                )
                break

            payload = _extract_json_payload(raw)
            items = _payload_to_items(payload, now)
            if items:
                logger.info("LLM вернула напоминаний: %s (model=%s)", len(items), model)
                return items
            logger.warning("LLM вернула пустой/непонятный ответ (model=%s)", model)
            break

    return []


async def parse_reminders(
    user: Dict[str, Any],
    text: str,
    media: Optional[Dict[str, Any]] = None,
    local: Optional[parser.LocalParse] = None,
    now: Optional[datetime] = None,
) -> Tuple[List[parser.ParsedReminder], str]:
    """Возвращает (напоминания, источник). Всегда есть локальный фоллбэк."""
    now = now or config.now_minsk()
    local = local if local is not None else parser.local_parse(text, now)

    if not should_use_ai(local, text, media):
        return parser.items_from_local(local), "local"

    try:
        ai_items = await _ask_ai(user, text, media, local, now)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Неожиданная ошибка AI-парсера, использую локальный результат")
        ai_items = []

    if ai_items:
        merged = parser.merge_ai_with_local(ai_items, local, now)
        sources = {item.source for item in merged}
        source = "hybrid" if "hybrid" in sources else "ai"
        return merged, source

    return parser.items_from_local(local), "local"
