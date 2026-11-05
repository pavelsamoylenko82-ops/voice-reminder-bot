"""Детерминированные тесты парсера (без Telegram, БД и сети).

Запуск:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parser


class ParserRequiredCases(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 9, 38, 20)  # вторник, 09:38:20

    def assert_reminder(self, text, scheduled, target, task):
        result = parser.local_parse(text, self.NOW)
        self.assertEqual(result.scheduled_at, scheduled, text)
        self.assertEqual(result.target_role_key, target, text)
        self.assertEqual(result.task_text, task, text)

    def test_near_time_is_not_shifted_by_an_hour(self):
        self.assert_reminder(
            "в 9:40 напомни в про звонок",
            datetime(2026, 10, 27, 9, 40),
            "self",
            "Про звонок",
        )

    def test_typo_and_aftertomorrow(self):
        self.assert_reminder(
            "стомотолог в 10 утра послезавтра",
            datetime(2026, 10, 29, 10, 0),
            "self",
            "Стоматолог",
        )

    def test_target_and_today(self):
        self.assert_reminder(
            "напомни папе сегодня в 19:00 забрать ключи",
            datetime(2026, 10, 27, 19, 0),
            "denis",
            "Забрать ключи",
        )

    def test_relative_minutes(self):
        self.assert_reminder(
            "через 20 минут выключить духовку",
            datetime(2026, 10, 27, 9, 58, 20),
            "self",
            "Выключить духовку",
        )


class ParserFormats(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 9, 38, 20)

    def parse(self, text):
        return parser.local_parse(text, self.NOW)

    def test_absolute_formats(self):
        cases = {
            "в 09.40 напомни про звонок": datetime(2026, 10, 27, 9, 40),
            "в 18:30 ужин": datetime(2026, 10, 27, 18, 30),
            "в 10 утра стоматолог": datetime(2026, 10, 27, 10, 0),
            "в 7 вечера позвонить маме": datetime(2026, 10, 27, 19, 0),
            "в 2 часа дня встреча": datetime(2026, 10, 27, 14, 0),
            "в 2 часа 30 минут дня встреча": datetime(2026, 10, 27, 14, 30),
            "в 2 часа 30 минут встреча": datetime(2026, 10, 28, 2, 30),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).scheduled_at, expected)

    def test_relative_formats(self):
        cases = {
            "через 5 минут выключить духовку": datetime(2026, 10, 27, 9, 43, 20),
            "через 10 мин выключить духовку": datetime(2026, 10, 27, 9, 48, 20),
            "через полчаса выключить духовку": datetime(2026, 10, 27, 10, 8, 20),
            "через час выключить духовку": datetime(2026, 10, 27, 10, 38, 20),
            "через 2 часа выключить духовку": datetime(2026, 10, 27, 11, 38, 20),
            "через полтора часа выключить духовку": datetime(2026, 10, 27, 11, 8, 20),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).scheduled_at, expected)

    def test_days_and_weekdays(self):
        self.assertEqual(self.parse("завтра").scheduled_at, datetime(2026, 10, 28, 9, 0))
        self.assertEqual(
            self.parse("завтра в 9:00 забрать ключи").scheduled_at,
            datetime(2026, 10, 28, 9, 0),
        )
        self.assertEqual(
            self.parse("завтра в 10 стоматолог").scheduled_at,
            datetime(2026, 10, 28, 10, 0),
        )
        self.assertEqual(
            self.parse("послезавтра 10:00 стоматолог").scheduled_at,
            datetime(2026, 10, 29, 10, 0),
        )
        self.assertEqual(
            self.parse("в понедельник в 10:00 встреча").scheduled_at,
            datetime(2026, 11, 2, 10, 0),
        )

    def test_bare_hour_does_not_eat_units(self):
        self.assertIsNone(self.parse("в 10 раз больше").scheduled_at)
        self.assertIsNone(self.parse("в 2 года").scheduled_at)

    def test_dates(self):
        self.assertEqual(
            self.parse("29.10 в 12:00 стоматолог").scheduled_at,
            datetime(2026, 10, 29, 12, 0),
        )
        self.assertEqual(
            self.parse("29 октября стоматолог").scheduled_at,
            datetime(2026, 10, 29, 9, 0),
        )
        self.assertEqual(
            self.parse("29.10.2027 в 12:00 стоматолог").scheduled_at,
            datetime(2027, 10, 29, 12, 0),
        )
        self.assertEqual(
            self.parse("01.01 в 00:05 новый год").scheduled_at,
            datetime(2027, 1, 1, 0, 5),
        )

    def test_decimal_quantity_is_not_a_date(self):
        result = self.parse("купить 1.5 кг картошки")
        self.assertIsNone(result.scheduled_at)
        self.assertEqual(result.task_text, "Купить 1.5 кг картошки")
        self.assertEqual(
            parser.sanitize_task_text("Купить 1.5 кг картошки", self.NOW),
            "Купить 1.5 кг картошки",
        )

    def test_past_time_rolls_to_tomorrow_only_without_today(self):
        self.assertEqual(self.parse("в 8:00 зарядка").scheduled_at, datetime(2026, 10, 28, 8, 0))
        self.assertEqual(
            self.parse("в 8:00 сегодня зарядка").scheduled_at,
            datetime(2026, 10, 27, 8, 0),
        )

    def test_near_future_stays_today(self):
        self.assertEqual(
            self.parse("в 9:39 напомни позвонить").scheduled_at,
            datetime(2026, 10, 27, 9, 39),
        )

    def test_weekday_today_with_future_time_stays_today(self):
        # 27.10.2026 — вторник.
        self.assertEqual(
            self.parse("во вторник в 18:00 встреча").scheduled_at,
            datetime(2026, 10, 27, 18, 0),
        )
        self.assertEqual(
            self.parse("во вторник в 8:00 встреча").scheduled_at,
            datetime(2026, 11, 3, 8, 0),
        )

    def test_daypart_defaults(self):
        self.assertEqual(
            self.parse("завтра вечером посмотреть фильм").scheduled_at,
            datetime(2026, 10, 28, 19, 0),
        )
        self.assertEqual(
            self.parse("сегодня утром зарядка").scheduled_at,
            datetime(2026, 10, 27, 9, 0),
        )


class ParserTextAndRoles(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 9, 38, 20)

    def parse(self, text):
        return parser.local_parse(text, self.NOW)

    def test_garbage_preposition_cleanup(self):
        self.assertEqual(self.parse("в 9:40 напомни в про звонок").task_text, "Про звонок")
        self.assertEqual(self.parse("в 09.40 напомни про звонок").task_text, "Про звонок")
        self.assertEqual(
            self.parse("пожалуйста, напомни мне в 9:40 про звонок").task_text,
            "Про звонок",
        )

    def test_target_synonyms(self):
        cases = {
            "напомни маме завтра в 12:00 купить хлеб": "yulya",
            "напомни папе завтра в 12:00 купить хлеб": "denis",
            "напомни Вере завтра в 12:00 купить хлеб": "vera",
            "напомни Верочке завтра в 12:00 купить хлеб": "verochka",
            "напомни брату завтра в 12:00 купить хлеб": "pasha",
            "напомни всем вечером посмотреть фильм": "all",
            "напомни мне завтра в 12:00 купить хлеб": "self",
        }
        for text, target in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).target_role_key, target)

    def test_target_inside_task_is_preserved(self):
        result = self.parse("в 7 вечера позвонить маме")
        self.assertEqual(result.target_role_key, "self")
        self.assertEqual(result.task_text, "Позвонить маме")

    def test_typo_fix_and_capitalization(self):
        self.assertEqual(self.parse("стомотолог в 10 утра").task_text, "Стоматолог")
        self.assertEqual(self.parse("через 20 минут выключить духовку").task_text, "Выключить духовку")

    def test_no_time_returns_none(self):
        result = self.parse("напомни про звонок")
        self.assertIsNone(result.scheduled_at)
        self.assertEqual(result.confidence, "none")

    def test_multiple_time_markers_detected(self):
        self.assertTrue(parser.has_multiple_time_markers("в 9:40 и в 18:00 дела"))
        self.assertFalse(parser.has_multiple_time_markers("сегодня в 19:00 забрать ключи"))


class ParserPostValidation(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 9, 38, 20)

    def test_ai_hallucination_overridden_by_explicit_time(self):
        local = parser.local_parse("в 9:40 напомни про звонок", self.NOW)
        ai = [
            parser.ParsedReminder(
                target_role_key="self",
                scheduled_at=datetime(2026, 10, 27, 10, 38),
                task_text="В 9:40 напомни про звонок",
                source="ai",
            )
        ]
        merged = parser.merge_ai_with_local(ai, local, self.NOW)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].scheduled_at, datetime(2026, 10, 27, 9, 40))
        self.assertEqual(merged[0].task_text, "Про звонок")
        self.assertEqual(merged[0].source, "hybrid")

    def test_ai_is_kept_when_no_explicit_time(self):
        ai = [
            parser.ParsedReminder(
                target_role_key="denis",
                scheduled_at=datetime(2026, 10, 28, 10, 0),
                task_text="Стоматолог",
                source="ai",
            )
        ]
        merged = parser.merge_ai_with_local(ai, None, self.NOW)
        self.assertEqual(merged[0].scheduled_at, datetime(2026, 10, 28, 10, 0))
        self.assertEqual(merged[0].target_role_key, "denis")

    def test_ai_time_is_cleaned(self):
        self.assertEqual(
            parser.sanitize_task_text("В 9:40 напомни, пожалуйста, про звонок", self.NOW),
            "Про звонок",
        )

    def test_flexible_datetime(self):
        self.assertEqual(
            parser.parse_datetime_flexible("2026-10-27T09:40:00+03:00", self.NOW),
            datetime(2026, 10, 27, 9, 40),
        )
        self.assertEqual(
            parser.parse_datetime_flexible("27.10.2026 09:40", self.NOW),
            datetime(2026, 10, 27, 9, 40),
        )
        self.assertEqual(
            parser.parse_datetime_flexible("2026-10-29", self.NOW),
            datetime(2026, 10, 29, 9, 0),
        )


if __name__ == "__main__":
    unittest.main()
