import { useEffect, useMemo, useRef, useState } from 'react'
import './App.css'

const DEFAULT_API_URL = 'http://127.0.0.1:8000'

const COLUMNS = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'todo', label: 'Todo' },
  { id: 'review', label: 'Review' },
  { id: 'done', label: 'Done' },
]

const QUICK_COLUMNS = new Set(['backlog'])
const DEFAULT_QUICK_TITLES = { backlog: '' }

const clampPriority = (value) => {
  const parsed = Number(value)
  if (Number.isNaN(parsed)) return 1
  if (parsed < 1) return 1
  if (parsed > 3) return 3
  return parsed
}

const priorityTone = (priority) => {
  if (priority <= 1) return 'high'
  if (priority === 2) return 'medium'
  return 'low'
}

const parseAcceptanceCriteria = (text) => {
  return text
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}

const normalizeStatus = (status) => {
  if (status === 'backlog' || status === 'todo' || status === 'review' || status === 'done') {
    return status
  }
  if (status === 'plan' || status === 'ready' || status === 'active') {
    return 'todo'
  }
  return 'backlog'
}

const formatAcceptanceCriteria = (criteria) => {
  if (!Array.isArray(criteria)) return ''
  return criteria.join('\n')
}

const formatClock = (value) => {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date)
}

