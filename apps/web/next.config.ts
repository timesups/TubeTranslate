import type { NextConfig } from "next";

function apiProxyTarget() {
  const configured =
    process.env.NEXT_SERVER_API_BASE_URL ||
    "http://127.0.0.1:8000";
  return configured.replace(/\/$/, "");
}

const nextConfig: NextConfig = {
  allowedDevOrigins: ["172.27.2.90", "100.94.222.54"],
  experimental: {
    // Match backend LOCAL_UPLOAD_MAX_BYTES (default 4 GiB).
    // Without this, Next.js proxies truncate bodies over 10MB and uploads fail with ECONNRESET.
    proxyClientMaxBodySize: "4gb",
    // Avoid Turbopack persistent-cache restore panics ("Every task must have a task type").
    turbopackFileSystemCacheForDev: false,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget()}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
