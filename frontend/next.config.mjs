/** @type {import('next').NextConfig} */

// The backend base URL. In Compose this points at the api service; locally it
// defaults to the FastAPI dev server. All browser calls go through the /api/*
// proxy below so the frontend is always same-origin (no CORS handling needed).
const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle so the Docker image stays small.
  output: "standalone",
  // Allow the dev server to accept requests proxied from other hosts (e.g. a
  // remote browser); production (next start) is unaffected.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${backendUrl}/:path*`,
      },
    ];
  },
};

export default nextConfig;
