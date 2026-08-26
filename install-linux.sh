#!/bin/bash
set -euo pipefail
umask 077
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

APP_NAME="ica-litellm-key-router"
REPO_SLUG="sehoon123/ica-litellm-key-router"
SOURCE_REF="${ICA_ROUTER_REF:-v0.2.1}"
LITELLM_VERSION="1.98.0"
PYTHON_VERSION="3.12.13"
UV_VERSION="0.12.2"
UV_INSTALLER_SHA256="58bee0cf8814385d5ffc2324e06d63db29de35ce8e740b9cadd2529bff929f50"
INSTALL_ROOT="${ICA_ROUTER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME}"
STATE_DIR="$INSTALL_ROOT/state"
RELEASES_DIR="$INSTALL_ROOT/releases"
CURRENT_LINK="$INSTALL_ROOT/current"
TOOLS_DIR="$INSTALL_ROOT/tools/uv-$UV_VERSION"
UV_BIN="$TOOLS_DIR/uv"
TEMP_SOURCE_DIR=""
SWITCHED=0
OLD_STOPPED=0
INSTALL_SUCCESS=0
OLD_RELEASE=""
LOCAL_SOURCE=0
RELEASE_DIR=""
ROLLBACK_DIR=""
RELEASE_PREEXISTED=0
INSTALL_LOCK_HELD=0
FORCE_INSTALL=0
REPLACE_KEYS=0
SYSTEMD_USER=0
SYSTEMD_UNIT_EXISTED=0
SYSTEMD_WAS_ENABLED=0
SYSTEMD_WAS_ACTIVE=0
OLD_ROUTER_WAS_RUNNING=0
MODEL_CLIENTS=()

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./install-linux.sh [options]

With no options:
  - first run: install LiteLLM, prompt for each pool's API keys until blank,
    generate configuration, and start the local router;
  - later runs: reuse saved keys/configuration and only ensure the router is running.

Options:
  --pi-models          Create or merge ~/.pi/agent/models.json.
  --prime-models       Create or merge ~/.prime/agent/models.json.
  --models-json PATH   Create or merge a custom models.json (repeatable).
  --replace-keys       Prompt for all API keys again, then restart the router.
  --force-install      Reinstall/update even when a valid installation exists.
  --systemd-user       Enable and start the managed systemd user service.
  -h, --help           Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --pi-models)
      MODEL_CLIENTS+=("$HOME/.pi/agent/models.json")
      shift
      ;;
    --prime-models)
      MODEL_CLIENTS+=("$HOME/.prime/agent/models.json")
      shift
      ;;
    --models-json)
      (($# >= 2)) || die "--models-json requires a path"
      MODEL_CLIENTS+=("$2")
      shift 2
      ;;
    --replace-keys)
      REPLACE_KEYS=1
      shift
      ;;
    --force-install)
      FORCE_INSTALL=1
      shift
      ;;
    --systemd-user)
      SYSTEMD_USER=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (use --help)"
      ;;
  esac
done

