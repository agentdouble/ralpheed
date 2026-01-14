# Ralph Integration Desk

Mini dashboard to manage multiple Ralph agents in a kanban flow: queue work per agent, then review and approve/reject. Edit or delete tasks directly from the board. Tasks persist to `prd.json`.

## Structure
- `frontend/`: React + Vite dashboard
- `backend/`: FastAPI API
- `start.sh`: starts backend + frontend together
- `.env*`: ports and CORS config

## Requirements
- Python 3.11+
- `uv`
- Node 18+ and npm

## Setup
Backend deps (uv):
```
cd backend
uv sync
```

Frontend deps:
```
cd frontend
npm install
```

## Run
Always start the app via `start.sh`:
```
./start.sh
```

When you click Start Ralph (or call `POST /api/ralph/start`), the backend launches `ralph.sh` to run Codex iterations in a git worktree created for the task branch.
Ralph reads and updates `prd.json`/`progress.txt` from this repo, and the workspace path must point to a valid git repository.
Worktrees are created under a sibling folder named `<repo>-worktrees`, and `.env`/`.env.*` files are copied into each worktree.
`ralph.sh` runs Codex non-interactively with approvals bypassed so runs do not pause for follow-ups.

## Config
Edit `.env` to change ports or CORS origins:
```
BACKEND_HOST=127.0.0.1
BACKEND_PORT=8000
FRONTEND_HOST=127.0.0.1
FRONTEND_PORT=5173
CORS_ORIGINS=
```

## PRD storage
The app reads/writes `prd.json`. Multi-agent state is stored under `agents`, each with `id`, `name`, `branchName`, `workspacePath`, and `userStories`. Story ids are auto-generated (`US-###`) and priority defaults to P1. The UI adds optional fields per story (`status`, `owner`, `effort`, `branch`, `commit`, `summary`) to keep board state and Ralph output. Legacy single-agent files are auto-migrated on first write.

## API quick reference
- When `agent_id` is omitted, the API uses the first agent in `prd.json`.
- `GET /api/agents`
- `POST /api/agents`
- `GET /api/state?agent_id=agent-1`
- `POST /api/workspace?agent_id=agent-1`
  - body: `{ "path": "/absolute/path" }`
- `POST /api/tasks?agent_id=agent-1`
  - body: `{ "title": "...", "acceptance_criteria": ["..."], "priority": 1-3, "passes": false, "notes": "...", "status": "backlog|todo|review|done" }`
- `PATCH /api/tasks/{task_id}?agent_id=agent-1`
  - body: `{ "status": "backlog|todo|review|done", "priority": 1-3, "passes": true, "notes": "...", ... }`
- `POST /api/tasks/{task_id}/codex?agent_id=agent-1` (review only; opens iTerm running Codex in the task worktree)
- `POST /api/tasks/{task_id}/openpr?agent_id=agent-1` (review only; runs the `openpr` prompt via Codex; uses `prompts/openpr.md` or `~/.codex/prompts/openpr.md` when present, otherwise sends `/prompts:openpr`)

Note: when a task is marked `passes: true`, its status is automatically moved to `review` (unless already `done`).
Ralph only processes tasks in `todo`.
Ralph processes todo tasks in rounds (up to the `iterations` count); tasks that stay `todo` are retried in the next round.
- `DELETE /api/tasks/{task_id}?agent_id=agent-1`
- `POST /api/ralph/start?agent_id=agent-1`
  - body (optional): `{ "iterations": 10 }`
- `POST /api/tasks/{task_id}/review?agent_id=agent-1`
  - body: `{ "decision": "approved|rejected" }`
- `POST /api/pull-latest?agent_id=agent-1` (stub)
- `POST /api/logs/clear?agent_id=agent-1`
- `POST /api/agents/start-all`
  - body (optional): `{ "iterations": 10 }`

Note: while Ralph is running, task/workspace mutations return 409 to avoid concurrent writes to `prd.json`.
Note: on macOS, the Codex button requires iTerm to be installed.
