from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
PRD_PATH = ROOT_DIR / "prd.json"
RALPH_SCRIPT_PATH = ROOT_DIR / "ralph.sh"
DEFAULT_BRANCH_NAME = "ralph/feature"
DEFAULT_AGENT_ID = "agent-1"
DEFAULT_AGENT_NAME = "Ralph"
MAX_LOG_LINES = 220

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

TaskStatus = Literal["backlog", "todo", "review", "done"]
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


@dataclass
class AgentBoard:
    id: str
    name: str
    branch_name: str
    workspace_path: str
    tasks: dict[str, TaskState]
    order: list[str]
    task_sequence: int
    agent: AgentState
    logs: list[str]
    worker: asyncio.Task[None] | None = None


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
    id: str
    name: str
    status: AgentStatus
    signal: AgentSignal
    last_update: datetime
    current_task_id: str | None


class AgentSummary(BaseModel):
    id: str
    name: str
    status: AgentStatus
    current_task_id: str | None


class AgentsResponse(BaseModel):
    agents: list[AgentSummary]


class BoardStateResponse(BaseModel):
    agent: AgentResponse
    tasks: list[TaskResponse]
    logs: list[str]
    workspace_path: str


class CreateAgentRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)


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


class WorkspaceRequest(BaseModel):
    path: str = Field(default="", max_length=512)


class StartRalphRequest(BaseModel):
    iterations: int | None = Field(default=None, ge=1)


AGENTS: dict[str, AgentBoard] = {}
AGENT_ORDER: list[str] = []
AGENT_SEQUENCE = 1

STATE_LOCK = asyncio.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _append_log(board: AgentBoard, message: str) -> None:
    timestamp = _utc_now().strftime("%H:%M:%S")
    board.logs.append(f"{timestamp}  {message}")
    if len(board.logs) > MAX_LOG_LINES:
        excess = len(board.logs) - MAX_LOG_LINES
        del board.logs[:excess]


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
    if not isinstance(raw, str):
        return "backlog"
    if raw in ("backlog", "todo", "review", "done"):
        return raw
    if raw in ("plan", "ready", "active"):
        return "todo"
    return "backlog"


def _extract_sequence(task_id: str) -> int:
    parts = task_id.split("-")
    if len(parts) < 2:
        return 0
    suffix = parts[-1]
    if suffix.isdigit():
        return int(suffix)
    return 0


def _extract_agent_sequence(agent_id: str) -> int:
    parts = agent_id.split("-")
    if len(parts) < 2:
        return 0
    suffix = parts[-1]
    if suffix.isdigit():
        return int(suffix)
    return 0


def _default_agent_name(sequence: int) -> str:
    if sequence <= 1:
        return DEFAULT_AGENT_NAME
    return f"{DEFAULT_AGENT_NAME} {sequence}"


def _read_prd() -> dict[str, object]:
    if not PRD_PATH.exists():
        return {"agents": []}
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
    agents_payload: list[dict[str, object]] = []
    for agent_id in AGENT_ORDER:
        board = AGENTS.get(agent_id)
        if not board:
            continue
        stories = []
        for task_id in board.order:
            task = board.tasks.get(task_id)
            if task:
                stories.append(_task_to_story(task))
        agents_payload.append(
            {
                "id": board.id,
                "name": board.name,
                "branchName": board.branch_name,
                "workspacePath": board.workspace_path,
                "userStories": stories,
            }
        )
    _write_prd({"agents": agents_payload})


def _any_worker_running() -> bool:
    return any(board.worker and not board.worker.done() for board in AGENTS.values())


def _ensure_mutation_allowed() -> None:
    if _any_worker_running():
        raise HTTPException(status_code=409, detail="Ralph is running")


def _resolve_workspace_path(board: AgentBoard) -> Path | None:
    raw = board.workspace_path.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    if not path.is_dir():
        return None
    return path


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _resolve_repo_root(path: Path) -> Path | None:
    result = _run_git(["git", "rev-parse", "--show-toplevel"], path)
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip())
    if not root.is_dir():
        return None
    return root


