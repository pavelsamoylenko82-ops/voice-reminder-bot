"""Слой доступа к SQLite.

Все запросы к `/app/data/reminders.db` собраны здесь. Схема обновляется
безопасно: при старте выполняется `CREATE TABLE IF NOT EXISTS` и набор
`ALTER TABLE ... ADD COLUMN`, поэтому старые базы (включая базу из v2)
продолжают работать без ручных миграций.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

# Разрешённые поля для update_reminder — защита от случайного SQL-инъекционного
# формирования имён колонок.
_REMINDER_UPDATABLE_FIELDS = {
    "creator_id",
    "target_id",
    "target_role_key",
    "original_text",
    "parsed_text",
    "scheduled_at",
    "status",
    "retry_count",
    "last_sent_at",
    "sent_message_id",
}


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> bool:
    """Добавляет колонку, если её ещё нет. Возвращает True при изменении схемы."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        logger.info("Миграция БД: %s.%s добавлена", table, column)
        return True
    except sqlite3.OperationalError as exc:
        # Гонка двух контейнеров/повторный старт: колонка уже могла появиться.
        if "duplicate column" in str(exc).lower():
            return False
        logger.exception("Не удалось добавить колонку %s.%s", table, column)
        raise


def init_db() -> None:
    """Создаёт таблицы, индексы и догоняет схему до v3.0."""
    with connection() as conn:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:  # например, ФС без WAL
            logger.warning("WAL-режим недоступен: %s", exc)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                role_key TEXT DEFAULT 'pasha',
                display_name TEXT,
                roles TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                target_id INTEGER,
                target_role_key TEXT DEFAULT 'self',
                original_text TEXT,
                parsed_text TEXT,
                scheduled_at TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TEXT,
                retry_count INTEGER DEFAULT 0,
                last_sent_at TEXT DEFAULT '',
                sent_message_id INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shopping_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_text TEXT,
                added_by TEXT,
                is_bought INTEGER DEFAULT 0,
                created_at TEXT,
                price_cents INTEGER DEFAULT 0,
                finance_exported_at TEXT DEFAULT '',
                finance_batch_id TEXT DEFAULT ''
            )
            """
        )

        # --- Миграции старых баз ---
        _add_column(conn, "users", "role_key", "TEXT DEFAULT 'pasha'")
        _add_column(conn, "users", "display_name", "TEXT")
        _add_column(conn, "users", "roles", "TEXT")
        _add_column(conn, "users", "created_at", "TEXT")
        _add_column(conn, "reminders", "target_role_key", "TEXT DEFAULT 'self'")
        _add_column(conn, "reminders", "retry_count", "INTEGER DEFAULT 0")
        _add_column(conn, "reminders", "last_sent_at", "TEXT DEFAULT ''")
        _add_column(conn, "reminders", "sent_message_id", "INTEGER DEFAULT 0")
        _add_column(conn, "shopping_list", "price_cents", "INTEGER DEFAULT 0")
        _add_column(conn, "shopping_list", "finance_exported_at", "TEXT DEFAULT ''")
        _add_column(conn, "shopping_list", "finance_batch_id", "TEXT DEFAULT ''")

        conn.execute("UPDATE shopping_list SET price_cents = 0 WHERE price_cents IS NULL")
        conn.execute("UPDATE shopping_list SET finance_exported_at = '' WHERE finance_exported_at IS NULL")
        conn.execute("UPDATE shopping_list SET finance_batch_id = '' WHERE finance_batch_id IS NULL")

        conn.execute("UPDATE reminders SET retry_count = 0 WHERE retry_count IS NULL")
        conn.execute("UPDATE reminders SET last_sent_at = '' WHERE last_sent_at IS NULL")
        conn.execute("UPDATE reminders SET sent_message_id = 0 WHERE sent_message_id IS NULL")
        conn.execute("UPDATE reminders SET target_role_key = 'self' WHERE target_role_key IS NULL OR target_role_key = ''")

        # Совместимость с самой первой версией профиля Паши.
        pasha = config.PREDEFINED_ROLES["pasha"]
        conn.execute(
            "UPDATE users SET role_key = 'pasha', display_name = ?, roles = ? "
            "WHERE LOWER(COALESCE(display_name, '')) IN ('паша', 'pasha', 'павел') "
            "AND (role_key IS NULL OR role_key = '')",
            (pasha["name"], ", ".join(pasha["synonyms"])),
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_status_due "
            "ON reminders(status, scheduled_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_status_retry "
            "ON reminders(status, last_sent_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_target "
            "ON reminders(target_id, target_role_key, status)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role_key)")

    logger.info("База данных готова: %s", config.DB_PATH)


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
def get_user(tg_id: int) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return dict(row) if row else None


def get_user_list() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC, tg_id ASC").fetchall()
    return [dict(row) for row in rows]


def get_user_by_role(role_key: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role_key = ? ORDER BY created_at DESC, tg_id DESC LIMIT 1",
            (role_key,),
        ).fetchone()
    return dict(row) if row else None


def connected_roles() -> set[str]:
    with connection() as conn:
        rows = conn.execute("SELECT DISTINCT role_key FROM users WHERE role_key IS NOT NULL").fetchall()
    return {row["role_key"] for row in rows if row["role_key"]}


def upsert_user(tg_id: int, role_key: str, display_name: str, roles: str) -> None:
    now = config.format_db_dt(config.now_minsk())
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO users (tg_id, role_key, display_name, roles, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                role_key = excluded.role_key,
                display_name = excluded.display_name,
                roles = excluded.roles
            """,
            (tg_id, role_key, display_name, roles, now),
        )
    attach_role_reminders(role_key, tg_id)