const formatTime = (value) => {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

const mergeSet = (prev, value, enabled) => {
  const next = new Set(prev)
  if (enabled) next.add(value)
  else next.delete(value)
  return next
}

const parseIterations = (value) => {
  const raw = String(value ?? '').trim()
  if (!raw) return null
  const parsed = Number.parseInt(raw, 10)
  if (Number.isNaN(parsed) || parsed < 1) return undefined
  return parsed
}

const TaskCard = ({
  task,
  draggable,
  isPending,
  onDragStart,
  onReview,
  onEdit,
  onDelete,
  onCodex,
  onOpenPr,
}) => {
  const priorityValue = clampPriority(task.priority)
  const tone = priorityTone(priorityValue)
  const criteria = Array.isArray(task.acceptance_criteria) ? task.acceptance_criteria : []
  const preview = criteria.slice(0, 3)
  const remaining = criteria.length - preview.length

  return (
    <article
      className={`board-task ${draggable ? 'board-task--draggable' : ''} ${
        isPending ? 'board-task--pending' : ''
      }`}
      draggable={draggable && !isPending}
      onDragStart={draggable ? (event) => onDragStart(event, task.id) : undefined}
    >
      <header className="board-task__header">
        <span className={`priority-tag priority-tag--${tone}`}>P{priorityValue}</span>
        <span className="task-code">{task.id}</span>
      </header>
      <h4 className="board-task__title">{task.title}</h4>
      {task.notes ? <p className="board-task__desc">{task.notes}</p> : null}

      {preview.length ? (
        <ul className="criteria-list">
          {preview.map((item) => (
            <li key={item} className="criteria-item">
              {item}
            </li>
          ))}
          {remaining > 0 ? <li className="criteria-more">+{remaining} more</li> : null}
        </ul>
      ) : null}

      <div className="board-task__meta">
        {task.owner ? <span className="meta-chip">{task.owner}</span> : null}
        {task.effort ? <span className="meta-chip">{task.effort}</span> : null}
        {task.branch ? <span className="meta-chip meta-chip--mono">{task.branch}</span> : null}
        {task.commit ? <span className="meta-chip meta-chip--mono">{task.commit}</span> : null}
        {task.passes ? <span className="meta-chip meta-chip--pass">Passes</span> : null}
      </div>

      {task.status === 'review' ? (
        <>
          <div className="task-actions">
            <button
              className="chip-button chip-button--approve"
              type="button"
              onClick={() => onReview(task.id, 'approved')}
              disabled={isPending}
            >
              Approve
            </button>
            <button
              className="chip-button chip-button--reject"
              type="button"
              onClick={() => onReview(task.id, 'rejected')}
              disabled={isPending}
            >
              Reject
            </button>
          </div>
          <div className="task-actions task-actions--review">
            <button
              className="chip-button chip-button--codex"
              type="button"
              onClick={() => onCodex(task.id)}
              disabled={isPending}
            >
              Codex
            </button>
            <button
              className="chip-button chip-button--openpr"
              type="button"
              onClick={() => onOpenPr(task.id)}
              disabled={isPending}
            >
              openPR
            </button>
          </div>
        </>
      ) : null}

      <div className="task-actions task-actions--secondary">
        <button
          className="chip-button chip-button--edit"
          type="button"
          onClick={() => onEdit(task)}
          disabled={isPending}
        >
          Edit
        </button>
        <button
          className="chip-button chip-button--delete"
          type="button"
          onClick={() => onDelete(task.id)}
          disabled={isPending}
        >
          Delete
        </button>
      </div>

      {task.status === 'done' && task.summary ? (
        <div className="task-summary">
          <span className="summary-label">Summary</span>
          <p className="summary-text">{task.summary}</p>
        </div>
      ) : null}
    </article>
  )
}

export default function App() {
  const apiBaseUrl = useMemo(() => {
    const raw = import.meta.env.VITE_API_URL || DEFAULT_API_URL
    return raw.replace(/\/+$/, '')
  }, [])

  const [healthStatus, setHealthStatus] = useState('checking')
  const [board, setBoard] = useState(null)
  const [agents, setAgents] = useState([])
  const [activeAgentId, setActiveAgentId] = useState('')
  const [fetchError, setFetchError] = useState('')
  const [actionError, setActionError] = useState('')

  const [isAddOpen, setIsAddOpen] = useState(false)
  const [editingTaskId, setEditingTaskId] = useState(null)
  const [quickTitleByColumn, setQuickTitleByColumn] = useState(DEFAULT_QUICK_TITLES)
  const [creatingColumns, setCreatingColumns] = useState(new Set())

  const [newTitle, setNewTitle] = useState('')
  const [newAcceptanceText, setNewAcceptanceText] = useState('')
  const [newPriority, setNewPriority] = useState('1')
  const [newPasses, setNewPasses] = useState(false)
  const [newNotes, setNewNotes] = useState('')
  const [newStatus, setNewStatus] = useState('backlog')
  const [isSavingTask, setIsSavingTask] = useState(false)

  const [isStartingAgent, setIsStartingAgent] = useState(false)
  const [isPullingLatest, setIsPullingLatest] = useState(false)
  const [isClearingLogs, setIsClearingLogs] = useState(false)
  const [isExpandedLog, setIsExpandedLog] = useState(false)
  const [workspacePath, setWorkspacePath] = useState('')
  const [isSavingWorkspace, setIsSavingWorkspace] = useState(false)
  const [workspaceDirty, setWorkspaceDirty] = useState(false)
  const [isCreatingAgent, setIsCreatingAgent] = useState(false)
  const [iterationsInput, setIterationsInput] = useState('10')

  const [pendingTaskIds, setPendingTaskIds] = useState(new Set())
  const [dropTarget, setDropTarget] = useState(null)

  const fetchVersionRef = useRef(0)
  const stateVersionRef = useRef(0)
  const actionsLockRef = useRef(false)
  const activeAgentRef = useRef('')

  const activeAgent = useMemo(
    () => agents.find((item) => item.id === activeAgentId) ?? null,
    [agents, activeAgentId]
  )

  const tasks = board?.tasks ?? []
  const agent = board?.agent ?? null
  const logs = board?.logs ?? []
  const workspacePathFromBoard = board?.workspace_path ?? ''
  const agentLabel = activeAgent?.name ?? agent?.name ?? 'Agent'
  const isEditing = Boolean(editingTaskId)

  useEffect(() => {
    activeAgentRef.current = activeAgentId
  }, [activeAgentId])

  const currentTask = useMemo(() => {
    if (!agent?.current_task_id) return null
    return tasks.find((task) => task.id === agent.current_task_id) ?? null
  }, [agent?.current_task_id, tasks])

  const tasksByStatus = useMemo(() => {
    const map = new Map()
    for (const column of COLUMNS) map.set(column.id, [])
    for (const task of tasks) {
      const bucket = map.get(normalizeStatus(task.status)) ?? map.get('backlog')
      bucket.push(task)
    }
    return map
  }, [tasks])

  const agentHeadline =
    agent?.status === 'running'
      ? currentTask
        ? `${agentLabel}: Working on ${currentTask.id}`
        : `${agentLabel}: Running`
      : `${agentLabel}: Waiting for tasks`

  const buildAgentUrl = (path, agentId) => {
    const url = new URL(`${apiBaseUrl}${path}`)
    if (agentId) url.searchParams.set('agent_id', agentId)
    return url.toString()
  }

  const bumpStateVersion = () => {
    stateVersionRef.current += 1
  }

  const syncAgentsFromBoard = (data) => {
    if (!data?.agent?.id) return
    const snapshot = {
      id: data.agent.id,
      name: data.agent.name,
      status: data.agent.status,
      current_task_id: data.agent.current_task_id ?? null,
    }
    setAgents((prev) => {
      const existing = prev.find((item) => item.id === snapshot.id)
      if (!existing) {
        return [...prev, snapshot]
      }
      return prev.map((item) => (item.id === snapshot.id ? { ...item, ...snapshot } : item))
    })
  }

  const refreshState = async (agentId, signal) => {
    if (!agentId) return null
    const version = fetchVersionRef.current + 1
    fetchVersionRef.current = version
    const guard = stateVersionRef.current

    const res = await fetch(buildAgentUrl('/api/state', agentId), { signal })
    if (!res.ok) throw new Error(`State fetch failed (${res.status})`)
    const data = await res.json()
    if (
      fetchVersionRef.current === version &&
      stateVersionRef.current === guard &&
      activeAgentRef.current === agentId
    ) {
      setBoard(data)
      syncAgentsFromBoard(data)
      setFetchError('')
      setHealthStatus('online')
    }
    return data
  }

  const loadAgents = async (signal) => {
    const res = await fetch(`${apiBaseUrl}/api/agents`, { signal })
    if (!res.ok) throw new Error(`Agents fetch failed (${res.status})`)
    const data = await res.json()
    const nextAgents = Array.isArray(data?.agents) ? data.agents : []
    setAgents(nextAgents)
    if (nextAgents.length === 0) {
      setActiveAgentId('')
      setBoard(null)
      return nextAgents
    }
    setActiveAgentId((prev) => {
      if (prev && nextAgents.some((item) => item.id === prev)) return prev
      return nextAgents[0].id
    })
    return nextAgents
  }

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    let timerId

    const tick = async () => {
      try {
        await loadAgents(controller.signal)
        if (active) {
          setFetchError('')
          setHealthStatus('online')
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setFetchError('Unable to reach the API. Start the backend and try again.')
          setHealthStatus('offline')
          if (active) timerId = window.setTimeout(tick, 3200)
        }
      }
    }

    tick()
    return () => {
      active = false
      controller.abort()
      window.clearTimeout(timerId)
    }
  }, [apiBaseUrl])

  useEffect(() => {
    if (!activeAgentId) return
    let timerId
    const controller = new AbortController()
    let active = true

    const tick = async () => {
      try {
        const data = await refreshState(activeAgentId, controller.signal)
        const nextDelay = data?.agent?.status === 'running' ? 1200 : 2800
        if (active) timerId = window.setTimeout(tick, nextDelay)
      } catch (error) {
        if (!controller.signal.aborted) {
          setFetchError('Unable to reach the API. Start the backend and try again.')
          setHealthStatus('offline')
          if (active) timerId = window.setTimeout(tick, 3200)
        }
      }
    }

    tick()
    return () => {
      active = false
      controller.abort()
      window.clearTimeout(timerId)
    }
  }, [apiBaseUrl, activeAgentId])

  useEffect(() => {
    if (!workspaceDirty && workspacePathFromBoard !== workspacePath) {
      setWorkspacePath(workspacePathFromBoard)
    }
  }, [workspaceDirty, workspacePath, workspacePathFromBoard])

  useEffect(() => {
    setBoard(null)
    setIsAddOpen(false)
    setEditingTaskId(null)
    setQuickTitleByColumn({ ...DEFAULT_QUICK_TITLES })
    setCreatingColumns(new Set())
    setNewTitle('')
    setNewAcceptanceText('')
    setNewPriority('1')
    setNewPasses(false)
    setNewNotes('')
    setNewStatus('backlog')
    setIsSavingTask(false)
    setIsStartingAgent(false)
    setIsPullingLatest(false)
    setIsClearingLogs(false)
    setIsSavingWorkspace(false)
    setWorkspacePath('')
    setWorkspaceDirty(false)
    setPendingTaskIds(new Set())
    setDropTarget(null)
    setActionError('')
  }, [activeAgentId])

  const resetModalFields = (status = 'backlog') => {
    setNewTitle('')
    setNewAcceptanceText('')
    setNewPriority('1')
    setNewPasses(false)
    setNewNotes('')
    setNewStatus(status)
  }

  const closeModal = () => {
    setIsAddOpen(false)
    setEditingTaskId(null)
    resetModalFields('backlog')
  }

  const openModalForColumn = (status, seedTitle = '') => {
    setEditingTaskId(null)
    resetModalFields(status)
    if (seedTitle) {
      setNewTitle(seedTitle)
    }
    setIsAddOpen(true)
  }

  const openModalForEdit = (task) => {
    setEditingTaskId(task.id)
    setNewTitle(task.title ?? '')
    setNewAcceptanceText(formatAcceptanceCriteria(task.acceptance_criteria))
    setNewPriority(String(clampPriority(task.priority)))
    setNewPasses(Boolean(task.passes))
    setNewNotes(task.notes ?? '')
    setNewStatus(normalizeStatus(task.status))
    setIsAddOpen(true)
    setActionError('')
  }

  const createTask = async ({ title, acceptance_criteria, priority, passes, notes, status }) => {
    if (!activeAgentId) throw new Error('No agent selected')
    const requestAgentId = activeAgentId
    const payload = {
      title,
      acceptance_criteria,
      priority: clampPriority(priority),
      passes,
      notes,
      status,
    }

    const res = await fetch(buildAgentUrl('/api/tasks', requestAgentId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })

    if (!res.ok) {
      throw new Error(`Create failed (${res.status})`)
    }

    const task = await res.json()
    bumpStateVersion()
    if (activeAgentRef.current !== requestAgentId) return task
    setBoard((prev) => {
      const fallbackAgent = {
        id: requestAgentId,
        name: agentLabel,
        status: 'waiting',
        signal: 'RALPH_WAITING',
        last_update: new Date().toISOString(),
        current_task_id: null,
      }

      if (!prev || prev.agent?.id !== requestAgentId) {
        return {
          agent: fallbackAgent,
          tasks: [task],
          logs: [],
          workspace_path: '',
        }
      }

      if (prev.tasks.some((existing) => existing.id === task.id)) {
        return prev
      }

      return {
        ...prev,
        tasks: [...prev.tasks, task],
      }
    })

    return task
  }

  const safePost = async (path, body, setPending, agentId = activeAgentId) => {
    if (!agentId) return null
    if (actionsLockRef.current) return null
    const requestAgentId = agentId
    actionsLockRef.current = true
    setPending(true)
    setActionError('')
    try {
      const res = await fetch(buildAgentUrl(path, requestAgentId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      })
      if (!res.ok) throw new Error(`Request failed (${res.status})`)
      const data = await res.json()
      bumpStateVersion()
      if (activeAgentRef.current === requestAgentId) {
        setBoard(data)
        syncAgentsFromBoard(data)
      }
      return data
    } catch (error) {
      setActionError('Action failed. Try again.')
      return null
    } finally {
      actionsLockRef.current = false
      setPending(false)
    }
  }

  const handleAddAgent = async () => {
    if (isCreatingAgent) return
    setIsCreatingAgent(true)
    setActionError('')
    try {
      const res = await fetch(`${apiBaseUrl}/api/agents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!res.ok) throw new Error(`Create agent failed (${res.status})`)
      const data = await res.json()
      setAgents((prev) => [...prev, data])
      setActiveAgentId(data.id)
    } catch (error) {
      setActionError('Unable to add agent. Try again.')
    } finally {
      setIsCreatingAgent(false)
    }
  }

  const handleStartAll = async () => {
    const iterations = parseIterations(iterationsInput)
    if (iterations === undefined) {
      setActionError('Iterations must be a positive number.')
      return
    }
    await safePost('/api/agents/start-all', iterations === null ? null : { iterations }, () => {})
  }

  const handleStartAgent = async () => {
    if (isStartingAgent || !activeAgentId) return
    const iterations = parseIterations(iterationsInput)
    if (iterations === undefined) {
      setActionError('Iterations must be a positive number.')
      return
    }
    await safePost('/api/ralph/start', iterations === null ? null : { iterations }, setIsStartingAgent)
  }

  const handlePullLatest = async () => {
    if (isPullingLatest || !activeAgentId) return
    await safePost('/api/pull-latest', null, setIsPullingLatest)
  }

  const handleClearLogs = async () => {
    if (isClearingLogs || !activeAgentId) return
    await safePost('/api/logs/clear', null, setIsClearingLogs)
  }

  const handleWorkspaceChange = (event) => {
    const nextValue = event.target.value
    setWorkspacePath(nextValue)
    setWorkspaceDirty(nextValue !== workspacePathFromBoard)
  }

  const handleWorkspaceSave = async (event) => {
    event.preventDefault()
    if (isSavingWorkspace || !activeAgentId) return
    const trimmed = workspacePath.trim()
    if (!trimmed) return
    const data = await safePost('/api/workspace', { path: trimmed }, setIsSavingWorkspace)
    if (data) {
      setWorkspaceDirty(false)
    }
  }

  const handleCreateTask = async (event) => {
    event.preventDefault()
    if (isSavingTask || !activeAgentId) return

    const title = newTitle.trim()
    if (!title) {
      setActionError('Task title is required.')
      return
    }

    const acceptance = parseAcceptanceCriteria(newAcceptanceText)
    setIsSavingTask(true)
    setActionError('')
    try {
      const payload = {
        title,
        acceptance_criteria: acceptance,
        priority: newPriority,
        passes: newPasses,
        notes: newNotes.trim(),
        status: newStatus === 'done' ? 'review' : newStatus,
      }

      if (isEditing) {
        await updateTask(editingTaskId, payload)
      } else {
        await createTask(payload)
      }
      closeModal()
    } catch (error) {
      setActionError(isEditing ? 'Unable to update task. Try again.' : 'Unable to add task. Check the API and try again.')
    } finally {
      setIsSavingTask(false)
    }
  }

  const handleQuickTitleChange = (columnId, value) => {
    setQuickTitleByColumn((prev) => ({
      ...prev,
      [columnId]: value,
    }))
  }

  const handleQuickAdd = async (columnId) => {
    if (!QUICK_COLUMNS.has(columnId)) return
    if (creatingColumns.has(columnId) || !activeAgentId) return

    const title = (quickTitleByColumn[columnId] || '').trim()
    if (!title) {
      setActionError('Task title is required.')
      return
    }

    setCreatingColumns((prev) => mergeSet(prev, columnId, true))
    setActionError('')
    try {
      await createTask({
        title,
        acceptance_criteria: [],
        priority: 1,
        passes: false,
        notes: '',
        status: columnId,
      })
      setQuickTitleByColumn((prev) => ({
        ...prev,
        [columnId]: '',
      }))
    } catch (error) {
      setActionError('Unable to add task. Check the API and try again.')
    } finally {
      setCreatingColumns((prev) => mergeSet(prev, columnId, false))
    }
  }

  const updateTask = async (taskId, patch) => {
    if (!taskId || !activeAgentId) return
    const requestAgentId = activeAgentId
    const nextPatch = patch?.status === 'done' ? { ...patch, status: 'review' } : patch
    setPendingTaskIds((prev) => mergeSet(prev, taskId, true))
    setActionError('')
    try {
      const res = await fetch(buildAgentUrl(`/api/tasks/${taskId}`, requestAgentId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(nextPatch),
      })
      if (!res.ok) throw new Error(`Update failed (${res.status})`)
      const updated = await res.json()
      bumpStateVersion()
      if (activeAgentRef.current !== requestAgentId) return
      setBoard((prev) => {
        if (!prev || prev.agent?.id !== requestAgentId) return prev
        return {
          ...prev,
          tasks: prev.tasks.map((task) => (task.id === updated.id ? updated : task)),
        }
      })
    } catch (error) {
      setActionError('Unable to update task. Try again.')
    } finally {
      setPendingTaskIds((prev) => mergeSet(prev, taskId, false))
    }
  }

  const deleteTask = async (taskId) => {
    if (!taskId || !activeAgentId) return
    if (!window.confirm('Delete task?')) return
    const requestAgentId = activeAgentId
    setPendingTaskIds((prev) => mergeSet(prev, taskId, true))
    setActionError('')
    try {
      const res = await fetch(buildAgentUrl(`/api/tasks/${taskId}`, requestAgentId), {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(`Delete failed (${res.status})`)
      await res.json()
      bumpStateVersion()
      if (activeAgentRef.current !== requestAgentId) return
      setBoard((prev) => {
        if (!prev || prev.agent?.id !== requestAgentId) return prev
        const nextTasks = prev.tasks.filter((task) => task.id !== taskId)
        const nextAgent =
          prev.agent?.current_task_id === taskId
            ? { ...prev.agent, current_task_id: null }
            : prev.agent
        return {
          ...prev,
          tasks: nextTasks,
          agent: nextAgent,
        }
      })
    } catch (error) {
      setActionError('Unable to delete task. Try again.')
    } finally {
      setPendingTaskIds((prev) => mergeSet(prev, taskId, false))
    }
  }

  const runTaskAction = async (taskId, action, errorMessage) => {
    if (!taskId || !activeAgentId) return
    const requestAgentId = activeAgentId
    setPendingTaskIds((prev) => mergeSet(prev, taskId, true))
    setActionError('')
    try {
      const res = await fetch(buildAgentUrl(`/api/tasks/${taskId}/${action}`, requestAgentId), {
        method: 'POST',
      })
      if (!res.ok) throw new Error(`Request failed (${res.status})`)
      const data = await res.json()
      bumpStateVersion()
      if (activeAgentRef.current !== requestAgentId) return
      setBoard(data)
      syncAgentsFromBoard(data)
    } catch (error) {
      setActionError(errorMessage)
    } finally {
      setPendingTaskIds((prev) => mergeSet(prev, taskId, false))
    }
  }

  const handleCodex = async (taskId) => {
    await runTaskAction(taskId, 'codex', 'Unable to open Codex. Try again.')
  }

  const handleOpenPr = async (taskId) => {
    await runTaskAction(taskId, 'openpr', 'OpenPR failed. Try again.')
  }

  const handleReview = async (taskId, decision) => {
    if (!activeAgentId) return
    const requestAgentId = activeAgentId
    setPendingTaskIds((prev) => mergeSet(prev, taskId, true))
    setActionError('')
    try {
      const res = await fetch(buildAgentUrl(`/api/tasks/${taskId}/review`, requestAgentId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      })
      if (!res.ok) throw new Error(`Review failed (${res.status})`)
      const updated = await res.json()
      bumpStateVersion()
      if (activeAgentRef.current !== requestAgentId) return
      setBoard((prev) => {
        if (!prev || prev.agent?.id !== requestAgentId) return prev
        return {
          ...prev,
          tasks: prev.tasks.map((task) => (task.id === updated.id ? updated : task)),
        }
      })
    } catch (error) {
      setActionError('Review failed. Try again.')
    } finally {
      setPendingTaskIds((prev) => mergeSet(prev, taskId, false))
    }
  }

  const handleDragStart = (event, taskId) => {
    setDropTarget(null)
    event.dataTransfer.setData('text/plain', taskId)
    event.dataTransfer.effectAllowed = 'move'
  }

  const handleDrop = async (event, status) => {
    event.preventDefault()
    setDropTarget(null)
    const taskId = event.dataTransfer.getData('text/plain')
    const task = tasks.find((item) => item.id === taskId)
    if (!task) return
    const currentStatus = normalizeStatus(task.status)
    const targetStatus = status === 'done' ? 'review' : status
    if (currentStatus === targetStatus) return
    await updateTask(taskId, { status: targetStatus })
  }

  const hasWorkspacePath = workspacePath.trim().length > 0
  const canSaveWorkspace = workspaceDirty && hasWorkspacePath && !isSavingWorkspace
  const workspaceButtonLabel = isSavingWorkspace
    ? 'Saving'
    : workspaceDirty || !hasWorkspacePath
      ? 'Save'
      : 'Saved'

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>ralpheed</h1>
        </div>
        <div className="top-actions">
          <span className={`pill pill--status pill--${healthStatus}`}>API {healthStatus}</span>
          <button className="ghost-button" type="button" onClick={handlePullLatest} disabled={isPullingLatest}>
            Pull Latest
          </button>
          <button
            className="primary-button"
            type="button"
            onClick={() => openModalForColumn('backlog')}
            disabled={!activeAgentId}
          >
            + Add Task
          </button>
        </div>
      </header>

      <section className="agent-tabs" aria-label="Agents">
        <div className="agent-tabs__list">
          {agents.map((item) => (
            <button
              key={item.id}
              className={`agent-tab ${item.id === activeAgentId ? 'agent-tab--active' : ''}`}
              type="button"
              onClick={() => setActiveAgentId(item.id)}
            >
              {item.name}
            </button>
          ))}
        </div>
        <button
          className="ghost-button"
          type="button"
          onClick={handleAddAgent}
          disabled={isCreatingAgent}
        >
          {isCreatingAgent ? 'Adding' : '+ Add Agent'}
        </button>
      </section>

      {fetchError ? <p className="callout callout--error">{fetchError}</p> : null}
      {actionError ? <p className="callout callout--error">{actionError}</p> : null}

      <section className="agent-panel">
        <div className="agent-panel__header">
          <div className="agent-headline">
            <span className={`agent-dot agent-dot--${agent?.status ?? 'waiting'}`} />
            <p className="agent-title">{agentHeadline}</p>
          </div>
          <div className="agent-actions">
            <label className="agent-iterations">
              <span>Iterations</span>
              <input
                type="number"
                min="1"
                inputMode="numeric"
                value={iterationsInput}
                onChange={(event) => setIterationsInput(event.target.value)}
                placeholder="Default"
                disabled={!activeAgentId}
              />
            </label>
            <button className="ghost-button ghost-button--inverse" type="button" onClick={handleStartAll}>
              Start All Agents
            </button>
            <button
              className="primary-button primary-button--inverse"
              type="button"
              onClick={handleStartAgent}
              disabled={isStartingAgent || agent?.status === 'running' || !activeAgentId}
            >
              {agent?.status === 'running'
                ? `${agentLabel} running`
                : isStartingAgent
                  ? 'Starting...'
                  : `Start ${agentLabel}`}
            </button>
          </div>
        </div>

        <div className="agent-panel__grid">
          <div className="agent-card">
            <p className="agent-card__label">Current task</p>
            {currentTask ? (
              <>
                <p className="agent-card__value">
                  {currentTask.id} <span className="agent-card__muted">{currentTask.title}</span>
                </p>
                <p className="agent-card__sub">Started at {formatTime(currentTask.updated_at)}</p>
              </>
            ) : (
              <>
                <p className="agent-card__value agent-card__muted">No active task</p>
                <p className="agent-card__sub">Move tasks from Backlog to Todo and start {agentLabel}.</p>
              </>
            )}
          </div>
          <div className="agent-card">
            <p className="agent-card__label">Status</p>
            <div className="agent-status">
              <div>
                <p className="agent-status__key">State</p>
                <p className="agent-status__value">{agent?.status ?? '-'}</p>
              </div>
              <div>
                <p className="agent-status__key">Signal</p>
                <p className="agent-status__value">{agent?.signal ?? '-'}</p>
              </div>
              <div>
                <p className="agent-status__key">Last update</p>
                <p className="agent-status__value">{formatClock(agent?.last_update)}</p>
              </div>
            </div>
          </div>
          <div className="agent-card agent-card--workspace">
            <p className="agent-card__label">Workspace</p>
            <form className="workspace-form" onSubmit={handleWorkspaceSave}>
              <div className="workspace-form__row">
                <input
                  className="workspace-form__input"
                  value={workspacePath}
                  onChange={handleWorkspaceChange}
                  placeholder="/path/to/project"
                  aria-label="Workspace path"
                  disabled={!activeAgentId}
                />
                <button className="workspace-form__button" type="submit" disabled={!canSaveWorkspace}>
                  {workspaceButtonLabel}
                </button>
              </div>
            </form>
          </div>
        </div>

        <div className="log-panel">
          <div className="log-panel__header">
            <div>
              <p className="log-panel__label">Progress log</p>
              <p className="log-panel__hint">Latest worker events and state changes.</p>
            </div>
            <div className="log-panel__actions">
              <button
                className="ghost-button ghost-button--inverse"
                type="button"
                onClick={handleClearLogs}
                disabled={isClearingLogs || !activeAgentId}
              >
                Clear
              </button>
              <button
                className="ghost-button ghost-button--inverse"
                type="button"
                onClick={() => setIsExpandedLog((prev) => !prev)}
              >
                {isExpandedLog ? 'Collapse' : 'Expand'}
              </button>
            </div>
          </div>
          <pre className={`log-panel__body ${isExpandedLog ? 'log-panel__body--expanded' : ''}`}>
            {logs.length ? logs.join('\n') : 'RALPH_WAITING - No todo tasks'}
          </pre>
        </div>
      </section>

      <section className="board" aria-label="Task board">
        {COLUMNS.map((column) => {
          const columnTasks = tasksByStatus.get(column.id) ?? []
          const canDrop = true
          const isDropTarget = dropTarget === column.id
          const quickValue = quickTitleByColumn[column.id] ?? ''
          const isQuickCreate = creatingColumns.has(column.id)
          const allowQuickCreate = QUICK_COLUMNS.has(column.id)

          return (
            <div
              key={column.id}
              className={`column ${isDropTarget ? 'column--drop' : ''}`}
              data-status={column.id}
              onDragOver={
                canDrop
                  ? (event) => {
                      event.preventDefault()
                      event.dataTransfer.dropEffect = 'move'
                    }
                  : undefined
              }
              onDragEnter={canDrop ? () => setDropTarget(column.id) : undefined}
              onDragLeave={canDrop ? () => setDropTarget(null) : undefined}
              onDrop={canDrop ? (event) => handleDrop(event, column.id) : undefined}
            >
              <header className="column__header">
                <h3 className="column__title">{column.label}</h3>
                <div className="column__meta">
                  <span className="column__count">{columnTasks.length}</span>
                  {allowQuickCreate ? (
                    <button
                      className="column__action"
                      type="button"
                      onClick={() => openModalForColumn(column.id)}
                      aria-label={`Add detailed task to ${column.label}`}
                      disabled={!activeAgentId}
                    >
                      +
                    </button>
                  ) : null}
                </div>
              </header>

              {allowQuickCreate ? (
                <form
                  className="column__add"
                  onSubmit={(event) => {
                    event.preventDefault()
                    handleQuickAdd(column.id)
                  }}
                >
                  <input
                    className="column__input"
                    value={quickValue}
                    onChange={(event) => handleQuickTitleChange(column.id, event.target.value)}
                    placeholder={`Add to ${column.label}`}
                    aria-label={`Add task to ${column.label}`}
                    disabled={!activeAgentId}
                  />
                  <button
                    className="column__button"
                    type="submit"
                    disabled={isQuickCreate || !quickValue.trim() || !activeAgentId}
                  >
                    {isQuickCreate ? 'Adding' : 'Add'}
                  </button>
                </form>
              ) : null}

              <div className="column__body">
                {columnTasks.length === 0 ? (
                  <p className="empty empty--tight">No tasks</p>
                ) : (
                  columnTasks.map((task) => (
                    <TaskCard
                      key={task.id}
                      task={task}
                      draggable={!pendingTaskIds.has(task.id)}
                      isPending={pendingTaskIds.has(task.id)}
                      onDragStart={handleDragStart}
                      onReview={handleReview}
                      onEdit={openModalForEdit}
                      onDelete={deleteTask}
                      onCodex={handleCodex}
                      onOpenPr={handleOpenPr}
                    />
                  ))
                )}
              </div>
            </div>
          )
        })}
      </section>

      {isAddOpen ? (
        <div className="modal-overlay" role="dialog" aria-modal="true">
          <div className="modal">
            <header className="modal__header">
              <h2>{isEditing ? 'Edit task' : 'Add task'}</h2>
              <button className="ghost-button" type="button" onClick={closeModal}>
                Close
              </button>
            </header>
            <form className="modal__form" onSubmit={handleCreateTask}>
              <label className="field">
                <span>Title</span>
                <input
                  value={newTitle}
                  onChange={(event) => setNewTitle(event.target.value)}
                  placeholder="Describe the task"
                  autoFocus
                />
              </label>
              <div className="modal__row">
                <label className="field field--inline">
                  <span>Priority</span>
                  <select value={newPriority} onChange={(event) => setNewPriority(event.target.value)}>
                    <option value="1">P1 (high)</option>
                    <option value="2">P2 (medium)</option>
                    <option value="3">P3 (low)</option>
                  </select>
                </label>
                <label className="field field--inline">
                  <span>Column</span>
                  <select value={newStatus} onChange={(event) => setNewStatus(event.target.value)}>
                    {COLUMNS.map((column) => (
                      <option key={column.id} value={column.id}>
                        {column.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="field">
                <div className="field__row">
                  <span>Acceptance criteria (one per line)</span>
                  <button className="field__button" type="button" disabled title="Coming soon">
                    AI
                  </button>
                </div>
                <textarea
                  value={newAcceptanceText}
                  onChange={(event) => setNewAcceptanceText(event.target.value)}
                  placeholder="Define what must pass for this story"
                  rows={4}
                />
              </label>
              <label className="field">
                <span>Notes</span>
                <textarea
                  value={newNotes}
                  onChange={(event) => setNewNotes(event.target.value)}
                  placeholder="Extra context for Ralph and reviewers"
                  rows={3}
                />
              </label>
              <label className="field field--toggle">
                <input
                  type="checkbox"
                  checked={newPasses}
                  onChange={(event) => setNewPasses(event.target.checked)}
                />
                <span>Passes acceptance criteria</span>
              </label>
              <button className="primary-button" type="submit" disabled={isSavingTask}>
                {isSavingTask ? (isEditing ? 'Saving...' : 'Adding...') : isEditing ? 'Save changes' : 'Add task'}
              </button>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  )
}
