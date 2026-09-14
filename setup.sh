#!/usr/bin/env bash
# LocalCode bootstrap — brings up the full stack from a clean checkout.
#
#   ./setup.sh            # prep + start everything in the background
#   ./setup.sh login      # run `claude login` and (if installed) `codex login`
#   ./setup.sh stop       # stop backend + frontend
#   ./setup.sh down       # stop everything (alias for stop now that the stack is purely host-side)
#   ./setup.sh status     # show whether services are running
#   ./setup.sh logs       # tail backend + frontend logs
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
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"


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
  fail "$label did not become ready in ${timeout}s — check $BACKEND_LOG"
}

# ── subcommands ──────────────────────────────────────────────────────────────
cmd_status() {
  load_env
  if is_running "$BACKEND_PID";  then ok  "backend running (pid $(cat "$BACKEND_PID"))";  else warn "backend stopped";  fi
  if is_running "$FRONTEND_PID"; then ok  "frontend running (pid $(cat "$FRONTEND_PID"))"; else warn "frontend stopped"; fi
}

cmd_stop() {
  stop_pidfile "$BACKEND_PID"  "backend"
  stop_pidfile "$FRONTEND_PID" "frontend"
}

# `down` is now an alias for `stop` — there's no docker stack to bring down
# anymore (Postgres has been replaced with on-disk session storage).
cmd_down() {
  cmd_stop
}

cmd_logs() {
  log "tailing logs (Ctrl-C to exit)"
  touch "$BACKEND_LOG" "$FRONTEND_LOG"
  tail -n 50 -F "$BACKEND_LOG" "$FRONTEND_LOG"
}

# Log each vendor CLI in, in its own CLI. One-shot — the token persists and
# auto-refreshes, so this rarely needs re-running. LocalCode never reads these
# stores; it spawns the CLI and lets the CLI find its own credentials.
cmd_login() {
  if ! command -v claude >/dev/null 2>&1; then
    fail "claude CLI not installed yet — run ./setup.sh first"
  fi

  log "logging in to Claude Code (browser will open)"
  if [[ -f "$HOME/.claude/.credentials.json" ]]; then
    ok "already authenticated with Claude (~/.claude/.credentials.json present) — skipping"
  else
    claude login
  fi

  # Codex is the ChatGPT-subscription path and is optional: Claude alone is a
  # working install, so a missing binary is a note rather than a failure.
  if command -v codex >/dev/null 2>&1; then
    log "logging in to Codex (ChatGPT subscription)"
    codex login
    ok "login complete. Tokens persist at ~/.claude and ~/.codex/."
  else
    ok "login complete. Tokens persist at ~/.claude/."
    warn "codex CLI not installed — the ChatGPT path is unavailable until you run:"
    warn "    npm i -g @openai/codex && codex login"
  fi
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

# Codex writes its own credential store; we only ask whether one exists.
_codex_logged_in() {
  [[ -f "$HOME/.codex/auth.json" ]] && return 0
  return 1
}

# Codex is optional. Unlike the retired OpenCode provider it needs no
# long-running server — the backend spawns `codex app-server` per session over
# stdio — so there is nothing to start here, only something to check for.
check_codex() {
  if command -v codex >/dev/null 2>&1; then
    ok "codex installed ($(codex --version 2>/dev/null | head -1))"
  else
    warn "codex not installed — Claude works without it; for the ChatGPT path run:"
    warn "    npm i -g @openai/codex && codex login"
  fi
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

  # 4c. Codex CLI (optional) — the ChatGPT-subscription path.
  check_codex

  # 5. Sessions are stored on disk under <session.cwd>/.localcode/sessions/
  #    plus a user-global index at ~/.localcode/sessions-index.json. No
  #    database to provision, no docker stack to bring up. Cleanup of stale
  #    sessions is bounded by SESSION_RETENTION_DAYS (default 7) and runs
  #    on every backend startup.

  # 6. Start backend (uvicorn) in the background
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
  if command -v codex >/dev/null 2>&1 && ! _codex_logged_in; then need_login+=("codex"); fi
  if (( ${#need_login[@]} > 0 )); then
    warn "not yet authenticated: ${need_login[*]}"
    warn "  Run:  ./setup.sh login   (one-time browser-based login; tokens are reused thereafter)"
  fi

  cat <<EOF

${C_OK}LocalCode is up.${C_END}

  UI:        http://localhost:5173
  Backend:   http://localhost:${PORT:-8080}/api/health

  Sessions: stored on disk under <session.cwd>/.localcode/sessions/
            (index at ~/.localcode/sessions-index.json). Auto-swept after
            SESSION_RETENTION_DAYS days on backend startup.

  ./setup.sh login   # one-shot Claude (+ Codex) login (browser opens)
  ./setup.sh logs    # tail backend + frontend
  ./setup.sh status  # show what's running
  ./setup.sh stop    # stop backend + frontend
  ./setup.sh down    # alias for stop (no docker stack anymore)

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
