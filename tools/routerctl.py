#!/usr/bin/env python3
"""Generate and run a local LiteLLM key router for IBM ICA gateways.

This control plane intentionally uses only the Python standard library. Raw
provider credentials live only in state/secrets.json and process environment.
Generated LiteLLM configuration contains environment-variable references.
"""
from __future__ import annotations

import argparse
import base64
import copy
from contextlib import contextmanager
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets as secrets_module
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

APP_NAME = "ica-litellm-key-router"
SCHEMA_VERSION = 1
MASTER_ENV = "ICA_ROUTER_MASTER_KEY"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4000
DEFAULT_MAX_FALLBACKS = 2
DEFAULT_COOLDOWN_SECONDS = 60
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_KEYS_PER_POOL = 256
DEPRECATED_POOL_IDS = {"ibm-ica-nextgen"}
_WINDOWS_SID_CACHE: str | None = None
DEPRECATED_CLIENT_PROVIDER_IDS = {
    "ibm-ica-router",
    "ibm-ica-claude-router",
    "ibm-ica-gemini-router",
}
ALLOWED_APIS = {
    "azure-openai-responses",
    "openai-responses",
    "anthropic-messages",
    "google-generative-ai",
}
PROVIDER_PREFIX = {
    "azure-openai-responses": "openai/",
    "openai-responses": "openai/",
    "anthropic-messages": "anthropic/",
    "google-generative-ai": "gemini/",
}
CLIENT_API = {
    "azure-openai-responses": "openai-responses",
    "openai-responses": "openai-responses",
    "anthropic-messages": "anthropic-messages",
    "google-generative-ai": "google-generative-ai",
}
PLACEHOLDER_RE = re.compile(r"(?:REPLACE[_-]?ME|YOUR[_-]?KEY|<[^>]+>)", re.I)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SYSTEMD_UNIT_NAME = "ica-litellm-key-router.service"
SYSTEMD_UNIT_MARKER = "# Managed by ICA LiteLLM Key Router"
DEFAULT_CLAUDE_MODEL = "ica-se-claude--claude-opus-5"
DEFAULT_CLAUDE_OPUS_MODEL = "ica-se-claude--claude-opus-5"
DEFAULT_CLAUDE_SONNET_MODEL = "ica-se-claude--claude-sonnet-5"
DEFAULT_CLAUDE_HAIKU_MODEL = "ica-se-claude--claude-haiku-4-5"
DEFAULT_CODEX_MODEL = "ica-se-openai--gpt-5.6-sol"


def windows_system_directory() -> Path:
    if os.name != "nt":
        raise ConfigError("Windows system directory requested on a non-Windows host")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise ConfigError("could not resolve the trusted Windows system directory")
    return Path(buffer.value)


def windows_powershell() -> str:
    path = windows_system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file():
        raise ConfigError(f"trusted Windows PowerShell not found: {path}")
    return str(path)


def windows_system_command(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError("invalid Windows system command name")
    path = windows_system_directory() / name
    if not path.is_file():
        raise ConfigError(f"trusted Windows system command not found: {path}")
    return str(path)


def windows_clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    system_dir = windows_system_directory()
    windows_dir = system_dir.parent
    environment = {
        "SystemRoot": str(windows_dir),
        "WINDIR": str(windows_dir),
        "COMSPEC": str(system_dir / "cmd.exe"),
        "PATH": str(system_dir),
        "TEMP": os.environ.get("TEMP", str(windows_dir / "Temp")),
        "TMP": os.environ.get("TMP", str(windows_dir / "Temp")),
    }
    if extra:
        environment.update(extra)
    return environment


def posix_system_command(name: str) -> str:
    if os.name == "nt" or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError("invalid POSIX system command request")
    for directory in ("/usr/bin", "/bin"):
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise ConfigError(f"trusted POSIX system command not found: {name}")


class ConfigError(RuntimeError):
    pass


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def default_state_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "IcaLiteLLMKeyRouter" / "state"
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / APP_NAME / "state"


def default_venv_dir(state_dir: Path) -> Path:
    return state_dir.parent / ".venv"


def load_json(path: Path, label: str) -> Any:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} not found: {path}") from exc
    if size > MAX_JSON_BYTES:
        raise ConfigError(f"{label} is too large: {size} bytes")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid {label}: {path}: {exc}") from exc


