#!/usr/bin/env bash
# LocalCode bootstrap — brings up the full stack from a clean checkout.
#
#   ./setup.sh            # prep + start everything in the background
#   ./setup.sh login      # run `claude login` and `opencode auth login` interactively
#   ./setup.sh stop       # stop backend + frontend + opencode
#   ./setup.sh down       # stop everything including docker compose
#   ./setup.sh status     # show whether services are running
#   ./setup.sh logs       # tail backend + frontend + opencode logs
#
# Re-runnable: each step is idempotent.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/.run"
mkdir -p "$RUN_DIR"

VENV_DIR="$ROOT_DIR/.venv"
BACKEND_PID="$RUN_DIR/backend.pid"
FRONTEND_PID="$RUN_DIR/frontend.pid"
OPENCODE_PID="$RUN_DIR/opencode.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
OPENCODE_LOG="$RUN_DIR/opencode.log"

# Where the official installer lands `opencode` on macOS / Linux.
OPENCODE_HOME="$HOME/.opencode"
OPENCODE_BIN="$OPENCODE_HOME/bin/opencode"

# ── colours ──────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_INFO=$'\033[1;36m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_END=$'\033[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_END=""
fi
log()  { printf "%s==>%s %s\n" "$C_INFO" "$C_END" "$*"; }
ok()   { printf "%s ✓ %s%s\n" "$C_OK"   "$*" "$C_END"; }
warn() { printf "%s ! %s%s\n" "$C_WARN" "$*" "$C_END"; }
fail() { printf "%s ✗ %s%s\n" "$C_ERR"  "$*" "$C_END"; exit 1; }

need() {
  local bin="$1" hint="${2:-}"
  if ! command -v "$bin" >/dev/null 2>&1; then
    if [[ -n "$hint" ]]; then
      fail "missing dependency: $bin — install with: $hint"
    else
      fail "missing dependency: $bin"
    fi
  fi
}

