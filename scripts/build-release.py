#!/usr/bin/env python3
"""Build deterministic, provenance-checked GitHub release assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
import stat
import subprocess
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INCLUDE_PREFIXES = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "README.ko.md",
    "SECURITY.md",
    "VERSION",
    "catalog.json",
    "examples/",
    "install-linux.sh",
    "install-windows.ps1",
    "pyproject.toml",
    "scripts/",
    "tests/",
    "tools/",
    "uv.lock",
)
REQUIRED = {
    ".gitignore", "LICENSE", "README.md", "README.ko.md", "SECURITY.md", "VERSION",
    "catalog.json", "examples/secrets.example.json", "install-linux.sh",
    "install-windows.ps1", "pyproject.toml", "scripts/build-release.py",
    "tests/test_litellm_no_log.py", "tests/test_routerctl.py",
    "tools/litellm_no_log.py", "tools/routerctl.py", "uv.lock",
}
FIXED_TIME = (2020, 1, 1, 0, 0, 0)
MAX_MEMBERS = 2000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def is_included(name: str) -> bool:
    return any(name == prefix or (prefix.endswith("/") and name.startswith(prefix)) for prefix in INCLUDE_PREFIXES)


def tracked_release_files(version: str, development: bool) -> tuple[list[Path], str]:
    top = Path(git("rev-parse", "--show-toplevel")).resolve()
    if top != ROOT.resolve():
        raise SystemExit(f"build must run from repository root {ROOT}")
    commit = git("rev-parse", "HEAD")
    if not development:
        status = git("status", "--porcelain=v1", "--untracked-files=all")
        if status:
            raise SystemExit("official release requires a completely clean working tree")
        expected_tag = f"v{version}"
        actual_tag = git("describe", "--tags", "--exact-match", "HEAD")
        if actual_tag != expected_tag:
            raise SystemExit(f"HEAD must be exactly tagged {expected_tag}, got {actual_tag!r}")
    names = git("ls-files", "-z")
    # subprocess text preserves NUL separators.
    selected_names = sorted(name for name in names.split("\0") if name and is_included(name))
    missing = REQUIRED - set(selected_names)
    if missing:
        raise SystemExit(f"required tracked release files are missing: {sorted(missing)}")
    if len(selected_names) > MAX_MEMBERS:
        raise SystemExit("release has too many files")
    files: list[Path] = []
    total = 0
    for name in selected_names:
        path = ROOT / name
        if path.is_symlink():
            raise SystemExit(f"release symlinks are forbidden: {name}")
        if not path.is_file():
            raise SystemExit(f"tracked release path is not a regular file: {name}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SystemExit(f"release file is too large: {name}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise SystemExit("release expanded size is too large")
        files.append(path)
    return files, commit


def add_file(zf: zipfile.ZipFile, path: Path, name: str) -> None:
    data = path.read_bytes()
    executable = data.startswith(b"#!")
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    info._compresslevel = 9
    zf.writestr(info, data)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--development", action="store_true",
        help="allow an untagged/dirty tree for local testing; never use for an official release",
    )
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    runtime_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    expected_runtime_version = version.replace("-rc.", "rc")
    if runtime_version != expected_runtime_version:
        raise SystemExit(
            f"pyproject version {runtime_version!r} does not match VERSION {version!r}"
        )
    tag = f"v{version}"
    top = f"ica-litellm-key-router-{tag}"
    files, commit = tracked_release_files(version, args.development)
    output_dir = args.output_dir.resolve()
    dedicated_dist = (ROOT / "dist").resolve()
    if output_dir == ROOT.resolve() or (ROOT.resolve() in output_dir.parents and output_dir != dedicated_dist):
        raise SystemExit("output directory must not be the repository or a source subdirectory")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"output directory must be empty: {output_dir}")

    archive = output_dir / f"{top}.zip"
    with zipfile.ZipFile(archive, "w", allowZip64=False) as zf:
        for path in files:
            add_file(zf, path, f"{top}/{path.relative_to(ROOT).as_posix()}")

    standalone = []
    for name in ("install-linux.sh", "install-windows.ps1"):
        destination = output_dir / name
        shutil.copyfile(ROOT / name, destination)
        standalone.append(destination)
    primary_digests = {path.name: sha256(path) for path in [archive, *standalone]}
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(
        f"{primary_digests[archive.name]}  {archive.name}\n",
        encoding="ascii", newline="\n",
    )
    manifest = output_dir / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "version": version,
                "tag": tag,
                "sourceCommit": commit,
                "assets": {name: {"sha256": digest} for name, digest in primary_digests.items()},
            },
            indent=2,
        ) + "\n",
        encoding="utf-8", newline="\n",
    )
    checksummed = [archive, sidecar, *standalone, manifest]
    sums = output_dir / "SHA256SUMS"
    sums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(checksummed, key=lambda p: p.name)),
        encoding="ascii", newline="\n",
    )
    print(f"Built {archive.name} from {commit} ({len(files)} tracked files)")
    print(f"SHA256 {primary_digests[archive.name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