def _worktrees_root(repo_root: Path) -> Path:
    return repo_root.parent / f"{repo_root.name}-worktrees"


def _sanitize_branch_name(branch: str) -> str:
    cleaned = branch.strip().replace("/", "__")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned or "worktree"


def _default_branch_for_task(task: TaskState) -> str:
    return f"feat/{task.id.lower()}"


async def _ensure_worktree_path(repo_root: Path, branch: str) -> tuple[Path | None, str | None]:
    worktrees_root = _worktrees_root(repo_root)
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktrees_root / _sanitize_branch_name(branch)

    if worktree_path.exists():
        if not worktree_path.is_dir():
            return None, f"invalid path {worktree_path}"

        result = await asyncio.to_thread(
            _run_git,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            worktree_path,
        )
        if result.returncode != 0:
            error = result.stderr.strip() or "unable to read worktree"
            return None, error
        current_branch = result.stdout.strip()
        if current_branch != branch:
            return None, f"{worktree_path} on {current_branch}, expected {branch}"
        return worktree_path, None

    exists_result = await asyncio.to_thread(
        _run_git,
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        repo_root,
    )
    if exists_result.returncode == 0:
        cmd = ["git", "worktree", "add", str(worktree_path), branch]
    else:
        cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path)]
    result = await asyncio.to_thread(_run_git, cmd, repo_root)
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip() or "unable to create worktree"
        return None, error
    return worktree_path, None


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _launch_codex_terminal(worktree_path: Path) -> tuple[bool, str]:
    if not shutil.which("codex"):
        return False, "codex not found"

    command = f"cd {shlex.quote(str(worktree_path))} && codex"
    if sys.platform == "darwin":
        script = (
            'tell application "iTerm"\n'
            "activate\n"
            "set newWindow to (create window with default profile)\n"
            f'tell current session of newWindow to write text "{_escape_osascript(command)}"\n'
            "end tell"
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            error = result.stderr.strip() or "unable to open iTerm"
            return False, error
        return True, ""

    if sys.platform.startswith("linux"):
        terminal = (
            shutil.which("x-terminal-emulator")
            or shutil.which("gnome-terminal")
            or shutil.which("konsole")
            or shutil.which("xterm")
        )
        if not terminal:
            return False, "no terminal available"
        if terminal.endswith("gnome-terminal"):
            args = [terminal, "--", "bash", "-lc", command]
        else:
            args = [terminal, "-e", "bash", "-lc", command]
        subprocess.Popen(args, cwd=str(worktree_path))
        return True, ""

    if sys.platform == "win32":
        args = ["cmd", "/c", "start", "cmd", "/k", f"cd /d {worktree_path} && codex"]
        subprocess.Popen(args)
        return True, ""

    return False, "unsupported platform"


def _resolve_codex_prompt(name: str) -> tuple[str | None, Path | None]:
    candidates = [
        ROOT_DIR / "prompts" / f"{name}.md",
        Path.home() / ".codex" / "prompts" / f"{name}.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8"), candidate
            except OSError:
                continue
    return None, None


def _copy_env_files(source: Path, dest: Path) -> list[str]:
    copied: list[str] = []
    if not source.is_dir() or not dest.is_dir():
        return copied
    for item in source.iterdir():
        if not item.is_file():
            continue
        name = item.name
        if name == ".env" or name.startswith(".env."):
            shutil.copy2(item, dest / name)
            copied.append(name)
    return copied


def _select_task_for_run(board: AgentBoard) -> TaskState | None:
    best: TaskState | None = None
    best_key: tuple[int, int] | None = None
    for index, task_id in enumerate(board.order):
        task = board.tasks.get(task_id)
        if not task or task.passes or task.status != "todo":
            continue
        key = (task.priority, index)
        if best_key is None or key < best_key:
            best = task
            best_key = key
    return best


def _sync_board_from_prd(board: AgentBoard) -> None:
    data = _read_prd()
    agents_raw = data.get("agents")
    if isinstance(agents_raw, list) and agents_raw:
        agent_items = agents_raw
    else:
        agent_items = [
            {
                "id": DEFAULT_AGENT_ID,
                "name": DEFAULT_AGENT_NAME,
                "branchName": data.get("branchName"),
                "workspacePath": data.get("workspacePath"),
                "userStories": data.get("userStories"),
            }
        ]

    for index, raw in enumerate(agent_items, start=1):
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("id") or "").strip()
        agent_id = raw_id or (DEFAULT_AGENT_ID if index == 1 else f"agent-{index}")
        if agent_id != board.id:
            continue

        raw_name = str(raw.get("name") or "").strip()
        sequence = _extract_agent_sequence(agent_id) or index
        name = raw_name or _default_agent_name(sequence)

        branch_raw = raw.get("branchName")
        if isinstance(branch_raw, str) and branch_raw.strip():
            branch_name = branch_raw.strip()
        else:
            branch_name = DEFAULT_BRANCH_NAME

        workspace_raw = raw.get("workspacePath")
        workspace_path = workspace_raw if isinstance(workspace_raw, str) else ""

        stories_raw = raw.get("userStories")
        stories = stories_raw if isinstance(stories_raw, list) else []

        tasks, order, sequence = _load_tasks_from_stories(stories)
        board.name = name
        board.branch_name = branch_name
        board.workspace_path = workspace_path
        board.tasks = tasks
        board.order = order
        board.task_sequence = sequence
        return


