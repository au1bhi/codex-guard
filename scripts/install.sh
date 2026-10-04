#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
LIB_DIR="${HOME}/.local/lib/codex-guard"
SERVICE_DIR="${HOME}/.config/systemd/user"

echo "=== Installing Codex Guard ==="

# 1. Install binary
mkdir -p "${BIN_DIR}" "${LIB_DIR}"
STAGING_FILE="$(mktemp "${LIB_DIR}/.codex-guard.XXXXXX")"
trap 'rm -f "${STAGING_FILE}"' EXIT
install -m 0755 "${DIR}/bin/codex-guard" "${STAGING_FILE}"
mv -fT "${STAGING_FILE}" "${LIB_DIR}/codex-guard"
# Only the three public commands live on PATH; the service executable is private.
ln -sf "${LIB_DIR}/codex-guard" "${BIN_DIR}/codexguard-start"
ln -sf "${LIB_DIR}/codex-guard" "${BIN_DIR}/codexguard-stop"
ln -sf "${LIB_DIR}/codex-guard" "${BIN_DIR}/codex-top"
rm -f "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-quota" "${BIN_DIR}/codex-stop" "${BIN_DIR}/codex-kill" "${BIN_DIR}/codex-fix-mouse"
echo "✓ Installed codexguard-start, codexguard-stop and codex-top"

# 2. Install systemd service
mkdir -p "${SERVICE_DIR}"
# Keep systemd specifiers; shell substitution breaks homes containing &, | or %.
install -m 0644 "${DIR}/systemd/codex-quota-guard.service" "${SERVICE_DIR}/codex-quota-guard.service"
systemctl --user daemon-reload
systemctl --user enable codex-quota-guard.service
systemctl --user restart codex-quota-guard.service
echo "✓ Installed and started systemd user service: codex-quota-guard.service"

echo ""
echo "=== Installation Complete ==="
echo "Start guard: codexguard-start"
echo "Stop guard:  codexguard-stop"
echo "Dashboard:   codex-top"
