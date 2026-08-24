from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
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

    def test_catalog_has_only_services_essentials_providers_and_11_models(self) -> None:
        self.assertEqual(3, len(self.catalog["providers"]))
        self.assertEqual(11, sum(len(p["models"]) for p in self.catalog["providers"].values()))

    def test_config_expands_models_by_pool_keys_without_raw_secrets(self) -> None:
        config = routerctl.generate_litellm_config(self.catalog, self.secrets)
        # 11 Services Essentials models * 3 keys.
        self.assertEqual(33, len(config["model_list"]))
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
        openai_deployment = next(
            item for item in config["model_list"] if item["model_name"].startswith("ica-se-openai--")
        )
        self.assertTrue(openai_deployment["litellm_params"]["model"].startswith("azure/"))
        self.assertEqual("v1", openai_deployment["litellm_params"]["api_version"])
        self.assertIn(
            "/responses?_litellm_route=/openai/responses",
            openai_deployment["litellm_params"]["api_base"],
        )

    def test_model_alias_is_provider_qualified(self) -> None:
        alias = routerctl.model_alias("ica-se-openai", "gpt-5.6-luna-dzus")
        self.assertEqual("ica-se-openai--gpt-5.6-luna-dzus", alias)

    def test_client_protocol_bases_preserve_native_surfaces(self) -> None:
        generated = routerctl.generate_client_providers(
            self.catalog, self.secrets["masterKey"], "127.0.0.1", 4000
        )["providers"]
        self.assertEqual("http://127.0.0.1:4000/v1", generated["ica-se-openai-router"]["baseUrl"])
        self.assertEqual("openai-responses", generated["ica-se-openai-router"]["api"])
        self.assertEqual("http://127.0.0.1:4000", generated["ica-se-claude-router"]["baseUrl"])
        self.assertEqual(
            "http://127.0.0.1:4000/v1beta",
            generated["ica-se-gemini-router"]["baseUrl"],
        )
        self.assertEqual(
            f"Bearer {self.secrets['masterKey']}",
            generated["ica-se-gemini-router"]["headers"]["Authorization"],
        )
        self.assertEqual(11, sum(len(p["models"]) for p in generated.values()))

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
            self.catalog, self.secrets["masterKey"], "127.0.0.1", 4000
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
            changed, backup = routerctl.merge_client_models(path, generated)
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
            changed2, backup2 = routerctl.merge_client_models(path, generated)
            self.assertFalse(changed2)
            self.assertIsNone(backup2)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

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
            routerctl.write_generated_state(state, self.catalog, self.secrets, "127.0.0.1", 4000, 2, 60)
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
            fresh_clients = routerctl.load_private_json(
                fresh_state / "client-models.generated.json", "clients"
            )
            self.assertTrue(all(":4000" in provider["baseUrl"] for provider in fresh_clients["providers"].values()))

            args.state_dir = state
            routerctl.cmd_bootstrap(args)
            runtime = routerctl.load_private_json(state / "runtime.json", "runtime")
            self.assertEqual((runtime["port"], runtime["maxFallbacks"], runtime["cooldownSeconds"]), (4100, 7, 123))
            clients = routerctl.load_private_json(state / "client-models.generated.json", "clients")
            self.assertTrue(all(":4100" in provider["baseUrl"] for provider in clients["providers"].values()))

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
            clients = routerctl.load_private_json(state / "client-models.generated.json", "clients")
            self.assertTrue(all(":4100" in provider["baseUrl"] for provider in clients["providers"].values()))

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


if __name__ == "__main__":
    unittest.main()
