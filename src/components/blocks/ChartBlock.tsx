import { lazy, Suspense } from 'react'
import { Box, Paper, Skeleton, Typography } from '@mui/material'
import type { ChartBlock as ChartBlockT } from '../../types/blocks'

const ChartBlockPlotly = lazy(() => import('./ChartBlock.plotly'))
const ChartBlockEcharts = lazy(() => import('./ChartBlock.echarts'))

export function ChartBlock({
  block,
  contextPrompt,
}: {
  block: ChartBlockT
  contextPrompt?: string
}) {
  const Renderer = block.renderer === 'echarts' ? ChartBlockEcharts : ChartBlockPlotly

  return (
    <Paper variant="outlined" sx={{ borderColor: 'divider', p: 2 }}>
      {block.title && (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ textTransform: 'uppercase', letterSpacing: 0.4, mb: 1.2, display: 'block' }}
        >
          {block.title}
        </Typography>
      )}
      <Suspense fallback={<Box sx={{ height: 340 }}><Skeleton variant="rounded" height="100%" /></Box>}>
        <Renderer block={block} contextPrompt={contextPrompt} />
      </Suspense>
    </Paper>
  )
}
