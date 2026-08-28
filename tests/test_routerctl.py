from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("routerctl", ROOT / "tools" / "routerctl.py")
assert SPEC and SPEC.loader
routerctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routerctl)


class RouterConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # Unit tests cover routing/state logic. The hosted Windows installer
        # smoke test exercises the real owner-only ACL implementation once,
        # without spawning hundreds of nested PowerShell processes here.
        if os.name == "nt":
            for name in (
                "restrict_windows_directory",
                "restrict_windows_file",
                "verify_windows_private_file",
            ):
                patcher = mock.patch.object(routerctl, name, return_value=None)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.catalog = routerctl.validate_catalog(json.loads((ROOT / "catalog.json").read_text()))
        self.secrets = {
            "schemaVersion": 1,
            "masterKey": "sk-local-test-master-key-1234567890",
            "pools": {
                "ica-services-essentials": {
                    "keys": [
                        {"id": "a", "value": "sk-services-test-value-a"},
                        {"id": "b", "value": "sk-services-test-value-b"},
                        {"id": "c", "value": "sk-services-test-value-c"},
                    ]
                },
            },
        }

    def test_catalog_has_only_services_essentials_providers_and_12_models(self) -> None:
        self.assertEqual(3, len(self.catalog["providers"]))
        self.assertEqual(12, sum(len(p["models"]) for p in self.catalog["providers"].values()))

    def test_config_expands_models_by_pool_keys_without_raw_secrets(self) -> None:
        config = routerctl.generate_litellm_config(self.catalog, self.secrets)
        # 12 Services Essentials models * 3 keys.
        self.assertEqual(36, len(config["model_list"]))
        rendered = json.dumps(config)
        self.assertNotIn("nextgen", rendered.lower())
        self.assertNotIn(self.secrets["masterKey"], rendered)
        for pool in self.secrets["pools"].values():
            for key in pool["keys"]:
                self.assertNotIn(key["value"], rendered)
        self.assertEqual(2, config["router_settings"]["num_retries"])
        self.assertFalse(config["router_settings"]["enable_weighted_failover"])
        self.assertEqual(0, config["router_settings"]["retry_policy"]["BadRequestErrorRetries"])
        self.assertEqual(2, config["router_settings"]["retry_policy"]["RateLimitErrorRetries"])
        self.assertEqual(0, config["router_settings"]["allowed_fails"])
        self.assertEqual(
            {
                routerctl.client_model_id(model): routerctl.model_alias(
                    provider_id, model["id"]
                )
                for provider_id, provider in self.catalog["providers"].items()
                for model in provider["models"]
            },
            config["router_settings"]["model_group_alias"],
        )
        self.assertTrue(
            {
                routerctl.DEFAULT_CLAUDE_MODEL,
                routerctl.DEFAULT_CLAUDE_SONNET_MODEL,
                routerctl.DEFAULT_CLAUDE_HAIKU_MODEL,
                routerctl.DEFAULT_CODEX_MODEL,
            }.issubset(config["router_settings"]["model_group_alias"])
        )
        self.assertEqual(
            [routerctl.NO_LOG_CALLBACK], config["litellm_settings"]["callbacks"]
        )
        self.assertTrue(
            all(
                deployment["litellm_params"]["extra_body"] == {"no-log": True}
                for deployment in config["model_list"]
            )
        )
        openai_deployment = next(
            item for item in config["model_list"] if item["model_name"].startswith("ica-se-openai--")
        )
        self.assertTrue(openai_deployment["litellm_params"]["model"].startswith("azure/"))
        self.assertEqual("v1", openai_deployment["litellm_params"]["api_version"])
        self.assertIn(
            "/responses?_litellm_route=/openai/responses",
            openai_deployment["litellm_params"]["api_base"],
        )
        for model in self.catalog["providers"]["ica-se-openai"]["models"]:
            alias = routerctl.model_alias("ica-se-openai", model["id"])
            deployment = next(
                item for item in config["model_list"] if item["model_name"] == alias
            )
            self.assertEqual(
                f"azure/{model['id']}", deployment["litellm_params"]["model"]
            )
            self.assertEqual(
                model["litellmBaseModel"], deployment["model_info"]["base_model"]
            )

    def test_model_alias_is_provider_qualified(self) -> None:
        alias = routerctl.model_alias("ica-se-openai", "gpt-5.6-luna")
        self.assertEqual("ica-se-openai--gpt-5.6-luna", alias)

    def test_client_model_ids_must_be_globally_unique(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        duplicate = json.loads(json.dumps(catalog["providers"]["ica-se-claude"]))
        catalog["providers"]["ica-se-claude-duplicate"] = duplicate
        catalog["pools"][0]["providers"].append("ica-se-claude-duplicate")
        with self.assertRaisesRegex(routerctl.ConfigError, "duplicate client model id"):
            routerctl.validate_catalog(catalog)

    def test_client_model_ids_must_be_unique_within_provider(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        models = catalog["providers"]["ica-se-claude"]["models"]
        models[1]["clientModelId"] = routerctl.client_model_id(models[0])
        with self.assertRaisesRegex(routerctl.ConfigError, "duplicate client model id"):
            routerctl.validate_catalog(catalog)

    def test_client_model_ids_cannot_shadow_internal_aliases(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        catalog["providers"]["ica-se-claude"]["models"][0][
            "clientModelId"
        ] = routerctl.model_alias("ica-se-openai", "gpt-5.6-sol")
        with self.assertRaisesRegex(
            routerctl.ConfigError, "client model id collides with internal alias"
        ):
            routerctl.validate_catalog(catalog)

    def test_catalog_model_ids_must_be_safe_client_ids(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        catalog["providers"]["ica-se-claude"]["models"][0]["id"] = "bad model"
        with self.assertRaisesRegex(routerctl.ConfigError, "invalid model id"):
            routerctl.validate_catalog(catalog)

    def test_catalog_client_model_ids_must_be_safe(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        catalog["providers"]["ica-se-claude"]["models"][0][
            "clientModelId"
        ] = "bad client model"
        with self.assertRaisesRegex(routerctl.ConfigError, "invalid client model id"):
            routerctl.validate_catalog(catalog)

    def test_gemini_client_model_ids_cannot_contain_method_separator(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        catalog["providers"]["ica-se-gemini"]["models"][0][
            "clientModelId"
        ] = "gemini:bad"
        with self.assertRaisesRegex(routerctl.ConfigError, "cannot contain ':'"):
            routerctl.validate_catalog(catalog)

    def test_azure_catalog_model_requires_litellm_base_model(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        del catalog["providers"]["ica-se-openai"]["models"][0]["litellmBaseModel"]
        with self.assertRaisesRegex(routerctl.ConfigError, "requires a valid litellmBaseModel"):
            routerctl.validate_catalog(catalog)

    def test_direct_openai_responses_catalog_remains_supported(self) -> None:
        catalog = json.loads(json.dumps(self.catalog))
        provider = catalog["providers"]["ica-se-openai"]
        provider["api"] = "openai-responses"
        for model in provider["models"]:
            model.pop("litellmBaseModel")
        config = routerctl.generate_litellm_config(catalog, self.secrets)
        deployment = next(
            item
            for item in config["model_list"]
            if item["model_name"].startswith("ica-se-openai--")
        )
        self.assertTrue(deployment["litellm_params"]["model"].startswith("openai/"))
        self.assertEqual(provider["baseUrl"], deployment["litellm_params"]["api_base"])
        self.assertNotIn("api_version", deployment["litellm_params"])
        self.assertNotIn("base_model", deployment["model_info"])

    def test_client_protocol_bases_preserve_native_surfaces(self) -> None:
        state_dir = Path("/private/router/state")
        generated = routerctl.generate_client_providers(
            self.catalog, "127.0.0.1", 4000, state_dir
        )["providers"]
        self.assertEqual("http://127.0.0.1:4000/v1", generated["ica-se-openai-router"]["baseUrl"])
        self.assertEqual("openai-responses", generated["ica-se-openai-router"]["api"])
        self.assertEqual("http://127.0.0.1:4000", generated["ica-se-claude-router"]["baseUrl"])
        for provider_id, provider in self.catalog["providers"].items():
            self.assertEqual(
                [routerctl.client_model_id(model) for model in provider["models"]],
                [
                    model["id"]
                    for model in generated[f"{provider_id}-router"]["models"]
                ],
            )
        self.assertEqual(
            "http://127.0.0.1:4000/v1beta",
            generated["ica-se-gemini-router"]["baseUrl"],
        )
        self.assertEqual(
            f"!{routerctl.client_token_command(state_dir)}",
            generated["ica-se-openai-router"]["apiKey"],
        )
        self.assertEqual(
            f"!{routerctl.client_token_command(state_dir, bearer=True)}",
            generated["ica-se-gemini-router"]["headers"]["Authorization"],
        )
        self.assertEqual(12, sum(len(p["models"]) for p in generated.values()))
        rendered = json.dumps(generated)
        self.assertNotIn("litellmBaseModel", rendered)
        self.assertNotIn("clientModelId", rendered)
        self.assertNotIn(self.secrets["masterKey"], rendered)

    def test_client_token_reads_private_state_without_persisting_another_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(mode=0o700)
            secrets_path = state_dir / "secrets.json"
            secrets_path.write_text(json.dumps(self.secrets))
            if os.name != "nt":
                secrets_path.chmod(0o600)
            output = io.StringIO()
            with mock.patch.object(routerctl.sys, "stdout", output):
                self.assertEqual(
                    0,
                    routerctl.cmd_client_token(
                        types.SimpleNamespace(state_dir=state_dir, bearer=True)
                    ),
                )
            self.assertEqual(f"Bearer {self.secrets['masterKey']}\n", output.getvalue())

    def test_claude_settings_use_helper_and_preserve_unrelated_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Bash(git status)"]},
                        "env": {
                            "KEEP_ME": "yes",
                            "ANTHROPIC_API_KEY": "old-persisted-key",
                        },
                    }
                )
            )
            rendered = routerctl.render_claude_code_settings(
                path,
                "/private/router/ica-router client-token",
                "http://127.0.0.1:4000",
                routerctl.DEFAULT_CLAUDE_MODEL,
            )
            changed, backup = routerctl.write_private_client_file(
                path, rendered, "Claude Code settings"
            )
            self.assertTrue(changed)
            self.assertIsNotNone(backup)
            data = json.loads(path.read_text())
            self.assertEqual("yes", data["env"]["KEEP_ME"])
            self.assertNotIn("ANTHROPIC_API_KEY", data["env"])
            self.assertEqual(
                "/private/router/ica-router client-token", data["apiKeyHelper"]
            )
            self.assertEqual(routerctl.DEFAULT_CLAUDE_MODEL, data["env"]["ANTHROPIC_MODEL"])
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_configure_harnesses_prevalidates_all_targets_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "router"
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            wrapper = root / ("ica-router.ps1" if os.name == "nt" else "ica-router")
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o700)
            pi_path = Path(tmp) / "pi-models.json"
            pi_original = '{"providers":{"keep":{"api":"x"}}}\n'
            pi_path.write_text(pi_original)
            claude_path = Path(tmp) / "claude-settings.json"
            claude_path.write_text('{"env":"malformed"}\n')
            runtime = {
                "schemaVersion": 1,
                "host": "127.0.0.1",
                "port": 4000,
                "maxFallbacks": 2,
                "cooldownSeconds": 60,
            }
            args = types.SimpleNamespace(
                state_dir=state_dir,
                catalog=ROOT / "catalog.json",
                all=False,
                pi=True,
                prime=False,
                claude_code=True,
                codex=False,
                pi_models=pi_path,
                prime_models=None,
                claude_settings=claude_path,
                codex_profile=None,
                claude_model=routerctl.DEFAULT_CLAUDE_MODEL,
                codex_model=routerctl.DEFAULT_CODEX_MODEL,
            )
            with mock.patch.object(
                routerctl, "load_state", return_value=(self.catalog, self.secrets, runtime)
            ):
                with self.assertRaises(routerctl.ConfigError):
                    routerctl.cmd_configure_harnesses(args)
            self.assertEqual(pi_original, pi_path.read_text())
            self.assertEqual([], list(pi_path.parent.glob("pi-models.json.backup-*")))

    def test_configure_harnesses_rolls_back_an_earlier_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "router"
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            wrapper = root / ("ica-router.ps1" if os.name == "nt" else "ica-router")
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o700)
            pi_path = Path(tmp) / "pi-models.json"
            pi_original = '{"providers":{"keep":{"api":"x"}}}\n'
            pi_path.write_text(pi_original)
            runtime = {
                "schemaVersion": 1,
                "host": "127.0.0.1",
                "port": 4000,
                "maxFallbacks": 2,
                "cooldownSeconds": 60,
            }
            args = types.SimpleNamespace(
                state_dir=state_dir,
                catalog=ROOT / "catalog.json",
                all=False,
                pi=True,
                prime=False,
                claude_code=False,
                codex=True,
                pi_models=pi_path,
                prime_models=None,
                claude_settings=None,
                codex_profile=Path(tmp) / "codex-profile.toml",
                claude_model=routerctl.DEFAULT_CLAUDE_MODEL,
                codex_model=routerctl.DEFAULT_CODEX_MODEL,
            )
            with (
                mock.patch.object(
                    routerctl, "load_state", return_value=(self.catalog, self.secrets, runtime)
                ),
                mock.patch.object(
                    routerctl,
                    "write_private_client_file",
                    side_effect=routerctl.ConfigError("simulated Codex write failure"),
                ),
            ):
                with self.assertRaises(routerctl.ConfigError):
                    routerctl.cmd_configure_harnesses(args)
            self.assertEqual(pi_original, pi_path.read_text())

    def test_prime_models_path_implies_prime_only_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "router"
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            wrapper = root / ("ica-router.ps1" if os.name == "nt" else "ica-router")
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o700)
            prime_path = Path(tmp) / "prime-models.json"
            runtime = {
                "schemaVersion": 1,
                "host": "127.0.0.1",
                "port": 4000,
                "maxFallbacks": 2,
                "cooldownSeconds": 60,
            }
            args = types.SimpleNamespace(
                state_dir=state_dir,
                catalog=ROOT / "catalog.json",
                all=False,
                pi=False,
                prime=False,
                claude_code=False,
                codex=False,
                pi_models=None,
                prime_models=prime_path,
                claude_settings=None,
                codex_profile=None,
                claude_model=routerctl.DEFAULT_CLAUDE_MODEL,
                codex_model=routerctl.DEFAULT_CODEX_MODEL,
            )
            with mock.patch.object(
                routerctl, "load_state", return_value=(self.catalog, self.secrets, runtime)
            ):
                self.assertEqual(0, routerctl.cmd_configure_harnesses(args))
            self.assertTrue(prime_path.is_file())
            self.assertEqual(
                {
                    "ica-se-openai-router",
                    "ica-se-claude-router",
                    "ica-se-gemini-router",
                },
                set(json.loads(prime_path.read_text())["providers"]),
            )

    def test_windows_shell_helper_hides_metacharacter_path_in_encoded_command(self) -> None:
        wrapper = Path(r"C:\private&name\%TEMP%\quote'name\ica-router.ps1")
        command = routerctl.windows_client_token_command(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            wrapper,
            bearer=True,
        )
        self.assertNotIn(str(wrapper), command)
        encoded = command.split()[-1]
        script = base64.b64decode(encoded).decode("utf-16le")
        self.assertIn(str(wrapper).replace("'", "''"), script)
        self.assertIn("client-token --bearer", script)

    def test_codex_profile_uses_command_auth_and_upstream_model_id(self) -> None:
        profile = routerctl.generate_codex_profile(
            Path("/private/router/state"),
            "http://127.0.0.1:4000/v1",
            routerctl.DEFAULT_CODEX_MODEL,
        )
        self.assertIn('model = "gpt-5.6-sol"', profile)
        self.assertIn('model_provider = "ica-router"', profile)
        self.assertIn('wire_api = "responses"', profile)
        self.assertIn('"client-token"', profile)
        self.assertIn("request_max_retries = 0", profile)
        self.assertNotIn(self.secrets["masterKey"], profile)
        parsed = tomllib.loads(profile)
        self.assertEqual(routerctl.DEFAULT_CODEX_MODEL, parsed["model"])
        self.assertEqual("responses", parsed["model_providers"]["ica-router"]["wire_api"])

    def test_systemd_unit_runs_foreground_router_and_waits_for_readiness(self) -> None:
        if os.name == "nt":
            self.skipTest("systemd user units are Linux-only")
        with mock.patch.object(routerctl.sys, "platform", "linux"):
            unit = routerctl.generate_systemd_user_unit(Path("/private/ICA Router/state"))
        self.assertIn(routerctl.SYSTEMD_UNIT_MARKER, unit)
        self.assertIn('ExecStart="/private/ICA Router/ica-router" run-foreground', unit)
        self.assertIn("wait-ready --start-timeout 120", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn(self.secrets["masterKey"], unit)

    def test_public_lifecycle_delegates_to_managed_systemd_unit(self) -> None:
        if os.name == "nt":
            self.skipTest("systemd user units are Linux-only")
        args = types.SimpleNamespace(state_dir=Path("/private/router/state"))
        calls: list[tuple[str, ...]] = []

        def fake_systemctl(*arguments: str, check: bool = True) -> types.SimpleNamespace:
            calls.append(arguments)
            returncode = 1 if arguments and arguments[0] == "is-active" else 0
            return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")

        with (
            mock.patch.object(routerctl, "managed_systemd_user_unit", return_value=True),
            mock.patch.object(routerctl, "run_systemctl", side_effect=fake_systemctl),
            mock.patch.object(routerctl, "cmd_wait_ready", return_value=0) as wait_ready,
            mock.patch.object(routerctl, "cmd_start_worker") as start_worker,
            mock.patch.object(routerctl, "cmd_stop_worker") as stop_worker,
        ):
            self.assertEqual(0, routerctl.cmd_start(args))
            self.assertEqual(0, routerctl.cmd_stop(args))
        self.assertIn(("start", routerctl.SYSTEMD_UNIT_NAME), calls)
        self.assertIn(("stop", routerctl.SYSTEMD_UNIT_NAME), calls)
        wait_ready.assert_called_once_with(args)
        start_worker.assert_not_called()
        stop_worker.assert_not_called()

    def test_install_systemd_user_writes_managed_unit_and_enables_it(self) -> None:
        if os.name == "nt":
            self.skipTest("systemd user units are Linux-only")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "router"
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            wrapper = root / ("ica-router.ps1" if os.name == "nt" else "ica-router")
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o700)
            config_home = Path(tmp) / "config"
            calls: list[tuple[str, ...]] = []

            def fake_systemctl(*arguments: str, check: bool = True) -> types.SimpleNamespace:
                calls.append(arguments)
                stdout = "active\n" if arguments and arguments[0] == "is-active" else ""
                return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            args = types.SimpleNamespace(
                state_dir=state_dir,
                catalog=ROOT / "catalog.json",
                venv=root / ".venv",
            )
            runtime = {
                "schemaVersion": 1,
                "host": "127.0.0.1",
                "port": 4000,
                "maxFallbacks": 2,
                "cooldownSeconds": 60,
            }
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch.object(routerctl, "load_state", return_value=(self.catalog, self.secrets, runtime)),
                mock.patch.object(routerctl, "cmd_stop_worker", return_value=0),
                mock.patch.object(routerctl, "run_systemctl", side_effect=fake_systemctl),
            ):
                self.assertEqual(0, routerctl.cmd_install_systemd_user(args))
            unit_path = config_home / "systemd/user" / routerctl.SYSTEMD_UNIT_NAME
            self.assertTrue(unit_path.is_file())
            self.assertIn(routerctl.SYSTEMD_UNIT_MARKER, unit_path.read_text())
            self.assertIn(("daemon-reload",), calls)
            self.assertIn(("enable", "--now", routerctl.SYSTEMD_UNIT_NAME), calls)

    def test_install_systemd_user_removes_new_unit_when_start_fails(self) -> None:
        if os.name == "nt":
            self.skipTest("systemd user units are Linux-only")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "router"
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            wrapper = root / ("ica-router.ps1" if os.name == "nt" else "ica-router")
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o700)
            config_home = Path(tmp) / "config"

            def fake_systemctl(*arguments: str, check: bool = True) -> types.SimpleNamespace:
                if arguments[:2] == ("enable", "--now"):
                    raise routerctl.ConfigError("simulated systemd start failure")
                returncode = 1 if arguments and arguments[0] in {"is-enabled", "is-active"} else 0
                return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")

            args = types.SimpleNamespace(
                state_dir=state_dir,
                catalog=ROOT / "catalog.json",
                venv=root / ".venv",
            )
            runtime = {
                "schemaVersion": 1,
                "host": "127.0.0.1",
                "port": 4000,
                "maxFallbacks": 2,
                "cooldownSeconds": 60,
            }
            with (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch.object(
                    routerctl, "load_state", return_value=(self.catalog, self.secrets, runtime)
                ),
                mock.patch.object(routerctl, "cmd_stop_worker", return_value=0),
                mock.patch.object(routerctl, "run_systemctl", side_effect=fake_systemctl),
            ):
                with self.assertRaisesRegex(routerctl.ConfigError, "simulated systemd"):
                    routerctl.cmd_install_systemd_user(args)
            unit_path = config_home / "systemd/user" / routerctl.SYSTEMD_UNIT_NAME
            self.assertFalse(unit_path.exists())

    def test_imports_literal_rotator_without_printing_or_transforming_values(self) -> None:
        rotator = {
            "pools": [
                {
                    "poolId": pool_id,
                    "keys": [
                        {"id": "one", "value": f"sk-{pool_id}-one"},
                        {"id": "two", "value": f"sk-{pool_id}-two"},
                    ],
                }
                for pool_id in self.secrets["pools"]
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key-rotator.json"
            path.write_text(json.dumps(rotator))
            imported = routerctl.secrets_from_rotator(path, self.catalog)
        self.assertTrue(imported["masterKey"].startswith("sk-local-"))
        for source in rotator["pools"]:
            actual = imported["pools"][source["poolId"]]["keys"]
            self.assertEqual(source["keys"], actual)

    def test_import_ignores_the_explicitly_deprecated_nextgen_pool(self) -> None:
        rotator = {
            "pools": [
                {
                    "poolId": "ibm-ica-nextgen",
                    "keys": [{"id": "retired", "value": "retired-placeholder-value"}],
                },
                {
                    "poolId": "ica-services-essentials",
                    "keys": [{"id": "active", "value": "active-placeholder-value"}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key-rotator.json"
            path.write_text(json.dumps(rotator))
            imported = routerctl.secrets_from_rotator(path, self.catalog)
        self.assertEqual({"ica-services-essentials"}, set(imported["pools"]))
        self.assertEqual(
            "active-placeholder-value",
            imported["pools"]["ica-services-essentials"]["keys"][0]["value"],
        )

    def test_client_merge_is_idempotent_and_keeps_unrelated_provider(self) -> None:
        generated = routerctl.generate_client_providers(
            self.catalog, "127.0.0.1", 4000, Path("/private/router/state")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "keep-me": {"baseUrl": "http://local"},
                            "ibm-ica-router": {"baseUrl": "http://deprecated"},
                        }
                    }
                )
            )
            changed, backup = routerctl.write_rendered_client_models(
                path, routerctl.render_merged_client_models(path, generated)
            )
            self.assertTrue(changed)
            self.assertIsNotNone(backup)
            data = json.loads(path.read_text())
            self.assertIn("keep-me", data["providers"])
            self.assertNotIn("ibm-ica-router", data["providers"])
            self.assertEqual(
                {"ica-se-openai-router", "ica-se-claude-router", "ica-se-gemini-router"},
                set(data["providers"]) - {"keep-me"},
            )
            if os.name != "nt":
                path.chmod(0o644)
            changed2, backup2 = routerctl.write_rendered_client_models(
                path, routerctl.render_merged_client_models(path, generated)
            )
            self.assertFalse(changed2)
            self.assertIsNone(backup2)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_single_services_essentials_key_is_valid(self) -> None:
        document = json.loads(json.dumps(self.secrets))
        document["pools"]["ica-services-essentials"]["keys"] = document["pools"][
            "ica-services-essentials"
        ]["keys"][:1]
        self.assertEqual(document, routerctl.validate_secrets(document, self.catalog))
        config = routerctl.generate_litellm_config(self.catalog, document)
        self.assertEqual(0, config["router_settings"]["num_retries"])
        self.assertEqual(
            0, config["router_settings"]["retry_policy"]["RateLimitErrorRetries"]
        )

        document["pools"]["ica-services-essentials"]["keys"] = self.secrets["pools"][
            "ica-services-essentials"
        ]["keys"][:2]
        config = routerctl.generate_litellm_config(self.catalog, document)
        self.assertEqual(1, config["router_settings"]["num_retries"])

    def test_placeholder_secrets_are_rejected(self) -> None:
        bad = json.loads(json.dumps(self.secrets))
        bad["pools"]["ica-services-essentials"]["keys"][0]["value"] = "REPLACE_ME"
        with self.assertRaises(routerctl.ConfigError):
            routerctl.validate_secrets(bad, self.catalog)

    def test_generation_marker_rejects_mixed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir(mode=0o700)
            routerctl.atomic_write(
                state / "secrets.json", json.dumps(self.secrets) + "\n", private=True
            )
            routerctl.atomic_write(
                state / "client-models.generated.json", "{}\n", private=True
            )
            routerctl.write_generated_state(state, self.catalog, self.secrets, "127.0.0.1", 4000, 2, 60)
            self.assertFalse((state / "client-models.generated.json").exists())
            generation = routerctl.load_private_json(
                state / "generation.json", "generation"
            )
            self.assertNotIn("generatedClients", generation["documents"])
            loaded_catalog, loaded_secrets, runtime = routerctl.load_state(
                state, ROOT / "catalog.json"
            )
            self.assertEqual(self.secrets, loaded_secrets)
            self.assertEqual(4000, runtime["port"])
            config = json.loads((state / "config.yaml").read_text())
            config["model_list"].pop()
            routerctl.atomic_write(state / "config.yaml", json.dumps(config), private=True)
            with self.assertRaises(routerctl.ConfigError):
                routerctl.load_state(state, ROOT / "catalog.json")

    def test_runtime_is_strictly_loopback_and_nonzero_cooldown(self) -> None:
        valid = {
            "schemaVersion": 1,
            "host": "127.0.0.1",
            "port": 4000,
            "maxFallbacks": 2,
            "cooldownSeconds": 60,
        }
        self.assertEqual(valid, routerctl.validate_runtime(valid))
        for field, value in (("host", "0.0.0.0"), ("cooldownSeconds", 0)):
            invalid = dict(valid)
            invalid[field] = value
            with self.assertRaises(routerctl.ConfigError):
                routerctl.validate_runtime(invalid)

    def test_secret_size_control_char_and_key_id_bounds(self) -> None:
        for mutate in (
            lambda doc: doc.__setitem__("masterKey", "sk-" + "x" * 2000),
            lambda doc: doc["pools"]["ica-services-essentials"]["keys"][0].__setitem__("id", "bad id"),
            lambda doc: doc["pools"]["ica-services-essentials"]["keys"][0].__setitem__("value", "bad\nkey"),
        ):
            bad = json.loads(json.dumps(self.secrets))
            mutate(bad)
            with self.assertRaises(routerctl.ConfigError):
                routerctl.validate_secrets(bad, self.catalog)

    def test_duplicate_and_unknown_rotator_pools_are_rejected(self) -> None:
        base_pool = {
            "poolId": "ica-services-essentials",
            "keys": [{"id": "a", "value": "sk-a"}, {"id": "b", "value": "sk-b"}],
        }
        for pools in ([base_pool, dict(base_pool)], [dict(base_pool, poolId="unknown")]):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "rotator.json"
                path.write_text(json.dumps({"pools": pools}))
                with self.assertRaises(routerctl.ConfigError):
                    routerctl.secrets_from_rotator(path, self.catalog)

    def test_catalog_rejects_environment_name_collisions(self) -> None:
        bad = json.loads(json.dumps(self.catalog))
        bad["pools"][0]["id"] = "a-b"
        bad["pools"].append(
            {"id": "a_b", "providers": list(bad["pools"][0]["providers"])}
        )
        with self.assertRaises(routerctl.ConfigError):
            routerctl.validate_catalog(bad)

    def test_bootstrap_explicit_client_does_not_implicitly_add_auto(self) -> None:
        parser = routerctl.build_parser()
        args = parser.parse_args(["bootstrap", "--client", "/tmp/only-this.json"])
        self.assertEqual(["/tmp/only-this.json"], args.client)
        self.assertFalse(args.no_configure_clients)
        args2 = parser.parse_args(["bootstrap", "--no-configure-clients", "--prompt-keys"])
        self.assertTrue(args2.no_configure_clients)
        self.assertTrue(args2.prompt_keys)
        args3 = parser.parse_args(["bootstrap", "--replace-secrets", "--rotate-master-key"])
        self.assertTrue(args3.rotate_master_key)

    def test_runtime_environment_forces_safe_logging_and_no_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            routerctl.atomic_write(
                state / "secrets.json", json.dumps(self.secrets) + "\n", private=True
            )
            routerctl.write_generated_state(state, self.catalog, self.secrets, "127.0.0.1", 4000, 2, 60)
            venv = root / "venv"
            executable = venv / ("Scripts/litellm.exe" if os.name == "nt" else "bin/litellm")
            executable.parent.mkdir(parents=True)
            executable.write_text("")
            args = types.SimpleNamespace(state_dir=state, catalog=ROOT / "catalog.json", venv=venv)
            with mock.patch.dict(
                os.environ,
                {
                    "LITELLM_LOG": "DEBUG",
                    "HTTPS_PROXY": "http://untrusted.invalid",
                    "AMBIENT_TEST_SECRET": "must-not-reach-child",
                },
                clear=False,
            ):
                _command, environment, _runtime = routerctl.serve_command(args)
            self.assertEqual("ERROR", environment["LITELLM_LOG"])
            self.assertNotIn("HTTPS_PROXY", environment)
            self.assertEqual("PRODUCTION", environment["LITELLM_MODE"])
            self.assertNotIn("AMBIENT_TEST_SECRET", environment)
            self.assertEqual("true", environment["OTEL_SDK_DISABLED"])
            self.assertEqual(str(ROOT.resolve()), environment["PYTHONPATH"])

    def test_interactive_secret_entry_requires_tty(self) -> None:
        with mock.patch.object(routerctl.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(routerctl.ConfigError):
                routerctl.interactive_secrets(self.catalog)

    def test_interactive_secret_entry_reads_until_blank_per_pool(self) -> None:
        entered = ["", "se-secret-one", "se-secret-two", ""]
        with (
            mock.patch.object(routerctl.sys.stdin, "isatty", return_value=True),
            mock.patch.object(routerctl.sys.stderr, "isatty", return_value=True),
            mock.patch.object(routerctl.getpass, "getpass", side_effect=entered),
        ):
            document = routerctl.interactive_secrets(self.catalog)

        services = document["pools"]["ica-services-essentials"]["keys"]
        self.assertEqual([entry["id"] for entry in services], ["key-1", "key-2"])
        self.assertEqual(
            [entry["value"] for entry in services],
            ["se-secret-one", "se-secret-two"],
        )

    def test_replace_keys_repairs_invalid_old_pools_but_preserves_valid_master(self) -> None:
        invalid_old = json.loads(json.dumps(self.secrets))
        invalid_old["pools"] = {
            "unknown-retired-pool": {
                "keys": [{"id": "old", "value": "old-invalid-pool-value"}]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            routerctl.atomic_write(
                state / "secrets.json", json.dumps(invalid_old) + "\n", private=True
            )
            rotator = root / "rotator.json"
            rotator.write_text(
                json.dumps(
                    {
                        "pools": [
                            {
                                "poolId": "ica-services-essentials",
                                "keys": [
                                    {"id": "new", "value": "new-services-value"}
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = types.SimpleNamespace(
                state_dir=state,
                catalog=ROOT / "catalog.json",
                host="127.0.0.1",
                port=None,
                max_fallbacks=None,
                cooldown_seconds=None,
                replace_secrets=True,
                rotate_master_key=False,
                prompt_keys=False,
                import_key_rotator=str(rotator),
                non_interactive=True,
                no_configure_clients=True,
                client=[],
            )
            routerctl.cmd_bootstrap(args)
            repaired = routerctl.load_private_json(state / "secrets.json", "secrets")
            self.assertEqual(invalid_old["masterKey"], repaired["masterKey"])
            self.assertEqual({"ica-services-essentials"}, set(repaired["pools"]))

    def test_explicit_master_rotation_repairs_malformed_old_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            routerctl.atomic_write(state / "secrets.json", "{malformed\n", private=True)
            rotator = root / "rotator.json"
            rotator.write_text(
                json.dumps(
                    {
                        "pools": [
                            {
                                "poolId": "ica-services-essentials",
                                "keys": [{"id": "new", "value": "new-services-value"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = types.SimpleNamespace(
                state_dir=state,
                catalog=ROOT / "catalog.json",
                host="127.0.0.1",
                port=None,
                max_fallbacks=None,
                cooldown_seconds=None,
                replace_secrets=True,
                rotate_master_key=True,
                prompt_keys=False,
                import_key_rotator=str(rotator),
                non_interactive=True,
                no_configure_clients=True,
                client=[],
            )
            routerctl.cmd_bootstrap(args)
            repaired = routerctl.load_private_json(state / "secrets.json", "secrets")
            self.assertTrue(repaired["masterKey"].startswith("sk-local-"))
            backups = list(state.glob("secrets.json.backup-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual("{malformed\n", backups[0].read_text(encoding="utf-8"))

    def test_bootstrap_migrates_only_the_deprecated_nextgen_pool(self) -> None:
        legacy = json.loads(json.dumps(self.secrets))
        legacy["pools"]["ibm-ica-nextgen"] = {
            "keys": [{"id": "old", "value": "ci-retired-nextgen-placeholder-value"}]
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir(mode=0o700)
            routerctl.atomic_write(
                state / "secrets.json", json.dumps(legacy) + "\n", private=True
            )
            args = types.SimpleNamespace(
                state_dir=state,
                catalog=ROOT / "catalog.json",
                host="127.0.0.1",
                port=None,
                max_fallbacks=None,
                cooldown_seconds=None,
                replace_secrets=False,
                import_key_rotator=None,
                non_interactive=True,
                no_configure_clients=True,
                client=[],
            )
            routerctl.cmd_bootstrap(args)
            saved = routerctl.load_private_json(state / "secrets.json", "secrets")
            self.assertEqual({"ica-services-essentials"}, set(saved["pools"]))
            self.assertEqual(
                self.secrets["pools"]["ica-services-essentials"],
                saved["pools"]["ica-services-essentials"],
            )
            self.assertEqual(1, len(list(state.glob("secrets.json.backup-*"))))

    def test_bootstrap_preserves_custom_runtime_on_update_and_secret_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            routerctl.atomic_write(
                state / "secrets.json", json.dumps(self.secrets) + "\n", private=True
            )
            routerctl.write_generated_state(
                state, self.catalog, self.secrets, "127.0.0.1", 4100, 7, 123
            )
            args = types.SimpleNamespace(
                state_dir=state,
                catalog=ROOT / "catalog.json",
                host="127.0.0.1",
                port=None,
                max_fallbacks=None,
                cooldown_seconds=None,
                replace_secrets=False,
                import_key_rotator=None,
                non_interactive=True,
                no_configure_clients=True,
                client=[],
            )
            fresh_state = root / "fresh-state"
            fresh_state.mkdir(mode=0o700)
            routerctl.atomic_write(
                fresh_state / "secrets.json", json.dumps(self.secrets) + "\n", private=True
            )
            args.state_dir = fresh_state
            routerctl.cmd_bootstrap(args)
            fresh_runtime = routerctl.load_private_json(fresh_state / "runtime.json", "runtime")
            self.assertEqual(fresh_runtime["port"], 4000)

            args.state_dir = state
            routerctl.cmd_bootstrap(args)
            runtime = routerctl.load_private_json(state / "runtime.json", "runtime")
            self.assertEqual((runtime["port"], runtime["maxFallbacks"], runtime["cooldownSeconds"]), (4100, 7, 123))

            rotator = root / "rotator.json"
            rotator.write_text(
                json.dumps({
                    "pools": [
                        {
                            "poolId": pool["id"],
                            "keys": [
                                {"id": f"rotated-{key_index}", "value": f"rotated-value-{index}-{key_index}"}
                                for key_index in range(2)
                            ],
                        }
                        for index, pool in enumerate(self.catalog["pools"])
                    ]
                }),
                encoding="utf-8",
            )
            args.replace_secrets = True
            args.import_key_rotator = str(rotator)
            routerctl.cmd_bootstrap(args)
            replaced_secrets = routerctl.load_private_json(state / "secrets.json", "secrets")
            self.assertEqual(self.secrets["masterKey"], replaced_secrets["masterKey"])
            runtime = routerctl.load_private_json(state / "runtime.json", "runtime")
            self.assertEqual((runtime["port"], runtime["maxFallbacks"], runtime["cooldownSeconds"]), (4100, 7, 123))

    def test_dead_run_state_does_not_hide_foreign_listener(self) -> None:
        import socket

        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            state = Path(tmp)
            if os.name != "nt":
                state.chmod(0o700)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            runtime = {
                "schemaVersion": 1,
                "host": "127.0.0.1",
                "port": port,
                "maxFallbacks": 2,
                "cooldownSeconds": 60,
            }
            run = {
                "schemaVersion": 1,
                "pid": 99999999,
                "startToken": "dead",
                "executable": "/dead/litellm",
                "configPath": "/dead/config",
                "configSha256": "0" * 64,
                "host": "127.0.0.1",
                "port": port,
            }
            routerctl.atomic_write(state / "runtime.json", json.dumps(runtime), private=True)
            routerctl.atomic_write(state / "run.json", json.dumps(run), private=True)
            with self.assertRaises(routerctl.ConfigError):
                routerctl.require_router_stopped_for_mutation(state)
            self.assertFalse((state / "run.json").exists())

    @unittest.skipIf(os.name == "nt", "POSIX ancestor symlink fixture")
    def test_main_canonicalizes_state_path_through_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            captured: dict[str, Path] = {}

            def fake_status(args: object) -> int:
                captured["state"] = args.state_dir  # type: ignore[attr-defined]
                return 0

            with mock.patch.object(routerctl, "cmd_status", fake_status):
                result = routerctl.main([
                    "--state-dir", str(alias / "state"),
                    "--catalog", str(ROOT / "catalog.json"),
                    "status",
                ])
            self.assertEqual(result, 0)
            self.assertEqual(captured["state"], (real / "state").resolve())

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_backup_rejects_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.write_text("secret")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(routerctl.ConfigError):
                routerctl.backup_file(link)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_client_path_rejects_final_symlink_but_canonicalizes_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real"
            real_parent.mkdir()
            parent_alias = root / "parent-alias"
            parent_alias.symlink_to(real_parent, target_is_directory=True)
            resolved = routerctl.canonical_client_path(parent_alias / "models.json")
            self.assertEqual(real_parent / "models.json", resolved)
            target = real_parent / "target.json"
            target.write_text("{}")
            final_link = real_parent / "models.json"
            final_link.symlink_to(target)
            with self.assertRaises(routerctl.ConfigError):
                routerctl.canonical_client_path(final_link)


@unittest.skipUnless(os.name == "nt", "native Windows security APIs")
class WindowsNativeSecurityTests(unittest.TestCase):
    def test_acl_and_process_identity_do_not_spawn_shells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private & %TEMP% [path] $ tick`"
            private.mkdir()
            secret = private / "secret.json"
            secret.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                routerctl.subprocess,
                "run",
                side_effect=AssertionError("native Windows checks must not spawn subprocesses"),
            ):
                routerctl.restrict_windows_directory(private)
                routerctl.restrict_windows_file(secret)
                routerctl.verify_windows_private_file(secret)
                self.assertTrue(routerctl.process_alive(os.getpid()))
                token = routerctl.process_start_token(os.getpid())
                self.assertRegex(token or "", r"^windows:\d+$")
                image = routerctl.windows_process_image(os.getpid())
                self.assertIsNotNone(image)
                document = {
                    "pid": os.getpid(),
                    "startToken": token,
                    "executable": sys.executable,
                    "configPath": str(secret),
                }
                matched, reason = routerctl.process_matches_run_state(document)
                self.assertTrue(matched, reason)
                wrong_token = dict(document, startToken="windows:0")
                self.assertFalse(routerctl.process_matches_run_state(wrong_token)[0])
                wrong_image = dict(document, executable=str(secret))
                self.assertFalse(routerctl.process_matches_run_state(wrong_image)[0])
                with mock.patch.object(routerctl, "_open_windows_process", return_value=(None, 87)):
                    self.assertFalse(routerctl.process_alive(42424242))
                with mock.patch.object(routerctl, "_open_windows_process", return_value=(None, 5)):
                    self.assertTrue(routerctl.process_alive(42424242))
                with mock.patch.object(routerctl, "_open_windows_process", return_value=(None, 8)):
                    with self.assertRaises(routerctl.ConfigError):
                        routerctl.process_alive(42424242)

            child = routerctl.subprocess.Popen([sys.executable, "-I", "-c", "pass"])
            child.wait(timeout=30)
            self.assertFalse(routerctl.process_alive(child.pid))

    def test_native_start_token_matches_legacy_powershell_ticks(self) -> None:
        token = routerctl.process_start_token(os.getpid())
        result = routerctl.subprocess.run(
            [
                routerctl.windows_powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Process -Id {os.getpid()} -ErrorAction Stop).StartTime.ToUniversalTime().Ticks",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(token, f"windows:{result.stdout.strip()}")

    def test_native_acl_verifier_rejects_inheritance_and_foreign_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "negative-acl"
            private.mkdir()
            secret = private / "secret.json"
            secret.write_text("{}", encoding="utf-8")
            routerctl.restrict_windows_directory(private)
            routerctl.restrict_windows_file(secret)
            environment = os.environ.copy()
            environment["ICA_ROUTER_TEST_ACL_PATH"] = str(secret)

            # Use .NET Framework APIs directly. GitHub's pwsh parent can export a
            # PSModulePath whose PowerShell 7 Security module is incompatible with
            # the trusted Windows PowerShell child used for this fixture.
            inherit_script = r"""
$ErrorActionPreference = 'Stop'
$acl = [System.IO.File]::GetAccessControl($env:ICA_ROUTER_TEST_ACL_PATH)
$acl.SetAccessRuleProtection($false, $true)
[System.IO.File]::SetAccessControl($env:ICA_ROUTER_TEST_ACL_PATH, $acl)
"""
            result = routerctl.subprocess.run(
                [routerctl.windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", inherit_script],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            with self.assertRaises(routerctl.ConfigError):
                routerctl.verify_windows_private_file(secret)
            routerctl.restrict_windows_file(secret)

            foreign_allow_script = r"""
$ErrorActionPreference = 'Stop'
$acl = [System.IO.File]::GetAccessControl($env:ICA_ROUTER_TEST_ACL_PATH)
$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
  $sid, 'Read', [System.Security.AccessControl.AccessControlType]::Allow)
[void]$acl.AddAccessRule($rule)
[System.IO.File]::SetAccessControl($env:ICA_ROUTER_TEST_ACL_PATH, $acl)
"""
            result = routerctl.subprocess.run(
                [routerctl.windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", foreign_allow_script],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            try:
                with self.assertRaises(routerctl.ConfigError):
                    routerctl.verify_windows_private_file(secret)
            finally:
                routerctl.restrict_windows_file(secret)


if __name__ == "__main__":
    unittest.main()
