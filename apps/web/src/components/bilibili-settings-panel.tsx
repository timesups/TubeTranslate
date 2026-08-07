"use client"

import { useEffect, useState } from "react"
import { Loader2, LogOut, QrCode } from "lucide-react"

import {
  createBilibiliQr,
  getBilibiliAuthStatus,
  getBilibiliSettings,
  logoutBilibili,
  pollBilibiliQr,
  saveBilibiliSettings,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export function BilibiliSettingsPanel({ active }: { active: boolean }) {
  const { t } = useI18n()
  const [loggedIn, setLoggedIn] = useState(false)
  const [uname, setUname] = useState("")
  const [defaultTid, setDefaultTid] = useState("201")
  const [defaultTag, setDefaultTag] = useState("配音")
  const [videoDir, setVideoDir] = useState("")
  const [qrImage, setQrImage] = useState("")
  const [qrKey, setQrKey] = useState("")
  const [busy, setBusy] = useState("")
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    if (!active) return
    void Promise.all([getBilibiliAuthStatus(), getBilibiliSettings()])
      .then(([auth, settings]) => {
        setLoggedIn(Boolean(auth.logged_in))
        setUname(auth.uname || "")
        setDefaultTid(String(settings.default_tid || 201))
        setDefaultTag(settings.default_tag || "配音")
        setVideoDir(settings.video_dir || "")
        setMessage("")
        setError("")
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : t.settings.loadModelsError)
      })
  }, [active, t.settings.loadModelsError])

  useEffect(() => {
    if (!qrKey) return
    const timer = window.setInterval(() => {
      void pollBilibiliQr(qrKey)
        .then((result) => {
          if (result.logged_in || result.code === 0) {
            setLoggedIn(true)
            setUname(result.uname || "")
            setQrImage("")
            setQrKey("")
            setMessage(result.uname || t.settings.bilibiliStatus)
          }
        })
        .catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [qrKey, t.settings.bilibiliStatus])

  async function handleQrLogin() {
    setBusy("qr")
    setError("")
    try {
      const data = await createBilibiliQr()
      setQrKey(data.qrcode_key)
      setQrImage(data.image)
      setMessage(t.settings.bilibiliQrHint)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.settings.loadModelsError)
    } finally {
      setBusy("")
    }
  }

  async function handleLogout() {
    setBusy("logout")
    setError("")
    try {
      await logoutBilibili()
      setLoggedIn(false)
      setUname("")
      setQrImage("")
      setQrKey("")
      setMessage(t.settings.bilibiliLoggedOut)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.settings.loadModelsError)
    } finally {
      setBusy("")
    }
  }

  async function handleSaveSettings() {
    setBusy("save")
    setError("")
    try {
      const settings = await saveBilibiliSettings({
        default_tid: Number(defaultTid.replace(/[^0-9]/g, "") || 201),
        default_tag: defaultTag.trim() || "配音",
        default_copyright: 1,
        video_dir: videoDir.trim(),
      })
      setDefaultTid(String(settings.default_tid))
      setDefaultTag(settings.default_tag)
      setVideoDir(settings.video_dir)
      setMessage(t.settings.bilibiliSave)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.settings.loadModelsError)
    } finally {
      setBusy("")
    }
  }

  return (
    <div className="space-y-4 rounded-lg border border-border/60 p-3">
      <div>
        <h3 className="text-sm font-medium">{t.settings.bilibiliSection}</h3>
        <p className="mt-1 text-xs text-muted-foreground">{t.settings.bilibiliHelp}</p>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">{t.settings.bilibiliStatus}:</span>
        <span>{loggedIn ? uname || "Bilibili" : t.settings.bilibiliLoggedOut}</span>
        {loggedIn ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={Boolean(busy)}
            onClick={() => void handleLogout()}
          >
            {busy === "logout" ? <Loader2 className="size-4 animate-spin" /> : <LogOut className="size-4" />}
            {t.settings.bilibiliLogout}
          </Button>
        ) : null}
      </div>

      <div className="space-y-2">
        <Button
          type="button"
          variant="secondary"
          disabled={Boolean(busy)}
          onClick={() => void handleQrLogin()}
        >
          {busy === "qr" ? <Loader2 className="size-4 animate-spin" /> : <QrCode className="size-4" />}
          {t.settings.bilibiliLoginQr}
        </Button>
        {qrImage ? (
          <img
            src={qrImage}
            alt="Bilibili QR"
            className="size-44 rounded-md border bg-white p-2"
          />
        ) : null}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label htmlFor="bili-default-tid">{t.settings.bilibiliDefaultTid}</Label>
          <Input
            id="bili-default-tid"
            inputMode="numeric"
            value={defaultTid}
            onChange={(event) => setDefaultTid(event.target.value.replace(/[^0-9]/g, ""))}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="bili-default-tag">{t.settings.bilibiliDefaultTag}</Label>
          <Input
            id="bili-default-tag"
            value={defaultTag}
            onChange={(event) => setDefaultTag(event.target.value)}
          />
        </div>
      </div>

      <div className="grid gap-2">
        <Label htmlFor="bili-video-dir">{t.settings.bilibiliVideoDir}</Label>
        <Input
          id="bili-video-dir"
          value={videoDir}
          onChange={(event) => setVideoDir(event.target.value)}
        />
      </div>

      <Button type="button" disabled={Boolean(busy)} onClick={() => void handleSaveSettings()}>
        {busy === "save" ? <Loader2 className="size-4 animate-spin" /> : null}
        {t.settings.bilibiliSave}
      </Button>

      {error ? <p className="text-sm text-red-300">{error}</p> : null}
      {message ? <p className="text-sm text-muted-foreground">{message}</p> : null}
    </div>
  )
}
