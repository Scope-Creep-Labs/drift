import { Box, Stack } from '@mui/material'
import type { RenderBlock } from '../../types/blocks'
import { MarkdownBlock } from './MarkdownBlock'
import { ChartBlock } from './ChartBlock'
import { TableBlock } from './TableBlock'
import { MetricBlock } from './MetricBlock'
import { TimelineBlock } from './TimelineBlock'
import { LiveChartBlock } from './LiveChartBlock'

function renderOne(block: RenderBlock, contextPrompt: string, key: string) {
  switch (block.type) {
    case 'markdown':
      return <MarkdownBlock key={key} block={block} />
    case 'chart':
      return <ChartBlock key={key} block={block} contextPrompt={contextPrompt} />
    case 'table':
      return <TableBlock key={key} block={block} />
    case 'metric':
      return <MetricBlock key={key} block={block} />
    case 'timeline':
      return <TimelineBlock key={key} block={block} />
    case 'live_chart':
      // Use the stable chart_key so the React component instance survives
      // replace-in-place updates from later agent turns. Without this the
      // Plotly chart would remount on every mutation (refresh rate change,
      // adding/removing series) and lose zoom/hover.
      return <LiveChartBlock key={`live:${block.chart_key}`} block={block} />
  }
}

export function BlockRenderer({
  blocks,
  contextPrompt,
}: {
  blocks: RenderBlock[]
  contextPrompt: string
}) {
  // Group runs of metric blocks into a single horizontal Stack.
  const grouped: Array<{ kind: 'metrics'; blocks: RenderBlock[] } | { kind: 'single'; block: RenderBlock }> = []
  for (const b of blocks) {
    const last = grouped[grouped.length - 1]
    if (b.type === 'metric') {
      if (last && last.kind === 'metrics') last.blocks.push(b)
      else grouped.push({ kind: 'metrics', blocks: [b] })
    } else {
      grouped.push({ kind: 'single', block: b })
    }
  }

  return (
    <Stack spacing={2}>
      {grouped.map((g, i) =>
        g.kind === 'metrics' ? (
          <Box key={i} sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap' }}>
            {g.blocks.map((b, j) => renderOne(b, contextPrompt, `${i}-${j}`))}
          </Box>
        ) : (
          renderOne(g.block, contextPrompt, String(i))
        ),
      )}
    </Stack>
  )
}
