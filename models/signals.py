from dataclasses import dataclass, field
from typing import Dict
from enum import Enum

class StrategyHorizon(Enum):
    LONG_TERM = "long_term"
    SHORT_TERM = "short_term"

class ActionDecision(Enum):
    STRONG_LONG = "STRONG_LONG"
    STANDARD_LONG = "STANDARD_LONG"
    WATCH_LONG = "WATCH_LONG"
    NEUTRAL = "NEUTRAL"
    WATCH_SHORT = "WATCH_SHORT"
    STANDARD_SHORT = "STANDARD_SHORT"
    STRONG_SHORT = "STRONG_SHORT"

@dataclass
class DimensionScores:
    """每个维度的信号分 [-100, +100]. 正=看多, 负=看空."""
    news: float
    data: float
    tech: float
    prediction: float

    def all_scores(self):
        return [self.news, self.data, self.tech, self.prediction]

@dataclass
class EvaluationResult:
    horizon: StrategyHorizon
    composite_score: float           # [-100, +100]
    dimension_scores: DimensionScores
    weighted_breakdown: Dict[str, float]
    decision: ActionDecision
    safety_valve_triggered: bool
    reasoning: str = ""
    details: Dict = field(default_factory=dict)

    @property
    def direction(self):
        if self.composite_score > 0:
            return "LONG"
        elif self.composite_score < 0:
            return "SHORT"
        return "NEUTRAL"
