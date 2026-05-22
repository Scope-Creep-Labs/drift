import { Avatar, Box, Checkbox, Paper, Stack, Tooltip, Typography } from '@mui/material'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import type { Turn as TurnT, StreamingTurn } from '../state/investigationStore'
import { useInvestigationStore } from '../state/investigationStore'
import { BlockRenderer } from './blocks/BlockRenderer'
import { Scratchpad } from './Scratchpad'
import { costForUsage, formatUsd } from '../lib/pricing'

type TurnLike = TurnT | (StreamingTurn & { id: string; createdAt: string })

// Compact local-time label for a turn's createdAt. Hovering the chip
// reveals the absolute timestamp via Tooltip in the caller. We avoid a
// dep like date-fns for the tiny set of cases we actually need.
function formatTurnTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = Date.now()
  const diffSec = Math.max(0, Math.round((now - d.getTime()) / 1000))
  if (diffSec < 45) return 'just now'
  if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`
  // Same calendar day → just the time.
  const sameDay =
    d.getFullYear() === new Date(now).getFullYear() &&
    d.getMonth() === new Date(now).getMonth() &&
    d.getDate() === new Date(now).getDate()
  if (sameDay) {
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  }
  // Yesterday is common enough to label explicitly.
  const yesterday = new Date(now - 86400000)
  if (
    d.getFullYear() === yesterday.getFullYear() &&
    d.getMonth() === yesterday.getMonth() &&
    d.getDate() === yesterday.getDate()
  ) {
    return `Yesterday ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`
  }
  return d.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function Turn({ turn, streaming = false }: { turn: TurnLike; streaming?: boolean }) {
  const selectMode = useInvestigationStore((s) => s.selectMode)
  const isSelected = useInvestigationStore((s) => s.selectedTurnIds.has(turn.id))
  const toggleSelected = useInvestigationStore((s) => s.toggleTurnSelected)
  // Streaming turns can't be selected for export — their final shape isn't
  // committed to history yet.
  const selectable = selectMode && !streaming

  return (
    <Stack
      spacing={2.5}
      sx={{
        mb: 4,
        ...(selectable
          ? {
              border: 1,
              borderColor: isSelected ? 'primary.main' : 'transparent',
              borderRadius: 2,
              p: 1.2,
              cursor: 'pointer',
              bgcolor: isSelected ? 'rgba(99, 102, 241, 0.04)' : 'transparent',
              '&:hover': { borderColor: isSelected ? 'primary.main' : 'divider' },
            }
          : {}),
      }}
      onClick={selectable ? () => toggleSelected(turn.id) : undefined}
    >
      <Stack direction="row" spacing={1.5} alignItems="flex-start">
        {selectable && (
          <Checkbox
            checked={isSelected}
            onChange={(e) => {
              e.stopPropagation()
              toggleSelected(turn.id)
            }}
            onClick={(e) => e.stopPropagation()}
            size="small"
            sx={{ p: 0.4, mt: 0.2 }}
          />
        )}
        <Avatar sx={{ width: 28, height: 28, bgcolor: 'transparent', border: 1, borderColor: 'divider' }}>
          <PersonOutlineIcon fontSize="small" />
        </Avatar>
        <Paper
          variant="outlined"
          sx={{ flex: 1, px: 2, py: 1.4, borderColor: 'divider', bgcolor: 'background.paper' }}
        >
          {turn.createdAt && (
            <Tooltip title={new Date(turn.createdAt).toLocaleString()} placement="top-end">
              <Typography
                variant="caption"
                color="text.disabled"
                sx={{ display: 'block', mb: 0.5, fontSize: '0.66rem' }}
              >
                {formatTurnTime(turn.createdAt)}
              </Typography>
            </Tooltip>
          )}
          <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
            {turn.prompt}
          </Typography>
        </Paper>
      </Stack>

      <Stack direction="row" spacing={1.5} alignItems="flex-start">
        <Avatar
          sx={{
            width: 28,
            height: 28,
            bgcolor: 'transparent',
            border: 1,
            borderColor: 'primary.main',
            color: 'primary.main',
          }}
        >
          <AutoAwesomeIcon fontSize="small" />
        </Avatar>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          {turn.metadata && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', mb: 1, textTransform: 'lowercase' }}
            >
              {turn.metadata.engine ?? 'agent'}
              {(() => {
                const u = turn.metadata.usage
                if (!u) return null
                const parts: string[] = []
                if (u.input_tokens) parts.push(`in ${u.input_tokens.toLocaleString()}`)
                if (u.output_tokens) parts.push(`out ${u.output_tokens.toLocaleString()}`)
                if (u.cache_read_input_tokens) parts.push(`cache hit ${u.cache_read_input_tokens.toLocaleString()}`)
                if (u.cache_creation_input_tokens) parts.push(`cache write ${u.cache_creation_input_tokens.toLocaleString()}`)
                const cost = costForUsage(u)
                return (
                  <>
                    {parts.length > 0 && ` · ${parts.join(' · ')} tok`}
                    {cost > 0 && ` · ${formatUsd(cost)}`}
                  </>
                )
              })()}
              {turn.metadata.stop_reason && ` · ${turn.metadata.stop_reason}`}
            </Typography>
          )}

          <Scratchpad trace={turn.trace} defaultOpen={streaming} isStreaming={streaming} />

          <BlockRenderer blocks={turn.blocks} contextPrompt={turn.prompt} />

          {streaming && turn.blocks.length === 0 && <StreamingPlaceholder />}

          {/* Defensive fallback: if a turn finished with zero render blocks
              (agent forgot to call make_markdown for a chat-style reply), surface
              the narrative text so the user isn't staring at an empty turn. */}
          {!streaming && turn.blocks.length === 0 && (() => {
            const narrative = turn.trace
              .filter((t): t is { kind: 'narrative'; text: string } => t.kind === 'narrative')
              .map((t) => t.text)
              .join('')
              .trim()
            if (!narrative) return null
            return (
              <Box
                sx={{
                  mt: 1,
                  p: 1.4,
                  border: 1,
                  borderColor: 'warning.main',
                  borderRadius: 1,
                  bgcolor: 'rgba(255,176,32,0.06)',
                }}
              >
                <Typography variant="caption" color="warning.main" sx={{ display: 'block', mb: 0.6, fontWeight: 600 }}>
                  Unrendered narrative (agent should have used make_markdown):
                </Typography>
                <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                  {narrative}
                </Typography>
              </Box>
            )
          })()}

          {turn.error && (
            <Box
              sx={{
                mt: 2,
                p: 1.4,
                border: 1,
                borderColor: 'error.main',
                borderRadius: 1,
                bgcolor: 'rgba(255,107,107,0.08)',
              }}
            >
              <Typography variant="caption" color="error.main">
                {turn.error}
              </Typography>
            </Box>
          )}
        </Box>
      </Stack>
    </Stack>
  )
}

function StreamingPlaceholder() {
  return (
    <Stack direction="row" spacing={0.6} sx={{ mt: 1.5 }}>
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
  )
}
