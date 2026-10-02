import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  // Where the FastAPI backend lives. Overridable so the dev server can target a
  // backend running in Docker instead of on the host.
  const backendUrl = env.BACKEND_URL ?? 'http://localhost:8000'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      // Proxying keeps the browser on a single origin during development, so
      // there is no CORS preflight. Production uses the same path via nginx.
      proxy: {
        '/api': { target: backendUrl, changeOrigin: true },
        '/health': { target: backendUrl, changeOrigin: true },
      },
    },
    preview: {
      port: 4173,
    },
  }
})