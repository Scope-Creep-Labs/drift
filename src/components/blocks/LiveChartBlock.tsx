import { useEffect, useMemo, useRef, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-cartesian-dist-min'
import { Box, Chip, IconButton, Stack, Tooltip, Typography, useTheme } from '@mui/material'
import PauseIcon from '@mui/icons-material/Pause'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import type { LiveChartBlock as LiveChartBlockT } from '../../types/blocks'
import { useLiveChartSession } from '../../state/liveChartSession'

const Plot = createPlotlyComponent(Plotly as unknown as Parameters<typeof createPlotlyComponent>[0])

const API_BASE: string =
  import.meta.env.VITE_API_BASE || `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api`

type VmRangeResponse = {
  status: string
  data: {
    resultType: string
    result: Array<{
      metric: Record<string, string>
      values: Array<[number, string]>
    }>
  }
}

type PlotlyTrace = {
  type: 'scatter'
  mode: 'lines'
  name: string
  x: number[]
  y: number[]
  hovertemplate: string
}

// Run one trace's PromQL against /api/query. The endpoint is a thin
// authed passthrough — the cookie travels via credentials: include.
async function fetchTrace(
  promql: string,
  start: number,
  end: number,
  step: number,
): Promise<VmRangeResponse> {
  const res = await fetch(`${API_BASE}/query`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ promql, start, end, step }),
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
  }
  return (await res.json()) as VmRangeResponse
}

// Build a deterministic legend label for a series. Prefer the trace's
// configured name when there's exactly one series; otherwise append the
// disambiguating labels VM returned (e.g. `instance`, `device`).
function seriesLabel(
  configuredName: string,
  metric: Record<string, string>,
  totalSeriesInTrace: number,
): string {
  if (totalSeriesInTrace <= 1) return configuredName
  const meaningful = Object.entries(metric).filter(([k]) => k !== '__name__')
  if (meaningful.length === 0) return configuredName
  const suffix = meaningful.map(([k, v]) => `${k}=${v}`).join(', ')
  return `${configuredName} · ${suffix}`
}

