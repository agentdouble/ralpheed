#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

API_BASE_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
export VITE_API_URL="${VITE_API_URL:-$API_BASE_URL}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it, then re-run ./start.sh" >&2
  exit 1
fi

echo "Backend URL:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Frontend URL: http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "API URL:     ${API_BASE_URL}"
echo

backend_pid=""
frontend_pid=""

cleanup() {
  if [ -n "${frontend_pid}" ] && kill -0 "${frontend_pid}" 2>/dev/null; then
    kill "${frontend_pid}" 2>/dev/null || true
  fi
  if [ -n "${backend_pid}" ] && kill -0 "${backend_pid}" 2>/dev/null; then
    kill "${backend_pid}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

(
  cd "$ROOT/backend"
  exec uv run -- uvicorn app.main:app --reload --host "$BACKEND_HOST" --port "$BACKEND_PORT"
) &
backend_pid=$!

(
  cd "$ROOT/frontend"
  exec npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --strictPort
) &
frontend_pid=$!

wait "$backend_pid" "$frontend_pid"
