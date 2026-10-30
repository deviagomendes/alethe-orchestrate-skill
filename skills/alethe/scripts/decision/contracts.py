"""The shape of a decision recommendation. It is untrusted input: validating it runs nothing."""
from typing import Any, Collection, Dict, Optional

SCHEMA_VERSION = 1
LEVELS = ("low", "medium", "high")
TIERS = ("cheap", "standard", "strong")


class DecisionError(Exception):
    """Anything that makes the decision layer step aside; callers fall back to the native flow."""


def _level(value: Any, name: str) -> str:
    if value not in LEVELS:
        raise DecisionError(f"{name} must be one of {', '.join(LEVELS)}")
    return value


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise DecisionError(f"{name} must be true or false")
    return value


def _probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise DecisionError(f"{name} must be a number from 0 to 1")
    return float(value)


def _choice(value: Any, name: str, allowed: Collection[str]) -> Optional[str]:
    if value is None:
        return None
    if value not in allowed:
        raise DecisionError(f"{name} {value!r} is not one Alethe has")
    return value


def validate(raw: Any, playbooks: Collection[str], strategies: Collection[str]) -> Dict[str, Any]:
    """Return a clean copy of a recommendation, or raise DecisionError."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise DecisionError("not a version 1 recommendation")
    cls, rec, conf = raw.get("classification"), raw.get("recommendation"), raw.get("confidence")
    if not all(isinstance(part, dict) for part in (cls, rec, conf)):
        raise DecisionError("classification, recommendation and confidence are required")
    by_question = conf.get("by_question")
    if not isinstance(by_question, dict):
        raise DecisionError("confidence.by_question is required")
    return {
        "schema_version": SCHEMA_VERSION,
        "source": str(raw.get("source", "unknown"))[:64],
        "proposal": True,
        "abstained": _flag(raw.get("abstained", False), "abstained"),
        "classification": {
            "complexity": _level(cls.get("complexity"), "complexity"),
            "risk": _level(cls.get("risk"), "risk"),
            "delegation_recommended": _flag(cls.get("delegation_recommended"), "delegation_recommended"),
            "parallelizable": _flag(cls.get("parallelizable"), "parallelizable"),
        },
        "recommendation": {
            "playbook": _choice(rec.get("playbook"), "playbook", playbooks),
            "tier": _choice(rec.get("tier"), "tier", TIERS),
            "runner_strategy": _choice(rec.get("runner_strategy"), "runner_strategy", strategies),
        },
        "confidence": {
            "overall": _probability(conf.get("overall"), "confidence.overall"),
            "by_question": {str(k)[:32]: _probability(v, f"confidence.{k}") for k, v in by_question.items()},
        },
        "provenance": {
            key: str(value)[:128] for key, value in (raw.get("provenance") or {}).items() if isinstance(value, (str, int, float))
        },
    }
