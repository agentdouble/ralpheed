from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
PRD_STORAGE_ROOT = Path.home() / ".ralpheed" / "prd"
INDEX_PATH = PRD_STORAGE_ROOT / "index.json"
DEFAULT_PRD_PATH = PRD_STORAGE_ROOT / "default" / "prd.json"
LEGACY_PRD_PATH = ROOT_DIR / "prd.json"
RALPH_SCRIPT_PATH = ROOT_DIR / "ralph.sh"
DEFAULT_BRANCH_NAME = "ralph/feature"
WORKTREE_BASE_BRANCH = "dev"
DEFAULT_AGENT_ID = "agent-1"
DEFAULT_AGENT_NAME = "Ralph"
MAX_LOG_LINES = 220
ACCEPTANCE_CRITERIA_MIN = 2
ACCEPTANCE_CRITERIA_MAX = 6
ACCEPTANCE_TIMEOUT_SECONDS = 120
ACCEPTANCE_MODEL = "gpt-5.2-codex"
ACCEPTANCE_REASONING_EFFORT = "low"
AI_TASKS_DEFAULT_COUNT = 3
AI_TASKS_MAX = 8
AI_TASKS_TIMEOUT_SECONDS = 180
AI_TASKS_MODEL = "gpt-5.2-codex"
AI_TASKS_REASONING_EFFORT = "low"
AI_TASKS_CONTEXT_MAX_CHARS = 2400
AI_TASKS_EXISTING_LIMIT = 40
ACCEPTANCE_NOISE_PREFIXES = (
    "OpenAI Codex",
    "workdir:",
    "model:",
    "provider:",
    "approval:",
)

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
    worktree: str
    wait_for_validation: bool
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
    ralph_worker: asyncio.Task[None] | None = None
    openpr_workers: dict[str, asyncio.Task[None]] = field(default_factory=dict)


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
    worktree: str
    wait_for_validation: bool
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
    worktree: str = Field(default="", max_length=120)
    wait_for_validation: bool = False


class UpdateTaskRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    acceptance_criteria: list[str] | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    passes: bool | None = None
    notes: str | None = Field(default=None, max_length=2_000)
    status: TaskStatus | None = None
    worktree: str | None = Field(default=None, max_length=120)
    wait_for_validation: bool | None = None


class ReviewRequest(BaseModel):
    decision: ReviewDecision


class WorkspaceRequest(BaseModel):
    path: str = Field(default="", max_length=512)


class StartRalphRequest(BaseModel):
    iterations: int | None = Field(default=None, ge=1)


class AcceptanceCriteriaRequest(BaseModel):
    title: str = Field(min_length=1, max_length=180)


class AcceptanceCriteriaResponse(BaseModel):
    criteria: list[str]


class AiTasksRequest(BaseModel):
    count: int = Field(default=AI_TASKS_DEFAULT_COUNT, ge=1, le=AI_TASKS_MAX)
    theme: str | None = Field(default=None, max_length=200)


class AiTasksResponse(BaseModel):
    tasks: list[TaskResponse]


AGENTS: dict[str, AgentBoard] = {}
AGENT_ORDER: list[str] = []
AGENT_SEQUENCE = 1

STATE_LOCK = asyncio.Lock()
WORKTREE_LOCK = asyncio.Lock()


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


def _normalize_worktree(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if len(value) > 120:
        value = value[:120].rstrip()
    return value


def _normalize_wait_for_validation(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return False


def _build_acceptance_prompt(title: str) -> str:
    return (
        "You are an Acceptance Criteria Generator to test whether an implementation matches expectations and works.\n"
        "Write 2 to 6 acceptance criteria that are 100% AI-verifiable.\n"
        "Rules:\n"
        "1) Each criterion is a single, atomic verification.\n"
        "2) No human judgment required.\n"
        "3) No vague wording (e.g. \"clean UI\", \"works\", \"optimized\", \"fast\").\n"
        "Output ONLY a JSON array of strings. No markdown, no numbering.\n"
        "Use the same language as the title.\n"
        f"Title: {title}\n"
    )


def _clean_acceptance_items(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        if value.startswith(ACCEPTANCE_NOISE_PREFIXES):
            continue
        if not value.strip("-"):
            continue
        cleaned.append(value)
        seen.add(value)
    return cleaned


def _parse_acceptance_output(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []

    fenced = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
    if fenced:
        text = fenced.group(1).strip()

    json_match = re.search(r"\[[\s\S]*\]", text)
    if json_match:
        candidate = json_match.group(0)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, str)]
            cleaned = _clean_acceptance_items(items)
            if cleaned:
                return cleaned

    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        stripped = re.sub(r"^[-*\\d+.\\)]\\s*", "", stripped).strip()
        if stripped:
            lines.append(stripped)
    return _clean_acceptance_items(lines)


async def _generate_acceptance_criteria(title: str) -> list[str]:
    if not shutil.which("codex"):
        raise HTTPException(status_code=500, detail="codex not found")

    proc: asyncio.subprocess.Process | None = None
    output_path: str | None = None
    stdout = b""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            output_path = tmp.name
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            ACCEPTANCE_MODEL,
            "-c",
            f'model_reasoning_effort="{ACCEPTANCE_REASONING_EFFORT}"',
            "--output-last-message",
            output_path,
            _build_acceptance_prompt(title),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ROOT_DIR),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=ACCEPTANCE_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail="Acceptance criteria generation failed")

        message_text = ""
        if output_path:
            try:
                message_text = Path(output_path).read_text(encoding="utf-8")
            except OSError:
                message_text = ""

        criteria_source = message_text.strip()
        if not criteria_source:
            criteria_source = stdout.decode(errors="replace").strip() if stdout else ""

        criteria = _parse_acceptance_output(criteria_source)
        if len(criteria) > ACCEPTANCE_CRITERIA_MAX:
            criteria = criteria[:ACCEPTANCE_CRITERIA_MAX]
        if len(criteria) < ACCEPTANCE_CRITERIA_MIN:
            raise HTTPException(status_code=502, detail="Acceptance criteria generation failed")
        return criteria
    except asyncio.TimeoutError as exc:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        raise HTTPException(status_code=504, detail="Acceptance criteria generation timed out") from exc
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    finally:
        if output_path:
            try:
                Path(output_path).unlink()
            except OSError:
                pass


