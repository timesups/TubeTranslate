"use client"

import { FormEvent, useEffect, useState } from "react"
import { Eye, EyeOff, RefreshCw, Settings } from "lucide-react"

import {
  ApiError,
  getAzureTtsSettings,
  getAzureTtsVoices,
  getCookieInfo,
  getOpenAIModels,
  getOpenAISettings,
  getOutputSettings,
  getYtdlpSettings,
  saveAzureTtsSettings,
  saveCookie,
  saveOpenAISettings,
  saveOutputSettings,
  saveYtdlpSettings,
  validateAzureTtsKeys,
} from "@/lib/api"
import { LANGUAGE_OPTIONS, useI18n } from "@/lib/i18n"
import { BilibiliSettingsPanel } from "@/components/bilibili-settings-panel"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"

type SettingsForm = {
  cookie: string
  baseUrl: string
  apiKey: string
  model: string
  translateConcurrency: string
  proxyPort: string
  outputDir: string
  azureSubscriptionKey: string
  azureRegion: string
  azureVoice: string
  azureLocale: string
  azureEndpoint: string
  azureOutputFormat: string
  azureSpeechRate: string
  azureConcurrency: string
}

const SAVED_API_KEY_MASK = "********"
const SAVED_COOKIE_SENTINEL = "__YOUDUB_SAVED_COOKIE__"

type MessageKey = "keySaved"
type SaveSection = "cookie" | "openai" | "ytdlp" | "output" | "azure"
type SaveResult = {
  section: SaveSection
  status: "saved" | "failed" | "unchanged"
  httpStatus?: number
}

const defaultSettings: SettingsForm = {
  cookie: "",
  baseUrl: "https://api.openai.com/v1",
  apiKey: "",
  model: "gpt-4o-mini",
  translateConcurrency: "8",
  proxyPort: "",
  outputDir: "",
  azureSubscriptionKey: "",
  azureRegion: "eastasia",
  azureVoice: "zh-CN-XiaoxiaoNeural",
  azureLocale: "zh-CN",
  azureEndpoint: "",
  azureOutputFormat: "audio-24khz-48kbitrate-mono-mp3",
  azureSpeechRate: "0",
  azureConcurrency: "4",
}

function uniqueModels(models: string[]) {
  return Array.from(new Set(models.map((model) => model.trim()).filter(Boolean)))
}

