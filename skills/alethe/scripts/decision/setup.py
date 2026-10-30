"""Opt-in install of the jevos runtime: pinned release, SHA-256 checked, extracted without escaping its folder.

Nothing is downloaded or run until the user agreed (`--yes`). Weights and binary live in the
user's data folder, never in a project.
"""
import hashlib
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Optional

from .contracts import DecisionError

RELEASE = "jevos-v5"
BASE_URL = f"https://github.com/feder-cr/jev/releases/download/{RELEASE}/"
WEIGHTS = "jevos-v5-openvino-int8.zip"
CHECKSUMS = "SHA256SUMS.txt"
RUNTIMES = {  # (system, machine) -> (archive, binary name); sizes are the release's, to be shown before asking
    ("Windows", "AMD64"): ("jev-windows-x64.zip", "jev.exe"),
    ("Linux", "x86_64"): ("jev-linux-x64.tar.gz", "jev"),
    ("Darwin", "arm64"): ("jev-macos-arm64.tar.gz", "jev"),
}
MAX_DOWNLOAD_BYTES = 1 << 30
MAX_EXTRACTED_BYTES = 3 << 30
Fetch = Callable[[str, Path, int], None]


def runtime_for_platform() -> "tuple[str, str]":
    key = (platform.system(), platform.machine())
    if key not in RUNTIMES:
        raise DecisionError(f"no jevos runtime for {key[0]} {key[1]}; the Alethe flow is unchanged")
    return RUNTIMES[key]


def plan() -> Dict[str, Any]:
    archive, binary = runtime_for_platform()
    return {
        "release": RELEASE, "source": BASE_URL, "runtime": archive, "weights": WEIGHTS, "binary": binary,
        "downloadMB": "about 20-26 (runtime) + 600 (weights)", "memory": "about 1-1.4 GB RAM while running",
        "listens": "127.0.0.1 only; the task text never leaves this machine",
    }


def download(url: str, target: Path, limit: int) -> None:
    if not url.startswith("https://"):
        raise DecisionError("downloads must use https")
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:  # noqa: S310 (https checked)
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise DecisionError(f"{url} is larger than {limit} bytes")
                out.write(chunk)
    except OSError as error:
        raise DecisionError(f"download failed: {error}") from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_hash(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and len(parts[0]) == 64:
            return parts[0].lower()
    raise DecisionError(f"{CHECKSUMS} has no entry for {name}")


def _checked_target(root: Path, name: str) -> Path:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise DecisionError(f"unsafe path in archive: {name}")
    target = (root / Path(*pure.parts)).resolve()
    if root.resolve() != target and root.resolve() not in target.parents:
        raise DecisionError(f"unsafe path in archive: {name}")
    return target


def safe_extract(archive: Path, root: Path) -> None:
    """Extract members one by one; refuse links, devices, escaping paths and oversized archives."""
    root.mkdir(parents=True, exist_ok=True)
    total = 0
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise DecisionError(f"link in archive: {info.filename}")
                target = _checked_target(root, info.filename)
                total += info.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise DecisionError("archive expands to too much data")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, target.open("wb") as out:
                        out.write(source.read())
    else:
        with tarfile.open(archive) as bundle:
            for member in bundle:
                if not (member.isfile() or member.isdir()):
                    raise DecisionError(f"link or special file in archive: {member.name}")
                target = _checked_target(root, member.name)
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    raise DecisionError("archive expands to too much data")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    with target.open("wb") as out:
                        out.write(source.read() if source else b"")
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)


def find_binary(root: Path, name: str) -> Optional[Path]:
    return next((path for path in sorted(root.rglob(name)) if path.is_file()), None)


def install(data_dir: Path, fetch: Fetch = download) -> Dict[str, Any]:
    """Download, verify and unpack runtime and weights. `fetch` is replaceable for tests."""
    archive, binary_name = runtime_for_platform()
    install_dir = data_dir / RELEASE
    staging = data_dir / f".staging-{RELEASE}"
    staging.mkdir(parents=True, exist_ok=True)
    sums = staging / CHECKSUMS
    fetch(BASE_URL + CHECKSUMS, sums, 1 << 20)
    sums_text = sums.read_text(encoding="utf-8", errors="replace")
    for name in (archive, WEIGHTS):
        path = staging / name
        fetch(BASE_URL + name, path, MAX_DOWNLOAD_BYTES)
        if sha256(path) != expected_hash(sums_text, name):
            path.unlink(missing_ok=True)
            raise DecisionError(f"checksum mismatch for {name}; nothing was installed")
    safe_extract(staging / archive, install_dir)
    binary = find_binary(install_dir, binary_name)
    if binary is None:
        raise DecisionError(f"{binary_name} not found in {archive}")
    safe_extract(staging / WEIGHTS, binary.parent)
    if not (binary.parent / "model").is_dir():
        raise DecisionError("the weights archive did not create model/")
    binary.chmod(0o755)
    for leftover in staging.iterdir():
        leftover.unlink()
    staging.rmdir()
    return {"installDir": str(install_dir), "binary": str(binary)}