def _read_readme_excerpt(root_path: Path) -> str:
    readme_path = root_path / "README.md"
    if not readme_path.is_file():
        return ""
    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > AI_TASKS_CONTEXT_MAX_CHARS:
        text = text[:AI_TASKS_CONTEXT_MAX_CHARS].rstrip()
    return text


def _build_ai_tasks_prompt(count: int, theme: str, readme: str, existing_titles: list[str]) -> str:
    theme_line = f"Theme: {theme}" if theme else "Theme: (infer from project context)"
    existing = ""
    if existing_titles:
        trimmed = existing_titles[:AI_TASKS_EXISTING_LIMIT]
        existing = f"Existing tasks: {json.dumps(trimmed, ensure_ascii=True)}\n"
    if readme:
        context = f"Project context (README excerpt):\n{readme}\n"
    else:
        context = "Project context: (README unavailable)\n"
    return (
        "You are a product manager creating backlog tasks for this project.\n"
        f"Generate {count} tasks.\n"
        f"{theme_line}\n"
        f"{context}"
        f"{existing}"
        "Rules:\n"
        "1) Output ONLY a JSON array of objects.\n"
        '2) Each object: {"title": string, "notes": string, "priority": 1-3, "status": "backlog", '
        '"worktree": string, "wait_for_validation": boolean}.\n'
        "3) Titles must be short and specific.\n"
        "4) Avoid duplicates of existing tasks.\n"
        "5) Use the same language as the theme if provided; otherwise use the README language.\n"
        "6) If tasks depend on each other, give them the same worktree.\n"
        "7) Set wait_for_validation=true on the principal tache.\n"
    )


def _parse_ai_tasks_output(raw: str) -> list[object]:
    text = raw.strip()
    if not text:
        return []

    fenced = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
    if fenced:
        text = fenced.group(1).strip()

    json_match = re.search(r"\[[\s\S]*\]", text)
    if json_match:
        candidate = json_match.group(0)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return data

    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        stripped = re.sub(r"^[-*\d+.\)]\s*", "", stripped).strip()
        if stripped:
            lines.append(stripped)
    return lines


