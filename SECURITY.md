# Security Policy

## Supported releases

Security fixes are provided for the latest tagged release only. Older releases may not receive backports. [`VERSION`](VERSION) is the project version. The shipped runtime is exact Python `3.12.13`, `uv` `0.12.2`, and LiteLLM `1.98.0`; Python/LiteLLM dependencies are resolved by [`uv.lock`](uv.lock).

Read release notes before updating. Catalog, state schema, model behavior, and dependency changes can affect security even when the local API stays compatible.

## Report a vulnerability

Do **not** open a public issue for a suspected vulnerability or credential exposure.

Use the repository's [private GitHub Security Advisory form](https://github.com/sehoon123/ica-litellm-key-router/security/advisories/new). Include:

- affected release, operating system, and installation method;
- the smallest reproducible sequence;
- expected and observed behavior;
- security impact and required attacker access;
- relevant logs with every key, token, prompt, user value, host-specific path, and identifier redacted; and
- whether the issue is public or actively exploited.

Use placeholders such as `REPLACE_ME_WITH_REDACTED_VALUE`. Never send a working IBM ICA credential, local master key, complete private state, complete client `models.json`, process environment, or unredacted request body.

Maintainers aim to acknowledge a complete report within five business days and provide a triage update within ten business days. These are targets, not a service-level guarantee. Coordinate disclosure until a fix or mitigation is available.

Report a defect wholly within LiteLLM, Python, `uv`, IBM ICA, or a model provider through that project's security channel too. Report it here when this integration or its defaults materially change the impact.

## Security objective and scope

The router reduces the number of local clients that receive upstream IBM ICA keys. Raw upstream values remain in one private state document and the LiteLLM process environment. Clients receive a separate local master key.

The supported deployment is one trusted operating-system user on one workstation, with one LiteLLM worker on loopback. This project is **not**:

- a hardware-backed secret manager;
- a hostile multi-tenant gateway;
- a remote access service;
- a distributed load balancer or high-availability service;
- a sandbox around all LiteLLM functionality; or
- an exactly-once request processor.

## Data flow and trust boundaries

```text
trusted local client
  -> loopback HTTP + local master key
  -> one local LiteLLM worker
  -> direct HTTPS + one pool credential
  -> catalog-selected IBM ICA gateway
```

“Local-only” applies only to the client listener. Prompts, uploaded content, responses, tool data, metadata, and the selected upstream credential leave the machine over HTTPS. IBM and model providers control upstream retention, abuse monitoring, regional processing, model behavior, account policy, and quota.

A normal installation trusts:

1. the operating-system account, kernel, local storage, and ACL/mode enforcement;
2. the exact standalone installer that the user verifies before execution;
3. the release source, especially `catalog.json`, `tools/routerctl.py`, installers, wrapper, and lock file;
4. Astral's `uv` installer and managed-Python distribution channel;
5. the frozen Python package artifacts and LiteLLM;
6. every local client that receives the master key; and
7. IBM ICA and selected upstream model services.

At runtime the launcher constructs a minimal LiteLLM environment instead of copying the caller's environment. It supplies fixed system/venv paths, private home/cache/temp locations under state, provider keys, production/error logging, telemetry-disable controls, and `NO_PROXY=*`. Ambient proxy, custom-CA, user-site, and environment-based provider settings are intentionally not inherited. Direct IBM HTTPS with the normal system trust configuration is therefore required.

## Assets to protect

Protect:

- every IBM ICA upstream credential;
- the local router master key;
- `<install-root>/state/secrets.json` and any backup;
- `client-models.generated.json`;
- modified Pi/prime-agent `models.json` files and their whole-file timestamped backups;
- temporary installer rollback snapshots;
- state-owned `process-home`, `process-cache`, and `process-tmp`, which can retain dependency cache or temporary data;
- process environments, memory, debug sessions, crash dumps, swap, and hibernation data;
- prompts, responses, tool data, and uploaded content;
- `router.log` and `router.log.1`;
- lifecycle integrity files such as `generation.json`, `run.json`, and `command.lock`;
- the `current` pointer, wrappers, releases, and install locks; and
- `catalog.json`, because it controls where credentials and requests are sent.

## Threat model

### Protections provided in part

