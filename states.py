"""Состояния FSM для интерактивных сценариев."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ShoppingStates(StatesGroup):
    waiting_price = State()
