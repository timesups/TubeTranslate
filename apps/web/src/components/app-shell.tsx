"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { Clapperboard, ListTodo, Upload } from "lucide-react"

import { AppHeader } from "@/components/app-header"
import { useI18n } from "@/lib/i18n"
import { cn } from "@/lib/utils"

const NAV = [
  { href: "/", icon: ListTodo, labelKey: "navTasks" as const },
  { href: "/publish", icon: Upload, labelKey: "navPublish" as const },
]

export function AppShell({
  children,
  backHref,
}: {
  children: React.ReactNode
  backHref?: string
}) {
  const pathname = usePathname()
  const { t } = useI18n()

  return (
    <main className="page-bg min-h-screen text-foreground">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <AppHeader backHref={backHref} />
        <div className="flex flex-col gap-6 lg:flex-row">
          <aside className="lg:w-52 shrink-0">
            <nav className="flex gap-2 overflow-x-auto lg:flex-col">
              {NAV.map((item) => {
                const active =
                  item.href === "/"
                    ? pathname === "/"
                    : pathname.startsWith(item.href)
                const Icon = item.icon
                const label =
                  item.labelKey === "navTasks" ? t.nav.tasks : t.nav.publish
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={cn(
                      "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors",
                      active
                        ? "border-[#00aeec]/40 bg-[#00aeec]/10 text-foreground"
                        : "border-transparent text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                    )}
                  >
                    <Icon className="size-4" />
                    {label}
                  </Link>
                )
              })}
              <div className="hidden items-center gap-2 px-3 py-2 text-xs text-muted-foreground lg:flex">
                <Clapperboard className="size-3.5" />
                {t.nav.tagline}
              </div>
            </nav>
          </aside>
          <div className="min-w-0 flex-1">{children}</div>
        </div>
      </div>
    </main>
  )
}
