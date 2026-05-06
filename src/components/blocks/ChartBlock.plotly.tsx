import { useMemo } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import { Box, Skeleton, Typography, useTheme } from '@mui/material'
import type { ChartBlock as ChartBlockT } from '../../types/blocks'
import { useDataRef } from '../../query/useDataRef'

// react-plotly.js/factory accepts the plotly module and returns a React component.
// Using the factory + plotly.js-dist-min keeps the bundle ~40% smaller than the full plotly.js.
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
    return (
      <Typography variant="body2" color="error">
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
