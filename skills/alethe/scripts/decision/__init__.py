"""Optional decision layer: a local classifier that proposes, and never decides.

`off` (the default) does nothing. `shadow` records what the classifier would propose next to what
Alethe actually did, and changes nothing. Every failure here ends in the native flow.
"""
import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Collection, Dict, Optional

from . import config as settings
from . import manager
from .classifier import classify, classify_heuristic
from .client import Client
from .contracts import DecisionError

RECORD_FILE = "decision.json"


def _client(config: Dict[str, Any]) -> Client:
    return Client(config["endpoint"], float(config["timeoutSeconds"]))


def classify_task(policy_path: Path, task: str, playbooks: Collection[str], strategies: Collection[str],
                  wait: Optional[float] = None) -> Dict[str, Any]:
    """Start the runtime if needed, then classify. Raises DecisionError and nothing else."""
    config = settings.load(policy_path)
    client = _client(config)
    manager.ensure(config, settings.data_dir(policy_path), client, config["timeoutSeconds"] if wait is None else wait)
    return classify(client, task, playbooks, strategies, float(config["minConfidence"]))


def advise_task(policy_path: Path, task: str, playbooks: Collection[str], strategies: Collection[str]) -> Dict[str, Any]:
    """Fast task advice for `playbook --pick --task`: uses warm jevos in shadow mode, or instant heuristic."""
    config = settings.load(policy_path)
    min_conf = float(config["minConfidence"])
    if config["mode"] == "shadow":
        client = _client(config)
        if manager.healthy(client):
            try:
                return classify(client, task, playbooks, strategies, min_conf)
            except DecisionError:
                pass
    return classify_heuristic(task, playbooks, strategies, min_conf)


def record_outcome(folder: Path, worker_id: str, mark: str, why: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """When `mark` records whether a worker was used or discarded, append the outcome to decision.json if present."""
    try:
        path = folder / RECORD_FILE
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return None
        outcomes = record.get("outcomes") if isinstance(record.get("outcomes"), dict) else {}
        outcomes[worker_id] = {
            "mark": mark,
            "why": why,
            "markedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        counts = {k: sum(1 for v in outcomes.values() if isinstance(v, dict) and v.get("mark") == k)
                  for k in ("used", "partial", "discarded")}
        record["outcomes"] = outcomes
        record["outcomeSummary"] = counts
        _write(path, record)
        return record
    except Exception:
        return None


def report(folders: Collection[Path]) -> Dict[str, Any]:
    """Compare shadow proposals with the playbook the lead ran and the worker marks, across folders."""
    marks = ("used", "partial", "discarded")
    groups = {name: dict.fromkeys(marks, 0) for name in ("agreed", "overridden", "unknown")}
    totals = {"records": 0, "fallback": 0, "abstained": 0, "agreed": 0, "overridden": 0, "unknown": 0}
    pairs: Dict[str, int] = {}
    for folder in folders:
        try:
            record = json.loads((Path(folder) / RECORD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        totals["records"] += 1
        if record.get("status") != "ok":
            totals["fallback"] += 1
            continue
        proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
        totals["abstained"] += bool(proposal.get("abstained"))
        agreed = (record.get("agreement") or {}).get("playbook")
        group = "unknown" if agreed is None else ("agreed" if agreed else "overridden")
        totals[group] += 1
        if group == "overridden":
            pair = f"{(proposal.get('recommendation') or {}).get('playbook')} -> {(record.get('actual') or {}).get('playbook')}"
            pairs[pair] = pairs.get(pair, 0) + 1
        for outcome in (record.get("outcomes") or {}).values():
            if isinstance(outcome, dict) and outcome.get("mark") in marks:
                groups[group][outcome["mark"]] += 1
    return {**totals, "marks": groups, "overrides": dict(sorted(pairs.items(), key=lambda kv: -kv[1])),
            "note": "agreed/overridden compares the proposal with the playbook passed to `new`; marks are worker outcomes"}


def _write(path: Path, value: Any) -> None:
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".decision-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def shadow(policy_path: Path, folder: Path, task: str, actual_playbook: Optional[str],
           playbooks: Collection[str], strategies: Collection[str]) -> Optional[Dict[str, Any]]:
    """In shadow mode, write decision.json beside the orchestration. Never raises, never changes the run."""
    try:
        if settings.load(policy_path)["mode"] != "shadow":
            return None
        record: Dict[str, Any] = {"version": 1, "mode": "shadow", "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                  "actual": {"playbook": actual_playbook}}
        started = time.monotonic()
        try:
            proposal = classify_task(policy_path, task, playbooks, strategies)
            record.update(status="ok", proposal=proposal)
            proposed = proposal["recommendation"]["playbook"]
            record["agreement"] = {"playbook": None if not (proposed and actual_playbook) else proposed == actual_playbook}
        except DecisionError as error:
            record.update(status="fallback", error=str(error))
        record["latencyMs"] = round((time.monotonic() - started) * 1000)
        _write(folder / RECORD_FILE, record)
        return record
    except Exception:  # the decision layer must never take a run down
        return None
