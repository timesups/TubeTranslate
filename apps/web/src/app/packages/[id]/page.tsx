"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { use, useCallback, useMemo, useState } from "react"
import { CheckCircle2, Circle, Loader2, Pause, Play, RotateCw, Trash2, XCircle } from "lucide-react"

import {
  PackageItemStatus,
  StageStatus,
  TaskPackage,
  continueTaskPackage,
  deleteTaskPackage,
  getTaskPackage,
  isAbortError,
  pauseTaskPackage,
  retryFailedTaskPackage,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { statusBadgeClass } from "@/lib/status"
import { SerialPollingContext, useSerialPolling } from "@/lib/use-serial-polling"
import { AppShell } from "@/components/app-shell"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"

function stageIcon(status: StageStatus | PackageItemStatus) {
  if (status === "succeeded" || status === "skipped") {
    return <CheckCircle2 className="size-5 text-[#00aeec]" />
  }
  if (status === "failed") return <XCircle className="size-5 text-[#ff4d6d]" />
  if (status === "running") return <Loader2 className="size-5 animate-spin text-[#fb7299]" />
  return <Circle className="size-5 text-muted-foreground" />
}

export default function PackageDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const router = useRouter()
  const { stageLabel, statusLabel, t } = useI18n()
  const [pkg, setPkg] = useState<TaskPackage | null>(null)
  const [error, setError] = useState("")
  const [continuing, setContinuing] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const pollPackage = useCallback(async ({ signal, isCurrent }: SerialPollingContext) => {
    try {
      const next = await getTaskPackage(id, signal)
      if (!isCurrent()) return
      setPkg(next)
      setError("")
    } catch (err) {
      if (!isCurrent() || isAbortError(err)) return
      setError(err instanceof Error ? err.message : "Failed to load package")
    }
  }, [id])

  useSerialPolling(pollPackage)

  const failedCount = useMemo(() => {
    if (typeof pkg?.failed_count === "number") return pkg.failed_count
    return (pkg?.items || []).filter((item) => item.status === "failed").length
  }, [pkg?.failed_count, pkg?.items])

  const canRetryFailed = failedCount > 0 && !["running", "queued"].includes(pkg?.status || "")
  const canPause = pkg?.status === "running" || pkg?.status === "queued"
  const pausePending = Boolean(pkg?.pause_requested)

  const progress = useMemo(() => {
    const items = pkg?.items || []
    if (!items.length) return 0
    const done = items.filter((item) => ["succeeded", "skipped", "failed"].includes(item.status)).length
    return Math.round((done / items.length) * 100)
  }, [pkg?.items])

  async function handleContinue() {
    setContinuing(true)
    setError("")
    try {
      const next = await continueTaskPackage(id)
      setPkg(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to continue package")
    } finally {
      setContinuing(false)
    }
  }

  async function handlePause() {
    setPausing(true)
    setError("")
    try {
      const next = await pauseTaskPackage(id)
      setPkg(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.task.pausePackageError)
    } finally {
      setPausing(false)
    }
  }

  async function handleRetryFailed() {
    setRetrying(true)
    setError("")
    try {
      const next = await retryFailedTaskPackage(id)
      setPkg(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to retry package items")
    } finally {
      setRetrying(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    setError("")
    try {
      await deleteTaskPackage(id)
      router.push("/")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete package")
    } finally {
      setDeleting(false)
    }
  }

  return (
    <AppShell backHref="/" title={pkg?.name || t.home.packageSectionTitle}>
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 p-4">
        {pkg ? (
          <Card>
            <CardHeader className="gap-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <CardTitle>{pkg.name || pkg.source_root}</CardTitle>
                  <p className="mt-1 text-sm text-muted-foreground">{pkg.source_root}</p>
                </div>
                <Badge className={statusBadgeClass(pkg.status)}>{statusLabel(pkg.status)}</Badge>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>
                    {pkg.succeeded_count ?? 0}/{pkg.item_count ?? pkg.items?.length ?? 0} done
                  </span>
                  <span>{progress}%</span>
                </div>
                <Progress value={progress} />
              </div>
              <div className="flex flex-wrap gap-2">
                {canPause ? (
                  <Button variant="outline" onClick={handlePause} disabled={pausing || pausePending}>
                    {pausing || pausePending ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <Pause className="size-4" />
                    )}
                    {pausing
                      ? t.home.packagePausing
                      : pausePending
                        ? t.home.packagePausingRequested
                        : t.home.packagePause}
                  </Button>
                ) : null}
                {pkg.status === "paused" ? (
                  <Button onClick={handleContinue} disabled={continuing}>
                    <Play className="size-4" />
                    {continuing ? t.home.submitting : t.home.packageContinue}
                  </Button>
                ) : null}
                {canRetryFailed ? (
                  <Button variant="outline" onClick={handleRetryFailed} disabled={retrying}>
                    <RotateCw className="size-4" />
                    {retrying
                      ? t.home.submitting
                      : `${t.home.packageRetryFailed} (${failedCount})`}
                  </Button>
                ) : null}
                <Button variant="outline" onClick={handleDelete} disabled={deleting}>
                  <Trash2 className="size-4" />
                  {deleting ? t.home.batchDeleting : t.home.batchDelete}
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-xs text-muted-foreground">
                {t.home.packageSuffixLabel}: <span className="font-mono">{pkg.output_suffix}</span>
              </p>
              {pkg.error_message ? (
                <p className="rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                  {pkg.error_message}
                </p>
              ) : null}
              {(pkg.items || []).map((item) => (
                <div key={item.id} className="rounded-lg border border-border/60 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{item.title || item.relative_path || item.source_path}</p>
                      <p className="truncate text-xs text-muted-foreground">{item.source_path}</p>
                    </div>
                    <Badge className={statusBadgeClass(item.status)}>{statusLabel(item.status)}</Badge>
                  </div>
                  {item.exported_video_path ? (
                    <p className="mt-2 truncate text-xs text-emerald-300">{item.exported_video_path}</p>
                  ) : null}
                  {item.error_message ? (
                    <p className="mt-2 text-xs text-red-300">{item.error_message}</p>
                  ) : null}
                  <div className="mt-3 space-y-2">
                    {(item.stages || []).map((stage) => (
                      <div key={stage.name} className="flex items-start gap-2 text-sm">
                        {stageIcon(stage.status)}
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span>{stageLabel(stage.name)}</span>
                            <span className="text-xs text-muted-foreground">{statusLabel(stage.status)}</span>
                          </div>
                          {stage.last_message ? (
                            <p className="truncate text-xs text-muted-foreground">{stage.last_message}</p>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="py-8 text-sm text-muted-foreground">{t.common.loading}</CardContent>
          </Card>
        )}
        {error ? (
          <div className="rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        ) : null}
        <Button nativeButton={false} render={<Link href="/" />}>
          {t.common.back}
        </Button>
      </div>
    </AppShell>
  )
}
