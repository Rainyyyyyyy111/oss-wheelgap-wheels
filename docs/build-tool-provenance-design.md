# Build-tool provenance and publication trust boundary

Gap wheels are built twice in independent, untrusted GitHub Actions runs. A
wheel is eligible for publication only when both runs produce identical wheel
bytes from the exact PyPI sdist, the toolchains match a coordinator-generated
lock, the wheel passes the platform and licence checks, and a fresh installation
test succeeds.

This document explains what the mechanism verifies, where its trust boundaries
are, and what it deliberately does not promise. The public overview is in
[README.md](../README.md).

## Coordinator: resolve inputs without executing an sdist

The trusted `plan` job reads clean candidates from the tracker build lab.
For each release/platform pair it:

1. Downloads the exact PyPI sdist, verifies its published SHA-256 and reads
   the sole top-level `pyproject.toml` as archive data. Nothing is extracted
   or executed in the coordinator.
2. Uses the defined setuptools legacy defaults when the build-system table
   is absent. Otherwise reads static `build-system.requires`, rejecting
   direct URLs, extra indexes, nested requirements and unsupported inputs.
3. Resolves dependencies with **uv 0.13.9**, pinned in the workflow and
   configured with the target's Python version and platform. This avoids
   evaluating `sys_platform` and `platform_machine` on the coordinator's
   Linux interpreter. Only wheel distributions may be selected.
4. Supplements the static build requirements with known hook requirements for
   meson-python (ninja and Linux patchelf) and scikit-build-core (cmake and
   ninja). Maturin uses the existing pinned Rust toolchain. These tools are
   resolved in the coordinator, not dynamically downloaded by the backend.
5. Downloads the exact pinned versions with `--no-deps` for the target's
   ABI/platform. Every downloaded wheel must appear in its PyPI release
   listing and match its published SHA-256.
6. Independently resolves Linux repair tools (`auditwheel`, `patchelf`,
   their dependencies and pip) into a **separate lock**. The build backend
   never imports or executes from the repair tool environment.

The lock records the source filename, URL and hash, and for each of the two
tool groups its package name, exact version, filename, PyPI URL and SHA-256.
Its canonical JSON digest binds all of these fields.

A bounded four-worker pool resolves pairs. Unresolvable or oversized pair
locks (over 24,000 bytes) are skipped before dispatch, not after builds.
The step summary reports locked and skipped pairs, with reasons. Known
Meson and scikit-build-core extras are included, but unknown or arbitrary
`get_requires_for_build_wheel` requirements still fail closed without
fetching new packages during a build.
No source distribution is executed during resolution.

## Dispatch: no extra permissions on untrusted builds

The coordinator retains the complete trusted bundle. The trusted dispatcher
checks each pair's lock against the planned digest and sends that *one pair's*
compact, size-bounded lock as a workflow-dispatch input to each independent
build. Build workflows retain **`contents: read` only**. They do not need
`actions: read` or the coordinator's artifact token.

Both independent builds receive the same lock bytes and expected digest.
Each refuses a missing, malformed or mismatched lock, including a lock for
another project, release or platform. They re-download the PyPI sdist and
the tool wheels, re-check each SHA-256 and install with `--no-deps`,
`--no-index` and `--require-hashes` into clean virtual environments.
The build itself runs with `--no-build-isolation --no-index`, so it cannot
silently resolve additional backend requirements.

Linux repair occurs **after** the source build, from a separately locked
repair venv. The Windows bundling and retagging tools remain individually
version- and hash-pinned.

## Environment provenance

Each result records:

- The coordinator lock's SHA-256, all backend wheels and Linux repair wheels.
- Exact Python patch and interpreter build strings.
- The pinned Linux container digest or exact Windows runner image version.
- Compiler and linker executable/version identifiers, plus the available
  Rust compiler and CMake executable/version identifiers.
- Existing hash-pinned Windows repair and retagging tool records.

On Windows the build selects a matching MSVC `cl.exe` and `link.exe`
pair, invokes that installation's `vcvarsall.bat` and verifies
`VCToolsInstallDir` points at the same toolset. It exports the active SDK
and compiler environment, with `DISTUTILS_USE_SDK=1`, `MSSdk=1`,
`CC`, `CXX` and the target-specific Cargo linker set, then checks the
compiler and linker on `PATH`. This prevents silently recording a different
MSVC toolset from the one configured for the backend. Linux image and pinned Rust toolchains are still
part of the runner's environment. Backend build hooks may invoke other
tools not deterministically discoverable by the runner, so provenance is
not a complete system-call trace.

The trusted `merge` step rejects a missing result, malformed provenance,
unexpected package, mismatched tool lock, changed compiler/interpreter/image
record or different wheel or source hashes. Even identical wheel bytes are
rejected if the toolchain records differ.

## Stage, test and publication

The trusted stage job validates provenance **before** downloading or writing
the wheel file. It also checks the actual wheel bytes, platform tags,
libraries, licence and opt-out policy.

The full staged records and provenance are written to `staged/staged.json`.
Only the **SHA-256 of that file** and a small wheel count cross the GitHub
job-output boundary. Publication downloads the staged artifact, verifies
its exact SHA-256 against the trusted job output, validates its schema
again, and only then selects candidate files. This avoids the Linux
128 KiB per-argument/environment-string limit when publishing many wheels.
The publish environment still requires maintainer approval, and each
new wheel's bytes and install-test result are rechecked as before.

## Fail-closed and compatibility cases

- No static build metadata: use PEP 517's setuptools legacy defaults.
- Unsupported backend hooks or unavailable target wheels: skip or fail
  the pair rather than resolving dependencies in an untrusted build.
- No pinned resolver, incomplete PyPI hashes or package metadata: skip.
- Missing compiler identification or mismatched environment: reject.
- Missing, altered or oversized staged metadata: reject, not partially
  publish.
- Existing published index entries remain readable; the new provenance
  requirement applies to **newly staged** wheels.

## What verification does not establish

Two matching builds prove repeatability with these locked inputs, not that
the upstream sdist or its build tools are trustworthy. They cannot detect
a malicious tool used identically in both builds, and they do not constitute
independently reproducible builds from a third-party source.

Windows runner images are supplied by GitHub and float between releases;
their identifiers are recorded and must match for a pair to be accepted.
Rust and CMake version information records available executables, not a
complete trace of processes invoked by every build backend.

## Testing and operations

Offline tests in `tests/test_build_tool_lock.py` and
`tests/test_gap_wheels.py` cover source and tool hashes, lock substitution,
provenance mismatches, staging and publishing gates, and refusal of
unresolved dependencies.

Before enabling a production publish after a resolver or schema change:

1. Run the complete unit suite and Ruff checks.
2. Run a **no-publication** target-specific resolution over all current
   tracker candidates and all published wheel records. Report both
   successful locks and skipped pairs, including reasons.
3. Exercise at least one real Linux build/repair and a Windows compiler
   identification/lock installation on both architectures.
4. Review the staged-file hash boundary and the build workflow's read-only
   permissions. Keep the publish environment's maintainer gate active.

The first production publish after any significant lock-schema or
environment change must be treated as a security-sensitive rollout.