- **Reduced key distribution.** Local clients receive one local master key instead of all upstream keys.
- **Loopback-only listener.** Host validation rejects non-`127.0.0.1` addresses.
- **Local authentication.** LiteLLM requires the local master key for authenticated proxy routes.
- **Raw-key exclusion from generated config.** `config.yaml` uses environment references, and `doctor` checks known raw values are absent.
- **Fail-closed generated state.** `generation.json` binds normalized digests of catalog, secrets, generated config, generated clients, and runtime; an interrupted or partial rewrite is rejected.
- **Private local files.** Unix control-plane private directories/files use `0700`/`0600`. Windows control-plane restriction removes inheritance, sets the current user as owner, allows only that SID, verifies the result, and fails closed; runtime-created cache/temp descendants inherit current-user-only access from protected parents.
- **Safer imports.** Key-rotator `command` sources are refused instead of executed. Literal and available environment-backed values are copied into private state.
- **Safer client merge.** Unrelated provider IDs are retained, changed files are backed up, and writes use replacement rather than in-place truncation.
- **Process identity fencing.** `run.json` records creation identity, executable, config path/digest, host, and port. `stop` refuses to signal a mismatched live PID.
- **Command serialization.** OS locks on `command.lock` serialize lifecycle and state-changing commands. Installer-wide locks serialize installation/update attempts.
- **Startup readiness.** `start` verifies process identity, local liveness, and an authenticated model list containing a configured alias without calling IBM.
- **Bounded release extraction.** Remote installers reject ZIP traversal, links/special files, collisions, unexpected top-level names, excessive members, and excessive expanded data.
- **Dependency/source checks.** Remote source ZIP SHA-256, pinned `uv` installer SHA-256, exact Python/LiteLLM versions, frozen sync, and `uv pip check` are enforced.
- **Failed-credential availability.** Selected error classes make a deployment immediately eligible for cooldown and a same-alias router retry.

### Threats not addressed

- malware, a debugger, injected code, or another process running as the same user;
- an administrator/root user, kernel or hypervisor compromise, endpoint agent, or backup operator;
- an administrator taking ownership of a Windows current-user-only ACL;
- snapshots, swap, crash reports, synchronized folders, insecure backups, or secure deletion;
- hostile multi-user access to the same loopback stack or stolen local master key;
- denial of service, local port squatting, resource exhaustion, or malicious request volume;
- a malicious or compromised LiteLLM, Python, `uv`, PyPI artifact, release publisher, or signing identity;
- a compromised download channel when checksum and artifact are obtained from that same channel without independent provenance;
- a malicious local-source directory supplied through an override;
- a modified catalog that still passes structural and HTTPS validation;
- concurrent modification of a client `models.json` during read/modify/write;
- replacement or deletion of a protected client file by an attacker who can write its parent directory;
- TLS interception by a trusted host/corporate certificate authority;
- compromise, retention, or misuse at IBM ICA or an upstream model service;
- perfect fairness, round-robin ordering, key secrecy through scheduling, or key revocation;
- exactly-once delivery, prevention of duplicate billing, or mid-stream recovery; and
- power-loss-atomic installation or guaranteed rollback.

## Local listener and authentication

The router enforces one worker on `127.0.0.1`; `4000` is the default port. Loopback traffic uses plain HTTP. Do not bypass host validation, forward the listener, expose it through a container/network bridge shared with untrusted workloads, or treat loopback as an identity boundary between mutually untrusted applications.

The local master key grants authenticated access to the LiteLLM proxy surface, not just one model. It cannot protect against a process that can read the same user's files, environment, traffic, or memory. The liveness endpoint used by lifecycle checks may be visible locally without the master key; startup's `/v1/models` readiness probe is authenticated.

The project does not install TLS certificates, configure a firewall, validate reverse-proxy forwarding headers, provide per-client roles, or rate-limit a hostile local caller. Remote access requires a separately reviewed security boundary and is outside the supported model.

## Secret storage and handling

### Upstream credentials

Canonical upstream values are plaintext in `<install-root>/state/secrets.json`. Each pool requires 2–256 keys. Individual values are limited to 4096 UTF-8 bytes and the combined secret environment to 24 KiB. Imported values are copied; they do not remain dynamically linked to an environment variable or external file.

At start, keys are copied into the LiteLLM process environment. `config.yaml` contains variable references, not raw values. Same-user diagnostics on some systems, administrators, crash collectors, the process itself, and injected code can still observe the environment.

Do not hand a secret document to an untrusted parser or editor. Stop the managed router before state changes. Prefer `bootstrap --replace-secrets --import-key-rotator ...` for rotation so validation, atomic private writes, generation binding, client merge, and backup behavior are applied.

### Local master key and client files

The master key is different from every IBM key. It is stored in `secrets.json` and intentionally copied to:

- `<state-dir>/client-models.generated.json`; and
- each configured Pi/prime-agent `models.json`.

A timestamped backup is a copy of the entire previous client file. It can contain an older local master key and unrelated provider credentials. The update rollback area can temporarily hold the same data. Protect and expire these copies deliberately.

The router restricts the client file and backup but does not change an arbitrary parent directory's Windows ACL. A user who can replace names in that directory can bypass a file-only ACL. Use an access-controlled local directory and coordinate other writers.

### Logs and request data

