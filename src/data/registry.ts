type ResolveFn = () => unknown | Promise<unknown>

const store = new Map<string, unknown>()
const lazyResolvers = new Map<string, ResolveFn>()

export const dataRegistry = {
  put(uri: string, data: unknown): void {
    store.set(uri, data)
  },

  registerLazy(uri: string, fn: ResolveFn): void {
    lazyResolvers.set(uri, fn)
  },

  has(uri: string): boolean {
    return store.has(uri) || lazyResolvers.has(uri)
  },

  get(uri: string): unknown | undefined {
    return store.get(uri)
  },

  async resolve(uri: string): Promise<unknown> {
    if (store.has(uri)) return store.get(uri)
    const fn = lazyResolvers.get(uri)
    if (fn) {
      const value = await fn()
      store.set(uri, value)
      return value
    }
    throw new Error(`dataRef not found: ${uri}`)
  },

  clear(): void {
    store.clear()
    lazyResolvers.clear()
  },
}
