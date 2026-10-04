#!/usr/bin/env bash
set -euo pipefail

SERVICE_FILE="${HOME}/.config/systemd/user/codex-quota-guard.service"
BIN_DIR="${HOME}/.local/bin"
LIB_DIR="${HOME}/.local/lib/codex-guard"

echo "=== Uninstalling Codex Guard ==="

# Stop and disable systemd service
if systemctl --user is-active --quiet codex-quota-guard 2>/dev/null; then
    systemctl --user stop codex-quota-guard
fi
if systemctl --user is-enabled --quiet codex-quota-guard 2>/dev/null; then
    systemctl --user disable codex-quota-guard
fi
if [ -f "${SERVICE_FILE}" ]; then
    rm -f "${SERVICE_FILE}"
    systemctl --user daemon-reload
fi
echo "✓ Stopped and removed systemd service"

# Remove binaries
rm -f "${BIN_DIR}/codexguard-start" "${BIN_DIR}/codexguard-stop" "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-quota" "${BIN_DIR}/codex-top" "${BIN_DIR}/codex-stop" "${BIN_DIR}/codex-kill" "${BIN_DIR}/codex-fix-mouse"
rm -f "${LIB_DIR}/codex-guard"
if [ -d "${LIB_DIR}" ]; then
    rmdir "${LIB_DIR}" 2>/dev/null || true
fi
echo "✓ Removed binaries from ${BIN_DIR}"

echo "=== Uninstallation Complete ==="