export function SettingsDialog() {
  const { language, loadedModelsText, setLanguage, t } = useI18n()
  const [open, setOpen] = useState(false)
  const [settings, setSettings] = useState(defaultSettings)
  const [message, setMessage] = useState("")
  const [messageKey, setMessageKey] = useState<MessageKey | null>(null)
  const [modelOptions, setModelOptions] = useState<string[]>([])
  const [modelsLoaded, setModelsLoaded] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [showApiKey, setShowApiKey] = useState(false)
  const [showAzureKey, setShowAzureKey] = useState(false)
  const [cookieDirty, setCookieDirty] = useState(false)
  const [apiKeyDirty, setApiKeyDirty] = useState(false)
  const [azureKeyDirty, setAzureKeyDirty] = useState(false)
  const [azureKeyCount, setAzureKeyCount] = useState(0)
  const [azureVoiceOptions, setAzureVoiceOptions] = useState<string[]>([])
  const [azureVoicesLoaded, setAzureVoicesLoaded] = useState(false)
  const [azureVoicesLoading, setAzureVoicesLoading] = useState(false)
  const [azureKeysValidating, setAzureKeysValidating] = useState(false)
  const [azureKeyValidationMessage, setAzureKeyValidationMessage] = useState("")
  const [azureKeyValidationOk, setAzureKeyValidationOk] = useState<boolean | null>(null)
  const [saveResults, setSaveResults] = useState<SaveResult[]>([])
  const [saving, setSaving] = useState(false)

  const cookieValue =
    settings.cookie === SAVED_COOKIE_SENTINEL ? t.settings.savedCookie : settings.cookie
  const visibleMessage = messageKey === "keySaved" ? t.settings.keySaved : message

  useEffect(() => {
    if (!open) return
    Promise.all([
      getCookieInfo(),
      getOpenAISettings(),
      getYtdlpSettings(),
      getOutputSettings(),
      getAzureTtsSettings(),
    ])
      .then(([cookie, openai, ytdlp, output, azure]) => {
        setSettings({
          cookie: cookie.exists ? SAVED_COOKIE_SENTINEL : "",
          baseUrl: openai.base_url,
          apiKey: openai.has_api_key ? openai.api_key || SAVED_API_KEY_MASK : "",
          model: openai.model,
          translateConcurrency: openai.translate_concurrency || "8",
          proxyPort: ytdlp.proxy_port,
          outputDir: output.output_dir,
          azureSubscriptionKey: "",
          azureRegion: azure.region,
          azureVoice: azure.voice,
          azureLocale: azure.locale,
          azureEndpoint: azure.endpoint,
          azureOutputFormat: azure.output_format,
          azureSpeechRate: azure.speech_rate,
          azureConcurrency: azure.concurrency || "4",
        })
        setModelOptions(uniqueModels([openai.model]))
        setAzureVoiceOptions(uniqueModels([azure.voice]))
        setAzureKeyCount(azure.key_count || (azure.has_subscription_key ? 1 : 0))
        setModelsLoaded(false)
        setAzureVoicesLoaded(false)
        setShowApiKey(false)
        setShowAzureKey(false)
        setCookieDirty(false)
        setApiKeyDirty(false)
        setAzureKeyDirty(false)
        setAzureKeyValidationMessage("")
        setAzureKeyValidationOk(null)
        setSaveResults([])
        setMessage("")
        setMessageKey(openai.has_api_key ? "keySaved" : null)
      })
      .catch((err) => {
        setMessageKey(null)
        setMessage(err.message)
      })
  }, [open])

  async function refreshSettingsFromServer() {
    const [cookieResult, openaiResult, ytdlpResult, outputResult, azureResult] =
      await Promise.allSettled([
        getCookieInfo(),
        getOpenAISettings(),
        getYtdlpSettings(),
        getOutputSettings(),
        getAzureTtsSettings(),
      ])

    setSettings((current) => {
      const refreshed = { ...current }
      if (cookieResult.status === "fulfilled") {
        refreshed.cookie = cookieResult.value.exists ? SAVED_COOKIE_SENTINEL : ""
      }
      if (openaiResult.status === "fulfilled") {
        const openai = openaiResult.value
        refreshed.baseUrl = openai.base_url
        refreshed.apiKey = openai.has_api_key ? openai.api_key || SAVED_API_KEY_MASK : ""
        refreshed.model = openai.model
        refreshed.translateConcurrency = openai.translate_concurrency || "8"
      }
      if (ytdlpResult.status === "fulfilled") {
        refreshed.proxyPort = ytdlpResult.value.proxy_port
      }
      if (outputResult.status === "fulfilled") {
        refreshed.outputDir = outputResult.value.output_dir
      }
      if (azureResult.status === "fulfilled") {
        const azure = azureResult.value
        refreshed.azureSubscriptionKey = ""
        refreshed.azureRegion = azure.region
        refreshed.azureVoice = azure.voice
        refreshed.azureLocale = azure.locale
        refreshed.azureEndpoint = azure.endpoint
        refreshed.azureOutputFormat = azure.output_format
        refreshed.azureSpeechRate = azure.speech_rate
        refreshed.azureConcurrency = azure.concurrency || "4"
      }
      return refreshed
    })

    if (cookieResult.status === "fulfilled") setCookieDirty(false)
    if (openaiResult.status === "fulfilled") {
      setApiKeyDirty(false)
      setShowApiKey(false)
      setModelOptions(uniqueModels([openaiResult.value.model]))
      setModelsLoaded(false)
    }
    if (azureResult.status === "fulfilled") {
      setAzureKeyDirty(false)
      setShowAzureKey(false)
      setAzureKeyCount(
        azureResult.value.key_count || (azureResult.value.has_subscription_key ? 1 : 0),
      )
      setAzureVoiceOptions(uniqueModels([azureResult.value.voice]))
      setAzureVoicesLoaded(false)
    }

    return [cookieResult, openaiResult, ytdlpResult, outputResult, azureResult].every(
      (result) => result.status === "fulfilled",
    )
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage("")
    setMessageKey(null)
    setSaveResults([])
    setSaving(true)
    const results: SaveResult[] = []

    async function saveSection(section: SaveSection, action: () => Promise<unknown>) {
      try {
        await action()
        results.push({ section, status: "saved" })
      } catch (err) {
        results.push({
          section,
          status: "failed",
          httpStatus: err instanceof ApiError ? err.status : undefined,
        })
      }
    }

    try {
      if (cookieDirty) {
        await saveSection("cookie", () => saveCookie(settings.cookie))
      } else {
        results.push({ section: "cookie", status: "unchanged" })
      }
      const clearApiKey = apiKeyDirty && !settings.apiKey.trim()
      await saveSection("openai", () => saveOpenAISettings({
        base_url: settings.baseUrl,
        api_key: apiKeyDirty ? settings.apiKey : "",
        clear_api_key: clearApiKey,
        model: settings.model,
        translate_concurrency: settings.translateConcurrency,
      }))
      await saveSection("ytdlp", () => saveYtdlpSettings({ proxy_port: settings.proxyPort }))
      await saveSection("output", () => saveOutputSettings({ output_dir: settings.outputDir }))
      const clearAzureKey = azureKeyDirty && !settings.azureSubscriptionKey.trim()
      await saveSection("azure", () => saveAzureTtsSettings({
        subscription_key: azureKeyDirty ? settings.azureSubscriptionKey : "",
        clear_subscription_key: clearAzureKey,
        region: settings.azureRegion,
        voice: settings.azureVoice,
        locale: settings.azureLocale,
        endpoint: settings.azureEndpoint,
        output_format: settings.azureOutputFormat,
        speech_rate: settings.azureSpeechRate,
        concurrency: settings.azureConcurrency,
      }))
      setSaveResults(results)
      setSettings((current) => ({
        ...current,
        cookie: cookieDirty ? "" : current.cookie,
        apiKey: apiKeyDirty ? "" : current.apiKey,
        azureSubscriptionKey: azureKeyDirty ? "" : current.azureSubscriptionKey,
      }))
      if (cookieDirty) setCookieDirty(false)
      if (apiKeyDirty) setApiKeyDirty(false)
      if (azureKeyDirty) setAzureKeyDirty(false)
      setShowApiKey(false)
      setShowAzureKey(false)

      const refreshed = await refreshSettingsFromServer()
      if (!refreshed) setMessage(t.settings.reloadError)
    } finally {
      setSaving(false)
    }
  }

  async function fetchModels() {
    setMessage("")
    setMessageKey(null)
    setModelsLoading(true)
    try {
      const response = await getOpenAIModels({
        base_url: settings.baseUrl,
        api_key: apiKeyDirty ? settings.apiKey : "",
      })
      const models = uniqueModels([settings.model, ...response.models])
      setModelOptions(models)
      setModelsLoaded(true)
      setSettings((current) => ({ ...current, model: current.model || models[0] || "" }))
      setMessage(models.length ? loadedModelsText(models.length) : t.settings.noModels)
    } catch (err) {
      setMessageKey(null)
      setMessage(err instanceof Error ? err.message : t.settings.loadModelsError)
    } finally {
      setModelsLoading(false)
    }
  }

  async function fetchAzureVoices() {
    setMessage("")
    setMessageKey(null)
    setAzureVoicesLoading(true)
    try {
      const response = await getAzureTtsVoices({
        region: settings.azureRegion,
        subscription_key: azureKeyDirty ? settings.azureSubscriptionKey : "",
        endpoint: settings.azureEndpoint,
      })
      const localePrefix = settings.azureLocale.trim()
      const fetched = response.voices.map((voice) => voice.trim()).filter(Boolean)
      const preferred = localePrefix
        ? fetched.filter((voice) => voice.toLowerCase().startsWith(localePrefix.toLowerCase()))
        : fetched
      const voices = uniqueModels([
        settings.azureVoice,
        ...preferred,
        ...fetched,
      ])
      setAzureVoiceOptions(voices)
      setAzureVoicesLoaded(true)
      setSettings((current) => ({
        ...current,
        azureVoice:
          current.azureVoice && voices.includes(current.azureVoice)
            ? current.azureVoice
            : preferred[0] || voices[0] || "",
      }))
      setMessage(voices.length ? loadedModelsText(voices.length) : t.settings.azureNoVoices)
    } catch (err) {
      setAzureVoicesLoaded(false)
      setMessageKey(null)
      setMessage(err instanceof Error ? err.message : t.settings.azureLoadVoicesError)
    } finally {
      setAzureVoicesLoading(false)
    }
  }

  async function validateAzureKeys() {
    setMessage("")
    setMessageKey(null)
    setAzureKeyValidationMessage("")
    setAzureKeyValidationOk(null)
    setAzureKeysValidating(true)
    try {
      const result = await validateAzureTtsKeys({
        region: settings.azureRegion,
        subscription_key: azureKeyDirty ? settings.azureSubscriptionKey : "",
        endpoint: settings.azureEndpoint,
      })
      const summary = result.ok
        ? t.settings.azureValidateKeysOk.replace("{count}", String(result.total))
        : t.settings.azureValidateKeysPartial
            .replace("{ok}", String(result.ok_count))
            .replace("{total}", String(result.total))
            .replace("{failed}", String(result.failed_count))
      const details = result.results
        .map((item) => `${item.label}: ${item.ok ? "OK" : item.detail}`)
        .join("\n")
      setAzureKeyValidationOk(result.ok)
      setAzureKeyValidationMessage(`${summary}\n${details}`)
      setMessage(summary)
    } catch (err) {
      setAzureKeyValidationOk(false)
      const detail = err instanceof Error ? err.message : t.settings.azureValidateKeysError
      setAzureKeyValidationMessage(detail)
      setMessage(detail)
    } finally {
      setAzureKeysValidating(false)
    }
  }

  const saveSectionLabels: Record<SaveSection, string> = {
    cookie: t.settings.cookie,
    openai: t.settings.openaiSaveSection,
    ytdlp: t.settings.ytdlpSaveSection,
    output: t.settings.outputSaveSection,
    azure: t.settings.azureSaveSection,
  }

  function saveResultText(result: SaveResult) {
    if (result.status === "saved") return t.settings.saveSucceeded
    if (result.status === "unchanged") return t.settings.saveUnchanged
    return `${t.settings.saveFailed}${result.httpStatus ? ` (HTTP ${result.httpStatus})` : ""}`
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" />}>
        <Settings className="size-4" />
        {t.settings.button}
      </DialogTrigger>
      <DialogContent className="top-4 flex max-h-[calc(100dvh-2rem)] w-full flex-col gap-0 overflow-hidden p-0 sm:top-6 sm:max-w-2xl">
        <form onSubmit={submit} className="flex min-h-0 max-h-[calc(100dvh-2rem)] flex-col overflow-hidden">
          <DialogHeader className="shrink-0 border-b border-border/60 px-4 py-4 pr-12">
            <DialogTitle>{t.settings.title}</DialogTitle>
            <DialogDescription>{t.settings.description}</DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4">
            <div className="grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="uiLanguage">{t.settings.language}</Label>
                <Select
                  value={language}
                  onValueChange={(value) => {
                    if (value === "en" || value === "zh") setLanguage(value)
                  }}
                >
                  <SelectTrigger id="uiLanguage">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {LANGUAGE_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="cookie">{t.settings.cookie}</Label>
                <Textarea
                  id="cookie"
                  value={cookieValue}
                  onFocus={(event) => {
                    if (!cookieDirty && settings.cookie === SAVED_COOKIE_SENTINEL) {
                      event.currentTarget.select()
                    }
                  }}
                  onChange={(event) => {
                    setCookieDirty(true)
                    setSettings((current) => ({
                      ...current,
                      cookie:
                        current.cookie === SAVED_COOKIE_SENTINEL
                          ? event.target.value.replace(t.settings.savedCookie, "")
                          : event.target.value,
                    }))
                  }}
                  placeholder={t.settings.cookiePlaceholder}
                  className="min-h-28 max-h-[24dvh] overflow-auto font-mono text-xs leading-relaxed"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="proxyPort">{t.settings.proxyPort}</Label>
                <Input
                  id="proxyPort"
                  inputMode="numeric"
                  value={settings.proxyPort}
                  onChange={(event) =>
                    setSettings((current) => ({ ...current, proxyPort: event.target.value }))
                  }
                  placeholder="7890"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="outputDir">{t.settings.outputDir}</Label>
                <Input
                  id="outputDir"
                  value={settings.outputDir}
                  onChange={(event) =>
                    setSettings((current) => ({ ...current, outputDir: event.target.value }))
                  }
                  placeholder={t.settings.outputDirPlaceholder}
                />
                <p className="text-xs text-muted-foreground">{t.settings.outputDirHelp}</p>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="baseUrl">{t.settings.baseUrl}</Label>
                <Input
                  id="baseUrl"
                  value={settings.baseUrl}
                  onChange={(event) =>
                    setSettings((current) => ({ ...current, baseUrl: event.target.value }))
                  }
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="apiKey">{t.settings.apiKey}</Label>
                <div className="relative">
                  <Input
                    id="apiKey"
                    type={showApiKey ? "text" : "password"}
                    value={settings.apiKey}
                    onFocus={(event) => {
                      if (!apiKeyDirty && settings.apiKey === SAVED_API_KEY_MASK) {
                        event.currentTarget.select()
                      }
                    }}
                    onChange={(event) => {
                      setApiKeyDirty(true)
                      setSettings((current) => ({
                        ...current,
                        apiKey: event.target.value.replace(SAVED_API_KEY_MASK, ""),
                      }))
                    }}
                    placeholder={t.settings.apiKeyPlaceholder}
                    className="pr-9"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    className="absolute top-0.5 right-0.5"
                    onClick={() => setShowApiKey((current) => !current)}
                  >
                    {showApiKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    <span className="sr-only">{showApiKey ? t.settings.hideApiKey : t.settings.showApiKey}</span>
                  </Button>
                </div>
              </div>
              <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                <div className="grid gap-2">
                  <Label htmlFor="model">{t.settings.model}</Label>
                  {modelsLoaded && modelOptions.length > 0 ? (
                    <Select
                      value={settings.model}
                      onValueChange={(value) =>
                        setSettings((current) => ({ ...current, model: value || "" }))
                      }
                    >
                      <SelectTrigger id="model">
                        <SelectValue placeholder={t.settings.selectModel} />
                      </SelectTrigger>
                      <SelectContent>
                        {modelOptions.map((model) => (
                          <SelectItem key={model} value={model}>
                            {model}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <Input
                      id="model"
                      value={settings.model}
                      onChange={(event) =>
                        setSettings((current) => ({ ...current, model: event.target.value }))
                      }
                    />
                  )}
                </div>
                <div className="grid gap-2 sm:self-end">
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={fetchModels}
                    disabled={modelsLoading || !settings.baseUrl.trim()}
                  >
                    <RefreshCw className="size-4" />
                    {modelsLoading ? t.settings.loading : t.settings.getModels}
                  </Button>
                </div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="translateConcurrency">{t.settings.translateConcurrency}</Label>
                <Input
                  id="translateConcurrency"
                  inputMode="numeric"
                  value={settings.translateConcurrency}
                  onChange={(event) =>
                    setSettings((current) => ({
                      ...current,
                      translateConcurrency: event.target.value.replace(/[^0-9]/g, ""),
                    }))
                  }
                  placeholder="8"
                />
                <p className="text-xs text-muted-foreground">
                  {t.settings.concurrencyHelp}
                </p>
              </div>

              <div className="grid gap-3 rounded-lg border border-border/60 p-3">
                <div className="grid gap-1">
                  <p className="text-sm font-medium">{t.settings.azureSection}</p>
                  <p className="text-xs text-muted-foreground">{t.settings.azureHelp}</p>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="azureSubscriptionKey">{t.settings.azureSubscriptionKey}</Label>
                  <div className="relative">
                    <Textarea
                      id="azureSubscriptionKey"
                      rows={3}
                      value={settings.azureSubscriptionKey}
                      onChange={(event) => {
                        setAzureKeyDirty(true)
                        setSettings((current) => ({
                          ...current,
                          azureSubscriptionKey: event.target.value,
                        }))
                      }}
                      onKeyDown={(event) => {
                        // Keep Enter as newline; never let it bubble as dialog/form activation.
                        if (event.key === "Enter") event.stopPropagation()
                      }}
                      placeholder={t.settings.azureSubscriptionKeyPlaceholder}
                      className="pr-9 font-mono text-xs"
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      className="absolute top-0.5 right-0.5"
                      onClick={() => setShowAzureKey((current) => !current)}
                    >
                      {showAzureKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                      <span className="sr-only">
                        {showAzureKey ? t.settings.hideApiKey : t.settings.showApiKey}
                      </span>
                    </Button>
                  </div>
                  {!azureKeyDirty && azureKeyCount > 0 ? (
                    <p className="text-xs text-muted-foreground">
                      {t.settings.azureKeyCount.replace("{count}", String(azureKeyCount))}
                    </p>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={validateAzureKeys}
                      disabled={
                        azureKeysValidating
                        || (!settings.azureRegion.trim() && !settings.azureEndpoint.trim())
                        || (!azureKeyDirty && azureKeyCount === 0)
                        || (azureKeyDirty && !settings.azureSubscriptionKey.trim())
                      }
                    >
                      <RefreshCw className={`size-4 ${azureKeysValidating ? "animate-spin" : ""}`} />
                      {azureKeysValidating
                        ? t.settings.azureValidatingKeys
                        : t.settings.azureValidateKeys}
                    </Button>
                    <p className="text-xs text-muted-foreground">{t.settings.azureValidateKeysHelp}</p>
                  </div>
                  {azureKeyValidationMessage ? (
                    <pre
                      className={
                        azureKeyValidationOk
                          ? "whitespace-pre-wrap rounded-md border border-emerald-500/30 bg-emerald-950/30 px-3 py-2 text-xs text-emerald-200"
                          : "whitespace-pre-wrap rounded-md border border-red-500/30 bg-red-950/30 px-3 py-2 text-xs text-red-200"
                      }
                    >
                      {azureKeyValidationMessage}
                    </pre>
                  ) : null}
                </div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div className="grid gap-2">
                    <Label htmlFor="azureRegion">{t.settings.azureRegion}</Label>
                    <Input
                      id="azureRegion"
                      value={settings.azureRegion}
                      onChange={(event) =>
                        setSettings((current) => ({ ...current, azureRegion: event.target.value }))
                      }
                      placeholder="eastasia"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="azureLocale">{t.settings.azureLocale}</Label>
                    <Input
                      id="azureLocale"
                      value={settings.azureLocale}
                      onChange={(event) =>
                        setSettings((current) => ({ ...current, azureLocale: event.target.value }))
                      }
                      placeholder="zh-CN"
                    />
                  </div>
                </div>
                <div className="grid gap-2">
                  <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
                    <div className="grid min-w-0 gap-2">
                      <Label htmlFor="azureVoice">{t.settings.azureVoice}</Label>
                      {azureVoicesLoaded && azureVoiceOptions.length > 0 ? (
                        <select
                          id="azureVoice"
                          value={settings.azureVoice}
                          onChange={(event) =>
                            setSettings((current) => ({
                              ...current,
                              azureVoice: event.target.value,
                            }))
                          }
                          className="flex h-8 w-full rounded-lg border border-input bg-transparent px-2.5 py-1 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                        >
                          {azureVoiceOptions.map((voice) => (
                            <option key={voice} value={voice}>
                              {voice}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <Input
                          id="azureVoice"
                          value={settings.azureVoice}
                          onChange={(event) =>
                            setSettings((current) => ({
                              ...current,
                              azureVoice: event.target.value,
                            }))
                          }
                          placeholder="zh-CN-XiaoxiaoNeural"
                        />
                      )}
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={fetchAzureVoices}
                      disabled={azureVoicesLoading || !settings.azureRegion.trim()}
                    >
                      <RefreshCw className="size-4" />
                      {azureVoicesLoading ? t.settings.azureLoadingVoices : t.settings.azureGetVoices}
                    </Button>
                  </div>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="azureEndpoint">{t.settings.azureEndpoint}</Label>
                  <Input
                    id="azureEndpoint"
                    value={settings.azureEndpoint}
                    onChange={(event) =>
                      setSettings((current) => ({ ...current, azureEndpoint: event.target.value }))
                    }
                    placeholder={t.settings.azureEndpointPlaceholder}
                  />
                </div>
                <div className="grid gap-2 sm:grid-cols-3">
                  <div className="grid gap-2 sm:col-span-1">
                    <Label htmlFor="azureOutputFormat">{t.settings.azureOutputFormat}</Label>
                    <Input
                      id="azureOutputFormat"
                      value={settings.azureOutputFormat}
                      onChange={(event) =>
                        setSettings((current) => ({
                          ...current,
                          azureOutputFormat: event.target.value,
                        }))
                      }
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="azureSpeechRate">{t.settings.azureSpeechRate}</Label>
                    <Input
                      id="azureSpeechRate"
                      value={settings.azureSpeechRate}
                      onChange={(event) =>
                        setSettings((current) => ({
                          ...current,
                          azureSpeechRate: event.target.value,
                        }))
                      }
                      placeholder="0"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="azureConcurrency">{t.settings.azureConcurrency}</Label>
                    <Input
                      id="azureConcurrency"
                      inputMode="numeric"
                      value={settings.azureConcurrency}
                      onChange={(event) =>
                        setSettings((current) => ({
                          ...current,
                          azureConcurrency: event.target.value.replace(/[^0-9]/g, ""),
                        }))
                      }
                      placeholder="4"
                    />
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">
                  {t.settings.azureConcurrencyHelp}
                </p>
              </div>

              <BilibiliSettingsPanel active={open} />

              {saveResults.length > 0 ? (
                <div
                  data-testid="settings-save-results"
                  className="rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-sm"
                  aria-live="polite"
                >
                  <p className="font-medium">{t.settings.saveResultsTitle}</p>
                  <ul className="mt-1 space-y-1">
                    {saveResults.map((result) => (
                      <li
                        key={result.section}
                        className={result.status === "failed" ? "text-red-300" : "text-muted-foreground"}
                      >
                        {saveSectionLabels[result.section]}: {saveResultText(result)}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {visibleMessage ? <p className="text-sm text-muted-foreground">{visibleMessage}</p> : null}
            </div>
          </div>
          <DialogFooter className="mx-0 mb-0 shrink-0 rounded-none border-t bg-muted/50 px-4 py-3 sm:justify-end">
            <Button type="submit" disabled={saving}>
              {saving ? t.settings.saving : t.settings.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