def _load_tasks_from_stories(stories: list[object]) -> tuple[dict[str, TaskState], list[str], int]:
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
        if passes and status != "done":
            status = "review"
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

    sequence = highest + 1 if highest else max(len(order) + 1, 1)
    return tasks, order, sequence


def _create_board(
    agent_id: str,
    name: str,
    branch_name: str,
    workspace_path: str,
    stories: list[object],
) -> AgentBoard:
    tasks, order, sequence = _load_tasks_from_stories(stories)
    agent_state = AgentState(
        status="waiting",
        signal="RALPH_WAITING",
        last_update=_utc_now(),
        current_task_id=None,
    )
    return AgentBoard(
        id=agent_id,
        name=name,
        branch_name=branch_name,
        workspace_path=workspace_path,
        tasks=tasks,
        order=order,
        task_sequence=sequence,
        agent=agent_state,
        logs=[],
    )


def _load_prd_state() -> None:
    global AGENTS, AGENT_ORDER, AGENT_SEQUENCE

    data = _read_prd()
    agents_raw = data.get("agents")
    if isinstance(agents_raw, list) and agents_raw:
        agent_items = agents_raw
    else:
        agent_items = [
            {
                "id": DEFAULT_AGENT_ID,
                "name": DEFAULT_AGENT_NAME,
                "branchName": data.get("branchName"),
                "workspacePath": data.get("workspacePath"),
                "userStories": data.get("userStories"),
            }
        ]

    agents: dict[str, AgentBoard] = {}
    order: list[str] = []
    highest = 0

    for index, raw in enumerate(agent_items, start=1):
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("id") or "").strip()
        agent_id = raw_id or (DEFAULT_AGENT_ID if index == 1 else f"agent-{index}")
        if agent_id in agents:
            continue
        raw_name = str(raw.get("name") or "").strip()
        sequence = _extract_agent_sequence(agent_id) or index
        name = raw_name or _default_agent_name(sequence)
        branch_raw = raw.get("branchName")
        if isinstance(branch_raw, str) and branch_raw.strip():
            branch_name = branch_raw.strip()
        else:
            branch_name = DEFAULT_BRANCH_NAME
        workspace_raw = raw.get("workspacePath")
        workspace_path = workspace_raw if isinstance(workspace_raw, str) else ""
        stories_raw = raw.get("userStories")
        stories = stories_raw if isinstance(stories_raw, list) else []

        board = _create_board(agent_id, name, branch_name, workspace_path, stories)
        agents[agent_id] = board
        order.append(agent_id)
        highest = max(highest, _extract_agent_sequence(agent_id))

    if not agents:
        board = _create_board(DEFAULT_AGENT_ID, DEFAULT_AGENT_NAME, DEFAULT_BRANCH_NAME, "", [])
        agents[DEFAULT_AGENT_ID] = board
        order = [DEFAULT_AGENT_ID]
        highest = _extract_agent_sequence(DEFAULT_AGENT_ID)

    AGENTS = agents
    AGENT_ORDER = order
    AGENT_SEQUENCE = highest + 1 if highest else len(order) + 1


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


