#!/usr/bin/env bash
# Dependency installer for Theseus AI on Pop!_OS / Ubuntu 24.04.
#
# The application itself needs none of this - it is pure Python 3 standard
# library and runs with `./run.sh` on a stock install.
#
# By default this installs only the two agent CLIs the pipeline drives, using
# each vendor's first-party installer. Those drop a standalone binary into
# ~/.local/bin, so no Node, no npm and no sudo are involved.
#
# Google's Antigravity CLI (`agy`) is a third option the app can drive, behind
# --antigravity: it is a ~190 MB binary and not one of the defaults. Projects
# Mode does assign it the QA chair out of the box, so install it if you intend
# to use that tab - or reassign QA to codex or claude in the agent matrix.
#
# Everything else (gh, python3-pip/venv, VS Code) is optional, needs root, and
# is skipped unless you pass --extras or --vscode.
#
# These installers pipe a remote script to bash. They are the official sources,
# but read them first if you would rather not:
#   curl -fsSL https://claude.ai/install.sh              | less
#   curl -fsSL https://chatgpt.com/codex/install.sh      | less
#   curl -fsSL https://antigravity.google/cli/install.sh | less

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
WANT_EXTRAS=0
WANT_AGY=0
for arg in "$@"; do
  case "$arg" in
    --vscode)   WANT_VSCODE=1; WANT_EXTRAS=1 ;;
    --extras)   WANT_EXTRAS=1 ;;
    --antigravity) WANT_AGY=1 ;;
    --check)    WANT_ALL=0 ;;
    -h|--help)
      # Print the header comment block, stopping at the first non-comment line
      # so the help text cannot drift out of sync with the file again.
      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
      echo
      echo "Usage: $0 [--check] [--extras] [--vscode] [--antigravity]"
      echo "  (default) install the codex and claude CLIs only - no sudo needed"
      echo "  --check   report what is present, install nothing"
      echo "  --extras  also install gh and the missing system python packages (needs sudo)"
      echo "  --vscode  also install Visual Studio Code (implies --extras)"
      echo "  --antigravity  also install Google's agy CLI (~190 MB, opt-in; Projects' QA chair)"
      exit 0 ;;
    *) warn "unknown option: $arg" ;;
  esac
done

# --------------------------------------------------------------------------
say "Current state"
# --------------------------------------------------------------------------
for tool in python3 git node npm gh code claude codex agy; do
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

# --------------------------------------------------------------------------
say "Agent CLIs"
# --------------------------------------------------------------------------
# These are what make the pipeline free at the point of use: each authenticates
# against your existing subscription rather than a metered API key.
#
# Both vendors ship first-party installers that drop a standalone binary into
# ~/.local/bin and wire up PATH. That is preferred over `npm install -g` here
# for two reasons: it needs neither Node nor sudo, and the npm build of
# claude-code now requires Node >= 22, which would drag in a whole toolchain
# for no benefit. The Codex installer also places `codex-code-mode-host`
# alongside the main binary, which a hand-rolled release download misses.
export PATH="$HOME/.local/bin:$PATH"

if have claude; then
  ok "claude already installed: $(command -v claude)"
else
  curl -fsSL https://claude.ai/install.sh | bash || warn "claude CLI install failed"
fi

if have codex; then
  ok "codex already installed: $(command -v codex)"
else
  curl -fsSL https://chatgpt.com/codex/install.sh | bash || warn "codex CLI install failed"
fi

# Google's Antigravity CLI, which replaced Gemini CLI for personal accounts in
# June 2026. Opt-in rather than default: the two above are what the pipeline
# ships configured for, and this one is a ~190 MB binary nobody should get by
# accident. Its installer verifies a SHA-512 against the release manifest.
if [[ $WANT_AGY -eq 1 ]]; then
  if have agy; then
    ok "agy already installed: $(command -v agy)"
  else
    curl -fsSL https://antigravity.google/cli/install.sh | bash || warn "Antigravity CLI install failed"
  fi
fi

SHELL_RC="$HOME/.bashrc"
[[ -n "${ZSH_VERSION:-}" ]] && SHELL_RC="$HOME/.zshrc"
if ! grep -qs '.local/bin' "$SHELL_RC"; then
  echo '' >> "$SHELL_RC"
  echo '# added by ai-council/scripts/install-deps.sh' >> "$SHELL_RC"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
  ok "added ~/.local/bin to PATH in $SHELL_RC"
fi

# --------------------------------------------------------------------------
# Everything past this point is optional and needs root. The agent CLIs above
# are what Theseus AI actually requires, and they are already installed, so a
# machine without passwordless sudo is not blocked from a working pipeline.
# --------------------------------------------------------------------------
if [[ "$WANT_EXTRAS" -eq 0 ]]; then
  say "Skipping sudo-gated extras (pass --extras to install gh and dev packages)"
else
  if ! have sudo; then
    warn "sudo not available; skipping the optional system packages."
  else
    echo "${DIM}The remaining steps need your password. Ctrl-C to stop here -${RESET}"
    echo "${DIM}the agent CLIs above are already installed and sufficient.${RESET}"
    echo

    # ----------------------------------------------------------------------
    say "APT packages"
    # ----------------------------------------------------------------------
    # None of these are needed to run Theseus AI; they fill in the gaps in
    # this machine's unusually bare base image.
    APT_PKGS=(git curl ca-certificates python3-pip python3-venv python3-tk)
    sudo apt-get update
    sudo apt-get install -y "${APT_PKGS[@]}"

    # ----------------------------------------------------------------------
    say "GitHub CLI"
    # ----------------------------------------------------------------------
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

    # ----------------------------------------------------------------------
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
  fi
fi

# --------------------------------------------------------------------------
say "Done"
# --------------------------------------------------------------------------
cat <<'EOF'

Next steps:

  1. Reload your shell so the PATH change takes effect:
       source ~/.bashrc

  2. Authenticate each CLI against your subscription (one time, interactive):
       claude          # then follow the browser login for Claude Pro
       codex login     # then follow the browser login for ChatGPT Plus/Pro

     These are SUBSCRIPTION logins, not API keys. That is what keeps AI
     Council at zero per-token cost - setting an API key instead would put
     every run on metered billing.

  3. Confirm Theseus AI can see both CLIs:
       ./run.sh --doctor

  4. Launch:
       ./run.sh

EOF