def _title_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _clean_task_title(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    for prefix in ACCEPTANCE_NOISE_PREFIXES:
        if value.startswith(prefix):
            return ""
    value = re.sub(r"^[-*\d+.\)]\s*", "", value).strip()
    if not value:
        return ""
    if len(value) > 180:
        value = value[:180].rstrip()
    return value


def _clean_task_notes(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if len(value) > 2_000:
        value = value[:2_000].rstrip()
    return value


def _clean_ai_tasks(
    items: list[object],
    existing_titles: list[str],
    count: int,
) -> list[dict[str, object]]:
    existing_keys = {_title_key(title) for title in existing_titles if isinstance(title, str) and title.strip()}
    cleaned: list[dict[str, object]] = []
    seen: set[str] = set()

    for item in items:
        title = ""
        notes = ""
        priority = 1
        status: TaskStatus = "backlog"
        worktree = ""
        wait_for_validation = False

        if isinstance(item, dict):
            title = _clean_task_title(item.get("title") or item.get("name") or item.get("task") or "")
            notes = _clean_task_notes(item.get("notes") or item.get("description") or "")
            priority = _normalize_priority(item.get("priority"))
            status = _normalize_status(item.get("status"))
            worktree = _normalize_worktree(item.get("worktree") or item.get("worktreeGroup"))
            wait_for_validation = _normalize_wait_for_validation(
                item.get("wait_for_validation") if "wait_for_validation" in item else item.get("waitForValidation")
            )
        elif isinstance(item, str):
            title = _clean_task_title(item)
        else:
            continue

        if not title:
            continue
        key = _title_key(title)
        if key in seen or key in existing_keys:
            continue
        cleaned.append(
            {
                "title": title,
                "notes": notes,
                "priority": priority,
                "status": status,
                "worktree": worktree,
                "wait_for_validation": wait_for_validation,
            }
        )
        seen.add(key)
        if len(cleaned) >= count:
            break

    return cleaned


async def _generate_ai_tasks(
    count: int,
    theme: str,
    existing_titles: list[str],
    context_root: Path,
) -> list[dict[str, object]]:
    if not shutil.which("codex"):
        raise HTTPException(status_code=500, detail="codex not found")

    proc: asyncio.subprocess.Process | None = None
    output_path: str | None = None
    stdout = b""
    readme = _read_readme_excerpt(context_root)
    prompt = _build_ai_tasks_prompt(count, theme, readme, existing_titles)
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            output_path = tmp.name
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            AI_TASKS_MODEL,
            "-c",
            f'model_reasoning_effort="{AI_TASKS_REASONING_EFFORT}"',
            "--output-last-message",
            output_path,
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(context_root),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=AI_TASKS_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail="AI task generation failed")

        message_text = ""
        if output_path:
            try:
                message_text = Path(output_path).read_text(encoding="utf-8")
            except OSError:
                message_text = ""

        response_text = message_text.strip()
        if not response_text:
            response_text = stdout.decode(errors="replace").strip() if stdout else ""

        items = _parse_ai_tasks_output(response_text)
        cleaned = _clean_ai_tasks(items, existing_titles, count)
        if not cleaned:
            raise HTTPException(status_code=502, detail="AI task generation failed")
        return cleaned
    except asyncio.TimeoutError as exc:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        raise HTTPException(status_code=504, detail="AI task generation timed out") from exc
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    finally:
        if output_path:
            try:
                Path(output_path).unlink()
            except OSError:
                pass


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


def _workspace_slug(workspace_root: Path) -> str:
    name = workspace_root.name.strip() or "workspace"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "workspace"
    digest = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:10]
    return f"{safe_name}-{digest}"


def _prd_path_for_workspace_root(workspace_root: Path | None) -> Path:
    if not workspace_root:
        return DEFAULT_PRD_PATH
    return PRD_STORAGE_ROOT / _workspace_slug(workspace_root) / "prd.json"


def _normalize_workspace_path(raw: str) -> Path | None:
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def _resolve_workspace_root_for_storage(path: str) -> Path | None:
    candidate = _normalize_workspace_path(path)
    if not candidate:
        return None
    if candidate.is_dir():
        repo_root = _resolve_repo_root(candidate)
        return repo_root or candidate
    return candidate


def _prd_path_for_workspace(path: str) -> Path:
    root = _resolve_workspace_root_for_storage(path)
    return _prd_path_for_workspace_root(root)


