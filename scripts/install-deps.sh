#!/usr/bin/env bash
# Optional extras for AI Council on Pop!_OS / Ubuntu 24.04.
#
# The application itself needs NONE of this - it is pure Python 3 standard
# library and runs with `./run.sh` on a stock Pop!_OS install. This script
# installs the surrounding developer tooling:
#
#   * the two agent CLIs the pipeline drives (codex, claude)  <- the important bit
#   * Node.js, which those CLIs are distributed through
#   * git tooling (gh) for pushing this repo to GitHub
#   * VS Code, if you want it
#   * python3-pip / python3-venv, absent from this machine's base image
#
# It is interactive by design: it uses sudo, and you should read what it runs.

set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
RED=$'\033[31m'; RESET=$'\033[0m'

say()  { echo "${BOLD}==>${RESET} $*"; }
warn() { echo "${YELLOW}[warn]${RESET} $*"; }
ok()   { echo "${GREEN}  ok${RESET} $*"; }
bad()  { echo "${RED} miss${RESET} $*"; }

have() { command -v "$1" >/dev/null 2>&1; }

WANT_ALL=1
WANT_VSCODE=0
for arg in "$@"; do
  case "$arg" in
    --vscode)   WANT_VSCODE=1 ;;
    --check)    WANT_ALL=0 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      echo
      echo "Usage: $0 [--check] [--vscode]"
      echo "  --check   report what is present, install nothing"
      echo "  --vscode  also install Visual Studio Code"
      exit 0 ;;
    *) warn "unknown option: $arg" ;;
  esac
done

# --------------------------------------------------------------------------
say "Current state"
# --------------------------------------------------------------------------
for tool in python3 git node npm gh code claude codex; do
  if have "$tool"; then
    ok "$(printf '%-8s' "$tool") $(command -v "$tool")"
  else
    bad "$(printf '%-8s' "$tool") not found"
  fi
done
echo

if [[ "$WANT_ALL" -eq 0 ]]; then
  exit 0
fi

if ! have sudo; then
  echo "${RED}error:${RESET} sudo is required to install system packages." >&2
  exit 1
fi

echo "${DIM}You will be prompted for your password. Ctrl-C to abort.${RESET}"
echo

# --------------------------------------------------------------------------
say "APT packages"
# --------------------------------------------------------------------------
APT_PKGS=(git curl ca-certificates python3-pip python3-venv python3-tk)
sudo apt-get update
sudo apt-get install -y "${APT_PKGS[@]}"

# --------------------------------------------------------------------------
say "Node.js 22 LTS (NodeSource)"
# --------------------------------------------------------------------------
# The agent CLIs ship as npm packages, so Node is a prerequisite for them
# rather than for this app.
if have node && [[ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -ge 20 ]]; then
  ok "node $(node -v) already satisfies the requirement"
else
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

# Install global npm packages without sudo by giving npm a user-owned prefix.
# This avoids the classic root-owned ~/.npm permission mess.
NPM_PREFIX="$HOME/.npm-global"
mkdir -p "$NPM_PREFIX"
npm config set prefix "$NPM_PREFIX"
export PATH="$NPM_PREFIX/bin:$PATH"

SHELL_RC="$HOME/.bashrc"
[[ -n "${ZSH_VERSION:-}" ]] && SHELL_RC="$HOME/.zshrc"
if ! grep -qs 'npm-global/bin' "$SHELL_RC"; then
  echo '' >> "$SHELL_RC"
  echo '# added by ai-council/scripts/install-deps.sh' >> "$SHELL_RC"
  echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$SHELL_RC"
  ok "added ~/.npm-global/bin to PATH in $SHELL_RC"
fi

# --------------------------------------------------------------------------
say "Agent CLIs"
# --------------------------------------------------------------------------
# These are what make the pipeline free at the point of use: each authenticates
# against your existing subscription rather than a metered API key.
if have claude; then
  ok "claude already installed: $(command -v claude)"
else
  npm install -g @anthropic-ai/claude-code || warn "claude CLI install failed"
fi

if have codex; then
  ok "codex already installed: $(command -v codex)"
else
  npm install -g @openai/codex || warn "codex CLI install failed"
fi

# --------------------------------------------------------------------------
say "GitHub CLI"
# --------------------------------------------------------------------------
if have gh; then
  ok "gh already installed"
else
  sudo mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y gh
fi

# --------------------------------------------------------------------------
if [[ "$WANT_VSCODE" -eq 1 ]]; then
  say "Visual Studio Code"
  if have code; then
    ok "code already installed"
  else
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | sudo gpg --dearmor -o /etc/apt/keyrings/packages.microsoft.gpg
    echo "deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main" \
      | sudo tee /etc/apt/sources.list.d/vscode.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y code
  fi
  if have code; then
    for ext in ms-python.python ms-python.vscode-pylance eamodio.gitlens \
               esbenp.prettier-vscode; do
      code --install-extension "$ext" --force >/dev/null 2>&1 \
        && ok "extension $ext" || warn "extension $ext failed"
    done
  fi
fi

# --------------------------------------------------------------------------
say "Done"
# --------------------------------------------------------------------------
cat <<'EOF'

Next steps:

  1. Reload your shell so the npm PATH change takes effect:
       source ~/.bashrc

  2. Authenticate each CLI against your subscription (one time, interactive):
       claude          # then follow the browser login for Claude Pro
       codex login     # then follow the browser login for ChatGPT Plus/Pro

     No API keys are involved. AI Council reads no keys and stores none.

  3. Confirm AI Council can see both CLIs:
       ./run.sh --doctor

  4. Launch:
       ./run.sh

EOF
