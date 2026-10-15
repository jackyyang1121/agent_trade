"""Observable, point-in-time signals and transparent heuristic decision rules."""

from agent.signals.quant import quant_signals, reverse_research_candidates
from agent.signals.research import research_signal
from agent.signals.fusion import fuse

__all__ = ["quant_signals", "research_signal", "fuse", "reverse_research_candidates"]
