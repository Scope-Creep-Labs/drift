import { useState, useCallback, useMemo, useRef, ChangeEvent, KeyboardEvent } from 'react'
import { Box, Button, IconButton, Paper, TextField, Tooltip, CircularProgress, Typography } from '@mui/material'
import SendIcon from '@mui/icons-material/Send'
import StopIcon from '@mui/icons-material/Stop'
import IosShareIcon from '@mui/icons-material/IosShare'
import { useInvestigate } from '../query/useInvestigate'
import { useActiveInvestigation, useInvestigationStore } from '../state/investigationStore'
import { useFleetStore } from '../state/fleetStore'
import { costForUsage, formatUsd, sumUsage, totalTokens } from '../lib/pricing'
import { AutocompletePopup, type AutocompleteItem, type AutocompleteKind } from './AutocompletePopup'

// Trigger char → which list to search.
const TRIGGERS: Record<string, AutocompleteKind> = {
  '@': 'device',
  '#': 'app',
  ':': 'group',
}

// Extract the active autocomplete token from text + caret position.
// Looks backward from the caret to the previous whitespace; the resulting
// "word" must start with a trigger char and contain only sane name
// characters (so a stray `:` followed by a space or punctuation doesn't
// keep the popup open).
function getActiveTrigger(text: string, caret: number): { kind: AutocompleteKind; filter: string; start: number } | null {
  // Walk backwards from caret to find the token-start position.
  let i = caret
  while (i > 0 && /[A-Za-z0-9_\-./@#:]/.test(text[i - 1])) i--
  // First char of the token must be a trigger; preceding char (if any)
  // must be whitespace or start-of-string so e.g. an email's "@" doesn't
  // trip the popup.
  const trigger = text[i]
  const kind = TRIGGERS[trigger]
  if (!kind) return null
  if (i > 0 && !/\s/.test(text[i - 1])) return null
  const word = text.slice(i, caret)
  // Reject if the token contains another trigger char past position 0
  // (avoids "@home@" double-prompt weirdness).
  if (word.slice(1).match(/[@#:]/)) return null
  return { kind, filter: word.slice(1).toLowerCase(), start: i }
}

export function PromptInput() {
  const [value, setValue] = useState('')
  const [caretPos, setCaretPos] = useState(0)
  const [acIndex, setAcIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const { submit, cancel, isStreaming, error } = useInvestigate()
  const investigation = useActiveInvestigation()
  const streaming = useInvestigationStore((s) => s.streaming)
  const selectMode = useInvestigationStore((s) => s.selectMode)
  const enterSelectMode = useInvestigationStore((s) => s.enterSelectMode)
  const devices = useFleetStore((s) => s.devices)
  const apps = useFleetStore((s) => s.apps)
  const groups = useFleetStore((s) => s.groups)

  // Active autocomplete state derived from the live caret + value.
  // Memoized so we don't recompute on unrelated re-renders.
  const ac = useMemo(() => {
    const trig = getActiveTrigger(value, caretPos)
    if (!trig) return null
    const filter = trig.filter
    let items: AutocompleteItem[] = []
    if (trig.kind === 'device') {
      items = devices
        .filter((d) => d.name.toLowerCase().includes(filter))
        .slice(0, 8)
        .map((d) => ({ value: d.name, hint: d.group_id ? `· ${d.group_id}` : undefined }))
    } else if (trig.kind === 'app') {
      items = apps
        .filter((a) => a.name.toLowerCase().includes(filter))
        .slice(0, 8)
        .map((a) => ({ value: a.name }))
    } else if (trig.kind === 'group') {
      items = groups
        .filter((g) => g.toLowerCase().includes(filter))
        .slice(0, 8)
        .map((g) => {
          const count = devices.filter((d) => d.group_id === g).length
          return { value: g, hint: `${count} device${count === 1 ? '' : 's'}` }
        })
    }
    if (items.length === 0) return null
    return { ...trig, items }
  }, [value, caretPos, devices, apps, groups])

  // Clamp the selected index whenever the items list shrinks.
  const clampedAcIndex = ac ? Math.min(acIndex, ac.items.length - 1) : 0

  const insertAutocomplete = (picked: AutocompleteItem) => {
    if (!ac) return
    // Replace the trigger word with the picked value + a trailing space
    // so the user can keep typing the next word naturally.
    const before = value.slice(0, ac.start)
    const after = value.slice(caretPos)
    const insertion = picked.value + ' '
    const next = before + insertion + after
    setValue(next)
    const newCaret = (before + insertion).length
    requestAnimationFrame(() => {
      const ta = inputRef.current as HTMLTextAreaElement | null
      if (!ta) return
      ta.selectionStart = ta.selectionEnd = newCaret
      setCaretPos(newCaret)
    })
    setAcIndex(0)
  }

  const turns = investigation?.turns ?? []
  const canExportReport = turns.length > 0 && !selectMode && !isStreaming
  const liveTurn =
    streaming && (!investigation || streaming.investigationId === investigation.id)
      ? [{ metadata: streaming.metadata }]
      : []
  const aggregate = sumUsage([...turns, ...liveTurn])
  const totalTok = totalTokens(aggregate)
  // Per-turn cost using each turn's actual model. costForUsage(aggregate)
  // would default to DEFAULT_MODEL (claude-opus-4-7) for any session
  // that used a different model, which over-prices anything cheaper by
  // up to 50x (a non-Opus session of gpt-5.4-mini gets billed as Opus).
  // Pricing each turn against its own metadata.engine produces the
  // correct number even for mixed-model sessions.
  const totalCost = [...turns, ...liveTurn].reduce((sum, t) => {
    const u = t.metadata?.usage
    if (!u) return sum
    return sum + costForUsage(u, t.metadata?.engine ?? undefined)
  }, 0)

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

    // When the autocomplete popup is open, it steals arrow / Enter /
    // Tab / Escape so the operator can navigate + insert. History
    // recall (↑/↓ on first/last line) and submit-on-Enter only apply
    // when the popup is NOT showing.
    if (ac) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setAcIndex((i) => Math.min(i + 1, ac.items.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setAcIndex((i) => Math.max(i - 1, 0))
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        insertAutocomplete(ac.items[clampedAcIndex])
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        // Inserting an out-of-list marker would be too magical; the
        // operator probably wants to keep typing freely. Just hide the
        // popup by jumping the caret one char back (forcing ac→null
        // would require a separate "dismissed" flag; easier to nudge
        // the trigger out of the active token by inserting a no-op).
        // Simpler: do nothing; the next keystroke that breaks the
        // trigger pattern will dismiss it organically.
        return
      }
    }

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
    const ta = e.target as HTMLTextAreaElement
    setValue(ta.value)
    // Track caret so getActiveTrigger has fresh position on the very
    // first keystroke that introduces a `@`/`#`/`:`.
    setCaretPos(ta.selectionStart ?? ta.value.length)
    setAcIndex(0)
    // Once the user types/edits, drop history navigation state — the
    // buffer is now their own working draft, not a recalled entry.
    historyIdx.current = -1
  }

  // Selection changes (arrow keys, mouse-click in textarea) don't fire
  // onChange. Track them so autocomplete keeps up when the operator
  // clicks into the middle of an existing prompt.
  const onSelect = () => {
    const ta = inputRef.current as HTMLTextAreaElement | null
    if (!ta) return
    setCaretPos(ta.selectionStart ?? ta.value.length)
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
          position: 'relative',  // anchor for the absolutely-positioned popup
        }}
      >
        {ac && (
          <AutocompletePopup
            items={ac.items}
            selectedIndex={clampedAcIndex}
            kind={ac.kind}
            filter={ac.filter}
            onPick={insertAutocomplete}
          />
        )}
        <TextField
          multiline
          minRows={1}
          maxRows={6}
          fullWidth
          variant="standard"
          placeholder="Ask about telemetry, anomalies, regressions, or optimization. Type @device, #app, :group to autocomplete."
          value={value}
          onChange={onChange}
          onKeyDown={onKeyDown}
          onSelect={onSelect}
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
