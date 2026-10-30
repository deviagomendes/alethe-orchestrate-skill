"""`orchestrate.py decision ...`: status, setup, shadow, off, stop, classify, benchmark."""
import json
import platform
import time
from pathlib import Path
from typing import Any, Collection, Dict

from . import classify_task, manager, setup
from . import config as settings
from .client import Client
from .contracts import DecisionError

FIXTURES = Path(__file__).resolve().parents[2] / "references" / "decision-benchmark.json"
FIELDS = {"delegate": ("classification", "delegation_recommended"), "parallel": ("classification", "parallelizable"),
          "complexity": ("classification", "complexity"), "risk": ("classification", "risk")}


def status(policy_path: Path) -> Dict[str, Any]:
    config = settings.load(policy_path)
    client = Client(config["endpoint"], float(config["timeoutSeconds"]))
    try:
        runtime = setup.runtime_for_platform()[0]
    except DecisionError:
        runtime = None
    return {
        "mode": config["mode"], "endpoint": config["endpoint"], "supportedPlatform": runtime is not None,
        "installed": bool(config["binary"] and Path(config["binary"]).is_file()),
        "running": manager.healthy(client), "managedPid": manager.managed_pid(settings.data_dir(policy_path)),
        "python": platform.python_version(), "config": str(settings.config_path(policy_path)),
    }


def do_setup(policy_path: Path, yes: bool, endpoint: str) -> Dict[str, Any]:
    config = settings.load(policy_path)
    if endpoint:
        config["endpoint"] = settings.local_endpoint(endpoint)
        settings.save(policy_path, config)
        return {"endpoint": config["endpoint"], "note": "using a runtime you already run; Alethe will not start or stop it"}
    offer = setup.plan()
    if not yes:
        return {"consent": "required", "plan": offer, "next": "ask the user, then run `decision setup --yes`"}
    installed = setup.install(settings.data_dir(policy_path))
    config.update(installDir=installed["installDir"], binary=installed["binary"])
    settings.save(policy_path, config)
    return {"installed": installed, "mode": config["mode"], "next": "run `decision shadow` to turn it on"}


def benchmark(policy_path: Path, fixtures: Path, playbooks: Collection[str], strategies: Collection[str]) -> Dict[str, Any]:
    cases = json.loads(fixtures.read_text(encoding="utf-8"))["cases"]
    rows, hits, total = [], 0, 0
    for index, case in enumerate(cases):
        started = time.monotonic()
        row: Dict[str, Any] = {"id": case["id"], "first": index == 0}
        try:
            proposal = classify_task(policy_path, case["task"], playbooks, strategies, wait=60.0 if index == 0 else None)
            row["agree"] = {}
            for name, (group, key) in FIELDS.items():
                if name in case["expect"]:
                    ok = proposal[group][key] == case["expect"][name]
                    row["agree"][name] = ok
                    hits, total = hits + ok, total + 1
            row["abstained"] = proposal["abstained"]
            row["playbook"] = proposal["recommendation"]["playbook"]
        except DecisionError as error:
            row["error"] = str(error)
        row["ms"] = round((time.monotonic() - started) * 1000)
        rows.append(row)
    return {"cases": len(rows), "errors": sum("error" in r for r in rows), "fieldsMatchingFixture": hits,
            "fieldsCompared": total, "note": "fixture labels are reviewer judgement, not ground truth; the first row includes cold start",
            "rows": rows}


def run(args: Any, policy_path: Path, playbooks: Collection[str], strategies: Collection[str]) -> Dict[str, Any]:
    action = args.decision_action
    if action == "status":
        return status(policy_path)
    if action == "setup":
        return do_setup(policy_path, args.yes, args.endpoint)
    if action in ("shadow", "off"):
        config = settings.load(policy_path)
        config["mode"] = action
        settings.save(policy_path, config)
        return {"mode": action}
    if action == "stop":
        config = settings.load(policy_path)
        return {"stopped": manager.stop(settings.data_dir(policy_path), Client(config["endpoint"], 2.0))}
    if action == "classify":
        return classify_task(policy_path, args.task, playbooks, strategies, wait=args.wait)
    return benchmark(policy_path, args.fixtures or FIXTURES, playbooks, strategies)
