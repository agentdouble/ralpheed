from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
PRD_PATH = ROOT_DIR / "prd.json"
DEFAULT_BRANCH_NAME = "ralph/feature"

load_dotenv(dotenv_path=ROOT_DIR / ".env", override=False)

frontend_host = os.getenv("FRONTEND_HOST", "127.0.0.1")
frontend_port = os.getenv("FRONTEND_PORT", "5173")
cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()

if cors_origins_env:
    allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
else:
    allow_origins = [
        f"http://{frontend_host}:{frontend_port}",
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
    ]

app = FastAPI(title="Ralph Integration API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TaskStatus = Literal["backlog", "plan", "ready", "active", "review", "done"]
ReviewDecision = Literal["approved", "rejected"]
AgentStatus = Literal["waiting", "running"]
AgentSignal = Literal["RALPH_WAITING", "RALPH_RUNNING"]


@dataclass
class TaskState:
    id: str
    title: str
    acceptance_criteria: list[str]
    priority: int
    passes: bool
    notes: str
    status: TaskStatus
    owner: str
    effort: str
    branch: str
    commit: str
    summary: str
    updated_at: datetime


@dataclass
class AgentState:
    status: AgentStatus
    signal: AgentSignal
    last_update: datetime
    current_task_id: str | None


class TaskResponse(BaseModel):
    id: str
    title: str
    acceptance_criteria: list[str]
    priority: int
    passes: bool
    notes: str
    status: TaskStatus
    owner: str
    effort: str
    branch: str
    commit: str
    summary: str
    updated_at: datetime


class AgentResponse(BaseModel):
    status: AgentStatus
    signal: AgentSignal
    last_update: datetime
    current_task_id: str | None


class BoardStateResponse(BaseModel):
    agent: AgentResponse
    tasks: list[TaskResponse]
    logs: list[str]


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: int = Field(default=1, ge=1, le=3)
    passes: bool = False
    notes: str = Field(default="", max_length=2_000)
    status: TaskStatus = "backlog"


class UpdateTaskRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    acceptance_criteria: list[str] | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    passes: bool | None = None
    notes: str | None = Field(default=None, max_length=2_000)
    status: TaskStatus | None = None


class ReviewRequest(BaseModel):
    decision: ReviewDecision


TASKS: dict[str, TaskState] = {}
TASK_ORDER: list[str] = []
TASK_SEQUENCE = 1
BRANCH_NAME = DEFAULT_BRANCH_NAME

STATE_LOCK = asyncio.Lock()
RALPH_WORKER: asyncio.Task[None] | None = None

AGENT = AgentState(
    status="waiting",
    signal="RALPH_WAITING",
    last_update=datetime.now(timezone.utc),
    current_task_id=None,
)

LOGS: list[str] = []
MAX_LOG_LINES = 220
PROCESSING_DELAY_SECONDS = 2.2


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _append_log(message: str) -> None:
    timestamp = _utc_now().strftime("%H:%M:%S")
    LOGS.append(f"{timestamp}  {message}")
    if len(LOGS) > MAX_LOG_LINES:
        excess = len(LOGS) - MAX_LOG_LINES
        del LOGS[:excess]


def _normalize_acceptance(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []
    cleaned: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value:
            cleaned.append(value)
    return cleaned


def _normalize_priority(raw: object) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    if value < 1:
        return 1
    if value > 3:
        return 3
    return value


def _normalize_status(raw: object) -> TaskStatus:
    if isinstance(raw, str) and raw in (
        "backlog",
        "plan",
        "ready",
        "active",
        "review",
        "done",
    ):
        return raw
    return "backlog"


def _extract_sequence(task_id: str) -> int:
    parts = task_id.split("-")
    if len(parts) < 2:
        return 0
    suffix = parts[-1]
    if suffix.isdigit():
        return int(suffix)
    return 0


def _read_prd() -> dict[str, object]:
    if not PRD_PATH.exists():
        return {"branchName": DEFAULT_BRANCH_NAME, "userStories": []}
    return json.loads(PRD_PATH.read_text(encoding="utf-8"))


def _write_prd(data: dict[str, object]) -> None:
    PRD_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = PRD_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(PRD_PATH)


def _task_to_story(task: TaskState) -> dict[str, object]:
    return {
        "id": task.id,
        "title": task.title,
        "acceptanceCriteria": task.acceptance_criteria,
        "priority": task.priority,
        "passes": task.passes,
        "notes": task.notes,
        "status": task.status,
        "owner": task.owner,
        "effort": task.effort,
        "branch": task.branch,
        "commit": task.commit,
        "summary": task.summary,
    }


def _persist_prd() -> None:
    stories = []
    for task_id in TASK_ORDER:
        task = TASKS.get(task_id)
        if task:
            stories.append(_task_to_story(task))
    data = {"branchName": BRANCH_NAME, "userStories": stories}
    _write_prd(data)


def _load_prd_state() -> None:
    global BRANCH_NAME, TASKS, TASK_ORDER, TASK_SEQUENCE

    data = _read_prd()
    branch = data.get("branchName")
    BRANCH_NAME = branch if isinstance(branch, str) and branch.strip() else DEFAULT_BRANCH_NAME

    stories = data.get("userStories")
    if not isinstance(stories, list):
        stories = []

    tasks: dict[str, TaskState] = {}
    order: list[str] = []
    highest = 0

    for raw in stories:
        if not isinstance(raw, dict):
            continue
        story_id = str(raw.get("id") or "").strip()
        if not story_id:
            continue
        title = str(raw.get("title") or "").strip() or story_id
        acceptance_criteria = _normalize_acceptance(raw.get("acceptanceCriteria"))
        priority = _normalize_priority(raw.get("priority"))
        passes = raw.get("passes") is True
        notes = str(raw.get("notes") or "")
        status = _normalize_status(raw.get("status"))
        owner = str(raw.get("owner") or "")
        effort = str(raw.get("effort") or "")
        branch = str(raw.get("branch") or "")
        commit = str(raw.get("commit") or "")
        summary = str(raw.get("summary") or "")
        updated_at = _utc_now()

        tasks[story_id] = TaskState(
            id=story_id,
            title=title,
            acceptance_criteria=acceptance_criteria,
            priority=priority,
            passes=passes,
            notes=notes,
            status=status,
            owner=owner,
            effort=effort,
            branch=branch,
            commit=commit,
            summary=summary,
            updated_at=updated_at,
        )
        order.append(story_id)
        highest = max(highest, _extract_sequence(story_id))

    TASKS = tasks
    TASK_ORDER = order
    TASK_SEQUENCE = highest + 1 if highest else max(len(order) + 1, 1)


def _task_to_response(task: TaskState) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        title=task.title,
        acceptance_criteria=task.acceptance_criteria,
        priority=task.priority,
        passes=task.passes,
        notes=task.notes,
        status=task.status,
        owner=task.owner,
        effort=task.effort,
        branch=task.branch,
        commit=task.commit,
        summary=task.summary,
        updated_at=task.updated_at,
    )


def _agent_to_response() -> AgentResponse:
    return AgentResponse(
        status=AGENT.status,
        signal=AGENT.signal,
        last_update=AGENT.last_update,
        current_task_id=AGENT.current_task_id,
    )


def _board_state_response() -> BoardStateResponse:
    tasks = [TASKS[task_id] for task_id in TASK_ORDER if task_id in TASKS]
    return BoardStateResponse(
        agent=_agent_to_response(),
        tasks=[_task_to_response(task) for task in tasks],
        logs=list(LOGS),
    )


def _generate_story_id() -> str:
    global TASK_SEQUENCE

    while True:
        candidate = f"US-{TASK_SEQUENCE:03d}"
        TASK_SEQUENCE += 1
        if candidate not in TASKS:
            return candidate


def _suggest_effort(priority: int) -> str:
    if priority <= 1:
        return "2d"
    if priority == 2:
        return "1d"
    return "0.5d"


def _suggest_branch(task_id: str) -> str:
    suffix = uuid4().hex[:4]
    return f"feat/{task_id.lower()}-{suffix}"


def _suggest_commit() -> str:
    return uuid4().hex[:7]


def _set_agent_waiting() -> None:
    AGENT.status = "waiting"
    AGENT.signal = "RALPH_WAITING"
    AGENT.current_task_id = None
    AGENT.last_update = _utc_now()


def _set_agent_running(task_id: str | None) -> None:
    AGENT.status = "running"
    AGENT.signal = "RALPH_RUNNING"
    AGENT.current_task_id = task_id
    AGENT.last_update = _utc_now()


def _next_ready_task() -> TaskState | None:
    for task_id in TASK_ORDER:
        task = TASKS.get(task_id)
        if task and task.status == "ready":
            return task
    return None


async def _ralph_worker_loop() -> None:
    try:
        while True:
            async with STATE_LOCK:
                task = _next_ready_task()
                if not task:
                    _set_agent_waiting()
                    _append_log("RALPH_WAITING - No tasks in ready queue")
                    return

                task.status = "active"
                task.owner = task.owner or "Ralph"
                task.updated_at = _utc_now()
                _set_agent_running(task.id)
                _append_log(f"RALPH_RUNNING - Started {task.id}: {task.title}")
                _persist_prd()

            await asyncio.sleep(PROCESSING_DELAY_SECONDS)

            async with STATE_LOCK:
                refreshed = TASKS.get(task.id)
                if not refreshed or refreshed.status != "active":
                    _append_log(f"RALPH_WARNING - Skipped {task.id} (task changed during processing)")
                    continue

                refreshed.effort = refreshed.effort or _suggest_effort(refreshed.priority)
                refreshed.branch = refreshed.branch or _suggest_branch(refreshed.id)
                refreshed.commit = refreshed.commit or _suggest_commit()
                refreshed.summary = refreshed.summary or "Auto-filled by Ralph."
                refreshed.status = "review"
                refreshed.updated_at = _utc_now()

                _append_log(f"RALPH_REVIEW - Ready: {refreshed.id} waiting for human review")
                _set_agent_running(None)
                _persist_prd()

                if not any(task.status == "ready" for task in TASKS.values()):
                    _set_agent_waiting()
                    _append_log("RALPH_WAITING - Queue empty")
                    return
    except Exception as exc:  # pragma: no cover
        async with STATE_LOCK:
            _append_log(f"RALPH_ERROR - {exc.__class__.__name__}: {exc}")
            _set_agent_waiting()


_load_prd_state()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/state", response_model=BoardStateResponse)
async def get_state() -> BoardStateResponse:
    async with STATE_LOCK:
        return _board_state_response()


@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(payload: CreateTaskRequest) -> TaskResponse:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    acceptance_criteria = _normalize_acceptance(payload.acceptance_criteria)

    async with STATE_LOCK:
        task_id = _generate_story_id()

        now = _utc_now()
        task = TaskState(
            id=task_id,
            title=title,
            acceptance_criteria=acceptance_criteria,
            priority=payload.priority,
            passes=payload.passes,
            notes=payload.notes.strip(),
            status=payload.status,
            owner="",
            effort="",
            branch="",
            commit="",
            summary="",
            updated_at=now,
        )
        TASKS[task_id] = task
        TASK_ORDER.append(task_id)
        _append_log(f"TASK_CREATED - {task_id} {title}")
        _persist_prd()
        return _task_to_response(task)


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, payload: UpdateTaskRequest) -> TaskResponse:
    async with STATE_LOCK:
        task = TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if payload.title is not None:
            title = payload.title.strip()
            if not title:
                raise HTTPException(status_code=400, detail="Title is required")
            task.title = title

        if payload.acceptance_criteria is not None:
            task.acceptance_criteria = _normalize_acceptance(payload.acceptance_criteria)

        if payload.priority is not None:
            task.priority = payload.priority

        if payload.passes is not None:
            task.passes = payload.passes

        if payload.notes is not None:
            task.notes = payload.notes.strip()

        if payload.status is not None:
            previous_status = task.status
            task.status = payload.status
            if payload.status == "ready" and previous_status != "ready":
                _append_log(f"QUEUE_READY - {task.id} queued for Ralph")
            elif previous_status != payload.status:
                _append_log(f"STATUS_CHANGE - {task.id} {previous_status} -> {payload.status}")

            if payload.status != "active" and AGENT.current_task_id == task.id:
                _set_agent_running(None)

        task.updated_at = _utc_now()
        _persist_prd()
        return _task_to_response(task)