def _reject_unsafe_existing_file(path: Path, label: str) -> None:
    """Reject symlinks and non-regular files before reading or replacing them."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigError(f"{label} must be a regular, non-symlink file: {path}")
    if os.name != "nt" and metadata.st_uid != os.getuid():
        raise ConfigError(f"{label} is not owned by the current user: {path}")


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ConfigError(f"private directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ConfigError(f"private directory is not a directory: {path}")
    if os.name != "nt":
        metadata = path.stat()
        if metadata.st_uid != os.getuid():
            raise ConfigError(f"private directory is not owned by the current user: {path}")
        path.chmod(0o700)
    else:
        restrict_windows_directory(path)


def validate_private_file(path: Path, label: str) -> None:
    _reject_unsafe_existing_file(path, label)
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} not found: {path}") from exc
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise ConfigError(f"{label} permissions are too broad: {path} mode={oct(metadata.st_mode & 0o777)}")
    if os.name == "nt":
        verify_windows_private_file(path)


def load_private_json(path: Path, label: str) -> Any:
    validate_private_file(path, label)
    return load_json(path, label)


def atomic_write(path: Path, text: str, private: bool = False) -> None:
    if path.parent.is_symlink():
        raise ConfigError(f"parent directory must not be a symlink: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        _reject_unsafe_existing_file(path, path.name)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if private and os.name != "nt":
            os.fchmod(fd, 0o600)
        elif private:
            # Windows may deny Set-Acl while the CRT mkstemp handle is open.
            # Close it, restrict the still-empty file, then reopen for content.
            os.close(fd)
            fd = -1
            restrict_windows_file(tmp)
        if fd >= 0:
            handle_context = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd = -1
        else:
            handle_context = tmp.open("w", encoding="utf-8", newline="\n")
        with handle_context as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if private and os.name != "nt":
            path.chmod(0o600)
        elif private:
            verify_windows_private_file(path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def backup_file(path: Path) -> Path | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    _reject_unsafe_existing_file(path, "file to back up")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(path, source_flags)
    candidate: Path | None = None
    output_fd: int | None = None
    try:
        source_meta = os.fstat(source_fd)
        if not stat.S_ISREG(source_meta.st_mode):
            raise ConfigError(f"file to back up is not regular: {path}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        counter = 0
        while True:
            suffix = "" if counter == 0 else f"-{counter}"
            candidate = path.with_name(f"{path.name}.backup-{stamp}{suffix}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                output_fd = os.open(candidate, flags, 0o600)
                break
            except FileExistsError:
                counter += 1
        if os.name == "nt":
            os.close(output_fd)
            output_fd = None
            restrict_windows_file(candidate)
            output_fd = os.open(candidate, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(source_fd, "rb", closefd=False) as source, os.fdopen(
            output_fd, "wb", closefd=False
        ) as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            candidate.chmod(0o600)
        else:
            verify_windows_private_file(candidate)
        return candidate
    except BaseException:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if output_fd is not None:
            os.close(output_fd)

def sanitize_id(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    if not result:
        raise ConfigError(f"cannot sanitize empty identifier from {value!r}")
    return result[:64]


def model_alias(provider_id: str, model_id: str) -> str:
    # Provider prefix prevents two ICA gateway pools with the same upstream
    # model ID from being merged into one LiteLLM model group.
    return f"{sanitize_id(provider_id)}--{sanitize_id(model_id)}"


def key_env_name(pool_id: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", pool_id).strip("_").upper()
    return f"ICA_ROUTER_{stem}_KEY_{index + 1:02d}"


def validate_catalog(catalog: Any) -> dict[str, Any]:
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigError("catalog schemaVersion must be 1")
    pools = catalog.get("pools")
    providers = catalog.get("providers")
    if not isinstance(pools, list) or not pools or not isinstance(providers, dict) or not providers:
        raise ConfigError("catalog must contain non-empty pools and providers")
    pool_ids: set[str] = set()
    pool_env_prefixes: set[str] = set()
    referenced: set[str] = set()
    aliases: set[str] = set()
    for pool in pools:
        if not isinstance(pool, dict) or not isinstance(pool.get("id"), str):
            raise ConfigError("each catalog pool requires a string id")
        pool_id = pool["id"]
        if not SAFE_ID_RE.fullmatch(pool_id) or pool_id in pool_ids:
            raise ConfigError(f"invalid or duplicate pool id: {pool_id!r}")
        pool_ids.add(pool_id)
        env_prefix = key_env_name(pool_id, 0).rsplit("_", 1)[0]
        if env_prefix in pool_env_prefixes:
            raise ConfigError(f"catalog pool IDs collide after environment sanitization: {pool_id}")
        pool_env_prefixes.add(env_prefix)
        pids = pool.get("providers")
        if not isinstance(pids, list) or not pids:
            raise ConfigError(f"pool {pool_id} has no providers")
        for provider_id in pids:
            if not isinstance(provider_id, str) or provider_id not in providers:
                raise ConfigError(f"pool {pool_id} references unknown provider {provider_id!r}")
            if provider_id in referenced:
                raise ConfigError(f"provider belongs to more than one pool: {provider_id}")
            referenced.add(provider_id)
    if referenced != set(providers):
        raise ConfigError(f"unassigned catalog providers: {sorted(set(providers) - referenced)}")
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            raise ConfigError(f"provider {provider_id} must be an object")
        api = provider.get("api")
        base_url = provider.get("baseUrl")
        models = provider.get("models")
        if api not in ALLOWED_APIS:
            raise ConfigError(f"unsupported api for {provider_id}: {api!r}")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ConfigError(f"provider {provider_id} requires an https baseUrl")
        if not isinstance(models, list) or not models:
            raise ConfigError(f"provider {provider_id} has no models")
        seen_models: set[str] = set()
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                raise ConfigError(f"provider {provider_id} has invalid model")
            model_id = model["id"]
            if model_id in seen_models:
                raise ConfigError(f"duplicate model {provider_id}/{model_id}")
            if api == "azure-openai-responses":
                base_model = model.get("litellmBaseModel")
                if not isinstance(base_model, str) or not SAFE_ID_RE.fullmatch(base_model):
                    raise ConfigError(
                        f"Azure model {provider_id}/{model_id} requires a valid litellmBaseModel"
                    )
            elif "litellmBaseModel" in model:
                raise ConfigError(
                    f"litellmBaseModel is only valid for Azure models: {provider_id}/{model_id}"
                )
            seen_models.add(model_id)
            alias = model_alias(provider_id, model_id)
            if alias in aliases:
                raise ConfigError(f"duplicate generated model alias: {alias}")
            aliases.add(alias)
    return catalog


def pool_by_provider(catalog: dict[str, Any]) -> dict[str, str]:
    return {
        provider_id: pool["id"]
        for pool in catalog["pools"]
        for provider_id in pool["providers"]
    }


def normalize_rotator_pools(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        raise ConfigError("key-rotator document must be an object")
    pools = document.get("pools")
    if pools is None:
        pools = [document]
    if not isinstance(pools, list):
        raise ConfigError("key-rotator pools must be an array")
    return pools


def resolve_rotator_key(entry: dict[str, Any], key_id: str) -> str:
    sources = [name for name in ("value", "env", "command") if name in entry]
    if len(sources) != 1:
        raise ConfigError(f"key {key_id} must use exactly one source")
    source = sources[0]
    if source == "value":
        value = entry["value"]
    elif source == "env":
        env_name = entry["env"]
        value = os.environ.get(env_name) if isinstance(env_name, str) else None
        if value is None:
            raise ConfigError(f"environment variable for key {key_id} is unavailable")
    else:
        # Never execute arbitrary commands while importing a config downloaded
        # or supplied by another user. Resolve command-backed keys separately.
        raise ConfigError(f"command-backed key {key_id} cannot be imported automatically")
    if not isinstance(value, str) or not value:
        raise ConfigError(f"key {key_id} resolved to an empty value")
    return value


def secrets_from_rotator(path: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    document = load_json(path, "key-rotator config")
    source_pools = normalize_rotator_pools(document)
    by_id: dict[str, dict[str, Any]] = {}
    expected_pool_ids = {pool["id"] for pool in catalog["pools"]}
    for pool in source_pools:
        if not isinstance(pool, dict) or not isinstance(pool.get("poolId"), str):
            raise ConfigError("each key-rotator pool requires a string poolId")
        pool_id = pool["poolId"]
        if pool_id in DEPRECATED_POOL_IDS and pool_id not in expected_pool_ids:
            continue
        if pool_id not in expected_pool_ids:
            raise ConfigError(f"key-rotator config contains unknown pool {pool_id}")
        if pool_id in by_id:
            raise ConfigError(f"key-rotator config contains duplicate pool {pool_id}")
        by_id[pool_id] = pool
    output: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "masterKey": "sk-local-" + secrets_module.token_urlsafe(32),
        "pools": {},
    }
    for catalog_pool in catalog["pools"]:
        pool_id = catalog_pool["id"]
        source = by_id.get(pool_id)
        if source is None:
            raise ConfigError(f"key-rotator config is missing pool {pool_id}")
        entries = source.get("keys")
        if not isinstance(entries, list):
            raise ConfigError(f"key-rotator pool {pool_id} has no keys")
        keys: list[dict[str, str]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ConfigError(f"invalid key entry in pool {pool_id}")
            key_id = entry.get("id")
            if not isinstance(key_id, str) or not key_id:
                key_id = f"key-{index + 1}"
            keys.append({"id": key_id, "value": resolve_rotator_key(entry, key_id)})
        output["pools"][pool_id] = {"keys": keys}
    return validate_secrets(output, catalog)


def interactive_secrets(catalog: dict[str, Any]) -> dict[str, Any]:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ConfigError(
            "interactive secret entry requires a real terminal; use --non-interactive "
            "with --import-key-rotator"
        )
    print("No saved ICA keys were found. Enter each pool's keys securely.")
    print("Submit an empty value after the last key in each pool.")
    output: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "masterKey": "sk-local-" + secrets_module.token_urlsafe(32),
        "pools": {},
    }
    for pool in catalog["pools"]:
        pool_id = pool["id"]
        print(f"\nICA keys for {pool_id}:")
        keys: list[dict[str, str]] = []
        while len(keys) < MAX_KEYS_PER_POOL:
            index = len(keys) + 1
            value = getpass.getpass(
                f"  API key {index} (press Enter to finish this pool): "
            )
            if not value:
                if keys:
                    break
                print("  Enter at least one key before finishing this pool.")
                continue
            keys.append({"id": f"key-{index}", "value": value})
        if len(keys) == MAX_KEYS_PER_POOL:
            print(f"  Reached the maximum of {MAX_KEYS_PER_POOL} keys.")
        output["pools"][pool_id] = {"keys": keys}
    return validate_secrets(output, catalog)


def validate_master_key(master: Any) -> str:
    if (
        not isinstance(master, str)
        or not master.startswith("sk-")
        or not (24 <= len(master.encode("utf-8")) <= 1024)
        or PLACEHOLDER_RE.search(master)
        or any(char in master for char in "\x00\r\n")
    ):
        raise ConfigError("masterKey is missing, invalid, too large, or still a placeholder")
    return master


def validate_secrets(document: Any, catalog: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigError("secrets schemaVersion must be 1")
    master = validate_master_key(document.get("masterKey"))
    pools = document.get("pools")
    if not isinstance(pools, dict):
        raise ConfigError("secrets pools must be an object")
    expected = {p["id"] for p in catalog["pools"]}
    if set(pools) != expected:
        raise ConfigError(f"secrets pools mismatch; expected {sorted(expected)}")
    total_environment_bytes = len(MASTER_ENV.encode("utf-8")) + len(master.encode("utf-8")) + 2
    for pool_id in sorted(expected):
        pool = pools[pool_id]
        entries = pool.get("keys") if isinstance(pool, dict) else None
        if not isinstance(entries, list) or not (1 <= len(entries) <= MAX_KEYS_PER_POOL):
            raise ConfigError(
                f"pool {pool_id} requires 1-{MAX_KEYS_PER_POOL} keys"
            )
        ids: set[str] = set()
        fingerprints: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ConfigError(f"invalid secret entry in pool {pool_id}")
            key_id, value = entry.get("id"), entry.get("value")
            if (
                not isinstance(key_id, str)
                or not SAFE_ID_RE.fullmatch(key_id)
                or key_id in ids
            ):
                raise ConfigError(f"invalid or duplicate key id in pool {pool_id}")
            if (
                not isinstance(value, str)
                or not value
                or PLACEHOLDER_RE.search(value)
                or any(char in value for char in "\x00\r\n")
            ):
                raise ConfigError(f"key {pool_id}/{key_id} is empty, invalid, or still a placeholder")
            value_bytes = value.encode("utf-8")
            if len(value_bytes) > 4096:
                raise ConfigError(f"key {pool_id}/{key_id} is too large")
            fingerprint = hashlib.sha256(value_bytes).hexdigest()
            if fingerprint in fingerprints:
                raise ConfigError(f"duplicate key value in pool {pool_id}")
            ids.add(key_id)
            fingerprints.add(fingerprint)
            env_name = key_env_name(pool_id, index)
            total_environment_bytes += len(env_name.encode("utf-8")) + len(value_bytes) + 2
    if total_environment_bytes > 24 * 1024:
        raise ConfigError("provider keys exceed the safe process-environment size limit")
    if len(json.dumps(document, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ConfigError("secrets document is too large to reload")
    return document


def migrate_deprecated_secret_pools(
    document: Any, catalog: dict[str, Any]
) -> tuple[dict[str, Any], list[str]] | None:
    """Drop only explicitly retired pools while preserving all active secrets."""
    if not isinstance(document, dict) or not isinstance(document.get("pools"), dict):
        return None
    expected = {pool["id"] for pool in catalog["pools"]}
    actual = set(document["pools"])
    removed = actual - expected
    if not removed or expected - actual or not removed.issubset(DEPRECATED_POOL_IDS):
        return None
    migrated = copy.deepcopy(document)
    migrated["pools"] = {
        pool_id: migrated["pools"][pool_id] for pool_id in sorted(expected)
    }
    return validate_secrets(migrated, catalog), sorted(removed)


def runtime_environment(secrets_doc: dict[str, Any], catalog: dict[str, Any]) -> dict[str, str]:
    env = {MASTER_ENV: secrets_doc["masterKey"]}
    for pool in catalog["pools"]:
        pool_id = pool["id"]
        for index, entry in enumerate(secrets_doc["pools"][pool_id]["keys"]):
            env[key_env_name(pool_id, index)] = entry["value"]
    return env


def generate_litellm_config(
    catalog: dict[str, Any],
    secrets_doc: dict[str, Any],
    max_fallbacks: int = DEFAULT_MAX_FALLBACKS,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> dict[str, Any]:
    validate_catalog(catalog)
    validate_secrets(secrets_doc, catalog)
    # Never schedule more router retries than distinct alternate credentials.
    # Router retry settings are global, so use the smallest active pool.
    effective_fallbacks = min(
        max_fallbacks,
        min(len(secrets_doc["pools"][pool["id"]]["keys"]) - 1 for pool in catalog["pools"]),
    )
    provider_pool = pool_by_provider(catalog)
    model_list: list[dict[str, Any]] = []
    for provider_id, provider in catalog["providers"].items():
        pool_id = provider_pool[provider_id]
        keys = secrets_doc["pools"][pool_id]["keys"]
        prefix = PROVIDER_PREFIX[provider["api"]]
        for model in provider["models"]:
            alias = model_alias(provider_id, model["id"])
            upstream_model = prefix + model["id"]
            upstream_base = provider["baseUrl"]
            if provider["api"] == "azure-openai-responses":
                # Use LiteLLM's Azure Responses transformer so api-version is
                # placed in the URL. The marker makes its Azure URL builder
                # recognize ICA's already-complete, nonstandard /responses path.
                upstream_model = "azure/" + model["id"]
                upstream_base = (
                    provider["baseUrl"].rstrip("/")
                    + "/responses?_litellm_route=/openai/responses"
                )
            for index, entry in enumerate(keys):
                deployment_seed = f"{pool_id}\0{provider_id}\0{model['id']}\0{index}\0{entry['id']}"
                deployment_id = "ica-" + hashlib.sha256(deployment_seed.encode("utf-8")).hexdigest()[:24]
                litellm_params: dict[str, Any] = {
                    "model": upstream_model,
                    "api_base": upstream_base,
                    "api_key": f"os.environ/{key_env_name(pool_id, index)}",
                    "weight": 1,
                    "max_retries": 0,
                }
                model_info = {"id": deployment_id}
                if provider["api"] == "azure-openai-responses":
                    # Pi's Azure Responses adapter uses the same stable version.
                    litellm_params["api_version"] = "v1"
                    # ICA deployment aliases can differ from LiteLLM's canonical
                    # Azure model IDs. This keeps token and cost metadata accurate.
                    model_info["base_model"] = model["litellmBaseModel"]
                model_list.append(
                    {
                        "model_name": alias,
                        "litellm_params": litellm_params,
                        "model_info": model_info,
                    }
                )
    return {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            # Router-owned retries select another healthy sibling after the
            # failed deployment is cooled down. Provider SDK retries remain 0.
            "num_retries": effective_fallbacks,
            "enable_weighted_failover": False,
            "retry_policy": {
                "BadRequestErrorRetries": 0,
                "ContentPolicyViolationErrorRetries": 0,
                "AuthenticationErrorRetries": effective_fallbacks,
                "TimeoutErrorRetries": effective_fallbacks,
                "RateLimitErrorRetries": effective_fallbacks,
            },
            "allowed_fails": 0,
            "cooldown_time": cooldown_seconds,
            "allowed_fails_policy": {
                "AuthenticationErrorAllowedFails": 0,
                "RateLimitErrorAllowedFails": 0,
                "TimeoutErrorAllowedFails": 0,
                "InternalServerErrorAllowedFails": 0,
                "ServiceUnavailableErrorAllowedFails": 0,
                "BadGatewayErrorAllowedFails": 0,
            },
            "enable_pre_call_checks": True,
            "optional_pre_call_checks": [
                "responses_api_deployment_check",
                "encrypted_content_affinity",
            ],
        },
        "litellm_settings": {
            "set_verbose": False,
            "drop_params": False,
        },
        "general_settings": {
            "master_key": f"os.environ/{MASTER_ENV}",
        },
    }


def local_base_url(api: str, host: str, port: int) -> str:
    root = f"http://{host}:{port}"
    if api in {"azure-openai-responses", "openai-responses"}:
        return root + "/v1"
    if api == "anthropic-messages":
        return root
    if api == "google-generative-ai":
        # Root native router endpoint. /gemini is provider pass-through and
        # bypasses model_list load balancing.
        return root + "/v1beta"
    raise ConfigError(f"unsupported client api: {api}")


def router_wrapper_invocation(state_dir: Path) -> tuple[str, list[str]]:
    root = state_dir.parent
    if os.name == "nt":
        wrapper = root / "ica-router.ps1"
        return windows_powershell(), [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
        ]
    return str(root / "ica-router"), []


def shell_command(arguments: list[str]) -> str:
    if any("\x00" in value or "\n" in value or "\r" in value for value in arguments):
        raise ConfigError("client helper command contains a control character")
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def windows_client_token_command(
    powershell: str, wrapper: Path, bearer: bool = False
) -> str:
    wrapper_value = str(wrapper)
    if any(char in wrapper_value for char in ("\x00", "\n", "\r")):
        raise ConfigError("Windows client helper path contains a control character")
    quoted_wrapper = "'" + wrapper_value.replace("'", "''") + "'"
    helper_args = " client-token" + (" --bearer" if bearer else "")
    script = (
        f"& {quoted_wrapper}{helper_args}; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # The encoded payload keeps install-root metacharacters away from cmd.exe,
    # which invokes Pi/Claude shell helpers on Windows.
    return subprocess.list2cmdline(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
    )


def client_token_command(state_dir: Path, bearer: bool = False) -> str:
    if os.name == "nt":
        return windows_client_token_command(
            windows_powershell(), state_dir.parent / "ica-router.ps1", bearer
        )
    executable, prefix_args = router_wrapper_invocation(state_dir)
    arguments = [executable, *prefix_args, "client-token"]
    if bearer:
        arguments.append("--bearer")
    return shell_command(arguments)


def generate_client_providers(
    catalog: dict[str, Any], host: str, port: int, state_dir: Path
) -> dict[str, Any]:
    token_command = client_token_command(state_dir)
    bearer_command = client_token_command(state_dir, bearer=True)
    providers: dict[str, Any] = {}
    for provider_id, provider in catalog["providers"].items():
        local_id = provider_id + "-router"
        local: dict[str, Any] = {
            "name": f"{provider.get('name', provider_id)} via local LiteLLM",
            "baseUrl": local_base_url(provider["api"], host, port),
            "api": CLIENT_API[provider["api"]],
            "apiKey": f"!{token_command}",
            "models": [],
        }
        if "compat" in provider:
            local["compat"] = copy.deepcopy(provider["compat"])
        if provider["api"] == "google-generative-ai":
            # Google SDK auth is normally x-goog-api-key. LiteLLM's unified
            # native router endpoint authenticates its local master key as a
            # Bearer token, while the upstream key stays inside LiteLLM.
            local["headers"] = {"Authorization": f"!{bearer_command}"}
        for original in provider["models"]:
            model = copy.deepcopy(original)
            # Router-only LiteLLM metadata is not part of Pi's model schema.
            model.pop("litellmBaseModel", None)
            model["id"] = model_alias(provider_id, original["id"])
            model["name"] = f"{original.get('name', original['id'])} (key router)"
            local["models"].append(model)
        providers[local_id] = local
    return {"providers": providers}


def render_merged_client_models(path: Path, generated: dict[str, Any]) -> str:
    if path.exists() or path.is_symlink():
        _reject_unsafe_existing_file(path, "client models.json")
        current = load_json(path, "client models.json")
        if not isinstance(current, dict) or not isinstance(current.get("providers"), dict):
            raise ConfigError(f"client models.json has no providers object: {path}")
    else:
        current = {"providers": {}}
    updated = copy.deepcopy(current)
    for provider_id in DEPRECATED_CLIENT_PROVIDER_IDS:
        updated["providers"].pop(provider_id, None)
    for provider_id, provider in generated["providers"].items():
        updated["providers"][provider_id] = provider
    return json.dumps(updated, ensure_ascii=False, indent=2) + "\n"


def write_rendered_client_models(path: Path, rendered: str) -> tuple[bool, Path | None]:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == rendered:
        _reject_unsafe_existing_file(path, "client models.json")
        if os.name != "nt":
            path.chmod(0o600)
        else:
            restrict_windows_file(path)
        validate_private_file(path, "client models.json")
        return False, None
    backup = backup_file(path)
    atomic_write(path, rendered, private=True)
    if os.name != "nt":
        path.chmod(0o600)
    else:
        restrict_windows_file(path)
    validate_private_file(path, "client models.json")
    return True, backup


def merge_client_models(path: Path, generated: dict[str, Any]) -> tuple[bool, Path | None]:
    return write_rendered_client_models(path, render_merged_client_models(path, generated))


def canonical_client_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ConfigError(f"client configuration must not be a symlink: {expanded}")
    return expanded.parent.resolve() / expanded.name


def auto_rotator_candidates() -> list[Path]:
    return [
        Path.home() / ".pi" / "agent" / "key-rotator.json",
        Path.home() / ".prime" / "agent" / "key-rotator.json",
    ]


def auto_client_candidates() -> list[Path]:
    return [
        Path.home() / ".pi" / "agent" / "models.json",
        Path.home() / ".prime" / "agent" / "models.json",
    ]


def document_digest(document: Any) -> str:
    rendered = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def write_generated_state(
    state_dir: Path,
    catalog: dict[str, Any],
    secrets_doc: dict[str, Any],
    host: str,
    port: int,
    max_fallbacks: int,
    cooldown_seconds: int,
) -> None:
    ensure_private_directory(state_dir)
    config = generate_litellm_config(catalog, secrets_doc, max_fallbacks, cooldown_seconds)
    generated_clients = generate_client_providers(catalog, host, port, state_dir)
    runtime = validate_runtime(
        {
            "schemaVersion": SCHEMA_VERSION,
            "host": host,
            "port": port,
            "maxFallbacks": max_fallbacks,
            "cooldownSeconds": cooldown_seconds,
        }
    )
    documents = {
        "catalog": catalog,
        "secrets": secrets_doc,
        "config": config,
        "generatedClients": generated_clients,
        "runtime": runtime,
    }
    # JSON is valid YAML. Write the generation marker last. A crash between
    # individual atomic replacements then fails closed on the next load.
    atomic_write(state_dir / "config.yaml", json.dumps(config, indent=2) + "\n", private=True)
    atomic_write(
        state_dir / "client-models.generated.json",
        json.dumps(generated_clients, ensure_ascii=False, indent=2) + "\n",
        private=True,
    )
    atomic_write(state_dir / "runtime.json", json.dumps(runtime, indent=2) + "\n", private=True)
    generation = {
        "schemaVersion": SCHEMA_VERSION,
        "documents": {name: document_digest(value) for name, value in documents.items()},
    }
    atomic_write(
        state_dir / "generation.json", json.dumps(generation, indent=2) + "\n", private=True
    )


def validate_runtime(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigError("runtime settings schemaVersion must be 1")
    if document.get("host") != DEFAULT_HOST:
        raise ConfigError(f"runtime host must be local-only {DEFAULT_HOST}")
    port = document.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("runtime port must be an integer from 1 to 65535")
    max_fallbacks = document.get("maxFallbacks")
    cooldown = document.get("cooldownSeconds")
    if not isinstance(max_fallbacks, int) or not (0 <= max_fallbacks <= 20):
        raise ConfigError("runtime maxFallbacks must be 0-20")
    if not isinstance(cooldown, int) or not (1 <= cooldown <= 86400):
        raise ConfigError("runtime cooldownSeconds must be 1-86400")
    return document


def load_state(state_dir: Path, catalog_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ensure_private_directory(state_dir)
    catalog = validate_catalog(load_json(catalog_path, "catalog"))
    secrets_doc = validate_secrets(load_private_json(state_dir / "secrets.json", "secrets"), catalog)
    runtime = validate_runtime(load_private_json(state_dir / "runtime.json", "runtime settings"))
    config = load_private_json(state_dir / "config.yaml", "generated LiteLLM config")
    generated_clients = load_private_json(
        state_dir / "client-models.generated.json", "generated client models"
    )
    generation = load_private_json(state_dir / "generation.json", "generation marker")
    documents = {
        "catalog": catalog,
        "secrets": secrets_doc,
        "config": config,
        "generatedClients": generated_clients,
        "runtime": runtime,
    }
    expected = {name: document_digest(value) for name, value in documents.items()}
    if (
        not isinstance(generation, dict)
        or generation.get("schemaVersion") != SCHEMA_VERSION
        or generation.get("documents") != expected
    ):
        raise ConfigError("private state generation is incomplete or inconsistent; run bootstrap again")
    return catalog, secrets_doc, runtime


def current_windows_sid() -> str:
    global _WINDOWS_SID_CACHE
    if _WINDOWS_SID_CACHE is not None:
        return _WINDOWS_SID_CACHE
    result = subprocess.run(
        [
            windows_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        capture_output=True,
        text=True,
        env=windows_clean_environment(),
        check=False,
    )
    sid = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"S-1-(?:\d+-)+\d+", sid):
        raise ConfigError("could not resolve the current Windows user SID")
    _WINDOWS_SID_CACHE = sid
    return sid


def restrict_windows_directory(path: Path) -> None:
    if os.name != "nt":
        return
    if path.is_symlink() or not path.is_dir():
        raise ConfigError(f"private Windows directory is unsafe: {path}")
    sid = current_windows_sid()
    script = r"""
