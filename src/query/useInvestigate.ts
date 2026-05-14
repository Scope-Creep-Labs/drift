import { useCallback, useRef, useState } from 'react'
import { getAdapter } from '../adapters'
import { dataRegistry } from '../data/registry'
import { useInvestigationStore } from '../state/investigationStore'
import type { PromptRequest } from '../types/prompt'

export type InvestigateState = {
  isStreaming: boolean
  error: string | null
}

export function useInvestigate() {
  const [state, setState] = useState<InvestigateState>({ isStreaming: false, error: null })
  const abortRef = useRef<AbortController | null>(null)

  const {
    beginStream,
    appendThinking,
    appendNarrative,
    upsertToolCall,
    finishToolCall,
    addBlock,
    setStreamMetadata,
    setStreamError,
    finalizeStream,
    abortStream,
  } = useInvestigationStore.getState()

  const submit = useCallback(
    async (req: PromptRequest) => {
      if (state.isStreaming) return
      setState({ isStreaming: true, error: null })

      const ac = new AbortController()
      abortRef.current = ac
      const { investigationId } = beginStream(req.prompt)

      // Pass investigationId so the backend can stitch this turn onto the
      // session history for that investigation (multi-turn conversation,
      // including the propose/apply confirmation pattern).
      const enrichedReq: PromptRequest = {
        ...req,
        context: { ...(req.context ?? {}), investigationId },
      }

      try {
        const adapter = getAdapter()
        for await (const ev of adapter.stream(enrichedReq, ac.signal)) {
          switch (ev.type) {
            case 'thinking':
              appendThinking(ev.text)
              break
            case 'narrative':
              appendNarrative(ev.text)
              break
            case 'tool_call':
              upsertToolCall(ev.id, ev.name, ev.args)
              break
            case 'tool_result':
              finishToolCall(ev.id, ev.summary, ev.is_error)
              break
            case 'data':
              dataRegistry.put(ev.ref, ev.traces)
              break
            case 'block':
              addBlock(ev.block)
              break
            case 'metadata':
              setStreamMetadata(ev.metadata)
              break
            case 'error':
              setStreamError(ev.error)
              setState({ isStreaming: false, error: ev.error })
              break
            case 'done':
              finalizeStream()
              setState({ isStreaming: false, error: null })
              return
            case 'start':
              break
          }
        }
        // Stream ended without an explicit 'done' event; finalize anyway.
        finalizeStream()
        setState({ isStreaming: false, error: null })
      } catch (e) {
        if ((e as Error).name === 'AbortError') {
          abortStream()
          setState({ isStreaming: false, error: null })
          return
        }
        const msg = (e as Error).message ?? String(e)
        setStreamError(msg)
        finalizeStream()
        setState({ isStreaming: false, error: msg })
      }
    },
    [
      state.isStreaming,
      beginStream,
      appendThinking,
      appendNarrative,
      upsertToolCall,
      finishToolCall,
      addBlock,
      setStreamMetadata,
      setStreamError,
      finalizeStream,
      abortStream,
    ],
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { submit, cancel, ...state }
}