def _read_prd_at(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"agents": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_prd_at(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _extract_agent_items(payload: dict[str, object]) -> list[dict[str, object]]:
    agents_raw = payload.get("agents")
    if isinstance(agents_raw, list) and agents_raw:
        return [item for item in agents_raw if isinstance(item, dict)]
    return [
        {
            "id": DEFAULT_AGENT_ID,
            "name": DEFAULT_AGENT_NAME,
            "branchName": payload.get("branchName"),
            "workspacePath": payload.get("workspacePath"),
            "userStories": payload.get("userStories"),
        }
    ]


def _find_agent_entry(payload: dict[str, object], agent_id: str) -> dict[str, object] | None:
    for item in _extract_agent_items(payload):
        entry_id = str(item.get("id") or "").strip()
        if entry_id == agent_id:
            return item
    return None


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
        "worktree": task.worktree,
        "waitForValidation": task.wait_for_validation,
        "branch": task.branch,
        "commit": task.commit,
        "summary": task.summary,
    }


def _agent_payload(board: AgentBoard, include_tasks: bool) -> dict[str, object]:
    stories = []
    if include_tasks:
        for task_id in board.order:
            task = board.tasks.get(task_id)
            if task:
                stories.append(_task_to_story(task))
    return {
        "id": board.id,
        "name": board.name,
        "branchName": board.branch_name,
        "workspacePath": board.workspace_path,
        "userStories": stories,
    }


def _persist_prd() -> None:
    index_agents: list[dict[str, object]] = []
    workspace_updates: dict[Path, dict[str, dict[str, object]]] = {}

    for agent_id in AGENT_ORDER:
        board = AGENTS.get(agent_id)
        if not board:
            continue
        index_agents.append(_agent_payload(board, include_tasks=False))
        workspace_path = _prd_path_for_workspace(board.workspace_path)
        workspace_updates.setdefault(workspace_path, {})[board.id] = _agent_payload(board, include_tasks=True)

    _write_prd_at(INDEX_PATH, {"agents": index_agents})

    for path, updates in workspace_updates.items():
        existing_payload = _read_prd_at(path)
        existing_items = _extract_agent_items(existing_payload)
        existing_by_id: dict[str, dict[str, object]] = {}
        for item in existing_items:
            agent_id = str(item.get("id") or "").strip()
            if agent_id:
                existing_by_id[agent_id] = item

        existing_by_id.update(updates)

        ordered: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in existing_items:
            agent_id = str(item.get("id") or "").strip()
            if not agent_id:
                continue
            if agent_id in updates:
                ordered.append(existing_by_id[agent_id])
            else:
                ordered.append(item)
            seen.add(agent_id)
        for agent_id, item in existing_by_id.items():
            if agent_id in seen:
                continue
            ordered.append(item)

        _write_prd_at(path, {"agents": ordered})
def _any_worker_running() -> bool:
    return any(board.ralph_worker and not board.ralph_worker.done() for board in AGENTS.values())


def _ensure_mutation_allowed() -> None:
    if _any_worker_running():
        raise HTTPException(status_code=409, detail="Ralph is running")


def _resolve_workspace_value(raw: str) -> Path | None:
    path = _normalize_workspace_path(raw)
    if not path or not path.is_dir():
        return None
    return path


def _resolve_workspace_path(board: AgentBoard) -> Path | None:
    return _resolve_workspace_value(board.workspace_path)


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


def _resolve_workspace_root(path: str) -> Path | None:
    workspace_path = _resolve_workspace_value(path)
    if not workspace_path:
        return None
    repo_root = _resolve_repo_root(workspace_path)
    return repo_root or workspace_path


def _select_git_remote(repo_root: Path) -> tuple[str | None, str | None]:
    result = _run_git(["git", "remote"], repo_root)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unable to list remotes"
        return None, error
    remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not remotes:
        return None, "no git remotes"
    if "origin" in remotes:
        return "origin", None
    if len(remotes) == 1:
        return remotes[0], None
    return None, "multiple remotes without origin"


def _worktrees_root(repo_root: Path) -> Path:
    return repo_root.parent / f"{repo_root.name}-worktrees"


def _sanitize_branch_name(branch: str) -> str:
    cleaned = branch.strip().replace("/", "__")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned or "worktree"


def _default_branch_for_task(task: TaskState) -> str:
    return f"feat/{task.id.lower()}"


def _resolve_task_branch(task: TaskState) -> str:
    worktree = task.worktree.strip()
    if worktree:
        return worktree
    if task.branch.strip():
        return task.branch.strip()
    return _default_branch_for_task(task)


async def _refresh_base_branch(repo_root: Path, base_branch: str) -> tuple[str | None, str | None]:
    remote, error = await asyncio.to_thread(_select_git_remote, repo_root)
    if not remote:
        return None, error
    fetch_result = await asyncio.to_thread(_run_git, ["git", "fetch", remote, base_branch], repo_root)
    if fetch_result.returncode != 0:
        error = fetch_result.stderr.strip() or fetch_result.stdout.strip() or "unable to fetch base branch"
        return None, error
    remote_ref = f"refs/remotes/{remote}/{base_branch}"
    rev_result = await asyncio.to_thread(_run_git, ["git", "rev-parse", "--verify", remote_ref], repo_root)
    if rev_result.returncode != 0:
        error = rev_result.stderr.strip() or rev_result.stdout.strip() or "unable to resolve base branch"
        return None, error
    return remote_ref, None


async def _ensure_worktree_path(repo_root: Path, branch: str) -> tuple[Path | None, str | None]:
    async with WORKTREE_LOCK:
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
            base_ref, error = await _refresh_base_branch(repo_root, WORKTREE_BASE_BRANCH)
            if not base_ref:
                return None, error
            cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path), base_ref]
        result = await asyncio.to_thread(_run_git, cmd, repo_root)
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip() or "unable to create worktree"
            return None, error
        return worktree_path, None


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _launch_terminal_command(worktree_path: Path, command: str) -> tuple[bool, str]:
    full_command = f"cd {shlex.quote(str(worktree_path))} && {command}"
    if sys.platform == "darwin":
        script = (
            'tell application "iTerm"\n'
            "activate\n"
            "set newWindow to (create window with default profile)\n"
            f'tell current session of newWindow to write text "{_escape_osascript(full_command)}"\n'
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
            args = [terminal, "--", "bash", "-lc", full_command]
        else:
            args = [terminal, "-e", "bash", "-lc", full_command]
        subprocess.Popen(args, cwd=str(worktree_path))
        return True, ""

    if sys.platform == "win32":
        args = ["cmd", "/c", "start", "cmd", "/k", f"cd /d {worktree_path} && {command}"]
        subprocess.Popen(args)
        return True, ""

    return False, "unsupported platform"


def _launch_codex_terminal(worktree_path: Path) -> tuple[bool, str]:
    if not shutil.which("codex"):
        return False, "codex not found"
    return _launch_terminal_command(worktree_path, "codex")


def _launch_start_terminal(worktree_path: Path) -> tuple[bool, str]:
    start_script = worktree_path / "start.sh"
    if not start_script.is_file():
        return False, "start.sh not found"
    if not os.access(start_script, os.X_OK):
        return False, "start.sh not executable"
    return _launch_terminal_command(worktree_path, "./start.sh")


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
    skip_dirs = {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "out",
        ".next",
        ".turbo",
        ".cache",
    }
    for root, dirs, files in os.walk(source):
        dirs[:] = [name for name in dirs if name not in skip_dirs]
        for filename in files:
            if filename != ".env" and not filename.startswith(".env."):
                continue
            src_path = Path(root) / filename
            rel_path = src_path.relative_to(source)
            dest_path = dest / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)
            copied.append(str(rel_path))
    copied.sort()
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


