# Ralph Integration Desk

Mini dashboard to manage Ralph integration tasks in a kanban flow: queue work for Ralph, then review and approve/reject. Tasks persist to `prd.json`.

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
The app reads/writes `prd.json`. Story ids are auto-generated (`US-###`) and priority defaults to P1. The UI adds optional fields per story (`status`, `owner`, `effort`, `branch`, `commit`, `summary`) to keep board state and Ralph output. The Ralph workspace path is stored as `workspacePath`.

## API quick reference
- `GET /api/state`
- `POST /api/workspace`
  - body: `{ "path": "/absolute/path" }`
- `POST /api/tasks`
  - body: `{ "title": "...", "acceptance_criteria": ["..."], "priority": 1-3, "passes": false, "notes": "...", "status": "backlog|plan|ready|review|done" }`
- `PATCH /api/tasks/{task_id}`
  - body: `{ "status": "backlog|plan|ready|review|done", "priority": 1-3, "passes": true, "notes": "...", ... }`
- `POST /api/ralph/start`
- `POST /api/tasks/{task_id}/review`
  - body: `{ "decision": "approved|rejected" }`
- `POST /api/pull-latest` (stub)
- `POST /api/logs/clear`
- `POST /api/agents/start-all` (stub)
