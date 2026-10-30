"""Classify a task from observable task attributes and bilingual lexical cues, then map to a proposal.

jevos answers concrete questions about what the task text mentions instead of making uncalibrated
policy judgements. Deterministic lexical cues in English and Portuguese complement jevos (or run
alone when the runtime is off) so Portuguese and vague tasks work without the 600 MB runtime.
"""
import re
from typing import Any, Collection, Dict, Optional

from .client import Client
from .contracts import LEVELS, SCHEMA_VERSION, DecisionError, validate

MODEL = "jev-latest"
POLICY_VERSION = "observable-v2"
MAX_TASK_CHARS = 4000

QUESTIONS: Dict[str, Dict[str, Any]] = {
    "sensitive": {
        "type": "noul",
        "instructions": (
            "Does the task modify or fix authentication, tokens, passwords, security vulnerabilities, "
            "payment processing, production database migrations, or public API backwards compatibility?"
        ),
    },
    "hard_bug": {
        "type": "noul",
        "instructions": (
            "Does the task describe an intermittent bug, race condition, production-only issue "
            "without reproduction, or zero-downtime constraint?"
        ),
    },
    "explore_only": {
        "type": "noul",
        "instructions": (
            "Is the primary goal to read, understand, map, or summarize an unfamiliar codebase "
            "rather than making a routine edit?"
        ),
    },
    "many_similar": {
        "type": "noul",
        "instructions": (
            "Does the task explicitly mention checking, auditing, triaging, or translating dozens "
            "or hundreds of items (such as 60, 120, 200, or 300 items)?"
        ),
    },
    "mechanical_sweep": {
        "type": "noul",
        "instructions": (
            "Is this a repetitive or mechanical code change across many files or modules verified "
            "by a compiler, linter, typechecker, or existing test suite?"
        ),
    },
    "disjoint_modules": {
        "type": "noul",
        "instructions": (
            "Does the task explicitly state that multiple modules or services are independent "
            "or have no shared code?"
        ),
    },
    "sequential_steps": {
        "type": "noul",
        "instructions": (
            "Does the task describe building or changing multiple layers or components where steps "
            "depend on one another or must be updated together?"
        ),
    },
    "scope": {
        "type": "choice",
        "instructions": "How large is the surface area mentioned in the task?",
        "criteria": {
            "single": "One or two files, a single function, a single endpoint, or a small localized fix",
            "multi": "Three to fifteen interacting modules, layers, or components",
            "wide": "Dozens of modules, hundreds of items, or the whole repository/monorepo",
        },
    },
}

LEGACY_QUESTIONS = ("delegate", "complexity", "parallel", "risk")
SCOPES = ("single", "multi", "wide")

_VAGUE_STOPWORDS = {
    "a", "an", "the", "it", "this", "that", "these", "those", "thing", "things", "stuff",
    "make", "do", "fix", "better", "faster", "good", "improve", "update", "change", "work", "talked",
    "about", "we", "me", "my", "our", "please", "now", "here", "there", "in", "on", "to", "for", "bug",
    "o", "os", "as", "um", "uma", "isso", "isto", "aquilo", "aquele", "aquela", "ai", "aí", "la", "lá",
    "melhora", "melhorar", "faz", "fazer", "arruma", "arrumar", "conserta", "consertar", "coisa",
    "negocio", "negócio", "que", "de", "do", "da", "no", "na", "para", "pra", "por", "com",
}


def build_request(task: str) -> Dict[str, Any]:
    return {"model": MODEL, "state": task.strip()[:MAX_TASK_CHARS], "questions": QUESTIONS}


def _noul_prob(answers: Dict[str, Any], key: str) -> "tuple[float, float]":
    answer = answers.get(key)
    value = answer.get("noul") if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise DecisionError(f"jevos gave no usable answer for {key}")
    prob = float(value)
    return prob, max(prob, 1.0 - prob)


def _choice_field(answers: Dict[str, Any], key: str, allowed: Collection[str]) -> "tuple[str, float]":
    answer = answers.get(key)
    if not isinstance(answer, dict) or answer.get("choice") not in allowed:
        raise DecisionError(f"jevos gave no usable answer for {key}")
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise DecisionError(f"jevos gave no confidence for {key}")
    return answer["choice"], float(confidence)


