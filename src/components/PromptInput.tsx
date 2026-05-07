import { useState, useCallback, KeyboardEvent } from 'react'
import { Box, IconButton, Paper, TextField, Tooltip, CircularProgress } from '@mui/material'
import SendIcon from '@mui/icons-material/Send'
import StopIcon from '@mui/icons-material/Stop'
import { useInvestigate } from '../query/useInvestigate'

export function PromptInput() {
  const [value, setValue] = useState('')
  const { submit, cancel, isStreaming, error } = useInvestigate()

  const onSubmit = useCallback(() => {
    const prompt = value.trim()
    if (!prompt || isStreaming) return
    submit({ prompt })
    setValue('')
  }, [value, isStreaming, submit])

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      onSubmit()
    }
  }

  return (
    <Box sx={{ position: 'sticky', bottom: 0, pt: 2, pb: 2.5, bgcolor: 'background.default' }}>
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
          disabled={isStreaming}
          InputProps={{ disableUnderline: true, sx: { fontSize: '0.95rem', px: 1 } }}
        />
        {isStreaming ? (
          <Tooltip title="Stop">
            <IconButton color="error" onClick={cancel} sx={{ alignSelf: 'flex-end' }}>
              <StopIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : (
          <Tooltip title="Send (⌘/Ctrl + Enter)">
            <span>
              <IconButton
                color="primary"
                onClick={onSubmit}
                disabled={!value.trim()}
                sx={{ alignSelf: 'flex-end' }}
              >
                <SendIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        )}
      </Paper>
      {isStreaming && (
        <Box sx={{ mt: 1, display: 'flex', alignItems: 'center', gap: 1, color: 'text.secondary' }}>
          <CircularProgress size={12} />
          <Box sx={{ fontSize: '0.78rem' }}>Investigating…</Box>
        </Box>
      )}
      {error && (
        <Box sx={{ mt: 1, color: 'error.main', fontSize: '0.8rem' }}>{error}</Box>
      )}
    </Box>
  )
}
