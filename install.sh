#!/usr/bin/env bash
# Linux bootstrap for Dotori: installs Docker if missing, fetches/updates the
# repo, and hands off to install.py for the actual setup wizard / operate
# commands. Re-running this script later (e.g. `./install.sh --status`) pulls
# the latest code and forwards any arguments straight to install.py -- it
# doesn't need a separate "update" script.
#
# Typical first run:
#   curl -fsSL https://raw.githubusercontent.com/smallwanderer/dotori/main/install.sh | bash
#
# After this finishes, systemd already keeps Docker running across reboots
# and every compose service has `restart: unless-stopped`, so nothing here
# needs to set up its own autostart -- see dev-docs/install-ux-plan.md.
set -euo pipefail

DOTORI_REPO_URL="${DOTORI_REPO_URL:-https://github.com/smallwanderer/dotori.git}"
DOTORI_BRANCH="${DOTORI_BRANCH:-main}"
DOTORI_INSTALL_DIR="${DOTORI_INSTALL_DIR:-$HOME/dotori}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$1" >&2; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$1" >&2; }
die() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$1" >&2; exit 1; }

detect_package_manager() {
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf >/dev/null 2>&1; then echo dnf
    elif command -v yum >/dev/null 2>&1; then echo yum
    elif command -v pacman >/dev/null 2>&1; then echo pacman
    elif command -v apk >/dev/null 2>&1; then echo apk
    else echo unknown
    fi
}

install_packages() {
    # $@: package names to install, if missing, using whatever package
    # manager this distro has. No-ops (with a warning) on an unrecognized
    # distro -- the caller's own command -v check will fail loudly next.
    local pm
    pm="$(detect_package_manager)"
    case "$pm" in
        apt)
            sudo apt-get update -qq
            sudo apt-get install -y "$@"
            ;;
        dnf) sudo dnf install -y "$@" ;;
        yum) sudo yum install -y "$@" ;;
        pacman) sudo pacman -Sy --noconfirm "$@" ;;
        apk) sudo apk add --no-cache "$@" ;;
        *)
            warn "Unrecognized package manager; install manually: $*"
            ;;
    esac
}

ensure_git() {
    if command -v git >/dev/null 2>&1; then
        return
    fi
    log "git not found, installing..."
    install_packages git
    command -v git >/dev/null 2>&1 || die "git installation failed"
}

ensure_python() {
    if command -v python3 >/dev/null 2>&1; then
        return
    fi
    log "python3 not found, installing..."
    local pm
    pm="$(detect_package_manager)"
    case "$pm" in
        apt) install_packages python3 python3-venv python3-pip ;;
        *) install_packages python3 python3-pip ;;
    esac
    command -v python3 >/dev/null 2>&1 || die "python3 installation failed"
}

ensure_python_venv_module() {
    # python3-venv is a separate apt package from python3 on Debian/Ubuntu;
    # its absence is a common "ensurepip is not available" surprise.
    if python3 -c "import venv" >/dev/null 2>&1; then
        return
    fi
    log "python3 venv module not found, installing..."
    install_packages python3-venv
    python3 -c "import venv" >/dev/null 2>&1 || die "python3-venv installation failed"
}

ensure_docker() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        log "Docker already installed and running."
        return
    fi
    if ! command -v docker >/dev/null 2>&1; then
        log "Docker not found, installing via get.docker.com..."
        curl -fsSL https://get.docker.com | sh
    fi
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl enable --now docker
    fi
    if ! groups "$USER" | grep -qw docker; then
        log "Adding $USER to the docker group (needed to run docker without sudo)..."
        sudo usermod -aG docker "$USER"
        warn "Group membership doesn't apply to the current session."
        warn "Log out and back in, then re-run this script to continue -- it will"
        warn "pick up right where it left off (repo pull + install.py are both safe"
        warn "to run again)."
        exit 0
    fi
    docker info >/dev/null 2>&1 || die "Docker was installed but the daemon isn't reachable. Is it running?"
}

clone_or_update_repo() {
    if [ -d "$DOTORI_INSTALL_DIR/.git" ]; then
        log "Dotori already cloned at $DOTORI_INSTALL_DIR, pulling latest..."
        git -C "$DOTORI_INSTALL_DIR" pull --ff-only origin "$DOTORI_BRANCH"
    else
        log "Cloning Dotori into $DOTORI_INSTALL_DIR..."
        git clone --branch "$DOTORI_BRANCH" "$DOTORI_REPO_URL" "$DOTORI_INSTALL_DIR"
    fi
}

ensure_install_venv() {
    # A dedicated venv for install.py's own (tiny) dependency list, so this
    # doesn't fight PEP 668 "externally-managed-environment" restrictions on
    # modern Debian/Ubuntu and doesn't touch system Python at all.
    local venv_dir="$DOTORI_INSTALL_DIR/.install-venv"
    if [ ! -x "$venv_dir/bin/python" ]; then
        log "Creating a venv for install.py's dependencies..."
        python3 -m venv "$venv_dir"
    fi
    "$venv_dir/bin/pip" install --quiet --upgrade pip
    "$venv_dir/bin/pip" install --quiet requests rich
    echo "$venv_dir/bin/python"
}

main() {
    ensure_git
    ensure_python
    ensure_python_venv_module
    ensure_docker
    clone_or_update_repo

    local install_python
    install_python="$(ensure_install_venv)"

    log "Handing off to install.py..."
    cd "$DOTORI_INSTALL_DIR"
    # Read from the real terminal, not this script's own stdin: when invoked
    # as `curl ... | bash`, stdin is the piped script source and is already
    # at EOF by now, which would make install.py's setup wizard (input())
    # fail instantly instead of prompting.
    "$install_python" install.py "$@" < /dev/tty
}

main "$@"