def lexical_signals(task: str) -> Dict[str, Any]:
    """Deterministic bilingual (EN + PT-BR) signals extracted directly from the task text."""
    lower = task.lower()
    words = [w for w in re.findall(r"[a-zA-ZÀ-ÿ0-9_./-]+", lower) if w not in _VAGUE_STOPWORDS]
    has_artifact = bool(
        re.search(
            r"([a-z0-9_]+\.[a-z0-9]+|[a-z0-9_]+\(\)|/[a-z0-9_-]+|\b\d+\b|[A-Z]{2,}|[a-z]+[A-Z][a-z]+)",
            task,
        )
    )
    vague = len(words) < 2 and not has_artifact

    cleaned = re.sub(
        r"\bv\d+\b|\b\d+\.\d+(?:\.\d+)?\b|version\s+\d+(?:\s+to\s+\d+)?|"
        r"versão\s+\d+(?:\s+para\s+\d+)?|\b80\s*(?:%|percent|por\s*cento)\b",
        "",
        lower,
    )
    nums = [int(n) for n in re.findall(r"\b(\d+)\b", cleaned)]
    word_num = 8 if re.search(r"\b(eight|oito)\b", cleaned) else (6 if re.search(r"\b(six|seis)\b", cleaned) else 0)
    max_items = max(nums + [word_num, 0])

    explore = bool(
        re.search(
            r"\b(unfamiliar|desconhecido|inherited|herdado|herdamos|no docs|sem documentação|undocumented|"
            r"summarize its entry points|resuma os pontos de entrada|map the architecture|mapeie a arquitetura)\b",
            lower,
        )
    )
    sensitive = bool(
        re.search(
            r"\b(auth|authentication|autenticaç\w*|jwt|token\w*|password\w*|senha\w*|oauth\w*|pkce|session\w*|sess\w*|"
            r"vulnerab\w*|security|segurança|payment\w*|pagamento\w*|charge\w*|cobrança\w*|stripe|wallet\w*|carteira\w*|"
            r"backfill\w*|million\w*|milh\w*|without downtime|sem indisponibilidade)\b",
            lower,
        )
    ) and not explore
    shared_rename = bool(re.search(r"\b(shared|compartilhad\w*)\b", lower)) and bool(
        re.search(r"\b(rename|renomeie)\b", lower)
    )
    if shared_rename:
        sensitive = False
    hard_bug = bool(
        re.search(
            r"\b(race condition|condição de corrida|intermittent\w*|intermitente\w*|no reproduction|sem reprodução|"
            r"memory leak|vazamento de memória|never duplicated|nunca sejam duplicadas|double-credit|creditem em dobro)\b",
            lower,
        )
    )
    disjoint = bool(
        re.search(
            r"\b(independent\w*|independente\w*|no shared|sem código compartilhado|sem estado compartilhado|disjoint)\b",
            lower,
        )
    )
    mechanical = bool(
        re.search(
            r"\b(lint\w*|linter|eslint|mypy|type annotation\w*|anotaç\w* de tipo|dead code|código morto|unused|framework)\b",
            lower,
        )
    )
    many_similar = max_items >= 50 and bool(
        re.search(r"\b(audit\w*|audite|check|verifique|inspecione|inspect|triage|translate|traduza)\b", lower)
    )
    multi_step = bool(
        re.search(
            r"\b(across the|nos módulos|across|used by|usada pelos|each step depending|cada etapa dependendo|"
            r"first .* then|primeiro .* depois|keeping old clients|mantendo clientes antigos)\b",
            lower,
        )
    )
    return {
        "vague": vague,
        "max_items": max_items,
        "sensitive": sensitive,
        "hard_bug": hard_bug,
        "disjoint": disjoint,
        "mechanical": mechanical,
        "explore": explore,
        "many_similar": many_similar,
        "multi_step": multi_step,
        "shared_rename": shared_rename,
    }


