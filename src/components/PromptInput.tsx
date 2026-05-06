import { useState, useCallback, KeyboardEvent } from 'react'
import { Box, IconButton, Paper, TextField, Tooltip, CircularProgress } from '@mui/material'
import SendIcon from '@mui/icons-material/Send'
import { usePromptMutation } from '../query/usePromptMutation'
import { useInvestigationStore } from '../state/investigationStore'

export function PromptInput() {
  const [value, setValue] = useState('')
  const mutation = usePromptMutation()
  const activeId = useInvestigationStore((s) => s.activeId)
  const create = useInvestigationStore((s) => s.createInvestigation)

  const submit = useCallback(() => {
    const prompt = value.trim()
    if (!prompt || mutation.isPending) return
    if (!activeId) create()
    mutation.mutate(
      { prompt },
      {
        onSuccess: () => setValue(''),
      },
    )
  }, [value, mutation, activeId, create])

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <Box sx={{ position: 'sticky', bottom: 0, pt: 2, pb: 2.5, px: 0, bgcolor: 'background.default' }}>
      <Paper
        variant="outlined"
        sx={{
          display: 'flex',
          alignItems: 'flex-end',
          gap: 1,
          p: 1.2,
          borderColor: 'divider',
          bgcolor: 'background.paper',
        }}
      >
        <TextField
          multiline
          minRows={1}
          maxRows={6}
          fullWidth
          variant="standard"
          placeholder="Ask about telemetry, anomalies, regressions, or optimization…"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={mutation.isPending}
          InputProps={{ disableUnderline: true, sx: { fontSize: '0.95rem', px: 1 } }}
        />
        <Tooltip title="Send (⌘/Ctrl + Enter)">
          <span>
            <IconButton
              color="primary"
              onClick={submit}
              disabled={!value.trim() || mutation.isPending}
              sx={{ alignSelf: 'flex-end' }}
            >
              {mutation.isPending ? <CircularProgress size={18} /> : <SendIcon fontSize="small" />}
            </IconButton>
          </span>
        </Tooltip>
      </Paper>
      {mutation.isError && (
        <Box sx={{ mt: 1, color: 'error.main', fontSize: '0.8rem' }}>
          {(mutation.error as Error).message}
        </Box>
      )}
    </Box>
  )
}