if ((${#MODEL_CLIENTS[@]} > 0)); then
  for client_path in "${MODEL_CLIENTS[@]}"; do
    [[ -n "$client_path" ]] || die "models.json path must not be empty"
    [[ "$client_path" != *$'\n'* && "$client_path" != *$'\r'* ]] \
      || die "models.json path contains a control character"
  done
fi

cleanup() {
  local status=$?
  set +e
  if [[ "$SWITCHED" == "1" || "$OLD_STOPPED" == "1" ]] && declare -F rollback >/dev/null 2>&1; then
    rollback
  fi
  if [[ "$INSTALL_SUCCESS" != "1" && "$RELEASE_PREEXISTED" != "1" && -n "$RELEASE_DIR" && -d "$RELEASE_DIR" ]]; then
    local selected=""
    if [[ -L "$CURRENT_LINK" ]]; then selected="$(cd -- "$CURRENT_LINK" 2>/dev/null && pwd -P)"; fi
    if [[ "$selected" != "$RELEASE_DIR" ]]; then rm -rf -- "$RELEASE_DIR"; fi
  fi
  if [[ -n "$ROLLBACK_DIR" && -d "$ROLLBACK_DIR" ]]; then rm -rf -- "$ROLLBACK_DIR"; fi
  if [[ -n "$TEMP_SOURCE_DIR" && -d "$TEMP_SOURCE_DIR" ]]; then rm -rf -- "$TEMP_SOURCE_DIR"; fi
  if [[ "$INSTALL_LOCK_HELD" == "1" ]] \
    && [[ "$(cat "$INSTALL_ROOT/.install.lock/pid" 2>/dev/null)" == "$$" ]]; then
    rm -f -- "$INSTALL_ROOT/.install.lock/pid"
    rmdir -- "$INSTALL_ROOT/.install.lock" 2>/dev/null
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

[[ "$SOURCE_REF" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "ICA_ROUTER_REF must be an exact vMAJOR.MINOR.PATCH tag"
[[ ! -L "$INSTALL_ROOT" ]] || die "install root must not be a symlink: $INSTALL_ROOT"
for private_path in "$STATE_DIR" "$RELEASES_DIR" "$INSTALL_ROOT/tools" "$INSTALL_ROOT/cache"; do
  [[ ! -L "$private_path" ]] || die "private install path must not be a symlink: $private_path"
done
mkdir -p -- "$INSTALL_ROOT" "$STATE_DIR" "$RELEASES_DIR" "$TOOLS_DIR"
RELEASES_DIR="$(cd -- "$RELEASES_DIR" && pwd -P)"
chmod 700 "$INSTALL_ROOT" "$STATE_DIR" "$RELEASES_DIR" "$TOOLS_DIR" 2>/dev/null || true
if ! mkdir -m 700 -- "$INSTALL_ROOT/.install.lock" 2>/dev/null; then
  die "another installer is running (remove $INSTALL_ROOT/.install.lock only if it is stale)"
fi
INSTALL_LOCK_HELD=1
printf '%s\n' "$$" > "$INSTALL_ROOT/.install.lock/pid"
[[ ! -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]] || die "current pointer must be a symlink"

WRAPPER="$INSTALL_ROOT/ica-router"

configure_requested_clients() {
  ((${#MODEL_CLIENTS[@]} > 0)) || return 0
  local client
  local client_args=()
  for client in "${MODEL_CLIENTS[@]}"; do
    client_args+=(--client "$client")
  done
  "$WRAPPER" configure-clients "${client_args[@]}"
}

print_models_hint() {
  if ((${#MODEL_CLIENTS[@]} == 0)); then
    say "Pi models.json was not modified."
    say "  During install: $0 --pi-models"
    say "  Or without stopping the router:"
    say "    $WRAPPER configure-harnesses --pi"
  fi
}

SYSTEMD_UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ica-litellm-key-router.service"
if [[ -f "$SYSTEMD_UNIT_PATH" && ! -L "$SYSTEMD_UNIT_PATH" ]]; then
  SYSTEMD_UNIT_EXISTED=1
fi
if [[ "$SYSTEMD_UNIT_EXISTED" == "1" && -x /usr/bin/systemctl ]]; then
  if /usr/bin/systemctl --user is-enabled ica-litellm-key-router.service >/dev/null 2>&1; then
    SYSTEMD_WAS_ENABLED=1
    SYSTEMD_USER=1
  fi
  if /usr/bin/systemctl --user is-active ica-litellm-key-router.service >/dev/null 2>&1; then
    SYSTEMD_WAS_ACTIVE=1
  fi
fi

start_router() {
  if [[ "$SYSTEMD_USER" == "1" ]]; then
    "$WRAPPER" install-systemd-user
  else
    "$WRAPPER" start
  fi
}

EXISTING_READY=0
if [[ "$FORCE_INSTALL" == "0" ]] &&
  [[ -s "$STATE_DIR/secrets.json" ]] &&
  [[ -x "$WRAPPER" ]] &&
  [[ -L "$CURRENT_LINK" ]]; then
  CURRENT_RELEASE="$(cd -- "$CURRENT_LINK" 2>/dev/null && pwd -P || true)"
  case "$CURRENT_RELEASE" in
    "$RELEASES_DIR"/*)
      if [[ -f "$CURRENT_RELEASE/.complete" ]] && "$WRAPPER" doctor >/dev/null 2>&1; then
        EXISTING_READY=1
        INSTALLER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
        for source_relative in catalog.json tools/routerctl.py; do
          if [[ -f "$INSTALLER_DIR/$source_relative" ]] &&
            [[ -f "$CURRENT_RELEASE/app/$source_relative" ]] &&
            [[ ! "$INSTALLER_DIR/$source_relative" -ef "$CURRENT_RELEASE/app/$source_relative" ]] &&
            ! cmp -s -- "$INSTALLER_DIR/$source_relative" "$CURRENT_RELEASE/app/$source_relative"; then
            say "Router source changed since the selected release; performing an update."
            EXISTING_READY=0
            break
          fi
        done
      fi
      ;;
  esac
fi

if [[ "$EXISTING_READY" == "1" ]]; then
  if ((${#MODEL_CLIENTS[@]} > 0)); then
    # Complete all parent preparation before stopping a healthy router.
    for client in "${MODEL_CLIENTS[@]}"; do
      mkdir -p -- "$(dirname -- "$client")"
    done
  fi
  if [[ "$REPLACE_KEYS" == "1" ]]; then
    [[ -r /dev/tty ]] || die "--replace-keys requires an interactive terminal"
    say "Replacing saved Services Essentials keys. Submit an empty value after the last key."
    "$WRAPPER" stop >/dev/null 2>&1 || die "could not safely stop the existing router"
    bootstrap_args=(bootstrap --replace-secrets --prompt-keys)
    if ((${#MODEL_CLIENTS[@]} > 0)); then
      for client in "${MODEL_CLIENTS[@]}"; do
        bootstrap_args+=(--client "$client")
      done
    else
      bootstrap_args+=(--no-configure-clients)
    fi
    if ! "$WRAPPER" "${bootstrap_args[@]}" </dev/tty; then
      start_router >/dev/null 2>&1 \
        || say "WARNING: router could not be restarted after key replacement failed" >&2
      die "key replacement failed"
    fi
  elif ((${#MODEL_CLIENTS[@]} > 0)); then
    "$WRAPPER" stop >/dev/null 2>&1 || die "could not safely stop the existing router"
    if ! configure_requested_clients; then
      start_router >/dev/null 2>&1 \
        || say "WARNING: router could not be restarted after client configuration failed" >&2
      die "client configuration failed"
    fi
  else
    say "Saved ICA keys and a valid LiteLLM configuration already exist."
    say "Skipping installation and ensuring LiteLLM is running."
  fi

  start_router
  "$WRAPPER" status
  print_models_hint
  INSTALL_SUCCESS=1
  exit 0
fi

fetch() {
  local url="$1" output="$2"
  local curl_bin=""
  for candidate in /usr/bin/curl /bin/curl; do
    if [[ -x "$candidate" ]]; then curl_bin="$candidate"; break; fi
  done
  [[ -n "$curl_bin" ]] || die "system curl is required"
  "$curl_bin" --fail --silent --show-error --location \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 300 --max-redirs 5 --retry 3 \
    "$url" -o "$output"
}

sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    die "sha256sum or shasum is required"
  fi
}

uv_version_is_exact() {
  [[ -f "$UV_BIN" && -x "$UV_BIN" && ! -L "$UV_BIN" ]] || return 1
  local output product version remainder
  output="$("$UV_BIN" --version 2>/dev/null)" || return 1
  read -r product version remainder <<<"$output"
  [[ "$product" == "uv" && "$version" == "$UV_VERSION" ]]
}

install_uv() {
  if uv_version_is_exact; then
    return
  fi
  [[ ! -L "$UV_BIN" ]] || die "private uv binary must not be a symlink: $UV_BIN"
  local installer="$INSTALL_ROOT/.uv-install-$UV_VERSION.sh" actual
  say "Installing verified uv $UV_VERSION ..."
  fetch "https://astral.sh/uv/$UV_VERSION/install.sh" "$installer"
  actual="$(sha256_file "$installer")"
  [[ "$actual" == "$UV_INSTALLER_SHA256" ]] || die "uv installer SHA-256 mismatch"
  chmod 700 "$installer"
  env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" TMPDIR="${TMPDIR:-/tmp}" \
    UV_UNMANAGED_INSTALL="$TOOLS_DIR" UV_NO_MODIFY_PATH=1 UV_DISABLE_UPDATE=1 \
    /bin/sh "$installer"
  rm -f -- "$installer"
  [[ -f "$UV_BIN" && -x "$UV_BIN" && ! -L "$UV_BIN" ]] || die "verified uv installer did not create a regular private binary"
  uv_version_is_exact || die "uv version mismatch"
}

run_uv() {
  local project_environment="${UV_PROJECT_ENVIRONMENT:-}"
  local -a clean_env
  clean_env=(
    HOME="$HOME"
    PATH="/usr/bin:/bin:/usr/sbin:/sbin"
    TMPDIR="${TMPDIR:-/tmp}"
    UV_CACHE_DIR="$INSTALL_ROOT/cache/uv"
    UV_DEFAULT_INDEX="https://pypi.org/simple"
    UV_PYTHON_PREFERENCE="only-managed"
  )
  if [[ -n "$project_environment" ]]; then
    clean_env+=(UV_PROJECT_ENVIRONMENT="$project_environment")
  fi
  env -i "${clean_env[@]}" "$UV_BIN" --no-config "$@"
}

install_uv
run_uv python install "$PYTHON_VERSION"
MANAGED_PYTHON="$(run_uv python find "$PYTHON_VERSION")"
[[ -x "$MANAGED_PYTHON" ]] || die "uv-managed Python 3.12 was not found"

safe_extract_zip() {
  local archive="$1" destination="$2" expected_top="$3" extractor
  extractor="$INSTALL_ROOT/.safe-extract-$$.py"
  cat >"$extractor" <<'PY'
from pathlib import Path, PurePosixPath
import shutil, stat, sys, zipfile
archive, root, expected = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
root.mkdir(mode=0o700, parents=True, exist_ok=False)
seen = set(); total = 0
with zipfile.ZipFile(archive) as zf:
    infos = zf.infolist()
    if not infos or len(infos) > 2000:
        raise SystemExit("unsafe ZIP member count")
    for info in infos:
        raw = info.filename
        if not raw or "\\" in raw or "\x00" in raw:
            raise SystemExit("unsafe ZIP member name")
        parts = PurePosixPath(raw).parts
        if not parts or parts[0] != expected or any(p in {"", ".", ".."} for p in parts):
            raise SystemExit("ZIP member escaped the expected top-level directory")
        folded = raw.casefold()
        if folded in seen:
            raise SystemExit("duplicate or case-colliding ZIP member")
        seen.add(folded)
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise SystemExit("ZIP links and special files are forbidden")
        total += info.file_size
        if total > 128 * 1024 * 1024:
            raise SystemExit("ZIP expanded size is too large")
    for info in infos:
        target = root.joinpath(*PurePosixPath(info.filename).parts)
        if info.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with zf.open(info) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o600)
PY
  chmod 700 "$extractor"
  "$MANAGED_PYTHON" -I "$extractor" "$archive" "$destination" "$expected_top"
  rm -f -- "$extractor"
}

resolve_source() {
  if [[ -n "${ICA_ROUTER_SOURCE_DIR:-}" ]]; then
    SOURCE_DIR="$(cd -- "$ICA_ROUTER_SOURCE_DIR" && pwd -P)"
    LOCAL_SOURCE=1
  else
    local here asset checksum line digest filename extra actual top extract_root
    here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
    if [[ -f "$here/catalog.json" && -f "$here/tools/routerctl.py" ]]; then
      SOURCE_DIR="$here"
      LOCAL_SOURCE=1
    else
      TEMP_SOURCE_DIR="$INSTALL_ROOT/.download-$$"
      mkdir -m 700 -- "$TEMP_SOURCE_DIR"
      asset="$APP_NAME-$SOURCE_REF.zip"
      checksum="$asset.sha256"
      say "Downloading verified $REPO_SLUG@$SOURCE_REF ..."
      fetch "https://github.com/$REPO_SLUG/releases/download/$SOURCE_REF/$asset" "$TEMP_SOURCE_DIR/$asset"
      fetch "https://github.com/$REPO_SLUG/releases/download/$SOURCE_REF/$checksum" "$TEMP_SOURCE_DIR/$checksum"
      [[ "$(wc -l < "$TEMP_SOURCE_DIR/$checksum" | tr -d ' ')" == "1" ]] || die "invalid checksum manifest"
      IFS=' ' read -r digest filename extra < "$TEMP_SOURCE_DIR/$checksum"
      [[ "$digest" =~ ^[0-9a-f]{64}$ && "$filename" == "$asset" && -z "${extra:-}" ]] || die "invalid checksum manifest"
      actual="$(sha256_file "$TEMP_SOURCE_DIR/$asset")"
      [[ "$actual" == "$digest" ]] || die "release asset SHA-256 mismatch"
      top="$APP_NAME-$SOURCE_REF"
      extract_root="$TEMP_SOURCE_DIR/extracted"
      safe_extract_zip "$TEMP_SOURCE_DIR/$asset" "$extract_root" "$top"
      SOURCE_DIR="$extract_root/$top"
    fi
  fi
  [[ -f "$SOURCE_DIR/catalog.json" && -f "$SOURCE_DIR/tools/routerctl.py" ]] || die "source is incomplete"
  [[ "$(tr -d '\r\n' < "$SOURCE_DIR/VERSION")" == "${SOURCE_REF#v}" ]] || die "source VERSION does not match $SOURCE_REF"
}

resolve_source
if [[ "$LOCAL_SOURCE" == "1" ]]; then
  RELEASE_ID="$SOURCE_REF-local-$(date +%Y%m%d%H%M%S)-$$"
else
  RELEASE_ID="$SOURCE_REF"
fi
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
APP_DIR="$RELEASE_DIR/app"
VENV_DIR="$RELEASE_DIR/.venv"

REUSE_RELEASE=0
SELECTED_RELEASE_AT_START=""
if [[ -L "$CURRENT_LINK" ]]; then
  SELECTED_RELEASE_AT_START="$(cd -- "$CURRENT_LINK" 2>/dev/null && pwd -P || true)"
fi
if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" ]]; then
  [[ "$LOCAL_SOURCE" != "1" ]] || die "unexpected local release collision: $RELEASE_DIR"
  [[ -d "$RELEASE_DIR" && ! -L "$RELEASE_DIR" ]] || die "unsafe release path: $RELEASE_DIR"
  if [[ -f "$RELEASE_DIR/.complete" ]]     && [[ "$(tr -d '\r\n' < "$RELEASE_DIR/.complete")" == "$SOURCE_REF" ]]     && [[ -x "$VENV_DIR/bin/python" && -f "$APP_DIR/tools/routerctl.py" ]]; then
    REUSE_RELEASE=1
    RELEASE_PREEXISTED=1
  else
    [[ "$SELECTED_RELEASE_AT_START" != "$RELEASE_DIR" ]] \
      || die "selected release is incomplete; refusing to delete a live release"
    rm -rf -- "$RELEASE_DIR"
  fi
fi
if [[ "$REUSE_RELEASE" != "1" ]]; then
  mkdir -m 700 -- "$RELEASE_DIR" "$APP_DIR"
  for item in catalog.json tools examples scripts README.md README.ko.md SECURITY.md LICENSE VERSION .gitignore pyproject.toml uv.lock; do
    if [[ -e "$SOURCE_DIR/$item" ]]; then
      if [[ -L "$SOURCE_DIR/$item" ]] || find "$SOURCE_DIR/$item" -type l -print -quit 2>/dev/null | grep -q .; then
        die "source item contains a symlink: $item"
      fi
      cp -R -- "$SOURCE_DIR/$item" "$APP_DIR/"
    fi
  done
  [[ -f "$APP_DIR/tools/routerctl.py" && -f "$APP_DIR/uv.lock" ]] || die "staged release is incomplete"

  say "Installing locked LiteLLM $LITELLM_VERSION runtime ..."
  UV_PROJECT_ENVIRONMENT="$VENV_DIR" run_uv sync --frozen --no-dev --project "$APP_DIR" --python "$PYTHON_VERSION"
  run_uv pip check --python "$VENV_DIR/bin/python"
  INSTALLED_VERSION="$("$VENV_DIR/bin/python" -I -c 'from importlib.metadata import version; print(version("litellm"))')"
  [[ "$INSTALLED_VERSION" == "$LITELLM_VERSION" ]] || die "LiteLLM version mismatch: $INSTALLED_VERSION"
  printf '%s\n' "$SOURCE_REF" > "$RELEASE_DIR/.complete"
  chmod 600 "$RELEASE_DIR/.complete"
else
  INSTALLED_VERSION="$("$VENV_DIR/bin/python" -I -c 'from importlib.metadata import version; print(version("litellm"))')"
  [[ "$INSTALLED_VERSION" == "$LITELLM_VERSION" ]] || die "existing release has wrong LiteLLM version"
fi
INSTALLED_PYTHON_VERSION="$("$VENV_DIR/bin/python" -I -c 'import platform; print(platform.python_version())')"
[[ "$INSTALLED_PYTHON_VERSION" == "$PYTHON_VERSION" ]] || die "Python version mismatch: $INSTALLED_PYTHON_VERSION"

resolve_wrapper_root='SELF=$0
while [ -L "$SELF" ]; do
  DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd -P)
  LINK=$(readlink "$SELF")
  case "$LINK" in /*) SELF=$LINK ;; *) SELF=$DIR/$LINK ;; esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd -P)'
WRAPPER="$INSTALL_ROOT/ica-router"
cat >"$WRAPPER" <<EOF
#!/bin/sh
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
$resolve_wrapper_root
CURRENT="\$ROOT/current"
exec "\$CURRENT/.venv/bin/python" "\$CURRENT/app/tools/routerctl.py" --state-dir "\$ROOT/state" --catalog "\$CURRENT/app/catalog.json" --venv "\$CURRENT/.venv" "\$@"
EOF
chmod 700 "$WRAPPER"

if [[ -L "$CURRENT_LINK" || -d "$CURRENT_LINK" ]]; then
  OLD_RELEASE="$(cd -- "$CURRENT_LINK" 2>/dev/null && pwd -P || true)"
fi
if [[ -n "$OLD_RELEASE" ]]; then
  case "$OLD_RELEASE" in "$RELEASES_DIR"/*) ;; *) die "current points outside the managed releases directory" ;; esac
fi
OLD_PY=""; OLD_CONTROL=""; OLD_CATALOG=""; OLD_VENV=""
if [[ -n "$OLD_RELEASE" && -x "$OLD_RELEASE/.venv/bin/python" ]]; then
  OLD_PY="$OLD_RELEASE/.venv/bin/python"; OLD_CONTROL="$OLD_RELEASE/app/tools/routerctl.py"
  OLD_CATALOG="$OLD_RELEASE/app/catalog.json"; OLD_VENV="$OLD_RELEASE/.venv"
elif [[ -x "$INSTALL_ROOT/.venv/bin/python" && -f "$INSTALL_ROOT/app/tools/routerctl.py" ]]; then
  OLD_PY="$INSTALL_ROOT/.venv/bin/python"; OLD_CONTROL="$INSTALL_ROOT/app/tools/routerctl.py"
  OLD_CATALOG="$INSTALL_ROOT/app/catalog.json"; OLD_VENV="$INSTALL_ROOT/.venv"
fi
if [[ -n "$OLD_PY" && -f "$OLD_CONTROL" ]] \
  && "$OLD_PY" "$OLD_CONTROL" --state-dir "$STATE_DIR" --catalog "$OLD_CATALOG" --venv "$OLD_VENV" status >/dev/null 2>&1; then
  OLD_ROUTER_WAS_RUNNING=1
fi
ROLLBACK_DIR="$INSTALL_ROOT/.rollback-$$"
mkdir -m 700 -- "$ROLLBACK_DIR" "$ROLLBACK_DIR/state" "$ROLLBACK_DIR/clients"
if [[ "$SYSTEMD_UNIT_EXISTED" == "1" ]]; then
  [[ -f "$SYSTEMD_UNIT_PATH" && ! -L "$SYSTEMD_UNIT_PATH" ]] \
    || die "unsafe systemd user unit: $SYSTEMD_UNIT_PATH"
  cp -- "$SYSTEMD_UNIT_PATH" "$ROLLBACK_DIR/systemd.unit"
  chmod 600 "$ROLLBACK_DIR/systemd.unit"
fi
for name in secrets.json config.yaml client-models.generated.json runtime.json generation.json; do
  if [[ -e "$STATE_DIR/$name" || -L "$STATE_DIR/$name" ]]; then
    [[ -f "$STATE_DIR/$name" && ! -L "$STATE_DIR/$name" ]] || die "unsafe state file: $name"
    cp -- "$STATE_DIR/$name" "$ROLLBACK_DIR/state/$name"
    chmod 600 "$ROLLBACK_DIR/state/$name"
  fi
done
CLIENT_COUNT=0
if ((${#MODEL_CLIENTS[@]} > 0)); then
  for client_path in "${MODEL_CLIENTS[@]}"; do
    CLIENT_COUNT=$((CLIENT_COUNT + 1))
    printf '%s\n' "$client_path" > "$ROLLBACK_DIR/clients/$CLIENT_COUNT.path"
    if [[ -e "$client_path" || -L "$client_path" ]]; then
      [[ -f "$client_path" && ! -L "$client_path" ]] || die "unsafe client models file: $client_path"
      cp -- "$client_path" "$ROLLBACK_DIR/clients/$CLIENT_COUNT.data"
      chmod 600 "$ROLLBACK_DIR/clients/$CLIENT_COUNT.data"
    fi
  done
fi

rollback() {
  set +e
  "$VENV_DIR/bin/python" "$APP_DIR/tools/routerctl.py" --state-dir "$STATE_DIR" --catalog "$APP_DIR/catalog.json" --venv "$VENV_DIR" stop >/dev/null 2>&1
  rm -f -- "$CURRENT_LINK"
  if [[ -n "$OLD_RELEASE" ]]; then ln -s -- "$OLD_RELEASE" "$CURRENT_LINK"; fi
  for name in secrets.json config.yaml client-models.generated.json runtime.json generation.json; do
    if [[ -f "$ROLLBACK_DIR/state/$name" ]]; then
      cp -- "$ROLLBACK_DIR/state/$name" "$STATE_DIR/.$name.rollback.$$"
      chmod 600 "$STATE_DIR/.$name.rollback.$$"
      "$MANAGED_PYTHON" -I -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])'         "$STATE_DIR/.$name.rollback.$$" "$STATE_DIR/$name"
    else
      rm -f -- "$STATE_DIR/$name"
    fi
  done
  for ((index = 1; index <= CLIENT_COUNT; index++)); do
    client_path="$(cat "$ROLLBACK_DIR/clients/$index.path")"
    if [[ -f "$ROLLBACK_DIR/clients/$index.data" ]]; then
      mkdir -p -- "$(dirname -- "$client_path")"
      cp -- "$ROLLBACK_DIR/clients/$index.data" "$client_path.rollback.$$"
      chmod 600 "$client_path.rollback.$$"
      "$MANAGED_PYTHON" -I -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' \
        "$client_path.rollback.$$" "$client_path"
    else
      rm -f -- "$client_path"
    fi
  done
  if [[ ( "$SYSTEMD_USER" == "1" || "$SYSTEMD_UNIT_EXISTED" == "1" ) && -x /usr/bin/systemctl ]]; then
    /usr/bin/systemctl --user disable --now ica-litellm-key-router.service >/dev/null 2>&1
    if [[ "$SYSTEMD_UNIT_EXISTED" == "1" && -f "$ROLLBACK_DIR/systemd.unit" ]]; then
      mkdir -p -- "$(dirname -- "$SYSTEMD_UNIT_PATH")"
      cp -- "$ROLLBACK_DIR/systemd.unit" "$SYSTEMD_UNIT_PATH.rollback.$$"
      chmod 600 "$SYSTEMD_UNIT_PATH.rollback.$$"
      "$MANAGED_PYTHON" -I -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' \
        "$SYSTEMD_UNIT_PATH.rollback.$$" "$SYSTEMD_UNIT_PATH"
    else
      rm -f -- "$SYSTEMD_UNIT_PATH"
    fi
    /usr/bin/systemctl --user daemon-reload >/dev/null 2>&1
    if [[ "$SYSTEMD_WAS_ENABLED" == "1" ]]; then
      /usr/bin/systemctl --user enable ica-litellm-key-router.service >/dev/null 2>&1 \
        || say "WARNING: previous systemd enablement could not be restored" >&2
    fi
    if [[ "$SYSTEMD_WAS_ACTIVE" == "1" ]]; then
      /usr/bin/systemctl --user start ica-litellm-key-router.service >/dev/null 2>&1 \
        || say "WARNING: previous systemd router could not be restarted automatically" >&2
    elif [[ "$OLD_ROUTER_WAS_RUNNING" == "1" && -n "$OLD_PY" && -f "$OLD_CONTROL" ]]; then
      "$OLD_PY" "$OLD_CONTROL" --state-dir "$STATE_DIR" --catalog "$OLD_CATALOG" --venv "$OLD_VENV" start >/dev/null 2>&1 \
        || say "WARNING: previous router could not be restarted automatically" >&2
    fi
  elif [[ "$OLD_ROUTER_WAS_RUNNING" == "1" && -n "$OLD_PY" && -f "$OLD_CONTROL" ]]; then
    "$OLD_PY" "$OLD_CONTROL" --state-dir "$STATE_DIR" --catalog "$OLD_CATALOG" --venv "$OLD_VENV" start >/dev/null 2>&1 \
      || say "WARNING: previous router could not be restarted automatically" >&2
  fi
  SWITCHED=0
  OLD_STOPPED=0
}

if [[ -n "$OLD_PY" && -f "$OLD_CONTROL" ]]; then
  "$OLD_PY" "$OLD_CONTROL" --state-dir "$STATE_DIR" --catalog "$OLD_CATALOG" --venv "$OLD_VENV" stop >/dev/null 2>&1 \
    || die "could not safely stop the existing router"
  OLD_STOPPED=1
fi

[[ ! -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]] || die "current pointer must be a symlink"
CURRENT_TMP="$INSTALL_ROOT/.current.$$"
rm -f -- "$CURRENT_TMP"
ln -s -- "$RELEASE_DIR" "$CURRENT_TMP"
"$MANAGED_PYTHON" -I -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$CURRENT_TMP" "$CURRENT_LINK"
SWITCHED=1



BOOTSTRAP=("$VENV_DIR/bin/python" "$APP_DIR/tools/routerctl.py" \
  --state-dir "$STATE_DIR" --catalog "$APP_DIR/catalog.json" --venv "$VENV_DIR" \
  bootstrap)
if ((${#MODEL_CLIENTS[@]} > 0)); then
  for client in "${MODEL_CLIENTS[@]}"; do
    [[ -n "$client" ]] || die "models.json path must not be empty"
    mkdir -p -- "$(dirname -- "$client")"
    BOOTSTRAP+=(--client "$client")
  done
else
  BOOTSTRAP+=(--no-configure-clients)
fi
if [[ "$REPLACE_KEYS" == "1" && -z "${ICA_ROUTER_KEY_ROTATOR:-}" ]]; then
  BOOTSTRAP+=(--replace-secrets --prompt-keys)
elif [[ -n "${ICA_ROUTER_KEY_ROTATOR:-}" ]]; then
  BOOTSTRAP+=(--import-key-rotator "$ICA_ROUTER_KEY_ROTATOR")
  [[ "$REPLACE_KEYS" == "0" ]] || BOOTSTRAP+=(--replace-secrets)
else
  BOOTSTRAP+=(--import-key-rotator auto)
fi
USE_TTY=0
if [[ "${ICA_ROUTER_NON_INTERACTIVE:-0}" == "1" || ! -r /dev/tty ]]; then
  BOOTSTRAP+=(--non-interactive)
else
  USE_TTY=1
fi
if [[ "$USE_TTY" == "1" ]]; then
  if ! "${BOOTSTRAP[@]}" </dev/tty; then rollback; die "configuration failed; previous release restored"; fi
else
  if ! "${BOOTSTRAP[@]}"; then rollback; die "configuration failed; previous release restored"; fi
fi
if ! "$WRAPPER" doctor; then rollback; die "doctor failed; previous release restored"; fi
if ! start_router; then rollback; die "router failed to start; previous release restored"; fi
SWITCHED=0
OLD_STOPPED=0

BIN_DIR="$HOME/.local/bin"
mkdir -p -- "$BIN_DIR"
chmod 700 "$BIN_DIR" 2>/dev/null || true
if ln -sfn -- "$WRAPPER" "$BIN_DIR/ica-router" 2>/dev/null; then
  say "Command installed: $BIN_DIR/ica-router"
fi
INSTALL_SUCCESS=1

say ""
say "Installed successfully."
say "  Home:    $INSTALL_ROOT"
say "  Release: $RELEASE_ID"
say "  Status:  $WRAPPER status"
say "  Stop:    $WRAPPER stop"
if [[ "$SYSTEMD_USER" == "1" ]]; then
  say "  Service: systemctl --user status ica-litellm-key-router.service"
  say "  Start:   systemctl --user start ica-litellm-key-router.service"
else
  say "  Start:   $WRAPPER start"
fi
say "  Doctor:  $WRAPPER doctor"
print_models_hint
if ((${#MODEL_CLIENTS[@]} > 0)); then
  say "Restart Pi/prime-agent, then select a provider ending in '-router'."
fi