The launcher supplies `LITELLM_LOG` and `LITELLM_LOG_LEVEL` as `ERROR`, sets production mode, and disables telemetry through environment and command-line controls. It gives LiteLLM private state-owned home/cache/temp directories. These controls reduce exposure; they do not prove that exceptions, dependency diagnostics, cached artifacts, temporary data, operating-system captures, or upstream error bodies contain no sensitive data.

`router.log` is private and rotates to one `router.log.1` after it exceeds 10 MiB and the router starts again. Redact before sharing. The project configures no hosted relay or request database, but it cannot guarantee that LiteLLM dependencies, local tooling, the OS, IBM, or model providers retain nothing.

## Catalog and generated-state integrity

`catalog.json` defines API types, credential-pool mappings, model IDs, and upstream base URLs. Validation requires HTTPS but does not use a compiled hostname allowlist. A modified catalog can redirect credentials and content to an attacker-controlled HTTPS host.

Protect and verify the complete install root, not only `state`. `doctor` validates structure, generation digests, deployment count, known-secret absence from generated config, and Unix modes. It does **not** prove source provenance, compare the catalog to an official digest, or call IBM.

Do not hand-edit generated files. State-changing commands acquire `command.lock` and refuse to run while the managed router or a foreign process owns the configured port. `generation.json` is written last. If one generated file changes or generation is interrupted, loading fails closed. Use stopped `bootstrap` to create a new complete generation; `generate` itself requires the current generation to be consistent.

Review every explicit `--catalog`, `--state-dir`, `--venv`, `--client`, `ICA_ROUTER_HOME`, `ICA_ROUTER_SOURCE_DIR`, `ICA_ROUTER_KEY_ROTATOR`, and `ICA_ROUTER_REF` value. Exact-tag validation does not authenticate a local source directory.

## Routing and failure security

Each provider-qualified alias expands to equally weighted deployments for the keys in its own pool. `simple-shuffle` is random, **not round-robin**, and does not guarantee equal use or a stable order. Weighted failover and cross-provider/model fallback maps are disabled.

Defaults are:

- router `num_retries: 2` from `--max-fallbacks` / `maxFallbacks`;
- provider SDK/deployment `max_retries: 0`;
- zero configured retries for bad-request and content-policy errors;
- up to two router retries for authentication, timeout, and rate-limit errors;
- the same global count for intended 5xx behavior and any other status/exception stock LiteLLM `1.98.0` classifies as retryable;
- zero allowed failures for configured authentication/rate/timeout/server/unavailable/gateway classes, making the deployment immediately cooldown-eligible; and
- a 60-second cooldown.

A request can therefore make the initial attempt plus at most two eligible router retries by default. It does not exhaust a large pool. Explicit policy entries block bad-request and content-policy retries, but stock LiteLLM `1.98.0` has no `InternalServerErrorRetries` key. The global count is necessarily broader to cover intended 5xx behavior and can follow other standard retryable-status decisions; this is not a strict four-class allowlist. Availability controls do not validate key ownership, revoke a leaked key, or guarantee an upstream did not accept a request before returning an error.

A pre-output failure can be ambiguous: an upstream may execute or bill work before the router observes failure and selects another same-alias deployment. There is **no mid-stream failover**. Retrying a stream can repeat billable or state-changing work. Applications must design for idempotency and duplicate handling where the native API supports them.

One worker is a security and consistency boundary. Do not raise the worker count or run multiple controllers against one state directory and assume process identity, cooldown, encrypted-content affinity, or selection state is coordinated.

## Installer, update, and supply-chain security

### What the installers verify

Remote-source mode:

1. requires an exact `vMAJOR.MINOR.PATCH` `ICA_ROUTER_REF`;
2. downloads `ica-litellm-key-router-<tag>.zip` and its exact `.zip.sha256` sidecar over HTTPS;
3. validates the strict sidecar filename/digest format and SHA-256;
4. safely extracts one expected top-level directory and validates `VERSION`;
5. downloads the platform-specific Astral `uv` `0.12.2` installer and compares an embedded platform digest before execution;
6. uses a private `uv` with ambient `UV_*`, `PIP_*`, and `PYTHON*` configuration removed;
7. installs exact Python `3.12.13` through `uv`;
8. runs frozen, no-dev sync from `uv.lock`, `uv pip check`, and exact LiteLLM `1.98.0` verification.

The project pins the Python version, not a project-owned digest for every platform-specific managed-Python artifact. Trust in that download is delegated to `uv` and its distribution/TLS chain. Likewise, a valid package lock does not make a compromised upstream publisher harmless.

Local-source mode validates completeness and `VERSION`, but it does not authenticate or hash the supplied directory. Verify it before use.

### Release authenticity