def _task_sort_key(task: TaskState, order_index: dict[str, int]) -> tuple[int, int]:
    return (task.priority, order_index.get(task.id, 0))


def _worktree_sequences(board: AgentBoard, order_index: dict[str, int]) -> dict[str, list[TaskState]]:
    sequences: dict[str, list[TaskState]] = {}
    for task_id in board.order:
        task = board.tasks.get(task_id)
        if not task:
            continue
        worktree = task.worktree.strip()
        if not worktree:
            continue
        sequences.setdefault(worktree, []).append(task)
    for tasks in sequences.values():
        tasks.sort(key=lambda item: _task_sort_key(item, order_index))
    return sequences


def _task_blocked_by_validation(
    task: TaskState,
    sequences: dict[str, list[TaskState]],
) -> bool:
    worktree = task.worktree.strip()
    if not worktree:
        return False
    group = sequences.get(worktree)
    if not group:
        return False
    for sibling in group:
        if sibling.id == task.id:
            break
        if sibling.wait_for_validation and not sibling.passes and sibling.status not in {"review", "done"}:
            return True
    return False


def _sync_board_from_prd(board: AgentBoard) -> None:
    payload = _read_prd_at(_prd_path_for_workspace(board.workspace_path))
    entry = _find_agent_entry(payload, board.id)
    if not entry:
        return

    stories_raw = entry.get("userStories")
    stories = stories_raw if isinstance(stories_raw, list) else []
    tasks, order, sequence = _load_tasks_from_stories(stories)
    board.tasks = tasks
    board.order = order
    board.task_sequence = sequence

    branch_raw = entry.get("branchName")
    if isinstance(branch_raw, str) and branch_raw.strip():
        board.branch_name = branch_raw.strip()

    name_raw = entry.get("name")
    if not board.name and isinstance(name_raw, str) and name_raw.strip():
        board.name = name_raw.strip()


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
        owner = str(raw.get("owner") or "")
        effort = str(raw.get("effort") or "")
        worktree = _normalize_worktree(raw.get("worktree") or raw.get("worktreeGroup"))
        wait_for_validation = _normalize_wait_for_validation(
            raw.get("waitForValidation") if "waitForValidation" in raw else raw.get("wait_for_validation")
        )
        branch = str(raw.get("branch") or "")
        if worktree:
            branch = worktree
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
            worktree=worktree,
            wait_for_validation=wait_for_validation,
            branch=branch,
            commit=commit,
            summary=summary,
            updated_at=updated_at,
        )
        order.append(story_id)
        highest = max(highest, _extract_sequence(story_id))

    sequence = highest + 1 if highest else max(len(order) + 1, 1)
    return tasks, order, sequence


def _apply_stories_to_board(board: AgentBoard, stories: list[object]) -> None:
    tasks, order, sequence = _load_tasks_from_stories(stories)
    board.tasks = tasks
    board.order = order
    board.task_sequence = sequence


def _merge_stories(primary: list[object], fallback: list[object]) -> list[object]:
    merged: list[object] = []
    seen: set[str] = set()

    for item in primary:
        if not isinstance(item, dict):
            continue
        story_id = str(item.get("id") or "").strip()
        if not story_id or story_id in seen:
            continue
        merged.append(item)
        seen.add(story_id)

    for item in fallback:
        if not isinstance(item, dict):
            continue
        story_id = str(item.get("id") or "").strip()
        if not story_id or story_id in seen:
            continue
        merged.append(item)
        seen.add(story_id)

    return merged


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

    used_legacy = False
    if INDEX_PATH.exists():
        index_data = _read_prd_at(INDEX_PATH)
    elif DEFAULT_PRD_PATH.exists():
        index_data = _read_prd_at(DEFAULT_PRD_PATH)
        used_legacy = True
    elif LEGACY_PRD_PATH.exists():
        index_data = _read_prd_at(LEGACY_PRD_PATH)
        used_legacy = True
    else:
        index_data = {"agents": []}

    index_items = _extract_agent_items(index_data)
    index_by_id: dict[str, dict[str, object]] = {}
    for item in index_items:
        agent_id = str(item.get("id") or "").strip()
        if agent_id:
            index_by_id[agent_id] = item

    agents: dict[str, AgentBoard] = {}
    order: list[str] = []
    highest = 0

    for index, raw in enumerate(index_items, start=1):
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
        branch_name = branch_raw.strip() if isinstance(branch_raw, str) and branch_raw.strip() else DEFAULT_BRANCH_NAME
        workspace_raw = raw.get("workspacePath")
        workspace_path = workspace_raw if isinstance(workspace_raw, str) else ""

        board = _create_board(agent_id, name, branch_name, workspace_path, [])
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

    migrated = used_legacy or not INDEX_PATH.exists()

    for board in AGENTS.values():
        workspace_payload = _read_prd_at(_prd_path_for_workspace(board.workspace_path))
        entry = _find_agent_entry(workspace_payload, board.id)
        entry_stories_raw = entry.get("userStories") if entry else None
        entry_stories = entry_stories_raw if isinstance(entry_stories_raw, list) else []
        fallback = index_by_id.get(board.id)
        fallback_stories_raw = fallback.get("userStories") if fallback else None
        fallback_stories = fallback_stories_raw if isinstance(fallback_stories_raw, list) else []

        if used_legacy and fallback_stories:
            merged = _merge_stories(entry_stories, fallback_stories) if entry else fallback_stories
            _apply_stories_to_board(board, merged)
            if merged != entry_stories:
                migrated = True
            branch_raw = entry.get("branchName") if entry else None
            if not (isinstance(branch_raw, str) and branch_raw.strip()):
                branch_raw = fallback.get("branchName") if fallback else None
            if isinstance(branch_raw, str) and branch_raw.strip():
                board.branch_name = branch_raw.strip()
            continue

        if entry:
            _apply_stories_to_board(board, entry_stories)
            branch_raw = entry.get("branchName")
            if isinstance(branch_raw, str) and branch_raw.strip():
                board.branch_name = branch_raw.strip()
            continue

        if fallback_stories:
            _apply_stories_to_board(board, fallback_stories)
            migrated = True

    if migrated:
        _persist_prd()


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
        worktree=task.worktree,
        wait_for_validation=task.wait_for_validation,
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
    if board.ralph_worker and not board.ralph_worker.done():
        return
    board.ralph_worker = asyncio.create_task(_agent_worker_loop(board.id, iterations))


