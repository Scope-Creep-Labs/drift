/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ENGINE?: string
  readonly VITE_API_BASE?: string
  readonly VITE_AGENT_DEV_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