export function LiveChartBlock({ block }: { block: LiveChartBlockT }) {
  const theme = useTheme()
  // Was this chart emitted in the current browser session, or did it
  // come back from localStorage? Charts rehydrated from a prior session
  // mount paused so a page refresh doesn't silently spam /api/query
  // for every chart in your history. The store entry is added in
  // investigationStore.addBlock; mutation (same chart_key re-emitted)
  // bumps the timestamp → useEffect below auto-resumes.
  const emittedAt = useLiveChartSession(
    (s) => s.emissions[block.chart_key],
  )
  const [data, setData] = useState<PlotlyTrace[]>([])
  const [error, setError] = useState<string | null>(null)
  const [paused, setPaused] = useState(!emittedAt)
  const [lastTickAt, setLastTickAt] = useState<number | null>(null)
  // Tracks the in-flight poll so we can cancel from a re-entrant tick.
  // setInterval can fire while a previous poll is still resolving (slow
  // VM, paused tab catching up) — without this guard we'd race ourselves
  // and the chart would flicker between stale and fresh frames.
  const inFlight = useRef<AbortController | null>(null)

  // Stable string of the traces config so the polling effect doesn't
  // restart on every parent re-render (object identity changes even
  // when content is identical because the store rebuilds streaming.blocks).
  const tracesKey = useMemo(() => JSON.stringify(block.traces), [block.traces])

  // Auto-resume when the agent re-emits this chart_key (mutation flow).
  // Component instance survives the replace-in-place; this effect lets
  // the latest emission un-pause if the user had paused or the chart
  // had been rehydrated paused.
  useEffect(() => {
    if (emittedAt) setPaused(false)
  }, [emittedAt])

  // Layout is memoized on the stable bits only — title and trace units.
  // Plotly.react sees a stable layout ref and only diffs data, which is
  // what preserves zoom/hover across ticks.
  const layout = useMemo(() => {
    const yTitle = block.traces.map((t) => t.unit).find((u) => !!u) ?? ''
    return {
      autosize: true,
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: {
        color: theme.palette.text.primary,
        family: theme.typography.fontFamily,
        size: 11,
      },
      margin: { l: 56, r: 16, t: 16, b: 44 },
      xaxis: {
        type: 'date' as const,
        gridcolor: 'rgba(255,255,255,0.06)',
        zerolinecolor: 'rgba(255,255,255,0.1)',
      },
      yaxis: {
        title: yTitle ? { text: yTitle } : undefined,
        gridcolor: 'rgba(255,255,255,0.06)',
        zerolinecolor: 'rgba(255,255,255,0.1)',
      },
      legend: { bgcolor: 'rgba(0,0,0,0)', orientation: 'h' as const, y: -0.2 },
      uirevision: block.chart_key,
    }
    // theme is the only other dep; tracesKey is intentionally not here
    // because changing units shouldn't rebuild layout on every tick.
  }, [block.chart_key, theme, tracesKey])

  useEffect(() => {
    if (paused) return
    let cancelled = false

    const poll = async () => {
      // Cancel any prior in-flight poll so we don't race ourselves when
      // VM is slow or the tab was backgrounded and is now catching up.
      inFlight.current?.abort()
      const ctl = new AbortController()
      inFlight.current = ctl
      try {
        const end = Math.floor(Date.now() / 1000)
        const start = end - block.range_seconds
        const responses = await Promise.all(
          block.traces.map((t) =>
            fetchTrace(t.promql, start, end, block.step_seconds).catch((e) => {
              throw new Error(`${t.name}: ${(e as Error).message}`)
            }),
          ),
        )
        if (cancelled || ctl.signal.aborted) return
        const next: PlotlyTrace[] = []
        responses.forEach((resp, i) => {
          const traceCfg = block.traces[i]
          const series = resp.data?.result ?? []
          for (const s of series) {
            const xs: number[] = []
            const ys: number[] = []
            for (const [ts, v] of s.values) {
              xs.push(ts * 1000) // Plotly date axis wants ms epoch
              const n = parseFloat(v)
              ys.push(Number.isFinite(n) ? n : NaN)
            }
            next.push({
              type: 'scatter',
              mode: 'lines',
              name: seriesLabel(traceCfg.name, s.metric, series.length),
              x: xs,
              y: ys,
              hovertemplate:
                `<b>${traceCfg.name}</b>%{x|%H:%M:%S}<br>%{y}` +
                (traceCfg.unit ? ` ${traceCfg.unit}` : '') +
                '<extra></extra>',
            })
          }
        })
        if (cancelled || ctl.signal.aborted) return
        // Replacing the whole array each tick is fine: react-plotly.js
        // calls Plotly.react under the hood which diffs and only updates
        // changed series — Plotly preserves zoom/hover when the layout
        // ref is stable (see useMemo above).
        setData(next)
        setError(null)
        setLastTickAt(Date.now())
      } catch (e) {
        if (cancelled || ctl.signal.aborted) return
        setError((e as Error).message)
      }
    }

    poll() // immediate, no first-interval wait
    const id = window.setInterval(poll, Math.max(1000, block.refresh_ms))
    return () => {
      cancelled = true
      inFlight.current?.abort()
      window.clearInterval(id)
    }
    // tracesKey covers traces; the other deps are scalars.
  }, [tracesKey, block.refresh_ms, block.range_seconds, block.step_seconds, paused, block.traces])

  const refreshLabel = `${(block.refresh_ms / 1000).toFixed(block.refresh_ms < 1000 ? 2 : 0)}s`

  return (
    <Box>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{ mb: 0.5, px: 0.5 }}
      >
        <Stack direction="row" alignItems="center" spacing={1}>
          {block.title && (
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              {block.title}
            </Typography>
          )}
          <Chip
            size="small"
            label={`live · ${refreshLabel}`}
            color={paused ? 'default' : 'success'}
            variant="outlined"
            sx={{ height: 18, fontSize: 10 }}
          />
          {error && (
            <Tooltip title={error}>
              <Chip
                size="small"
                label="error"
                color="error"
                variant="outlined"
                sx={{ height: 18, fontSize: 10 }}
              />
            </Tooltip>
          )}
          {lastTickAt && !error && (
            <Typography variant="caption" color="text.disabled">
              updated {new Date(lastTickAt).toLocaleTimeString()}
            </Typography>
          )}
        </Stack>
        <Tooltip title={paused ? 'Resume' : 'Pause'}>
          <IconButton size="small" onClick={() => setPaused((p) => !p)}>
            {paused ? <PlayArrowIcon fontSize="small" /> : <PauseIcon fontSize="small" />}
          </IconButton>
        </Tooltip>
      </Stack>
      <Box sx={{ width: '100%', height: 340 }}>
        <Plot
          data={data as Plotly.Data[]}
          layout={layout as Partial<Plotly.Layout>}
          config={{ displaylogo: false, responsive: true, displayModeBar: 'hover' }}
          useResizeHandler
          style={{ width: '100%', height: '100%' }}
        />
      </Box>
    </Box>
  )
}