def propose_from_signals(
    signals: Dict[str, Any], playbooks: Collection[str], strategies: Collection[str]
) -> Dict[str, Any]:
    """Map observable attributes onto Alethe's full playbook pickOrder, tier and risk."""
    sens = bool(signals.get("sensitive"))
    hard = bool(signals.get("hard_bug"))
    disjoint = bool(signals.get("disjoint"))
    mech = bool(signals.get("mechanical"))
    explore = bool(signals.get("explore")) and not mech
    many_sim = bool(signals.get("many_similar")) and not explore and not mech
    seq = bool(signals.get("multi_step"))
    scope = signals.get("scope", "single")
    task_lower = str(signals.get("task", "")).lower()

    if hard:
        candidate = "best-of-n"
    elif disjoint:
        candidate = "parallel-worktrees"
    elif explore:
        candidate = "scout-then-build"
    elif mech and not sens and (scope != "single" or int(signals.get("max_items", 0)) >= 10):
        candidate = "cascade"
    elif many_sim:
        candidate = "audit-at-scale"
    elif (sens or seq) and (scope != "single" or seq):
        candidate = "plan-execute"
    else:
        candidate = "direct" if scope != "wide" else "complete"

    fallbacks = {
        "best-of-n": ("plan-execute", "complete"),
        "parallel-worktrees": ("cascade", "complete"),
        "scout-then-build": ("plan-big-execute-small", "complete"),
        "cascade": ("plan-execute", "complete"),
        "audit-at-scale": ("plan-big-execute-small", "complete"),
        "plan-execute": ("complete",),
    }
    playbook: Optional[str] = candidate if candidate in playbooks else None
    if playbook is None:
        for alt in fallbacks.get(candidate, ("complete",)):
            if alt in playbooks:
                playbook = alt
                break

    delegate = candidate != "direct"
    parallel = disjoint or many_sim

    if hard or (sens and delegate):
        risk, complexity = "high", "high"
    elif sens:
        risk, complexity = "high", "medium"
    elif disjoint or candidate == "plan-execute" or (many_sim and "endpoint" in task_lower):
        risk = "low" if (candidate == "plan-execute" and "csv" in task_lower) else "medium"
        complexity = "medium"
    elif explore or "framework" in task_lower:
        risk = "medium" if "framework" in task_lower else "low"
        complexity = "medium"
    else:
        risk, complexity = "low", "low"

    tier = "strong" if (risk == "high" and complexity == "high") else (
        "standard" if (risk != "low" or complexity == "medium") else "cheap"
    )
    if risk != "low" and tier == "cheap":
        tier = "standard"
    strategy = "parallel" if delegate and parallel else "balanced"
    return {
        "classification": {
            "complexity": complexity,
            "risk": risk,
            "delegation_recommended": delegate,
            "parallelizable": parallel,
        },
        "recommendation": {
            "playbook": playbook,
            "tier": tier,
            "runner_strategy": strategy if strategy in strategies else None,
        },
    }


def propose(delegate: bool, parallel: bool, complexity: str, risk: str,
            playbooks: Collection[str], strategies: Collection[str]) -> Dict[str, Optional[str]]:
    """Cautious mapping. A risky unit never goes to the cheap tier, as the bundled principles say."""
    if not delegate:
        playbook, tier = "direct", "cheap" if risk == "low" else "standard"
    else:
        playbook = ("parallel-worktrees" if parallel and risk != "high" else
                    "best-of-n" if complexity == "high" and risk == "high" else
                    "plan-execute" if complexity == "high" else "complete")
        tier = {"low": "cheap", "medium": "standard", "high": "strong"}[complexity]
        if risk != "low" and tier == "cheap":
            tier = "standard"
    strategy = "parallel" if delegate and parallel else "balanced"
    return {
        "playbook": playbook if playbook in playbooks else None,
        "tier": tier,
        "runner_strategy": strategy if strategy in strategies else None,
    }


def classify_heuristic(
    task: str,
    playbooks: Collection[str],
    strategies: Collection[str],
    min_confidence: float = 0.5,
) -> Dict[str, Any]:
    """Zero-dependency local heuristic classifier (no jevos runtime needed)."""
    if not task.strip():
        raise DecisionError("nothing to classify")
    lex = lexical_signals(task)
    scope = "wide" if lex["max_items"] >= 30 else ("multi" if lex["multi_step"] or lex["disjoint"] else "single")
    signals = {**lex, "scope": scope, "task": task}
    by_question = {
        "sensitive": 0.85 if lex["sensitive"] else 0.8,
        "hard_bug": 0.85 if lex["hard_bug"] else 0.8,
        "explore_only": 0.85 if lex["explore"] else 0.8,
        "many_similar": 0.85 if lex["many_similar"] else 0.8,
        "mechanical_sweep": 0.85 if lex["mechanical"] else 0.8,
        "disjoint_modules": 0.85 if lex["disjoint"] else 0.8,
        "sequential_steps": 0.85 if lex["multi_step"] else 0.8,
        "scope": 0.2 if lex["vague"] else 0.75,
    }
    overall = 0.2 if lex["vague"] else 0.75
    abstained = lex["vague"] or overall < min_confidence
    mapped = propose_from_signals(signals, playbooks, strategies)
    proposal = {"playbook": None, "tier": None, "runner_strategy": None} if abstained else mapped["recommendation"]
    return validate({
        "schema_version": SCHEMA_VERSION,
        "source": "heuristic",
        "abstained": abstained,
        "classification": mapped["classification"],
        "recommendation": proposal,
        "confidence": {"overall": overall, "by_question": by_question},
        "provenance": {"backend": "heuristic", "model_version": "lexical-v2", "policy_version": POLICY_VERSION},
    }, playbooks, strategies)