$ErrorActionPreference = 'Stop'
$target = $env:ICA_ROUTER_ACL_TARGET
$sidText = $env:ICA_ROUTER_ACL_SID
$sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
  $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
[void]$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl($target, $acl)
$check = Get-Acl -LiteralPath $target
$ownerSid = ([System.Security.Principal.NTAccount]$check.Owner).Translate([System.Security.Principal.SecurityIdentifier]).Value
if ($ownerSid -ne $sidText) { exit 6 }
$allowed = @($check.Access | Where-Object { $_.AccessControlType -eq 'Allow' })
if ($allowed.Count -eq 0) { exit 7 }
foreach ($entry in $allowed) {
  $entrySid = $entry.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
  if ($entrySid -ne $sidText) { exit 8 }
}
"""
    result = subprocess.run(
        [
            windows_powershell(), "-NoProfile", "-NonInteractive", "-Command",
            script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=windows_clean_environment({
            "ICA_ROUTER_ACL_TARGET": str(path),
            "ICA_ROUTER_ACL_SID": sid,
        }),
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().replace("\n", " ")[:500]
        raise ConfigError(
            f"could not restrict Windows directory ACL for {path} (exit {result.returncode}): {detail}"
        )


def _run_windows_acl_check(path: Path, set_acl: bool) -> None:
    sid = current_windows_sid()
    script = r"""
