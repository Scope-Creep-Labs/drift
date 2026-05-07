import { useState } from 'react'
import { Box, Chip, Collapse, IconButton, Stack, Typography } from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import CheckIcon from '@mui/icons-material/Check'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline'
import HourglassTopIcon from '@mui/icons-material/HourglassTop'
import PsychologyAltIcon from '@mui/icons-material/PsychologyAlt'
import type { TraceEntry } from '../types/agentEvents'

const TOOL_COLOR: Record<string, string> = {
  pending: '#7c9cff',
  done: '#5ad1c1',
  error: '#ff6b6b',
}

export function Scratchpad({
  trace,
  defaultOpen = false,
  isStreaming = false,
}: {
  trace: TraceEntry[]
  defaultOpen?: boolean
  isStreaming?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const toolCount = trace.filter((t) => t.kind === 'tool_call').length
  const pendingCount = trace.filter((t) => t.kind === 'tool_call' && t.status === 'pending').length

  if (trace.length === 0 && !isStreaming) return null

  return (
    <Box
      sx={{
        border: 1,
        borderColor: 'divider',
        borderRadius: 1.5,
        bgcolor: 'rgba(255,255,255,0.02)',
        mb: 2,
        overflow: 'hidden',
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        spacing={1}
        sx={{
          px: 1.5,
          py: 0.8,
          cursor: 'pointer',
          '&:hover': { bgcolor: 'rgba(255,255,255,0.04)' },
        }}
        onClick={() => setOpen(!open)}
      >
        <PsychologyAltIcon fontSize="inherit" sx={{ fontSize: 14, color: 'text.secondary' }} />
        <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
          {isStreaming
            ? `Investigating · ${toolCount} tool ${toolCount === 1 ? 'call' : 'calls'}${pendingCount > 0 ? ` (${pendingCount} pending)` : ''}`
            : `Reasoning · ${toolCount} tool ${toolCount === 1 ? 'call' : 'calls'}`}
        </Typography>
        {isStreaming && pendingCount > 0 && <PulseDots />}
        <IconButton
          size="small"
          sx={{
            transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
            transition: 'transform 200ms',
          }}
        >
          <ExpandMoreIcon fontSize="inherit" />
        </IconButton>
      </Stack>
      <Collapse in={open} unmountOnExit>
        <Box sx={{ px: 1.8, pb: 1.5, pt: 0.5 }}>
          <Stack spacing={1}>
            {trace.map((t, i) => {
              if (t.kind === 'thinking' || t.kind === 'narrative') {
                return (
                  <Typography
                    key={i}
                    variant="caption"
                    component="div"
                    sx={{
                      fontFamily:
                        t.kind === 'thinking'
                          ? '"JetBrains Mono", monospace'
                          : 'inherit',
                      color: t.kind === 'thinking' ? 'text.secondary' : 'text.primary',
                      whiteSpace: 'pre-wrap',
                      lineHeight: 1.55,
                      fontSize: '0.78rem',
                      opacity: t.kind === 'thinking' ? 0.85 : 1,
                    }}
                  >
                    {t.text}
                  </Typography>
                )
              }
              const StatusIcon =
                t.status === 'done'
                  ? CheckIcon
                  : t.status === 'error'
                    ? ErrorOutlineIcon
                    : HourglassTopIcon
              return (
                <Stack key={i} direction="row" spacing={1} alignItems="center" flexWrap="wrap">
                  <Chip
                    icon={<StatusIcon sx={{ fontSize: 12 }} />}
                    label={t.name}
                    size="small"
                    sx={{
                      height: 22,
                      fontSize: '0.72rem',
                      fontFamily: '"JetBrains Mono", monospace',
                      bgcolor: 'transparent',
                      border: 1,
                      borderColor: TOOL_COLOR[t.status],
                      color: TOOL_COLOR[t.status],
                      '& .MuiChip-icon': { color: 'inherit', ml: 0.6 },
                    }}
                  />
                  {t.summary && (
                    <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.72rem' }}>
                      · {t.summary}
                    </Typography>
                  )}
                </Stack>
              )
            })}
            {isStreaming && trace.length === 0 && (
              <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                Waiting for first event…
              </Typography>
            )}
          </Stack>
        </Box>
      </Collapse>
    </Box>
  )
}

function PulseDots() {
  return (
    <Stack direction="row" spacing={0.4}>
      {[0, 1, 2].map((i) => (
        <Box
          key={i}
          sx={{
            width: 4,
            height: 4,
            borderRadius: '50%',
            bgcolor: 'primary.main',
            animation: 'driftPulse 1.2s infinite ease-in-out',
            animationDelay: `${i * 0.18}s`,
            '@keyframes driftPulse': {
              '0%, 80%, 100%': { opacity: 0.25, transform: 'scale(0.85)' },
              '40%': { opacity: 1, transform: 'scale(1.4)' },
            },
          }}
        />
      ))}
    </Stack>
  )
}
