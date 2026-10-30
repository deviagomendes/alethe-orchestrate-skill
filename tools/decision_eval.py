#!/usr/bin/env python3
"""Score the decision layer against baselines on references/decision-corpus.json.

Needs the jevos runtime (`orchestrate.py decision setup --yes` once). Starts it if it is not running
and stops it again only if this script started it. Prints one table per split; `--out file.json`
keeps the raw jevos answers so the scoring can be re-run without the runtime (`--raw file.json`).
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "alethe" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import orchestrate  # noqa: E402
from decision import classifier, config as settings, manager  # noqa: E402
from decision.client import Client  # noqa: E402

PLAYBOOKS = ["complete", "direct", "plan-execute", "plan-big-execute-small", "scout-then-build", "advisor-escalation",
             "cascade", "parallel-worktrees", "best-of-n", "review-gate", "audit-at-scale"]
STRATEGIES = ["economy", "balanced", "parallel"]
TIERS = {"cheap": 0, "standard": 1, "strong": 2}


LEGACY_QUESTIONS = {
    "delegate": {
        "type": "noul",
        "instructions": "Is delegating this software task to one or more worker agents justified, rather than doing it directly?",
    },
    "complexity": {
        "type": "choice",
        "instructions": "Estimate the software engineering complexity of this task, not its urgency.",
        "criteria": {
            "low": "Small, explicit, localized change",
            "medium": "Several steps with manageable dependencies",
            "high": "Uncertain architecture, several interacting components or heavy reasoning",
        },
    },
    "parallel": {
        "type": "noul",
        "instructions": "Is there evidence that at least two independent units of this task can progress in parallel without touching the same files?",
    },
    "risk": {
        "type": "choice",
        "instructions": "Assess the risk of regression, data loss or security impact.",
        "criteria": {
            "low": "Limited impact and easy rollback",
            "medium": "Moderate impact, needs tests",
            "high": "Sensitive authentication, data loss, security or a broad breaking change",
        },
    },
}


def jevos_legacy_flags(answers):
    return (
        answers["delegate"]["noul"] >= 0.5,
        answers["parallel"]["noul"] >= 0.5,
        answers["complexity"]["choice"],
        answers["risk"]["choice"],
    )


def keyword_flags(task):
    """A deliberately naive baseline: a handful of words, no model."""
    t = task.lower()
    big = bool(re.search(r"\b(\d{2,}|eight|oito|dozen)\b", t)) or any(
        w in t for w in ("across", "all ", "every", "todos", "whole", "inherited", "unfamiliar", "desconhecido"))
    risky = any(w in t for w in ("auth", "token", "jwt", "password", "payment", "pagamento", "production", "produção",
                                 "security", "migration", "migração", "migre", "customer"))
    hard = any(w in t for w in ("race", "corrida", "intermittent", "no reproduction", "without downtime", "sem indisponibilidade"))
    parallel = "independent" in t or "independentes" in t or bool(re.search(r"\b\d{2,}\b", t))
    risk = "high" if risky and (hard or "production" in t or "produção" in t) else ("medium" if risky else "low")
    return big or hard, parallel, "high" if hard else ("medium" if big else "low"), risk


def propose(flags):
    return classifier.propose(*flags, PLAYBOOKS, STRATEGIES)


def propose_v2(case, raw):
    entry = raw[case["id"]]
    obs = entry["v2"] if isinstance(entry, dict) and "v2" in entry else entry
    res = classifier.combine_answers(case["task"], obs, PLAYBOOKS, STRATEGIES, 0.5)
    return res["recommendation"]


def propose_legacy(case, raw):
    entry = raw[case["id"]]
    leg = entry["v1"] if isinstance(entry, dict) and "v1" in entry else entry
    if "delegate" not in leg:
        return {"playbook": None, "tier": "standard"}
    return propose(jevos_legacy_flags(leg))


SYSTEMS = {
    "always complete/standard": lambda case, raw: {"playbook": "complete", "tier": "standard"},
    "always direct/cheap": lambda case, raw: {"playbook": "direct", "tier": "cheap"},
    "keyword baseline": lambda case, raw: propose(keyword_flags(case["task"])),
    "jevos v1 (4 policy q)": propose_legacy,
    "heuristic v2 (no runtime)": lambda case, raw: classifier.classify_heuristic(
        case["task"], PLAYBOOKS, STRATEGIES
    )["recommendation"],
    "hybrid v2 (jevos+lex)": propose_v2,
}


def score(cases, system, raw):
    totals = dict(best=0, ok=0, tier=0, over=0, under=0, direct_for_delegate=0)
    for case in cases:
        expect, got = case["expect"], system(case, raw)
        low, high = (f(TIERS[t] for t in expect["tiers"]) for f in (min, max))
        totals["best"] += got["playbook"] in expect["best"]
        totals["ok"] += got["playbook"] in expect["ok"]
        totals["tier"] += low <= TIERS[got["tier"]] <= high
        totals["over"] += TIERS[got["tier"]] > high
        totals["under"] += TIERS[got["tier"]] < low
        totals["direct_for_delegate"] += got["playbook"] == "direct" and expect["delegate"]
    return totals


def fetch(cases):
    config = settings.load(orchestrate.default_policy_path())
    data_dir = settings.data_dir(orchestrate.default_policy_path())
    client = Client(config["endpoint"], 30)
    started = not manager.healthy(client)
    manager.ensure(config, data_dir, client, 120)
    try:
        out = {}
        for c in cases:
            v1 = client.systemone({"model": classifier.MODEL, "state": c["task"], "questions": LEGACY_QUESTIONS})["answers"]
            v2 = client.systemone(classifier.build_request(c["task"]))["answers"]
            out[c["id"]] = {"v1": v1, "v2": v2}
        return out
    finally:
        if started:
            manager.stop(data_dir, client)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--raw", help="reuse raw answers saved with --out instead of calling the runtime")
    parser.add_argument("--out", help="save the raw jevos answers here")
    args = parser.parse_args()
    corpus = json.loads((SCRIPTS.parent / "references" / "decision-corpus.json").read_text("utf-8"))["cases"]
    cases = [c for c in corpus if "abstain" not in c["expect"]]
    raw = json.loads(Path(args.raw).read_text("utf-8")) if args.raw else fetch(corpus)
    if args.out:
        Path(args.out).write_text(json.dumps(raw, indent=1), encoding="utf-8")
    for split in ("dev", "test", "all", "pt"):
        subset = [c for c in cases if c["lang"] == "pt"] if split == "pt" else [
            c for c in cases if split == "all" or c["split"] == split
        ]
        print(f"\n{split}: {len(subset)} cases")
        print(f"{'':26s} best  ok  tier  over  under  direct-for-delegate")
        for name, system in SYSTEMS.items():
            s = score(subset, system, raw)
            print(f"{name:26s} {s['best']:4d} {s['ok']:3d} {s['tier']:5d} {s['over']:5d} {s['under']:6d} {s['direct_for_delegate']:8d}")

    vague_cases = [c for c in corpus if "abstain" in c["expect"]]
    vague_hits = sum(
        classifier.combine_answers(
            c["task"],
            raw[c["id"]]["v2"] if "v2" in raw[c["id"]] else raw[c["id"]],
            PLAYBOOKS,
            STRATEGIES,
            0.5,
        )["abstained"]
        for c in vague_cases
    )
    false_abstains = sum(
        classifier.combine_answers(
            c["task"],
            raw[c["id"]]["v2"] if "v2" in raw[c["id"]] else raw[c["id"]],
            PLAYBOOKS,
            STRATEGIES,
            0.5,
        )["abstained"]
        for c in cases
    )
    print(f"\nabstain: {vague_hits}/{len(vague_cases)} vague tasks abstained; {false_abstains}/{len(cases)} clear tasks abstained")


if __name__ == "__main__":
    main()