$ErrorActionPreference = 'Stop'
$target = $env:ICA_ROUTER_ACL_TARGET
$sidText = $env:ICA_ROUTER_ACL_SID
$sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
if ($env:ICA_ROUTER_ACL_MODE -eq 'set') {
  $acl = New-Object System.Security.AccessControl.FileSecurity
  $acl.SetAccessRuleProtection($true, $false)
  $acl.SetOwner($sid)
  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow')
  [void]$acl.AddAccessRule($rule)
  [System.IO.File]::SetAccessControl($target, $acl)
}
$check = Get-Acl -LiteralPath $target
$ownerSid = ([System.Security.Principal.NTAccount]$check.Owner).Translate([System.Security.Principal.SecurityIdentifier]).Value
if ($ownerSid -ne $sidText) { exit 6 }
$allowed = @($check.Access | Where-Object { $_.AccessControlType -eq 'Allow' })
if ($allowed.Count -eq 0) { exit 7 }
foreach ($entry in $allowed) {
  $entrySid = $entry.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
  if ($entrySid -ne $sidText) { exit 8 }
}
"""
    result = subprocess.run(
        [
            windows_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=windows_clean_environment({
            "ICA_ROUTER_ACL_TARGET": str(path),
            "ICA_ROUTER_ACL_SID": sid,
            "ICA_ROUTER_ACL_MODE": "set" if set_acl else "verify",
        }),
        check=False,
    )
    if result.returncode != 0:
        action = "restrict" if set_acl else "verify"
        detail = (result.stderr or "").strip().replace("\n", " ")[:500]
        raise ConfigError(
            f"could not {action} Windows ACL for {path} (exit {result.returncode}): {detail}"
        )


def restrict_windows_file(path: Path) -> None:
    if os.name != "nt" or not path.exists():
        return
    _reject_unsafe_existing_file(path, "private Windows file")
    _run_windows_acl_check(path, set_acl=True)


def verify_windows_private_file(path: Path) -> None:
    if os.name != "nt":
        return
    _reject_unsafe_existing_file(path, "private Windows file")
    _run_windows_acl_check(path, set_acl=False)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    state_dir: Path = args.state_dir
    if getattr(args, "rotate_master_key", False) and not args.replace_secrets:
        raise ConfigError("--rotate-master-key requires --replace-secrets")
    if args.host != DEFAULT_HOST:
        raise ConfigError(f"--host must remain local-only {DEFAULT_HOST}")
    catalog = validate_catalog(load_json(args.catalog, "catalog"))
    ensure_private_directory(state_dir)
    existing_runtime: dict[str, Any] | None = None
    runtime_path = state_dir / "runtime.json"
    if runtime_path.exists():
        existing_runtime = validate_runtime(load_private_json(runtime_path, "runtime settings"))
    port = args.port if args.port is not None else int((existing_runtime or {}).get("port", DEFAULT_PORT))
    max_fallbacks = (
        args.max_fallbacks
        if args.max_fallbacks is not None
        else int((existing_runtime or {}).get("maxFallbacks", DEFAULT_MAX_FALLBACKS))
    )
    cooldown_seconds = (
        args.cooldown_seconds
        if args.cooldown_seconds is not None
        else int((existing_runtime or {}).get("cooldownSeconds", DEFAULT_COOLDOWN_SECONDS))
    )
    secrets_path = state_dir / "secrets.json"
    loaded_secrets: Any | None = None
    if secrets_path.exists() or secrets_path.is_symlink():
        _reject_unsafe_existing_file(secrets_path, "secrets")
        if os.name == "nt":
            restrict_windows_file(secrets_path)
        if not (args.replace_secrets and getattr(args, "rotate_master_key", False)):
            try:
                loaded_secrets = load_private_json(secrets_path, "secrets")
            except ConfigError:
                if not args.replace_secrets:
                    raise
                print(
                    "WARNING: existing secrets cannot be parsed or validated; "
                    "generated a new local master key. Generated command-backed clients "
                    "remain valid; reconfigure any client that stored a literal key.",
                    file=sys.stderr,
                )

    if loaded_secrets is not None and not args.replace_secrets:
        migration = migrate_deprecated_secret_pools(loaded_secrets, catalog)
        if migration is not None:
            secrets_doc, removed_pools = migration
            backup = backup_file(secrets_path)
            if backup:
                print(f"Backed up previous secrets to {backup}")
            atomic_write(
                secrets_path,
                json.dumps(secrets_doc, ensure_ascii=False, indent=2) + "\n",
                private=True,
            )
            restrict_windows_file(secrets_path)
            print(
                "Removed deprecated secret pools: "
                + ", ".join(removed_pools)
                + " (secret values were not printed)"
            )
        else:
            secrets_doc = validate_secrets(loaded_secrets, catalog)
        print(f"Preserving existing secrets: {secrets_path}")
    else:
        import_path: Path | None = None
        if getattr(args, "prompt_keys", False):
            import_path = None
        elif args.import_key_rotator:
            if args.import_key_rotator == "auto":
                import_path = next((p for p in auto_rotator_candidates() if p.exists()), None)
            else:
                import_path = Path(args.import_key_rotator).expanduser().resolve()
        if import_path is not None:
            print(f"Importing key pools from {import_path} (secret values will not be printed)")
            secrets_doc = secrets_from_rotator(import_path, catalog)
        elif args.non_interactive:
            raise ConfigError("no existing secrets or importable key-rotator config")
        else:
            secrets_doc = interactive_secrets(catalog)

        # Replacing upstream API keys must not silently invalidate existing
        # client files. Preserve the local proxy master key unless rotation is
        # explicitly requested.
        if loaded_secrets is not None and not getattr(args, "rotate_master_key", False):
            try:
                existing_master = validate_master_key(
                    loaded_secrets.get("masterKey")
                    if isinstance(loaded_secrets, dict)
                    else None
                )
            except ConfigError:
                print(
                    "WARNING: existing local master key is invalid; generated a new one. "
                    "Generated command-backed clients remain valid; reconfigure any "
                    "client that stored a literal key.",
                    file=sys.stderr,
                )
            else:
                secrets_doc["masterKey"] = existing_master
                secrets_doc = validate_secrets(secrets_doc, catalog)

        if secrets_path.exists():
            backup = backup_file(secrets_path)
            if backup:
                print(f"Backed up previous secrets to {backup}")
        atomic_write(
            secrets_path,
            json.dumps(secrets_doc, ensure_ascii=False, indent=2) + "\n",
            private=True,
        )
        restrict_windows_file(secrets_path)
        print(f"Wrote private secrets file: {secrets_path}")
    write_generated_state(
        state_dir,
        catalog,
        secrets_doc,
        args.host,
        port,
        max_fallbacks,
        cooldown_seconds,
    )
    for private_name in ("config.yaml", "client-models.generated.json", "runtime.json"):
        restrict_windows_file(state_dir / private_name)
    client_paths: list[Path] = []
    client_values = [] if args.no_configure_clients else (args.client or ["auto"])
    if "auto" in client_values:
        client_paths.extend(path for path in auto_client_candidates() if path.exists())
    client_paths.extend(
        canonical_client_path(Path(value)) for value in client_values if value != "auto"
    )
    generated = generate_client_providers(catalog, args.host, port, state_dir)
    seen: set[Path] = set()
    for path in client_paths:
        if path in seen:
            continue
        seen.add(path)
        changed, backup = merge_client_models(path, generated)
        restrict_windows_file(path)
        if changed:
            print(f"Updated client model config: {path}")
            if backup:
                print(f"Backup: {backup}")
        else:
            print(f"Client model config already current: {path}")
    config = load_json(state_dir / "config.yaml", "generated LiteLLM config")
    print(
        f"Generated {len(config['model_list'])} deployments for "
        f"{sum(len(p['models']) for p in catalog['providers'].values())} models."
    )
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    catalog, secrets_doc, runtime = load_state(args.state_dir, args.catalog)
    write_generated_state(
        args.state_dir,
        catalog,
        secrets_doc,
        str(runtime.get("host", DEFAULT_HOST)),
        int(runtime.get("port", DEFAULT_PORT)),
        int(runtime.get("maxFallbacks", DEFAULT_MAX_FALLBACKS)),
        int(runtime.get("cooldownSeconds", DEFAULT_COOLDOWN_SECONDS)),
    )
    print(f"Regenerated {args.state_dir / 'config.yaml'}")
    return 0


def cmd_configure_clients(args: argparse.Namespace) -> int:
    catalog, secrets_doc, runtime = load_state(args.state_dir, args.catalog)
    generated = generate_client_providers(
        catalog,
        str(runtime.get("host", DEFAULT_HOST)),
        int(runtime.get("port", DEFAULT_PORT)),
        args.state_dir,
    )
    values = args.client or ["auto"]
    paths: list[Path] = []
    if "auto" in values:
        paths.extend(p for p in auto_client_candidates() if p.exists())
    paths.extend(canonical_client_path(Path(v)) for v in values if v != "auto")
    if not paths:
        raise ConfigError("no client models.json files found")
    for path in dict.fromkeys(paths):
        changed, backup = merge_client_models(path, generated)
        restrict_windows_file(path)
        print(f"{'Updated' if changed else 'Already current'}: {path}")
        if backup:
            print(f"Backup: {backup}")
    return 0


def write_private_client_file(path: Path, rendered: str, label: str) -> tuple[bool, Path | None]:
    ensure_private_directory(path.parent)
    old: str | None = None
    if path.exists() or path.is_symlink():
        _reject_unsafe_existing_file(path, label)
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ConfigError(f"{label} is too large: {path}")
        old = path.read_text(encoding="utf-8-sig")
    if old == rendered:
        if os.name != "nt":
            path.chmod(0o600)
        else:
            restrict_windows_file(path)
        validate_private_file(path, label)
        return False, None
    backup = backup_file(path)
    atomic_write(path, rendered, private=True)
    return True, backup


def render_claude_code_settings(
    path: Path,
    token_helper: str,
    base_url: str,
    model: str,
) -> str:
    if path.exists() or path.is_symlink():
        _reject_unsafe_existing_file(path, "Claude Code settings")
        current = load_json(path, "Claude Code settings")
        if not isinstance(current, dict):
            raise ConfigError(f"Claude Code settings must be a JSON object: {path}")
    else:
        current = {}
    updated = copy.deepcopy(current)
    env = updated.get("env", {})
    if not isinstance(env, dict):
        raise ConfigError(f"Claude Code settings env must be an object: {path}")
    env = copy.deepcopy(env)
    # apiKeyHelper supplies both Authorization and x-api-key. Remove persisted
    # credential variables from this managed settings block to prevent conflicts.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": DEFAULT_CLAUDE_OPUS_MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": DEFAULT_CLAUDE_SONNET_MODEL,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": DEFAULT_CLAUDE_HAIKU_MODEL,
        }
    )
    updated["env"] = env
    updated["apiKeyHelper"] = token_helper
    return json.dumps(updated, ensure_ascii=False, indent=2) + "\n"


def merge_claude_code_settings(
    path: Path,
    token_helper: str,
    base_url: str,
    model: str,
) -> tuple[bool, Path | None]:
    rendered = render_claude_code_settings(path, token_helper, base_url, model)
    return write_private_client_file(path, rendered, "Claude Code settings")


def toml_string(value: str) -> str:
    if "\x00" in value:
        raise ConfigError("TOML string contains NUL")
    return json.dumps(value, ensure_ascii=False)


def generate_codex_profile(state_dir: Path, base_url: str, model: str) -> str:
    # Codex 0.134.0+ loads ~/.codex/<name>.config.toml via --profile <name>.
    executable, prefix_args = router_wrapper_invocation(state_dir)
    auth_args = [*prefix_args, "client-token"]
    args_toml = ", ".join(toml_string(value) for value in auth_args)
    return (
        "# Managed by ICA LiteLLM Key Router. Use: codex --profile ica-router\n"
        f"model = {toml_string(model)}\n"
        'model_provider = "ica-router"\n'
        "model_context_window = 1000000\n\n"
        "[model_providers.ica-router]\n"
        'name = "ICA local key router"\n'
        f"base_url = {toml_string(base_url)}\n"
        'wire_api = "responses"\n'
        "request_max_retries = 0\n"
        "stream_max_retries = 0\n\n"
        "[model_providers.ica-router.auth]\n"
        f"command = {toml_string(executable)}\n"
        f"args = [{args_toml}]\n"
        "timeout_ms = 5000\n"
        "refresh_interval_ms = 300000\n"
    )


def cmd_client_token(args: argparse.Namespace) -> int:
    document = load_private_json(args.state_dir / "secrets.json", "secrets")
    token = validate_master_key(document.get("masterKey") if isinstance(document, dict) else None)
    prefix = "Bearer " if args.bearer else ""
    sys.stdout.write(prefix + token + "\n")
    return 0


def cmd_configure_harnesses(args: argparse.Namespace) -> int:
    catalog, _secrets_doc, runtime = load_state(args.state_dir, args.catalog)
    wrapper = args.state_dir.parent / ("ica-router.ps1" if os.name == "nt" else "ica-router")
    if not wrapper.is_file() or (os.name != "nt" and not os.access(wrapper, os.X_OK)):
        raise ConfigError(f"router wrapper is unavailable: {wrapper}")

    explicit = any(
        (
            args.all,
            args.pi,
            args.prime,
            args.claude_code,
            args.codex,
            args.pi_models is not None,
            args.prime_models is not None,
            args.claude_settings is not None,
            args.codex_profile is not None,
        )
    )
    configure_pi = args.pi or args.pi_models is not None or args.all or not explicit
    configure_prime = args.prime or args.prime_models is not None
    configure_claude = (
        args.claude_code or args.claude_settings is not None or args.all or not explicit
    )
    configure_codex = args.codex or args.codex_profile is not None or args.all or not explicit
    host = str(runtime.get("host", DEFAULT_HOST))
    port = int(runtime.get("port", DEFAULT_PORT))

    # Build and validate every selected output before changing the first file.
    # Each plan is label, path, rendered text, writer kind, and file label.
    plans: list[tuple[str, Path, str, str, str]] = []
    if configure_pi or configure_prime:
        generated = generate_client_providers(catalog, host, port, args.state_dir)
        if configure_pi:
            path = canonical_client_path(
                args.pi_models or Path.home() / ".pi/agent/models.json"
            )
            plans.append(
                ("Pi", path, render_merged_client_models(path, generated), "pi", "client models.json")
            )
        if configure_prime:
            path = canonical_client_path(
                args.prime_models or Path.home() / ".prime/agent/models.json"
            )
            plans.append(
                (
                    "prime-agent",
                    path,
                    render_merged_client_models(path, generated),
                    "pi",
                    "client models.json",
                )
            )
    if configure_claude:
        path = canonical_client_path(
            args.claude_settings or Path.home() / ".claude/settings.json"
        )
        rendered = render_claude_code_settings(
            path,
            client_token_command(args.state_dir),
            local_base_url("anthropic-messages", host, port),
            args.claude_model,
        )
        plans.append(("Claude Code", path, rendered, "private", "Claude Code settings"))
    if configure_codex:
        path = canonical_client_path(
            args.codex_profile or Path.home() / ".codex/ica-router.config.toml"
        )
        rendered = generate_codex_profile(
            args.state_dir,
            local_base_url("openai-responses", host, port),
            args.codex_model,
        )
        plans.append(("Codex", path, rendered, "private", "Codex ICA router profile"))

    paths = [plan[1] for plan in plans]
    if len(set(paths)) != len(paths):
        raise ConfigError("selected harness configuration paths must be distinct")
    snapshots: dict[Path, tuple[bool, str | None, int | None]] = {}
    for _label, path, _rendered, _kind, file_label in plans:
        if path.exists() or path.is_symlink():
            _reject_unsafe_existing_file(path, file_label)
            snapshots[path] = (
                True,
                path.read_text(encoding="utf-8-sig"),
                stat.S_IMODE(path.stat().st_mode) if os.name != "nt" else None,
            )
        else:
            snapshots[path] = (False, None, None)

    results: list[tuple[str, Path, bool, Path | None]] = []
    applied: list[Path] = []
    try:
        for label, path, rendered, kind, file_label in plans:
            if kind == "pi":
                changed, backup = write_rendered_client_models(path, rendered)
            else:
                changed, backup = write_private_client_file(path, rendered, file_label)
            applied.append(path)
            results.append((label, path, changed, backup))
    except BaseException:
        rollback_errors: list[str] = []
        for path in reversed(applied):
            existed, old_text, old_mode = snapshots[path]
            try:
                if existed and old_text is not None:
                    atomic_write(path, old_text, private=True)
                    if os.name != "nt" and old_mode is not None:
                        path.chmod(old_mode)
                    elif os.name == "nt":
                        restrict_windows_file(path)
                else:
                    path.unlink(missing_ok=True)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            eprint("WARNING: harness configuration rollback was incomplete: " + "; ".join(rollback_errors))
        raise

    for label, path, changed, backup in results:
        print(f"{label}: {'updated' if changed else 'already current'}: {path}")
        if backup:
            print(f"Backup: {backup}")
    if configure_claude:
        print("Claude Code shell ANTHROPIC_API_KEY/AUTH_TOKEN values must be unset to avoid conflicts.")
    if configure_codex:
        print("Use Codex 0.134.0 or later with: codex --profile ica-router")
    return 0


def executable_for_venv(venv: Path) -> Path:
    candidates = (
        [venv / "Scripts" / "litellm.exe", venv / "Scripts" / "litellm"]
        if os.name == "nt"
        else [venv / "bin" / "litellm"]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ConfigError(f"LiteLLM executable not found in venv: {venv}")


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            [windows_system_command("tasklist.exe"), "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            closing = raw.rfind(")")
            return closing > 0 and raw[closing + 2 :].split()[0] != "Z"
        except (OSError, IndexError, UnicodeError):
            return False
    result = subprocess.run(
        [posix_system_command("ps"), "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")

def read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError, UnicodeError):
        return None
    return value if value > 0 else None


@contextmanager
def command_lock(state_dir: Path) -> Iterator[None]:
    """Serialize lifecycle commands without trusting a PID file alone."""
    ensure_private_directory(state_dir)
    lock_path = state_dir / "command.lock"
    _reject_unsafe_existing_file(lock_path, "command lock")
    if os.name == "nt":
        # Set the DACL with no CRT handle open, then lock the stable file.
        with lock_path.open("a+b"):
            pass
        restrict_windows_file(lock_path)
        handle = lock_path.open("a+b")
    else:
        handle = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as exc:
                raise ConfigError("another router lifecycle command is in progress") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise ConfigError("another router lifecycle command is in progress") from exc
        yield
    finally:
        try:
            if acquired and os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif acquired:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

def process_start_token(pid: int) -> str | None:
    if not process_alive(pid):
        return None
    if sys.platform.startswith("linux"):
        try:
            # Field 22 is process start time in clock ticks since boot. Pair it
            # with boot_id so a reboot cannot make an old run record match.
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            closing = raw.rfind(")")
            fields_after_comm = raw[closing + 2 :].split()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            return f"linux:{boot_id}:{fields_after_comm[19]}"
        except (OSError, IndexError, UnicodeError):
            return None
    if os.name == "nt":
        command = (
            f"$p=Get-Process -Id {pid} -ErrorAction Stop; "
            "$p.StartTime.ToUniversalTime().Ticks"
        )
        result = subprocess.run(
            [windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        # macOS/BSD ps exposes only second resolution, so command and executable
        # identity are also required before any signal is sent.
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        result = subprocess.run(
            [posix_system_command("ps"), "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    value = " ".join(result.stdout.split())
    prefix = "windows" if os.name == "nt" else "posix"
    return f"{prefix}:{value}" if result.returncode == 0 and value else None

def process_command_line(pid: int) -> str | None:
    if not process_alive(pid):
        return None
    if os.name == "nt":
        command = (
            f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={pid}"; '
            "if ($null -ne $p) { $p.CommandLine }"
        )
        result = subprocess.run(
            [windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            [posix_system_command("ps"), "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def load_run_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    document = load_private_json(path, "router run state")
    required = {
        "schemaVersion", "pid", "startToken", "executable", "configPath",
        "configSha256", "host", "port"
    }
    if not isinstance(document, dict) or not required.issubset(document):
        raise ConfigError(f"invalid router run state: {path}")
    if document.get("schemaVersion") != SCHEMA_VERSION or not isinstance(document.get("pid"), int):
        raise ConfigError(f"invalid router run state: {path}")
    return document


def process_matches_run_state(document: dict[str, Any]) -> tuple[bool, str]:
    pid = int(document["pid"])
    if not process_alive(pid):
        return False, "process is not alive"
    token = process_start_token(pid)
    if token != document.get("startToken"):
        return False, "process creation time does not match"
    command_line = process_command_line(pid)
    if not command_line:
        return False, "process command line is unavailable"
    for expected in (str(document.get("executable", "")), str(document.get("configPath", ""))):
        if not expected or expected not in command_line:
            return False, "process command line does not match the recorded router"
    return True, "matched"



def require_router_stopped_for_mutation(state_dir: Path) -> None:
    def reject_foreign_listener() -> None:
        runtime_path = state_dir / "runtime.json"
        if runtime_path.exists():
            runtime = validate_runtime(load_private_json(runtime_path, "runtime settings"))
            if port_is_open(str(runtime["host"]), int(runtime["port"])):
                raise ConfigError("configured port is owned by an unmanaged or foreign process")

    run_path = state_dir / "run.json"
    document = load_run_state(run_path)
    if document is None:
        reject_foreign_listener()
        return
    pid = int(document["pid"])
    if not process_alive(pid):
        run_path.unlink(missing_ok=True)
        reject_foreign_listener()
        return
    matched, reason = process_matches_run_state(document)
    if matched:
        raise ConfigError(f"router PID {pid} is running; stop it before changing private state")
    raise ConfigError(f"stale run state for live PID {pid}; refusing state mutation: {reason}")

def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def terminate_process_group(pid: int, expected_start_token: str, timeout: float = 10.0) -> None:
    """Terminate only while the precise recorded process instance still matches."""
    if process_start_token(pid) != expected_start_token:
        if process_alive(pid):
            raise ConfigError(f"refusing to signal PID {pid}: process creation time changed")
        return
    if os.name == "nt":
        subprocess.run(
            [windows_system_command("taskkill.exe"), "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + timeout
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not process_alive(pid):
        return
    if process_start_token(pid) != expected_start_token:
        raise ConfigError(f"refusing to force-kill PID {pid}: process creation time changed")
    if os.name == "nt":
        subprocess.run(
            [windows_system_command("taskkill.exe"), "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + 5.0
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_alive(pid) and process_start_token(pid) == expected_start_token:
        raise ConfigError(f"router PID {pid} survived forced termination")


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=min(1.0, max(timeout, 0.05))):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _local_http_get(
    host: str, port: int, path: str, headers: dict[str, str], timeout: float, limit: int
) -> tuple[int, bytes]:
    """Direct loopback HTTP; never consult ambient proxy environment variables."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(limit + 1)
        return response.status, body
    finally:
        connection.close()