def _start_openpr_worker(board: AgentBoard, task_id: str) -> None:
    existing = board.openpr_workers.get(task_id)
    if existing and not existing.done():
        return
    board.openpr_workers[task_id] = asyncio.create_task(_openpr_worker_loop(board.id, task_id))


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

                promoted = False
                for task in board.tasks.values():
                    if task.passes and task.status not in {"review", "done"}:
                        task.status = "review"
                        task.updated_at = _utc_now()
                        _append_log(board, f"STATUS_AUTO - {task.id} passes -> review")
                        promoted = True
                if promoted:
                    _persist_prd()

                order_index = {task_id: index for index, task_id in enumerate(board.order)}
                todo_ids = [
                    task_id
                    for task_id in board.order
                    if (task := board.tasks.get(task_id)) and task.status == "todo" and not task.passes
                ]
                todo_ids.sort(key=lambda item: _task_sort_key(board.tasks[item], order_index))
                worktree_sequences = _worktree_sequences(board, order_index)
                eligible_ids = [
                    task_id
                    for task_id in todo_ids
                    if not _task_blocked_by_validation(board.tasks[task_id], worktree_sequences)
                ]

                if not todo_ids:
                    _set_agent_waiting(board)
                    _append_log(board, "RALPH_WAITING - No todo tasks")
                    return
                if not eligible_ids:
                    _set_agent_waiting(board)
                    _append_log(board, "RALPH_WAITING - Waiting for principal tache")
                    return

                if round_index >= max_rounds:
                    _set_agent_waiting(board)
                    _append_log(board, "RALPH_WAITING - Max rounds reached")
                    return

                round_index += 1
                _append_log(board, f"RALPH_ROUND - {round_index}/{max_rounds} ({len(eligible_ids)} tasks)")

            for task_id in eligible_ids:
                async with STATE_LOCK:
                    board = AGENTS.get(agent_id)
                    if not board:
                        return

                    task = board.tasks.get(task_id)
                    if not task or task.status != "todo" or task.passes:
                        continue

                    branch = _resolve_task_branch(task)
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
                    prd_path = _prd_path_for_workspace(board.workspace_path) if board else DEFAULT_PRD_PATH

                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash",
                    str(RALPH_SCRIPT_PATH),
                    *([str(iterations)] if iterations is not None else []),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(worktree_path),
                    env={**os.environ, "RALPHEED_PRD_PATH": str(prd_path)},
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
                            _append_log(board, f"RALPH_OUT - {text}")

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
    except asyncio.CancelledError:
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if board:
                task_id = board.agent.current_task_id or ""
                if task_id:
                    _append_log(board, f"RALPH_CUT - {task_id}")
                else:
                    _append_log(board, "RALPH_CUT - canceled")
                _set_agent_waiting(board)
        raise
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
                _append_log(board, f"OPENPR_ERROR - {task_id} Task not found")
                return
            if task.status not in {"review", "done"}:
                _append_log(board, f"OPENPR_ERROR - {task.id} not in review")
                return

            workspace_path = _resolve_workspace_path(board)
            if not workspace_path:
                _append_log(board, "WORKSPACE_INVALID - Set a valid workspace path before starting.")
                return

            branch = _resolve_task_branch(task)
            if branch != task.branch:
                task.branch = branch
                task.updated_at = _utc_now()
                _append_log(board, f"BRANCH_SET - {task.id} {branch}")
                _persist_prd()

            _append_log(board, f"OPENPR_START - {task.id} on {branch}")

        repo_root = await asyncio.to_thread(_resolve_repo_root, workspace_path)
        if not repo_root:
            async with STATE_LOCK:
                board = AGENTS.get(agent_id)
                if not board:
                    return
                _append_log(board, "WORKSPACE_INVALID - Not a git repository")
            return

        worktree_path, error = await _ensure_worktree_path(repo_root, branch)
        if not worktree_path:
            async with STATE_LOCK:
                board = AGENTS.get(agent_id)
                if not board:
                    return
                _append_log(board, f"WORKTREE_ERROR - {error}")
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
                    _append_log(board, f"OPENPR_PROMPT - {task_id} {prompt_path}")
                else:
                    _append_log(board, f"OPENPR_PROMPT - {task_id} /prompts:openpr")

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
                    _append_log(board, f"OPENPR_OUT - {task_id} {text}")

        return_code = await proc.wait()
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return
            _sync_board_from_prd(board)
            _append_log(board, f"OPENPR_DONE - {task_id} exit {return_code}")
    except asyncio.CancelledError:
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if board:
                _append_log(board, f"OPENPR_CUT - {task_id}")
        raise
    except Exception as exc:  # pragma: no cover
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if not board:
                return
            _append_log(board, f"OPENPR_ERROR - {task_id} {exc.__class__.__name__}: {exc}")
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        async with STATE_LOCK:
            board = AGENTS.get(agent_id)
            if board:
                board.openpr_workers.pop(task_id, None)


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
    workspace_root = _resolve_workspace_root(path)
    if not workspace_root:
        raise HTTPException(status_code=400, detail="Workspace path is invalid")

    async with STATE_LOCK:
        _ensure_mutation_allowed()
        if any(
            worker and not worker.done()
            for board in AGENTS.values()
            for worker in board.openpr_workers.values()
        ):
            raise HTTPException(status_code=409, detail="OpenPR is running")
        board = _get_board(agent_id)
        if path == board.workspace_path:
            return _board_state_response(board)

        _persist_prd()
        board.workspace_path = path
        _append_log(board, f"WORKSPACE_SET - {path}")

        payload = _read_prd_at(_prd_path_for_workspace(path))
        entry = _find_agent_entry(payload, board.id)
        if entry:
            stories_raw = entry.get("userStories")
            stories = stories_raw if isinstance(stories_raw, list) else []
            _apply_stories_to_board(board, stories)
            branch_raw = entry.get("branchName")
            if isinstance(branch_raw, str) and branch_raw.strip():
                board.branch_name = branch_raw.strip()
        else:
            _apply_stories_to_board(board, [])
            board.branch_name = DEFAULT_BRANCH_NAME

        _persist_prd()
        return _board_state_response(board)


