import type { ReactNode } from "react"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import PublishPage from "@/app/publish/page"
import { ApiError, type BilibiliJob } from "@/lib/api"
import { LanguageProvider } from "@/lib/i18n"

const mocks = vi.hoisted(() => ({
  getJob: vi.fn<(id: string, options?: RequestInit) => Promise<BilibiliJob>>(),
}))

vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children }: { children: ReactNode }) => <main>{children}</main>,
}))
vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  getBilibiliAuthStatus: async () => ({ logged_in: true, uname: "test" }),
  getBilibiliPartitions: async () => [],
  listBilibiliReady: async () => ({ video_dir: "/staging", items: [{
    id: "clip", name: "clip.mp4", stem: "clip", video_path: "/clip.mp4",
    cover_path: "/clip.jpg", srt_path: "/clip.srt", has_cover: true, has_srt: true, size: 100, ready: true,
  }] }),
  generateBilibiliMeta: async () => ({
    id: "clip", name: "clip.mp4", title: "test title", desc: "description", tag: "test",
    dynamic: "", tid: 229, copyright: 1, cover_path: "/clip.jpg", srt_path: "/clip.srt",
  }),
  publishBilibili: async () => ({ jobs: [{
    id: "job", job_id: "job", status: "queued", progress: 0, message: "queued",
  }] }),
  getBilibiliJob: mocks.getJob,
}))

beforeEach(() => {
  vi.useFakeTimers()
  mocks.getJob.mockReset()
  window.localStorage.clear()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

async function startUpload() {
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(<LanguageProvider><PublishPage /></LanguageProvider>) })
  expect(screen.getByText("clip.mp4")).toBeInTheDocument()
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "生成简介" })) })
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "投稿所选 (1)" })) })
  return view
}

describe("投稿进度轮询", () => {
  it("慢请求不重叠，卸载时取消请求", async () => {
    let resolve!: (job: BilibiliJob) => void
    mocks.getJob.mockImplementation(() => new Promise((done) => { resolve = done }))
    const view = await startUpload()
    expect(mocks.getJob).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(mocks.getJob).toHaveBeenCalledTimes(1)
    const signal = mocks.getJob.mock.calls[0][1]?.signal
    view.unmount()
    expect(signal?.aborted).toBe(true)
    await act(async () => {
      resolve({ id: "job", status: "success", progress: 100, message: "done" })
      await vi.advanceTimersByTimeAsync(6000)
    })
    expect(mocks.getJob).toHaveBeenCalledTimes(1)
  })

  it("断网显示错误，恢复后继续轮询并停止已完成任务", async () => {
    mocks.getJob.mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValue({ id: "job", status: "success", progress: 100, message: "done" })
    await startUpload()
    expect(screen.getByText("network unavailable")).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText("done")).toBeInTheDocument()
    expect(screen.queryByText("network unavailable")).not.toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(mocks.getJob).toHaveBeenCalledTimes(2)
  })

  it.each([401, 404])("HTTP %s 显示错误并停止查询", async (status) => {
    mocks.getJob.mockRejectedValue(new ApiError("job unavailable", status))
    await startUpload()
    expect(screen.getAllByText("job unavailable").length).toBeGreaterThan(0)
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(mocks.getJob).toHaveBeenCalledTimes(1)
  })
})