def _agent_to_response(board: AgentBoard) -> AgentResponse:
    return AgentResponse(
        id=board.id,
        name=board.name,
        status=board.agent.status,
        signal=board.agent.signal,
        last_update=board.agent.last_update,
        current_task_id=board.agent.current_task_id,
    )


def _agent_to_summary(board: AgentBoard) -> AgentSummary:
    return AgentSummary(
        id=board.id,
        name=board.name,
        status=board.agent.status,
        current_task_id=board.agent.current_task_id,
    )


def _board_state_response(board: AgentBoard) -> BoardStateResponse:
    tasks = [board.tasks[task_id] for task_id in board.order if task_id in board.tasks]
    return BoardStateResponse(
        agent=_agent_to_response(board),
        tasks=[_task_to_response(task) for task in tasks],
        logs=list(board.logs),
        workspace_path=board.workspace_path,
    )


def _generate_story_id(board: AgentBoard) -> str:
    while True:
        candidate = f"US-{board.task_sequence:03d}"
        board.task_sequence += 1
        if candidate not in board.tasks:
            return candidate


def _set_agent_waiting(board: AgentBoard) -> None:
    board.agent.status = "waiting"
    board.agent.signal = "RALPH_WAITING"
    board.agent.current_task_id = None
    board.agent.last_update = _utc_now()


def _set_agent_running(board: AgentBoard, task_id: str | None) -> None:
    board.agent.status = "running"
    board.agent.signal = "RALPH_RUNNING"
    board.agent.current_task_id = task_id
    board.agent.last_update = _utc_now()


def _resolve_agent_id(agent_id: str | None) -> str:
    candidate = (agent_id or "").strip()
    if candidate:
        return candidate
    if AGENT_ORDER:
        return AGENT_ORDER[0]
    return DEFAULT_AGENT_ID


def _get_board(agent_id: str | None) -> AgentBoard:
    resolved = _resolve_agent_id(agent_id)
    board = AGENTS.get(resolved)
    if not board:
        raise HTTPException(status_code=404, detail="Agent not found")
    return board


def _next_agent_id() -> str:
    global AGENT_SEQUENCE

    while True:
        candidate = f"agent-{AGENT_SEQUENCE}"
        AGENT_SEQUENCE += 1
        if candidate not in AGENTS:
            return candidate


def _start_worker(board: AgentBoard, iterations: int | None = None) -> None:
    if board.worker and not board.worker.done():
        return
    board.worker = asyncio.create_task(_agent_worker_loop(board.id, iterations))


def _start_openpr_worker(board: AgentBoard, task_id: str) -> None:
    if board.worker and not board.worker.done():
        return
    board.worker = asyncio.create_task(_openpr_worker_loop(board.id, task_id))


