import { useMutation } from '@tanstack/react-query'
import { getAdapter } from '../adapters'
import type { PromptRequest, PromptResponse } from '../types/prompt'
import { useInvestigationStore } from '../state/investigationStore'

export function usePromptMutation() {
  const appendTurn = useInvestigationStore((s) => s.appendTurn)

  return useMutation<PromptResponse, Error, PromptRequest>({
    mutationFn: (req) => getAdapter().run(req),
    onSuccess: (response, variables) => {
      appendTurn(variables.prompt, response)
    },
  })
}