@app.post("/api/acceptance-criteria", response_model=AcceptanceCriteriaResponse)
async def generate_acceptance_criteria(payload: AcceptanceCriteriaRequest) -> AcceptanceCriteriaResponse:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    criteria = await _generate_acceptance_criteria(title)
    return AcceptanceCriteriaResponse(criteria=criteria)


@app.post("/api/tasks/ai", response_model=AiTasksResponse)
async def create_ai_tasks(payload: AiTasksRequest, agent_id: str | None = None) -> AiTasksResponse:
    theme = payload.theme.strip() if payload.theme else ""
    count = payload.count

    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        existing_titles = [
            task.title for task_id in board.order if (task := board.tasks.get(task_id))
        ]
        workspace_raw = board.workspace_path

    context_root = ROOT_DIR
    workspace_path = _resolve_workspace_value(workspace_raw)
    if workspace_path:
        repo_root = await asyncio.to_thread(_resolve_repo_root, workspace_path)
        context_root = repo_root or workspace_path

    candidates = await _generate_ai_tasks(count, theme, existing_titles, context_root)

    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        existing_keys = {_title_key(task.title) for task in board.tasks.values()}
        created: list[TaskResponse] = []

        for candidate in candidates:
            title = _clean_task_title(candidate.get("title"))
            if not title:
                continue
            if _title_key(title) in existing_keys:
                continue

            status = _normalize_status(candidate.get("status"))
            if status == "done":
                status = "review"
            worktree = _normalize_worktree(candidate.get("worktree"))
            wait_for_validation = _normalize_wait_for_validation(
                candidate.get("wait_for_validation") if "wait_for_validation" in candidate else candidate.get("waitForValidation")
            )
            branch = worktree if worktree else ""

            now = _utc_now()
            task_id = _generate_story_id(board)
            task = TaskState(
                id=task_id,
                title=title,
                acceptance_criteria=[],
                priority=_normalize_priority(candidate.get("priority")),
                passes=False,
                notes=_clean_task_notes(candidate.get("notes")),
                status=status,
                owner="",
                effort="",
                worktree=worktree,
                wait_for_validation=wait_for_validation,
                branch=branch,
                commit="",
                summary="",
                updated_at=now,
            )
            board.tasks[task_id] = task
            board.order.append(task_id)
            _append_log(board, f"TASK_CREATED - {task_id} {title}")
            created.append(_task_to_response(task))
            existing_keys.add(_title_key(title))

        if not created:
            raise HTTPException(status_code=502, detail="AI task generation failed")

        _persist_prd()
        return AiTasksResponse(tasks=created)