async def _agent_worker_loop(agent_id: str, iterations: int | None = None) -> None:
    proc: asyncio.subprocess.Process | None = None
    workspace_path: Path | None = None
    try:
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return

            if not RALPH_SCRIPT_PATH.exists():
                _append_log(board, "RALPH_ERROR - ralph.sh not found")
                _set_agent_waiting(board)
                return

            workspace_path = _resolve_workspace_path(board)
            if not workspace_path:
                _append_log(board, "WORKSPACE_INVALID - Set a valid workspace path before starting.")
                _set_agent_waiting(board)
                return

        repo_root = await asyncio.to_thread(_resolve_repo_root, workspace_path)
        if not repo_root:
            async with STATE_LOCK:
                board = AGENTS.get(agent_id)
                if not board:
                    return
                _append_log(board, "WORKSPACE_INVALID - Not a git repository")
                _set_agent_waiting(board)
            return

        worktrees_root = _worktrees_root(repo_root)
        worktrees_root.mkdir(parents=True, exist_ok=True)

        max_rounds = iterations if iterations is not None else 1
        round_index = 0

        while True:
            async with STATE_LOCK:
                board = AGENTS.get(agent_id)
                if not board:
                    return

                todo_ids = [
                    task_id
                    for task_id in board.order
                    if (task := board.tasks.get(task_id)) and task.status == "todo" and not task.passes
                ]

                if not todo_ids:
                    _set_agent_waiting(board)
                    _append_log(board, "RALPH_WAITING - No todo tasks")
                    return

                if round_index >= max_rounds:
                    _set_agent_waiting(board)
                    _append_log(board, "RALPH_WAITING - Max rounds reached")
                    return

                round_index += 1
                _append_log(board, f"RALPH_ROUND - {round_index}/{max_rounds} ({len(todo_ids)} tasks)")

            for task_id in todo_ids:
                async with STATE_LOCK:
                    board = AGENTS.get(agent_id)
                    if not board:
                        return

                    task = board.tasks.get(task_id)
                    if not task or task.status != "todo" or task.passes:
                        continue

                    branch = task.branch.strip() or _default_branch_for_task(task)
                    if branch != task.branch:
                        task.branch = branch
                        task.updated_at = _utc_now()
                        _append_log(board, f"BRANCH_SET - {task.id} {branch}")
                        _persist_prd()

                    _set_agent_running(board, task.id)
                    _append_log(board, f"RALPH_PREP - {task.id} on {branch}")

                worktree_path, error = await _ensure_worktree_path(repo_root, branch)
                if not worktree_path:
                    async with STATE_LOCK:
                        board = AGENTS.get(agent_id)
                        if not board:
                            return
                        _append_log(board, f"WORKTREE_ERROR - {error}")
                        _set_agent_waiting(board)
                    return

                copied = _copy_env_files(repo_root, worktree_path)

                async with STATE_LOCK:
                    board = AGENTS.get(agent_id)
                    if board:
                        if copied:
                            _append_log(board, f"ENV_SYNC - {', '.join(copied)}")
                        if iterations is None:
                            _append_log(board, f"RALPH_RUNNING - Script started in {worktree_path}")
                        else:
                            _append_log(
                                board,
                                f"RALPH_RUNNING - Script started in {worktree_path} ({iterations} iterations)",
                            )

                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash",
                    str(RALPH_SCRIPT_PATH),
                    *([str(iterations)] if iterations is not None else []),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(worktree_path),
                )

                if proc.stdout:
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        text = line.decode(errors="replace").rstrip()
                        if not text:
                            continue
                        async with STATE_LOCK:
                            board = AGENTS.get(agent_id)
                            if not board:
                                continue
                            _append_log(board, text)

                return_code = await proc.wait()

                async with STATE_LOCK:
                    board = AGENTS.get(agent_id)
                    if not board:
                        return
                    _sync_board_from_prd(board)
                    updated = board.tasks.get(task_id)
                    if updated and not updated.passes and updated.status == "todo":
                        _append_log(board, f"RALPH_REQUEUE - {task_id} still todo")
                    _append_log(board, f"RALPH_DONE - {task_id} exit {return_code}")
                    _set_agent_running(board, None)
    except Exception as exc:  # pragma: no cover
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return
            _append_log(board, f"RALPH_ERROR - {exc.__class__.__name__}: {exc}")
            _set_agent_waiting(board)
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


