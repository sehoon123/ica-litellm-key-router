# ICA LiteLLM Key Router

[English](README.md) | [한국어](README.ko.md)

[![CI](https://github.com/sehoon123/ica-litellm-key-router/actions/workflows/ci.yml/badge.svg)](https://github.com/sehoon123/ica-litellm-key-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A local, authenticated [LiteLLM](https://github.com/BerriAI/litellm) proxy that spreads IBM ICA Services Essentials requests across authorized API keys. It keeps the native OpenAI Responses, Anthropic Messages, and Gemini `generateContent` interfaces used by Pi, prime-agent, Codex, and Claude Code.

The router does not issue credentials. Use only credentials and IBM services that you are authorized to use.

## What it does

- Runs exactly one LiteLLM worker on `127.0.0.1:4000` by default.
- Exposes three local client providers and 12 provider-qualified model aliases.
- Creates one LiteLLM deployment per alias and credential in that alias's pool.
- Selects a healthy deployment randomly with `simple-shuffle`; this is not round-robin.
- Cools down a deployment immediately after configured failure classes and retries eligible pre-output failures.
- Sets provider SDK retries to zero and disables weighted failover.
- Keeps raw upstream keys out of generated `config.yaml`.
- Uses a command-backed client credential, so generated Pi, Claude Code, and Codex configuration does not persist another copy of the local master key.
- Configures Pi/prime-agent, Claude Code, and a separate Codex profile without deleting unrelated settings or replacing Codex subscription defaults.
- Can install a supervised, restart-on-failure `systemd --user` service on Linux.
- Installs a private, exact runtime: Python `3.12.13`, `uv` `0.12.2`, and LiteLLM `1.98.0` from `uv.lock`.

## Architecture and trust boundary

```text
Pi / prime-agent / Claude Code / Codex / curl
        |
        | command-fetched local master key + loopback HTTP
        v
LiteLLM 1.98.0, one worker, 127.0.0.1:4000
        |
        | alias -> random healthy credential deployment
        | provider keys supplied through process environment
        v
IBM ICA Services Essentials HTTPS gateway
  - ica-services-essentials pool
```

`tools/routerctl.py` is the standard-library-only control plane. LiteLLM is the data plane. The installer creates a versioned release, generates private state, runs an offline `doctor`, and starts the router.

“Local-only” applies to the client-facing listener. Prompts, responses, tool data, and model metadata still leave the machine over HTTPS for the IBM endpoint selected by `catalog.json`. This project has no hosted relay or database. The launcher creates a minimal LiteLLM child environment with a fixed path, key variables, forced production/error logging, telemetry controls, and private `process-home`, `process-cache`, and `process-tmp` directories under state. Ambient proxy/custom-CA variables and other environment-based provider features are not inherited, so direct IBM HTTPS access with the normal system trust configuration is required.

The loopback listener uses HTTP because it does not leave the host. Bootstrap and runtime validation reject any host other than `127.0.0.1`; the port is configurable. This project does not provide TLS, remote access controls, or multi-user isolation.

### Credential pools and upstreams

| Pool | Catalog providers | HTTPS bases from `catalog.json` |
|---|---|---|
| `ica-services-essentials` | `ica-se-openai`, `ica-se-claude`, `ica-se-gemini` | `https://api.servicesessentials.ibm.com/v1`, `https://api.servicesessentials.ibm.com`, `https://api.servicesessentials.ibm.com/v1beta` |

The OpenAI entries use LiteLLM's Azure Responses transformer with API version `v1`. Provider-qualified aliases keep the OpenAI, Anthropic, and Gemini surfaces as distinct LiteLLM model groups. Treat changes to `catalog.json` as changes to credential destinations. `ibm-ica-nextgen` is deprecated and is not generated or contacted.
The original IBM Services Essentials Responses endpoint is `https://api.servicesessentials.ibm.com/v1/responses`. Generated LiteLLM `api_base` values add `?_litellm_route=/openai/responses` only as a LiteLLM `1.98.0` URL-builder compatibility marker so its Azure transformer does not append a second `/openai/responses`. It is not an IBM API parameter; omit it from direct IBM calls.

### Routing and retry behavior

The generated router settings use:

- `routing_strategy: simple-shuffle` with equal weights;
- `enable_weighted_failover: false`;
- router `num_retries: 2` by default, capped at the number of alternate keys (`key count - 1`), with `--max-fallbacks` / `maxFallbacks` as the configured ceiling;
- per-deployment/provider SDK `max_retries: 0`;
- zero retries for LiteLLM `BadRequestError` and `ContentPolicyViolationError`;
- up to two router retries for authentication, timeout, and rate-limit errors; the same global count necessarily covers intended 5xx retries and stock LiteLLM 1.98.0's other standard retryable statuses/exceptions;
- `allowed_fails: 0` and zero allowed failures for authentication, rate limit, timeout, internal-server, unavailable, and bad-gateway classes, making affected deployments immediately eligible for cooldown;
- a 60-second cooldown by default; and
- pre-call checks for Responses deployments and encrypted-content affinity.

**`simple-shuffle` is random, not round-robin.** It does not promise a stable key order, equal short-term traffic, or that every key will be tried. With the default `maxFallbacks: 2`, an eligible request can make the initial attempt plus at most two router retries, but never more retries than there are alternate keys. One configured key therefore produces zero router retries. Retries remain within the deployments for the same provider-qualified alias; there is no cross-model or cross-provider fallback map.

Explicit policy entries block retries for bad requests and content-policy failures. Stock LiteLLM `1.98.0` has no `InternalServerErrorRetries` policy key, so the global count must remain broader to cover intended 5xx retry behavior; it can also follow LiteLLM's other standard retryable-status decisions. This is not a strict four-class retry allowlist.

A pre-output failure can be ambiguous. IBM may have accepted, executed, or billed work before the router observes an error and retries it. This is not an exactly-once system. **There is no mid-stream failover.** Once response bytes start, a failed stream must be retried by the client, with the same duplicate-work and cost risk.

The router deliberately uses a **single worker**. Its local process identity, lock, cooldown state, and controller are not a multi-worker or distributed design.

## Client providers and all 12 aliases

Bootstrap generates these three provider IDs. Pi and prime-agent show their models under names ending in `(key router)`.

| Local client provider | Native API | Model aliases |
|---|---|---|
| `ica-se-openai-router` | OpenAI Responses | `ica-se-openai--gpt-5.6-luna-dzus`<br>`ica-se-openai--gpt-5.6-terra-dzus`<br>`ica-se-openai--gpt-5.6-sol` |
| `ica-se-claude-router` | Anthropic Messages | `ica-se-claude--claude-sonnet-4-6`<br>`ica-se-claude--claude-sonnet-5`<br>`ica-se-claude--claude-opus-4-6`<br>`ica-se-claude--claude-opus-4-8`<br>`ica-se-claude--claude-opus-5`<br>`ica-se-claude--claude-haiku-4-5` |
| `ica-se-gemini-router` | Gemini `generateContent` | `ica-se-gemini--gemini-3.7-flash`<br>`ica-se-gemini--gemini-3.6-flash`<br>`ica-se-gemini--gemini-3.5-flash` |

Deployment count is:

```text
12 × number of ica-services-essentials keys
```

The required minimum of one key produces 12 deployments.

## Requirements

- One or more distinct, authorized keys in each catalog pool.
- Direct outbound HTTPS to the IBM gateways at runtime.
- Installation access to GitHub, `astral.sh`, PyPI, and the source used by `uv` for managed Python.
- A free loopback port, `4000` by default.
- Linux: Bash, `curl`, and `sha256sum` or `shasum`.
- Windows: Windows PowerShell 5.1 or PowerShell 7, NTFS-style ACL support, and the inbox `%SystemRoot%\System32\curl.exe`.
- Disk space for the private tool cache and a separate environment for each retained release.

The installer does not need a preinstalled project Python. It verifies and privately installs `uv` `0.12.2`, installs exact Python `3.12.13`, runs `uv sync --frozen --no-dev`, runs `uv pip check`, and verifies LiteLLM `1.98.0`. Ambient `UV_*`, `PIP_*`, and `PYTHON*` settings do not select dependencies. Other operating systems and shared-service deployments are unsupported.

## Quick start from a reviewed clone

Clone and review the revision you intend to trust, then install from that local source tree:

```bash
git clone https://github.com/sehoon123/ica-litellm-key-router.git
cd ica-litellm-key-router
```

If `~/.pi/agent/key-rotator.json` already contains the `ica-services-essentials` pool, the shortest non-interactive setup imports it automatically and enables the supervised Linux user service:

```bash
ICA_ROUTER_NON_INTERACTIVE=1 bash ./install-linux.sh --systemd-user
```

On a fresh machine without an importable key-rotator file, run interactively and enter the authorized Services Essentials keys when prompted:

```bash
bash ./install-linux.sh --systemd-user
```

Configure Pi, Claude Code, and a separate Codex profile. Generated clients fetch the local credential through `ica-router client-token`; they do not persist the token:

```bash
$HOME/.local/share/ica-litellm-key-router/ica-router configure-harnesses --all
```

Verify the router and GPT-5.6 Sol through Pi:

```bash
$HOME/.local/share/ica-litellm-key-router/ica-router doctor
$HOME/.local/share/ica-litellm-key-router/ica-router status
pi --print \
  --model 'ica-se-openai-router/ica-se-openai--gpt-5.6-sol' \
  'Reply with exactly: OK'
```

With Codex 0.134.0 or later, use `codex --profile ica-router` for the generated `~/.codex/ica-router.config.toml` profile. Claude Code reads `~/.claude/settings.json`; unset shell-level `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` values that would conflict with `apiKeyHelper`. The installer preserves unrelated Pi providers, and the harness merger preserves unrelated Claude settings. The clone path is only the installation source; runtime state is stored under `$HOME/.local/share/ica-litellm-key-router`.

## Verify a release before installation

For `v0.2.2-rc.1`, the runtime source asset is:

> `v0.2.2-rc.1` is a prerelease. Its initial assets may be published manually during a GitHub Actions outage; verify `SHA256SUMS`, the exact-tag manifest, and the annotated tag. GitHub provenance attestations become available after the release workflow recovers and reconciles the deterministic assets.

```text
ica-litellm-key-router-v0.2.2-rc.1.zip
ica-litellm-key-router-v0.2.2-rc.1.zip.sha256
```

The ZIP sidecar is exactly one line with a lowercase SHA-256 digest, two spaces, the exact ZIP filename, and a final newline. A release also contains standalone `install-linux.sh`, `install-windows.ps1`, `release-manifest.json`, and `SHA256SUMS`; the latter covers the ZIP, ZIP sidecar, both installers, and manifest.

Linux example after downloading the ZIP and its sidecar:

```bash
sha256sum --check --strict ica-litellm-key-router-v0.2.2-rc.1.zip.sha256
```

After downloading all release files named by `SHA256SUMS`, verify the complete set, including both standalone installers:

```bash
sha256sum --check --strict SHA256SUMS
```

Windows example for the exact ZIP sidecar:

```powershell
$asset = 'ica-litellm-key-router-v0.2.2-rc.1.zip'
$line = [IO.File]::ReadAllText("$asset.sha256", [Text.Encoding]::ASCII)
if ($line -notmatch '\A([0-9a-f]{64})  ([^\r\n]+)\r?\n\z' -or $Matches[2] -ne $asset) {
  throw 'Invalid checksum sidecar'
}
$actual = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $Matches[1]) { throw 'Checksum mismatch' }
```

For a standalone installer, read its exact entry from `SHA256SUMS` and compare without executing it:

```powershell
$name = 'install-windows.ps1'
$pattern = '^([0-9a-f]{64})  ' + [regex]::Escape($name) + '$'
$entry = @(Get-Content -LiteralPath .\SHA256SUMS | Where-Object { $_ -match $pattern })
if ($entry.Count -ne 1 -or $entry[0] -notmatch $pattern) { throw 'Missing or duplicate checksum entry' }
$expected = $Matches[1]
$actual = (Get-FileHash -LiteralPath $name -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'Checksum mismatch' }
```

A same-channel checksum detects corruption or inconsistent bytes; it does **not** independently authenticate the publisher. Before execution, also verify the exact tag/commit and a signature or artifact attestation from a trusted project identity. For a published GitHub artifact attestation, for example:

```bash
gh attestation verify ica-litellm-key-router-v0.2.2-rc.1.zip \
  --repo sehoon123/ica-litellm-key-router
```

The remote-source installer mode downloads the exact tag ZIP and its exact sidecar over HTTPS, verifies the SHA-256, validates `VERSION`, and uses a bounded safe extractor that rejects traversal, links/special files, duplicate or case-colliding names, excessive members, and excessive expanded size. It also verifies the pinned Astral installer script before execution. These checks do not replace signature/provenance verification. Local-source mode trusts the directory you supply; verify it first.

Maintainers can build the deterministic ZIP and sidecar with:

```bash
python scripts/build-release.py --output-dir dist
```

Publishing is a release gate: run CI, reproduce the ZIP, verify the sidecar, test safe extraction and fresh/update installs on Linux and Windows, then sign or attest the exact digest. `release-manifest.json` always records the version, tag, exact source commit, and primary asset digests; building requires a Git worktree.

## Install on Linux

From an extracted, verified release:

```bash
cd /path/to/ica-litellm-key-router-v0.2.2-rc.1
bash ./install-linux.sh
```

Or run a separately verified standalone installer. With no complete source tree beside it, it downloads the exact asset for `ICA_ROUTER_REF` (default `v0.2.2-rc.1`):

```bash
ICA_ROUTER_REF=v0.2.2-rc.1 bash ./install-linux.sh
```

On the first run, the installer asks for one key at a time for each catalog pool. Input is hidden. Press Enter on an empty key prompt after the last key in that pool. At least one key is required in each pool. It then generates all deployments and starts LiteLLM.

On later runs, if the saved secrets, generated state, and selected release pass `doctor`, the installer skips downloading and reinstalling dependencies. It only ensures that LiteLLM is running. Use `--force-install` when an actual reinstall or update is intended, and `--replace-keys` to replace all saved pool keys. `--replace-keys` preserves the local proxy master key, so existing client files remain valid.
When upgrading legacy state, bootstrap preserves `ica-services-essentials`, removes the deprecated `ibm-ica-nextgen` pool from active secrets, and writes a private timestamped backup first. An explicit client merge removes the three deprecated NextGen router provider IDs. The protected secrets backup can still contain retired NextGen values; delete that backup after verification if it is no longer required.

Client and service configuration are opt-in and may be done during installation or later:

```bash
# Do not touch models.json and use the background lifecycle (default).
bash ./install-linux.sh

# Enable supervised autostart at user login.
bash ./install-linux.sh --systemd-user

# Create or merge Pi's command-backed router providers.
bash ./install-linux.sh --pi-models

# Create or merge both Pi-format client files.
bash ./install-linux.sh --pi-models --prime-models

# Create or merge an explicit Pi-format path; repeat the option if needed.
bash ./install-linux.sh --models-json /private/path/models.json

# Configure all supported harnesses after installation.
ica-router configure-harnesses --all

# Or select individual harnesses. Prime-agent remains explicit.
ica-router configure-harnesses --pi --claude-code --codex
ica-router configure-harnesses --prime
```

`configure-harnesses` creates or merges:

- Pi router providers in `~/.pi/agent/models.json`;
- optional prime-agent providers in `~/.prime/agent/models.json`;
- Claude Code gateway settings and `apiKeyHelper` in `~/.claude/settings.json`; and
- a dedicated `~/.codex/ica-router.config.toml` profile for Codex 0.134.0 or later, leaving subscription defaults untouched.

All generated authentication settings call `ica-router client-token` instead of storing the local master key. The Codex profile disables its request and stream retries because the router already owns pre-output retries.

Apache Maka currently has neither a command-backed model credential hook nor a stable non-interactive model-connection command, so `configure-harnesses` deliberately does not edit its evolving workspace catalog or plaintext credential vault. Configure it once under **Settings → Models** with provider type `openai-responses-compatible`, base URL `http://127.0.0.1:4000/v1`, model `ica-se-openai--gpt-5.6-sol`, and the output of `ica-router client-token` as its API key. Maka persists that local router key, but upstream IBM key rotation remains centralized in this router. If “Maka” refers to a different project, verify its custom Responses endpoint contract separately.

The installer:

1. takes an install-wide lock;
2. resolves previously verified local source or a verified exact-tag ZIP;
3. installs the private pinned toolchain and frozen environment into a new versioned release;
4. snapshots private generated state and any explicitly requested client files;
5. safely stops an existing managed router;
6. atomically switches `current` to the new release;
7. preserves valid existing secrets, or on first install reads keys until an empty value ends each pool;
8. writes generated state and, only when requested, creates or merges client files;
9. runs `doctor`, starts one worker directly or through the requested/preserved systemd user service, and creates a best-effort `~/.local/bin/ica-router` symlink.

Default layout:

| Item | Linux path |
|---|---|
| Install root | `${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router` |
| Versioned releases | `<install-root>/releases/<release-id>` |
| Selected release | `<install-root>/current` symlink |
| Selected app/environment | `<install-root>/current/app`, `<install-root>/current/.venv` |
| Persistent private state | `<install-root>/state` |
| Private `uv` and cache | `<install-root>/tools/uv-0.12.2`, `<install-root>/cache/uv` |
| Direct wrapper | `<install-root>/ica-router` |
| Convenience symlink | `~/.local/bin/ica-router` |
| Optional managed user unit | `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ica-litellm-key-router.service` |

If `~/.local/bin` is not on `PATH`, invoke the direct wrapper or add the directory to `PATH`.

Useful overrides:

```bash
ICA_ROUTER_HOME='/private/path/ICA router' \
ICA_ROUTER_SOURCE_DIR='/verified/source/path' \
ICA_ROUTER_KEY_ROTATOR='/private/path/key-rotator.json' \
ICA_ROUTER_NON_INTERACTIVE=1 \
bash ./install-linux.sh
```

`ICA_ROUTER_REF` must be an exact `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH-rc.N` tag. Set `ICA_ROUTER_NON_INTERACTIVE=1` only when a valid existing `secrets.json` or importable key-rotator document is available. Otherwise installation fails closed instead of prompting. Without that setting, secret entry requires a real terminal.

A leftover `<install-root>/.install.lock` directory after a crash blocks Linux installation. Confirm that no installer is running before removing it.

## Install on Windows

From an extracted, verified release:

```powershell
Set-Location 'C:\path\to\ica-litellm-key-router-v0.2.2-rc.1'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
```

Default layout:

| Item | Windows path |
|---|---|
| Install root | `%LOCALAPPDATA%\IcaLiteLLMKeyRouter` |
| Versioned releases | `<install-root>\releases\<release-id>` |
| Selected release ID | text file `<install-root>\current` |
| Selected app/environment | `<install-root>\releases\<current>\app`, `<install-root>\releases\<current>\.venv` |
| Persistent private state | `<install-root>\state` |
| Private `uv` and cache | `<install-root>\tools\uv-0.12.2`, `<install-root>\cache\uv` |
| Wrapper | `<install-root>\ica-router.ps1` |

The Windows flow performs the same first-run prompt-until-empty, generated configuration, start, and later start-only fast path. It does not add the wrapper to `PATH`. It fails closed if it cannot remove inherited access and set/verify protected current-user-only allow ACLs on installer/control-plane private paths. Runtime cache/temp descendants inherit current-user-only access from protected parents. Client files and their backups are also restricted, but the router does not rewrite the ACL of an arbitrary client parent directory.

Client configuration is also opt-in:

```powershell
# Default: do not touch models.json.
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1

# Create or merge Pi's file.
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 -PiModels

# Other choices: -PrimeModels or -ModelsJson 'D:\Private\models.json'.
```

After installation, the Windows wrapper can generate the same command-backed harness configuration:

```powershell
$router = Join-Path $env:LOCALAPPDATA 'IcaLiteLLMKeyRouter\ica-router.ps1'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router configure-harnesses --all
```

Use `-ReplaceKeys` to enter all Services Essentials keys again and `-ForceInstall` for an actual reinstall/update.

Overrides:

```powershell
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 `
  -InstallRoot 'D:\Private\ICA Router' `
  -SourceDirectory 'D:\Verified\ica-litellm-key-router-v0.2.2-rc.1' `
  -KeyRotatorPath 'D:\Private\key-rotator.json' `
  -NonInteractive
```

Environment equivalents are `ICA_ROUTER_HOME`, `ICA_ROUTER_SOURCE_DIR`, `ICA_ROUTER_KEY_ROTATOR`, `ICA_ROUTER_NON_INTERACTIVE`, and exact-tag `ICA_ROUTER_REF`. Redirected input is treated as non-interactive. The persistent `install.lock` file is held with exclusive sharing while an installer is active; the file may remain afterward and should not be deleted merely because it exists.

## Secrets schema

`examples/secrets.example.json` and the block below contain placeholders only. They are intentionally unusable:

```json
{
  "schemaVersion": 1,
  "masterKey": "REPLACE_ME_WITH_A_RANDOM_SK_MASTER_KEY",
  "pools": {
    "ica-services-essentials": {
      "keys": [
        { "id": "key-1", "value": "REPLACE_ME_SERVICES_KEY_1" },
        { "id": "key-2", "value": "REPLACE_ME_SERVICES_KEY_2" }
      ]
    }
  }
}
```

Do not copy placeholders into private state. Interactive bootstrap creates a random local master key. Validation requires:

- `schemaVersion` exactly `1`;
- a `masterKey` beginning with `sk-`, 24–1024 UTF-8 bytes, with no NUL/newline or placeholder marker;
- exactly the `ica-services-essentials` pool ID;
- 1–256 keys in the pool;
- a unique key `id` in each pool matching a conservative identifier format;
- each provider value non-empty, at most 4096 UTF-8 bytes, with no NUL/newline or placeholder marker;
- no duplicate provider value within one pool;
- at most 24 KiB for all environment names/values, and at most 8 MiB for a parsed JSON document.

Auto-import checks `~/.pi/agent/key-rotator.json` and `~/.prime/agent/key-rotator.json`. An import entry must use exactly one of `value`, `env`, or `command`. Literal and available environment-backed entries are copied into `secrets.json`; `command` entries are refused and never executed automatically. Example import shape:

```json
{
  "pools": [
    {
      "poolId": "ica-services-essentials",
      "keys": [
        { "id": "key-1", "value": "REPLACE_ME_SERVICES_KEY_1" },
        { "id": "key-2", "value": "REPLACE_ME_SERVICES_KEY_2" }
      ]
    }
  ]
}
```

On Unix, installer-owned private directories use mode `0700` and private files use `0600`. Windows uses protected current-user-only ACLs and fails if restriction or verification fails. Raw upstream keys are stored in plaintext `state/secrets.json` and loaded into the LiteLLM process environment. `config.yaml` contains environment references rather than raw keys.

The local master key is not an IBM key. It grants authenticated access to every local router route and remains canonical only in `state/secrets.json` and the LiteLLM process environment. Generated Pi/prime-agent providers, Claude Code `apiKeyHelper`, and the Codex router profile execute `ica-router client-token`; they do not persist another key copy. Replacing or explicitly rotating the local key therefore does not require rewriting command-backed clients, although already running clients can cache the helper result briefly and may need to retry or restart. A timestamped backup contains the entire previous client file and can include unrelated provider secrets from before the merge. Treat state, client files, process environment, logs, memory, helper output, and backups as sensitive. Do not merge while another process writes the same client file.

## Native API endpoints

Set this placeholder to your private local `masterKey`; do not use an upstream ICA credential:

```bash
export ICA_ROUTER_MASTER_KEY='REPLACE_ME_WITH_LOCAL_MASTER_KEY'
```

### OpenAI Responses

```bash
curl --fail-with-body http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer ${ICA_ROUTER_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ica-se-openai--gpt-5.6-luna-dzus",
    "input": "Reply with OK.",
    "stream": false
  }'
```

Base URL: `http://127.0.0.1:4000/v1`<br>
Endpoint: `POST /v1/responses`

### Anthropic Messages

```bash
curl --fail-with-body http://127.0.0.1:4000/v1/messages \
  -H "x-api-key: ${ICA_ROUTER_MASTER_KEY}" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ica-se-claude--claude-sonnet-4-6",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Reply with OK."}]
  }'
```

Base URL: `http://127.0.0.1:4000`<br>
Endpoint: `POST /v1/messages`

### Gemini `generateContent`

```bash
curl --fail-with-body \
  'http://127.0.0.1:4000/v1beta/models/ica-se-gemini--gemini-3.7-flash:generateContent' \
  -H "Authorization: Bearer ${ICA_ROUTER_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}]
  }'
```

Base URL: `http://127.0.0.1:4000/v1beta`<br>
Endpoints:

- `POST /v1beta/models/{model}:generateContent`
- `POST /v1beta/models/{model}:streamGenerateContent?alt=sse`

Use `Authorization: Bearer ...` for Gemini. Do **not** point a client at LiteLLM's `/gemini` provider pass-through route. That route bypasses this project's generated `model_list` and its credential routing.

## Operate the router

Linux:

```bash
ica-router status
ica-router doctor
ica-router stop
ica-router start
ica-router install-systemd-user
ica-router uninstall-systemd-user
```

The managed systemd unit runs LiteLLM in the foreground, waits for authenticated readiness, restarts it after failures, and is enabled for user login. Starting at boot before login additionally requires the administrator/user to enable systemd lingering; the router does not change lingering automatically. The Linux installer preserves an already enabled managed unit across updates, or enables it explicitly with `--systemd-user`.

Windows:

```powershell
$router = Join-Path $env:LOCALAPPDATA 'IcaLiteLLMKeyRouter\ica-router.ps1'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router status
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router doctor
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router stop
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router start
```

Lifecycle commands use OS locking on private `state/command.lock`. `run.json` records the PID, process creation token, executable, config path and digest, host, and port. `stop` signals only a process whose creation token and command line match; it refuses a mismatched live PID.

`start` validates state, refuses a foreign listener, starts one worker, records identity, and waits for process identity, `/health/liveliness`, and an authenticated `/v1/models` response containing a configured alias. It does not contact IBM for that readiness check.

`status` exit codes are:

| Code | Meaning |
|---|---|
| `0` | recorded identity matches and local liveness succeeds |
| `1` | stopped; dead stale state is removed |
| `2` | matched process is not live, generated config changed and restart is required, or another validation error occurred |
| `3` | recorded PID is alive but its identity does not match |

`doctor` is offline with respect to IBM. It checks catalog/state schema, the generation marker, deployment count, absence of raw credentials in `config.yaml`, and Unix private modes. It cannot prove that IBM accepts a key or that the account has quota.

Private state-changing commands require the managed router to be stopped and also take `command.lock`:

```bash
ica-router generate
ica-router configure-clients
ica-router configure-clients --client /private/path/models.json
ica-router bootstrap --port 4100 --client auto
```

`bootstrap` creates or preserves secrets and rewrites a complete generated state. `generate` accepts only a currently consistent generation. `configure-clients` merges the three active router-owned Pi-format providers. Auto mode changes only existing `~/.pi/agent/models.json` and `~/.prime/agent/models.json` files.

Harness configuration can run while the router is healthy because it reads a generation-bound snapshot and changes only client files:

```bash
ica-router configure-harnesses --all
ica-router configure-harnesses --pi --claude-code --codex
ica-router configure-harnesses --prime
```

`client-token` is intended for generated command-backed authentication. It prints only the current local credential (or `Bearer <credential>` with `--bearer`) and must be treated as a secret-producing command.

Persistent state includes `secrets.json`, `config.yaml` (JSON valid as YAML), `client-models.generated.json`, `runtime.json`, and `generation.json`. The generation marker binds normalized digests of catalog, secrets, generated config, generated clients, and runtime. It is written last so an interrupted multi-file generation fails closed. Lifecycle files are `command.lock`, transient `run.json`, and `router.log`; logs above 10 MiB rotate to `router.log.1`. Private `process-home`, `process-cache`, and `process-tmp` directories isolate LiteLLM from the caller's home but can retain dependency cache or temporary data, so include them in state protection and retention handling.

## Troubleshooting

### `ica-router: command not found`

Use `<install-root>/ica-router`, or on Linux add the convenience directory:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Startup or readiness fails

Check status, port ownership, state consistency, and the private error log:

```bash
ica-router status
ica-router doctor
sed -n '1,200p' "${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router/state/router.log"
```

The runtime intentionally uses a minimal environment. Ambient proxy, custom CA, user-site, and environment-based provider settings are not inherited. A network that needs those features is unsupported without a reviewed code/config change.

To change the local port while preserving secrets:

```bash
ica-router stop
ica-router bootstrap --port 4100 --client auto
ica-router doctor
ica-router start
```

Restart Pi/prime-agent after client URLs change.

### Stale identity or lifecycle lock

Status `3` means the PID is alive but does not match `run.json`. Do not kill it based on the PID alone. Inspect the protected record and operating-system command line. Remove `run.json` only after confirming that the process is unrelated and no router owns the configured port.

“another router lifecycle command is in progress” means another command owns the OS lock on `command.lock`. The file normally remains after release; do not delete it to bypass a live lock.

### Incomplete or inconsistent generation

Stop the router and run `bootstrap` again. `generation.json` is deliberately fail-closed; editing one generated file or running `generate` against an already inconsistent set will not repair it.

### Permissions or ACL error

On Linux, inspect state before changing it:

```bash
find "${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router/state" -maxdepth 1 -printf '%m %p\n'
```

Private directories should be `0700` and files `0600`. On Windows, do not bypass an ACL failure. Use a local NTFS path owned by the intended user and review it with `Get-Acl` or `icacls`.

### Placeholder, pool, or duplicate-key error

The example is not valid secret material. The exact Services Essentials pool ID and at least one authorized key are required. Stop the router, correct/import the private source, and run `bootstrap`; use `--replace-secrets` only when you intend to replace and back up the existing secret document.

### Router provider is missing in Pi/prime-agent

Auto mode changes existing client files only. Supply an exact path after stopping the router:

```bash
ica-router configure-clients --client /private/path/models.json
```

The merge retains unrelated provider IDs, replaces the three active router-owned IDs and removes the three deprecated NextGen router IDs, and creates a whole-file timestamped backup only when content changes. Restart the client afterward.

### Local authentication fails

Run `ica-router client-token` only to verify that the command succeeds; do not paste its output into config. Rerun `configure-harnesses` (or the legacy Pi-only `configure-clients`) to repair helper paths. A running client may cache a previous helper result briefly after explicit master-key rotation, so retry or restart that client. Never substitute an IBM key for the local master key.

### Upstream authentication, timeout, or rate-limit failure

`doctor` does not call IBM. Check pool selection, authorization, quota, direct HTTPS reachability, cooldown, and the private log. A failed deployment can remain cooled down for 60 seconds by default. Logs are sensitive even at forced `ERROR` level.

## Update and rollback

1. Read release and schema notes.
2. Keep an independent, access-controlled backup of state and client files.
3. Verify the new exact release, checksum, and publisher provenance.
4. Run the new platform installer.
5. Confirm `doctor` and `status`, then restart Pi/prime-agent.

An installer serializes updates, stages a versioned release, snapshots generated state and explicitly requested client files, stops the old managed process, atomically switches `current`, bootstraps, checks, and starts. Valid existing port, `maxFallbacks`, and cooldown runtime settings are preserved unless an explicit bootstrap flag changes them. On a handled failure after stop/switch, the installer attempts to restore the previous pointer, state, and requested client files and restart the old release. Old versioned releases are retained.

Rollback is **best effort**, not a crash-proof transaction. Power loss, forced termination, storage failure, ACL failure, or custom client paths can leave partial work or require manual recovery. Unrelated pre-existing client secrets can exist in rollback snapshots and timestamped backups; command-backed router entries themselves contain only helper commands. Keep an independent protected backup until verification, and remove obsolete releases/backups deliberately.

## Uninstall

There is no automated uninstaller.

1. Stop the router.
2. Remove the three active `*-router` entries listed above and any deprecated `ibm-ica*-router` entries from every configured client file.
3. Review and remove timestamped client backups if retention is not required.
4. Remove the wrapper/symlink and the confirmed install root.
5. Rotate IBM credentials if the host, process environment, state, logs, or backups may have been exposed.

Linux defaults:

```bash
ica-router uninstall-systemd-user || true
ica-router stop || true
rm -f "$HOME/.local/bin/ica-router"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router"
```

Also remove the managed Claude Code settings and the dedicated Codex profile if they are no longer wanted; use their timestamped backups to restore previous content where applicable.

Windows defaults:

```powershell
$root = Join-Path $env:LOCALAPPDATA 'IcaLiteLLMKeyRouter'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ica-router.ps1') stop
Remove-Item -LiteralPath $root -Recurse -Force
```

Confirm the path, especially after `ICA_ROUTER_HOME` or `-InstallRoot`. Deletion is not guaranteed secure erasure on SSDs, snapshots, synchronized folders, or backups.

## Security and limitations

Read [SECURITY.md](SECURITY.md). Main limits:

- local, single-user use only; no TLS or remote-service hardening;
- one LiteLLM worker, not a distributed key service;
- random `simple-shuffle`, not round-robin or equal-share scheduling;
- weighted failover disabled and at most the configured pre-output retries;
- ambiguous retries can duplicate or bill work;
- no mid-stream failover;
- no IBM credential/quota validation in `doctor`;
- plaintext secrets protected by filesystem permissions/ACLs, not a vault;
- command-backed helpers reduce persistent copies but deliver the local master key to each client process at runtime;
- same-user, administrator, debugger, or compromised-process access remains in scope;
- a minimal runtime environment with direct HTTPS only; ambient proxies, custom CAs, and environment-based provider features are not inherited;
- checksum verification is not independent publisher authentication;
- best-effort update rollback is not power-loss atomic; and
- model availability, behavior, quotas, and upstream retention remain controlled by IBM and model providers.

## Development

Checks do not need real provider keys:

```bash
uv lock --check
python -m unittest discover -s tests -v
python -m py_compile tools/routerctl.py tests/test_routerctl.py scripts/build-release.py
bash -n install-linux.sh
out="$(mktemp -d)"
python scripts/build-release.py --development --output-dir "$out"
```

`--development` permits an untagged or dirty tree only for local testing. Official mode omits it, requires a completely clean worktree at the exact version tag, and requires an empty output directory.

CI pins every action to a full commit SHA. It runs tests and syntax checks on Ubuntu and Windows, scans all release text for likely secrets and release debris, checks the frozen lock, reproduces the deterministic ZIP, and verifies every `SHA256SUMS` entry. Linux and Windows installer jobs use only explicitly named dummy values. They perform two installs in shell-significant paths and test `doctor`, start/status/stop, update pointer changes, private permissions/ACLs, lock behavior, and stale live-PID refusal. After all gates pass on an exact `v*` tag push, [`.github/workflows/release.yml`](.github/workflows/release.yml) uses a least-privilege OIDC job to build official assets from the clean tag, create GitHub build-provenance attestations for every asset, and create the release. No CI job reads or requires a provider credential from repository secrets.

## License

[MIT](LICENSE)
