// Typed client for the Drift Deploy admin endpoints.
import { deployApiBase } from './apiBase'

const DEPLOY_BASE = deployApiBase()

export type App = {
  id: string
  name: string
  created_at: string
}

export type AppRevision = {
  id: string
  app_id: string
  version: number
  bundle_url: string | null
  bundle_sha256: string | null
  created_at: string
}

export type AppRevisionDetail = AppRevision & {
  files: Record<string, string>
}

export type RegistryCredential = {
  id: string
  registry: string
  // Group the credential belongs to. Devices only receive creds whose
  // group_id matches their own at check-in. Same registry can appear
  // once per group with distinct usernames/passwords.
  group_id: string
  username: string
  // Password is never returned by the API — write-only.
  created_at: string
  updated_at: string
}

export type DeploymentTarget = {
  id: string
  device_id: string
  app_id: string
  desired_revision_id: string | null
  current_revision_id: string | null
  status: 'pending' | 'healthy' | 'failed' | 'paused_retries' | 'removing' | 'removed' | string
  attempts: number
  max_retries: number
  last_error: string | null
  updated_at: string
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${DEPLOY_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ''}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export type Device = {
  id: string
  name: string
  status: string
  last_seen: string | null
  agent_version: string | null
  group_id: string | null
  facts: Record<string, unknown> | null
  created_at: string
}

export const deployApi = {
  listDevices: () => api<Device[]>('/devices'),

  listApps: () => api<App[]>('/apps'),

  createApp: (name: string) =>
    api<App>('/apps', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  listRevisions: (appName: string) =>
    api<AppRevision[]>(`/apps/${encodeURIComponent(appName)}/revisions`),

  // Special-case `version === 'latest'` is supported server-side; mirror it
  // here so callers can hand-roll the URL without thinking.
  getRevision: (appName: string, version: number | 'latest') =>
    api<AppRevisionDetail>(
      `/apps/${encodeURIComponent(appName)}/revisions/${version}`,
    ),

  createRevision: (appName: string, files: Record<string, string>) =>
    api<AppRevision>(`/apps/${encodeURIComponent(appName)}/revisions`, {
      method: 'POST',
      body: JSON.stringify({ files }),
    }),

  listDeployments: () => api<DeploymentTarget[]>('/deployments'),

  // Registry credentials. Upsert is PUT (idempotent): every save replaces
  // both username and password because the server never decrypts to
  // compare — operators re-paste the PAT to change anything.
  listRegistryCreds: () => api<RegistryCredential[]>('/registry-creds'),

  upsertRegistryCreds: (
    registry: string,
    group_id: string,
    username: string,
    password: string,
  ) =>
    api<RegistryCredential>('/registry-creds', {
      method: 'PUT',
      body: JSON.stringify({ registry, group_id, username, password }),
    }),

  deleteRegistryCreds: (registry: string, group_id: string) =>
    api<void>(
      `/registry-creds/${encodeURIComponent(registry)}?group_id=${encodeURIComponent(group_id)}`,
      { method: 'DELETE' },
    ),
}
