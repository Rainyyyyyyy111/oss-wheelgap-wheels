"""Offline security regression tests for the coordinator build-tool lock and publication provenance."""

import copy
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import build_tool_lock, gap_wheels

FILE_URL = "https://files.pythonhosted.org/packages/x/"
TARGET = "3.14 win-arm64"


def project_zip(requirements='["setuptools>=70"]'):
    data = f'[build-system]\nrequires = {requirements}\nbuild-backend = "setuptools.build_meta"\n'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("demo-1.0/pyproject.toml", data)
    return output.getvalue()


def project_lock(name="demo", target=TARGET):
    wheel_name = "setuptools-80.0.0-py3-none-any.whl"
    pip_file = "pip-26.3-py3-none-any.whl"
    return {"schema": 1, "name": name, "version": "1.0", "target": target,
            "sdist": f"{name}-1.0.zip", "sdist_url": FILE_URL + f"{name}-1.0.zip",
            "sdist_sha256": "a" * 64,
            "wheels": [{"name": "pip", "version": "26.3", "filename": pip_file,
                        "url": FILE_URL + pip_file, "sha256": "f" * 64},
                       {"name": "setuptools", "version": "80.0.0", "filename": wheel_name,
                        "url": FILE_URL + wheel_name, "sha256": "e" * 64}],
            "repair_wheels": []}


def provenance(lock):
    return {"lock_sha256": build_tool_lock.digest(lock), "packages": gap_wheels.expected_packages(lock),
            "repair_packages": gap_wheels.expected_packages({"wheels": lock["repair_wheels"]}),
            "repair_tools": gap_wheels.expected_repair_tools(lock["target"]), "python": "3.14.10", "python_build": "3.14.10 test", "runner_image": "20261118.1",
            "compiler": {"cc": "MSVC compiler 19.0", "link": "MSVC linker 14.0", "rustc": "", "cmake": ""}}


class ReadSdist(unittest.TestCase):
    def test_read_static_build_system_without_executing_code(self):
        self.assertEqual(build_tool_lock._project_requirements(project_zip(), "demo-1.0.zip"),
                         ["setuptools>=70"])
        stream = io.BytesIO()
        pyproject = b'[build-system]\nrequires=["wheel"]\nbuild-backend="setuptools.build_meta"\n'
        with tarfile.open(mode="w:gz", fileobj=stream) as archive:
            member = tarfile.TarInfo("demo-1.0/pyproject.toml")
            member.size = len(pyproject)
            archive.addfile(member, io.BytesIO(pyproject))
        self.assertEqual(build_tool_lock._project_requirements(stream.getvalue(), "demo-1.0.tar.gz"), ["wheel"])

    def test_known_backend_extra_requirements_are_locked_before_build(self):
        for backend, target, added in (
            ("scikit_build_core.build", "3.14 musl-aarch64", ["cmake>=3.15", "ninja>=1.10"]),
            ("mesonpy", "3.15 linux-x86_64", ["ninja>=1.8", "patchelf>=0.11"]),
            ("mesonpy", "3.15 win-amd64", ["ninja>=1.8"]),
            ("maturin", "3.14 musl-aarch64", []),
            ("setuptools.build_meta", "3.14 musl-aarch64", []),
        ):
            with self.subTest(backend=backend, target=target):
                data = f'[build-system]\nrequires = ["build-tool>=1"]\nbuild-backend = "{backend}"\n'
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w") as archive:
                    archive.writestr("demo-1.0/pyproject.toml", data)
                self.assertEqual(
                    build_tool_lock._project_requirements(out.getvalue(), "demo-1.0.zip", target),
                    ["build-tool>=1", *added],
                )
                # Source inspection alone must never run an sdist backend.
                self.assertEqual(
                    build_tool_lock._project_requirements(out.getvalue(), "demo-1.0.zip"),
                    ["build-tool>=1"],
                )

    def test_refuse_ambiguous_and_unsafe_metadata(self):
        for requirements in ('["--extra-index-url https://evil.example/simple"]',
                             '["somepackage @ https://evil.example/file.whl"]',
                             '["-r ../../other-requirements.txt"]'):
            with self.subTest(requirements=requirements), self.assertRaises(build_tool_lock.LockError):
                build_tool_lock._project_requirements(project_zip(requirements), "demo-1.0.zip")
        with self.assertRaises(build_tool_lock.LockError):
            build_tool_lock._project_requirements(b"not a zip", "demo-1.0.zip")
        with self.assertRaises(build_tool_lock.LockError):
            build_tool_lock._project_requirements(b"", "demo-1.0.tar.gz")


