#!/usr/bin/env bash
set -e

SERVICE_FILE="${HOME}/.config/systemd/user/codex-quota-guard.service"
BIN_DIR="${HOME}/.local/bin"

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
rm -f "${BIN_DIR}/codex-guard" "${BIN_DIR}/codex-quota" "${BIN_DIR}/codex-top"
echo "✓ Removed binaries from ${BIN_DIR}"

echo "=== Uninstallation Complete ==="