async def _openpr_worker_loop(agent_id: str, task_id: str) -> None:
    proc: asyncio.subprocess.Process | None = None
    workspace_path: Path | None = None
    branch = ""
    try:
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return
            task = board.tasks.get(task_id)
            if not task:
                _append_log(board, f"OPENPR_ERROR - Task not found {task_id}")
                _set_agent_waiting(board)
                return
            if task.status != "review":
                _append_log(board, f"OPENPR_ERROR - {task.id} not in review")
                _set_agent_waiting(board)
                return

            workspace_path = _resolve_workspace_path(board)
            if not workspace_path:
                _append_log(board, "WORKSPACE_INVALID - Set a valid workspace path before starting.")
                _set_agent_waiting(board)
                return

            branch = task.branch.strip() or _default_branch_for_task(task)
            if branch != task.branch:
                task.branch = branch
                task.updated_at = _utc_now()
                _append_log(board, f"BRANCH_SET - {task.id} {branch}")
                _persist_prd()

            _set_agent_running(board, task.id)
            _append_log(board, f"OPENPR_START - {task.id} on {branch}")

        repo_root = await asyncio.to_thread(_resolve_repo_root, workspace_path)
        if not repo_root:
            async with STATE_LOCK:
                board = AGENTS.get(agent_id)
                if not board:
                    return
                _append_log(board, "WORKSPACE_INVALID - Not a git repository")
                _set_agent_waiting(board)
            return

        worktree_path, error = await _ensure_worktree_path(repo_root, branch)
        if not worktree_path:
            async with STATE_LOCK:
                board = AGENTS.get(agent_id)
                if not board:
                    return
                _append_log(board, f"WORKTREE_ERROR - {error}")
                _set_agent_waiting(board)
            return

        copied = _copy_env_files(repo_root, worktree_path)
        prompt_text, prompt_path = _resolve_codex_prompt("openpr")
        if not prompt_text:
            prompt_text = "/prompts:openpr"

        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if board:
                if copied:
                    _append_log(board, f"ENV_SYNC - {', '.join(copied)}")
                _append_log(board, f"OPENPR_RUNNING - {task_id} in {worktree_path}")
                if prompt_path:
                    _append_log(board, f"OPENPR_PROMPT - {prompt_path}")
                else:
                    _append_log(board, "OPENPR_PROMPT - /prompts:openpr")

        proc = await asyncio.create_subprocess_exec(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            "gpt-5.2-codex",
            "-c",
            'model_reasoning_effort="xhigh"',
            "--add-dir",
            str(ROOT_DIR),
            prompt_text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(worktree_path),
        )

        if proc.stdout:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                async with STATE_LOCK:
                    board = AGENTS.get(agent_id)
                    if not board:
                        continue
                    _append_log(board, text)

        return_code = await proc.wait()
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return
            _sync_board_from_prd(board)
            _set_agent_waiting(board)
            _append_log(board, f"OPENPR_DONE - {task_id} exit {return_code}")
    except Exception as exc:  # pragma: no cover
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return
            _append_log(board, f"OPENPR_ERROR - {exc.__class__.__name__}: {exc}")
            _set_agent_waiting(board)
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


_load_prd_state()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/agents", response_model=AgentsResponse)
async def list_agents() -> AgentsResponse:
    async with STATE_LOCK:
        agents = [_agent_to_summary(AGENTS[agent_id]) for agent_id in AGENT_ORDER if agent_id in AGENTS]
        return AgentsResponse(agents=agents)


@app.post("/api/agents", response_model=AgentSummary)
async def create_agent(payload: CreateAgentRequest | None = None) -> AgentSummary:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        name = payload.name.strip() if payload and payload.name else ""
        agent_id = _next_agent_id()
        if not name:
            name = _default_agent_name(_extract_agent_sequence(agent_id) or len(AGENT_ORDER) + 1)

        board = _create_board(agent_id, name, DEFAULT_BRANCH_NAME, "", [])
        AGENTS[agent_id] = board
        AGENT_ORDER.append(agent_id)
        _persist_prd()
        return _agent_to_summary(board)


