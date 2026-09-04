"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { ChangeEvent, FormEvent, useCallback, useMemo, useRef, useState } from "react"
import { ChevronLeft, ChevronRight, FolderMinus, Loader2, Pause, Play, RotateCw, Search, Trash2, Upload } from "lucide-react"

import {
  AudioMode,
  ExecutionMode,
  LocalDirection,
  TaskListExecutionMode,
  TaskListResponse,
  TaskListSort,
  TaskListStatus,
  TaskSummary,
  TtsProvider,
  cleanupTasksBatch,
  createTasksBatch,
  createTaskPackage,
  cleanupPackagesBatch,
  continueTask,
  continueTaskPackage,
  deletePackagesBatch,
  deleteTasksBatch,
  isAbortError,
  listTaskPackages,
  listTasks,
  parseVideoUrls,
  pauseTask,
  pauseTaskPackage,
  retryFailedPackagesBatch,
  resumeTasksBatch,
  retryFailedTaskPackage,
  scanTaskPackage,
  TaskPackage,
  uploadLocalTasks,
} from "@/lib/api"
import { BILIBILI_PARTITIONS, DEFAULT_BILIBILI_TID } from "@/lib/bilibili-partitions"
import { useI18n } from "@/lib/i18n"
import { statusBadgeClass } from "@/lib/status"
import { SerialPollingContext, useSerialPolling } from "@/lib/use-serial-polling"
import uploadContract from "@/lib/upload-contract.json"
import { AppShell } from "@/components/app-shell"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import {
  JobKindFilter,
  filterJobEntries,
  filterJobEntriesByStatusAndMode,
  packageToJobEntry,
  sortJobEntries,
  taskToJobEntry,
} from "@/lib/job-list"

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100]
const TASK_SEARCH_MAX_LENGTH = 200
const LOCAL_VIDEO_ACCEPT = uploadContract.video_extensions.join(",")

function truncateSearchQuery(value: string) {
  return Array.from(value).slice(0, TASK_SEARCH_MAX_LENGTH).join("")
}

function isActive(status: string) {
  return status === "queued" || status === "running"
}

function isAwaitingAction(status: string) {
  return status === "paused"
}

function isDeletable(status: string) {
  return status !== "running"
}

function isFinished(status: string) {
  return status === "succeeded" || status === "failed" || status === "partial"
}

function isPackageDeletable(status: string) {
  return status !== "running"
}

function packageHasRetryableFailures(pkg: TaskPackage) {
  return (pkg.failed_count ?? 0) > 0 && !["running", "queued"].includes(pkg.status)
}

function selectedCountText(template: string, count: number) {
  return template.replace("{count}", String(count))
}

function batchDeleteSummaryText(
  template: string,
  deleted: number,
  skipped: number,
  missing: number,
  failed: number,
) {
  return template
    .replace("{deleted}", String(deleted))
    .replace("{skipped}", String(skipped))
    .replace("{missing}", String(missing))
    .replace("{failed}", String(failed))
}

function batchCleanupSummaryText(
  template: string,
  cleaned: number,
  skipped: number,
  missing: number,
  failed: number,
) {
  return template
    .replace("{cleaned}", String(cleaned))
    .replace("{skipped}", String(skipped))
    .replace("{missing}", String(missing))
    .replace("{failed}", String(failed))
}

function batchRetrySummaryText(
  template: string,
  resumed: number,
  skipped: number,
  missing: number,
  failed: number,
) {
  return template
    .replace("{resumed}", String(resumed))
    .replace("{skipped}", String(skipped))
    .replace("{missing}", String(missing))
    .replace("{failed}", String(failed))
}

