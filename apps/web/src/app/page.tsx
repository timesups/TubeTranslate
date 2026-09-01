"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { ChangeEvent, FormEvent, useCallback, useMemo, useRef, useState } from "react"
import { ChevronLeft, ChevronRight, FolderMinus, Loader2, Play, RotateCw, Search, Trash2, Upload } from "lucide-react"

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
  deleteTasksBatch,
  isAbortError,
  listTaskPackages,
  listTasks,
  parseVideoUrls,
  resumeTasksBatch,
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
  return status === "succeeded" || status === "failed"
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
  if (language === "zh") return `显示 ${start}-${end} / 共 ${total} 个任务`
  return `Showing ${start}-${end} of ${total} tasks`
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
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [taskTotal, setTaskTotal] = useState(0)
  const [activeTaskCount, setActiveTaskCount] = useState<number | null>(null)
  const [taskPage, setTaskPage] = useState(1)
  const [taskPageSize, setTaskPageSize] = useState(20)
  const [taskQuery, setTaskQuery] = useState("")
  const [taskStatus, setTaskStatus] = useState<TaskListStatus>("all")
  const [taskExecutionMode, setTaskExecutionMode] = useState<TaskListExecutionMode>("all")
  const [taskSort, setTaskSort] = useState<TaskListSort>("created_desc")
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [taskListError, setTaskListError] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(() => new Set())
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
  const [packageSuffix, setPackageSuffix] = useState("_译制")
  const [packageRecursive, setPackageRecursive] = useState(false)
  const [packageScanCount, setPackageScanCount] = useState<number | null>(null)
  const [packageScanSkipped, setPackageScanSkipped] = useState(0)
  const [packageScanning, setPackageScanning] = useState(false)
  const [packageSubmitting, setPackageSubmitting] = useState(false)
  const [packageMessage, setPackageMessage] = useState("")
  const [packageError, setPackageError] = useState("")
  const [packages, setPackages] = useState<TaskPackage[]>([])
  const parsedUrls = parseVideoUrls(urlsText)
  const selectableTasks = useMemo(
    () => tasks.filter((task) => isDeletable(task.status)),
    [tasks],
  )
  const selectedFailedCount = useMemo(
    () => tasks.filter((task) => selectedTaskIds.has(task.id) && task.status === "failed").length,
    [tasks, selectedTaskIds],
  )
  const selectedCount = selectedTaskIds.size
  const pageSelectedCount = useMemo(
    () => selectableTasks.filter((task) => selectedTaskIds.has(task.id)).length,
    [selectableTasks, selectedTaskIds],
  )
  const allPageSelected =
    selectableTasks.length > 0 && pageSelectedCount === selectableTasks.length
  const somePageSelected = pageSelectedCount > 0 && !allPageSelected

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
    { value: "volcengine", label: t.home.ttsVolcengine },
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
  const effectiveGenerateMeta = bilibiliAutoPublish ? true : bilibiliGenerateMeta

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
      const [result, packageResult] = await Promise.all([
        listTasks({
          page: taskPage,
          page_size: taskPageSize,
          q: taskQuery,
          status: taskStatus,
          execution_mode: taskExecutionMode,
          sort: taskSort,
        }, signal),
        listTaskPackages(10),
      ])
      if (isCurrent()) {
        setTaskListError("")
        applyTaskList(result)
        setPackages(packageResult.packages)
      }
    } catch (err) {
      if (isCurrent() && !isAbortError(err)) {
        setTaskListError(err instanceof Error ? err.message : t.home.loadError)
      }
    }
  }, [
    applyTaskList,
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

  function toggleSelectAllPage(checked: boolean) {
    setSelectedTaskIds((current) => {
      const next = new Set(current)
      for (const task of selectableTasks) {
        if (checked) next.add(task.id)
        else next.delete(task.id)
      }
      return next
    })
  }

  function selectFinishedOnPage() {
    setSelectedTaskIds((current) => {
      const next = new Set(current)
      for (const task of tasks) {
        if (isFinished(task.status)) next.add(task.id)
      }
      return next
    })
  }

  function clearTaskSelection() {
    setSelectedTaskIds(new Set())
  }

  async function handleBatchDelete() {
    const taskIds = Array.from(selectedTaskIds)
    if (!taskIds.length) {
      setBatchDeleteError(t.home.batchDeleteNone)
      return
    }
    setBatchDeleting(true)
    setBatchDeleteError("")
    try {
      const result = await deleteTasksBatch(taskIds)
      const deletedSet = new Set(result.deleted)
      setSelectedTaskIds((current) => {
        const next = new Set(current)
        for (const id of deletedSet) next.delete(id)
        for (const id of result.missing) next.delete(id)
        return next
      })
      setBatchDeleteOpen(false)
      setMessage(
        batchDeleteSummaryText(
          t.home.batchDeleteSummary,
          result.deleted.length,
          result.skipped.length,
          result.missing.length,
          result.failed.length,
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
    if (!taskIds.length) {
      setBatchCleanupError(t.home.batchCleanupNone)
      return
    }
    setBatchCleaning(true)
    setBatchCleanupError("")
    try {
      const result = await cleanupTasksBatch(taskIds)
      setBatchCleanupOpen(false)
      setMessage(
        batchCleanupSummaryText(
          t.home.batchCleanupSummary,
          result.cleaned.length,
          result.skipped.length,
          result.missing.length,
          result.failed.length,
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
    if (!taskIds.length) {
      setBatchRetryError(t.home.batchRetryNone)
      return
    }
    setBatchRetrying(true)
    setBatchRetryError("")
    try {
      const result = await resumeTasksBatch(taskIds)
      const resumedSet = new Set(result.resumed)
      setSelectedTaskIds((current) => {
        const next = new Set(current)
        for (const id of resumedSet) next.delete(id)
        for (const id of result.missing) next.delete(id)
        return next
      })
      setBatchRetryOpen(false)
      setMessage(
        batchRetrySummaryText(
          t.home.batchRetrySummary,
          result.resumed.length,
          result.skipped.length,
          result.missing.length,
          result.failed.length,
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
        output_suffix: packageSuffix.trim() || "_译制",
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
        output_suffix: packageSuffix.trim() || "_译制",
        direction: localDirection,
        execution_mode: executionMode,
        audio_mode: audioMode,
        tts_provider: ttsProvider,
        export_subtitle: true,
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
  const totalPages = Math.max(1, Math.ceil(taskTotal / taskPageSize))
  const displayPage = Math.min(taskPage, totalPages)
  const pageStart = taskTotal === 0 ? 0 : (displayPage - 1) * taskPageSize + 1
  const pageEnd = Math.min(taskTotal, displayPage * taskPageSize)
  const hasTaskFilters = Boolean(taskQuery.trim()) || taskStatus !== "all" || taskExecutionMode !== "all"

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
              <div className="grid gap-3 sm:grid-cols-[1fr_180px]">
                <div className="space-y-2">
                  <Label htmlFor="package-suffix">{t.home.packageSuffixLabel}</Label>
                  <Input
                    id="package-suffix"
                    value={packageSuffix}
                    onChange={(event) => setPackageSuffix(event.target.value)}
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
            {packages.length ? (
              <div className="mt-6 space-y-2">
                <p className="text-sm font-medium">{t.home.packageListTitle}</p>
                {packages.map((entry) => (
                  <div
                    key={entry.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/60 px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="truncate font-medium">{entry.name || entry.source_root}</p>
                      <p className="truncate text-xs text-muted-foreground">
                        {(entry.succeeded_count ?? 0)}/{entry.item_count ?? 0} · {entry.source_root}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge className={statusBadgeClass(entry.status)}>{statusLabel(entry.status)}</Badge>
                      <Button nativeButton={false} size="sm" variant="outline" render={<Link href={`/packages/${entry.id}`} />}>
                        {t.home.packageView}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t.home.taskHistory} ({taskTotal})</CardTitle>
          </CardHeader>
          <CardContent className="px-0">
            <div className="border-b border-border/60 px-4 pb-4">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_140px_140px_180px_120px]">
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

            {tasks.length > 0 ? (
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
                      disabled={selectableTasks.length === 0}
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
                    disabled={!tasks.some((task) => isFinished(task.status))}
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

            {tasks.length === 0 ? (
              <div className="px-6 py-12 text-center text-sm text-muted-foreground">
                {hasTaskFilters ? t.home.noMatchingTasks : t.home.empty}
              </div>
            ) : (
              <div className="max-h-[min(56dvh,calc(100dvh-18rem))] overflow-y-auto overscroll-contain">
                <ul className="flex flex-col">
                  {tasks.map((item) => {
                    const deletable = isDeletable(item.status)
                    const checked = selectedTaskIds.has(item.id)
                    return (
                      <li key={item.id} className="border-b border-border/60 last:border-b-0">
                        <div
                          className={cn(
                            "flex w-full items-center gap-3 px-4 py-3 text-sm transition-colors hover:bg-muted/60 sm:px-6",
                            checked ? "bg-muted/40" : "",
                          )}
                        >
                          <input
                            type="checkbox"
                            className="size-4 shrink-0 accent-zinc-900"
                            checked={checked}
                            disabled={!deletable}
                            onChange={(event) => toggleTaskSelected(item.id, event.target.checked)}
                            aria-label={`${t.home.selectTask}: ${item.title || shortUrl(item.url)}`}
                            onClick={(event) => event.stopPropagation()}
                          />
                          <Link
                            href={`/tasks/${item.id}`}
                            className="flex min-w-0 flex-1 items-center gap-3"
                          >
                            <div className="min-w-0 flex-1">
                              <p className="truncate text-left font-medium text-foreground">
                                {item.title || shortUrl(item.url)}
                              </p>
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

            {taskTotal > 0 ? (
              <div className="flex flex-col gap-3 border-t border-border/60 px-4 py-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
                <span>{pageRangeText(language, pageStart, pageEnd, taskTotal)}</span>
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
