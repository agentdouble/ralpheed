# Ralph Integration Desk

Mini dashboard to manage multiple Ralph agents in a kanban flow: queue work per agent, then review and approve/reject. Edit or delete tasks directly from the board. Tasks persist to workspace-scoped PRD files.

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
`start.sh` stops any existing listeners on the backend/frontend ports before starting (uses `lsof` when available).

When you click Start Ralph (or call `POST /api/ralph/start`), the backend launches `ralph.sh` to run Codex iterations in a git worktree created for the task branch.
Ralph reads and updates the PRD file for the agent workspace (under `~/.ralpheed/prd/.../prd.json`) plus `~/.ralpheed/prd/.../progress.txt`, and the workspace path must point to a valid git repository.
New task branches are created from the latest `dev` fetched from the default remote (origin when available).
AI task generation uses the workspace repo README for context when `workspacePath` is set; otherwise it falls back to this repo README.
When a workspace is set, Ralph reads `AGENTS.md` from the workspace root if it exists (otherwise it uses this repo's `AGENTS.md`).
Worktrees are created under a sibling folder named `<repo>-worktrees`, and all `.env`/`.env.*` files are copied into each worktree with their relative paths preserved.
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
Agents are indexed in `~/.ralpheed/prd/index.json`. Task state is stored per workspace under `~/.ralpheed/prd/<workspace>/prd.json`, where `<workspace>` is derived from the git root of the configured workspace path. When no workspace is set, the agent uses `~/.ralpheed/prd/default/prd.json`. On first run, legacy PRD data is migrated into the workspace files and index. Multi-agent state is stored under `agents`, each with `id`, `name`, `branchName`, `workspacePath`, and `userStories`. Story ids are auto-generated (`US-###`) and priority defaults to P1. The UI adds optional fields per story (`status`, `owner`, `effort`, `worktree`, `waitForValidation`, `branch`, `commit`, `summary`) to keep board state and Ralph output.

## API quick reference
- When `agent_id` is omitted, the API uses the first agent in the index.
- `GET /api/agents`
- `POST /api/agents`
- `GET /api/state?agent_id=agent-1`
- `POST /api/workspace?agent_id=agent-1`
  - body: `{ "path": "/absolute/path" }`
- `POST /api/acceptance-criteria`
  - body: `{ "title": "..." }`
  - response: `{ "criteria": ["...", "..."] }`
- `POST /api/tasks/ai?agent_id=agent-1`
  - body: `{ "count": 3, "theme": "..." }`
  - response: `{ "tasks": [ ... ] }`
- `POST /api/tasks?agent_id=agent-1`
  - body: `{ "title": "...", "acceptance_criteria": ["..."], "priority": 1-3, "passes": false, "notes": "...", "status": "backlog|todo|review|done", "worktree": "feature/epic", "wait_for_validation": false }`
- `PATCH /api/tasks/{task_id}?agent_id=agent-1`
  - body: `{ "status": "backlog|todo|review|done", "priority": 1-3, "passes": true, "notes": "...", "worktree": "feature/epic", "wait_for_validation": false, ... }`
- `POST /api/tasks/{task_id}/codex?agent_id=agent-1` (review only; opens iTerm running Codex in the task worktree)
- `POST /api/tasks/{task_id}/start?agent_id=agent-1` (review only; opens iTerm and runs `./start.sh` in the task worktree)
- `POST /api/tasks/{task_id}/openpr?agent_id=agent-1` (review only; runs the `openpr` prompt via Codex; uses `prompts/openpr.md` or `~/.codex/prompts/openpr.md` when present, otherwise sends `/prompts:openpr`)
- `POST /api/tasks/{task_id}/openpr/stop?agent_id=agent-1`

Ralph only processes tasks in `todo`.
Tasks with the same `worktree` share a git worktree; Ralph processes them by priority, and `wait_for_validation` marks the principal tache, blocking later tasks until it passes or reaches `review`.
Setting `passes` to true moves a `todo` task to `review` automatically.
Ralph processes todo tasks in rounds (up to the `iterations` count); tasks that stay `todo` are retried in the next round.
Approving a review automatically triggers the OpenPR flow (the OpenPR button remains available for manual runs).
The progress log UI shows per-agent tabs (All, Ralph, OpenPR per task) when corresponding logs exist.
Dragging a task from Review to Done approves it (and triggers OpenPR).
Tasks show running Ralph/OpenPR badges; clicking them jumps to the matching log tab.
OpenPR tabs can be closed; closing a running tab stops that subagent.
The AI Criteria button can populate missing acceptance criteria across tasks.
The AI Tasks button can create backlog tasks from a theme (or project context).
- `DELETE /api/tasks/{task_id}?agent_id=agent-1`
- `POST /api/ralph/start?agent_id=agent-1`
  - body (optional): `{ "iterations": 10 }`
- `POST /api/ralph/stop?agent_id=agent-1`
- `POST /api/openpr/stop?agent_id=agent-1`
- `POST /api/tasks/{task_id}/review?agent_id=agent-1`
  - body: `{ "decision": "approved|rejected" }`
- `POST /api/pull-latest?agent_id=agent-1` (stub)
- `POST /api/logs/clear?agent_id=agent-1`
- `POST /api/agents/start-all`
  - body (optional): `{ "iterations": 10 }`

Note: while Ralph is running, task/workspace mutations return 409 to avoid concurrent writes to PRD files.
Note: workspace changes return 409 while OpenPR is running.
Note: on macOS, the Codex and Start buttons require iTerm to be installed.
Note: `POST /api/acceptance-criteria` runs Codex in the background and requires the `codex` CLI.
Note: `POST /api/tasks/ai` runs Codex in the background and requires the `codex` CLI.
