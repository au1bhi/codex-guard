#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
SERVICE_DIR="${HOME}/.config/systemd/user"

echo "=== Installing Codex Guard ==="

# 1. Install binary
mkdir -p "${BIN_DIR}"
cp "${DIR}/bin/codex-guard" "${BIN_DIR}/codex-guard"
chmod +x "${BIN_DIR}/codex-guard"
ln -sf "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-quota"
ln -sf "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-top"
ln -sf "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-stop"
ln -sf "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-kill"
echo "✓ Installed codex-guard, codex-quota, codex-top, and codex-stop to ${BIN_DIR}"

# 2. Install systemd service
mkdir -p "${SERVICE_DIR}"
sed "s|%h|${HOME}|g; s|%U|$(id -u)|g" "${DIR}/systemd/codex-quota-guard.service" > "${SERVICE_DIR}/codex-quota-guard.service"
systemctl --user daemon-reload
systemctl --user enable --now codex-quota-guard.service
echo "✓ Installed and started systemd user service: codex-quota-guard.service"

echo ""
echo "=== Installation Complete ==="
echo "Check quota status with: codex-quota"
echo "Service status:         systemctl --user status codex-quota-guard"
