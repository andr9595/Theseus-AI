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
WANT_GH=0
AGENTS=()

# Used only when the "latest release" redirect cannot be followed - see
# `install_gh`. Bump it when convenient; nothing depends on it being current.
GH_FALLBACK_TAG="v2.97.0"

usage() {
  # Print the header comment block, stopping at the first non-comment line
  # so the help text cannot drift out of sync with the file again.
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  echo
  echo "Usage: $0 [--agent NAME]... [--gh] [--check] [--extras] [--vscode]"
  echo "  --agent NAME  install one agent CLI: codex, claude or agy (repeatable)"
  echo "  --gh          install the GitHub CLI into ~/.local/bin (no sudo)"
  echo "  --check       report what is present, install nothing"
  echo "  --extras      also install gh and the missing system python packages (needs sudo)"
  echo "  --vscode      also install Visual Studio Code (implies --extras)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vscode)   WANT_VSCODE=1; WANT_EXTRAS=1 ;;
    --extras)   WANT_EXTRAS=1 ;;
    --gh)       WANT_GH=1 ;;
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

# --------------------------------------------------------------------------
# The GitHub CLI, rootless
# --------------------------------------------------------------------------
# `--extras` installs gh with apt, which needs sudo. This does not: gh ships a
# static binary in its release tarballs, so it can live in ~/.local/bin beside
# the agent CLIs. That matters because Settings -> Agents offers the install as
# a button, and a button that stops to ask for a password in a pane that cannot
# accept one is worse than no button.
# A slim container has neither curl nor wget as often as it has one of them,
# so both are accepted and the *absence of both* is reported as itself. The
# failure this replaces said "could not work out the latest gh release", which
# blamed the network for a missing package.
fetch_to() {  # url, destination
  if have curl; then
    curl -fsSL "$1" -o "$2"
  elif have wget; then
    wget -qO "$2" "$1"
  else
    return 127
  fi
}

# The tag of the newest release, without spending an API call. The redirect on
# /releases/latest needs no token and is not rate-limited the way
# api.github.com is for an unauthenticated caller.
latest_gh_tag() {
  if have curl; then
    curl -fsSLI -o /dev/null -w '%{url_effective}' \
      https://github.com/cli/cli/releases/latest 2>/dev/null | sed 's#.*/tag/##'
  elif have wget; then
    # wget prints the redirect chain to stderr under -S; the last Location is
    # the resolved tag.
    wget -S --spider --max-redirect=10 \
      https://github.com/cli/cli/releases/latest 2>&1 \
      | sed -n 's#.*Location: .*/tag/\([^ ]*\).*#\1#p' | tail -1
  fi
}

install_gh() {
  if have gh; then
    ok "gh already installed: $(command -v gh)"
    return 0
  fi

  # Preflight, so a missing tool is named as a missing tool. In a container
  # this is the difference between one `apt-get install` and an afternoon.
  local missing=()
  have curl || have wget || missing+=("curl or wget")
  have tar || missing+=("tar")
  if [[ ${#missing[@]} -gt 0 ]]; then
    bad "cannot install gh: this system has no ${missing[*]}"
    echo "     Install ${missing[*]} first, or install gh yourself and"
    echo "     put it on PATH - the app only needs it to be findable."
    return 1
  fi

  local arch tag version url tmp
  case "$(uname -m)" in
    x86_64)  arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) bad "no prebuilt gh for $(uname -m); install gh from your package manager"
       return 1 ;;
  esac

  tag="$(latest_gh_tag)"
  if [[ -z "$tag" || "$tag" != v* ]]; then
    # Pinned so a blocked redirect, a proxy that rewrites it, or a rate limit
    # does not turn into "no GitHub for you". The version only decides which
    # tarball is fetched; a newer one is picked up next time.
    tag="$GH_FALLBACK_TAG"
    warn "could not resolve the latest gh release; falling back to $tag"
  fi
  version="${tag#v}"
  url="https://github.com/cli/cli/releases/download/${tag}/gh_${version}_linux_${arch}.tar.gz"

  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064 - expand tmp now, not at trap time
  trap "rm -rf '$tmp'" RETURN
  say "Fetching gh ${version} (${arch})"
  if ! fetch_to "$url" "$tmp/gh.tar.gz"; then
    warn "gh download failed: $url"
    return 1
  fi
  # Not preflighted with `have gzip`: GNU tar shells out to it, but busybox tar
  # decompresses in-process, so checking for it would refuse to run on images
  # where it would have worked. Named here instead, where it has actually failed.
  if ! tar xzf "$tmp/gh.tar.gz" -C "$tmp"; then
    warn "gh archive could not be unpacked - if tar reported a missing gzip,"
    warn "install gzip (or use a tar that decompresses on its own)"
    return 1
  fi
  # `install` is coreutils and present nearly everywhere, but "nearly" is what
  # bites in a distroless image, so cp is the fallback.
  mkdir -p "$HOME/.local/bin"
  if ! install -m 0755 "$tmp/gh_${version}_linux_${arch}/bin/gh" \
       "$HOME/.local/bin/gh" 2>/dev/null; then
    cp "$tmp/gh_${version}_linux_${arch}/bin/gh" "$HOME/.local/bin/gh" \
      && chmod 0755 "$HOME/.local/bin/gh" \
      || { warn "could not place gh in ~/.local/bin"; return 1; }
  fi
  ok "gh ${version} installed: $HOME/.local/bin/gh"
}

if [[ "$WANT_GH" -eq 1 ]]; then
  say "GitHub CLI"
  install_gh || true
  echo
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
