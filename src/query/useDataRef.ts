import { useQuery } from '@tanstack/react-query'
import { dataRegistry } from '../data/registry'
import { runScenario } from '../data/scenarios'

export function useDataRef<T = unknown>(uri: string | undefined, contextPrompt?: string) {
  return useQuery<T>({
    queryKey: ['dataRef', uri],
    enabled: !!uri,
    queryFn: async () => {
      if (!uri) throw new Error('no uri')
      if (!dataRegistry.has(uri) && contextPrompt) {
        runScenario(contextPrompt)
      }
      return (await dataRegistry.resolve(uri)) as T
    },
  })
}
