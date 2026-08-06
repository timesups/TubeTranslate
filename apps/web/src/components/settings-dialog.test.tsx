import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { SettingsDialog } from "@/components/settings-dialog"
import { LanguageProvider } from "@/lib/i18n"

const mocks = vi.hoisted(() => ({
  fetch: vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(),
}))

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

const defaultVolcengine = {
  app_id: "app-1",
  access_key: "********",
  has_access_key: true,
  api_key: "********",
  has_api_key: true,
  resource_id: "seed-tts-2.0",
  speaker: "zh_female_shuangkuaisisi_moon_bigtts",
  endpoint: "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
  sample_rate: "24000",
  speech_rate: "0",
  concurrency: "4",
  uid: "youdub-webui",
}

afterEach(() => {
  cleanup()
  window.localStorage.clear()
  vi.unstubAllGlobals()
})

describe("设置分项保存反馈", () => {
  it("前两项成功而最后一项失败时逐项展示结果并回读服务端状态", async () => {
    mocks.fetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const method = init?.method || "GET"

      if (method === "GET" && path === "/api/cookies/youtube") {
        return jsonResponse({ exists: true, size: 128, updated_at: 1, content: "" })
      }
      if (method === "GET" && path === "/api/settings/openai") {
        return jsonResponse({
          base_url: "https://api.openai.com/v1",
          api_key: "********",
          has_api_key: true,
          model: "gpt-4o-mini",
          translate_concurrency: "8",
        })
      }
      if (method === "GET" && path === "/api/settings/ytdlp") {
        return jsonResponse({ proxy_port: "7890" })
      }
      if (method === "GET" && path === "/api/settings/output") {
        return jsonResponse({ output_dir: "D:/YouDubExports" })
      }
      if (method === "GET" && path === "/api/settings/volcengine-tts") {
        return jsonResponse(defaultVolcengine)
      }
      if (method === "GET" && path === "/api/settings/azure-tts") {
        return jsonResponse({
          subscription_key: "********",
          has_subscription_key: true,
          region: "eastasia",
          voice: "zh-CN-XiaoxiaoNeural",
          locale: "zh-CN",
          endpoint: "",
          output_format: "audio-24khz-48kbitrate-mono-mp3",
          speech_rate: "0",
          concurrency: "4",
        })
      }
      if (method === "POST" && path === "/api/cookies/youtube") {
        return jsonResponse({ exists: true, size: 256, updated_at: 2, content: "" })
      }
      if (method === "POST" && path === "/api/settings/openai") {
        return jsonResponse({
          base_url: "https://api.openai.com/v1",
          api_key: "********",
          has_api_key: true,
          model: "gpt-4o-mini",
          translate_concurrency: "8",
        })
      }
      if (method === "POST" && path === "/api/settings/ytdlp") {
        return jsonResponse(
          { detail: "validation failed for cookie-secret and sk-secret" },
          422,
        )
      }
      if (method === "POST" && path === "/api/settings/output") {
        return jsonResponse({ output_dir: "D:/YouDubExports" })
      }
      if (method === "POST" && path === "/api/settings/volcengine-tts") {
        return jsonResponse(defaultVolcengine)
      }
      if (method === "POST" && path === "/api/settings/azure-tts") {
        return jsonResponse({
          subscription_key: "********",
          has_subscription_key: true,
          region: "eastasia",
          voice: "zh-CN-XiaoxiaoNeural",
          locale: "zh-CN",
          endpoint: "",
          output_format: "audio-24khz-48kbitrate-mono-mp3",
          speech_rate: "0",
          concurrency: "4",
        })
      }
      throw new Error(`未预期的请求: ${method} ${path}`)
    })
    vi.stubGlobal("fetch", mocks.fetch)

    const user = userEvent.setup()
    render(
      <LanguageProvider>
        <SettingsDialog />
      </LanguageProvider>,
    )

    await user.click(screen.getByRole("button", { name: "设置" }))
    const cookieInput = await screen.findByLabelText("YouTube Cookie")
    const apiKeyInput = screen.getByLabelText("OpenAI API Key")
    const proxyInput = screen.getByLabelText("yt-dlp 代理端口")
    await waitFor(() => expect(proxyInput).toHaveValue("7890"))

    await user.clear(cookieInput)
    await user.type(cookieInput, "cookie-secret")
    await user.clear(apiKeyInput)
    await user.type(apiKeyInput, "sk-secret")
    await user.clear(proxyInput)
    await user.type(proxyInput, "70000")
    await user.click(screen.getByRole("button", { name: "保存设置" }))

    const results = await screen.findByTestId("settings-save-results")
    expect(results).toHaveTextContent("YouTube Cookie: 保存成功")
    expect(results).toHaveTextContent("OpenAI 设置: 保存成功")
    expect(results).toHaveTextContent("yt-dlp 设置: 保存失败 (HTTP 422)")
    expect(results).toHaveTextContent("输出设置: 保存成功")
    expect(results).toHaveTextContent("火山引擎 TTS 设置: 保存成功")
    expect(results).toHaveTextContent("Azure TTS 设置: 保存成功")
    expect(results).not.toHaveTextContent("cookie-secret")
    expect(results).not.toHaveTextContent("sk-secret")

    await waitFor(() => expect(proxyInput).toHaveValue("7890"))
    expect(cookieInput).toHaveValue("******** 已保存 YouTube Cookie ********")
    expect(apiKeyInput).toHaveValue("********")

    const postPaths = mocks.fetch.mock.calls
      .filter(([, init]) => init?.method === "POST")
      .map(([input]) => String(input))
    expect(postPaths).toEqual([
      "/api/cookies/youtube",
      "/api/settings/openai",
      "/api/settings/ytdlp",
      "/api/settings/output",
      "/api/settings/volcengine-tts",
      "/api/settings/azure-tts",
    ])

    for (const path of [
      "/api/cookies/youtube",
      "/api/settings/openai",
      "/api/settings/ytdlp",
      "/api/settings/output",
      "/api/settings/volcengine-tts",
      "/api/settings/azure-tts",
    ]) {
      const getCount = mocks.fetch.mock.calls.filter(
        ([input, init]) => String(input) === path && (init?.method || "GET") === "GET",
      ).length
      expect(getCount).toBe(2)
    }

    const sensitiveInputValues = Array.from(
      document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea"),
      (element) => element.value,
    ).join("\n")
    expect(sensitiveInputValues).not.toContain("cookie-secret")
    expect(sensitiveInputValues).not.toContain("sk-secret")
  })
})
