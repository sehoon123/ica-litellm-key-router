# ICA LiteLLM 키 라우터

[English](README.md) | [한국어](README.ko.md)

[![CI](https://github.com/sehoon123/ica-litellm-key-router/actions/workflows/ci.yml/badge.svg)](https://github.com/sehoon123/ica-litellm-key-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

IBM ICA Services Essentials 요청을 여러 API key에 분산하는 로컬 인증 [LiteLLM](https://github.com/BerriAI/litellm) 프록시입니다. Pi, prime-agent, Codex, Claude Code가 사용하는 OpenAI Responses, Anthropic Messages, Gemini `generateContent` 네이티브 인터페이스를 유지합니다.

이 라우터는 credential을 발급하지 않습니다. 사용 권한이 있는 credential과 IBM 서비스만 사용하십시오.

## 주요 기능

- 기본적으로 정확히 한 개의 LiteLLM worker를 `127.0.0.1:4000`에서 실행합니다.
- 로컬 client provider 3개와 provider-qualified model alias 12개를 제공합니다.
- alias와 해당 pool의 credential 조합마다 LiteLLM deployment 하나를 만듭니다.
- `simple-shuffle`로 정상 deployment를 무작위 선택합니다. round-robin이 아닙니다.
- 지정된 오류가 발생하면 deployment를 즉시 cooldown하고, 출력 전 재시도가 가능한 오류를 재시도합니다.
- provider SDK 재시도는 0이고 weighted failover는 비활성화합니다.
- 로컬 LiteLLM worker에서 ICA로 보내는 모든 JSON body에 top-level `"no-log": true`를 강제로 넣습니다.
- Raw upstream key를 생성된 `config.yaml`에 넣지 않습니다.
- Command-backed client credential을 사용하므로 생성된 Pi, Claude Code, Codex 설정에 local master key 사본을 저장하지 않습니다.
- 관련 없는 설정이나 Codex 구독 기본값을 교체하지 않고 Pi/prime-agent, Claude Code, 별도 Codex profile을 구성합니다.
- Linux에서 supervision 및 restart-on-failure가 적용된 `systemd --user` service를 설치할 수 있습니다.
- Python `3.12.13`, `uv` `0.12.2`, LiteLLM `1.98.0` 및 `uv.lock`으로 고정된 전용 runtime을 설치합니다.

## 아키텍처와 신뢰 경계

```text
Pi / prime-agent / Claude Code / Codex / curl
        |
        | command로 가져온 local master key + loopback HTTP
        v
LiteLLM 1.98.0, worker 1개, 127.0.0.1:4000
        |
        | alias -> 무작위 정상 credential deployment
        | process environment로 provider key 전달
        v
IBM ICA Services Essentials HTTPS gateway
  - ica-services-essentials pool
```

`tools/routerctl.py`는 Python 표준 라이브러리만 사용하는 control plane입니다. LiteLLM은 data plane입니다. Installer는 versioned release를 만들고, private state를 생성하고, offline `doctor`를 실행한 후 라우터를 시작합니다.

“로컬 전용”은 client-facing listener가 같은 호스트에 있다는 뜻입니다. Prompt, response, tool data, model metadata는 `catalog.json`이 선택한 IBM endpoint로 HTTPS를 통해 컴퓨터 밖으로 전송됩니다. 이 프로젝트에는 hosted relay나 database가 없습니다. Launcher는 고정 path, key 변수, 강제 production/error logging, telemetry control, state 아래의 private `process-home`, `process-cache`, `process-tmp`만 포함한 최소 LiteLLM child environment를 만듭니다. Ambient proxy/custom-CA 변수와 environment 기반 provider 기능은 상속되지 않으므로 정상 system trust 설정으로 IBM에 직접 HTTPS 연결할 수 있어야 합니다.

생성되는 모든 deployment는 `extra_body`의 기본값을 `{"no-log": true}`로 설정합니다. Release에 포함된 callback은 LiteLLM이 deployment를 선택하고 request parameter를 병합한 뒤 이 값을 다시 적용하므로 들어오는 로컬 요청이 값을 덮어쓸 수 없습니다. 또한 LiteLLM `1.98.0`의 native Anthropic Messages allowlist를 보완하여 OpenAI Responses 및 Gemini와 동일한 top-level field를 ICA에 전달합니다. 이 설정은 local LiteLLM에서 ICA로 나가는 request body만 제어하며 ICA와 그 이후 upstream이 이 field를 준수하는지는 이 프로젝트의 trust boundary 밖입니다.

Loopback listener는 호스트 밖으로 나가지 않으므로 HTTP를 사용합니다. Bootstrap과 runtime 검증은 `127.0.0.1` 이외의 host를 거부합니다. Port는 변경할 수 있습니다. 이 프로젝트는 TLS, 원격 접근 제어, multi-user isolation을 제공하지 않습니다.

### Credential pool과 upstream

| Pool | Catalog provider | `catalog.json`의 HTTPS base |
|---|---|---|
| `ica-services-essentials` | `ica-se-openai`, `ica-se-claude`, `ica-se-gemini` | `https://api.servicesessentials.ibm.com/v1`, `https://api.servicesessentials.ibm.com`, `https://api.servicesessentials.ibm.com/v1beta` |

OpenAI 항목은 API version `v1`과 LiteLLM Azure Responses transformer를 사용합니다. Provider-qualified alias는 OpenAI, Anthropic, Gemini 표면을 서로 다른 LiteLLM model group으로 유지합니다. `catalog.json` 변경은 credential 전송 대상 변경으로 간주하십시오. `ibm-ica-nextgen`은 deprecated되었으며 생성하거나 호출하지 않습니다.
원래 IBM Services Essentials Responses endpoint는 `https://api.servicesessentials.ibm.com/v1/responses`입니다. 생성된 LiteLLM `api_base`의 `?_litellm_route=/openai/responses`는 Azure transformer가 `/openai/responses`를 한 번 더 붙이지 않게 하는 LiteLLM `1.98.0` 전용 URL-builder compatibility marker입니다. IBM API parameter가 아니므로 IBM endpoint를 직접 호출할 때는 넣지 마십시오.

### Routing과 재시도 동작

생성된 router 설정은 다음을 사용합니다.

- 동일한 weight의 `routing_strategy: simple-shuffle`;
- `enable_weighted_failover: false`;
- 기본 router `num_retries: 2`. 단, 대체 key 수(`key 수 - 1`) 이하로 제한되며 `--max-fallbacks` / private runtime state의 `maxFallbacks`는 설정 상한값입니다;
- deployment/provider SDK별 `max_retries: 0`;
- LiteLLM `BadRequestError`와 `ContentPolicyViolationError` 재시도 0;
- authentication, timeout, rate-limit 오류에 router 재시도 최대 2회. 같은 global 횟수는 의도된 5xx 재시도와 stock LiteLLM 1.98.0이 표준적으로 재시도 가능하다고 판단하는 다른 status/exception에도 적용됨;
- `allowed_fails: 0`, 그리고 authentication, rate limit, timeout, internal-server, unavailable, bad-gateway 오류의 허용 실패 횟수 0. 해당 deployment는 즉시 cooldown 대상이 됩니다;
- 기본 cooldown 60초;
- Responses deployment 및 encrypted-content affinity용 pre-call check.

**`simple-shuffle`은 무작위 선택이며 round-robin이 아닙니다.** 안정적인 key 순서, 단기간의 동일한 트래픽 분배, 모든 key 시도를 보장하지 않습니다. 기본 `maxFallbacks: 2`에서는 재시도 가능한 요청이 최초 시도 후 router 재시도를 최대 두 번 할 수 있지만 대체 key 수보다 많이 재시도하지 않습니다. 따라서 설정된 key가 한 개이면 router 재시도는 0회입니다. 재시도는 같은 provider-qualified alias의 deployment 안에서만 수행됩니다. Cross-model 또는 cross-provider fallback map은 없습니다.

명시적 policy 항목은 bad request와 content-policy 오류의 재시도를 막습니다. Stock LiteLLM `1.98.0`에는 `InternalServerErrorRetries` policy key가 없으므로 의도된 5xx 재시도를 위해 global 횟수가 더 넓게 적용되어야 합니다. LiteLLM의 다른 표준 retryable-status 판단도 따를 수 있습니다. 엄격한 네 가지 오류 class allowlist가 아닙니다.

출력 전 오류도 모호할 수 있습니다. Router가 오류를 확인하고 재시도하기 전에 IBM이 작업을 수락, 실행 또는 과금했을 수 있습니다. Exactly-once 시스템이 아닙니다. **Mid-stream failover는 없습니다.** 응답 byte 전송이 시작된 뒤 stream이 실패하면 client가 다시 요청해야 하며, 중복 작업과 비용 위험이 같습니다.

이 라우터는 의도적으로 **single worker**만 사용합니다. 로컬 process identity, lock, cooldown state, controller는 multi-worker 또는 distributed 설계가 아닙니다.

## Client provider 3개와 alias 12개 전체 목록

Bootstrap이 다음 provider ID를 만듭니다. Pi와 prime-agent에서는 model 이름 끝에 `(key router)`가 표시됩니다.

| 로컬 client provider | 네이티브 API | Model alias |
|---|---|---|
| `ica-se-openai-router` | OpenAI Responses | `ica-se-openai--gpt-5.6-luna-dzus`<br>`ica-se-openai--gpt-5.6-terra-dzus`<br>`ica-se-openai--gpt-5.6-sol` |
| `ica-se-claude-router` | Anthropic Messages | `ica-se-claude--claude-sonnet-4-6`<br>`ica-se-claude--claude-sonnet-5`<br>`ica-se-claude--claude-opus-4-6`<br>`ica-se-claude--claude-opus-4-8`<br>`ica-se-claude--claude-opus-5`<br>`ica-se-claude--claude-haiku-4-5` |
| `ica-se-gemini-router` | Gemini `generateContent` | `ica-se-gemini--gemini-3.7-flash`<br>`ica-se-gemini--gemini-3.6-flash`<br>`ica-se-gemini--gemini-3.5-flash` |

Deployment 수는 다음과 같습니다.

```text
12 × ica-services-essentials key 수
```

최소 한 개의 key를 넣으면 deployment 12개가 생성됩니다.

## 요구 사항

- 각 catalog pool에 사용 권한이 있는 key가 한 개 이상 있어야 합니다.
- Runtime에서 IBM gateway로 직접 outbound HTTPS 연결할 수 있어야 합니다.
- 설치 중 GitHub, `astral.sh`, PyPI 및 `uv` managed Python source에 접근할 수 있어야 합니다.
- 사용 가능한 loopback port가 있어야 합니다. 기본값은 `4000`입니다.
- Linux: Bash, `curl`, 그리고 `sha256sum` 또는 `shasum`.
- Windows: Windows PowerShell 5.1 또는 PowerShell 7, NTFS 방식 ACL 지원, inbox `%SystemRoot%\System32\curl.exe`.
- 전용 tool cache와 보존되는 release별 environment를 위한 disk 공간.

Installer 실행에 project용 Python을 미리 설치할 필요는 없습니다. Installer가 `uv` `0.12.2`를 검증하여 전용 위치에 설치하고, 정확한 Python `3.12.13`을 설치한 뒤 `uv sync --frozen --no-dev`, `uv pip check`를 실행하고 LiteLLM `1.98.0`을 확인합니다. Ambient `UV_*`, `PIP_*`, `PYTHON*` 설정은 dependency 선택에 사용되지 않습니다. 다른 OS와 shared-service 배포는 지원하지 않습니다.

## 검토한 clone에서 빠르게 설치

신뢰할 revision을 clone하고 검토한 뒤 해당 local source tree에서 설치합니다.

```bash
git clone https://github.com/sehoon123/ica-litellm-key-router.git
cd ica-litellm-key-router
```

`~/.pi/agent/key-rotator.json`에 `ica-services-essentials` pool이 이미 있다면 다음 명령이 가장 짧은 비대화식 설치 방법입니다. 기존 key를 자동으로 import하고 supervised Linux user service를 활성화합니다.

```bash
ICA_ROUTER_NON_INTERACTIVE=1 bash ./install-linux.sh --systemd-user
```

Import 가능한 key-rotator 파일이 없는 새 환경에서는 대화식으로 실행하고 prompt에 사용 권한이 있는 Services Essentials key를 입력합니다.

```bash
bash ./install-linux.sh --systemd-user
```

Pi, Claude Code, 별도 Codex profile을 구성합니다. 생성 client는 `ica-router client-token`으로 local credential을 가져오며 token을 저장하지 않습니다.

```bash
$HOME/.local/share/ica-litellm-key-router/ica-router configure-harnesses --all
```

Router 상태와 Pi를 통한 GPT-5.6 Sol 호출을 확인합니다.

```bash
$HOME/.local/share/ica-litellm-key-router/ica-router doctor
$HOME/.local/share/ica-litellm-key-router/ica-router status
pi --print \
  --model 'ica-se-openai-router/ica-se-openai--gpt-5.6-sol' \
  'Reply with exactly: OK'
```

Codex 0.134.0 이상에서는 생성된 `~/.codex/ica-router.config.toml` profile을 `codex --profile ica-router`로 사용합니다. Claude Code는 `~/.claude/settings.json`을 읽습니다. `apiKeyHelper`와 충돌할 수 있는 shell-level `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` 값은 해제하십시오. Installer는 관련 없는 Pi provider를 보존하고 harness merge는 관련 없는 Claude 설정을 보존합니다. Clone 경로는 설치 source일 뿐이며 runtime state는 `$HOME/.local/share/ica-litellm-key-router` 아래에 저장됩니다.

## 설치 전 release 검증

`v0.2.2-rc.5` runtime source asset 이름은 다음과 같습니다.

> `v0.2.2-rc.5`은 prerelease입니다. GitHub Actions 장애 중에는 초기 asset을 수동 게시할 수 있으므로 `SHA256SUMS`, exact-tag manifest, annotated tag를 검증하십시오. Release workflow가 복구되어 deterministic asset을 reconcile한 뒤 GitHub provenance attestation을 사용할 수 있습니다.

```text
ica-litellm-key-router-v0.2.2-rc.5.zip
ica-litellm-key-router-v0.2.2-rc.5.zip.sha256
```

ZIP sidecar는 소문자 SHA-256 digest, 공백 두 개, 정확한 ZIP filename, 마지막 newline으로 이루어진 한 줄이어야 합니다. Release에는 standalone `install-linux.sh`, `install-windows.ps1`, `release-manifest.json`, `SHA256SUMS`도 포함됩니다. `SHA256SUMS`는 ZIP, ZIP sidecar, installer 두 개, manifest를 모두 검증합니다.

ZIP과 sidecar를 받은 뒤 Linux에서 확인하는 예:

```bash
sha256sum --check --strict ica-litellm-key-router-v0.2.2-rc.5.zip.sha256
```

`SHA256SUMS`에 적힌 release 파일을 모두 받은 뒤 installer 두 개를 포함한 전체 set을 확인하십시오.

```bash
sha256sum --check --strict SHA256SUMS
```

Windows에서 exact ZIP sidecar를 확인하는 예:

```powershell
$asset = 'ica-litellm-key-router-v0.2.2-rc.5.zip'
$line = [IO.File]::ReadAllText("$asset.sha256", [Text.Encoding]::ASCII)
if ($line -notmatch '\A([0-9a-f]{64})  ([^\r\n]+)\r?\n\z' -or $Matches[2] -ne $asset) {
  throw 'Invalid checksum sidecar'
}
$actual = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $Matches[1]) { throw 'Checksum mismatch' }
```

Standalone installer는 실행하기 전에 `SHA256SUMS`의 정확한 항목과 비교하십시오.

```powershell
$name = 'install-windows.ps1'
$pattern = '^([0-9a-f]{64})  ' + [regex]::Escape($name) + '$'
$entry = @(Get-Content -LiteralPath .\SHA256SUMS | Where-Object { $_ -match $pattern })
if ($entry.Count -ne 1 -or $entry[0] -notmatch $pattern) { throw 'Missing or duplicate checksum entry' }
$expected = $Matches[1]
$actual = (Get-FileHash -LiteralPath $name -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'Checksum mismatch' }
```

같은 channel에서 받은 checksum은 파일 손상이나 byte 불일치를 찾지만 publisher identity를 독립적으로 인증하지는 않습니다. 실행하기 전에 정확한 tag/commit과 신뢰하는 프로젝트 identity의 signature 또는 artifact attestation도 확인하십시오. GitHub artifact attestation이 배포된 경우의 예:

```bash
gh attestation verify ica-litellm-key-router-v0.2.2-rc.5.zip \
  --repo sehoon123/ica-litellm-key-router
```

Remote-source installer mode는 정확한 tag ZIP과 정확한 sidecar를 HTTPS로 받고 SHA-256 및 `VERSION`을 확인합니다. Traversal, link/special file, 중복 또는 대소문자 충돌 이름, 과도한 member 수와 압축 해제 크기를 거부하는 bounded safe extractor를 사용합니다. 고정된 Astral installer script도 실행 전에 검증합니다. 이러한 검사는 signature/provenance 검증을 대신하지 않습니다. Local-source mode는 사용자가 제공한 directory를 신뢰하므로 먼저 검증해야 합니다.

Maintainer는 deterministic ZIP과 sidecar를 다음과 같이 만들 수 있습니다.

```bash
python scripts/build-release.py --output-dir dist
```

배포 전 CI 실행, ZIP 재현, sidecar 검증, safe extraction, Linux/Windows의 fresh install 및 update test, 정확한 digest의 signing 또는 attestation을 release gate로 수행해야 합니다. Git metadata가 있으면 `release-manifest.json`에는 version, tag, 정확한 source commit, primary asset digest가 항상 기록됩니다. Build에는 Git worktree가 필요합니다.

## Linux 설치

검증하고 압축 해제한 release에서 실행합니다.

```bash
cd /path/to/ica-litellm-key-router-v0.2.2-rc.5
bash ./install-linux.sh
```

별도로 검증한 standalone installer도 사용할 수 있습니다. 옆에 완전한 source tree가 없으면 `ICA_ROUTER_REF`의 정확한 asset을 받습니다. 기본값은 `v0.2.2-rc.5`입니다.

```bash
ICA_ROUTER_REF=v0.2.2-rc.5 bash ./install-linux.sh
```

최초 실행에서는 각 catalog pool의 key를 한 개씩 안전하게 입력받습니다. 입력 내용은 화면에 표시되지 않습니다. 해당 pool의 마지막 key 다음 prompt에서 아무것도 입력하지 않고 Enter를 누르면 입력이 끝납니다. 각 pool에는 최소 한 개의 key가 필요합니다. 이후 모든 deployment를 생성하고 LiteLLM을 시작합니다.

다시 실행했을 때 저장된 secrets, 생성 state, 선택 release가 `doctor`를 통과하면 dependency를 다시 받거나 설치하지 않습니다. LiteLLM이 실행 중인지 확인하고, 중지되어 있으면 시작하기만 합니다. 실제 재설치 또는 update가 필요하면 `--force-install`, 모든 key를 다시 입력하려면 `--replace-keys`를 사용합니다. `--replace-keys`는 로컬 proxy master key를 보존하므로 기존 client 파일의 인증이 유지됩니다.
기존 state를 upgrade할 때 bootstrap은 `ica-services-essentials`를 보존하고 active secrets에서 deprecated `ibm-ica-nextgen` pool을 제거하기 전에 private timestamp backup을 만듭니다. 명시적인 client merge는 deprecated NextGen router provider ID 3개도 제거합니다. 보호된 secrets backup에는 폐기된 NextGen 값이 남을 수 있으므로 검증 후 필요 없으면 해당 backup을 삭제하십시오.

Client와 service 설정은 명시적인 선택 사항이며 설치 중 또는 설치 후 따로 만들 수 있습니다.

```bash
# models.json을 변경하지 않고 background lifecycle 사용(기본 동작)
bash ./install-linux.sh

# User login 시 supervised autostart 활성화
bash ./install-linux.sh --systemd-user

# Pi의 command-backed router provider 생성 또는 병합
bash ./install-linux.sh --pi-models

# Pi-format client 파일 두 개 생성 또는 병합
bash ./install-linux.sh --pi-models --prime-models

# 원하는 Pi-format 경로 생성 또는 병합. 여러 경로면 옵션을 반복함
bash ./install-linux.sh --models-json /private/path/models.json

# 설치 후 지원 harness 모두 구성
ica-router configure-harnesses --all

# Harness를 개별 선택. prime-agent는 명시적으로 선택
ica-router configure-harnesses --pi --claude-code --codex
ica-router configure-harnesses --prime
```

`configure-harnesses`가 생성하거나 병합하는 항목:

- `~/.pi/agent/models.json`의 Pi router provider;
- 선택한 경우 `~/.prime/agent/models.json`의 prime-agent provider;
- `~/.claude/settings.json`의 Claude Code gateway 설정과 `apiKeyHelper`;
- Codex 0.134.0 이상에서 구독 기본값을 유지하는 별도 `~/.codex/ica-router.config.toml` profile.

생성된 모든 인증 설정은 local master key를 저장하지 않고 `ica-router client-token`을 호출합니다. Codex profile은 router가 출력 전 재시도를 담당하므로 자체 request/stream retry를 비활성화합니다.

현재 Apache Maka에는 command-backed model credential hook이나 안정적인 non-interactive model connection command가 없으므로 `configure-harnesses`가 변경 중인 workspace catalog 또는 plaintext credential vault를 직접 편집하지 않습니다. **Settings → Models**에서 provider type `openai-responses-compatible`, base URL `http://127.0.0.1:4000/v1`, model `ica-se-openai--gpt-5.6-sol`, API key에는 `ica-router client-token` 출력값을 한 번 설정하십시오. Maka에는 local router key 사본이 저장되지만 upstream IBM key rotation은 계속 이 router 한 곳에서 수행됩니다. 다른 Maka 프로젝트를 의미한다면 해당 프로젝트의 custom Responses endpoint contract를 별도로 확인해야 합니다.

Installer 동작:

1. 설치 전체 범위 lock을 획득합니다.
2. 미리 검증한 local source 또는 검증된 exact-tag ZIP을 선택합니다.
3. 고정된 전용 toolchain과 frozen environment를 새 versioned release에 설치합니다.
4. 생성된 private state와 명시적으로 요청한 client 파일을 snapshot합니다.
5. 기존 managed router를 안전하게 중지합니다.
6. `current`를 새 release로 atomic 전환합니다.
7. 유효한 기존 secrets를 보존합니다. 최초 설치에서는 각 pool에서 빈 값을 입력할 때까지 key를 받습니다.
8. 생성 state를 쓰고, 요청한 경우에만 client 파일을 생성하거나 병합합니다.
9. `doctor` 실행 후 worker 한 개를 직접 또는 요청/보존된 systemd user service로 시작하고, best-effort `~/.local/bin/ica-router` symlink를 만듭니다.

기본 layout:

| 항목 | Linux path |
|---|---|
| Install root | `${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router` |
| Versioned release | `<install-root>/releases/<release-id>` |
| 선택된 release | `<install-root>/current` symlink |
| 선택된 app/environment | `<install-root>/current/app`, `<install-root>/current/.venv` |
| 영구 private state | `<install-root>/state` |
| 전용 `uv`와 cache | `<install-root>/tools/uv-0.12.2`, `<install-root>/cache/uv` |
| 직접 wrapper | `<install-root>/ica-router` |
| 편의 symlink | `~/.local/bin/ica-router` |
| 선택 가능한 managed user unit | `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ica-litellm-key-router.service` |

`~/.local/bin`이 `PATH`에 없으면 직접 wrapper를 실행하거나 해당 directory를 `PATH`에 추가하십시오. Installer는 관련 없는 `~/.local/bin/ica-router` path를 덮어쓰지 않습니다. 선택 가능한 systemd unit 이름은 user 전체에서 하나입니다. Installer는 `ExecStart`가 같은 install root를 가리킬 때만 기존 unit을 보존하며, 다른 root에서 unit을 자동으로 가져오지 않고 다른 설치의 unit을 `--systemd-user`로 명시적으로 덮어쓰는 것도 거부합니다.

Override 예:

```bash
ICA_ROUTER_HOME='/private/path/ICA router' \
ICA_ROUTER_SOURCE_DIR='/verified/source/path' \
ICA_ROUTER_KEY_ROTATOR='/private/path/key-rotator.json' \
ICA_ROUTER_NON_INTERACTIVE=1 \
bash ./install-linux.sh
```

`ICA_ROUTER_REF`는 정확한 `vMAJOR.MINOR.PATCH` 또는 `vMAJOR.MINOR.PATCH-rc.N` tag여야 합니다. 유효한 기존 `secrets.json` 또는 import 가능한 key-rotator 문서가 있을 때만 `ICA_ROUTER_NON_INTERACTIVE=1`을 설정하십시오. 그렇지 않으면 prompting 대신 fail-closed합니다. 이 설정이 없으면 secret 입력에 실제 terminal이 필요합니다.

Crash 후 `<install-root>/.install.lock` directory가 남으면 Linux 설치가 차단됩니다. 실행 중인 installer가 없음을 확인한 후에만 제거하십시오.

## Windows 설치

검증하고 압축 해제한 release에서 실행합니다.

```powershell
Set-Location 'C:\path\to\ica-litellm-key-router-v0.2.2-rc.5'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
```

기본 layout:

| 항목 | Windows path |
|---|---|
| Install root | `%LOCALAPPDATA%\IcaLiteLLMKeyRouter` |
| Versioned release | `<install-root>\releases\<release-id>` |
| 선택된 release ID | text file `<install-root>\current` |
| 선택된 app/environment | `<install-root>\releases\<current>\app`, `<install-root>\releases\<current>\.venv` |
| 영구 private state | `<install-root>\state` |
| 전용 `uv`와 cache | `<install-root>\tools\uv-0.12.2`, `<install-root>\cache\uv` |
| Wrapper | `<install-root>\ica-router.ps1` |

Windows도 최초 실행의 빈 값까지 key 입력, 설정 생성, 시작 및 이후 start-only fast path를 수행합니다. Wrapper를 `PATH`에 추가하지 않습니다. Installer/control-plane private path의 inherited access를 제거하고 protected current-user-only allow ACL을 설정·검증할 수 없으면 fail-closed합니다. Runtime cache/temp 하위 항목은 protected parent에서 current-user-only access를 상속합니다. Client 파일과 backup도 제한하지만 임의 client parent directory의 ACL은 변경하지 않습니다.

Client 설정도 명시적인 선택 사항입니다.

```powershell
# 기본: models.json을 변경하지 않음
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1

# Pi 파일 생성 또는 병합
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 -PiModels

# 다른 선택: -PrimeModels 또는 -ModelsJson 'D:\Private\models.json'
```

설치 후 Windows wrapper에서도 같은 command-backed harness 설정을 생성할 수 있습니다.

```powershell
$router = Join-Path $env:LOCALAPPDATA 'IcaLiteLLMKeyRouter\ica-router.ps1'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router configure-harnesses --all
```

모든 Services Essentials key를 다시 입력하려면 `-ReplaceKeys`, 실제 재설치/update에는 `-ForceInstall`을 사용합니다.

Override 예:

```powershell
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 `
  -InstallRoot 'D:\Private\ICA Router' `
  -SourceDirectory 'D:\Verified\ica-litellm-key-router-v0.2.2-rc.5' `
  -KeyRotatorPath 'D:\Private\key-rotator.json' `
  -NonInteractive
```

Environment 대응값은 `ICA_ROUTER_HOME`, `ICA_ROUTER_SOURCE_DIR`, `ICA_ROUTER_KEY_ROTATOR`, `ICA_ROUTER_NON_INTERACTIVE`, exact-tag `ICA_ROUTER_REF`입니다. Redirected input은 non-interactive로 처리됩니다. 영구 `install.lock` 파일은 installer 실행 중 exclusive sharing으로 열립니다. 파일이 남는 것은 정상이며 존재한다는 이유만으로 삭제하지 마십시오.

## Secrets schema

`examples/secrets.example.json`과 아래 block에는 placeholder만 있습니다. 의도적으로 사용할 수 없습니다.

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

Placeholder를 private state에 복사하지 마십시오. Interactive bootstrap은 무작위 local master key를 만듭니다. 검증 조건:

- `schemaVersion`은 정확히 `1`;
- `masterKey`는 `sk-`로 시작하고 UTF-8 24–1024 byte이며 NUL/newline/placeholder marker가 없어야 함;
- 위의 정확한 `ica-services-essentials` pool ID 하나;
- pool에 key 1–256개;
- 각 pool 안에서 보수적인 identifier 형식에 맞는 고유한 key `id`;
- 각 provider value는 비어 있지 않고 UTF-8 4096 byte 이하이며 NUL/newline/placeholder marker가 없어야 함;
- 같은 pool 안에 중복 provider value가 없어야 함;
- 전체 environment 이름/value 합계 24 KiB 이하, parsing된 JSON 문서 8 MiB 이하.

Auto-import는 `~/.pi/agent/key-rotator.json`, `~/.prime/agent/key-rotator.json`을 확인합니다. Import 항목은 `value`, `env`, `command` 중 정확히 하나를 사용해야 합니다. Literal 또는 사용 가능한 environment 항목은 `secrets.json`으로 복사됩니다. `command` 항목은 거부되며 자동 실행되지 않습니다. Import 문서 예:

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

Unix에서 installer-owned private directory는 mode `0700`, private file은 `0600`을 사용합니다. Windows는 protected current-user-only ACL을 사용하며 제한 또는 검증이 실패하면 중단합니다. Raw upstream key는 `state/secrets.json`에 plaintext로 저장되고 LiteLLM process environment에 로드됩니다. `config.yaml`에는 raw key가 아닌 environment reference가 들어갑니다.

Local master key는 IBM key가 아닙니다. 모든 로컬 router route에 인증된 접근 권한을 주며 canonical 값은 `state/secrets.json`과 LiteLLM process environment에만 남습니다. 생성된 Pi/prime-agent provider, Claude Code `apiKeyHelper`, Codex router profile은 `ica-router client-token`을 실행하므로 key 사본을 저장하지 않습니다. 따라서 local key를 교체하거나 명시적으로 rotate해도 command-backed client file을 다시 쓸 필요는 없지만, 실행 중인 client가 helper 결과를 잠시 cache할 수 있어 retry 또는 restart가 필요할 수 있습니다. Timestamp backup은 merge 전 client 파일 전체를 포함하므로 기존의 다른 provider secret이 들어갈 수 있습니다. State, client file, process environment, log, memory, helper output, backup을 secret으로 취급하십시오. 다른 process가 같은 client 파일을 쓰는 동안 병합하지 마십시오.

## 네이티브 API endpoint

아래 placeholder를 private local `masterKey`로 바꾸십시오. Upstream ICA credential을 사용하면 안 됩니다.

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
Endpoint:

- `POST /v1beta/models/{model}:generateContent`
- `POST /v1beta/models/{model}:streamGenerateContent?alt=sse`

Gemini에는 `Authorization: Bearer ...`를 사용하십시오. LiteLLM의 `/gemini` provider pass-through route를 client에 설정하면 안 됩니다. 이 route는 프로젝트가 생성한 `model_list`와 credential routing을 우회합니다.

## 라우터 운영

Linux:

```bash
ica-router status
ica-router doctor
ica-router stop
ica-router start
ica-router install-systemd-user
ica-router uninstall-systemd-user
```

Managed systemd unit은 LiteLLM을 foreground로 실행하고 authenticated readiness를 기다리며 실패 시 재시작하고 user login 시 자동 시작하도록 enable됩니다. Login 전 boot 시점부터 실행하려면 관리자/사용자가 systemd lingering을 별도로 enable해야 하며 router가 lingering 설정을 자동 변경하지는 않습니다. Linux installer는 이미 enable된 managed unit을 update 후에도 보존하며 `--systemd-user`로 명시적으로 활성화할 수도 있습니다.

Windows:

```powershell
$router = Join-Path $env:LOCALAPPDATA 'IcaLiteLLMKeyRouter\ica-router.ps1'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router status
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router doctor
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router stop
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $router start
```

Lifecycle command는 private `state/command.lock`의 OS lock을 사용합니다. `run.json`에는 PID, process creation token, executable, config path와 digest가 기록됩니다. `stop`은 creation token과 command line이 모두 맞는 process에만 signal을 보내며, 살아 있는 PID의 identity가 다르면 거부합니다.

`start`는 state를 검증하고, foreign listener를 거부하고, worker 한 개를 시작하고 identity를 기록합니다. 그런 다음 process identity, `/health/liveliness`, 설정 alias가 포함된 인증된 `/v1/models` 응답을 기다립니다. 이 readiness check는 IBM을 호출하지 않습니다.

`status` exit code:

| Code | 의미 |
|---|---|
| `0` | 기록된 identity가 맞고 로컬 liveness 성공 |
| `1` | 중지 상태. 죽은 stale state는 제거됨 |
| `2` | 일치한 process의 liveness 실패, 생성 config 변경으로 restart 필요, 또는 다른 검증 오류 |
| `3` | 기록 PID는 살아 있지만 identity가 다름 |

`doctor`는 IBM에 대해 offline입니다. Catalog/state schema, generation marker, deployment 수, `config.yaml`에 raw credential이 없는지, Unix private mode를 검사합니다. IBM이 key를 승인하는지 또는 quota가 있는지는 검증하지 못합니다.

Private state 변경 command는 managed router가 중지되어 있어야 하며 `command.lock`도 획득합니다.

```bash
ica-router generate
ica-router configure-clients
ica-router configure-clients --client /private/path/models.json
ica-router bootstrap --port 4100 --client auto
```

`bootstrap`은 secret을 생성하거나 보존하고 완전한 생성 state를 다시 씁니다. `generate`는 현재 generation이 일관된 경우에만 실행됩니다. `configure-clients`는 현재 라우터 소유 Pi-format provider 3개를 병합합니다. Auto mode는 기존 `~/.pi/agent/models.json`, `~/.prime/agent/models.json`만 변경합니다.

Harness 설정은 generation-bound snapshot을 읽고 client file만 변경하므로 router가 정상 실행 중일 때도 사용할 수 있습니다.

```bash
ica-router configure-harnesses --all
ica-router configure-harnesses --pi --claude-code --codex
ica-router configure-harnesses --prime
```

`client-token`은 생성된 command-backed 인증용입니다. 현재 local credential만 출력하며 `--bearer`를 사용하면 `Bearer <credential>`을 출력하므로 secret을 출력하는 command로 취급해야 합니다.

영구 state에는 `secrets.json`, `config.yaml`(YAML로 유효한 JSON), `runtime.json`, `generation.json`이 있습니다. Client 설정은 선택한 client file을 merge할 때만 생성합니다. Generation marker는 catalog, secrets, 생성 config, runtime의 normalized digest를 묶고 마지막에 기록되므로 중단된 생성은 fail-closed합니다. Lifecycle file은 `command.lock`, 실행 중에만 존재하는 `run.json`, `router.log`입니다. 10 MiB를 넘는 log는 `router.log.1`로 rotate됩니다. Private `process-home`, `process-cache`, `process-tmp`는 caller home에서 LiteLLM을 격리하지만 dependency cache나 임시 data가 남을 수 있으므로 state 보호 및 retention 대상에 포함하십시오.

## 문제 해결

### `ica-router: command not found`

`<install-root>/ica-router`를 직접 실행하거나 Linux에서 다음을 설정하십시오.

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Startup 또는 readiness 실패

Status, port owner, state 일관성, private error log를 확인하십시오.

```bash
ica-router status
ica-router doctor
sed -n '1,200p' "${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router/state/router.log"
```

Runtime은 의도적으로 최소 environment를 사용합니다. Ambient proxy, custom CA, user-site, environment 기반 provider 설정은 상속되지 않습니다. 이러한 기능이 필요한 network는 검토된 code/config 변경 없이는 지원하지 않습니다.

Secret을 보존하면서 local port를 바꾸려면:

```bash
ica-router stop
ica-router bootstrap --port 4100 --client auto
ica-router doctor
ica-router start
```

Client URL이 바뀌면 Pi/prime-agent를 다시 시작하십시오.

### Stale identity 또는 lifecycle lock

Status `3`은 PID가 살아 있지만 `run.json`과 맞지 않는다는 뜻입니다. PID만 보고 종료하지 마십시오. 보호된 record와 OS command line을 확인하십시오. 해당 process가 관련 없고 설정 port를 router가 사용하지 않음을 확인한 후에만 `run.json`을 제거하십시오.

“another router lifecycle command is in progress”는 다른 command가 `command.lock`의 OS lock을 소유한다는 뜻입니다. File은 lock 해제 후에도 정상적으로 남습니다. Active lock을 우회하려고 삭제하지 마십시오.

### Incomplete 또는 inconsistent generation

Router를 중지하고 `bootstrap`을 다시 실행하십시오. `generation.json`은 의도적으로 fail-closed합니다. 생성 파일 하나만 편집하거나 이미 일관성이 깨진 set에 `generate`를 실행해도 복구되지 않습니다.

### Permission 또는 ACL 오류

Linux에서는 변경하기 전에 state를 확인하십시오.

```bash
find "${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router/state" -maxdepth 1 -printf '%m %p\n'
```

Private directory는 `0700`, file은 `0600`이어야 합니다. Windows에서는 ACL 오류를 우회하지 마십시오. 의도한 user가 소유한 local NTFS path를 사용하고 `Get-Acl` 또는 `icacls`로 확인하십시오.

### Placeholder, pool 또는 duplicate-key 오류

Example은 유효한 secret이 아닙니다. 정확한 Services Essentials pool ID 하나와 사용 권한이 있는 key 한 개 이상이 필요합니다. Router를 중지하고 private source를 수정/import한 뒤 `bootstrap`을 실행하십시오. 기존 secret 문서를 교체하고 backup하려는 경우에만 `--replace-secrets`를 사용하십시오.

### Pi/prime-agent에 router provider가 없음

Auto mode는 기존 client 파일만 변경합니다. Router를 중지하고 정확한 path를 지정하십시오.

```bash
ica-router configure-clients --client /private/path/models.json
```

Merge는 다른 provider ID를 보존하고 현재 라우터 소유 ID 3개를 교체하고 deprecated NextGen ID 3개를 제거하며, 내용이 바뀔 때만 전체 파일의 timestamp backup을 만듭니다. 그 후 client를 다시 시작하십시오.

### Local 인증 실패

`ica-router client-token`은 command가 성공하는지만 확인할 때 사용하고 출력값을 config에 붙여 넣지 마십시오. Helper path를 복구하려면 `configure-harnesses` 또는 legacy Pi-only `configure-clients`를 다시 실행하십시오. Local master key를 명시적으로 rotate한 직후 실행 중인 client가 이전 helper 결과를 잠시 cache할 수 있으므로 retry 또는 restart하십시오. Local master key 대신 IBM key를 넣으면 안 됩니다.

### Upstream authentication, timeout 또는 rate-limit 실패

`doctor`는 IBM을 호출하지 않습니다. Pool 선택, 권한, quota, direct HTTPS, cooldown, private log를 확인하십시오. 실패한 deployment는 기본 60초 동안 cooldown될 수 있습니다. 강제 `ERROR` level이어도 log는 민감 정보로 취급하십시오.

## Update와 rollback

1. Release 및 schema note를 읽습니다.
2. State와 client 파일을 별도의 access-controlled 위치에 backup합니다.
3. 새 exact release, checksum, publisher provenance를 검증합니다.
4. 새 platform installer를 실행합니다.
5. `doctor`와 `status`를 확인하고 Pi/prime-agent를 다시 시작합니다.

Installer는 versioned release를 stage하고 유효한 runtime 설정을 보존하며, 처리된 오류가 발생하면 이전 release와 요청한 client 파일의 복원을 시도합니다. Rollback은 best effort이고 이전 release가 보존되며 backup에는 다른 client secret도 포함될 수 있습니다. 실패 한계는 [SECURITY.md](SECURITY.md#update-transaction-and-rollback-limits)를 참조하십시오.

## 삭제

자동 uninstaller는 없습니다.

1. Router를 중지합니다.
2. 설정된 모든 client 파일에서 위의 현재 `*-router` 항목 3개와 deprecated NextGen 항목 3개를 제거합니다.
3. 보존이 필요하지 않으면 timestamp client backup을 확인하고 제거합니다.
4. Wrapper/symlink와 확인한 install root를 제거합니다.
5. Host, process environment, state, log 또는 backup 노출 가능성이 있으면 IBM credential을 rotate합니다.

Linux 기본값:

```bash
ica-router uninstall-systemd-user || true
ica-router stop || true
rm -f "$HOME/.local/bin/ica-router"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/ica-litellm-key-router"
```

더 이상 필요하지 않으면 managed Claude Code 설정과 별도 Codex profile도 제거하십시오. 필요한 경우 timestamp backup을 이용해 이전 내용을 복구합니다.

Windows 기본값:

```powershell
$root = Join-Path $env:LOCALAPPDATA 'IcaLiteLLMKeyRouter'
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ica-router.ps1') stop
Remove-Item -LiteralPath $root -Recurse -Force
```

`ICA_ROUTER_HOME` 또는 `-InstallRoot`를 사용했다면 특히 path를 다시 확인하십시오. SSD, snapshot, 동기화 folder, backup에서 파일 삭제가 secure erasure를 보장하지 않습니다.

## 보안과 제한 사항

배포 전에 [SECURITY.md](SECURITY.md)를 읽으십시오. 주요 제한:

- filesystem permission/ACL로 plaintext secret을 보호하는 local single-user 용도;
- 무작위 `simple-shuffle`을 사용하는 worker 한 개, 모호한 출력 전 재시도 및 mid-stream failover 없음;
- ambient proxy, custom CA, environment 기반 provider 기능을 상속하지 않는 direct HTTPS 전용 runtime;
- `doctor`는 IBM credential/quota를 검증하지 않으며 model 동작과 retention은 upstream이 제어함; 그리고
- checksum은 publisher를 인증하지 않으며 update rollback은 power-loss atomic이 아님.

## 개발

실제 provider key 없이 검사할 수 있습니다.

```bash
uv lock --check
python -m unittest discover -s tests -v
python -m py_compile tools/routerctl.py tools/litellm_no_log.py tests/test_routerctl.py tests/test_litellm_no_log.py scripts/build-release.py
bash -n install-linux.sh
out="$(mktemp -d)"
python scripts/build-release.py --development --output-dir "$out"
```

`--development`는 local test에서만 untagged 또는 dirty tree를 허용합니다. Official mode에서는 이 flag를 빼야 하며, 정확한 version tag의 완전히 clean한 worktree와 비어 있는 output directory가 필요합니다.

CI action은 모두 전체 commit SHA로 고정됩니다. Ubuntu와 Windows에서 test와 syntax check를 실행하고, release text 전체의 secret 및 release debris를 검사하고, frozen lock을 확인하고, deterministic ZIP을 두 번 재현하여 모든 `SHA256SUMS` 항목을 검증합니다. Linux/Windows installer job은 명시적으로 dummy라고 표시한 임시 값만 사용합니다. Shell-significant path에서 두 번 설치하고 `doctor`, start/status/stop, update pointer 변경, private permission/ACL, lock 동작, stale live-PID 거부를 검사합니다. 모든 gate를 통과한 exact `v*` tag push에서는 [`.github/workflows/release.yml`](.github/workflows/release.yml)의 least-privilege OIDC job이 clean tag에서 official asset을 만들고, 모든 asset에 GitHub build-provenance attestation을 생성하고 release를 만듭니다. 어떤 CI job도 repository secret의 provider credential을 읽거나 요구하지 않습니다.

## 라이선스

[MIT](LICENSE)