def attach_role_reminders(role_key: str, tg_id: int) -> None:
    """Привязывает «висящие» напоминания роли к только что зарегистрированному."""
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET target_id = ? "
            "WHERE target_id = 0 AND target_role_key = ? AND status IN ('PENDING', 'SENT')",
            (tg_id, role_key),
        )
        changed = cursor.rowcount
    if changed:
        logger.info("К роли %s привязано напоминаний: %s", role_key, changed)


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------
def create_reminder(
    creator_id: int,
    target_id: int,
    target_role_key: str,
    original_text: str,
    parsed_text: str,
    scheduled_at: str,
) -> int:
    now = config.format_db_dt(config.now_minsk())
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reminders (
                creator_id, target_id, target_role_key, original_text,
                parsed_text, scheduled_at, status, created_at,
                retry_count, last_sent_at, sent_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, 0, '', 0)
            """,
            (creator_id, target_id, target_role_key, original_text, parsed_text, scheduled_at, now),
        )
        return int(cursor.lastrowid)


def get_reminder(reminder_id: int) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return dict(row) if row else None


def update_reminder(reminder_id: int, **fields: Any) -> bool:
    unknown = set(fields) - _REMINDER_UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля обновления: {sorted(unknown)}")
    if not fields:
        return False
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [reminder_id]
    with connection() as conn:
        cursor = conn.execute(f"UPDATE reminders SET {assignments} WHERE id = ?", values)
        changed = cursor.rowcount
    return changed > 0


def mark_sent_started(reminder_id: int, now_str: str) -> None:
    update_reminder(
        reminder_id,
        status="SENT",
        retry_count=1,
        last_sent_at=now_str,
        sent_message_id=0,
    )


def set_sent_message_id(reminder_id: int, message_id: int) -> None:
    if message_id:
        update_reminder(reminder_id, sent_message_id=int(message_id))


def set_retry_state(
    reminder_id: int,
    retry_count: int,
    last_sent_at: str,
    status: Optional[str] = None,
) -> None:
    fields: Dict[str, Any] = {
        "retry_count": retry_count,
        "last_sent_at": last_sent_at,
        "sent_message_id": 0,
    }
    if status is not None:
        fields["status"] = status
    update_reminder(reminder_id, **fields)


def reschedule_reminder(reminder_id: int, scheduled_at: str) -> None:
    update_reminder(
        reminder_id,
        scheduled_at=scheduled_at,
        status="PENDING",
        retry_count=0,
        last_sent_at="",
        sent_message_id=0,
    )


def complete_reminder(reminder_id: int) -> None:
    update_reminder(reminder_id, status="COMPLETED")


def cancel_reminder(reminder_id: int) -> None:
    update_reminder(reminder_id, status="CANCELLED")


def get_due_pending(now_str: str, limit: int = 100) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'PENDING' AND scheduled_at <= ? "
            "ORDER BY scheduled_at ASC LIMIT ?",
            (now_str, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_due_retries(cutoff_str: str, limit: int = 100) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'SENT'
              AND retry_count > 0
              AND retry_count < ?
              AND last_sent_at IS NOT NULL
              AND last_sent_at != ''
              AND last_sent_at <= ?
            ORDER BY last_sent_at ASC
            LIMIT ?
            """,
            (config.MAX_RETRY_COUNT, cutoff_str, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def expire_finished_retries() -> int:
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'EXPIRED' "
            "WHERE status = 'SENT' AND retry_count >= ?",
            (config.MAX_RETRY_COUNT,),
        )
        changed = int(cursor.rowcount)
    if changed:
        logger.info("Помечено истёкшими напоминаний: %s", changed)
    return changed


