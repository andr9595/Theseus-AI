#!/usr/bin/env bash
# Dependency installer for Theseus AI on Pop!_OS / Ubuntu 24.04.
#
# The application itself needs none of this - it is pure Python 3 standard
# library and runs with `./run.sh` on a stock install.
#
# No agent is installed unless you name one. Which AI you use is your choice
# and this script has no opinion about it: pass --agent once per CLI you want,
# and nothing at all is installed if you pass none. Settings -> Agents does the
# same thing with buttons, and calls this script to do it.
#
#   ./install-deps.sh --agent codex --agent claude
#
# Each uses that vendor's first-party installer, which drops a standalone
# binary into ~/.local/bin - no Node, no npm and no sudo. Antigravity (`agy`)
# is a ~190 MB binary, which is worth knowing before you ask for it.
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
AGENTS=()

usage() {
  # Print the header comment block, stopping at the first non-comment line
  # so the help text cannot drift out of sync with the file again.
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  echo
  echo "Usage: $0 [--agent NAME]... [--check] [--extras] [--vscode]"
  echo "  --agent NAME  install one agent CLI: codex, claude or agy (repeatable)"
  echo "  --check       report what is present, install nothing"
  echo "  --extras      also install gh and the missing system python packages (needs sudo)"
  echo "  --vscode      also install Visual Studio Code (implies --extras)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vscode)   WANT_VSCODE=1; WANT_EXTRAS=1 ;;
    --extras)   WANT_EXTRAS=1 ;;
    --agent)
      shift
      case "${1:-}" in
        codex|claude|agy) AGENTS+=("$1") ;;
        *) bad "unknown agent: ${1:-(none)} - expected codex, claude or agy"
           exit 2 ;;
      esac ;;
    # The old spelling, kept working because it is in a released README.
    --antigravity) AGENTS+=("agy") ;;
    --check)    WANT_ALL=0 ;;
    -h|--help)  usage; exit 0 ;;
    *) warn "unknown option: $1" ;;
  esac
  shift
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
# All three vendors ship first-party installers that drop a standalone binary
# into ~/.local/bin and wire up PATH. That is preferred over `npm install -g`
# here for two reasons: it needs neither Node nor sudo, and the npm build of
# claude-code now requires Node >= 22, which would drag in a whole toolchain
# for no benefit. The Codex installer also places `codex-code-mode-host`
# alongside the main binary, which a hand-rolled release download misses.
export PATH="$HOME/.local/bin:$PATH"

install_agent() {
  local agent="$1" url=""
  case "$agent" in
    codex)  url=https://chatgpt.com/codex/install.sh ;;
    claude) url=https://claude.ai/install.sh ;;
    # Google's Antigravity CLI, which replaced Gemini CLI for personal accounts
    # in June 2026. Its installer verifies a SHA-512 against the manifest.
    agy)    url=https://antigravity.google/cli/install.sh ;;
  esac
  if have "$agent"; then
    ok "$agent already installed: $(command -v "$agent")"
    return 0
  fi
  curl -fsSL "$url" | bash || warn "$agent CLI install failed"
}

if [[ ${#AGENTS[@]} -eq 0 ]]; then
  echo "No agent named, so none installed. Pick whichever you have access to:"
  echo "  $0 --agent codex     # ChatGPT Plus/Pro/Business"
  echo "  $0 --agent claude    # Claude Pro/Max"
  echo "  $0 --agent agy       # Google account (~190 MB)"
  echo
  echo "${DIM}Or add them in the app: Settings -> Agents.${RESET}"
  echo
else
  for agent in "${AGENTS[@]}"; do
    install_agent "$agent"
  done
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
    echo "${DIM}whichever agent CLIs you asked for are already installed.${RESET}"
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

  2. Sign each CLI you installed in to your subscription (one time):
       codex login     # then follow the browser login for ChatGPT Plus/Pro
       claude auth login --claudeai   # browser login for Claude Pro/Max
       agy             # sign in inside the session, with a Google account

     These are SUBSCRIPTION logins, not API keys. That is what keeps Theseus
     AI at zero per-token cost - setting an API key instead would put every
     run on metered billing.

  3. Launch, and add the agents you installed in Settings -> Agents:
       ./run.sh

     Adding is the step that seats one. Installing a CLI does not, so a
     machine that happens to carry all three still runs only what you chose.
     That screen can also do steps 1 and 2 for you.

  4. Or check from the terminal instead:
       ./run.sh --doctor

EOF