@app.post("/api/tasks/{task_id}/review", response_model=TaskResponse)
async def review_task(task_id: str, payload: ReviewRequest) -> TaskResponse:
    async with STATE_LOCK:
        task = TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "review":
            raise HTTPException(status_code=409, detail="Task is not awaiting review")

        if payload.decision == "approved":
            task.status = "done"
            task.passes = True
            _append_log(f"REVIEW_APPROVED - {task.id} moved to done")
        else:
            task.status = "backlog"
            task.passes = False
            _append_log(f"REVIEW_REJECTED - {task.id} returned to backlog")

        task.updated_at = _utc_now()
        _persist_prd()
        return _task_to_response(task)


@app.post("/api/ralph/start", response_model=BoardStateResponse)
async def start_ralph() -> BoardStateResponse:
    global RALPH_WORKER

    async with STATE_LOCK:
        if RALPH_WORKER and not RALPH_WORKER.done():
            return _board_state_response()

        _append_log("RALPH_START - Requested")
        RALPH_WORKER = asyncio.create_task(_ralph_worker_loop())
        return _board_state_response()


@app.post("/api/agents/start-all", response_model=BoardStateResponse)
async def start_all_agents() -> BoardStateResponse:
    async with STATE_LOCK:
        _append_log("AGENTS_START_ALL - Requested (stub)")
        return _board_state_response()


@app.post("/api/pull-latest", response_model=BoardStateResponse)
async def pull_latest() -> BoardStateResponse:
    async with STATE_LOCK:
        _append_log("PULL_LATEST - Requested (stub)")
        return _board_state_response()


@app.post("/api/logs/clear", response_model=BoardStateResponse)
async def clear_logs() -> BoardStateResponse:
    async with STATE_LOCK:
        LOGS.clear()
        _append_log("LOGS_CLEARED")
        return _board_state_response()
