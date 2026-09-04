"use client"

import { useEffect, useState } from "react"
import { Loader2, LogOut, MonitorPlay } from "lucide-react"

import {
  getDouyinAuthStatus,
  getDouyinLoginStatus,
  getDouyinSettings,
  logoutDouyin,
  saveDouyinSettings,
  startDouyinLogin,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export function DouyinSettingsPanel({ active }: { active: boolean }) {
  const { t } = useI18n()
  const [loggedIn, setLoggedIn] = useState(false)
  const [defaultTags, setDefaultTags] = useState("配音,翻译")
  const [busy, setBusy] = useState("")
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")
  const [loginActive, setLoginActive] = useState(false)

  useEffect(() => {
    if (!active) return
    void Promise.all([getDouyinAuthStatus(), getDouyinSettings()])
      .then(([auth, settings]) => {
        setLoggedIn(Boolean(auth.logged_in))
        setDefaultTags(settings.default_tags || "配音,翻译")
        setMessage("")
        setError("")
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : t.settings.loadModelsError)
      })
  }, [active, t.settings.loadModelsError])

  useEffect(() => {
    if (!loginActive) return
    const timer = window.setInterval(() => {
      void getDouyinLoginStatus()
        .then((session) => {
          setMessage(session.message || "")
          if (session.logged_in) {
            setLoggedIn(true)
            setLoginActive(false)
          } else if (!session.active) {
            setLoginActive(false)
            if (session.error) {
              setError(session.message || String(session.error))
            }
          }
        })
        .catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [loginActive])

  async function handleLogin() {
    setBusy("login")
    setError("")
    try {
      const session = await startDouyinLogin(300)
      setLoginActive(Boolean(session?.active) || true)
      setMessage(session?.message || t.settings.douyinLoginHint)
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
      await logoutDouyin()
      setLoggedIn(false)
      setMessage(t.settings.douyinLoggedOut)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.settings.loadModelsError)
    } finally {
      setBusy("")
    }
  }

  async function handleSave() {
    setBusy("save")
    setError("")
    try {
      await saveDouyinSettings({ default_tags: defaultTags })
      setMessage(t.settings.saved)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.settings.loadModelsError)
    } finally {
      setBusy("")
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border/60 p-3 text-sm">
        <p className="font-medium">
          {loggedIn ? t.settings.douyinLoggedIn : t.settings.douyinLoggedOut}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">{t.settings.douyinLoginHelp}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button type="button" onClick={handleLogin} disabled={Boolean(busy) || loginActive}>
            {busy === "login" || loginActive ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <MonitorPlay className="size-4" />
            )}
            {loginActive ? t.settings.douyinWaitingLogin : t.settings.douyinLogin}
          </Button>
          <Button type="button" variant="outline" onClick={handleLogout} disabled={Boolean(busy) || !loggedIn}>
            <LogOut className="size-4" />
            {t.settings.douyinLogout}
          </Button>
        </div>
      </div>

      <div className="space-y-2">
        <Label htmlFor="douyin-default-tags">{t.settings.douyinDefaultTags}</Label>
        <Input
          id="douyin-default-tags"
          value={defaultTags}
          onChange={(event) => setDefaultTags(event.target.value)}
          placeholder="配音,翻译"
        />
      </div>

      <div className="flex justify-end">
        <Button type="button" onClick={handleSave} disabled={Boolean(busy)}>
          {busy === "save" ? <Loader2 className="size-4 animate-spin" /> : null}
          {t.settings.save}
        </Button>
      </div>

      {message ? <p className="text-xs text-muted-foreground">{message}</p> : null}
      {error ? <p className="text-xs text-red-400">{error}</p> : null}
    </div>
  )
}
