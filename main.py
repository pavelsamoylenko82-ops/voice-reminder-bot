"""Точка входа: бот «Семейный ИИ-Напоминатор» v3.0.

Запуск в Docker остаётся прежним:
    python main.py

База по умолчанию — /app/data/reminders.db (volume), LLM — OpenAI-совместимый
шлюз из OPENAI_BASE_URL. Все секреты читаются из окружения или .env.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import config
import db
import finance
import llm
from handlers import router
from scheduler import scheduler_loop

logger = logging.getLogger(__name__)


async def _set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить / выбрать роль"),
                BotCommand(command="list", description="Мои активные напоминания"),
                BotCommand(command="shop", description="Список покупок"),
                BotCommand(command="roles", description="Семья и роли"),
                BotCommand(command="help", description="Примеры и помощь"),
                BotCommand(command="cancel", description="Отменить ввод цены"),
            ]
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось установить меню команд: %s", exc)


async def main() -> None:
    config.setup_logging()
    logger.info("%s v%s запускается", config.APP_NAME, config.APP_VERSION)
    logger.info("База: %s", config.DB_PATH)
    logger.info("LLM: %s, модели: %s", config.BASE_URL, ", ".join(config.OPENAI_MODELS))

    db.init_db()

    bot = Bot(
        token=config.TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)

    scheduler_task = asyncio.create_task(scheduler_loop(bot), name="reminder-scheduler")

    try:
        await _set_commands(bot)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await llm.close_http_client()
        await finance.close_client()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Остановка по сигналу")
