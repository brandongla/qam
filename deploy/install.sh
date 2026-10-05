#!/usr/bin/env bash
# Install / upgrade QAM recorders on a Linux VM (arm64 or x86_64). Run from a repo checkout:
#
#   sudo ./deploy/install.sh            # install or upgrade, then (re)start recorders
#   sudo ./deploy/install.sh --smoke    # also run a 60s live smoke test per venue first
#
# Layout: code venv /opt/qam/venv, config /etc/qam/recorder.yaml, data /srv/qam/data,
# services qam-recorder@<venue>. Idempotent: safe to re-run for upgrades.
set -euo pipefail

VENUES=(hyperliquid lighter)
PYTHON_VERSION="3.12"
QAM_USER=qam
PREFIX=/opt/qam
CONF_DIR=/etc/qam
DATA_ROOT=/srv/qam/data
SMOKE=0
[[ "${1:-}" == "--smoke" ]] && SMOKE=1

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
echo "arch: $(uname -m)   repo: $REPO_DIR"

log "system user and directories"
id -u "$QAM_USER" >/dev/null 2>&1 || useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$QAM_USER"
install -d -o "$QAM_USER" -g "$QAM_USER" -m 0750 "$PREFIX" /srv/qam "$DATA_ROOT"
install -d -m 0755 "$CONF_DIR"

log "uv (Python toolchain manager)"
if ! command -v uv >/dev/null 2>&1; then
  command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi
UV="$(command -v uv)"

log "Python $PYTHON_VERSION venv + qam package"
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
"$UV" python install "$PYTHON_VERSION"
[[ -x "$PREFIX/venv/bin/python" ]] || "$UV" venv --python "$PYTHON_VERSION" "$PREFIX/venv"
"$UV" pip install --python "$PREFIX/venv/bin/python" --reinstall-package qam "$REPO_DIR"
chown -R "$QAM_USER:$QAM_USER" "$PREFIX"
"$PREFIX/venv/bin/qam" --help >/dev/null

log "config"
if [[ ! -f "$CONF_DIR/recorder.yaml" ]]; then
  install -m 0644 "$REPO_DIR/deploy/config/recorder.yaml" "$CONF_DIR/recorder.yaml"
  echo "installed default config at $CONF_DIR/recorder.yaml"
else
  echo "keeping existing $CONF_DIR/recorder.yaml (compare with deploy/config/recorder.yaml after upgrades)"
fi

if [[ $SMOKE -eq 1 ]]; then
  for v in "${VENUES[@]}"; do
    log "smoke test: $v (60s, scratch directory)"
    sudo -u "$QAM_USER" "$PREFIX/venv/bin/qam" record smoke --venue "$v" --seconds 60 \
      --config "$CONF_DIR/recorder.yaml" --data-root "$(sudo -u "$QAM_USER" mktemp -d)" || {
        echo "smoke test FAILED for $v. Not (re)starting services." >&2; exit 1; }
  done
fi

log "systemd services"
install -m 0644 "$REPO_DIR/deploy/systemd/qam-recorder@.service" /etc/systemd/system/
systemctl daemon-reload
for v in "${VENUES[@]}"; do
  systemctl enable "qam-recorder@$v" >/dev/null
  systemctl restart "qam-recorder@$v"
done
sleep 20
for v in "${VENUES[@]}"; do systemctl --no-pager --lines=0 status "qam-recorder@$v" | head -3; done

log "health"
sudo -u "$QAM_USER" "$PREFIX/venv/bin/qam" record status --data-root "$DATA_ROOT" || \
  echo "(some connections may still be subscribing. Re-check in a minute with: qam record status)"
echo
echo "Logs:   journalctl -u 'qam-recorder@*' -f"
echo "Health: sudo -u $QAM_USER $PREFIX/venv/bin/qam record status --data-root $DATA_ROOT"
