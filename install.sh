#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Shawn1565/antigravity-context-card.git"
PLUGIN_DIR="${HOME}/.gemini/config/plugins/context-card"
INSTALL_CLI=0
CLI_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --cli)
      INSTALL_CLI=1
      ;;
    *)
      CLI_ARGS+=("$arg")
      ;;
  esac
done

echo "==> Installing Antigravity Context Card into ${PLUGIN_DIR}"

if ! command -v git >/dev/null 2>&1; then
  echo "[ERROR] git is required. Please install git and re-run." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 (3.10+) is required. Please install Python 3.10+ and re-run." >&2
  exit 1
fi

mkdir -p "$(dirname "${PLUGIN_DIR}")"

if [ -d "${PLUGIN_DIR}/.git" ]; then
  echo "==> Existing installation found; updating via git pull --ff-only..."
  git -C "${PLUGIN_DIR}" pull --ff-only
elif [ -e "${PLUGIN_DIR}" ]; then
  echo "[ERROR] ${PLUGIN_DIR} already exists and is not a git repo. Move or remove it first." >&2
  exit 1
else
  git clone "${REPO_URL}" "${PLUGIN_DIR}"
fi

chmod +x \
  "${PLUGIN_DIR}/server.py" \
  "${PLUGIN_DIR}/cli_statusline.py" \
  "${PLUGIN_DIR}/cli_setup.py" \
  "${PLUGIN_DIR}/tmux_fill.py" \
  "${PLUGIN_DIR}/hooks/pre_invocation.py" \
  "${PLUGIN_DIR}/hooks/stop_hook.py" 2>/dev/null || true

if python3 -c "import mcp" >/dev/null 2>&1; then
  echo "==> Python MCP SDK already installed."
else
  echo "==> Installing Python MCP SDK (mcp>=1.2.0) for Desktop/IDE side panel..."
  if python3 -m pip install --user "mcp>=1.2.0" >/dev/null 2>&1; then
    echo "==> Installed mcp>=1.2.0."
  else
    echo "[WARN] Could not auto-install 'mcp>=1.2.0' (e.g., PEP 668 externally-managed Python)."
    echo "       For Desktop/IDE side panel, install it manually:"
    echo "         pip install \"mcp>=1.2.0\"   # or: pip install --user --break-system-packages \"mcp>=1.2.0\""
    echo "       (Antigravity CLI statusLine works with pure stdlib and does not need mcp.)"
  fi
fi

if [ "${INSTALL_CLI}" -eq 1 ]; then
  echo "==> Configuring Antigravity CLI statusLine & tmux bindings..."
  python3 "${PLUGIN_DIR}/cli_setup.py" install "${CLI_ARGS[@]}"
fi

echo ""
echo "[OK] Antigravity Context Card is installed at ${PLUGIN_DIR}"
echo "  • Desktop 2.0 / IDE: Open a chat or click '+ -> Context' in the right side panel."
if [ "${INSTALL_CLI}" -eq 0 ]; then
  echo "  • Antigravity CLI (agy) & tmux (optional):"
  echo "      python3 ${PLUGIN_DIR}/cli_setup.py install"
fi
