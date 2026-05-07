import { useEffect, useRef } from 'react'
import { Box, Chip, Stack, Typography } from '@mui/material'
import { useActiveInvestigation, useInvestigationStore } from '../state/investigationStore'
import { Turn } from './Turn'
import { SUGGESTED_PROMPTS } from '../data/scenarios'
import { useInvestigate } from '../query/useInvestigate'

export function Conversation() {
  const investigation = useActiveInvestigation()
  const streaming = useInvestigationStore((s) => s.streaming)
  const { submit, isStreaming } = useInvestigate()
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [investigation?.turns.length, streaming?.trace.length, streaming?.blocks.length])

  const turns = investigation?.turns ?? []
  const showStreaming =
    streaming && (!investigation || streaming.investigationId === investigation.id)

  return (
    <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', px: 4, py: 4 }}>
      {turns.length === 0 && !showStreaming && (
        <EmptyState onPick={(text) => submit({ prompt: text })} disabled={isStreaming} />
      )}
      {turns.map((t) => (
        <Turn key={t.id} turn={t} />
      ))}
      {showStreaming && (
        <Turn
          streaming
          turn={{
            id: streaming.turnId,
            prompt: streaming.prompt,
            trace: streaming.trace,
            blocks: streaming.blocks,
            metadata: streaming.metadata,
            error: streaming.error,
            createdAt: streaming.startedAt,
          }}
        />
      )}
      <div ref={bottomRef} />
    </Box>
  )
}

function EmptyState({
  onPick,
  disabled,
}: {
  onPick: (text: string) => void
  disabled: boolean
}) {
  const engine = (import.meta.env.VITE_ENGINE ?? 'mock').toString()
  return (
    <Box sx={{ maxWidth: 720, mx: 'auto', mt: 6 }}>
      <Typography variant="h5" sx={{ fontWeight: 600, mb: 1 }}>
        What do you want to investigate?
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {engine === 'agent'
          ? 'Drift queries your time-series store, runs analysis, and assembles charts, tables, and recommendations as it works. Ask anything about your hosts, containers, or metrics.'
          : 'Drift returns rich responses — markdown, time-series charts, anomaly overlays, tables, timelines — without leaving the prompt loop.'}
      </Typography>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ textTransform: 'uppercase', letterSpacing: 0.4, mb: 1.2, display: 'block' }}
      >
        Try
      </Typography>
      <Stack spacing={1}>
        {(engine === 'agent' ? AGENT_SUGGESTIONS : SUGGESTED_PROMPTS.map((p) => p.text)).map(
          (text) => (
            <Chip
              key={text}
              label={text}
              variant="outlined"
              disabled={disabled}
              onClick={() => onPick(text)}
              sx={{
                justifyContent: 'flex-start',
                py: 2.4,
                borderRadius: 2,
                borderColor: 'divider',
                '& .MuiChip-label': { px: 1.4, fontSize: '0.85rem', whiteSpace: 'normal' },
                cursor: 'pointer',
                '&:hover': { bgcolor: 'action.hover' },
              }}
            />
          ),
        )}
      </Stack>
    </Box>
  )
}

const AGENT_SUGGESTIONS = [
  'Which hosts are reporting metrics right now, and what jobs are scraping?',
  'Compare CPU usage between my hosts over the last hour. Where is it highest?',
  'Which Docker containers are using the most memory right now?',
  'Investigate whether anything looks anomalous on the cloud VM in the last hour.',
  'Find signals that correlate with high CPU on my Pi.',
]
