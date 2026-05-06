/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ENGINE?: string
  readonly VITE_LANGFLOW_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
