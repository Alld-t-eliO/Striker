set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROJECT_NAME="Striker"
VENV_DIR="venv"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=10
LAUNCHER_NAME="striker"
LOG_FILE="install.log"

if [ -t 1 ]; then
    C_CYAN="\033[36m"
    C_VIOLET="\033[35m"
    C_GREEN="\033[32m"
    C_YELLOW="\033[33m"
    C_RED="\033[31m"
    C_RESET="\033[0m"
    C_BOLD="\033[1m"
else
    C_CYAN=""; C_VIOLET=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""; C_BOLD=""
fi


log()    { echo -e "${C_CYAN}[*]${C_RESET} $*" | tee -a "$LOG_FILE"; }
ok()     { echo -e "${C_GREEN}[✓]${C_RESET} $*" | tee -a "$LOG_FILE"; }
warn()   { echo -e "${C_YELLOW}[!]${C_RESET} $*" | tee -a "$LOG_FILE"; }
err()    { echo -e "${C_RED}[✗]${C_RESET} $*" | tee -a "$LOG_FILE" >&2; }
title()  { echo -e "\n${C_VIOLET}${C_BOLD}══ $* ══${C_RESET}" | tee -a "$LOG_FILE"; }

die() { err "$*"; exit 1; }

banner() {
    cat <<'EOF'

   ______      _ __
  / __/ /_____(_) /_____ ____
 _\ \/ __/ __/ /  '_/ -_) __/
/___/\__/_/ /_/_/\_\\__/_/

EOF
}

MINIMAL=0
NO_VENV=0
SKIP_OPTIONAL=0

for arg in "$@"; do
    case "$arg" in
        --minimal)      MINIMAL=1 ;;
        --no-venv)      NO_VENV=1 ;;
        --skip-optional) SKIP_OPTIONAL=1 ;;
        -h|--help)
            cat <<EOF
${PROJECT_NAME} installer

Usage:
  ./install.sh [options]

Options:
  --minimal         Install core dependencies only
  --no-venv         Install into the current Python environment
  --skip-optional   Skip optional dependencies (curl_cffi, cloudscraper, etc.)
  -h, --help        Show this help message

Examples:
  ./install.sh                    # recommended
  ./install.sh --minimal          # fastest, no optional modules
  ./install.sh --no-venv          # for Docker / CI
EOF
            exit 0
            ;;
        *) die "Unknown option: $arg (use --help)" ;;
    esac
done


: > "$LOG_FILE"
banner
title "$PROJECT_NAME installer"
log "Log file: $LOG_FILE"
log "Working directory: $SCRIPT_DIR"


title "Step 1/6 — OS detection"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux*)   PLATFORM="linux" ;;
    Darwin*)  PLATFORM="macos" ;;
    *)        PLATFORM="unknown" ;;
esac

ok "Platform: $PLATFORM ($ARCH)"

if [ "$PLATFORM" = "unknown" ]; then
    warn "Unsupported OS: $OS — proceeding anyway."
fi


title "Step 2/6 — Python detection"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    err "No Python >= ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} found."
    case "$PLATFORM" in
        macos) err "Install with: brew install python@3.12" ;;
        linux) err "Install with: sudo apt install python3.12 python3.12-venv (or equivalent)" ;;
    esac
    exit 1
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
ok "Python: $PYTHON_BIN ($PY_VERSION)"

# ─────────────────────────────────────────────────────────────
#  3. Virtual environment
# ─────────────────────────────────────────────────────────────
title "Step 3/6 — Virtual environment"

if [ "$NO_VENV" -eq 1 ]; then
    warn "Skipping venv creation (--no-venv)"
    PIP_CMD="$PYTHON_BIN -m pip"
    PY_RUN="$PYTHON_BIN"
else
    if [ -d "$VENV_DIR" ]; then
        warn "Existing venv detected at ./$VENV_DIR — reusing"
    else
        log "Creating virtual environment in ./$VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR" || die "venv creation failed"
        ok "Virtual environment created"
    fi

    # Activate
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    PY_RUN="python"
    PIP_CMD="pip"

    # Upgrade pip / setuptools / wheel
    log "Upgrading pip, setuptools, wheel"
    "$PIP_CMD" install --quiet --upgrade pip setuptools wheel >>"$LOG_FILE" 2>&1 \
        || warn "pip upgrade failed (continuing)"
    ok "Build tools up to date"
fi

# ─────────────────────────────────────────────────────────────
#  4. Dependencies
# ─────────────────────────────────────────────────────────────
title "Step 4/6 — Dependencies"

# Core
CORE_DEPS=(textual requests aiohttp)
log "Installing core dependencies: ${CORE_DEPS[*]}"
"$PIP_CMD" install --quiet "${CORE_DEPS[@]}" >>"$LOG_FILE" 2>&1 \
    || die "Core dependency installation failed"
ok "Core dependencies installed"

