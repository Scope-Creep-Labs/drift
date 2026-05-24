import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const agentTarget = env.VITE_AGENT_DEV_URL || 'http://localhost:8000'
  // `./` emits asset URLs relative to index.html so the SAME build serves
  // at /, /drift/, or any subpath the operator wants. Runtime code reads
  // document.baseURI (set by Vite's injected <base href>) to derive API
  // URLs — see src/lib/apiBase.ts. Override via VITE_BASE=/foo/ only if
  // you need absolute-prefix paths for a CDN.
  const base = env.VITE_BASE || './'

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