def wait_for_liveness(host: str, port: int, timeout: float) -> bool:
    """Probe the no-provider-call liveness route; never use billable /health."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _body = _local_http_get(
                host, port, "/health/liveliness", {}, min(1.0, max(0.1, timeout)), 65536
            )
            if 200 <= status < 300:
                return True
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.15)
    return False


def wait_for_authenticated_models(
    host: str, port: int, master_key: str, expected_alias: str, timeout: float
) -> bool:
    """Authenticate a local, config-specific probe without calling any provider."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, raw = _local_http_get(
                host,
                port,
                "/v1/models",
                {"Authorization": f"Bearer {master_key}"},
                min(1.0, max(0.1, timeout)),
                2 * 1024 * 1024,
            )
            if 200 <= status < 300 and len(raw) <= 2 * 1024 * 1024:
                document = json.loads(raw)
                ids = {
                    item.get("id")
                    for item in document.get("data", [])
                    if isinstance(item, dict)
                }
                if expected_alias in ids:
                    return True
        except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
            pass
        time.sleep(0.15)
    return False


def serve_command(args: argparse.Namespace) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    catalog, secrets_doc, runtime = load_state(args.state_dir, args.catalog)
    config_path = args.state_dir / "config.yaml"
    private_home = args.state_dir / "process-home"
    private_cache = args.state_dir / "process-cache"
    private_tmp = args.state_dir / "process-tmp"
    for directory in (private_home, private_cache, private_tmp):
        ensure_private_directory(directory)
    if os.name == "nt":
        system_dir = windows_system_directory()
        windows_dir = system_dir.parent
        env = {
            "SystemRoot": str(windows_dir),
            "WINDIR": str(windows_dir),
            "COMSPEC": str(system_dir / "cmd.exe"),
            "PATH": os.pathsep.join((str(args.venv / "Scripts"), str(system_dir))),
            "USERPROFILE": str(private_home),
            "LOCALAPPDATA": str(private_cache),
            "APPDATA": str(private_home / "AppData"),
            "TEMP": str(private_tmp),
            "TMP": str(private_tmp),
        }
    else:
        env = {
            "PATH": os.pathsep.join((str(args.venv / "bin"), "/usr/bin", "/bin")),
            "HOME": str(private_home),
            "XDG_CONFIG_HOME": str(private_home / ".config"),
            "XDG_CACHE_HOME": str(private_cache),
            "TMPDIR": str(private_tmp),
        }
    env.update(runtime_environment(secrets_doc, catalog))
    env.update({
        "LITELLM_MODE": "PRODUCTION",
        "LITELLM_LOG": "ERROR",
        "LITELLM_LOG_LEVEL": "ERROR",
        "LITELLM_TELEMETRY": "False",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "DO_NOT_TRACK": "1",
        "OTEL_SDK_DISABLED": "true",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "SCARF_NO_ANALYTICS": "true",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    litellm = executable_for_venv(args.venv)
    host = str(runtime.get("host", DEFAULT_HOST))
    port = int(runtime.get("port", DEFAULT_PORT))
    cmd = [
        str(litellm),
        "--config",
        str(config_path),
        "--host",
        host,
        "--port",
        str(port),
        "--num_workers",
        "1",
        "--telemetry",
        "False",
    ]
    return cmd, env, runtime


def router_start_context(
    args: argparse.Namespace,
) -> tuple[list[str], dict[str, str], str, int, Path, str, str]:
    litellm_cmd, child_env, runtime = serve_command(args)
    host = str(runtime["host"])
    port = int(runtime["port"])
    config_path = args.state_dir / "config.yaml"
    config = load_private_json(config_path, "generated LiteLLM config")
    try:
        expected_alias = str(config["model_list"][0]["model_name"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ConfigError("generated LiteLLM config has no model aliases") from exc
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return (
        litellm_cmd,
        child_env,
        host,
        port,
        config_path,
        config_sha256,
        expected_alias,
    )


def prepare_router_log(state_dir: Path) -> Path:
    ensure_private_directory(state_dir)
    log_path = state_dir / "router.log"
    if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
        rotated = state_dir / "router.log.1"
        _reject_unsafe_existing_file(log_path, "router log")
        if rotated.exists():
            _reject_unsafe_existing_file(rotated, "rotated router log")
            rotated.unlink()
        os.replace(log_path, rotated)
    if not log_path.exists():
        atomic_write(log_path, "", private=True)
    else:
        validate_private_file(log_path, "router log")
    if os.name != "nt":
        log_path.chmod(0o600)
    restrict_windows_file(log_path)
    return log_path


def router_run_document(
    args: argparse.Namespace,
    pid: int,
    start_token: str,
    litellm_cmd: list[str],
    config_path: Path,
    config_sha256: str,
    host: str,
    port: int,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "pid": pid,
        "startToken": start_token,
        "executable": str(Path(litellm_cmd[0]).resolve()),
        "configPath": str(config_path.resolve()),
        "configSha256": config_sha256,
        "catalogPath": str(args.catalog),
        "venvPath": str(args.venv),
        "host": host,
        "port": port,
        "startedAt": int(time.time()),
    }


def cmd_start_worker(args: argparse.Namespace) -> int:
    run_path = args.state_dir / "run.json"
    with command_lock(args.state_dir):
        (
            litellm_cmd,
            child_env,
            host,
            port,
            config_path,
            config_sha256,
            expected_alias,
        ) = router_start_context(args)
        master_key = child_env[MASTER_ENV]

        existing = load_run_state(run_path)
        if existing is not None:
            matched, reason = process_matches_run_state(existing)
            if matched:
                if existing.get("configSha256") != config_sha256:
                    raise ConfigError(f"router PID {existing['pid']} uses an older config; stop and start it")
                if wait_for_authenticated_models(host, port, master_key, expected_alias, 2.0):
                    print(f"Router already running with PID {existing['pid']}")
                    return 0
                raise ConfigError(f"router PID {existing['pid']} matches but is not healthy")
            if process_alive(int(existing["pid"])):
                raise ConfigError(f"stale run state; refusing to reuse or signal PID {existing['pid']}: {reason}")
            run_path.unlink(missing_ok=True)
        if port_is_open(host, port):
            raise ConfigError(f"local port is already occupied: {host}:{port}")

        log_path = prepare_router_log(args.state_dir)

        command = litellm_cmd
        with log_path.open("ab", buffering=0) as log:
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": log,
                "close_fds": True,
                "env": child_env,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            child = subprocess.Popen(command, **kwargs)

        start_token: str | None = None
        try:
            token_deadline = time.monotonic() + min(5.0, args.start_timeout)
            start_token = process_start_token(child.pid)
            while start_token is None and process_alive(child.pid) and time.monotonic() < token_deadline:
                time.sleep(0.05)
                start_token = process_start_token(child.pid)
            if start_token is None:
                raise ConfigError(f"router exited before process identity could be recorded; inspect private log: {log_path}")
            run_document = router_run_document(
                args,
                child.pid,
                start_token,
                litellm_cmd,
                config_path,
                config_sha256,
                host,
                port,
            )
            atomic_write(run_path, json.dumps(run_document, indent=2) + "\n", private=True)
            restrict_windows_file(run_path)

            deadline = time.monotonic() + args.start_timeout
            identity_reason = "process has not exec'd LiteLLM yet"
            while time.monotonic() < deadline:
                if not process_alive(child.pid):
                    raise ConfigError(f"router exited during startup; inspect private log: {log_path}")
                matched, identity_reason = process_matches_run_state(run_document)
                if (
                    matched
                    and wait_for_liveness(host, port, 0.5)
                    and wait_for_authenticated_models(host, port, master_key, expected_alias, 1.0)
                ):
                    if process_alive(child.pid):
                        print(f"Router started: PID {child.pid}, http://{host}:{port}")
                        print(f"Private log: {log_path}")
                        return 0
                time.sleep(0.1)
            raise ConfigError(
                f"router startup did not pass identity and liveness checks on {host}:{port} "
                f"({identity_reason}); inspect private log: {log_path}"
            )
        except BaseException:
            if start_token is not None:
                terminate_process_group(child.pid, start_token)
            elif child.poll() is None:
                # No token was obtainable, but this is still our live Popen handle.
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            run_path.unlink(missing_ok=True)
            raise


def cmd_run_foreground(args: argparse.Namespace) -> int:
    if os.name == "nt":
        raise ConfigError("run-foreground is supported only on Unix-like systems")
    run_path = args.state_dir / "run.json"
    with command_lock(args.state_dir):
        (
            litellm_cmd,
            child_env,
            host,
            port,
            config_path,
            config_sha256,
            _expected_alias,
        ) = router_start_context(args)
        existing = load_run_state(run_path)
        if existing is not None:
            matched, reason = process_matches_run_state(existing)
            if matched:
                raise ConfigError(f"router is already running with PID {existing['pid']}")
            if process_alive(int(existing["pid"])):
                raise ConfigError(
                    f"stale run state; refusing live PID {existing['pid']}: {reason}"
                )
            run_path.unlink(missing_ok=True)
        if port_is_open(host, port):
            raise ConfigError(f"local port is already occupied: {host}:{port}")

        log_path = prepare_router_log(args.state_dir)
        if os.getpgrp() != os.getpid():
            os.setsid()
        pid = os.getpid()
        start_token = process_start_token(pid)
        if start_token is None:
            raise ConfigError("could not record foreground router process identity")
        run_document = router_run_document(
            args,
            pid,
            start_token,
            litellm_cmd,
            config_path,
            config_sha256,
            host,
            port,
        )
        atomic_write(run_path, json.dumps(run_document, indent=2) + "\n", private=True)
        restrict_windows_file(run_path)

        try:
            sys.stdout.flush()
            sys.stderr.flush()
            with open(os.devnull, "rb", buffering=0) as null_in, log_path.open(
                "ab", buffering=0
            ) as log:
                os.dup2(null_in.fileno(), 0)
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
            os.execve(litellm_cmd[0], litellm_cmd, child_env)
        except BaseException:
            run_path.unlink(missing_ok=True)
            raise
    return 0


def cmd_wait_ready(args: argparse.Namespace) -> int:
    (
        _litellm_cmd,
        child_env,
        host,
        port,
        _config_path,
        config_sha256,
        expected_alias,
    ) = router_start_context(args)
    master_key = child_env[MASTER_ENV]
    run_path = args.state_dir / "run.json"
    deadline = time.monotonic() + args.start_timeout
    last_reason = "run state is not available"
    while time.monotonic() < deadline:
        try:
            document = load_run_state(run_path)
            if document is None:
                last_reason = "run state is not available"
            elif document.get("configSha256") != config_sha256:
                last_reason = "run state uses an older configuration"
            else:
                matched, last_reason = process_matches_run_state(document)
                if (
                    matched
                    and wait_for_liveness(host, port, 0.5)
                    and wait_for_authenticated_models(host, port, master_key, expected_alias, 1.0)
                ):
                    print(f"Router ready: PID {document['pid']}, http://{host}:{port}")
                    return 0
                if not process_alive(int(document["pid"])):
                    last_reason = "router process exited"
        except ConfigError as exc:
            last_reason = str(exc)
        time.sleep(0.15)
    raise ConfigError(
        f"router readiness timed out on {host}:{port}: {last_reason}; "
        f"inspect private log: {args.state_dir / 'router.log'}"
    )


def cmd_stop_worker(args: argparse.Namespace) -> int:
    run_path = args.state_dir / "run.json"
    with command_lock(args.state_dir):
        document = load_run_state(run_path)
        if document is None:
            print("Router is not running")
            return 0
        pid = int(document["pid"])
        if not process_alive(pid):
            run_path.unlink(missing_ok=True)
            print("Router is not running (removed stale state)")
            return 0
        matched, reason = process_matches_run_state(document)
        if not matched:
            raise ConfigError(f"refusing to signal PID {pid}: {reason}")
        terminate_process_group(pid, str(document["startToken"]))
        if process_alive(pid):
            raise ConfigError(f"router PID {pid} did not stop")
        run_path.unlink(missing_ok=True)
        print(f"Router stopped: PID {pid}")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_path = args.state_dir / "run.json"
    with command_lock(args.state_dir):
        document = load_run_state(run_path)
        if document is None:
            print("stopped")
            return 1
        pid = int(document["pid"])
        if not process_alive(pid):
            run_path.unlink(missing_ok=True)
            print("stopped (removed stale state)")
            return 1
        matched, reason = process_matches_run_state(document)
        if not matched:
            print(f"stale PID={pid} identity=no reason={reason}")
            return 3
        config_path = args.state_dir / "config.yaml"
        validate_private_file(config_path, "generated LiteLLM config")
        current_config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
        if document.get("configSha256") != current_config_sha256:
            print(f"running PID={pid} identity=yes config=changed restart-required=yes")
            return 2
        runtime = validate_runtime(load_private_json(args.state_dir / "runtime.json", "runtime settings"))
        host, port = str(runtime["host"]), int(runtime["port"])
        live = wait_for_liveness(host, port, 1.0)
        print(f"running PID={pid} URL=http://{host}:{port} identity=yes liveness={'yes' if live else 'no'}")
        return 0 if live else 2

def cmd_doctor(args: argparse.Namespace) -> int:
    catalog, secrets_doc, runtime = load_state(args.state_dir, args.catalog)
    config = load_private_json(args.state_dir / "config.yaml", "generated LiteLLM config")
    expected = sum(
        len(catalog["providers"][pid]["models"]) * len(secrets_doc["pools"][pool["id"]]["keys"])
        for pool in catalog["pools"]
        for pid in pool["providers"]
    )
    actual = len(config.get("model_list", [])) if isinstance(config, dict) else -1
    if actual != expected:
        raise ConfigError(f"deployment count mismatch: expected {expected}, got {actual}")
    rendered = json.dumps(config, ensure_ascii=False)
    for pool in secrets_doc["pools"].values():
        for entry in pool["keys"]:
            if entry["value"] in rendered:
                raise ConfigError("raw provider key leaked into generated config")
    if secrets_doc["masterKey"] in rendered:
        raise ConfigError("raw master key leaked into generated LiteLLM config")
    if os.name != "nt":
        for name in ("secrets.json", "config.yaml", "client-models.generated.json", "runtime.json", "generation.json"):
            mode = (args.state_dir / name).stat().st_mode & 0o777
            if mode & 0o077:
                raise ConfigError(f"private file permissions are too broad: {name} mode={oct(mode)}")
    print(
        f"OK: {actual} deployments, "
        f"{sum(len(p['models']) for p in catalog['providers'].values())} models, "
        f"{sum(len(p['keys']) for p in secrets_doc['pools'].values())} keys, "
        f"listen={runtime.get('host')}:{runtime.get('port')}"
    )
    return 0


def systemd_user_unit_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "systemd" / "user" / SYSTEMD_UNIT_NAME


def systemd_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ConfigError("systemd command argument contains a control character")
    # Percent is a systemd specifier even inside quotes; double it for a literal.
    return json.dumps(value.replace("%", "%%"))


def generate_systemd_user_unit(state_dir: Path) -> str:
    if not sys.platform.startswith("linux"):
        raise ConfigError("systemd user service is supported only on Linux")
    wrapper = state_dir.parent / "ica-router"
    wrapper_arg = systemd_quote(str(wrapper))
    return (
        f"{SYSTEMD_UNIT_MARKER}\n"
        "[Unit]\n"
        "Description=ICA LiteLLM Key Router\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=exec\n"
        "UMask=0077\n"
        f"ExecStart={wrapper_arg} run-foreground\n"
        f"ExecStartPost={wrapper_arg} wait-ready --start-timeout 120\n"
        f"ExecStop={wrapper_arg} stop-worker\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "TimeoutStartSec=150s\n"
        "TimeoutStopSec=30s\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def managed_systemd_user_unit(state_dir: Path) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    unit_path = systemd_user_unit_path()
    if not unit_path.exists() and not unit_path.is_symlink():
        return False
    _reject_unsafe_existing_file(unit_path, "systemd user unit")
    content = unit_path.read_text(encoding="utf-8")
    return SYSTEMD_UNIT_MARKER in content and content == generate_systemd_user_unit(state_dir)


def run_systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    systemctl = posix_system_command("systemctl")
    result = subprocess.run(
        [systemctl, "--user", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ConfigError(
            f"systemctl --user {' '.join(arguments)} failed"
            + (f": {detail}" if detail else "")
        )
    return result


def cmd_start(args: argparse.Namespace) -> int:
    if managed_systemd_user_unit(args.state_dir):
        run_systemctl("start", SYSTEMD_UNIT_NAME)
        return cmd_wait_ready(args)
    return cmd_start_worker(args)


def cmd_stop(args: argparse.Namespace) -> int:
    if managed_systemd_user_unit(args.state_dir):
        run_systemctl("stop", SYSTEMD_UNIT_NAME)
        if run_systemctl("is-active", SYSTEMD_UNIT_NAME, check=False).returncode == 0:
            raise ConfigError(f"systemd user service did not stop: {SYSTEMD_UNIT_NAME}")
        print(f"Router stopped through systemd: {SYSTEMD_UNIT_NAME}")
        return 0
    return cmd_stop_worker(args)


def cmd_install_systemd_user(args: argparse.Namespace) -> int:
    if not sys.platform.startswith("linux"):
        raise ConfigError("systemd user service is supported only on Linux")
    wrapper = args.state_dir.parent / "ica-router"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise ConfigError(f"router wrapper is unavailable: {wrapper}")
    # Verify generated state before replacing any current lifecycle owner.
    load_state(args.state_dir, args.catalog)
    unit_path = systemd_user_unit_path()
    rendered = generate_systemd_user_unit(args.state_dir)
    prior_unit: str | None = None
    if unit_path.exists() or unit_path.is_symlink():
        _reject_unsafe_existing_file(unit_path, "systemd user unit")
        prior_unit = unit_path.read_text(encoding="utf-8")
        if SYSTEMD_UNIT_MARKER not in prior_unit:
            raise ConfigError(f"refusing to replace an unmanaged systemd unit: {unit_path}")
    prior_enabled = run_systemctl("is-enabled", SYSTEMD_UNIT_NAME, check=False).returncode == 0
    prior_active = run_systemctl("is-active", SYSTEMD_UNIT_NAME, check=False).returncode == 0
    prior_run = load_run_state(args.state_dir / "run.json")
    prior_router_running = False
    if prior_run is not None:
        prior_router_running, _reason = process_matches_run_state(prior_run)
    if (
        prior_unit == rendered
        and prior_enabled
        and prior_active
        and cmd_status(args) == 0
    ):
        print(f"systemd user service already current and active: {unit_path}")
        return 0

    backup: Path | None = None
    changed = False
    try:
        if prior_unit is not None:
            stopped = run_systemctl("stop", SYSTEMD_UNIT_NAME, check=False)
            if prior_active and stopped.returncode != 0:
                detail = (stopped.stderr or stopped.stdout).strip()
                raise ConfigError(
                    "could not stop existing systemd user service"
                    + (f": {detail}" if detail else "")
                )
        # Stop a manually started router after stopping any older managed unit.
        cmd_stop_worker(args)
        changed, backup = write_private_client_file(unit_path, rendered, "systemd user unit")
        run_systemctl("daemon-reload")
        run_systemctl("enable", "--now", SYSTEMD_UNIT_NAME)
        active = run_systemctl("is-active", SYSTEMD_UNIT_NAME)
        if active.stdout.strip() != "active":
            raise ConfigError(f"systemd user unit did not become active: {SYSTEMD_UNIT_NAME}")
    except BaseException:
        rollback_errors: list[str] = []
        try:
            run_systemctl("disable", "--now", SYSTEMD_UNIT_NAME, check=False)
            if prior_unit is None:
                unit_path.unlink(missing_ok=True)
            else:
                atomic_write(unit_path, prior_unit, private=True)
            run_systemctl("daemon-reload")
            if prior_enabled:
                run_systemctl("enable", SYSTEMD_UNIT_NAME)
            else:
                run_systemctl("disable", SYSTEMD_UNIT_NAME, check=False)
            if prior_active:
                run_systemctl("start", SYSTEMD_UNIT_NAME)
            elif prior_router_running:
                cmd_start_worker(args)
        except BaseException as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            eprint("WARNING: systemd rollback was incomplete: " + "; ".join(rollback_errors))
        raise

    print(f"systemd user service {'installed' if changed else 'already current'}: {unit_path}")
    if backup:
        print(f"Backup: {backup}")
    print("Autostart is enabled for user login; boot-without-login additionally requires user lingering.")
    return 0


def cmd_uninstall_systemd_user(args: argparse.Namespace) -> int:
    if not sys.platform.startswith("linux"):
        raise ConfigError("systemd user service is supported only on Linux")
    unit_path = systemd_user_unit_path()
    if unit_path.exists() or unit_path.is_symlink():
        _reject_unsafe_existing_file(unit_path, "systemd user unit")
        if SYSTEMD_UNIT_MARKER not in unit_path.read_text(encoding="utf-8"):
            raise ConfigError(f"refusing to remove an unmanaged systemd unit: {unit_path}")
    active = run_systemctl("is-active", SYSTEMD_UNIT_NAME, check=False).returncode == 0
    disabled = run_systemctl("disable", "--now", SYSTEMD_UNIT_NAME, check=False)
    if active and disabled.returncode != 0:
        detail = (disabled.stderr or disabled.stdout).strip()
        raise ConfigError(
            f"could not stop and disable systemd user service"
            + (f": {detail}" if detail else "")
        )
    unit_path.unlink(missing_ok=True)
    run_systemctl("daemon-reload")
    print(f"Removed systemd user service: {SYSTEMD_UNIT_NAME}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    default_catalog = Path(__file__).resolve().parents[1] / "catalog.json"
    parser.add_argument("--catalog", type=Path, default=default_catalog)
    parser.add_argument("--venv", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap", help="import/prompt keys and generate all configuration")
    bootstrap.add_argument("--import-key-rotator", default="auto")
    bootstrap.add_argument("--replace-secrets", action="store_true")
    bootstrap.add_argument(
        "--rotate-master-key", action="store_true",
        help="rotate the local proxy key when replacing upstream secrets",
    )
    bootstrap.add_argument(
        "--prompt-keys", action="store_true",
        help="read API keys interactively until an empty value ends each pool",
    )
    bootstrap.add_argument("--non-interactive", action="store_true")
    bootstrap.add_argument("--client", action="append", default=[])
    bootstrap.add_argument(
        "--no-configure-clients", action="store_true",
        help="generate local state without writing Pi/prime-agent models.json",
    )
    bootstrap.add_argument("--host", default=DEFAULT_HOST)
    bootstrap.add_argument("--port", type=int, default=None)
    bootstrap.add_argument("--max-fallbacks", type=int, default=None)
    bootstrap.add_argument("--cooldown-seconds", type=int, default=None)
    bootstrap.set_defaults(func=cmd_bootstrap)

    generate = sub.add_parser("generate", help="regenerate config from existing private state")
    generate.set_defaults(func=cmd_generate)

    clients = sub.add_parser("configure-clients", help="merge local providers into Pi/prime-agent models.json")
    clients.add_argument("--client", action="append", default=[])
    clients.set_defaults(func=cmd_configure_clients)

    harnesses = sub.add_parser(
        "configure-harnesses",
        help="configure Pi, Claude Code, and a separate Codex router profile",
    )
    harnesses.add_argument("--all", action="store_true", help="configure Pi, Claude Code, and Codex")
    harnesses.add_argument("--pi", action="store_true")
    harnesses.add_argument("--prime", action="store_true")
    harnesses.add_argument("--claude-code", action="store_true")
    harnesses.add_argument("--codex", action="store_true")
    harnesses.add_argument("--pi-models", type=Path, default=None)
    harnesses.add_argument("--prime-models", type=Path, default=None)
    harnesses.add_argument("--claude-settings", type=Path, default=None)
    harnesses.add_argument("--codex-profile", type=Path, default=None)
    harnesses.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    harnesses.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    harnesses.set_defaults(func=cmd_configure_harnesses)

    token = sub.add_parser("client-token", help="print the local router credential for a client helper")
    token.add_argument("--bearer", action="store_true", help="prefix the credential with 'Bearer '")
    token.set_defaults(func=cmd_client_token)

    start = sub.add_parser("start", help="start LiteLLM in the background")
    start.add_argument("--start-timeout", type=float, default=120.0)
    start.set_defaults(func=cmd_start)

    foreground = sub.add_parser(
        "run-foreground", help="exec LiteLLM in the foreground for a service manager"
    )
    foreground.set_defaults(func=cmd_run_foreground)

    ready = sub.add_parser("wait-ready", help="wait for authenticated router readiness")
    ready.add_argument("--start-timeout", type=float, default=120.0)
    ready.set_defaults(func=cmd_wait_ready)

    stop = sub.add_parser("stop", help="stop the background or service-managed router")
    stop.set_defaults(func=cmd_stop)

    stop_worker = sub.add_parser("stop-worker", help=argparse.SUPPRESS)
    stop_worker.set_defaults(func=cmd_stop_worker)

    status = sub.add_parser("status", help="show background router status")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="validate files without calling providers")
    doctor.set_defaults(func=cmd_doctor)

    systemd_install = sub.add_parser(
        "install-systemd-user", help="install and start a systemd user service on Linux"
    )
    systemd_install.set_defaults(func=cmd_install_systemd_user)

    systemd_remove = sub.add_parser(
        "uninstall-systemd-user", help="disable and remove the managed systemd user service"
    )
    systemd_remove.set_defaults(func=cmd_uninstall_systemd_user)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    expanded_state = args.state_dir.expanduser()
    if expanded_state.is_symlink():
        parser.error("--state-dir must not be a symlink")
    # Canonicalize ancestor aliases so process argv and recorded lifecycle identity agree.
    args.state_dir = Path(os.path.abspath(expanded_state)).resolve(strict=False)
    args.catalog = args.catalog.expanduser().resolve()
    if args.venv is None:
        args.venv = default_venv_dir(args.state_dir)
    else:
        args.venv = args.venv.expanduser().resolve()
    if getattr(args, "port", None) is not None and not (1 <= args.port <= 65535):
        parser.error("--port must be 1-65535")
    if getattr(args, "max_fallbacks", None) is not None and not (0 <= args.max_fallbacks <= 20):
        parser.error("--max-fallbacks must be 0-20")
    if getattr(args, "cooldown_seconds", None) is not None and not (1 <= args.cooldown_seconds <= 86400):
        parser.error("--cooldown-seconds must be 1-86400")
    if getattr(args, "start_timeout", None) is not None and args.start_timeout <= 0:
        parser.error("--start-timeout must be positive")
    try:
        if args.command in {"bootstrap", "generate", "configure-clients"}:
            with command_lock(args.state_dir):
                require_router_stopped_for_mutation(args.state_dir)
                return int(args.func(args))
        if args.command in {"doctor", "configure-harnesses"}:
            with command_lock(args.state_dir):
                return int(args.func(args))
        return int(args.func(args))
    except ConfigError as exc:
        eprint(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("Cancelled")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
