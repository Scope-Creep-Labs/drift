import { useEffect, useRef } from 'react'
import { Box, Paper, Stack, Typography } from '@mui/material'

export type AutocompleteItem = {
  // Primary text inserted into the prompt (e.g. "home-synology-001").
  value: string
  // Secondary label shown below — context like the device's group or
  // an app's revision count. Optional.
  hint?: string
}

export type AutocompleteKind = 'device' | 'app' | 'group'

const KIND_LABELS: Record<AutocompleteKind, string> = {
  device: 'Devices (@)',
  app: 'Apps (#)',
  group: 'Groups (:)',
}

export function AutocompletePopup({
  items,
  selectedIndex,
  kind,
  filter,
  onPick,
}: {
  items: AutocompleteItem[]
  selectedIndex: number
  kind: AutocompleteKind
  filter: string
  onPick: (item: AutocompleteItem) => void
}) {
  // Auto-scroll the selected row into view when arrow-keying past the
  // visible window.
  const listRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLDivElement>(`[data-index="${selectedIndex}"]`)
    node?.scrollIntoView({ block: 'nearest' })
  }, [selectedIndex])

  if (items.length === 0) return null

  return (
    <Paper
      variant="outlined"
      sx={{
        position: 'absolute',
        bottom: 'calc(100% + 4px)',
        left: 0,
        right: 0,
        maxHeight: 220,
        overflowY: 'auto',
        borderColor: 'divider',
        bgcolor: 'background.paper',
        zIndex: 100,
      }}
    >
      <Box
        sx={{
          px: 1.4,
          py: 0.4,
          borderBottom: 1,
          borderColor: 'divider',
          bgcolor: 'rgba(255,255,255,0.02)',
        }}
      >
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ fontSize: '0.66rem', textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 600 }}
        >
          {KIND_LABELS[kind]}
          {filter && ` · matching "${filter}"`}
        </Typography>
      </Box>
      <Box ref={listRef}>
        {items.map((item, i) => (
          <Box
            key={item.value}
            data-index={i}
            onMouseDown={(e) => {
              // mouseDown rather than onClick so we react BEFORE the
              // textarea sees a blur event (which would close the popup).
              e.preventDefault()
              onPick(item)
            }}
            sx={{
              px: 1.4,
              py: 0.6,
              cursor: 'pointer',
              bgcolor: i === selectedIndex ? 'action.selected' : 'transparent',
              '&:hover': { bgcolor: 'action.hover' },
            }}
          >
            <Stack direction="row" alignItems="baseline" spacing={1}>
              <Typography variant="body2" sx={{ fontSize: '0.85rem', fontWeight: 500 }}>
                {item.value}
              </Typography>
              {item.hint && (
                <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.7rem' }}>
                  {item.hint}
                </Typography>
              )}
            </Stack>
          </Box>
        ))}
      </Box>
    </Paper>
  )
}