class Coordinator(unittest.TestCase):
    def test_build_provenance_matches_across_different_temporary_directories(self):
        # The locked CMake executable has a different absolute path on each
        # runner. That path must not make two otherwise identical builds fail
        # the toolchain equality check in verify().
        records = []
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for directory in (first, second):
                cmake = Path(directory) / "build-venv" / "bin" / "cmake"

                def which(command):
                    return str(cmake) if command == "cmake" else f"/usr/bin/{command}"

                with (
                    mock.patch.object(gap_wheels.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], 0, "version 3.31", "")),
                    mock.patch.object(gap_wheels.shutil, "which", side_effect=which),
                ):
                    records.append(gap_wheels.build_tool_provenance(
                        "a" * 64, [], "3.14 musl-aarch64"
                    ))
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0]["compiler"]["cmake"], "version 3.31")
        self.assertNotIn("build-venv", records[0]["compiler"]["cmake"])

    def test_windows_vcvarsall_does_not_override_locked_build_tools(self):
        build_bin = Path("/locked/build-venv/Scripts")  # portable in this Linux-hosted Windows simulation

        def activate_msvc(target):
            gap_wheels.os.environ["PATH"] = "C:/VisualStudio/CMake;C:/Windows/System32"
            return "cl 19.0", "link 14.0"

        with (
            mock.patch.dict(gap_wheels.os.environ, {"PATH": "original", "ImageVersion": "runner"}, clear=False),
            mock.patch.object(gap_wheels.os, "name", "nt"),
            mock.patch.object(gap_wheels, "_msvc_tools", side_effect=activate_msvc),
            mock.patch.object(gap_wheels, "_tool_version", return_value="version"),
        ):
            tools = gap_wheels.build_tool_provenance(
                "a" * 64, [], "3.15 win-amd64", build_bin=build_bin
            )
            self.assertEqual(
                gap_wheels.os.environ["PATH"].split(gap_wheels.os.pathsep)[0], str(build_bin)
            )
            self.assertIn("VisualStudio", gap_wheels.os.environ["PATH"])
            self.assertEqual(tools["compiler"]["cc"], "cl 19.0")

    def test_windows_compiler_selection_initialises_and_checks_toolset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for release in ("2022", "18"):
                install = root / "Microsoft Visual Studio" / release / "Community"
                bin_dir = install / "VC" / "Tools" / "MSVC" / "14.44.0" / "bin" / "Hostx64" / "x64"
                bin_dir.mkdir(parents=True)
                for tool in ("cl.exe", "link.exe"):
                    (bin_dir / tool).touch()
                vcvars = install / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
                vcvars.parent.mkdir(parents=True)
                vcvars.write_text("@echo off\n")
            chosen = root / "Microsoft Visual Studio" / "18" / "Community"
            tools_dir = chosen / "VC" / "Tools" / "MSVC" / "14.44.0"
            compiler = tools_dir / "bin" / "Hostx64" / "x64" / "cl.exe"
            linker = compiler.with_name("link.exe")
            env_text = (
                f"VCINSTALLDIR={chosen / 'VC'}\n"
                f"VCToolsInstallDir={tools_dir}\nPATH={compiler.parent}\n"
            )
            def fake_which(cmd):
                return str(compiler if cmd == "cl.exe" else linker)
            with mock.patch.dict(gap_wheels.os.environ, {"ProgramFiles": str(root), "ProgramFiles(x86)": "",
                                                      "VSINSTALLDIR": ""}), \
                 mock.patch.object(gap_wheels.platform, "machine", return_value="AMD64"), \
                 mock.patch.object(gap_wheels.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, env_text, "")) as run, \
                 mock.patch.object(gap_wheels.shutil, "which", side_effect=fake_which), \
                 mock.patch.object(gap_wheels, "_tool_version", side_effect=["cl 19.0", "link 14.0"]):
                self.assertEqual(gap_wheels._msvc_tools("3.15 win-amd64"), ("cl 19.0", "link 14.0"))
                self.assertEqual(gap_wheels.os.environ["DISTUTILS_USE_SDK"], "1")
                self.assertEqual(gap_wheels.os.environ["MSSdk"], "1")
                self.assertEqual(gap_wheels.os.environ["CC"], str(compiler))
                self.assertEqual(gap_wheels.os.environ["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER"], str(linker))
                self.assertIn("cmd.exe", run.call_args.args[0])
                self.assertIn(str(compiler.parent), gap_wheels.os.environ["PATH"])
            # If vcvarsall silently chose another MSVC directory, reject it.
            mismatch = env_text.replace(str(tools_dir), str(tools_dir) + "-other")
            with mock.patch.dict(gap_wheels.os.environ, {"ProgramFiles": str(root), "ProgramFiles(x86)": "",
                                                      "VSINSTALLDIR": ""}), \
                 mock.patch.object(gap_wheels.platform, "machine", return_value="AMD64"), \
                 mock.patch.object(gap_wheels.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, mismatch, "")):
                self.assertEqual(gap_wheels._msvc_tools("3.15 win-amd64"), ("", ""))

    def test_repair_pins_cannot_drift_from_the_coordinator_lock(self):
        self.assertEqual(build_tool_lock.LINUX_REPAIR_TOOLS, gap_wheels.REPAIR_TOOLS)
        win = gap_wheels.expected_repair_tools("3.14 win-arm64")
        self.assertEqual({x["requirement"] for x in win},
                         {*gap_wheels.WINDOWS_REPAIR_TOOLS, *gap_wheels.RETAG_TOOLS})
        self.assertTrue(all(len(x["sha256"]) == 64 for x in win))
        linux = gap_wheels.expected_repair_tools("3.15 linux-x86_64")
        self.assertEqual({x["requirement"] for x in linux}, set(gap_wheels.RETAG_TOOLS))
        self.assertTrue(all(len(x["sha256"]) == 64 for x in linux))

    def test_resolve_uses_wheels_only_and_checks_pypi_hash(self):
        source = project_zip()
        wheel = b"fixed wheel bytes"
        filename = "setuptools-80.0.0-py3-none-any.whl"
        pip_filename = "pip-26.3-py3-none-any.whl"
        sdist_item = {"filename": "demo-1.0.zip", "packagetype": "sdist", "url": FILE_URL + "demo-1.0.zip",
                      "digests": {"sha256": hashlib.sha256(source).hexdigest()}}
        wheel_item = {"filename": filename, "url": FILE_URL + filename,
                      "digests": {"sha256": hashlib.sha256(wheel).hexdigest()}}
        pip_item = {"filename": pip_filename, "url": FILE_URL + pip_filename,
                    "digests": {"sha256": hashlib.sha256(wheel).hexdigest()}}
        seen = []

        def metadata(name, version):
            self.assertIn((name, version), (("demo", "1.0"), ("setuptools", "80.0.0"), ("pip", "26.3")))
            return {"urls": [sdist_item] if name == "demo" else [wheel_item] if name == "setuptools" else [pip_item]}

        def command(args, **kw):
            seen.append(args)
            if args[:2] == ["uv", "--version"]:
                return subprocess.CompletedProcess(args, 0, f"uv {build_tool_lock.UV_VERSION}\n", "")
            if args[:3] == ["uv", "pip", "compile"]:
                path = Path(args[args.index("--output-file") + 1])
                path.write_text("pip==26.3\nsetuptools==80.0.0\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if "download" in args:
                directory = Path(args[args.index("-d") + 1])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / filename).write_bytes(wheel)
                (directory / pip_filename).write_bytes(wheel)
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(build_tool_lock, "_metadata", side_effect=metadata), \
             mock.patch.object(build_tool_lock, "_from_pypi", return_value=source), \
             mock.patch.object(build_tool_lock.subprocess, "run", side_effect=command):
            lock = build_tool_lock.resolve({"name": "demo", "version": "1.0", "target": TARGET})
        self.assertTrue(build_tool_lock.validate(lock))
        self.assertEqual(lock["sdist_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual({w["sha256"] for w in lock["wheels"]}, {hashlib.sha256(wheel).hexdigest()})
        self.assertIn("--python-platform", seen[1])
        self.assertIn("aarch64-pc-windows-msvc", seen[1])
        self.assertIn("--only-binary=:all:", seen[-1])
        self.assertIn("--platform", seen[-1])
        self.assertIn("win_arm64", seen[-1])
        self.assertIn("--no-deps", seen[-1])

    def test_resolve_rejects_sdist_hash_before_dependency_resolution(self):
        item = {"filename": "demo-1.0.zip", "packagetype": "sdist",
                "url": FILE_URL + "demo-1.0.zip", "digests": {"sha256": "a" * 64}}
        with mock.patch.object(build_tool_lock, "_metadata", return_value={"urls": [item]}), \
             mock.patch.object(build_tool_lock, "_from_pypi", return_value=project_zip()), \
             mock.patch.object(build_tool_lock.subprocess, "run") as build:
            with self.assertRaisesRegex(build_tool_lock.LockError, "SHA-256 mismatch"):
                build_tool_lock.resolve({"name": "demo", "version": "1.0", "target": TARGET})
            build.assert_not_called()

    def test_lock_plan_dispatches_only_resolvable_pairs(self):
        jobs = [{"name": name, "version": "1.0", "target": TARGET, "id": id, "attempt": attempt}
                for id, name in (("0", "demo"), ("1", "other")) for attempt in (1, 2)]

        def resolver(job):
            if job["name"] == "other":
                raise build_tool_lock.LockError("unsupported backend")
            return project_lock()

        with mock.patch.object(build_tool_lock, "resolve", side_effect=resolver):
            selected, bundle = build_tool_lock.lock_plan(jobs)
        self.assertEqual(len(selected), 2)
        self.assertEqual(set(bundle["pairs"]), {"0"})
        self.assertEqual(selected[0]["lock_sha256"], build_tool_lock.digest(bundle["pairs"]["0"]))

    def test_lock_validator_rejects_malformed_target_and_sdist_url(self):
        lock = project_lock()
        self.assertTrue(build_tool_lock.validate(lock))
        self.assertFalse(build_tool_lock.validate({**lock, "target": ["3.14 win-arm64"]}))
        self.assertFalse(build_tool_lock.validate({**lock, "sdist_url": FILE_URL + "else.zip"}))

    def test_missing_or_tampered_lock_is_refused(self):
        job = {"name": "demo", "version": "1.0", "target": TARGET}
        lock = project_lock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "locks.json"
            path.write_text(json.dumps({"schema": 1, "pairs": {"0": lock}}))
            got = build_tool_lock.load_lock(path, "0", build_tool_lock.digest(lock), job)
            self.assertEqual(got, lock)
            with self.assertRaises(build_tool_lock.LockError):
                build_tool_lock.load_lock(path, "0", "f" * 64, job)
            with self.assertRaises(build_tool_lock.LockError):
                build_tool_lock.load_lock(path, "0", build_tool_lock.digest(lock),
                                          {**job, "name": "other"})
            with self.assertRaises(build_tool_lock.LockError):
                build_tool_lock.load_lock(path, "1", build_tool_lock.digest(lock), job)

    def test_download_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(build_tool_lock, "_from_pypi", return_value=b"altered"):
            path = Path(folder) / "tool.whl"
            with self.assertRaisesRegex(build_tool_lock.LockError, "SHA-256 mismatch"):
                build_tool_lock.download_locked({"url": FILE_URL + "tool.whl", "sha256": "a" * 64}, path)
            self.assertFalse(path.exists())

    def test_build_environment_installs_only_hash_pinned_wheels(self):
        lock = project_lock()
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            calls = []

            def run(cmd, **kw):
                calls.append(cmd)
                if "list" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, '[{"name":"pip","version":"26.3"},'
                                                       '{"name":"setuptools","version":"80.0.0"}]', "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch("venv.EnvBuilder"), \
                 mock.patch.object(build_tool_lock, "download_locked", side_effect=lambda item, p: p.write_bytes(b"ok")), \
                 mock.patch.object(build_tool_lock.subprocess, "run", side_effect=run):
                py, packages = build_tool_lock.create_build_environment(lock, work)
            self.assertEqual(packages, lock["wheels"])
            self.assertTrue(str(py).endswith("/bin/python") or str(py).endswith("\\Scripts\\python.exe"))
            self.assertIn("--require-hashes", calls[0])
            self.assertIn("--no-deps", calls[0])
            self.assertIn("--no-index", calls[0])
            text = (work / "build-requirements.txt").read_text()
            self.assertIn("setuptools==80.0.0 --hash=sha256:", text)
            self.assertIn("pip==26.3 --hash=sha256:", text)


class TrustedPublication(unittest.TestCase):
    def setUp(self):
        self.lock = project_lock()
        self.jobs = [{"id": "0", "name": "demo", "version": "1.0", "target": TARGET,
                      "attempt": attempt, "runner": "windows-11-arm",
                      "lock_sha256": build_tool_lock.digest(self.lock)} for attempt in (1, 2)]
        self.locks = {"schema": 1, "pairs": {"0": self.lock}}

    def result(self, attempt=1):
        return {"schema": gap_wheels.SCHEMA, "name": "demo", "version": "1.0", "target": TARGET,
                "attempt": attempt, "runner": "windows-11-arm",
                "sdist": "demo-1.0.zip", "sdist_sha256": "a" * 64,
                "wheel": "demo-1.0-cp314-cp314-win_arm64.whl", "sha256": "b" * 64,
                "installs": True if attempt == 1 else None, "tools": provenance(self.lock)}

    def verify(self, first, second, locks=None):
        results = {("0", 1): first, ("0", 2): second}
        return gap_wheels.verify(self.jobs, results,
                                lambda name, version: {"demo-1.0.zip": "a" * 64},
                                locks=self.locks if locks is None else locks)

    def test_linux_repair_venv_can_share_packages_with_backend_venv(self):
        """Both venvs use pip and packaging, but only duplicates within a venv are invalid."""
        lock = project_lock(target="3.15 linux-x86_64")
        lock["repair_wheels"] = [
            dict(w) for w in lock["wheels"] if w["name"] == "pip"
        ] + [
            {"name": name, "version": ver,
             "filename": f"{name}-{ver}-py3-none-any.whl",
             "url": FILE_URL + f"{name}-{ver}-py3-none-any.whl",
             "sha256": "d" * 64}
            for name, ver in (("auditwheel", "6.8.2"), ("patchelf", "0.19.1.0"))
        ]
        record = provenance(lock)
        self.assertTrue(build_tool_lock.validate(lock))
        self.assertTrue(gap_wheels.tool_provenance_valid(record))
        changed = copy.deepcopy(record)
        changed["repair_packages"].append(changed["repair_packages"][0])
        self.assertFalse(gap_wheels.tool_provenance_valid(changed))
        changed = copy.deepcopy(record)
        changed["repair_packages"][0]["sha256"] = "b" * 64
        self.assertTrue(gap_wheels.tool_provenance_valid(changed))
        self.assertNotEqual(changed["repair_packages"], gap_wheels.expected_packages(
            {"wheels": lock["repair_wheels"]}
        ))

    def test_provenance_is_kept_in_verified_manifest(self):
        verified, rows = self.verify(self.result(), self.result(2))
        self.assertEqual(rows[0]["verdict"], "verified")
        self.assertEqual(verified[0]["tools"], provenance(self.lock))

    def test_byte_identical_but_different_compiler_is_rejected(self):
        first, second = self.result(), self.result(2)
        second["tools"]["compiler"]["link"] = "MSVC linker 14.9"
        verified, rows = self.verify(first, second)
        self.assertFalse(verified)
        self.assertIn("different toolchains", rows[0]["verdict"])

    def test_changed_python_patch_or_runner_image_is_rejected(self):
        for key, changed in (("python", "3.14.11"), ("runner_image", "20261119.1")):
            with self.subTest(key=key):
                first, second = self.result(), self.result(2)
                second["tools"][key] = changed
                verified, rows = self.verify(first, second)
                self.assertEqual(verified, [])
                self.assertIn("different toolchains", rows[0]["verdict"])

    def test_forged_lock_or_packages_cannot_be_verified(self):
        first, second = self.result(), self.result(2)
        second["tools"]["packages"][0]["sha256"] = "b" * 64
        verified, rows = self.verify(first, second)
        self.assertFalse(verified)
        self.assertIn("coordinator's build-tool lock", rows[0]["verdict"])
        first, second = self.result(), self.result(2)
        first["tools"]["lock_sha256"] = "c" * 64
        verified, rows = self.verify(first, second)
        self.assertFalse(verified)
        self.assertIn("rejected", rows[0]["verdict"])

    def test_provenance_rejects_controls_and_accepts_prerelease_python(self):
        record = provenance(self.lock)
        for field, text in (
            ("python_build", "Python\n3.14"),
            ("runner_image", "runner\x07image"),
        ):
            with self.subTest(field=field):
                altered = copy.deepcopy(record)
                altered[field] = text
                self.assertFalse(gap_wheels.tool_provenance_valid(altered))
        altered = copy.deepcopy(record)
        altered["compiler"]["cc"] = "compiler\x1b[31m"
        self.assertFalse(gap_wheels.tool_provenance_valid(altered))
        altered = copy.deepcopy(record)
        altered["compiler"]["cmake"] = "CMake ☃"
        self.assertFalse(gap_wheels.tool_provenance_valid(altered))
        for version in ("3.16.0a1", "3.16.0b2", "3.16.0rc1", "3.16.0.dev3"):
            with self.subTest(version=version):
                altered = copy.deepcopy(record)
                altered["python"] = version
                self.assertTrue(gap_wheels.tool_provenance_valid(altered))

    def test_publication_refuses_missing_or_malformed_provenance(self):
        self.assertFalse(gap_wheels.tool_provenance_valid(None))
        self.assertFalse(gap_wheels.tool_provenance_valid({"lock_sha256": "b" * 64}))
        self.assertTrue(gap_wheels.tool_provenance_valid(provenance(self.lock)))
        with self.assertRaises(SystemExit):
            gap_wheels.trusted_staged(json.dumps([{
                "name": "demo", "version": "1.0", "target": TARGET,
                "wheel": "demo-1.0-cp314-cp314-win_arm64.whl", "sha256": "b" * 64,
                "sdist": "demo-1.0.zip", "sdist_sha256": "a" * 64, "builds": [],
                "licence": "MIT", "requires_python": None, "verified": "2026-11-18", "batch": "r"
            }]))

    def test_publish_requires_exact_digest_of_staged_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged_file = root / "staged.json"
            # Larger than Linux's single-argument limit, but safe in a file.
            staged_file.write_text(" " * 140_000 + "[]", encoding="utf-8")
            options = {
                "staged_file": str(staged_file),
                "staged_sha256": hashlib.sha256(staged_file.read_bytes()).hexdigest(),
                "files": str(root), "tested": str(root / "tested"),
                "index": str(root / "index.json"),
                "release_dir": str(root / "release"),
                "optout": str(root / "optout.txt"),
                "run": "https://github.com/wheelgap/wheels/actions/runs/1",
                "summary": None,
            }
            args = gap_wheels.argparse.Namespace(**options)
            with mock.patch.object(gap_wheels, "select", return_value=([], [])):
                self.assertEqual(gap_wheels.cmd_publish(args), 0)
            self.assertTrue((root / "index.json").is_file())
            args.staged_sha256 = "0" * 64
            with mock.patch.object(gap_wheels, "select") as choose:
                with self.assertRaisesRegex(SystemExit, "digest-verified"):
                    gap_wheels.cmd_publish(args)
                choose.assert_not_called()
            staged_file.write_text("[]", encoding="utf-8")
            args.staged_sha256 = "0" * 64
            with self.assertRaisesRegex(SystemExit, "digest-verified"):
                gap_wheels.cmd_publish(args)

    def test_stage_rejects_invalid_provenance_before_fetching_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = {
                "name": "demo", "version": "1.0", "target": TARGET,
                "wheel": "demo-1.0-cp314-cp314-win_arm64.whl",
                "sha256": "b" * 64,
                "tools": None,
            }
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"wheels": [wheel]}))
            output = root / "stage-output.txt"
            args = gap_wheels.argparse.Namespace(
                manifest=str(manifest), staged=str(root / "staged"),
                optout=str(root / "optout.txt"),
                repo="wheelgap/wheels", summary=None, github_output=str(output)
            )
            with (mock.patch.object(gap_wheels, "pypi_info") as info,
                  mock.patch.object(gap_wheels, "take_wheel") as take):
                gap_wheels.cmd_stage(args)
                info.assert_not_called()
                take.assert_not_called()
            self.assertEqual(json.loads((root / "staged" / "staged.json").read_text()), [])
            answers = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(answers["staged_count"], "0")
            self.assertEqual(len(answers["staged_sha256"]), 64)
            self.assertNotIn("staged", answers)

    def test_unpinned_build_never_launches_package_code(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(gap_wheels.subprocess, "run") as runner:
            result = gap_wheels.run_build("demo", "1.0", 1, Path(folder))
            runner.assert_not_called()
            self.assertIsNone(result["wheel"])
            self.assertIsNone(result["tools"])


if __name__ == "__main__":
    unittest.main()
