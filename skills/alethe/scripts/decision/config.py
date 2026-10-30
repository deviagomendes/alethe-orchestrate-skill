"""Decision layer settings, kept beside the Alethe policy file. `off` is the default."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from .contracts import DecisionError

MODES = ("off", "shadow")  # "auto" comes with the adaptive router, not here
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "mode": "off",
    "endpoint": "http://127.0.0.1:8017",
    "timeoutSeconds": 5.0,
    "minConfidence": 0.5,
    "installDir": None,
    "binary": None,
}


def config_path(policy_path: Path) -> Path:
    return policy_path.with_name("alethe-decision.json")


def data_dir(policy_path: Path) -> Path:
    return policy_path.with_name("decision")


def local_endpoint(endpoint: str) -> str:
    """The runtime only ever listens on this machine; refuse anything else."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS or not parsed.port:
        raise DecisionError("the endpoint must be http://127.0.0.1:<port> (or localhost)")
    return f"http://{parsed.hostname if parsed.hostname != '::1' else '[::1]'}:{parsed.port}"


def load(policy_path: Path) -> Dict[str, Any]:
    """Saved settings over the defaults. A missing or broken file means off."""
    config = dict(DEFAULTS)
    try:
        saved = json.loads(config_path(policy_path).read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            config.update({key: saved[key] for key in DEFAULTS if key in saved})
        local_endpoint(config["endpoint"])
        float(config["timeoutSeconds"]), float(config["minConfidence"])
    except (OSError, ValueError, TypeError, DecisionError):
        return dict(DEFAULTS)
    if config["mode"] not in MODES:
        config["mode"] = "off"
    return config


def save(policy_path: Path, config: Dict[str, Any]) -> None:
    path = config_path(policy_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".decision-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({key: config[key] for key in DEFAULTS}, stream, indent=2)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