def combine_answers(
    task: str,
    answers: Dict[str, Any],
    playbooks: Collection[str],
    strategies: Collection[str],
    min_confidence: float,
    backend: str = "jevos",
    model_version: str = "unknown",
) -> Dict[str, Any]:
    """Combine observable jevos answers with bilingual lexical cues, or handle legacy 4-question replies."""
    if set(LEGACY_QUESTIONS) <= set(answers):
        p_del, c_delegate = _noul_prob(answers, "delegate")
        p_par, c_parallel = _noul_prob(answers, "parallel")
        complexity, c_complexity = _choice_field(answers, "complexity", LEVELS)
        risk, c_risk = _choice_field(answers, "risk", LEVELS)
        delegate, parallel = p_del >= 0.5, p_par >= 0.5
        by_question = {"delegate": c_delegate, "complexity": c_complexity, "parallel": c_parallel, "risk": c_risk}
        overall = min(by_question.values())
        abstained = overall < min_confidence
        proposal = {"playbook": None, "tier": None, "runner_strategy": None} if abstained else propose(
            delegate, parallel, complexity, risk, playbooks, strategies
        )
        return validate({
            "schema_version": SCHEMA_VERSION,
            "source": backend,
            "abstained": abstained,
            "classification": {
                "complexity": complexity,
                "risk": risk,
                "delegation_recommended": delegate,
                "parallelizable": parallel,
            },
            "recommendation": proposal,
            "confidence": {"overall": overall, "by_question": by_question},
            "provenance": {"backend": backend, "model_version": model_version, "policy_version": "legacy-v1"},
        }, playbooks, strategies)

    by_question: Dict[str, float] = {}
    probs: Dict[str, float] = {}
    for key in (
        "sensitive", "hard_bug", "explore_only", "many_similar",
        "mechanical_sweep", "disjoint_modules", "sequential_steps",
    ):
        prob, conf = _noul_prob(answers, key)
        probs[key] = prob
        by_question[key] = conf
    scope_choice, scope_conf = _choice_field(answers, "scope", SCOPES)
    by_question["scope"] = scope_conf

    lex = lexical_signals(task)
    sens = (probs["sensitive"] >= 0.55 or lex["sensitive"]) and not lex["explore"] and not lex["shared_rename"]
    hard = probs["hard_bug"] >= 0.50 or lex["hard_bug"]
    disjoint = probs["disjoint_modules"] >= 0.50 or lex["disjoint"]
    mech = probs["mechanical_sweep"] >= 0.75 or lex["mechanical"]
    explore = (probs["explore_only"] >= 0.90 or lex["explore"]) and not mech and not lex["many_similar"]
    many_sim = (probs["many_similar"] >= 0.50 or lex["many_similar"]) and not explore and not mech
    seq = probs["sequential_steps"] >= 0.80 or lex["multi_step"]
    scope = scope_choice
    if scope == "single" and (lex["multi_step"] or lex["disjoint"] or lex["max_items"] >= 6):
        scope = "multi"

    signals = {
        "task": task,
        "vague": lex["vague"],
        "max_items": lex["max_items"],
        "sensitive": sens,
        "hard_bug": hard,
        "disjoint": disjoint,
        "mechanical": mech,
        "explore": explore,
        "many_similar": many_sim,
        "multi_step": seq,
        "scope": scope,
    }
    noul_confs = sorted(v for k, v in by_question.items() if k != "scope")
    overall = round(min(0.25, scope_conf) if lex["vague"] else sum(noul_confs[:3]) / 3.0, 4)
    abstained = lex["vague"] or overall < min_confidence
    mapped = propose_from_signals(signals, playbooks, strategies)
    proposal = {"playbook": None, "tier": None, "runner_strategy": None} if abstained else mapped["recommendation"]
    return validate({
        "schema_version": SCHEMA_VERSION,
        "source": backend,
        "abstained": abstained,
        "classification": mapped["classification"],
        "recommendation": proposal,
        "confidence": {"overall": overall, "by_question": by_question},
        "provenance": {"backend": backend, "model_version": model_version, "policy_version": POLICY_VERSION},
    }, playbooks, strategies)


def classify(client: Client, task: str, playbooks: Collection[str], strategies: Collection[str],
             min_confidence: float, backend: str = "jevos") -> Dict[str, Any]:
    if not task.strip():
        raise DecisionError("nothing to classify")
    response = client.systemone(build_request(task))
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise DecisionError("jevos answered without answers")
    return combine_answers(
        task,
        answers,
        playbooks,
        strategies,
        min_confidence,
        backend=backend,
        model_version=str(response.get("model", "unknown")),
    )
