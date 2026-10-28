"""FSM states for the pair / expiration selection flow.

There is no timeframe-selection state anymore — the bot analyzes a fixed
set of timeframes automatically per request (see analysis.multi_timeframe)
instead of the user picking one.
"""
from aiogram.fsm.state import State, StatesGroup


class SignalSetup(StatesGroup):
    menu = State()  # main menu shown, all callback selections resolve back here
    choosing_pair = State()
    choosing_expiration = State()