# Optional
if [ "$MINIMAL" -eq 0 ] && [ "$SKIP_OPTIONAL" -eq 0 ]; then
    OPTIONAL_DEPS=(curl_cffi cloudscraper redis)
    for pkg in "${OPTIONAL_DEPS[@]}"; do
        log "Installing optional: $pkg"
        if "$PIP_CMD" install --quiet "$pkg" >>"$LOG_FILE" 2>&1; then
            ok "  $pkg"
        else
            warn "  $pkg failed (optional, continuing)"
        fi
    done

    # Playwright is heavier and needs a browser download
    read -r -p "$(echo -e "${C_YELLOW}[?]${C_RESET} Install Playwright + Chromium for CAPTCHA fallback? [y/N] ")" PW_ANSWER
    if [[ "$PW_ANSWER" =~ ^[Yy]$ ]]; then
        log "Installing Playwright"
        if "$PIP_CMD" install --quiet playwright >>"$LOG_FILE" 2>&1; then
            ok "  playwright package"
            log "Downloading Chromium (~150 MB)"
            if "$PY_RUN" -m playwright install chromium >>"$LOG_FILE" 2>&1; then
                ok "  chromium browser"
            else
                warn "  chromium download failed — CAPTCHA fallback unavailable"
            fi
        else
            warn "  playwright install failed"
        fi
    else
        warn "Skipping Playwright (CAPTCHA fallback disabled)"
    fi
else
    warn "Skipping optional dependencies"
fi

# ─────────────────────────────────────────────────────────────
#  5. Project sanity check
# ─────────────────────────────────────────────────────────────
title "Step 5/6 — Project sanity check"

REQUIRED_FILES=(
    "main.py"
    "core/__init__.py"
    "core/ddos.py"
    "core/proxy_rotator.py"
    "core/user_agent_rotator.py"
    "core/delay_jitter.py"
    "core/adaptive_backoff.py"
    "core/request_fragmenter.py"
    "core/distributed_bots.py"
    "core/captcha_waf_bypass.py"
    "ui/ui.py"
)

MISSING=0
for f in "${REQUIRED_FILES[@]}"; do
    if [ -f "$f" ]; then
        ok "  $f"
    else
        warn "  MISSING: $f"
        MISSING=$((MISSING + 1))
    fi
done

if [ "$MISSING" -gt 0 ]; then
    warn "$MISSING file(s) missing — the project may not run correctly"
else
    ok "All required files present"
fi

# Ensure core/__init__.py exists
if [ ! -f "core/__init__.py" ]; then
    log "Creating empty core/__init__.py"
    touch "core/__init__.py"
    ok "core/__init__.py created"
fi

# ─────────────────────────────────────────────────────────────
#  6. Launcher + self-test
# ─────────────────────────────────────────────────────────────
title "Step 6/6 — Launcher and self-test"

# Self-test: import core modules
log "Running import self-test"
SELFTEST_FAILED=0
for mod in proxy_rotator user_agent_rotator delay_jitter adaptive_backoff \
           request_fragmenter distributed_bots captcha_waf_bypass ddos; do
    if "$PY_RUN" -c "import sys; sys.path.insert(0, '.'); from core.${mod} import *" >>"$LOG_FILE" 2>&1; then
        ok "  core.${mod}"
    else
        warn "  core.${mod} — import failed (see $LOG_FILE)"
        SELFTEST_FAILED=$((SELFTEST_FAILED + 1))
    fi
done

if [ "$SELFTEST_FAILED" -eq 0 ]; then
    ok "All core modules import cleanly"
else
    warn "$SELFTEST_FAILED module(s) failed to import"
fi

# Generate launcher
LAUNCHER_PATH="$SCRIPT_DIR/$LAUNCHER_NAME"
log "Generating launcher: $LAUNCHER_PATH"

cat > "$LAUNCHER_PATH" <<EOF
#!/usr/bin/env bash
# Auto-generated by install.sh
cd "$SCRIPT_DIR"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
fi
python ui/ui.py "\$@"
EOF

chmod +x "$LAUNCHER_PATH"
ok "Launcher created: $LAUNCHER_PATH"

# Optionally symlink to /usr/local/bin
if [ -w "/usr/local/bin" ]; then
    ln -sf "$LAUNCHER_PATH" "/usr/local/bin/$LAUNCHER_NAME"
    ok "Symlink installed: /usr/local/bin/$LAUNCHER_NAME"
    log "You can now run '$LAUNCHER_NAME' from anywhere"
else
    warn "Cannot write to /usr/local/bin — run manually:"
    echo "    sudo ln -sf \"$LAUNCHER_PATH\" /usr/local/bin/$LAUNCHER_NAME"
fi

# ─────────────────────────────────────────────────────────────
#  Done
# ─────────────────────────────────────────────────────────────
title "Installation complete"

cat <<EOF

${C_GREEN}${C_BOLD}✓ $PROJECT_NAME is ready.${C_RESET}

${C_CYAN}Launch the TUI:${C_RESET}
    ./$LAUNCHER_NAME
    # or
    $( [ "$NO_VENV" -eq 0 ] && echo "source $VENV_DIR/bin/activate && python ui/ui.py" || echo "python ui/ui.py" )

${C_CYAN}CLI — resilience test:${C_RESET}
    python main.py dos 192.168.56.20 -p 80 -t 10 -s 50 -i 15 -d 60

${C_CYAN}CLI — stealth HTTP:${C_RESET}
    python main.py scrape https://target.local/ --verbose

${C_YELLOW}Reminder:${C_RESET}
    The offensive module refuses non-private IPs.
    Use it only on isolated lab networks.

${C_CYAN}Logs:${C_RESET} $LOG_FILE

EOF