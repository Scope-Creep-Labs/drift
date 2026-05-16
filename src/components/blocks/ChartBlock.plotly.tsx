import { useMemo } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-cartesian-dist-min'
import { Box, Button, Skeleton, Stack, Typography, useTheme } from '@mui/material'
import ReplayIcon from '@mui/icons-material/Replay'
import type { ChartBlock as ChartBlockT } from '../../types/blocks'
import { useDataRef } from '../../query/useDataRef'
import { useInvestigate } from '../../query/useInvestigate'

// react-plotly.js/factory accepts the plotly module and returns a React component.
// Using the cartesian-dist-min subset (~1.4MB) instead of the full bundle
// (~4.7MB). Covers scatter/bar/heatmap/histogram/box/violin/contour/pie —
// everything Drift's agent emits. If a future trace type needs scattergl,
// 3d, or geo, swap back to plotly.js-dist-min or pick a different subset.
const Plot = createPlotlyComponent(Plotly as unknown as Parameters<typeof createPlotlyComponent>[0])

export default function ChartBlockPlotly({
  block,
  contextPrompt,
}: {
  block: ChartBlockT
  contextPrompt?: string
}) {
  const theme = useTheme()
  const { data, isLoading, error } = useDataRef<unknown[]>(block.dataRef, contextPrompt)
  const { submit, isStreaming } = useInvestigate()

  const layout = useMemo(() => {
    const userLayout = (block.spec?.layout as Record<string, unknown>) ?? {}
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
        gridcolor: 'rgba(255,255,255,0.06)',
        zerolinecolor: 'rgba(255,255,255,0.1)',
        ...(userLayout.xaxis as object),
      },
      yaxis: {
        gridcolor: 'rgba(255,255,255,0.06)',
        zerolinecolor: 'rgba(255,255,255,0.1)',
        ...(userLayout.yaxis as object),
      },
      legend: { bgcolor: 'rgba(0,0,0,0)', orientation: 'h', y: -0.2 },
      ...userLayout,
    }
  }, [block.spec, theme])

  if (isLoading) {
    return <Skeleton variant="rounded" height={320} />
  }
  if (error) {
    const isAgentRef = (block.dataRef ?? '').startsWith('prom://')
    if (isAgentRef) {
      // The dataRegistry is in-memory only by design (see CLAUDE.md §
      // dataRef pattern). When the user reopens an old investigation,
      // chart blocks have refs but no traces. We can't re-fetch the data
      // alone (no query args stored), so the "regenerate" button
      // re-submits the turn's original prompt — a new turn streams in at
      // the bottom with fresh charts. This block stays as-is (it's a
      // record of the original run).
      return (
        <Stack
          spacing={1}
          sx={{
            py: 2,
            px: 2,
            border: 1,
            borderColor: 'divider',
            borderRadius: 1,
            bgcolor: 'rgba(255,255,255,0.02)',
            alignItems: 'flex-start',
          }}
        >
          <Typography variant="body2" color="text.secondary" sx={{ fontStyle: 'italic' }}>
            Chart data is no longer in cache (the page was reloaded).
          </Typography>
          <Button
            size="small"
            variant="outlined"
            startIcon={<ReplayIcon fontSize="small" />}
            disabled={isStreaming || !contextPrompt}
            onClick={() => {
              if (!contextPrompt) return
              submit({ prompt: contextPrompt })
            }}
            sx={{ textTransform: 'none' }}
          >
            Regenerate chart
          </Button>
          {contextPrompt && (
            <Typography variant="caption" color="text.disabled">
              Re-runs the prompt — fresh charts will appear in a new turn below.
            </Typography>
          )}
        </Stack>
      )
    }
    return (
      <Typography variant="body2" color="text.secondary" sx={{ fontStyle: 'italic' }}>
        Failed to load chart data: {(error as Error).message}
      </Typography>
    )
  }
  if (!data) return null

  return (
    <Box sx={{ width: '100%', height: 340 }}>
      <Plot
        data={data as Plotly.Data[]}
        layout={layout as Partial<Plotly.Layout>}
        config={{ displaylogo: false, responsive: true, displayModeBar: 'hover' }}
        useResizeHandler
        style={{ width: '100%', height: '100%' }}
      />
    </Box>
  )
}
