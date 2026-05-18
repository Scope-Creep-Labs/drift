import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from 'react'

// Mirrors UserOut from the backend (drift-agent/app/users/routes.py).
export type AuthUser = {
  id: string
  username: string
  role: 'observe' | 'deploy' | 'admin'
  groups: string[]
}

type AuthState =
  | { status: 'loading' }
  | { status: 'unauthenticated' }
  | { status: 'authenticated'; user: AuthUser }

type AuthValue = AuthState & {
  refresh: () => Promise<void>
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthCtx = createContext<AuthValue | null>(null)

const API_BASE: string =
  import.meta.env.VITE_API_BASE || `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api`

// Predicates layered on top of role. Mirrors UserContext.is_admin / is_deploy
// on the backend — observe < deploy < admin.
export function isAdmin(user: AuthUser | undefined): boolean {
  return user?.role === 'admin'
}
export function isDeploy(user: AuthUser | undefined): boolean {
  return user?.role === 'deploy' || user?.role === 'admin'
}
export function hasGroup(user: AuthUser | undefined, group: string | undefined | null): boolean {
  if (!user || !group) return false
  if (user.role === 'admin') return true
  return user.groups.includes(group)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading' })

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/me`, {
        credentials: 'include',
      })
      if (res.status === 401) {
        setState({ status: 'unauthenticated' })
        return
      }
      if (!res.ok) {
        // Treat any other error as unauthenticated so the user can re-login.
        setState({ status: 'unauthenticated' })
        return
      }
      const user = (await res.json()) as AuthUser
      setState({ status: 'authenticated', user })
    } catch {
      setState({ status: 'unauthenticated' })
    }
  }, [])

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        let msg = res.statusText
        try {
          const j = JSON.parse(text)
          msg = j.detail || msg
        } catch {
          /* not JSON */
        }
        throw new Error(msg)
      }
      const user = (await res.json()) as AuthUser
      setState({ status: 'authenticated', user })
    },
    [],
  )

  const logout = useCallback(async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      })
    } finally {
      setState({ status: 'unauthenticated' })
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  return (
    <AuthCtx.Provider value={{ ...state, refresh, login, logout }}>{children}</AuthCtx.Provider>
  )
}

export function useAuth(): AuthValue {
  const v = useContext(AuthCtx)
  if (!v) throw new Error('useAuth: missing AuthProvider')
  return v
}

// Convenience hook: throws (well, returns null) if not authenticated.
// Components inside <Shell> can safely assume an authenticated user
// because the App-level gate hides Shell otherwise.
export function useAuthedUser(): AuthUser {
  const v = useAuth()
  if (v.status !== 'authenticated') {
    throw new Error('useAuthedUser called from an unauthenticated tree')
  }
  return v.user
}