@app.get("/api/state", response_model=BoardStateResponse)
async def get_state(agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        return _board_state_response(board)


@app.post("/api/workspace", response_model=BoardStateResponse)
async def update_workspace(payload: WorkspaceRequest, agent_id: str | None = None) -> BoardStateResponse:
    path = payload.path.strip()
    if not path:
        raise HTTPException(status_code=400, detail="Workspace path is required")

    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        if path != board.workspace_path:
            board.workspace_path = path
            _append_log(board, f"WORKSPACE_SET - {path}")
            _persist_prd()
        return _board_state_response(board)


@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(payload: CreateTaskRequest, agent_id: str | None = None) -> TaskResponse:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    acceptance_criteria = _normalize_acceptance(payload.acceptance_criteria)

    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        task_id = _generate_story_id(board)

        now = _utc_now()
        status = payload.status
        if status == "done":
            status = "review"
        if payload.passes and status != "done":
            status = "review"

        task = TaskState(
            id=task_id,
            title=title,
            acceptance_criteria=acceptance_criteria,
            priority=payload.priority,
            passes=payload.passes,
            notes=payload.notes.strip(),
            status=status,
            owner="",
            effort="",
            branch="",
            commit="",
            summary="",
            updated_at=now,
        )
        board.tasks[task_id] = task
        board.order.append(task_id)
        _append_log(board, f"TASK_CREATED - {task_id} {title}")
        _persist_prd()
        return _task_to_response(task)


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, payload: UpdateTaskRequest, agent_id: str | None = None) -> TaskResponse:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        task = board.tasks.get(task_id)
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
            next_status = payload.status
            if next_status == "done":
                next_status = "review"
                _append_log(board, f"STATUS_AUTO - {task.id} done -> review")

            task.status = next_status
            if previous_status != next_status:
                _append_log(board, f"STATUS_CHANGE - {task.id} {previous_status} -> {next_status}")

        if payload.passes is True and task.status != "done":
            if task.status != "review":
                _append_log(board, f"STATUS_AUTO - {task.id} passes -> review")
            task.status = "review"
        task.updated_at = _utc_now()
        _persist_prd()
        return _task_to_response(task)


@app.delete("/api/tasks/{task_id}", response_model=TaskResponse)
async def delete_task(task_id: str, agent_id: str | None = None) -> TaskResponse:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        task = board.tasks.pop(task_id, None)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        board.order = [item for item in board.order if item != task_id]
        if board.agent.current_task_id == task_id:
            _set_agent_waiting(board)

        _append_log(board, f"TASK_DELETED - {task.id} {task.title}")
        _persist_prd()
        return _task_to_response(task)


@app.post("/api/tasks/{task_id}/review", response_model=TaskResponse)
async def review_task(task_id: str, payload: ReviewRequest, agent_id: str | None = None) -> TaskResponse:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        task = board.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "review":
            raise HTTPException(status_code=409, detail="Task is not awaiting review")

        if payload.decision == "approved":
            task.status = "done"
            task.passes = True
            _append_log(board, f"REVIEW_APPROVED - {task.id} moved to done")
        else:
            task.status = "todo"
            task.passes = False
            _append_log(board, f"REVIEW_REJECTED - {task.id} returned to todo")

        task.updated_at = _utc_now()
        _persist_prd()
        return _task_to_response(task)


