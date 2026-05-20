import { useEffect, useRef } from 'react'
import { Box, Button, Chip, Stack, Typography } from '@mui/material'
import IosShareIcon from '@mui/icons-material/IosShare'
import CheckBoxOutlinedIcon from '@mui/icons-material/CheckBoxOutlined'
import { useAuth, type AuthUser } from '../auth/AuthContext'
import { useActiveInvestigation, useInvestigationStore } from '../state/investigationStore'
import { Turn } from './Turn'
import { SUGGESTED_PROMPTS } from '../data/scenarios'
import { useInvestigate } from '../query/useInvestigate'
import { downloadReport } from '../lib/exportReport'

export function Conversation() {
  const investigation = useActiveInvestigation()
  const streaming = useInvestigationStore((s) => s.streaming)
  const selectMode = useInvestigationStore((s) => s.selectMode)
  const selectedTurnIds = useInvestigationStore((s) => s.selectedTurnIds)
  const exitSelectMode = useInvestigationStore((s) => s.exitSelectMode)
  const selectAll = useInvestigationStore((s) => s.selectAllTurnsInActive)
  const { submit, isStreaming } = useInvestigate()
  const auth = useAuth()
  const user = auth.status === 'authenticated' ? auth.user : undefined
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [investigation?.turns.length, streaming?.trace.length, streaming?.blocks.length])

  const turns = investigation?.turns ?? []
  const showStreaming =
    streaming && (!investigation || streaming.investigationId === investigation.id)
  const selectedTurns = turns.filter((t) => selectedTurnIds.has(t.id))

  const handleExport = () => {
    if (!investigation || selectedTurns.length === 0) return
    downloadReport({
      title: investigation.title,
      turns: selectedTurns,
      exportedAt: new Date(),
    })
    exitSelectMode()
  }

  return (
    <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {selectMode && (
        <Box
          sx={{
            px: 4,
            py: 1.2,
            borderBottom: 1,
            borderColor: 'divider',
            bgcolor: 'rgba(99, 102, 241, 0.05)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <Typography variant="body2" color="text.secondary">
            Select turns to include in the report. Click a turn or its checkbox.
          </Typography>
          <Button size="small" onClick={selectAll} sx={{ textTransform: 'none' }}>
            Select all
          </Button>
        </Box>
      )}

      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', px: 4, py: 4 }}>
        {turns.length === 0 && !showStreaming && (
          <EmptyState onPick={(text) => submit({ prompt: text })} disabled={isStreaming} user={user} />
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

      {selectMode && (
        <Box
          sx={{
            px: 4,
            py: 1.4,
            borderTop: 1,
            borderColor: 'divider',
            bgcolor: 'background.paper',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 2,
          }}
        >
          <Stack direction="row" alignItems="center" spacing={1}>
            <CheckBoxOutlinedIcon fontSize="small" color={selectedTurns.length > 0 ? 'primary' : 'disabled'} />
            <Typography variant="body2" color="text.secondary">
              {selectedTurns.length === 0
                ? 'No turns selected'
                : `${selectedTurns.length} turn${selectedTurns.length === 1 ? '' : 's'} selected`}
            </Typography>
          </Stack>
          <Stack direction="row" spacing={1}>
            <Button size="small" onClick={exitSelectMode} sx={{ textTransform: 'none' }}>
              Cancel
            </Button>
            <Button
              size="small"
              variant="contained"
              disableElevation
              startIcon={<IosShareIcon fontSize="small" />}
              disabled={selectedTurns.length === 0}
              onClick={handleExport}
              sx={{ textTransform: 'none' }}
            >
              Export HTML
            </Button>
          </Stack>
        </Box>
      )}
    </Box>
  )
}

function EmptyState({
  onPick,
  disabled,
  user,
}: {
  onPick: (text: string) => void
  disabled: boolean
  user: AuthUser | undefined
}) {
  const engine = (import.meta.env.VITE_ENGINE ?? 'mock').toString()
  const suggestions =
    engine === 'agent' ? suggestionsForUser(user) : SUGGESTED_PROMPTS.map((p) => p.text)
  const greeting = user?.username ? `Hi ${user.username} — what can I help with?` : 'What can I help with?'
  return (
    <Box sx={{ maxWidth: 720, mx: 'auto', mt: 6 }}>
      <Typography variant="h5" sx={{ fontWeight: 600, mb: 1 }}>
        {greeting}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {engine === 'agent'
          ? 'Ask anything about metrics, logs, alerts, deployments, or the fleet. Drift queries your data, runs analysis, and assembles charts, tables, and recommendations as it works.'
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
        {suggestions.map((text) => (
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
        ))}
      </Stack>
    </Box>
  )
}

// Role-tiered starter prompts. Each list is the four most-useful entry
// points for the role's primary surface. Admin/deploy still cover the
// observability prompts implicitly — once the conversation starts the
// agent can route to anything they're authorized for.
const OBSERVE_SUGGESTIONS = [
  'Which hosts are reporting metrics right now?',
  'Compare CPU usage between my home devices in the last hour. Where is it highest?',
  'Show me recent error logs from nvidia-jetson-002.',
  'Set up an alert for any host with root disk < 10% free.',
]

const DEPLOY_SUGGESTIONS = [
  'Show me current deployment status across the fleet — anything not healthy?',
  'Which deployments are paused or failing? Group by app.',
  'Walk me through deploying a new app from a compose file.',
  'Retry any paused deployments on home-synology-001.',
]

const ADMIN_SUGGESTIONS = [
  "What's the state of the fleet right now? Devices, apps, anything failing.",
  'List users and what device groups they have access to.',
  'Create a new observe user with access to drift_home.',
  'Show me deployments that have been failing for the past day.',
]

function suggestionsForUser(user: AuthUser | undefined): string[] {
  if (!user) return OBSERVE_SUGGESTIONS
  if (user.role === 'admin') return ADMIN_SUGGESTIONS
  if (user.role === 'deploy') return DEPLOY_SUGGESTIONS
  return OBSERVE_SUGGESTIONS
}
