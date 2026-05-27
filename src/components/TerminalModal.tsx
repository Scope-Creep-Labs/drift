import { useEffect, useRef, useState } from 'react'
import {
  Box,
  Chip,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import TerminalIcon from '@mui/icons-material/Terminal'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { deployApiBase, deployWsBase } from '../lib/apiBase'

const DEPLOY_BASE = deployApiBase()

type Phase = 'creating' | 'waiting' | 'connected' | 'closed' | 'error'

export function TerminalModal({
  open,
  deviceName,
  onClose,
}: {
  open: boolean
  deviceName: string
  onClose: () => void
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const termRef = useRef<Terminal | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [phase, setPhase] = useState<Phase>('creating')
  const [errMsg, setErrMsg] = useState<string | null>(null)
  // Seconds elapsed since the WS opened in 'waiting' state. The edge
  // agent polls every POLL_INTERVAL (default 30s), so worst-case wait
  // for a session to land is one poll cycle — showing the counter
  // makes that bound concrete for the operator.
  const [waitingSeconds, setWaitingSeconds] = useState(0)

  useEffect(() => {
    if (phase !== 'waiting') {
      setWaitingSeconds(0)
      return
    }
    setWaitingSeconds(0)
    const tick = setInterval(() => setWaitingSeconds((s) => s + 1), 1000)
    return () => clearInterval(tick)
  }, [phase])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setPhase('creating')
    setErrMsg(null)

    const start = async () => {
      // 1) Reserve a session on the CP. The agent picks it up on its
      // next check-in (up to POLL_INTERVAL seconds — default 30s).
      let sessionId: string
      try {
        const res = await fetch(
          `${DEPLOY_BASE}/devices/${encodeURIComponent(deviceName)}/terminal`,
          { method: 'POST', credentials: 'include' },
        )
        if (!res.ok) {
          const body = await res.text().catch(() => '')
          throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
        }
        const { session_id } = (await res.json()) as { session_id: string }
        sessionId = session_id
      } catch (e) {
        if (cancelled) return
        setPhase('error')
        setErrMsg((e as Error).message)
        return
      }
      if (cancelled) return

      // 2) Initialize xterm into the container div.
      const term = new Terminal({
        cursorBlink: true,
        convertEol: true,
        fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
        fontSize: 13,
        theme: {
          background: '#0b0b0d',
          foreground: '#e8e8ea',
          cursor: '#7dd3fc',
          selectionBackground: '#334155',
        },
        scrollback: 5000,
      })
      const fit = new FitAddon()
      term.loadAddon(fit)
      termRef.current = term
      fitRef.current = fit
      if (containerRef.current) {
        term.open(containerRef.current)
        // requestAnimationFrame — the container needs a layout pass
        // before fit can measure cols/rows correctly.
        requestAnimationFrame(() => {
          fit.fit()
          // Focus inside the rAF so the dialog has finished its open
          // transition; MUI moves focus into the dialog on mount, so
          // calling focus() synchronously would lose to that. xterm's
          // focus() lands keystrokes in the terminal and starts the
          // cursor blink.
          term.focus()
        })
      }

      setPhase('waiting')

      // 3) Open the browser-side WS. The session cookie travels via the
      // browser's cookie jar automatically (same-origin).
      const ws = new WebSocket(
        `${deployWsBase()}/devices/${encodeURIComponent(deviceName)}/terminal/ws/${sessionId}`,
      )
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      // Backpressure helper: when the WS buffer is small, write the
      // data; otherwise the bridge would queue indefinitely on a slow
      // connection. xterm calls onData synchronously per keystroke, so
      // there's no need for batching.
      const sendBytes = (s: string) => {
        if (ws.readyState !== WebSocket.OPEN) return
        ws.send(new TextEncoder().encode(s))
      }
      const sendResize = () => {
        if (ws.readyState !== WebSocket.OPEN) return
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }

      term.onData(sendBytes)
      term.onBinary((d) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(d))
      })

      ws.onmessage = (ev) => {
        // Binary frames are pty stdio; text frames are status JSON the
        // relay sends (e.g. "agent did not attach within 60s"). We
        // surface text into the terminal so the operator sees the
        // reason without needing devtools open.
        if (ev.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(ev.data))
          if (phase !== 'connected') setPhase('connected')
        } else if (typeof ev.data === 'string') {
          try {
            const parsed = JSON.parse(ev.data)
            if (parsed.type === 'error') {
              term.write(`\r\n\x1b[31m[drift] ${parsed.message}\x1b[0m\r\n`)
            }
          } catch {
            term.write(`\r\n${ev.data}\r\n`)
          }
        } else if (ev.data instanceof Blob) {
          // Safari/old Firefox path — coerce blob to bytes.
          ev.data.arrayBuffer().then((buf) => term.write(new Uint8Array(buf)))
        }
      }

      ws.onopen = () => {
        // Send the initial size so the bridge boots `login` with a
        // matching pty geometry. The bridge then forwards subsequent
        // resizes via TIOCSWINSZ as the modal resizes.
        sendResize()
      }
      ws.onclose = (ev) => {
        if (cancelled) return
        if (phase === 'waiting') {
          // Most likely the relay closed us with 4408 (agent didn't
          // attach) or 4401 (auth). Surface the close code so the
          // operator can act on it.
          setPhase('error')
          setErrMsg(`session ended (code ${ev.code}${ev.reason ? `: ${ev.reason}` : ''})`)
        } else {
          setPhase('closed')
        }
      }
      ws.onerror = () => {
        if (cancelled) return
        setPhase('error')
        setErrMsg('WebSocket error (check connectivity / nginx upgrade headers)')
      }

      // 4) Handle window/modal resize → fit → send to bridge.
      const ro = new ResizeObserver(() => {
        try {
          fit.fit()
          sendResize()
        } catch {
          /* fit can throw if the container is hidden mid-transition; ignore */
        }
      })
      if (containerRef.current) ro.observe(containerRef.current)

      // Cleanup is registered on the outer effect cleanup; record refs.
      ;(window as any).__driftTermCleanup = () => {
        ro.disconnect()
        try {
          ws.close()
        } catch {
          /* ignore */
        }
        term.dispose()
      }
    }

    start()

    return () => {
      cancelled = true
      const cleanup = (window as any).__driftTermCleanup
      if (typeof cleanup === 'function') {
        cleanup()
        ;(window as any).__driftTermCleanup = undefined
      }
      termRef.current = null
      fitRef.current = null
      wsRef.current = null
    }
    // deviceName is the only meaningful dep — closing/reopening with a
    // different device intentionally tears down.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, deviceName])

  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullScreen
      // disableAutoFocus: stop MUI from focusing the Dialog root after
      // mount — we want focus on the xterm canvas so keystrokes go to
      // the remote shell and the cursor blinks without an extra click.
      // disableRestoreFocus: cosmetic; avoids a focus jump on close.
      // disableEscapeKeyDown: ESC is a real shell key (vim, less, etc.)
      // — without this MUI would close the modal on every ESC press.
      // Close button + onClose backdrop still work.
      disableAutoFocus
      disableRestoreFocus
      disableEscapeKeyDown
      PaperProps={{ sx: { bgcolor: '#0b0b0d', display: 'flex', flexDirection: 'column' } }}
    >
      <DialogTitle sx={{ pr: 1 }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="center" spacing={1}>
            <TerminalIcon fontSize="small" />
            <Typography variant="body1" sx={{ fontWeight: 600 }}>
              {deviceName}
            </Typography>
            <Chip
              size="small"
              variant="outlined"
              label={
                phase === 'creating'
                  ? 'creating…'
                  : phase === 'waiting'
                    // Show elapsed wait with the 30s upper bound so the
                    // operator knows the max patience required (edge
                    // agent polls every POLL_INTERVAL = 30s; worst-case
                    // session-land is one tick).
                    ? `waiting for agent… ${waitingSeconds}s / 30s`
                    : phase === 'connected'
                      ? 'connected'
                      : phase === 'closed'
                        ? 'closed'
                        : 'error'
              }
              color={
                phase === 'connected'
                  ? 'success'
                  : phase === 'waiting' || phase === 'creating'
                    ? 'default'
                    : 'error'
              }
              sx={{ height: 18, fontSize: 10 }}
            />
            {phase === 'waiting' && <CircularProgress size={14} />}
          </Stack>
          <IconButton size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
        {errMsg && (
          <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
            {errMsg}
          </Typography>
        )}
      </DialogTitle>
      <DialogContent
        sx={{
          p: 1.5,
          pt: 0,
          // Fullscreen layout: content fills the remaining height under
          // the DialogTitle. The xterm container takes flex:1 so the
          // ResizeObserver/fit() recomputes rows/cols to the actual
          // viewport on open and on window resize.
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <Box
          ref={containerRef}
          sx={{
            flex: 1,
            minHeight: 0,
            width: '100%',
            bgcolor: '#0b0b0d',
            borderRadius: 1,
          }}
        />
        <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 1, flexShrink: 0 }}>
          Log in as <code>drift</code> with the device's terminal password. Use{' '}
          <code>sudo</code> for root operations.
        </Typography>
      </DialogContent>
    </Dialog>
  )
}
