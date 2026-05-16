import { useEffect, useRef } from 'react'
import { Box, Button, Chip, Stack, Typography } from '@mui/material'
import IosShareIcon from '@mui/icons-material/IosShare'
import CheckBoxOutlinedIcon from '@mui/icons-material/CheckBoxOutlined'
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
  const enterSelectMode = useInvestigationStore((s) => s.enterSelectMode)
  const exitSelectMode = useInvestigationStore((s) => s.exitSelectMode)
  const selectAll = useInvestigationStore((s) => s.selectAllTurnsInActive)
  const { submit, isStreaming } = useInvestigate()
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
          <EmptyState onPick={(text) => submit({ prompt: text })} disabled={isStreaming} />
        )}

        {turns.length > 0 && !selectMode && !showStreaming && (
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 2 }}>
            <Button
              size="small"
              startIcon={<IosShareIcon fontSize="small" />}
              onClick={enterSelectMode}
              sx={{ textTransform: 'none', color: 'text.secondary' }}
            >
              Export report
            </Button>
          </Box>
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