function formatTime(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function shortUrl(url: string) {
  return url.replace(/^https?:\/\/(www\.)?/, "")
}

function selectedLabel<T extends string>(options: { value: T; label: string }[], value: T) {
  return options.find((option) => option.value === value)?.label || value
}

function pageRangeText(language: string, start: number, end: number, total: number) {
  if (language === "zh") return `显示 ${start}-${end} / 共 ${total} 项`
  return `Showing ${start}-${end} of ${total}`
}

function pageIndexText(language: string, page: number, totalPages: number) {
  if (language === "zh") return `第 ${page} / ${totalPages} 页`
  return `Page ${page} / ${totalPages}`
}

function batchSummaryText(
  template: string,
  created: number,
  existing: number,
  failed: number,
) {
  return template
    .replace("{created}", String(created))
    .replace("{existing}", String(existing))
    .replace("{failed}", String(failed))
}

function localBatchSummaryText(template: string, created: number, failed: number) {
  return template
    .replace("{created}", String(created))
    .replace("{failed}", String(failed))
}

export default function Home() {
  const router = useRouter()
  const { activeTasksText, language, stageLabel, statusLabel, t } = useI18n()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const subtitleInputRef = useRef<HTMLInputElement>(null)
  const [urlsText, setUrlsText] = useState("")
  const [localFiles, setLocalFiles] = useState<File[]>([])
  const [localSubtitleFile, setLocalSubtitleFile] = useState<File | null>(null)
  const [localDirection, setLocalDirection] = useState<LocalDirection>("en-zh")
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("auto")
  const [audioMode, setAudioMode] = useState<AudioMode>("replace")
  const [ttsProvider, setTtsProvider] = useState<TtsProvider>("azure")
  const [bilibiliTid, setBilibiliTid] = useState(DEFAULT_BILIBILI_TID)
  const [bilibiliAutoPublish, setBilibiliAutoPublish] = useState(true)
  const [bilibiliGenerateMeta, setBilibiliGenerateMeta] = useState(true)
  const [douyinAutoPublish, setDouyinAutoPublish] = useState(false)
  const [douyinGenerateMeta, setDouyinGenerateMeta] = useState(true)
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [taskTotal, setTaskTotal] = useState(0)
  const [activeTaskCount, setActiveTaskCount] = useState<number | null>(null)
  const [taskPage, setTaskPage] = useState(1)
  const [taskPageSize, setTaskPageSize] = useState(20)
  const [taskQuery, setTaskQuery] = useState("")
  const [taskStatus, setTaskStatus] = useState<TaskListStatus>("all")
  const [taskExecutionMode, setTaskExecutionMode] = useState<TaskListExecutionMode>("all")
  const [jobKindFilter, setJobKindFilter] = useState<JobKindFilter>("all")
  const [taskSort, setTaskSort] = useState<TaskListSort>("created_desc")
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [taskListError, setTaskListError] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(() => new Set())
  const [selectedPackageIds, setSelectedPackageIds] = useState<Set<string>>(() => new Set())
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [batchDeleteError, setBatchDeleteError] = useState("")
  const [batchCleanupOpen, setBatchCleanupOpen] = useState(false)
  const [batchCleaning, setBatchCleaning] = useState(false)
  const [batchCleanupError, setBatchCleanupError] = useState("")
  const [batchRetryOpen, setBatchRetryOpen] = useState(false)
  const [batchRetrying, setBatchRetrying] = useState(false)
  const [batchRetryError, setBatchRetryError] = useState("")
  const [packageSourceDir, setPackageSourceDir] = useState("")
  const [packageRecursive, setPackageRecursive] = useState(false)
  const [packageScanCount, setPackageScanCount] = useState<number | null>(null)
  const [packageScanSkipped, setPackageScanSkipped] = useState(0)
  const [packageScanning, setPackageScanning] = useState(false)
  const [packageSubmitting, setPackageSubmitting] = useState(false)
  const [packageMessage, setPackageMessage] = useState("")
  const [packageError, setPackageError] = useState("")
  const [packages, setPackages] = useState<TaskPackage[]>([])
  const [packageRetryingId, setPackageRetryingId] = useState<string | null>(null)
  const [taskPausingId, setTaskPausingId] = useState<string | null>(null)
  const [taskContinuingId, setTaskContinuingId] = useState<string | null>(null)
  const [packagePausingId, setPackagePausingId] = useState<string | null>(null)
  const [packageContinuingId, setPackageContinuingId] = useState<string | null>(null)
  const parsedUrls = parseVideoUrls(urlsText)
  const mergedEntries = useMemo(() => {
    if (jobKindFilter === "task") {
      return tasks.map(taskToJobEntry)
    }
    const taskEntries = jobKindFilter === "all" ? tasks.map(taskToJobEntry) : []
    const packageEntries = packages.map(packageToJobEntry)
    const filters = {
      query: taskQuery,
      status: taskStatus,
      execution_mode: taskExecutionMode,
    }
    const filteredTasks = jobKindFilter === "all"
      ? filterJobEntriesByStatusAndMode(taskEntries, filters)
      : []
    const filteredPackages = filterJobEntries(packageEntries, filters)
    const filtered = [...filteredTasks, ...filteredPackages]
    return sortJobEntries(filtered, taskSort)
  }, [jobKindFilter, packages, taskExecutionMode, taskQuery, taskSort, taskStatus, tasks])
  const listTotal = jobKindFilter === "task" ? taskTotal : mergedEntries.length
  const totalPages = Math.max(1, Math.ceil(listTotal / taskPageSize))
  const displayPage = Math.min(taskPage, totalPages)
  const pageStart = listTotal === 0 ? 0 : (displayPage - 1) * taskPageSize + 1
  const pageEnd = Math.min(listTotal, displayPage * taskPageSize)
  const visibleEntries = useMemo(() => {
    if (jobKindFilter === "task") return mergedEntries
    const start = (displayPage - 1) * taskPageSize
    return mergedEntries.slice(start, start + taskPageSize)
  }, [displayPage, jobKindFilter, mergedEntries, taskPageSize])
  const pageSelectableTasks = useMemo(
    () =>
      visibleEntries
        .filter((entry) => entry.kind === "task")
        .map((entry) => entry.task)
        .filter((task) => isDeletable(task.status)),
    [visibleEntries],
  )
  const hasTaskFilters =
    Boolean(taskQuery.trim())
    || taskStatus !== "all"
    || taskExecutionMode !== "all"
    || jobKindFilter !== "all"
  const pageSelectablePackages = useMemo(
    () =>
      visibleEntries
        .filter((entry) => entry.kind === "package")
        .map((entry) => entry.package)
        .filter((pkg) => isPackageDeletable(pkg.status)),
    [visibleEntries],
  )
  const selectedFailedCount = useMemo(() => {
    const failedTasks = tasks.filter(
      (task) => selectedTaskIds.has(task.id) && task.status === "failed",
    ).length
    const failedPackages = packages.filter(
      (pkg) => selectedPackageIds.has(pkg.id) && packageHasRetryableFailures(pkg),
    ).length
    return failedTasks + failedPackages
  }, [packages, selectedPackageIds, selectedTaskIds, tasks])
  const selectedCount = selectedTaskIds.size + selectedPackageIds.size
  const pageSelectedCount = useMemo(() => {
    const taskCount = pageSelectableTasks.filter((task) => selectedTaskIds.has(task.id)).length
    const packageCount = pageSelectablePackages.filter((pkg) => selectedPackageIds.has(pkg.id)).length
    return taskCount + packageCount
  }, [pageSelectablePackages, pageSelectableTasks, selectedPackageIds, selectedTaskIds])
  const pageSelectableCount =
    jobKindFilter === "package"
      ? pageSelectablePackages.length
      : pageSelectableTasks.length + (jobKindFilter === "all" ? pageSelectablePackages.length : 0)
  const allPageSelected =
    pageSelectableCount > 0 && pageSelectedCount === pageSelectableCount
  const somePageSelected = pageSelectedCount > 0 && !allPageSelected
  const showBatchActions = pageSelectableCount > 0

  const localDirectionOptions: { value: LocalDirection; label: string }[] = [
    { value: "en-zh", label: t.home.localEnZh },
    { value: "zh-en", label: t.home.localZhEn },
  ]

  const executionModeOptions: { value: ExecutionMode; label: string }[] = [
    { value: "auto", label: t.home.executionAuto },
    { value: "manual", label: t.home.executionManual },
  ]

  const audioModeOptions: { value: AudioMode; label: string }[] = [
    { value: "keep_bgm", label: t.home.audioKeepBgm },
    { value: "replace", label: t.home.audioReplace },
  ]

  const ttsProviderOptions: { value: TtsProvider; label: string }[] = [
    { value: "voxcpm", label: t.home.ttsVoxcpm },
    { value: "azure", label: t.home.ttsAzure },
  ]

  const bilibiliPartitionOptions = useMemo(
    () =>
      BILIBILI_PARTITIONS.map((part) => ({
        value: String(part.tid),
        label: `${part.group} / ${part.name}`,
      })),
    [],
  )

  const bilibiliAutoPublishOptions: { value: "true" | "false"; label: string }[] = [
    { value: "true", label: t.home.bilibiliAutoPublishYes },
    { value: "false", label: t.home.bilibiliAutoPublishNo },
  ]
  const bilibiliGenerateMetaOptions: { value: "true" | "false"; label: string }[] = [
    { value: "true", label: t.home.bilibiliGenerateMetaYes },
    { value: "false", label: t.home.bilibiliGenerateMetaNo },
  ]
  const douyinAutoPublishOptions: { value: "true" | "false"; label: string }[] = [
    { value: "true", label: t.home.douyinAutoPublishYes },
    { value: "false", label: t.home.douyinAutoPublishNo },
  ]
  const douyinGenerateMetaOptions: { value: "true" | "false"; label: string }[] = [
    { value: "true", label: t.home.douyinGenerateMetaYes },
    { value: "false", label: t.home.douyinGenerateMetaNo },
  ]
  const effectiveGenerateMeta = bilibiliAutoPublish ? true : bilibiliGenerateMeta
  const effectiveDouyinGenerateMeta = douyinAutoPublish ? true : douyinGenerateMeta

  const statusOptions: { value: TaskListStatus; label: string }[] = [
    { value: "all", label: t.home.allStatuses },
    { value: "queued", label: statusLabel("queued") },
    { value: "running", label: statusLabel("running") },
    { value: "paused", label: statusLabel("paused") },
    { value: "succeeded", label: statusLabel("succeeded") },
    { value: "failed", label: statusLabel("failed") },
  ]

  const modeOptions: { value: TaskListExecutionMode; label: string }[] = [
    { value: "all", label: t.home.allModes },
    { value: "auto", label: t.home.modeAuto },
    { value: "manual", label: t.home.modeManual },
  ]

  const sortOptions: { value: TaskListSort; label: string }[] = [
    { value: "created_desc", label: t.home.sortCreatedDesc },
    { value: "created_asc", label: t.home.sortCreatedAsc },
    { value: "started_desc", label: t.home.sortStartedDesc },
    { value: "started_asc", label: t.home.sortStartedAsc },
    { value: "completed_desc", label: t.home.sortCompletedDesc },
    { value: "completed_asc", label: t.home.sortCompletedAsc },
    { value: "status_asc", label: t.home.sortStatusAsc },
    { value: "status_desc", label: t.home.sortStatusDesc },
    { value: "title_asc", label: t.home.sortTitleAsc },
    { value: "title_desc", label: t.home.sortTitleDesc },
  ]

  const jobKindOptions: { value: JobKindFilter; label: string }[] = [
    { value: "all", label: t.home.jobKindAll },
    { value: "task", label: t.home.jobKindTask },
    { value: "package", label: t.home.jobKindPackage },
  ]

  const applyTaskList = useCallback((result: TaskListResponse) => {
    const lastPage = Math.max(1, Math.ceil(result.total / result.page_size))
    setTaskTotal(result.total)
    setActiveTaskCount(
      Number.isInteger(result.active_count) && result.active_count >= 0
        ? result.active_count
        : null,
    )
    if (result.total > 0 && result.tasks.length === 0 && result.page > lastPage) {
      setTasks([])
      setTaskPage(lastPage)
      return
    }
    setTasks(result.tasks)
  }, [])

  const pollTasks = useCallback(async (context?: SerialPollingContext) => {
    const signal = context?.signal
    const isCurrent = context?.isCurrent ?? (() => true)
    try {
      if (jobKindFilter === "task") {
        const result = await listTasks({
          page: taskPage,
          page_size: taskPageSize,
          q: taskQuery,
          status: taskStatus,
          execution_mode: taskExecutionMode,
          sort: taskSort,
        }, signal)
        if (isCurrent()) {
          setTaskListError("")
          applyTaskList(result)
          setPackages([])
        }
        return
      }

      const [taskResult, packageResult] = await Promise.all([
        jobKindFilter === "all"
          ? listTasks({
              page: 1,
              page_size: 100,
              q: taskQuery,
              status: taskStatus,
              execution_mode: taskExecutionMode,
              sort: "created_desc",
            }, signal)
          : Promise.resolve({
              tasks: [],
              total: 0,
              active_count: 0,
              page: 1,
              page_size: 0,
            }),
        listTaskPackages(200, signal),
      ])
      if (isCurrent()) {
        setTaskListError("")
        setTasks(jobKindFilter === "all" ? taskResult.tasks : [])
        setPackages(packageResult.packages)
        setTaskTotal(0)
        setActiveTaskCount(
          Number.isInteger(taskResult.active_count) && taskResult.active_count >= 0
            ? taskResult.active_count
            : null,
        )
      }
    } catch (err) {
      if (isCurrent() && !isAbortError(err)) {
        setTaskListError(err instanceof Error ? err.message : t.home.loadError)
      }
    }
  }, [
    applyTaskList,
    jobKindFilter,
    taskExecutionMode,
    taskPage,
    taskPageSize,
    taskQuery,
    taskSort,
    taskStatus,
    t.home.loadError,
  ])

  useSerialPolling(pollTasks)

  function resetTaskPage() {
    setTaskPage(1)
  }

  function toggleTaskSelected(taskId: string, checked: boolean) {
    setSelectedTaskIds((current) => {
      const next = new Set(current)
      if (checked) next.add(taskId)
      else next.delete(taskId)
      return next
    })
  }

  function togglePackageSelected(packageId: string, checked: boolean) {
    setSelectedPackageIds((current) => {
      const next = new Set(current)
      if (checked) next.add(packageId)
      else next.delete(packageId)
      return next
    })
  }

  function toggleSelectAllPage(checked: boolean) {
    setSelectedTaskIds((current) => {
      const next = new Set(current)
      for (const task of pageSelectableTasks) {
        if (checked) next.add(task.id)
        else next.delete(task.id)
      }
      return next
    })
    setSelectedPackageIds((current) => {
      const next = new Set(current)
      for (const pkg of pageSelectablePackages) {
        if (checked) next.add(pkg.id)
        else next.delete(pkg.id)
      }
      return next
    })
  }

  function selectFinishedOnPage() {
    setSelectedTaskIds((current) => {
      const next = new Set(current)
      for (const task of pageSelectableTasks) {
        if (isFinished(task.status)) next.add(task.id)
      }
      return next
    })
    setSelectedPackageIds((current) => {
      const next = new Set(current)
      for (const pkg of pageSelectablePackages) {
        if (isFinished(pkg.status)) next.add(pkg.id)
      }
      return next
    })
  }

  function clearTaskSelection() {
    setSelectedTaskIds(new Set())
    setSelectedPackageIds(new Set())
  }

  async function handleBatchDelete() {
    const taskIds = Array.from(selectedTaskIds)
    const packageIds = Array.from(selectedPackageIds)
    if (!taskIds.length && !packageIds.length) {
      setBatchDeleteError(t.home.batchDeleteNone)
      return
    }
    setBatchDeleting(true)
    setBatchDeleteError("")
    try {
      const [taskResult, packageResult] = await Promise.all([
        taskIds.length ? deleteTasksBatch(taskIds) : Promise.resolve({
          deleted: [],
          skipped: [],
          missing: [],
          failed: [],
        }),
        packageIds.length ? deletePackagesBatch(packageIds) : Promise.resolve({
          deleted: [],
          skipped: [],
          missing: [],
          failed: [],
        }),
      ])
      const deletedSet = new Set([...taskResult.deleted, ...packageResult.deleted])
      setSelectedTaskIds((current) => {
        const next = new Set(current)
        for (const id of deletedSet) next.delete(id)
        for (const id of [...taskResult.missing, ...packageResult.missing]) next.delete(id)
        return next
      })
      setSelectedPackageIds((current) => {
        const next = new Set(current)
        for (const id of deletedSet) next.delete(id)
        for (const id of [...taskResult.missing, ...packageResult.missing]) next.delete(id)
        return next
      })
      setBatchDeleteOpen(false)
      setMessage(
        batchDeleteSummaryText(
          t.home.batchDeleteSummary,
          taskResult.deleted.length + packageResult.deleted.length,
          taskResult.skipped.length + packageResult.skipped.length,
          taskResult.missing.length + packageResult.missing.length,
          taskResult.failed.length + packageResult.failed.length,
        ),
      )
      await pollTasks()
    } catch (err) {
      setBatchDeleteError(err instanceof Error ? err.message : t.home.batchDeleteError)
    } finally {
      setBatchDeleting(false)
    }
  }

  async function handleBatchCleanup() {
    const taskIds = Array.from(selectedTaskIds)
    const packageIds = Array.from(selectedPackageIds)
    if (!taskIds.length && !packageIds.length) {
      setBatchCleanupError(t.home.batchCleanupNone)
      return
    }
    setBatchCleaning(true)
    setBatchCleanupError("")
    try {
      const [taskResult, packageResult] = await Promise.all([
        taskIds.length ? cleanupTasksBatch(taskIds) : Promise.resolve({
          cleaned: [],
          skipped: [],
          missing: [],
          failed: [],
        }),
        packageIds.length ? cleanupPackagesBatch(packageIds) : Promise.resolve({
          cleaned: [],
          skipped: [],
          missing: [],
          failed: [],
        }),
      ])
      setBatchCleanupOpen(false)
      setMessage(
        batchCleanupSummaryText(
          t.home.batchCleanupSummary,
          taskResult.cleaned.length + packageResult.cleaned.length,
          taskResult.skipped.length + packageResult.skipped.length,
          taskResult.missing.length + packageResult.missing.length,
          taskResult.failed.length + packageResult.failed.length,
        ),
      )
      await pollTasks()
    } catch (err) {
      setBatchCleanupError(err instanceof Error ? err.message : t.home.batchCleanupError)
    } finally {
      setBatchCleaning(false)
    }
  }

  async function handleBatchRetry() {
    const taskIds = Array.from(selectedTaskIds)
    const packageIds = Array.from(selectedPackageIds)
    if (!taskIds.length && !packageIds.length) {
      setBatchRetryError(t.home.batchRetryNone)
      return
    }
    setBatchRetrying(true)
    setBatchRetryError("")
    try {
      const [taskResult, packageResult] = await Promise.all([
        taskIds.length ? resumeTasksBatch(taskIds) : Promise.resolve({
          resumed: [],
          skipped: [],
          missing: [],
          failed: [],
        }),
        packageIds.length ? retryFailedPackagesBatch(packageIds) : Promise.resolve({
          retried: [],
          skipped: [],
          missing: [],
          failed: [],
        }),
      ])
      const resumedSet = new Set([...taskResult.resumed, ...packageResult.retried])
      setSelectedTaskIds((current) => {
        const next = new Set(current)
        for (const id of resumedSet) next.delete(id)
        for (const id of [...taskResult.missing, ...packageResult.missing]) next.delete(id)
        return next
      })
      setSelectedPackageIds((current) => {
        const next = new Set(current)
        for (const id of resumedSet) next.delete(id)
        for (const id of [...taskResult.missing, ...packageResult.missing]) next.delete(id)
        return next
      })
      setBatchRetryOpen(false)
      setMessage(
        batchRetrySummaryText(
          t.home.batchRetrySummary,
          taskResult.resumed.length + packageResult.retried.length,
          taskResult.skipped.length + packageResult.skipped.length,
          taskResult.missing.length + packageResult.missing.length,
          taskResult.failed.length + packageResult.failed.length,
        ),
      )
      await pollTasks()
    } catch (err) {
      setBatchRetryError(err instanceof Error ? err.message : t.home.batchRetryError)
    } finally {
      setBatchRetrying(false)
    }
  }

  function clearLocalSelection() {
    setLocalFiles([])
    setLocalSubtitleFile(null)
    if (fileInputRef.current) {
      fileInputRef.current.value = ""
    }
    if (subtitleInputRef.current) {
      subtitleInputRef.current.value = ""
    }
  }

  function selectLocalFile(event: ChangeEvent<HTMLInputElement>) {
    setError("")
    setMessage("")
    const files = Array.from(event.target.files || [])
    setLocalFiles(files)
    setLocalSubtitleFile(null)
    if (subtitleInputRef.current) {
      subtitleInputRef.current.value = ""
    }
  }

  function selectLocalSubtitleFile(event: ChangeEvent<HTMLInputElement>) {
    setError("")
    setMessage("")
    setLocalSubtitleFile(event.target.files?.[0] || null)
  }

  async function scanPackage() {
    setPackageError("")
    setPackageMessage("")
    if (!packageSourceDir.trim()) return
    setPackageScanning(true)
    try {
      const result = await scanTaskPackage({
        source_dir: packageSourceDir.trim(),
        recursive: packageRecursive,
        skip_if_export_exists: true,
      })
      const skipped = result.files.filter((file) => file.will_skip).length
      setPackageScanCount(result.count)
      setPackageScanSkipped(skipped)
      setPackageMessage(
        t.home.packageScanSummary
          .replace("{count}", String(result.count))
          .replace("{skipped}", String(skipped)),
      )
    } catch (err) {
      setPackageError(err instanceof Error ? err.message : t.home.createError)
    } finally {
      setPackageScanning(false)
    }
  }

  async function submitPackage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPackageError("")
    setPackageMessage("")
    if (!packageSourceDir.trim()) return
    setPackageSubmitting(true)
    try {
      const created = await createTaskPackage({
        source_dir: packageSourceDir.trim(),
        recursive: packageRecursive,
        direction: localDirection,
        execution_mode: executionMode,
        audio_mode: audioMode,
        tts_provider: ttsProvider,
        continue_on_error: true,
        skip_if_export_exists: true,
      })
      setPackageMessage(
        t.home.packageCreateSummary.replace("{count}", String(created.items?.length || 0)),
      )
      setPackageSourceDir("")
      setPackageScanCount(null)
      setPackageScanSkipped(0)
      await pollTasks()
      router.push(`/packages/${created.id}`)
    } catch (err) {
      setPackageError(err instanceof Error ? err.message : t.home.createError)
    } finally {
      setPackageSubmitting(false)
    }
  }

  async function handlePauseTask(taskId: string) {
    setTaskListError("")
    setTaskPausingId(taskId)
    try {
      const updated = await pauseTask(taskId)
      setTasks((current) =>
        current.map((entry) => (entry.id === taskId ? { ...entry, ...updated } : entry)),
      )
      await pollTasks()
    } catch (err) {
      setTaskListError(err instanceof Error ? err.message : t.task.pauseError)
    } finally {
      setTaskPausingId(null)
    }
  }

  async function handleContinueTask(taskId: string) {
    setTaskListError("")
    setTaskContinuingId(taskId)
    try {
      const updated = await continueTask(taskId)
      setTasks((current) =>
        current.map((entry) => (entry.id === taskId ? { ...entry, ...updated } : entry)),
      )
      await pollTasks()
    } catch (err) {
      setTaskListError(err instanceof Error ? err.message : t.task.continueError)
    } finally {
      setTaskContinuingId(null)
    }
  }

  async function handlePausePackage(packageId: string) {
    setPackageError("")
    setPackageMessage("")
    setPackagePausingId(packageId)
    try {
      const updated = await pauseTaskPackage(packageId)
      setPackages((current) =>
        current.map((entry) => (entry.id === packageId ? { ...entry, ...updated } : entry)),
      )
      await pollTasks()
    } catch (err) {
      setPackageError(err instanceof Error ? err.message : t.task.pausePackageError)
    } finally {
      setPackagePausingId(null)
    }
  }

  async function handleContinuePackage(packageId: string) {
    setPackageError("")
    setPackageMessage("")
    setPackageContinuingId(packageId)
    try {
      const updated = await continueTaskPackage(packageId)
      setPackages((current) =>
        current.map((entry) => (entry.id === packageId ? { ...entry, ...updated } : entry)),
      )
      await pollTasks()
    } catch (err) {
      setPackageError(err instanceof Error ? err.message : t.task.continueError)
    } finally {
      setPackageContinuingId(null)
    }
  }

  async function handleRetryPackageFailed(packageId: string) {
    setPackageError("")
    setPackageMessage("")
    setPackageRetryingId(packageId)
    try {
      const updated = await retryFailedTaskPackage(packageId)
      setPackages((current) =>
        current.map((entry) => (entry.id === packageId ? { ...entry, ...updated } : entry)),
      )
      setPackageMessage(
        t.home.packageRetryQueued.replace(
          "{count}",
          String(updated.retried_count ?? 0),
        ),
      )
      await pollTasks()
    } catch (err) {
      setPackageError(err instanceof Error ? err.message : t.home.packageRetryError)
    } finally {
      setPackageRetryingId(null)
    }
  }

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError("")
    setMessage("")
    const urls = parseVideoUrls(urlsText)
    if (!urls.length && !localFiles.length) return
    setSubmitting(true)
    try {
      if (localFiles.length) {
        const result = await uploadLocalTasks(
          localFiles,
          localDirection,
          localSubtitleFile,
          executionMode,
          audioMode,
          ttsProvider,
          bilibiliTid,
          bilibiliAutoPublish,
          effectiveGenerateMeta,
          douyinAutoPublish,
          effectiveDouyinGenerateMeta,
        )
        const createdCount = result.created.length
        const failedCount = result.errors.length
        clearLocalSelection()

        if (!createdCount) {
          const details = result.errors.map((item) => item.detail).filter(Boolean)
          setError(details[0] || t.home.batchAllFailed)
          return
        }

        const summary = localBatchSummaryText(
          t.home.localBatchSummary,
          createdCount,
          failedCount,
        )
        if (failedCount > 0) {
          const firstError = result.errors[0]
          setError(
            `${summary}${firstError ? ` ${firstError.filename}: ${firstError.detail}` : ""}`,
          )
        } else {
          setMessage(summary)
        }

        if (createdCount === 1 && failedCount === 0) {
          router.push(`/tasks/${result.created[0].id}`)
          return
        }

        await pollTasks()
        return
      }

      const result = await createTasksBatch(
        urls,
        executionMode,
        audioMode,
        ttsProvider,
        bilibiliTid,
        bilibiliAutoPublish,
        effectiveGenerateMeta,
        douyinAutoPublish,
        effectiveDouyinGenerateMeta,
      )
      const createdCount = result.created.length
      const existingCount = result.existing.length
      const failedCount = result.errors.length
      const accepted = [...result.created, ...result.existing]
      setUrlsText("")

      if (!accepted.length) {
        const details = result.errors.map((item) => item.detail).filter(Boolean)
        setError(details[0] || t.home.batchAllFailed)
        return
      }

      const summary = batchSummaryText(
        t.home.batchSummary,
        createdCount,
        existingCount,
        failedCount,
      )
      if (failedCount > 0) {
        const firstError = result.errors[0]
        setError(
          `${summary}${firstError ? ` ${firstError.url}: ${firstError.detail}` : ""}`,
        )
      } else {
        setMessage(summary)
      }

      if (accepted.length === 1 && failedCount === 0) {
        router.push(`/tasks/${accepted[0].task.id}`)
        return
      }

      await pollTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : t.home.createError)
    } finally {
      setSubmitting(false)
    }
  }

  const hasUrl = parsedUrls.length > 0
  const hasLocalFile = localFiles.length > 0
  const isSingleLocalFile = localFiles.length === 1
  const canSubmit = Boolean((hasUrl || hasLocalFile) && !submitting)

  return (
    <AppShell>
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>{t.home.createTitle}</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={submitTask} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="video-urls">{t.home.urlsLabel}</Label>
                <Textarea
                  id="video-urls"
                  value={urlsText}
                  onChange={(event) => {
                    setError("")
                    setMessage("")
                    setUrlsText(event.target.value)
                  }}
                  placeholder={t.home.urlsPlaceholder}
                  disabled={hasLocalFile}
                  className="min-h-28 font-mono text-xs leading-relaxed"
                />
                <p className="text-xs text-muted-foreground">{t.home.urlsHelp}</p>
              </div>
              <div className="grid gap-3 sm:grid-cols-[1fr_180px]">
                <div className="space-y-2">
                  <Label htmlFor="local-video">{t.home.localVideoLabel}</Label>
                  <Input
                    ref={fileInputRef}
                    id="local-video"
                    type="file"
                    accept={LOCAL_VIDEO_ACCEPT}
                    multiple
                    onChange={selectLocalFile}
                    disabled={hasUrl}
                  />
                  <p className="text-xs text-muted-foreground">{t.home.localVideoHelp}</p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="local-direction">{t.home.localDirectionLabel}</Label>
                  <Select
                    value={localDirection}
                    onValueChange={(value) => setLocalDirection(value as LocalDirection)}
                    disabled={hasUrl}
                  >
                    <SelectTrigger id="local-direction" className="h-10">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(localDirectionOptions, localDirection)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {localDirectionOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="local-subtitle">{t.home.localSubtitleLabel}</Label>
                <Input
                  ref={subtitleInputRef}
                  id="local-subtitle"
                  type="file"
                  accept=".srt"
                  onChange={selectLocalSubtitleFile}
                  disabled={hasUrl || !isSingleLocalFile}
                />
                <p className="text-xs text-muted-foreground">
                  {isSingleLocalFile || !hasLocalFile
                    ? t.home.localSubtitleHelp
                    : t.home.localSubtitleMultiHelp}
                </p>
                {hasLocalFile ? (
                  <div
                    data-testid="local-upload-selection"
                    className="rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
                    aria-live="polite"
                  >
                    {isSingleLocalFile ? (
                      <>
                        <p>
                          {t.home.currentLocalVideo}:{" "}
                          <span className="font-medium text-foreground">{localFiles[0].name}</span>
                        </p>
                        <p>
                          {t.home.subtitleForCurrentVideo}:{" "}
                          <span className="font-medium text-foreground">
                            {localSubtitleFile?.name || t.home.noSubtitleSelected}
                          </span>
                        </p>
                      </>
                    ) : (
                      <>
                        <p>
                          {t.home.selectedLocalVideos}:{" "}
                          <span className="font-medium text-foreground">{localFiles.length}</span>
                        </p>
                        <ul className="mt-1 list-disc space-y-0.5 pl-4">
                          {localFiles.map((file) => (
                            <li key={`${file.name}-${file.size}-${file.lastModified}`}>
                              <span className="font-medium text-foreground">{file.name}</span>
                            </li>
                          ))}
                        </ul>
                      </>
                    )}
                  </div>
                ) : null}
              </div>
              <div className="space-y-2">
                <Label htmlFor="execution-mode">{t.home.executionModeLabel}</Label>
                <Select
                  value={executionMode}
                  onValueChange={(value) => setExecutionMode(value as ExecutionMode)}
                >
                  <SelectTrigger id="execution-mode" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(executionModeOptions, executionMode)}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {executionModeOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="audio-mode">{t.home.audioModeLabel}</Label>
                <Select
                  value={audioMode}
                  onValueChange={(value) => setAudioMode(value as AudioMode)}
                >
                  <SelectTrigger id="audio-mode" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(audioModeOptions, audioMode)}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {audioModeOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t.home.audioModeHelp}</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="tts-provider">{t.home.ttsProviderLabel}</Label>
                <Select
                  value={ttsProvider}
                  onValueChange={(value) => setTtsProvider(value as TtsProvider)}
                >
                  <SelectTrigger id="tts-provider" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(ttsProviderOptions, ttsProvider)}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {ttsProviderOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t.home.ttsProviderHelp}</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="bilibili-auto-publish">{t.home.bilibiliAutoPublishLabel}</Label>
                <Select
                  value={bilibiliAutoPublish ? "true" : "false"}
                  onValueChange={(value) => {
                    const next = value === "true"
                    setBilibiliAutoPublish(next)
                    if (next) setBilibiliGenerateMeta(true)
                  }}
                >
                  <SelectTrigger id="bilibili-auto-publish" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(
                        bilibiliAutoPublishOptions,
                        bilibiliAutoPublish ? "true" : "false",
                      )}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {bilibiliAutoPublishOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t.home.bilibiliAutoPublishHelp}</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="bilibili-tid">{t.home.bilibiliTidLabel}</Label>
                <Select
                  value={String(bilibiliTid)}
                  onValueChange={(value) => {
                    if (!bilibiliAutoPublish) return
                    setBilibiliTid(Number(value || DEFAULT_BILIBILI_TID))
                  }}
                  disabled={!bilibiliAutoPublish}
                >
                  <SelectTrigger id="bilibili-tid" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(bilibiliPartitionOptions, String(bilibiliTid)) ||
                        t.home.bilibiliTidDefault}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {bilibiliPartitionOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {bilibiliAutoPublish
                    ? t.home.bilibiliTidHelp
                    : t.home.bilibiliTidLocked}
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="bilibili-generate-meta">{t.home.bilibiliGenerateMetaLabel}</Label>
                <Select
                  value={effectiveGenerateMeta ? "true" : "false"}
                  onValueChange={(value) => {
                    if (bilibiliAutoPublish) return
                    setBilibiliGenerateMeta(value === "true")
                  }}
                  disabled={bilibiliAutoPublish}
                >
                  <SelectTrigger id="bilibili-generate-meta" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(
                        bilibiliGenerateMetaOptions,
                        effectiveGenerateMeta ? "true" : "false",
                      )}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {bilibiliGenerateMetaOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {bilibiliAutoPublish
                    ? t.home.bilibiliGenerateMetaLocked
                    : t.home.bilibiliGenerateMetaHelp}
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="douyin-auto-publish">{t.home.douyinAutoPublishLabel}</Label>
                <Select
                  value={douyinAutoPublish ? "true" : "false"}
                  onValueChange={(value) => {
                    const enabled = value === "true"
                    setDouyinAutoPublish(enabled)
                    if (enabled) setDouyinGenerateMeta(true)
                  }}
                >
                  <SelectTrigger id="douyin-auto-publish" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(
                        douyinAutoPublishOptions,
                        douyinAutoPublish ? "true" : "false",
                      )}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {douyinAutoPublishOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t.home.douyinAutoPublishHelp}</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="douyin-generate-meta">{t.home.douyinGenerateMetaLabel}</Label>
                <Select
                  value={effectiveDouyinGenerateMeta ? "true" : "false"}
                  onValueChange={(value) => {
                    if (douyinAutoPublish) return
                    setDouyinGenerateMeta(value === "true")
                  }}
                  disabled={douyinAutoPublish}
                >
                  <SelectTrigger id="douyin-generate-meta" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(
                        douyinGenerateMetaOptions,
                        effectiveDouyinGenerateMeta ? "true" : "false",
                      )}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {douyinGenerateMetaOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {douyinAutoPublish
                    ? t.home.douyinGenerateMetaLocked
                    : t.home.douyinGenerateMetaHelp}
                </p>
              </div>
              <div className="flex items-center justify-between gap-3">
                {activeTaskCount !== null && activeTaskCount > 0 ? (
                  <p className="text-xs text-muted-foreground">
                    {activeTasksText(activeTaskCount)}
                  </p>
                ) : (
                  <span />
                )}
                <Button type="submit" disabled={!canSubmit}>
                  {hasLocalFile ? <Upload className="size-4" /> : <Play className="size-4" />}
                  {submitting
                    ? t.home.submitting
                    : localFiles.length > 1 || parsedUrls.length > 1
                      ? t.home.createTasks
                      : t.home.createTask}
                </Button>
              </div>
            </form>

            {message ? (
              <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-300">
                {message}
              </div>
            ) : null}
            {error ? (
              <div className="mt-4 rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t.home.packageSectionTitle}</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={submitPackage} className="space-y-4">
              <p className="text-xs text-muted-foreground">{t.home.packageSourceDirHelp}</p>
              <p className="text-xs text-muted-foreground">{t.home.packageExportDirHelp}</p>
              <div className="space-y-2">
                <Label htmlFor="package-source-dir">{t.home.packageSourceDirLabel}</Label>
                <Input
                  id="package-source-dir"
                  value={packageSourceDir}
                  onChange={(event) => {
                    setPackageError("")
                    setPackageMessage("")
                    setPackageSourceDir(event.target.value)
                  }}
                  placeholder="D:/Videos/Course01"
                  className="font-mono text-xs"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="package-direction">{t.home.localDirectionLabel}</Label>
                <Select
                  value={localDirection}
                  onValueChange={(value) => setLocalDirection(value as LocalDirection)}
                >
                  <SelectTrigger id="package-direction" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(localDirectionOptions, localDirection)}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {localDirectionOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <label className="flex items-center gap-2 text-sm text-muted-foreground">
                <input
                  type="checkbox"
                  checked={packageRecursive}
                  onChange={(event) => setPackageRecursive(event.target.checked)}
                />
                {t.home.packageRecursive}
              </label>
              <p className="text-xs text-muted-foreground">{t.home.packageNoBilibiliNote}</p>
              <div className="flex flex-wrap items-center justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  disabled={!packageSourceDir.trim() || packageScanning}
                  onClick={scanPackage}
                >
                  {packageScanning ? t.home.submitting : t.home.packageScan}
                </Button>
                <Button type="submit" disabled={!packageSourceDir.trim() || packageSubmitting}>
                  {packageSubmitting ? t.home.submitting : t.home.packageCreate}
                </Button>
              </div>
            </form>
            {packageScanCount !== null ? (
              <p className="mt-3 text-xs text-muted-foreground">
                {t.home.packageScanSummary
                  .replace("{count}", String(packageScanCount))
                  .replace("{skipped}", String(packageScanSkipped))}
              </p>
            ) : null}
            {packageMessage ? (
              <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-300">
                {packageMessage}
              </div>
            ) : null}
            {packageError ? (
              <div className="mt-4 rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                {packageError}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t.home.jobHistory} ({listTotal})</CardTitle>
          </CardHeader>
          <CardContent className="px-0">
            <div className="border-b border-border/60 px-4 pb-4">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_120px_140px_140px_180px_120px]">
                <div className="relative sm:col-span-2 lg:col-span-1">
                  <Label htmlFor="task-search" className="sr-only">
                    {t.home.taskSearchPlaceholder}
                  </Label>
                  <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
                  <Input
                    id="task-search"
                    className="h-9 pl-8"
                    value={taskQuery}
                    onChange={(event) => {
                      setTaskQuery(truncateSearchQuery(event.target.value))
                      resetTaskPage()
                    }}
                    placeholder={t.home.taskSearchPlaceholder}
                  />
                </div>

                <div>
                  <Label htmlFor="job-kind-filter" className="sr-only">
                    {t.home.jobKindFilter}
                  </Label>
                  <Select
                    value={jobKindFilter}
                    onValueChange={(value) => {
                      setJobKindFilter(value as JobKindFilter)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="job-kind-filter" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(jobKindOptions, jobKindFilter)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {jobKindOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-status-filter" className="sr-only">
                    {t.home.taskStatusFilter}
                  </Label>
                  <Select
                    value={taskStatus}
                    onValueChange={(value) => {
                      setTaskStatus(value as TaskListStatus)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-status-filter" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(statusOptions, taskStatus)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {statusOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-mode-filter" className="sr-only">
                    {t.home.taskModeFilter}
                  </Label>
                  <Select
                    value={taskExecutionMode}
                    onValueChange={(value) => {
                      setTaskExecutionMode(value as TaskListExecutionMode)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-mode-filter" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(modeOptions, taskExecutionMode)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {modeOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-sort" className="sr-only">
                    {t.home.taskSort}
                  </Label>
                  <Select
                    value={taskSort}
                    onValueChange={(value) => {
                      setTaskSort(value as TaskListSort)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-sort" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(sortOptions, taskSort)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {sortOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-page-size" className="sr-only">
                    {t.home.taskPageSize}
                  </Label>
                  <Select
                    value={String(taskPageSize)}
                    onValueChange={(value) => {
                      setTaskPageSize(Number(value))
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-page-size" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {taskPageSize}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {PAGE_SIZE_OPTIONS.map((option) => (
                        <SelectItem key={option} value={String(option)}>
                          {option}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>

            {taskListError ? (
              <div className="mx-4 mt-4 rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                {taskListError}
              </div>
            ) : null}

            {showBatchActions ? (
              <div className="flex flex-col gap-2 border-b border-border/60 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-center gap-3">
                  <label className="flex items-center gap-2 text-sm text-muted-foreground">
                    <input
                      type="checkbox"
                      className="size-4 accent-zinc-900"
                      checked={allPageSelected}
                      ref={(element) => {
                        if (element) element.indeterminate = somePageSelected
                      }}
                      onChange={(event) => toggleSelectAllPage(event.target.checked)}
                      disabled={pageSelectableCount === 0}
                      aria-label={t.home.selectAllPage}
                    />
                    <span>
                      {selectedCount > 0
                        ? selectedCountText(t.home.selectedCount, selectedCount)
                        : t.home.selectAllPage}
                    </span>
                  </label>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={selectFinishedOnPage}
                    disabled={
                      !pageSelectableTasks.some((task) => isFinished(task.status))
                      && !pageSelectablePackages.some((pkg) => isFinished(pkg.status))
                    }
                  >
                    {t.home.selectFinishedPage}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={clearTaskSelection}
                    disabled={selectedCount === 0}
                  >
                    {t.home.clearSelection}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setBatchRetryError("")
                      setBatchRetryOpen(true)
                    }}
                    disabled={selectedCount === 0 || selectedFailedCount === 0}
                  >
                    <RotateCw className="size-3.5" />
                    {t.home.batchRetry}
                    {selectedFailedCount > 0 ? ` (${selectedFailedCount})` : ""}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setBatchCleanupError("")
                      setBatchCleanupOpen(true)
                    }}
                    disabled={selectedCount === 0}
                  >
                    <FolderMinus className="size-3.5" />
                    {t.home.batchCleanup}
                    {selectedCount > 0 ? ` (${selectedCount})` : ""}
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    onClick={() => {
                      setBatchDeleteError("")
                      setBatchDeleteOpen(true)
                    }}
                    disabled={selectedCount === 0}
                  >
                    <Trash2 className="size-3.5" />
                    {t.home.batchDelete}
                    {selectedCount > 0 ? ` (${selectedCount})` : ""}
                  </Button>
                </div>
              </div>
            ) : null}

            {visibleEntries.length === 0 ? (
              <div className="px-6 py-12 text-center text-sm text-muted-foreground">
                {hasTaskFilters ? t.home.noMatchingTasks : t.home.empty}
              </div>
            ) : (
              <div className="max-h-[min(56dvh,calc(100dvh-18rem))] overflow-y-auto overscroll-contain">
                <ul className="flex flex-col">
                  {visibleEntries.map((entry) => {
                    if (entry.kind === "task") {
                      const item = entry.task
                      const deletable = isDeletable(item.status)
                      const checked = selectedTaskIds.has(item.id)
                      const canPauseTask = item.status === "queued"
                      const canContinueTask = item.status === "paused"
                      return (
                        <li key={`task-${item.id}`} className="border-b border-border/60 last:border-b-0">
                          <div
                            className={cn(
                              "flex w-full items-center gap-3 px-4 py-3 text-sm transition-colors hover:bg-muted/60 sm:px-6",
                              checked ? "bg-muted/40" : "",
                            )}
                          >
                            {jobKindFilter !== "package" ? (
                              <input
                                type="checkbox"
                                className="size-4 shrink-0 accent-zinc-900"
                                checked={checked}
                                disabled={!deletable}
                                onChange={(event) => toggleTaskSelected(item.id, event.target.checked)}
                                aria-label={`${t.home.selectTask}: ${item.title || shortUrl(item.url)}`}
                                onClick={(event) => event.stopPropagation()}
                              />
                            ) : (
                              <span className="size-4 shrink-0" aria-hidden="true" />
                            )}
                            <Link href={entry.href} className="flex min-w-0 flex-1 items-center gap-3">
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-2">
                                  <p className="truncate text-left font-medium text-foreground">
                                    {entry.title}
                                  </p>
                                  {jobKindFilter === "all" ? (
                                    <Badge variant="outline" className="text-[10px]">
                                      {t.home.jobKindBadgeTask}
                                    </Badge>
                                  ) : null}
                                </div>
                                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                                  <Badge className={statusBadgeClass(item.status)}>
                                    {statusLabel(item.status)}
                                  </Badge>
                                  <span>{formatTime(item.created_at)}</span>
                                  {isActive(item.status) && item.current_stage ? (
                                    <span>· {stageLabel(item.current_stage)}</span>
                                  ) : null}
                                  {isAwaitingAction(item.status) ? (
                                    <span>· {t.status.paused}</span>
                                  ) : null}
                                </div>
                              </div>
                              <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                            </Link>
                            {canPauseTask ? (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={taskPausingId === item.id}
                                onClick={() => handlePauseTask(item.id)}
                              >
                                {taskPausingId === item.id ? (
                                  <Loader2 className="size-3.5 animate-spin" />
                                ) : (
                                  <Pause className="size-3.5" />
                                )}
                                {taskPausingId === item.id ? t.task.pausing : t.task.pauseTask}
                              </Button>
                            ) : null}
                            {canContinueTask ? (
                              <Button
                                size="sm"
                                disabled={taskContinuingId === item.id}
                                onClick={() => handleContinueTask(item.id)}
                              >
                                {taskContinuingId === item.id ? (
                                  <Loader2 className="size-3.5 animate-spin" />
                                ) : (
                                  <Play className="size-3.5" />
                                )}
                                {taskContinuingId === item.id ? t.task.continuing : t.task.resumePausedTask}
                              </Button>
                            ) : null}
                          </div>
                        </li>
                      )
                    }

                    const pkg = entry.package
                    const deletable = isPackageDeletable(pkg.status)
                    const checked = selectedPackageIds.has(pkg.id)
                    const canRetryPackage = packageHasRetryableFailures(pkg)
                    const canPausePackage = pkg.status === "queued"
                    const canContinuePackage = pkg.status === "paused"
                    return (
                      <li key={`package-${pkg.id}`} className="border-b border-border/60 last:border-b-0">
                        <div
                          className={cn(
                            "flex w-full items-center gap-3 px-4 py-3 text-sm transition-colors hover:bg-muted/60 sm:px-6",
                            checked ? "bg-muted/40" : "",
                          )}
                        >
                          {jobKindFilter !== "task" ? (
                            <input
                              type="checkbox"
                              className="size-4 shrink-0 accent-zinc-900"
                              checked={checked}
                              disabled={!deletable}
                              onChange={(event) => togglePackageSelected(pkg.id, event.target.checked)}
                              aria-label={`${t.home.selectPackage}: ${entry.title}`}
                              onClick={(event) => event.stopPropagation()}
                            />
                          ) : (
                            <span className="size-4 shrink-0" aria-hidden="true" />
                          )}
                          <Link href={entry.href} className="flex min-w-0 flex-1 items-center gap-3">
                            <div className="min-w-0 flex-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <p className="truncate text-left font-medium text-foreground">
                                  {entry.title}
                                </p>
                                {jobKindFilter === "all" ? (
                                  <Badge variant="outline" className="text-[10px]">
                                    {t.home.jobKindBadgePackage}
                                  </Badge>
                                ) : null}
                              </div>
                              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                                <Badge className={statusBadgeClass(pkg.status)}>
                                  {statusLabel(pkg.status)}
                                </Badge>
                                <span>{formatTime(pkg.created_at)}</span>
                                <span className="truncate">· {entry.subtitle}</span>
                              </div>
                            </div>
                            <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                          </Link>
                          {canPausePackage ? (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={packagePausingId === pkg.id}
                              onClick={() => handlePausePackage(pkg.id)}
                            >
                              {packagePausingId === pkg.id ? (
                                <Loader2 className="size-3.5 animate-spin" />
                              ) : (
                                <Pause className="size-3.5" />
                              )}
                              {packagePausingId === pkg.id
                                ? t.home.submitting
                                : t.home.packagePause}
                            </Button>
                          ) : null}
                          {canContinuePackage ? (
                            <Button
                              size="sm"
                              disabled={packageContinuingId === pkg.id}
                              onClick={() => handleContinuePackage(pkg.id)}
                            >
                              {packageContinuingId === pkg.id ? (
                                <Loader2 className="size-3.5 animate-spin" />
                              ) : (
                                <Play className="size-3.5" />
                              )}
                              {packageContinuingId === pkg.id
                                ? t.home.submitting
                                : t.home.packageContinue}
                            </Button>
                          ) : null}
                          {canRetryPackage ? (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={packageRetryingId === pkg.id}
                              onClick={() => handleRetryPackageFailed(pkg.id)}
                            >
                              <RotateCw className="size-3.5" />
                              {packageRetryingId === pkg.id
                                ? t.home.submitting
                                : t.home.packageRetryFailed}
                            </Button>
                          ) : null}
                        </div>
                      </li>
                    )
                  })}
                </ul>
              </div>
            )}

            <Dialog open={batchRetryOpen} onOpenChange={setBatchRetryOpen}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t.home.batchRetryTitle}</DialogTitle>
                  <DialogDescription>{t.home.batchRetryDescription}</DialogDescription>
                </DialogHeader>
                <p className="text-sm text-muted-foreground">
                  {selectedCountText(t.home.selectedCount, selectedCount)}
                  {selectedFailedCount > 0
                    ? ` · ${selectedCountText(t.home.batchRetryFailedCount, selectedFailedCount)}`
                    : ""}
                </p>
                {batchRetryError ? (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                    {batchRetryError}
                  </div>
                ) : null}
                <DialogFooter>
                  <DialogClose render={<Button variant="outline" disabled={batchRetrying} />}>
                    {t.common.cancel}
                  </DialogClose>
                  <Button
                    onClick={handleBatchRetry}
                    disabled={batchRetrying || selectedFailedCount === 0}
                  >
                    {batchRetrying ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <RotateCw className="size-4" />
                    )}
                    {batchRetrying ? t.home.batchRetrying : t.home.batchRetryConfirm}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>

            <Dialog open={batchCleanupOpen} onOpenChange={setBatchCleanupOpen}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t.home.batchCleanupTitle}</DialogTitle>
                  <DialogDescription>{t.home.batchCleanupDescription}</DialogDescription>
                </DialogHeader>
                <p className="text-sm text-muted-foreground">
                  {selectedCountText(t.home.selectedCount, selectedCount)}
                </p>
                {batchCleanupError ? (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                    {batchCleanupError}
                  </div>
                ) : null}
                <DialogFooter>
                  <DialogClose render={<Button variant="outline" disabled={batchCleaning} />}>
                    {t.common.cancel}
                  </DialogClose>
                  <Button
                    onClick={handleBatchCleanup}
                    disabled={batchCleaning || selectedCount === 0}
                  >
                    {batchCleaning ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <FolderMinus className="size-4" />
                    )}
                    {batchCleaning ? t.home.batchCleaning : t.home.batchCleanupConfirm}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>

            <Dialog open={batchDeleteOpen} onOpenChange={setBatchDeleteOpen}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t.home.batchDeleteTitle}</DialogTitle>
                  <DialogDescription>{t.home.batchDeleteDescription}</DialogDescription>
                </DialogHeader>
                <p className="text-sm text-muted-foreground">
                  {selectedCountText(t.home.selectedCount, selectedCount)}
                </p>
                {batchDeleteError ? (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                    {batchDeleteError}
                  </div>
                ) : null}
                <DialogFooter>
                  <DialogClose render={<Button variant="outline" disabled={batchDeleting} />}>
                    {t.common.cancel}
                  </DialogClose>
                  <Button
                    variant="destructive"
                    onClick={handleBatchDelete}
                    disabled={batchDeleting || selectedCount === 0}
                  >
                    {batchDeleting ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <Trash2 className="size-4" />
                    )}
                    {batchDeleting ? t.home.batchDeleting : t.home.batchDeleteConfirm}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>

            {listTotal > 0 ? (
              <div className="flex flex-col gap-3 border-t border-border/60 px-4 py-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
                <span>{pageRangeText(language, pageStart, pageEnd, listTotal)}</span>
                <div className="flex items-center justify-between gap-3 sm:justify-end">
                  <span>{pageIndexText(language, displayPage, totalPages)}</span>
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setTaskPage((page) => Math.max(1, page - 1))}
                      disabled={displayPage <= 1}
                    >
                      <ChevronLeft className="size-4" />
                      {t.home.previousPage}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setTaskPage((page) => Math.min(totalPages, page + 1))}
                      disabled={displayPage >= totalPages}
                    >
                      {t.home.nextPage}
                      <ChevronRight className="size-4" />
                    </Button>
                  </div>
                </div>
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  )
}
