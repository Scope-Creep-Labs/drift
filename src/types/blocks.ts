export type MarkdownBlock = {
  type: 'markdown'
  content: string
}

export type ChartBlock = {
  type: 'chart'
  renderer: 'plotly' | 'echarts'
  spec: Record<string, unknown>
  dataRef?: string
  title?: string
}

export type TableBlock = {
  type: 'table'
  columns: string[]
  rows: unknown[][]
  title?: string
}

export type MetricBlock = {
  type: 'metric'
  label: string
  value: string | number
  unit?: string
  trend?: 'up' | 'down' | 'flat'
}

export type TimelineEvent = {
  ts: string
  label: string
  severity?: 'info' | 'warn' | 'error'
}

export type TimelineBlock = {
  type: 'timeline'
  events: TimelineEvent[]
  title?: string
}

export type LiveChartTrace = {
  name: string
  promql: string
  unit?: string
}

// Auto-refreshing chart. The block carries no data — the LiveChart
// component polls /api/query on a setInterval and uses Plotly.react via
// react-plotly.js so only the series data updates (zoom/hover/axes are
// preserved). `chart_key` is the replace-in-place identity: when the
// store sees a new live_chart with the same chart_key, it overwrites
// the prior block instead of appending.
export type LiveChartBlock = {
  type: 'live_chart'
  chart_key: string
  title?: string
  traces: LiveChartTrace[]
  refresh_ms: number
  range_seconds: number
  step_seconds: number
}

export type RenderBlock =
  | MarkdownBlock
  | ChartBlock
  | TableBlock
  | MetricBlock
  | TimelineBlock
  | LiveChartBlock
