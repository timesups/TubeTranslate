export function statusBadgeClass(status?: string): string {
  if (status === "succeeded") return "bg-[#00aeec] text-white border-transparent"
  if (status === "failed") return "bg-[#ff3355]/20 text-[#ff8a9a] border-transparent"
  if (status === "running") return "bg-[#fb7299]/20 text-[#ffb3c9] border-transparent"
  if (status === "paused") return "bg-amber-500/15 text-amber-200 border-transparent"
  if (status === "queued") return "bg-muted text-foreground border-border"
  return "bg-background text-foreground border-border"
}
