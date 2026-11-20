"""Resolve and verify build-tool wheels before either untrusted build starts.

Only the coordinator calls resolve(). It reads a verified PyPI sdist as *data*,
does not execute its backend, and resolves wheels-only dependencies. Dynamic
requirements unsupported by this strict resolver cause the pair to be skipped.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from . import build_lab
except ImportError:  # executed directly by scripts/gap_wheels.py
    import build_lab

PYPI = "https://pypi.org/pypi/"
FILES = "https://files.pythonhosted.org/packages/"
MAX_DOWNLOAD = 48_000_000
MAX_PROJECT = 131_072
# Keep one pair's lock within dispatch and result-artifact limits before any builds run.
MAX_PAIR_LOCK_BYTES = 24_000
UV_VERSION = "0.13.9"
RESOLVE_WORKERS = 4
RESOLVE_TIMEOUT = 120
PINNED_PIP = "pip==26.3"  # the builder itself must also be a hash-verified wheel
LINUX_REPAIR_TOOLS = build_lab.REPAIR_TOOLS
SHA = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
WHEEL = re.compile(r"[A-Za-z0-9._+!-]{1,200}\.whl\Z")
SDIST = re.compile(r"[A-Za-z0-9._-]{1,120}\.(?:tar\.gz|zip)\Z")


class LockError(ValueError):
    pass


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _from_pypi(url: str) -> bytes:
    if not url.startswith((FILES, PYPI)):
        raise LockError("a download is not a PyPI-hosted artifact")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wheelgap-build-lock/1"}),
                                timeout=40) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise LockError("a PyPI artifact exceeds the download limit")
    return data


def _metadata(name: str, version: str) -> dict:
    if not (NAME.fullmatch(canonical(name)) and re.fullmatch(r"[A-Za-z0-9._+!-]{1,100}", version)):
        raise LockError("invalid package name or version")
    try:
        data = json.loads(_from_pypi(f"{PYPI}{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"))
        if not isinstance(data, dict) or not isinstance(data.get("urls"), list):
            raise LockError("PyPI metadata is not an object with files")
        return data
    except (OSError, ValueError, TimeoutError) as exc:
        raise LockError(f"cannot read PyPI release metadata: {exc}") from exc


def _project_requirements(archive: bytes, filename: str, target: str | None = None) -> list[str]:
    """Read only the one top-level pyproject.toml; never extract or execute archive members."""
    candidates = []
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                for member in z.infolist():
                    if len(Path(member.filename).parts) == 2 and member.filename.endswith("/pyproject.toml"):
                        if member.file_size > MAX_PROJECT:
                            raise LockError("pyproject.toml is too large")
                        candidates.append(z.read(member, pwd=None))
        else:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    if len(Path(member.name).parts) == 2 and member.name.endswith("/pyproject.toml"):
                        if not member.isfile() or member.size > MAX_PROJECT:
                            raise LockError("unsafe pyproject.toml member")
                        file = tar.extractfile(member)
                        if file is None:
                            raise LockError("cannot read project metadata")
                        candidates.append(file.read(MAX_PROJECT + 1))
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        raise LockError(f"cannot read sdist: {exc}") from exc
    if not candidates:
        # PEP 517's defined legacy setuptools default: no pyproject.toml.
        return ["setuptools>=40.8.0", "wheel"]
    if len(candidates) != 1:
        raise LockError("the sdist has ambiguous pyproject.toml files")
    try:
        meta = tomllib.loads(candidates[0].decode("utf-8"))
        system = meta.get("build-system")
        if system is None:
            return ["setuptools>=40.8.0", "wheel"]
        reqs = system["requires"]
        if not isinstance(reqs, list) or not reqs or not all(isinstance(r, str) and r.strip() for r in reqs):
            raise LockError("build-system.requires must be a nonempty string list")
        if "build-backend" in system and (
            not isinstance(system["build-backend"], str) or not system["build-backend"]
        ):
            raise LockError("invalid PEP 517 build backend")
        if "build-backend" not in system and "wheel" not in reqs:
            # The specified requires use the defined legacy setuptools hook.
            reqs = [*reqs, "wheel"]
        if target is not None:
            # Known backend hooks can request these tools only after pip starts
            # building. With --no-build-isolation --no-index, nothing would
            # install them then. Resolve and hash-pin them in the coordinator.
            # Unknown hooks continue to fail closed instead of fetching tools
            # in the untrusted build environment.
            backend = system.get("build-backend", "")
            if backend == "scikit_build_core.build":
                reqs = [*reqs, "cmake>=3.15", "ninja>=1.10"]
            elif backend == "mesonpy":
                reqs = [*reqs, "ninja>=1.8"]
                if "linux-" in target or "musl-" in target:
                    reqs.append("patchelf>=0.11")
        # These go into a pip requirements file: never allow an sdist to add index options,
        # includes, URLs or lines that change the resolver's trust boundary.
        if any(
            "\n" in req or "\r" in req or req.lstrip().startswith(("-", "#"))
            or any(part in req for part in ("@", "://", "\\", "--", "/"))
            or re.fullmatch(r"\s*[A-Za-z0-9][A-Za-z0-9._-]{0,120}.*", req) is None
            for req in reqs
        ):
            raise LockError("unsupported or unsafe PEP 508 build requirement")
        return reqs
    except (UnicodeError, KeyError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise LockError(f"unsupported pyproject.toml build requirements: {exc}") from exc


def _platforms(target: str) -> list[str]:
    if "win-arm64" in target:
        return ["win_arm64"]
    if "win-amd64" in target:
        return ["win_amd64"]
    if "musl-aarch64" in target:
        return [f"musllinux_1_{n}_aarch64" for n in (2, 1)]
    if "linux-x86_64" in target:
        return [f"manylinux_2_{n}_x86_64" for n in range(28, 4, -1)] + [
            "manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"
        ]
    raise LockError("unknown target platform")


def _archive_info(item: dict, filename: str) -> dict:
    if not isinstance(item, dict) or item.get("filename") != filename:
        raise LockError("PyPI file metadata mismatch")
    url, dig = item.get("url"), item.get("digests")
    if not isinstance(url, str) or not url.startswith(FILES):
        raise LockError("non-PyPI wheel location")
    if not isinstance(dig, dict) or not isinstance(dig.get("sha256"), str) or not SHA.fullmatch(dig["sha256"]):
        raise LockError("wheel has no valid PyPI SHA-256")
    if urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] != filename:
        raise LockError("wheel URL and filename differ")
    return {"url": url, "sha256": dig["sha256"]}


def validate(lock: object) -> bool:
    if not isinstance(lock, dict) or set(lock) != {
        "schema", "name", "version", "target", "sdist", "sdist_url",
        "sdist_sha256", "wheels", "repair_wheels"
    } or lock["schema"] != 1:
        return False
    if not (isinstance(lock["name"], str) and NAME.fullmatch(lock["name"])
            and isinstance(lock["version"], str) and build_lab.VERSION.fullmatch(lock["version"])
            and isinstance(lock["target"], str) and lock["target"] in build_lab.TARGETS
            and isinstance(lock["sdist"], str) and SDIST.fullmatch(lock["sdist"])
            and isinstance(lock["sdist_url"], str) and lock["sdist_url"].startswith(FILES)
            and urllib.parse.urlsplit(lock["sdist_url"]).path.rsplit("/", 1)[-1] == lock["sdist"]
            and isinstance(lock["sdist_sha256"], str) and SHA.fullmatch(lock["sdist_sha256"])):
        return False
    repair = lock["repair_wheels"]
    if (not isinstance(lock["wheels"], list) or not 0 < len(lock["wheels"]) <= 100
            or not isinstance(repair, list) or len(repair) > 100):
        return False
    if bool(repair) != ("linux-" in lock["target"] or "musl-" in lock["target"]):
        return False
    for group in (lock["wheels"], repair):
        names = set()
        for item in group:
            if not isinstance(item, dict) or set(item) != {"name", "version", "filename", "url", "sha256"}:
                return False
            name, version, filename, url, sha = (item[k] for k in ("name", "version", "filename", "url", "sha256"))
            if not (isinstance(name, str) and NAME.fullmatch(name) and name not in names
                    and isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9._+!-]{1,100}", version)
                    and isinstance(filename, str) and WHEEL.fullmatch(filename)
                    and isinstance(url, str) and url.startswith(FILES)
                    and urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] == filename
                    and isinstance(sha, str) and SHA.fullmatch(sha)):
                return False
            names.add(name)
        if group and "pip" not in names:
            return False
    return not (repair and not {req.split("==")[0] for req in LINUX_REPAIR_TOOLS}.issubset(
        {w["name"] for w in repair}
    ))


TARGET_UV_PLATFORMS = {
    "3.14 win-arm64": "aarch64-pc-windows-msvc",
    "3.14t win-arm64": "aarch64-pc-windows-msvc",
    "3.15 win-amd64": "x86_64-pc-windows-msvc",
    "3.14t linux-x86_64": "x86_64-manylinux_2_28",
    "3.14 musl-aarch64": "aarch64-unknown-linux-musl",
    "3.15 linux-x86_64": "x86_64-manylinux_2_28",
}
PINNED = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)\Z")


def _resolve_wheels(requirements: list[str], target: str, work: Path, group: str) -> list[dict]:
    """Resolve markers for the actual target in pinned uv, then download *exactly* those wheels.

    pip downloads with --no-deps so it cannot re-evaluate transitive environment
    markers against the coordinator's Linux process.
    """
    req = work / f"{group}.in"
    req.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    output = work / f"{group}.txt"
    python_version = target.split()[0].removesuffix("t")
    digits = python_version.replace(".", "")
    abi = f"cp{digits}{'t' if target.split()[0].endswith('t') else ''}"
    uv_platform = TARGET_UV_PLATFORMS[target]
    try:
        version = subprocess.run(["uv", "--version"], capture_output=True, text=True,
                                 check=True, timeout=12)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LockError("the pinned uv resolver is missing") from exc
    if version.stdout.strip().split()[:2] != ["uv", UV_VERSION]:
        raise LockError(f"expected uv {UV_VERSION}, got {version.stdout.strip()}")
    command = [
        "uv", "pip", "compile", "--no-config", "--no-header",
        "--no-annotate", "--only-binary", ":all:", "--python-platform", uv_platform,
        "--python-version", python_version, "--index-url", "https://pypi.org/simple/",
        "--output-file", str(output), str(req),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=RESOLVE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise LockError("target-specific uv resolution timed out") from exc
    if result.returncode:
        raise LockError("target-specific binary resolution failed: " + (result.stderr or result.stdout)[-450:])
    pins = []
    for line in output.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PINNED.fullmatch(stripped)
        if match is None:
            raise LockError(f"the resolver returned unsupported pin: {stripped[:90]}")
        pins.append(f"{match.group(1)}=={match.group(2)}")
    if not 0 < len(pins) <= 100:
        raise LockError("the resolver did not produce a bounded set of wheel pins")
    folder = work / f"{group}-wheels"
    folder.mkdir()
    cmd = [sys.executable, "-m", "pip", "--isolated", "download", "--index-url",
           "https://pypi.org/simple/", "--only-binary=:all:", "--no-deps", "--no-cache-dir",
           "--disable-pip-version-check", "--python-version", python_version,
           "--implementation", "cp", "--abi", abi]
    for platform in _platforms(target):
        cmd.extend(["--platform", platform])
    cmd.extend(["-d", str(folder), *pins])
    try:
        downloaded = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=RESOLVE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise LockError("exact wheel downloads timed out") from exc
    if downloaded.returncode:
        raise LockError("a pinned dependency has no wheel for this target: " +
                        (downloaded.stderr or downloaded.stdout)[-450:])
    files = sorted(folder.glob("*.whl"))
    if len(files) != len(pins):
        raise LockError("wheel download count differs from the target lock")
    locked = []
    for file in files:
        parts = file.name.split("-")
        if len(parts) not in (5, 6):
            raise LockError("invalid resolved wheel filename")
        project, version = canonical(parts[0]), parts[1]
        data = _metadata(project, version)
        matches = [x for x in data["urls"] if isinstance(x, dict) and x.get("filename") == file.name]
        if len(matches) != 1:
            raise LockError("resolved wheel is not listed by PyPI")
        wheel_meta = _archive_info(matches[0], file.name)
        if hashlib.sha256(file.read_bytes()).hexdigest() != wheel_meta["sha256"]:
            raise LockError("resolved wheel SHA-256 differs from PyPI")
        locked.append({"name": project, "version": version, "filename": file.name, **wheel_meta})
    names = {w["name"] for w in locked}
    if len(names) != len(locked):
        raise LockError("duplicate wheels in a target lock")
    return sorted(locked, key=lambda x: x["name"])


def resolve(pair: dict) -> dict:
    """Resolve one pair from PyPI without running the sdist. Fail closed on unsupported requirements."""
    name, version, target = pair["name"], pair["version"], pair["target"]
    release = _metadata(name, version)
    sdists = [x for x in release["urls"] if isinstance(x, dict)
              and x.get("packagetype") == "sdist" and isinstance(x.get("filename"), str)
              and SDIST.fullmatch(x["filename"])]
    if len(sdists) != 1:
        raise LockError("expected exactly one PyPI sdist")
    meta = _archive_info(sdists[0], sdists[0]["filename"])
    archive = _from_pypi(meta["url"])
    if hashlib.sha256(archive).hexdigest() != meta["sha256"]:
        raise LockError("PyPI sdist SHA-256 mismatch")
    requirements = _project_requirements(archive, sdists[0]["filename"], target)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        locked = _resolve_wheels([*requirements, PINNED_PIP], target, root, "build")
        repair = (
            _resolve_wheels([*LINUX_REPAIR_TOOLS, PINNED_PIP], target, root, "repair")
            if ("linux-" in target or "musl-" in target)
            else []
        )
    if "pip" not in {w["name"] for w in locked}:
        raise LockError("the pinned build pip was not resolved")
    result = {"schema": 1, "name": canonical(name), "version": version, "target": target,
              "sdist": sdists[0]["filename"], "sdist_url": meta["url"],
              "sdist_sha256": meta["sha256"], "wheels": locked, "repair_wheels": repair}
    if not validate(result):
        raise LockError("resolved tool lock is malformed")
    if len(canonical_bytes(result)) > MAX_PAIR_LOCK_BYTES:
        raise LockError("pair build-tool lock exceeds the coordinator size limit")
    return result


def lock_plan(jobs: list[dict], report: list[tuple[str, str]] | None = None) -> tuple[list[dict], dict]:
    """Resolve at most four pairs at once; skip unsupported pairs without publishing them."""
    pairs = {str(j["id"]): j for j in jobs}
    locks: dict[str, dict] = {}
    outcomes: dict[str, str] = {}
    if not pairs:
        return [], {"schema": 1, "pairs": locks}
    with ThreadPoolExecutor(max_workers=min(RESOLVE_WORKERS, len(pairs))) as pool:
        futures = {pool.submit(resolve, job): pid for pid, job in pairs.items()}
        for future in as_completed(futures):
            pid = futures[future]
            job = pairs[pid]
            try:
                lock = future.result()
                locks[pid] = lock
                status = f"locked ({len(lock['wheels'])} build, {len(lock['repair_wheels'])} repair wheels)"
            except (LockError, OSError, subprocess.TimeoutExpired) as exc:
                status = f"skipped: {type(exc).__name__}: {exc}"
            outcomes[pid] = status
            print(f"{job['name']} {job['version']} {job['target']}: {status}", file=sys.stderr)
    if report is not None:
        report.extend(
            (f"{pairs[pid]['name']} {pairs[pid]['version']} {pairs[pid]['target']}", outcomes[pid])
            for pid in pairs
        )
    filtered = [
        {**j, "lock_sha256": digest(locks[str(j["id"])])}
        for j in jobs if str(j["id"]) in locks
    ]
    return filtered, {"schema": 1, "pairs": locks}


def load_lock(bundle: Path, pair_id: str, expected: str, job: dict) -> dict:
    """The build may use only the exact coordinator lock its job was planned with."""
    if not SHA.fullmatch(expected) or not pair_id.isdigit() or not bundle.is_file() or bundle.stat().st_size > 4_000_000:
        raise LockError("missing or invalid coordinator tool-lock bundle")
    data = json.loads(bundle.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema", "pairs"} or data["schema"] != 1:
        raise LockError("invalid lock bundle")
    lock = data["pairs"].get(pair_id)
    if not validate(lock) or digest(lock) != expected:
        raise LockError("tool-lock digest mismatch or malformed lock")
    if (lock["name"], lock["version"], lock["target"]) != (
        canonical(job["name"]), job["version"], job["target"]
    ):
        raise LockError("tool lock was created for a different job")
    return lock


def download_locked(item: dict, path: Path) -> None:
    data = _from_pypi(item["url"])
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise LockError(f"SHA-256 mismatch: {path.name}")
    path.write_bytes(data)


def _install_environment(packages: list[dict], work: Path, group: str) -> tuple[Path, list[dict]]:
    """Install one independently resolved group with all wheel hashes enforced."""
    import venv

    root = work / f"{group}-venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(root)
    py = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    wheel_dir = work / f"{group}-locked-wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    for item in packages:
        download_locked(item, wheel_dir / item["filename"])
    reqs = work / f"{group}-requirements.txt"
    reqs.write_text("".join(
        f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n"
        for w in packages
    ), encoding="utf-8")
    # Never resolve dependencies again here; both groups were resolved by the
    # pinned target-aware coordinator and verified against PyPI's SHA-256.
    cmd = [str(py), "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
           "--require-hashes", "--only-binary=:all:", "--find-links", str(wheel_dir), "-r", str(reqs)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        raise LockError(f"hash-pinned {group} install failed: " + (result.stderr or result.stdout)[-500:])
    installed = subprocess.run([str(py), "-m", "pip", "list", "--format=json"], capture_output=True,
                               text=True, timeout=40, check=True)
    got = {canonical(x["name"]): x["version"].lower() for x in json.loads(installed.stdout)}
    if not any(w["name"] == "pip" and w["version"] == PINNED_PIP.split("==")[1] for w in packages):
        raise LockError(f"{group} pip must be present at the pinned version")
    if any(got.get(w["name"]) != w["version"].lower() for w in packages):
        raise LockError(f"installed {group} packages differ from the coordinator lock")
    if any(n not in {w["name"] for w in packages} for n in got):
        raise LockError(f"unexpected {group} environment package")
    return py, packages


def create_build_environment(lock: dict, work: Path) -> tuple[Path, list[dict]]:
    """Install only the coordinator's hash-verified PEP 517 build environment."""
    return _install_environment(lock["wheels"], work, "build")


def create_repair_environment(lock: dict, work: Path) -> tuple[Path, list[dict]] | None:
    """Keep Linux auditwheel/patchelf in a separate hash-verified venv."""
    if not lock["repair_wheels"]:
        return None
    return _install_environment(lock["repair_wheels"], work, "repair")