@app.post("/api/tasks/{task_id}/codex", response_model=BoardStateResponse)
async def open_task_codex(task_id: str, agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        if board.worker and not board.worker.done():
            raise HTTPException(status_code=409, detail="Ralph is running")

        task = board.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "review":
            raise HTTPException(status_code=409, detail="Task is not in review")

        workspace_path = _resolve_workspace_path(board)
        if not workspace_path:
            raise HTTPException(status_code=400, detail="Workspace path is required")

        branch = task.branch.strip() or _default_branch_for_task(task)
        if branch != task.branch:
            task.branch = branch
            task.updated_at = _utc_now()
            _append_log(board, f"BRANCH_SET - {task.id} {branch}")
            _persist_prd()

    repo_root = await asyncio.to_thread(_resolve_repo_root, workspace_path)
    if not repo_root:
        async with STATE_LOCK:
            board = _get_board(agent_id)
            _append_log(board, "WORKSPACE_INVALID - Not a git repository")
            return _board_state_response(board)

    worktree_path, error = await _ensure_worktree_path(repo_root, branch)
    if not worktree_path:
        async with STATE_LOCK:
            board = _get_board(agent_id)
            _append_log(board, f"WORKTREE_ERROR - {error}")
            return _board_state_response(board)

    copied = _copy_env_files(repo_root, worktree_path)
    success, message = _launch_codex_terminal(worktree_path)

    async with STATE_LOCK:
        board = _get_board(agent_id)
        if copied:
            _append_log(board, f"ENV_SYNC - {', '.join(copied)}")
        if success:
            _append_log(board, f"CODEX_OPEN - {task_id} in {worktree_path}")
        else:
            _append_log(board, f"CODEX_ERROR - {message}")
        return _board_state_response(board)


@app.post("/api/tasks/{task_id}/openpr", response_model=BoardStateResponse)
async def open_task_pr(task_id: str, agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        if board.worker and not board.worker.done():
            raise HTTPException(status_code=409, detail="Ralph is running")

        task = board.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "review":
            raise HTTPException(status_code=409, detail="Task is not in review")

        _append_log(board, f"OPENPR_REQUEST - {task.id}")
        _start_openpr_worker(board, task.id)
        return _board_state_response(board)


@app.post("/api/ralph/start", response_model=BoardStateResponse)
async def start_ralph(
    payload: StartRalphRequest | None = None,
    agent_id: str | None = None,
) -> BoardStateResponse:
    async with STATE_LOCK:
        if not RALPH_SCRIPT_PATH.exists():
            raise HTTPException(status_code=500, detail="ralph.sh not found")

        board = _get_board(agent_id)
        if not _resolve_workspace_path(board):
            _append_log(board, "WORKSPACE_INVALID - Set a valid workspace path before starting.")
            _set_agent_waiting(board)
            return _board_state_response(board)
        if board.worker and not board.worker.done():
            return _board_state_response(board)

        _append_log(board, "RALPH_START - Requested")
        _start_worker(board, payload.iterations if payload else None)
        return _board_state_response(board)


@app.post("/api/agents/start-all", response_model=BoardStateResponse)
async def start_all_agents(
    payload: StartRalphRequest | None = None,
    agent_id: str | None = None,
) -> BoardStateResponse:
    async with STATE_LOCK:
        if not RALPH_SCRIPT_PATH.exists():
            raise HTTPException(status_code=500, detail="ralph.sh not found")

        for board in AGENTS.values():
            if board.worker and not board.worker.done():
                continue
            if not _resolve_workspace_path(board):
                _append_log(board, "WORKSPACE_INVALID - Set a valid workspace path before starting.")
                _set_agent_waiting(board)
                continue
            _append_log(board, "AGENTS_START_ALL - Requested")
            _start_worker(board, payload.iterations if payload else None)

        board = _get_board(agent_id)
        return _board_state_response(board)


@app.post("/api/pull-latest", response_model=BoardStateResponse)
async def pull_latest(agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        _append_log(board, "PULL_LATEST - Requested (stub)")
        return _board_state_response(board)


@app.post("/api/logs/clear", response_model=BoardStateResponse)
async def clear_logs(agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        board.logs.clear()
        _append_log(board, "LOGS_CLEARED")
        return _board_state_response(board)
