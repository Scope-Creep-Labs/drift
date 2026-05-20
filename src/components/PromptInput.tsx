import { useState, useCallback, useRef, ChangeEvent, KeyboardEvent } from 'react'
import { Box, Button, IconButton, Paper, TextField, Tooltip, CircularProgress, Typography } from '@mui/material'
import SendIcon from '@mui/icons-material/Send'
import StopIcon from '@mui/icons-material/Stop'
import IosShareIcon from '@mui/icons-material/IosShare'
import { useInvestigate } from '../query/useInvestigate'
import { useActiveInvestigation, useInvestigationStore } from '../state/investigationStore'
import { costForUsage, formatUsd, sumUsage, totalTokens } from '../lib/pricing'

export function PromptInput() {
  const [value, setValue] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  const { submit, cancel, isStreaming, error } = useInvestigate()
  const investigation = useActiveInvestigation()
  const streaming = useInvestigationStore((s) => s.streaming)
  const selectMode = useInvestigationStore((s) => s.selectMode)
  const enterSelectMode = useInvestigationStore((s) => s.enterSelectMode)

  const turns = investigation?.turns ?? []
  const canExportReport = turns.length > 0 && !selectMode && !isStreaming
  const liveTurn =
    streaming && (!investigation || streaming.investigationId === investigation.id)
      ? [{ metadata: streaming.metadata }]
      : []
  const aggregate = sumUsage([...turns, ...liveTurn])
  const totalTok = totalTokens(aggregate)
  const totalCost = costForUsage(aggregate)

  // Prompt history navigation (↑/↓). -1 means "live draft", 0 = newest historical.
  const history = turns.map((t) => t.prompt)
  const historyIdx = useRef(-1)
  const draft = useRef('')

  const setCaretToEnd = () => {
    const ta = inputRef.current as HTMLTextAreaElement | null
    if (!ta) return
    requestAnimationFrame(() => {
      const len = ta.value.length
      ta.selectionStart = ta.selectionEnd = len
    })
  }

  const onSubmit = useCallback(() => {
    const prompt = value.trim()
    if (!prompt || isStreaming) return
    submit({ prompt })
    setValue('')
    historyIdx.current = -1
    draft.current = ''
  }, [value, isStreaming, submit])

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const ta = inputRef.current as HTMLTextAreaElement | null
    const caret = ta?.selectionStart ?? value.length

    if (e.key === 'ArrowUp' && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey) {
      const onFirstLine = value.slice(0, caret).indexOf('\n') === -1
      if (!onFirstLine || history.length === 0) return
      e.preventDefault()
      if (historyIdx.current === -1) draft.current = value
      const next = Math.min(historyIdx.current + 1, history.length - 1)
      historyIdx.current = next
      setValue(history[history.length - 1 - next])
      setCaretToEnd()
      return
    }

    if (e.key === 'ArrowDown' && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey) {
      const onLastLine = value.slice(caret).indexOf('\n') === -1
      if (!onLastLine || historyIdx.current === -1) return
      e.preventDefault()
      const next = historyIdx.current - 1
      historyIdx.current = next
      setValue(next === -1 ? draft.current : history[history.length - 1 - next])
      setCaretToEnd()
      return
    }

    if (e.key !== 'Enter' || e.shiftKey || e.altKey) return
    e.preventDefault()
    if (e.metaKey || e.ctrlKey) {
      // Insert a newline at the cursor.
      const start = ta?.selectionStart ?? value.length
      const end = ta?.selectionEnd ?? value.length
      const next = value.slice(0, start) + '\n' + value.slice(end)
      setValue(next)
      requestAnimationFrame(() => {
        if (!ta) return
        ta.selectionStart = ta.selectionEnd = start + 1
      })
      return
    }
    onSubmit()
  }

  const onChange = (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    setValue(e.target.value)
    // Once the user types/edits, drop history navigation state — the buffer
    // is now their own working draft, not a recalled entry.
    historyIdx.current = -1
  }

  return (
    <Box sx={{ position: 'sticky', bottom: 0, pt: 2, pb: 2.5, bgcolor: 'background.default' }}>
      {(totalTok > 0 || canExportReport) && (
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 0.6, gap: 1 }}>
          {totalTok > 0 ? (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ textTransform: 'lowercase' }}
              title={`input ${aggregate.input_tokens?.toLocaleString()} · output ${aggregate.output_tokens?.toLocaleString()} · cache hit ${aggregate.cache_read_input_tokens?.toLocaleString()} · cache write ${aggregate.cache_creation_input_tokens?.toLocaleString()}`}
            >
              session: {totalTok.toLocaleString()} tok · {formatUsd(totalCost)}
            </Typography>
          ) : (
            <span />
          )}
          {canExportReport && (
            <Button
              size="small"
              startIcon={<IosShareIcon fontSize="small" />}
              onClick={enterSelectMode}
              sx={{ textTransform: 'none', color: 'text.secondary', py: 0, minHeight: 0 }}
            >
              Export report
            </Button>
          )}
        </Box>
      )}
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
          onChange={onChange}
          onKeyDown={onKeyDown}
          disabled={isStreaming}
          inputRef={inputRef}
          // Stop Firefox/Chrome/password-managers from trying to autofill this
          // multiline prompt as an email/login field.
          inputProps={{
            autoComplete: 'off',
            autoCorrect: 'off',
            autoCapitalize: 'off',
            spellCheck: true,
            name: 'drift-prompt',
            'data-form-type': 'other',
            'data-1p-ignore': 'true',
            'data-lpignore': 'true',
          }}
          InputProps={{ disableUnderline: true, sx: { fontSize: '0.95rem', px: 1 } }}
        />
        {isStreaming ? (
          <Tooltip title="Stop">
            <IconButton color="error" onClick={cancel} sx={{ alignSelf: 'flex-end' }}>
              <StopIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : (
          <Tooltip title="Send (Enter) · Newline (⌘/Ctrl + Enter)">
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
          <Box sx={{ fontSize: '0.78rem' }}>Working…</Box>
        </Box>
      )}
      {error && (
        <Box sx={{ mt: 1, color: 'error.main', fontSize: '0.8rem' }}>{error}</Box>
      )}
    </Box>
  )
}