The deterministic release builder selects tracked release files, requires Git, rejects links/oversized input, writes a fixed-metadata ZIP, an exact ZIP sidecar, standalone installers, mandatory commit/digest `release-manifest.json`, and `SHA256SUMS`. Official mode requires a completely clean tree at the exact version tag and an empty output directory. CI reproduces and compares development-mode output and SHA-256 checks every listed asset. Only after the reusable CI's Ubuntu, Linux-installer, and Windows-installer gates pass on an exact `v*` tag push, `.github/workflows/release.yml` grants its publishing job scoped `contents: write`, `id-token: write`, and `attestations: write` permissions. It builds official assets, creates GitHub OIDC build-provenance attestations for every asset, and creates the release. Release publication remains a privileged operation; protect tag creation and workflow changes.

A consumer must:

1. obtain the exact release through a trusted channel;
2. require the ZIP sidecar or exact `SHA256SUMS` entry;
3. compare SHA-256 before extraction or execution;
4. independently verify a signature or artifact attestation from a trusted project identity;
5. review the exact source commit, catalog, and overrides; and
6. stop if provenance is unavailable or inconsistent.

A checksum and artifact from one compromised channel are not independent publisher authentication. Full-SHA-pinned CI actions reduce mutable-tag risk but do not eliminate action, runner, or GitHub compromise. Never place provider keys in CI or release automation.

### Update transaction and rollback limits

An installer-wide lock prevents concurrent normal updates. The installer stages a versioned release, snapshots core generated state and the two auto-detected client files, safely stops the prior managed process, atomically changes `current`, preserves valid existing port/retry/cooldown settings unless explicitly overridden, runs bootstrap/doctor/start, and retains old releases. On a handled failure after stop or switch, it attempts to restore the old pointer/state/auto clients and restart the previous release.

This is **best-effort rollback**, not an ACID or power-loss transaction. Forced termination, host crash, storage exhaustion, filesystem/ACL failure, malicious interference, or a custom client path can leave partial state. Rollback itself and old-process restart can fail. Keep an independent protected backup until verification. On Linux, a crash can leave `.install.lock`; remove it only after confirming no installer runs. On Windows, the `install.lock` file normally persists while only its exclusive handle denotes activity.

Versioned releases, caches, timestamped client backups, and external backups are not automatically pruned. They can preserve vulnerable code and secrets.

## Safe deployment checklist

Before first use:

- [ ] Verify ZIP/installer digests and independent signature or attestation.
- [ ] Confirm the exact tag, commit, `VERSION`, and `release-manifest.json`.
- [ ] Review `catalog.json` and all installer overrides.
- [ ] Use a dedicated, non-shared OS account/workstation where practical.
- [ ] Keep install/state/client paths off shared, synced, or network filesystems.
- [ ] Confirm the listener is `127.0.0.1`, not wildcard/LAN/forwarded.
- [ ] Use direct IBM HTTPS and normal system trust; custom proxy/CA environment is not inherited.
- [ ] Generate a unique local master key and at least two distinct authorized keys in each exact pool.
- [ ] Run `doctor` and check deployment count and listen address.
- [ ] Run `status` and confirm identity/liveness.
- [ ] Inspect Unix modes or Windows protected ACLs for state/client files.
- [ ] Protect or expire backups and installer rollback remnants.
- [ ] Confirm clients use only intended provider IDs ending in `-router`.

During operation:

- [ ] Keep the host and latest supported release patched.
- [ ] Treat error logs and diagnostics as sensitive.
- [ ] Monitor IBM usage and revoke unexpected credentials promptly.
- [ ] Do not infer fair use from `simple-shuffle`.
- [ ] Design clients for interrupted streams and possible duplicate work.
- [ ] Review retained releases/backups and remove them under policy.

## Rotation and incident response

For planned upstream-key rotation:

1. prepare a protected key-rotator document using literal or environment-backed values;
2. stop the router;
3. run `bootstrap --replace-secrets --import-key-rotator /protected/path --client auto`;
4. run `configure-clients --client /each/explicit/models.json` for client paths outside auto detection;
5. run `doctor` and start the router;
6. restart clients and confirm `status`; and
7. remove obsolete secret/client backups under the retention policy.

Replacement import creates a new random local master as well as replacing upstream values. Every client file must receive that new value before it can authenticate. Do not delete backups until rollback is no longer required, but do not retain them indefinitely.

For suspected compromise:

1. stop the router and isolate the host;
2. revoke affected IBM ICA credentials at the authoritative service;
3. rotate the local master and all possibly exposed upstream keys;
4. preserve only minimum forensic material under restricted access;
5. inspect source provenance, catalog, current pointer, state generation, clients, retained releases, backups, processes, and logs;
6. rebuild on a trusted host from a newly authenticated release when integrity is uncertain; and
7. report a product vulnerability privately without working secrets.

Deleting files does not revoke credentials and may not erase SSD blocks, snapshots, sync history, crash reports, or backups.