@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(payload: CreateTaskRequest, agent_id: str | None = None) -> TaskResponse:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    acceptance_criteria = _normalize_acceptance(payload.acceptance_criteria)
    worktree = _normalize_worktree(payload.worktree)
    branch = worktree if worktree else ""

    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        task_id = _generate_story_id(board)

        now = _utc_now()
        status = payload.status
        if status == "done":
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
            worktree=worktree,
            wait_for_validation=payload.wait_for_validation,
            branch=branch,
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

        if payload.worktree is not None:
            next_worktree = _normalize_worktree(payload.worktree)
            if next_worktree:
                task.worktree = next_worktree
                task.branch = next_worktree
            elif task.worktree:
                task.worktree = ""
                task.branch = ""

        if payload.wait_for_validation is not None:
            task.wait_for_validation = payload.wait_for_validation

        previous_status = task.status
        if payload.status is not None:
            next_status = payload.status
            if next_status == "done" and previous_status != "done":
                next_status = "review"
                _append_log(board, f"STATUS_AUTO - {task.id} done -> review")
        else:
            next_status = task.status

        if payload.passes is True and next_status == "todo":
            next_status = "review"
            _append_log(board, f"STATUS_AUTO - {task.id} passes -> review")

        if previous_status != next_status:
            task.status = next_status
            _append_log(board, f"STATUS_CHANGE - {task.id} {previous_status} -> {next_status}")

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
            _append_log(board, f"OPENPR_REQUEST - {task.id}")
            _start_openpr_worker(board, task.id)
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
        if board.ralph_worker and not board.ralph_worker.done():
            raise HTTPException(status_code=409, detail="Ralph is running")

        task = board.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "review":
            raise HTTPException(status_code=409, detail="Task is not in review")

        workspace_path = _resolve_workspace_path(board)
        if not workspace_path:
            raise HTTPException(status_code=400, detail="Workspace path is required")

        branch = _resolve_task_branch(task)
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


@app.post("/api/tasks/{task_id}/start", response_model=BoardStateResponse)
async def open_task_start(task_id: str, agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        task = board.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "review":
            raise HTTPException(status_code=409, detail="Task is not in review")

        workspace_path = _resolve_workspace_path(board)
        if not workspace_path:
            raise HTTPException(status_code=400, detail="Workspace path is required")

        branch = _resolve_task_branch(task)
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
    success, message = _launch_start_terminal(worktree_path)

    async with STATE_LOCK:
        board = _get_board(agent_id)
        if copied:
            _append_log(board, f"ENV_SYNC - {', '.join(copied)}")
        if success:
            _append_log(board, f"START_OPEN - {task_id} in {worktree_path}")
        else:
            _append_log(board, f"START_ERROR - {message}")
        return _board_state_response(board)


@app.post("/api/tasks/{task_id}/openpr", response_model=BoardStateResponse)
async def open_task_pr(task_id: str, agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        _ensure_mutation_allowed()
        board = _get_board(agent_id)
        if board.ralph_worker and not board.ralph_worker.done():
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
        if board.ralph_worker and not board.ralph_worker.done():
            return _board_state_response(board)

        _append_log(board, "RALPH_START - Requested")
        _start_worker(board, payload.iterations if payload else None)
        return _board_state_response(board)


@app.post("/api/ralph/stop", response_model=BoardStateResponse)
async def stop_ralph(agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        if not board.ralph_worker or board.ralph_worker.done():
            _append_log(board, "RALPH_STOP_NOOP - Not running")
            return _board_state_response(board)
        _append_log(board, "RALPH_STOP - Requested")
        board.ralph_worker.cancel()
        return _board_state_response(board)


@app.post("/api/openpr/stop", response_model=BoardStateResponse)
async def stop_openpr(agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        if not board.openpr_workers:
            _append_log(board, "OPENPR_STOP_NOOP - Not running")
            return _board_state_response(board)
        _append_log(board, "OPENPR_STOP - Requested")
        for task_id, worker in list(board.openpr_workers.items()):
            if worker and not worker.done():
                worker.cancel()
                _append_log(board, f"OPENPR_STOP - {task_id}")
        return _board_state_response(board)


@app.post("/api/tasks/{task_id}/openpr/stop", response_model=BoardStateResponse)
async def stop_openpr_task(task_id: str, agent_id: str | None = None) -> BoardStateResponse:
    async with STATE_LOCK:
        board = _get_board(agent_id)
        worker = board.openpr_workers.get(task_id)
        if not worker or worker.done():
            _append_log(board, f"OPENPR_STOP_NOOP - {task_id}")
            return _board_state_response(board)
        _append_log(board, f"OPENPR_STOP - {task_id}")
        worker.cancel()
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
            if board.ralph_worker and not board.ralph_worker.done():
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
