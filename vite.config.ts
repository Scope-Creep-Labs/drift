import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const agentTarget = env.VITE_AGENT_DEV_URL || 'http://localhost:8000'
  // Subroute support: set VITE_BASE=/drift/ to serve from https://host/drift/.
  // Must include a trailing slash. Defaults to '/' for root-served deployments and dev.
  const base = env.VITE_BASE || '/'

  return {
    base,
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: agentTarget,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ''),
          // SSE: don't buffer, hold the connection open for long agent runs.
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('Accept', 'text/event-stream')
            })
          },
          // Keep connection open for long streams.
          ws: false,
          timeout: 0,
          proxyTimeout: 0,
        },
      },
    },
  }
})
