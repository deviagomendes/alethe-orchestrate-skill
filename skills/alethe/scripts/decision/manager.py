"""Start, reuse and stop the local jevos process. Only a process this module started is ever stopped."""
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .client import Client
from .contracts import DecisionError

READY_SECONDS = 60.0  # loading ~600 MB of weights; the lead is never blocked this long in shadow (see ensure)


def _pid_file(data_dir: Path) -> Path:
    return data_dir / "jev.pid"


def healthy(client: Client) -> bool:
    try:
        client.health()
        return True
    except DecisionError:
        return False


def _alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill would terminate the process on Windows
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def managed_pid(data_dir: Path) -> Optional[int]:
    try:
        pid = int(_pid_file(data_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def start(config: dict[str, Any], data_dir: Path, client: Client) -> None:
    """Launch the runtime if nothing answers on its port. Never touches a runtime that is already up."""
    if healthy(client):
        return
    binary = config.get("binary")
    if not binary or not Path(binary).is_file():
        raise DecisionError("jevos is not installed; run `decision setup`")
    port = urlparse(config["endpoint"]).port
    binary_path = Path(binary)
    data_dir.mkdir(parents=True, exist_ok=True)
    flags: dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)}
        if os.name == "nt" else {"start_new_session": True}
    )
    with (data_dir / "jev.log").open("ab") as log:
        process = subprocess.Popen(  # list argv, no shell
            [str(binary_path), "serve", "--host", "127.0.0.1", "--port", str(port),
             "--model-dir", str(binary_path.parent / "model")],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(binary_path.parent), **flags,
        )
    _pid_file(data_dir).write_text(str(process.pid), encoding="utf-8")


def ensure(config: dict[str, Any], data_dir: Path, client: Client, wait: float) -> None:
    """Make the runtime answer within `wait` seconds or raise; a cold start can exceed a small wait."""
    start(config, data_dir, client)
    deadline = time.monotonic() + wait
    while not healthy(client):
        if time.monotonic() >= deadline:
            raise DecisionError("jevos is still starting; this task continues without it")
        time.sleep(0.25)


def stop(data_dir: Path, client: Client) -> bool:
    """Stop the runtime this module started. The pid file alone is not trusted (pids get reused):
    the port must also still answer."""
    pid = managed_pid(data_dir)
    if pid is None or not healthy(client):
        return False
    if os.name == "nt":  # a windowless console process ignores a polite taskkill; the runtime only reads its weights
        stopped = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True).returncode == 0
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            stopped = False
    if stopped:
        _pid_file(data_dir).unlink(missing_ok=True)
    return stopped
