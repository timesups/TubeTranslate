"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { Loader2, RefreshCw, Upload } from "lucide-react"

import {
  BilibiliDraft,
  BilibiliJob,
  BilibiliPartition,
  BilibiliReadyItem,
  generateBilibiliMeta,
  getBilibiliAuthStatus,
  getBilibiliJob,
  getBilibiliPartitions,
  listBilibiliReady,
  publishBilibili,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { AppShell } from "@/components/app-shell"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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

type DraftMap = Record<string, BilibiliDraft>
type JobMap = Record<string, BilibiliJob>

function formatSize(bytes: number) {
  if (!bytes) return "0 B"
  const units = ["B", "KB", "MB", "GB"]
  let n = bytes
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export default function PublishPage() {
  const { t } = useI18n()
  const [items, setItems] = useState<BilibiliReadyItem[]>([])
  const [videoDir, setVideoDir] = useState("")
  const [partitions, setPartitions] = useState<BilibiliPartition[]>([])
  const [loggedIn, setLoggedIn] = useState(false)
  const [uname, setUname] = useState("")
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [drafts, setDrafts] = useState<DraftMap>({})
  const [jobs, setJobs] = useState<JobMap>({})
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState("")
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")

  const readyItems = useMemo(() => items.filter((item) => item.ready), [items])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const [auth, ready, parts] = await Promise.all([
        getBilibiliAuthStatus(),
        listBilibiliReady(),
        getBilibiliPartitions(),
      ])
      setLoggedIn(Boolean(auth.logged_in))
      setUname(auth.uname || "")
      setItems(ready.items)
      setVideoDir(ready.video_dir)
      setPartitions(parts)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.publish.empty)
    } finally {
      setLoading(false)
    }
  }, [t.publish.empty])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    const active = Object.values(jobs).filter(
      (job) => job.status === "queued" || job.status === "uploading",
    )
    if (!active.length) return
    const timer = window.setInterval(() => {
      void Promise.all(
        active.map(async (job) => {
          const next = await getBilibiliJob(job.id)
          setJobs((current) => ({ ...current, [job.id]: next }))
        }),
      )
    }, 1500)
    return () => window.clearInterval(timer)
  }, [jobs])

  function toggleSelected(id: string, checked: boolean) {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function selectReady() {
    setSelected(new Set(readyItems.map((item) => item.id)))
  }

  async function handleGenerate(id: string) {
    setBusyId(id)
    setError("")
    try {
      const draft = await generateBilibiliMeta(id)
      setDrafts((current) => ({ ...current, [id]: draft }))
      setSelected((current) => new Set(current).add(id))
    } catch (err) {
      setError(err instanceof Error ? err.message : t.publish.jobError)
    } finally {
      setBusyId("")
    }
  }

  async function handleGenerateAll() {
    for (const item of readyItems) {
      if (drafts[item.id]) continue
      await handleGenerate(item.id)
    }
  }

  async function handlePublish() {
    if (!loggedIn) {
      setError(t.publish.loginRequired)
      return
    }
    const payload = Array.from(selected)
      .map((id) => drafts[id])
      .filter(Boolean)
      .map((draft) => ({
        id: draft.id,
        title: draft.title,
        desc: draft.desc,
        tag: draft.tag,
        dynamic: draft.dynamic,
        tid: draft.tid,
        copyright: draft.copyright || 1,
      }))
    if (!payload.length) {
      setError(t.publish.empty)
      return
    }
    setBusyId("__publish__")
    setError("")
    setMessage("")
    try {
      const result = await publishBilibili(payload)
      const nextJobs: JobMap = {}
      for (const job of result.jobs) {
        nextJobs[job.job_id] = job
      }
      setJobs((current) => ({ ...current, ...nextJobs }))
      setMessage(t.publish.publishSelected)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.publish.jobError)
    } finally {
      setBusyId("")
    }
  }

  function updateDraft(id: string, patch: Partial<BilibiliDraft>) {
    setDrafts((current) => {
      const base = current[id]
      if (!base) return current
      return { ...current, [id]: { ...base, ...patch } }
    })
  }

  return (
    <AppShell>
      <div className="flex flex-col gap-4">
        <Card>
          <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle>{t.publish.title}</CardTitle>
              <p className="mt-1 text-sm text-muted-foreground">{t.publish.subtitle}</p>
              {videoDir ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  {t.publish.videoDir}: {videoDir}
                </p>
              ) : null}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" onClick={() => void refresh()} disabled={loading}>
                {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                {t.publish.refresh}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => void handleGenerateAll()}
                disabled={!readyItems.length || Boolean(busyId)}
              >
                {t.publish.generateAll}
              </Button>
              <Button
                type="button"
                onClick={() => void handlePublish()}
                disabled={!selected.size || Boolean(busyId)}
              >
                {busyId === "__publish__" ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Upload className="size-4" />
                )}
                {t.publish.publishSelected}
                {selected.size ? ` (${selected.size})` : ""}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant={loggedIn ? "default" : "outline"}>
                {loggedIn ? uname || "Bilibili" : t.publish.loginRequired}
              </Badge>
              <Button type="button" size="sm" variant="ghost" onClick={selectReady}>
                {t.publish.selectReady}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setSelected(new Set())}
              >
                {t.publish.clearSelection}
              </Button>
            </div>
            {error ? (
              <div className="rounded-lg border border-red-500/30 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            ) : null}
            {message ? (
              <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-300">
                {message}
              </div>
            ) : null}
          </CardContent>
        </Card>

        {loading ? (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              {t.common.loading}
            </CardContent>
          </Card>
        ) : items.length === 0 ? (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              {t.publish.empty}
            </CardContent>
          </Card>
        ) : (
          items.map((item) => {
            const draft = drafts[item.id]
            const checked = selected.has(item.id)
            return (
              <Card key={item.id} className={cn(checked ? "ring-1 ring-[#00aeec]/30" : "")}>
                <CardContent className="space-y-4 pt-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <label className="flex items-start gap-3 text-sm">
                      <input
                        type="checkbox"
                        className="mt-1 size-4"
                        checked={checked}
                        disabled={!item.ready}
                        onChange={(event) => toggleSelected(item.id, event.target.checked)}
                      />
                      <span>
                        <span className="font-medium text-foreground">{item.name}</span>
                        <span className="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
                          <span>{formatSize(item.size)}</span>
                          {item.ready ? (
                            <Badge>{t.publish.ready}</Badge>
                          ) : (
                            <>
                              {!item.has_cover ? <Badge variant="outline">{t.publish.missingCover}</Badge> : null}
                              {!item.has_srt ? <Badge variant="outline">{t.publish.missingSrt}</Badge> : null}
                            </>
                          )}
                        </span>
                      </span>
                    </label>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!item.ready || busyId === item.id}
                      onClick={() => void handleGenerate(item.id)}
                    >
                      {busyId === item.id ? <Loader2 className="size-4 animate-spin" /> : null}
                      {t.publish.generate}
                    </Button>
                  </div>

                  {draft ? (
                    <div className="grid gap-3">
                      <div className="space-y-1.5">
                        <Label>{t.publish.titleLabel}</Label>
                        <Input
                          value={draft.title}
                          onChange={(event) => updateDraft(item.id, { title: event.target.value })}
                          maxLength={80}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>{t.publish.descLabel}</Label>
                        <Textarea
                          value={draft.desc}
                          onChange={(event) => updateDraft(item.id, { desc: event.target.value })}
                          className="min-h-28"
                        />
                      </div>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <div className="space-y-1.5">
                          <Label>{t.publish.tagLabel}</Label>
                          <Input
                            value={draft.tag}
                            onChange={(event) => updateDraft(item.id, { tag: event.target.value })}
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label>{t.publish.tidLabel}</Label>
                          <Select
                            value={String(draft.tid)}
                            onValueChange={(value) =>
                              updateDraft(item.id, { tid: Number(value || draft.tid) })
                            }
                          >
                            <SelectTrigger>
                              {partitions.find((part) => part.tid === draft.tid)?.name || draft.tid}
                            </SelectTrigger>
                            <SelectContent>
                              {partitions.map((part) => (
                                <SelectItem key={part.tid} value={String(part.tid)}>
                                  {part.name} ({part.tid})
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                      <div className="space-y-1.5">
                        <Label>{t.publish.dynamicLabel}</Label>
                        <Input
                          value={draft.dynamic}
                          onChange={(event) => updateDraft(item.id, { dynamic: event.target.value })}
                        />
                      </div>
                    </div>
                  ) : null}
                </CardContent>
              </Card>
            )
          })
        )}

        {Object.keys(jobs).length ? (
          <Card>
            <CardHeader>
              <CardTitle>{t.publish.progress}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {Object.values(jobs).map((job) => (
                <div key={job.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <span>{job.message}</span>
                    <span className="tabular-nums text-muted-foreground">{job.progress}%</span>
                  </div>
                  {job.result?.bvid ? (
                    <a
                      className="mt-1 inline-block text-[#00aeec] hover:underline"
                      href={`https://www.bilibili.com/video/${job.result.bvid}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {t.publish.jobSuccess}: {job.result.bvid}
                    </a>
                  ) : null}
                  {job.error ? <p className="mt-1 text-red-300">{job.error}</p> : null}
                </div>
              ))}
            </CardContent>
          </Card>
        ) : null}
      </div>
    </AppShell>
  )
}