def get_active_for_user(tg_id: int, role_key: str, limit: int = 20) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE status IN ('PENDING', 'SENT')
              AND (
                    creator_id = ?
                    OR target_id = ?
                    OR target_id = -1
                    OR (target_id = 0 AND target_role_key = ?)
                  )
            ORDER BY scheduled_at ASC
            LIMIT ?
            """,
            (tg_id, tg_id, role_key, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def count_active_for_user(tg_id: int, role_key: str) -> int:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM reminders
            WHERE status IN ('PENDING', 'SENT')
              AND (
                    creator_id = ?
                    OR target_id = ?
                    OR target_id = -1
                    OR (target_id = 0 AND target_role_key = ?)
                  )
            """,
            (tg_id, tg_id, role_key),
        ).fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# Список покупок
# ---------------------------------------------------------------------------
def get_shopping_items() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM shopping_list ORDER BY id ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_shopping_item(item_id: int) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM shopping_list WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def add_shopping_items(items: List[str], added_by: str) -> int:
    if not items:
        return 0
    now = config.format_db_dt(config.now_minsk())
    with connection() as conn:
        conn.executemany(
            "INSERT INTO shopping_list (item_text, added_by, is_bought, created_at, price_cents) "
            "VALUES (?, ?, 0, ?, 0)",
            [(item, added_by, now) for item in items],
        )
    return len(items)


def set_shopping_price(item_id: int, price_cents: int) -> bool:
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE shopping_list SET price_cents = ? WHERE id = ?",
            (max(0, int(price_cents)), item_id),
        )
        changed = cursor.rowcount
    return changed > 0


def get_unexported_purchased_items() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM shopping_list
            WHERE is_bought = 1
              AND price_cents > 0
              AND (finance_exported_at IS NULL OR finance_exported_at = '')
            ORDER BY id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_pending_shopping_items() -> List[Dict[str, Any]]:
    """Позиции, для которых партия уже заявлена, но успех ещё не подтверждён."""
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM shopping_list
            WHERE finance_batch_id IS NOT NULL
              AND finance_batch_id != ''
              AND (finance_exported_at IS NULL OR finance_exported_at = '')
            ORDER BY id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def begin_shopping_export(item_ids: List[int], batch_id: str) -> int:
    """Застолбить batch_id до HTTP-запроса, чтобы повтор шёл с тем же ключом."""
    if not item_ids or not batch_id:
        return 0
    placeholders = ", ".join("?" for _ in item_ids)
    values = [batch_id, *item_ids]
    with connection() as conn:
        cursor = conn.execute(
            f"UPDATE shopping_list SET finance_batch_id = ?, finance_exported_at = '' "
            f"WHERE id IN ({placeholders}) "
            f"AND (finance_exported_at IS NULL OR finance_exported_at = '') "
            f"AND (finance_batch_id IS NULL OR finance_batch_id = '')",
            values,
        )
        changed = int(cursor.rowcount)
    return changed


def clear_shopping_export_batch(batch_id: str) -> int:
    """Снять «зависшую» партию после окончательной ошибки, чтобы можно было править."""
    if not batch_id:
        return 0
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE shopping_list SET finance_batch_id = '' "
            "WHERE finance_batch_id = ? "
            "AND (finance_exported_at IS NULL OR finance_exported_at = '')",
            (batch_id,),
        )
        changed = int(cursor.rowcount)
    return changed


