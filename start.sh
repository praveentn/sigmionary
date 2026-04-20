#!/usr/bin/env bash
# ============================================================
#  Sigmionary Discord Bot — Unix startup script
#  Usage:
#    ./start.sh            (reads PORT from .env, default 8080)
#    PORT=9090 ./start.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  =========================================="
echo "    🎮  Sigmionary Discord Bot"
echo "  =========================================="
echo ""

# ── Read PORT from .env (env var takes precedence) ───────────
if [[ -z "${PORT:-}" ]]; then
    if [[ -f ".env" ]]; then
        PORT="$(grep -E '^PORT=' .env | cut -d= -f2- | tr -d ' "' | head -1)"
    fi
fi
PORT="${PORT:-8080}"
echo "  [sigmionary] Port   : $PORT"

# ── Detect python command ────────────────────────────────────
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null && "$cmd" -c "import sys; assert sys.version_info >= (3, 10)" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    echo "  [sigmionary] ERROR: Python 3.10+ not found in PATH."
    echo "  Install Python 3.10+ from https://python.org"
    exit 1
fi
echo "  [sigmionary] Python : $($PYTHON_CMD --version)"
echo ""

# ── Check / free the port ────────────────────────────────────
echo "  [sigmionary] Checking port $PORT..."
if lsof -i ":$PORT" -sTCP:LISTEN -t &>/dev/null; then
    PID="$(lsof -i ":$PORT" -sTCP:LISTEN -t | head -1)"
    echo "  [sigmionary] WARNING: Port $PORT in use by PID $PID — killing..."
    kill -9 "$PID" 2>/dev/null || true
    sleep 1
    echo "  [sigmionary] OK: Port $PORT freed."
else
    echo "  [sigmionary] OK: Port $PORT is free."
fi
echo ""

# ── Virtual environment ──────────────────────────────────────
if [[ ! -f "venv/bin/activate" ]]; then
    echo "  [sigmionary] Creating virtual environment..."
    "$PYTHON_CMD" -m venv venv
    echo "  [sigmionary] OK: venv created."
fi

echo "  [sigmionary] Activating venv..."
# shellcheck disable=SC1091
source venv/bin/activate
echo "  [sigmionary] venv   : $(python --version)"

# ── Install / sync requirements ──────────────────────────────
echo ""
echo "  [sigmionary] Checking requirements..."
pip install -r requirements.txt -q --disable-pip-version-check
echo "  [sigmionary] OK: Dependencies up to date."

# ── Token check (non-fatal warning) ─────────────────────────
echo ""
TOKEN_VAL=""
if [[ -f ".env" ]]; then
    TOKEN_VAL="$(grep -E '^DISCORD_TOKEN=' .env | cut -d= -f2- | tr -d ' "' | head -1 || true)"
fi

if [[ -z "$TOKEN_VAL" ]]; then
    echo "  [sigmionary] WARNING: DISCORD_TOKEN is not set in .env"
    echo "  [sigmionary]          Bot will start in local-only mode."
    echo "  [sigmionary]          Open http://localhost:${PORT}/ for setup instructions."
elif [[ "$TOKEN_VAL" == "your_bot_token_here" ]]; then
    echo "  [sigmionary] WARNING: DISCORD_TOKEN still has the placeholder value."
    echo "  [sigmionary]          Bot will start in local-only mode."
    echo "  [sigmionary]          Open http://localhost:${PORT}/ for setup instructions."
else
    echo "  [sigmionary] OK: Discord token found."
fi

# ── Launch ───────────────────────────────────────────────────
echo ""
echo "  [sigmionary] Starting bot  -->  http://localhost:${PORT}/"
echo "  [sigmionary] Press Ctrl+C to stop."
echo ""

python bot.py

echo ""
echo "  [sigmionary] Bot stopped."
