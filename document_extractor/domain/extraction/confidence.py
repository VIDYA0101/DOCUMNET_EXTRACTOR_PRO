"""Confidence scoring.

Multiplicative by design: any single strong negative signal should drag the
score down. A perfectly positioned value that fails its regex is not 80%
confident.

Every score carries its components so the review screen can answer "why is
this 0.62?" without guesswork.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STRATEGY_SCORE = {
    "anchor_relative": 1.00,
    "regex_page": 0.95,
    "label_pair": 0.95,
    "table_cell": 0.90,
    "region_absolute": 0.70,
    "constant": 1.00,
    "computed": 0.90,
}

FALLBACK_PENALTY = 0.85     # per position past the first strategy


@dataclass
class Score:
    value: float = 1.0
    components: dict = field(default_factory=dict)

    def apply(self, name: str, factor: float) -> "Score":
        factor = max(0.0, min(1.0, float(factor)))
        self.components[name] = round(factor, 4)
        self.value *= factor
        return self

    def finish(self) -> float:
        self.value = max(0.0, min(1.0, self.value))
        return self.value

    def explain(self) -> str:
        weakest = sorted(self.components.items(), key=lambda kv: kv[1])[:3]
        return ", ".join(f"{k}={v}" for k, v in weakest)


def field_confidence(*, template_match: float, strategy: str,
                     strategy_index: int, ocr_conf: float,
                     pattern_ok: bool | None, candidate_count: int,
                     type_ok: bool, coerced: bool = False,
                     cross_check_ok: bool | None = None,
                     anchor_score: float = 1.0) -> Score:
    s = Score()
    s.apply("template_match", template_match)

    base = STRATEGY_SCORE.get(strategy, 0.7)
    base *= FALLBACK_PENALTY ** max(0, strategy_index)
    s.apply("strategy", base)

    if strategy == "anchor_relative":
        s.apply("anchor", 0.85 + 0.15 * max(0.0, min(1.0, anchor_score)))

    s.apply("ocr", ocr_conf if ocr_conf > 0 else 1.0)

    if strategy == "constant":
        # Fixed by the template: nothing was extracted, so there is nothing to
        # be uncertain about. Discounting it for having no regex is wrong.
        s.apply("pattern", 1.00)
    elif pattern_ok is None:
        s.apply("pattern", 0.90)          # no pattern defined: mild discount
    else:
        s.apply("pattern", 1.00 if pattern_ok else 0.30)

    if candidate_count <= 1:
        s.apply("uniqueness", 1.00)
    elif candidate_count == 2:
        s.apply("uniqueness", 0.50)
    else:
        s.apply("uniqueness", 0.25)

    s.apply("type", 1.00 if type_ok and not coerced else (0.80 if coerced else 0.20))

    if cross_check_ok is None:
        s.apply("cross_check", 0.95)
    else:
        s.apply("cross_check", 1.00 if cross_check_ok else 0.40)

    s.finish()
    return s


def document_confidence(field_scores: list[float], match_score: float) -> float:
    """Document-level score: the weakest field dominates, tempered by the mean."""
    if not field_scores:
        return 0.0
    lowest = min(field_scores)
    mean = sum(field_scores) / len(field_scores)
    return round(match_score * (0.6 * lowest + 0.4 * mean), 4)
