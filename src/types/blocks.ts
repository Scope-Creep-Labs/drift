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

export type RenderBlock =
  | MarkdownBlock
  | ChartBlock
  | TableBlock
  | MetricBlock
  | TimelineBlock
