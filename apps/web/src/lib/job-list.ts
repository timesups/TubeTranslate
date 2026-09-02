import type {
  ExecutionMode,
  PackageStatus,
  TaskListExecutionMode,
  TaskListSort,
  TaskListStatus,
  TaskPackage,
  TaskSummary,
  TaskStatus,
} from "@/lib/api"

export type JobKindFilter = "all" | "task" | "package"

export type JobListEntry =
  | {
      kind: "task"
      id: string
      title: string
      subtitle: string
      status: TaskStatus
      created_at: string
      started_at: string | null
      completed_at: string | null
      execution_mode?: ExecutionMode
      current_stage: string | null
      href: string
      task: TaskSummary
    }
  | {
      kind: "package"
      id: string
      title: string
      subtitle: string
      status: PackageStatus
      created_at: string
      started_at: string | null
      completed_at: string | null
      execution_mode?: ExecutionMode
      current_stage: null
      href: string
      package: TaskPackage
    }

const STATUS_ORDER: Record<string, number> = {
  queued: 1,
  running: 2,
  paused: 3,
  partial: 4,
  failed: 4,
  succeeded: 5,
}

function shortUrl(url: string) {
  return url.replace(/^https?:\/\/(www\.)?/, "")
}

export function taskToJobEntry(task: TaskSummary): JobListEntry {
  return {
    kind: "task",
    id: task.id,
    title: task.title || shortUrl(task.url),
    subtitle: task.url,
    status: task.status,
    created_at: task.created_at,
    started_at: task.started_at,
    completed_at: task.completed_at,
    execution_mode: task.execution_mode,
    current_stage: task.current_stage,
    href: `/tasks/${task.id}`,
    task,
  }
}

export function packageToJobEntry(pkg: TaskPackage): JobListEntry {
  const succeeded = pkg.succeeded_count ?? 0
  const total = pkg.item_count ?? pkg.items?.length ?? 0
  return {
    kind: "package",
    id: pkg.id,
    title: pkg.name || pkg.source_root,
    subtitle: `${succeeded}/${total} · ${pkg.source_root}`,
    status: pkg.status,
    created_at: pkg.created_at,
    started_at: pkg.started_at,
    completed_at: pkg.completed_at,
    execution_mode: pkg.execution_mode,
    current_stage: null,
    href: `/packages/${pkg.id}`,
    package: pkg,
  }
}

function matchesStatus(entry: JobListEntry, status: TaskListStatus) {
  if (status === "all") return true
  if (entry.kind === "package" && status === "failed") {
    return entry.status === "failed" || entry.status === "partial"
  }
  return entry.status === status
}

function matchesExecutionMode(entry: JobListEntry, mode: TaskListExecutionMode) {
  if (mode === "all") return true
  return entry.execution_mode === mode
}

function matchesQuery(entry: JobListEntry, query: string) {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return (
    entry.title.toLowerCase().includes(needle)
    || entry.subtitle.toLowerCase().includes(needle)
    || entry.id.toLowerCase().includes(needle)
  )
}

export function filterJobEntries(
  entries: JobListEntry[],
  filters: {
    query: string
    status: TaskListStatus
    execution_mode: TaskListExecutionMode
  },
) {
  return entries.filter(
    (entry) =>
      matchesQuery(entry, filters.query)
      && matchesStatus(entry, filters.status)
      && matchesExecutionMode(entry, filters.execution_mode),
  )
}

export function filterJobEntriesByStatusAndMode(
  entries: JobListEntry[],
  filters: {
    status: TaskListStatus
    execution_mode: TaskListExecutionMode
  },
) {
  return entries.filter(
    (entry) =>
      matchesStatus(entry, filters.status)
      && matchesExecutionMode(entry, filters.execution_mode),
  )
}

function compareNullableTime(left: string | null, right: string | null) {
  const leftTime = left ? Date.parse(left) : Number.NaN
  const rightTime = right ? Date.parse(right) : Number.NaN
  if (Number.isNaN(leftTime) && Number.isNaN(rightTime)) return 0
  if (Number.isNaN(leftTime)) return 1
  if (Number.isNaN(rightTime)) return -1
  return leftTime - rightTime
}

export function sortJobEntries(entries: JobListEntry[], sort: TaskListSort) {
  const sorted = [...entries]
  sorted.sort((left, right) => {
    switch (sort) {
      case "created_asc":
        return compareNullableTime(left.created_at, right.created_at)
      case "started_desc":
        return compareNullableTime(right.started_at, left.started_at)
      case "started_asc":
        return compareNullableTime(left.started_at, right.started_at)
      case "completed_desc":
        return compareNullableTime(right.completed_at, left.completed_at)
      case "completed_asc":
        return compareNullableTime(left.completed_at, right.completed_at)
      case "status_asc":
        return (STATUS_ORDER[left.status] ?? 99) - (STATUS_ORDER[right.status] ?? 99)
      case "status_desc":
        return (STATUS_ORDER[right.status] ?? 99) - (STATUS_ORDER[left.status] ?? 99)
      case "title_asc":
        return left.title.localeCompare(right.title, undefined, { sensitivity: "base" })
      case "title_desc":
        return right.title.localeCompare(left.title, undefined, { sensitivity: "base" })
      case "created_desc":
      default:
        return compareNullableTime(right.created_at, left.created_at)
    }
  })
  return sorted
}
