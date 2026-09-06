import type { Metadata } from "next";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import { AuthProvider } from "@/lib/auth";
import { LanguageProvider } from "@/lib/i18n";
import "./globals.css";

export const metadata: Metadata = {
  title: "YouDub",
  description: "Minimal YouDub conversion console",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh-CN"
      className={`${GeistSans.variable} ${GeistMono.variable} h-full antialiased dark`}
      suppressHydrationWarning
    >
      <body className="min-h-full flex flex-col">
        <LanguageProvider>
          <AuthProvider>{children}</AuthProvider>
        </LanguageProvider>
      </body>
    </html>
  );
}