# ── helpers ──────────────────────────────────────────────────────────────────
load_env() {
  # Parse .env without `source` so unquoted values containing spaces don't
  # get re-interpreted as commands. Skips comments and blank lines.
  [[ -f .env ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    local k="${BASH_REMATCH[1]}" v="${BASH_REMATCH[2]}"
    # Strip a single layer of surrounding single or double quotes.
    if [[ "$v" =~ ^\"(.*)\"$ ]] || [[ "$v" =~ ^\'(.*)\'$ ]]; then
      v="${BASH_REMATCH[1]}"
    fi
    export "$k=$v"
  done < .env
}

is_running() {  # is_running <pidfile>
  local pf="$1"
  [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null
}

stop_pidfile() {  # stop_pidfile <pidfile> <name>
  local pf="$1" name="$2"
  if is_running "$pf"; then
    local pid="$(cat "$pf")"
    log "stopping $name (pid $pid)"
    # Kill the recorded pid and any descendants (npm → vite, uvicorn → workers).
    # We do NOT kill the whole process group: backgrounded daemons inherit our
    # own pgid by default, so a pgid kill takes out our siblings too.
    _kill_tree "$pid" TERM
    sleep 1
    _kill_tree "$pid" KILL
    rm -f "$pf"
    ok "$name stopped"
  else
    warn "$name not running"
    rm -f "$pf"
  fi
}

# Recursively kill `pid` and all its descendants with the given signal.
_kill_tree() {
  local pid="$1" sig="$2"
  [[ -z "$pid" ]] && return
  # Walk children first so they die before the parent reaps them.
  local child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    _kill_tree "$child" "$sig"
  done
  kill -"$sig" "$pid" 2>/dev/null || true
}

# Wait for an HTTP endpoint to return 2xx. wait_http <url> <timeout-seconds> <label>
wait_http() {
  local url="$1" timeout="$2" label="$3" elapsed=0
  log "waiting for $label ($url)"
  while (( elapsed < timeout )); do
    if curl -fsS -o /dev/null "$url"; then
      ok "$label is up"
      return 0
    fi
    sleep 2; elapsed=$(( elapsed + 2 ))
  done
  fail "$label did not become ready in ${timeout}s — check 'docker compose logs'"
}

# ── subcommands ──────────────────────────────────────────────────────────────
cmd_status() {
  load_env
  if is_running "$BACKEND_PID";  then ok  "backend running (pid $(cat "$BACKEND_PID"))";  else warn "backend stopped";  fi
  if is_running "$FRONTEND_PID"; then ok  "frontend running (pid $(cat "$FRONTEND_PID"))"; else warn "frontend stopped"; fi
  if command -v docker >/dev/null 2>&1; then
    docker compose ps 2>/dev/null || true
  fi
}

cmd_stop() {
  stop_pidfile "$BACKEND_PID"  "backend"
  stop_pidfile "$FRONTEND_PID" "frontend"
  stop_pidfile "$OPENCODE_PID" "opencode"
}

cmd_down() {
  cmd_stop
  if command -v docker >/dev/null 2>&1; then
    log "docker compose down"
    docker compose down
    ok "docker stack stopped"
  fi
}

cmd_logs() {
  log "tailing logs (Ctrl-C to exit)"
  touch "$BACKEND_LOG" "$FRONTEND_LOG" "$OPENCODE_LOG"
  tail -n 50 -F "$BACKEND_LOG" "$FRONTEND_LOG" "$OPENCODE_LOG"
}

# Run `claude login` and `opencode auth login` interactively. One-shot — once a
# provider is logged in, OpenCode/Claude reuse the OAuth token + auto-refresh,
# so this rarely needs re-running.
cmd_login() {
  ensure_opencode_installed
  if ! command -v claude >/dev/null 2>&1; then
    fail "claude CLI not installed yet — run ./setup.sh first"
  fi

  log "logging in to Claude Code (browser will open)"
  if [[ -f "$HOME/.claude/.credentials.json" ]]; then
    ok "already authenticated with Claude (~/.claude/.credentials.json present) — skipping"
  else
    claude login
  fi

  log "logging in to OpenCode (pick OpenAI for ChatGPT subscription / Codex)"
  "$OPENCODE_BIN" auth login

  ok "login complete. Tokens persist at ~/.claude and ~/.local/share/opencode/."
}

# Claude Code stores its OAuth token in ~/.claude/.credentials.json on Linux,
# but in the macOS Keychain on Darwin. Best signal we can read without trying
# to extract the secret: a non-empty ~/.claude/ directory after first login.
_claude_logged_in() {
  [[ -f "$HOME/.claude/.credentials.json" ]] && return 0
  if [[ "$(uname -s)" == "Darwin" ]]; then
    # Anything claude has written post-login (config, history, etc.) is enough.
    [[ -d "$HOME/.claude" ]] && [[ -n "$(ls -A "$HOME/.claude" 2>/dev/null)" ]] && return 0
  fi
  return 1
}

_opencode_logged_in() {
  [[ -f "$HOME/.local/share/opencode/auth.json" ]] && return 0
  [[ -f "$HOME/Library/Application Support/opencode/auth.json" ]] && return 0
  return 1
}

ensure_opencode_installed() {
  if [[ -x "$OPENCODE_BIN" ]]; then
    ok "opencode already installed ($("$OPENCODE_BIN" --version 2>/dev/null | head -1))"
    return
  fi
  log "installing opencode (host-side) — this enables OAuth flows that won't work in Docker"
  curl -fsSL https://opencode.ai/install | bash >/dev/null
  if [[ ! -x "$OPENCODE_BIN" ]]; then
    fail "opencode install completed but $OPENCODE_BIN not found"
  fi
  ok "opencode installed at $OPENCODE_BIN"
}

cmd_up() {
  # 1. Check prerequisites
  log "checking prerequisites"
  local hint_brew=""
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    hint_brew="brew install"
  fi
  need python3 "${hint_brew:+$hint_brew python}"
  need node    "${hint_brew:+$hint_brew node}"
  need npm     "${hint_brew:+$hint_brew node}"
  need curl    "${hint_brew:+$hint_brew curl}"
  need docker  "install Docker Desktop from https://www.docker.com/products/docker-desktop/"
  if ! docker compose version >/dev/null 2>&1; then
    fail "docker compose v2 is required (the legacy 'docker-compose' binary is not supported)"
  fi
  ok "all prerequisites present"

  # 2. .env
  if [[ ! -f .env ]]; then
    cp .env.example .env
    ok "created .env from .env.example — edit it to add ANTHROPIC_API_KEY / OPENAI_API_KEY"
  else
    ok ".env already present"
  fi
  load_env

  # 3. Python venv + deps
  if [[ ! -d "$VENV_DIR" ]]; then
    log "creating virtualenv at .venv"
    python3 -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  log "installing python deps"
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -e '.[dev]' >/dev/null
  ok "python deps installed"

  # 4. Frontend deps
  if [[ ! -d frontend/node_modules ]]; then
    log "installing frontend deps"
    (cd frontend && npm install --silent)
    ok "frontend deps installed"
  else
    ok "frontend deps already installed"
  fi

  # 4b. Claude Code CLI (host-side) — needed for the OAuth flow.
  if ! command -v claude >/dev/null 2>&1; then
    log "installing @anthropic-ai/claude-code globally"
    npm i -g @anthropic-ai/claude-code >/dev/null
    ok "claude CLI installed"
  else
    ok "claude CLI already installed ($(claude --version 2>/dev/null | head -1))"
  fi

  # 4c. OpenCode CLI (host-side) — required for OAuth flows; can't run in Docker.
  ensure_opencode_installed

  # 5. Docker stack (postgres only). OpenCode runs as a host process.
  log "bringing up docker stack (postgres)"
  docker compose up -d

  # 7. DB schema
  log "ensuring database schema"
  python -m backend.app.db_init
  ok "schema ready"

  # 8. Start backend (uvicorn) in the background
  if is_running "$BACKEND_PID"; then
    warn "backend already running (pid $(cat "$BACKEND_PID")) — leaving as-is"
  else
    log "starting backend on :${PORT:-8080}"
    # Redirect all three std fds so the child doesn't keep our caller's pipe open.
    nohup "$VENV_DIR/bin/uvicorn" backend.app.main:app \
      --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}" \
      </dev/null >>"$BACKEND_LOG" 2>&1 &
    echo $! >"$BACKEND_PID"
    disown 2>/dev/null || true
    ok "backend started (pid $(cat "$BACKEND_PID")) — log: $BACKEND_LOG"
  fi

  # 9. Start opencode serve on host so OAuth tokens at ~/.local/share/opencode/ work.
  if is_running "$OPENCODE_PID"; then
    warn "opencode already running (pid $(cat "$OPENCODE_PID")) — leaving as-is"
  else
    log "starting opencode serve on :4096"
    (cd "$ROOT_DIR/opencode" && nohup "$OPENCODE_BIN" serve --hostname 127.0.0.1 --port 4096 \
      </dev/null >>"$OPENCODE_LOG" 2>&1 & echo $! >"$OPENCODE_PID"; disown 2>/dev/null || true)
    ok "opencode started (pid $(cat "$OPENCODE_PID")) — log: $OPENCODE_LOG"
  fi
  wait_http "http://localhost:4096/doc" 30 "opencode server"

  # 10. Start frontend (vite) in the background
  if is_running "$FRONTEND_PID"; then
    warn "frontend already running (pid $(cat "$FRONTEND_PID")) — leaving as-is"
  else
    log "starting frontend on :5173"
    (cd frontend && nohup npm run dev -- --host </dev/null >>"$FRONTEND_LOG" 2>&1 & echo $! >"$FRONTEND_PID"; disown 2>/dev/null || true)
    ok "frontend started (pid $(cat "$FRONTEND_PID")) — log: $FRONTEND_LOG"
  fi

  # 11. Smoke check the backend itself
  wait_http "http://localhost:${PORT:-8080}/api/health" 30 "backend"

  # 12. Nudge the user to log in if either OAuth token is missing.
  local need_login=()
  if ! _claude_logged_in; then need_login+=("claude"); fi
  if ! _opencode_logged_in; then need_login+=("opencode"); fi
  if (( ${#need_login[@]} > 0 )); then
    warn "not yet authenticated: ${need_login[*]}"
    warn "  Run:  ./setup.sh login   (one-time browser-based login; tokens are reused thereafter)"
  fi

  cat <<EOF

${C_OK}LocalCode is up.${C_END}

  UI:        http://localhost:5173
  Backend:   http://localhost:${PORT:-8080}/api/health
  OpenCode:  http://localhost:4096   (host process, not docker — needed for OAuth flows.)

  ./setup.sh login   # one-shot Claude + OpenCode login (browser opens)
  ./setup.sh logs    # tail backend + frontend + opencode
  ./setup.sh status  # show what's running
  ./setup.sh stop    # stop backend + frontend + opencode
  ./setup.sh down    # stop everything (incl. docker)

EOF
}

# ── dispatch ─────────────────────────────────────────────────────────────────
case "${1:-up}" in
  up|"")    cmd_up ;;
  login)    cmd_login ;;
  stop)     cmd_stop ;;
  down)     cmd_down ;;
  status)   cmd_status ;;
  logs)     cmd_logs ;;
  *)        fail "unknown command: $1 (use up|login|stop|down|status|logs)" ;;
esac
