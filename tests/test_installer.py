from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-linux.sh"
WINDOWS_INSTALLER = ROOT / "install-windows.ps1"


@unittest.skipIf(os.name == "nt", "Linux installer tests")
class LinuxInstallerFastPathTests(unittest.TestCase):
    def make_existing_install(self, base: Path) -> tuple[Path, Path, Path]:
        home = base / "home"
        install = base / "install"
        release = install / "releases" / "existing"
        state = install / "state"
        release.mkdir(parents=True)
        state.mkdir(parents=True)
        home.mkdir()
        (release / ".complete").write_text("complete\n", encoding="utf-8")
        (state / "secrets.json").write_text("saved\n", encoding="utf-8")
        (install / "current").symlink_to(release)
        log = base / "wrapper.log"
        wrapper = install / "ica-router"
        wrapper.write_text(
            """#!/bin/bash
printf '%s\n' "$*" >> "$TEST_WRAPPER_LOG"
case "${1:-}" in
  doctor|start|status|stop|configure-clients) exit 0 ;;
  *) exit 91 ;;
esac
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        return home, install, log

    def run_installer(self, home: Path, install: Path, log: Path, *args: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "ICA_ROUTER_HOME": str(install),
                "TEST_WRAPPER_LOG": str(log),
            }
        )
        return subprocess.run(
            ["/bin/bash", str(INSTALLER), *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_help_documents_systemd_user_option(self) -> None:
        result = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--systemd-user", result.stdout)

    def test_saved_keys_skip_install_and_only_ensure_router_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, install, log = self.make_existing_install(Path(temporary))
            result = self.run_installer(home, install, log)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Skipping installation", result.stdout)
            self.assertEqual(log.read_text().splitlines(), ["doctor", "start", "status"])
            self.assertFalse((install / ".install.lock").exists())

    def test_pi_models_option_creates_parent_and_runs_explicit_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, install, log = self.make_existing_install(Path(temporary))
            result = self.run_installer(home, install, log, "--pi-models")
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = home / ".pi" / "agent" / "models.json"
            self.assertTrue(expected.parent.is_dir())
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    "doctor",
                    "stop",
                    f"configure-clients --client {expected}",
                    "start",
                    "status",
                ],
            )
            self.assertFalse((install / ".install.lock").exists())

    def test_invalid_custom_parent_fails_before_stopping_router(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, install, log = self.make_existing_install(Path(temporary))
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("block", encoding="utf-8")
            result = self.run_installer(
                home,
                install,
                log,
                "--models-json",
                str(blocker / "models.json"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text().splitlines(), ["doctor"])

    def test_custom_models_parent_permissions_are_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, install, log = self.make_existing_install(Path(temporary))
            parent = Path(temporary) / "shared-client-directory"
            parent.mkdir(mode=0o755)
            if os.name != "nt":
                parent.chmod(0o755)
            before_mode = parent.stat().st_mode & 0o777
            target = parent / "models.json"
            result = self.run_installer(
                home, install, log, "--models-json", str(target)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if os.name != "nt":
                self.assertEqual(parent.stat().st_mode & 0o777, before_mode)


class WindowsInstallerSourceTests(unittest.TestCase):
    def test_native_replacement_and_rollback_preserve_original_failure(self) -> None:
        source = WINDOWS_INSTALLER.read_text(encoding="utf-8")
        self.assertIn("IcaRouterNativeFile", source)
        self.assertIn("MoveFileEx($temporaryFull, $targetFull, 9)", source)
        self.assertIn("if (-not $check.AreAccessRulesProtected)", source)
        self.assertIn(
            'Write-Warning ("rollback failed while preserving the original installer error: "',
            source,
        )
        rollback = source.index("Restore-PreviousRelease", source.index("catch {"))
        warning = source.index("rollback failed while preserving", rollback)
        rethrow = source.index("throw $failure", warning)
        self.assertLess(rollback, warning)
        self.assertLess(warning, rethrow)


if __name__ == "__main__":
    unittest.main()
