"""Gate plant: a step without a budget row and a banned import in a step module."""

import scripts.inbox
from scripts.tick.step import CallStep

PLANTED = CallStep("planted_step", "g", scripts.inbox, needs=(), reads=(), writes=(), lane="l", effects=(), feature="f")
