import { useEffect, useRef } from 'react'
import { Box, Chip, Stack, Typography } from '@mui/material'
import { useActiveInvestigation } from '../state/investigationStore'
import { Turn } from './Turn'
import { SUGGESTED_PROMPTS } from '../data/scenarios'
import { usePromptMutation } from '../query/usePromptMutation'

export function Conversation() {
  const investigation = useActiveInvestigation()
  const mutation = usePromptMutation()
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [investigation?.turns.length, mutation.isPending])

  const turns = investigation?.turns ?? []

  return (
    <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', px: 4, py: 4 }}>
      {turns.length === 0 && (
        <EmptyState onPick={(text) => mutation.mutate({ prompt: text })} disabled={mutation.isPending} />
      )}
      {turns.map((t) => (
        <Turn key={t.id} turn={t} />
      ))}
      {mutation.isPending && <PendingIndicator />}
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
  return (
    <Box sx={{ maxWidth: 720, mx: 'auto', mt: 6 }}>
      <Typography variant="h5" sx={{ fontWeight: 600, mb: 1 }}>
        What do you want to investigate?
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Drift returns rich responses — markdown, time-series charts, anomaly overlays, tables,
        timelines — without leaving the prompt loop.
      </Typography>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ textTransform: 'uppercase', letterSpacing: 0.4, mb: 1.2, display: 'block' }}
      >
        Try one of these
      </Typography>
      <Stack spacing={1}>
        {SUGGESTED_PROMPTS.map((p) => (
          <Chip
            key={p.id}
            label={p.text}
            variant="outlined"
            disabled={disabled}
            onClick={() => onPick(p.text)}
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
        ))}
      </Stack>
    </Box>
  )
}

function PendingIndicator() {
  return (
    <Stack direction="row" spacing={1.5} sx={{ mb: 4 }}>
      <Box sx={{ width: 28, height: 28 }} />
      <Box>
        <Stack direction="row" spacing={0.6} sx={{ mt: 1.4 }}>
          {[0, 1, 2].map((i) => (
            <Box
              key={i}
              sx={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                bgcolor: 'primary.main',
                opacity: 0.5,
                animation: 'driftPulse 1.2s infinite ease-in-out',
                animationDelay: `${i * 0.18}s`,
                '@keyframes driftPulse': {
                  '0%, 80%, 100%': { opacity: 0.25, transform: 'scale(0.85)' },
                  '40%': { opacity: 1, transform: 'scale(1.2)' },
                },
              }}
            />
          ))}
        </Stack>
      </Box>
    </Stack>
  )
}