def mark_shopping_exported(item_ids: List[int], batch_id: str, exported_at: str) -> int:
    if not item_ids:
        return 0
    placeholders = ", ".join("?" for _ in item_ids)
    values = [exported_at, batch_id, *item_ids]
    with connection() as conn:
        cursor = conn.execute(
            f"UPDATE shopping_list SET finance_exported_at = ?, finance_batch_id = ? "
            f"WHERE id IN ({placeholders})",
            values,
        )
        changed = int(cursor.rowcount)
    return changed


def toggle_shopping_item(item_id: int) -> bool:
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE shopping_list SET is_bought = 1 - is_bought WHERE id = ?",
            (item_id,),
        )
        changed = cursor.rowcount
    return changed > 0


def clear_bought_items() -> int:
    with connection() as conn:
        cursor = conn.execute("DELETE FROM shopping_list WHERE is_bought = 1")
        changed = int(cursor.rowcount)
    return changed


# ---------------------------------------------------------------------------
# Резолвинг семейной роли в получателя
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TargetInfo:
    target_id: int
    role_key: str
    label: str
    registered: bool


def resolve_target(creator: Dict[str, Any], target_key: str) -> TargetInfo:
    """Превращает role_key от парсера в конкретный chat_id.

    * self / пусто / своя же роль -> автор;
    * all -> -1 (рассылка всей семье);
    * роль без регистрации -> target_id=0, напоминание привяжется позже.
    """
    creator_id = int(creator["tg_id"])
    creator_role = creator.get("role_key") or "self"
    key = (target_key or "self").strip().lower()

    if key in {"", "self", creator_role}:
        return TargetInfo(
            target_id=creator_id,
            role_key=config.SELF_ROLE_KEY,
            label=config.SELF_LABEL,
            registered=True,
        )

    if key == config.ALL_ROLE_KEY:
        return TargetInfo(
            target_id=-1,
            role_key=config.ALL_ROLE_KEY,
            label=config.ALL_LABEL,
            registered=True,
        )

    if key in config.PREDEFINED_ROLES:
        user = get_user_by_role(key)
        label = config.role_label(key)
        if user:
            return TargetInfo(
                target_id=int(user["tg_id"]),
                role_key=key,
                label=label,
                registered=True,
            )
        return TargetInfo(target_id=0, role_key=key, label=label, registered=False)

    return TargetInfo(
        target_id=creator_id,
        role_key=config.SELF_ROLE_KEY,
        label=config.SELF_LABEL,
        registered=True,
    )


def target_label_for_reminder(reminder: Dict[str, Any]) -> str:
    if reminder.get("target_id") == -1 or reminder.get("target_role_key") == config.ALL_ROLE_KEY:
        return config.ALL_LABEL
    if reminder.get("target_id") == reminder.get("creator_id") or reminder.get("target_role_key") in {
        None,
        "",
        config.SELF_ROLE_KEY,
    }:
        return config.SELF_LABEL
    return config.role_label(str(reminder.get("target_role_key") or config.SELF_ROLE_KEY))


def resolve_chat_ids_for_reminder(reminder: Dict[str, Any]) -> Tuple[List[int], Optional[str]]:
    """Возвращает список chat_id и предупреждение для автора (если роль не подключена)."""
    target_id = int(reminder.get("target_id") or 0)
    creator_id = int(reminder.get("creator_id") or 0)
    role_key = str(reminder.get("target_role_key") or config.SELF_ROLE_KEY)

    if target_id == -1 or role_key == config.ALL_ROLE_KEY:
        users = get_user_list()
        chat_ids = [int(user["tg_id"]) for user in users]
        if creator_id and creator_id not in chat_ids:
            chat_ids.append(creator_id)
        # Убираем дубли, сохраняя порядок.
        return list(dict.fromkeys(chat_ids)), None

    if target_id > 0:
        return [target_id], None

    if role_key in {config.SELF_ROLE_KEY, ""}:
        return ([creator_id], None) if creator_id else ([], "не удалось определить получателя")

    user = get_user_by_role(role_key) if role_key != config.ALL_ROLE_KEY else None
    if user:
        return [int(user["tg_id"])], None

    if creator_id:
        warning = f"получатель роли {config.role_label(role_key)} ещё не подключил бота"
        return [creator_id], warning
    return [], "не удалось определить получателя"
