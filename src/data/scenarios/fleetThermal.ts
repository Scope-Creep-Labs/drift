import type { PromptResponse } from '../../types/prompt'
import { dataRegistry } from '../registry'
import { seededRandom, sineWithNoise, timeAxis } from '../synth'

export function fleetThermal(): PromptResponse {
  const rng = seededRandom(91)
  const devices = Array.from({ length: 24 }, (_, i) => `gw-${String(i + 1).padStart(2, '0')}`)
  const outliers = ['gw-04', 'gw-09', 'gw-17', 'gw-22']

  const rows = devices
    .map((d) => {
      const baseline = 42 + rng() * 4
      const isOutlier = outliers.includes(d)
      const peak = baseline + (isOutlier ? 18 + rng() * 6 : rng() * 5)
      const drift = isOutlier ? 4 + rng() * 3 : rng() * 1.5
      const score = Math.round((peak - 42) * 4 + drift * 10) / 10
      return [d, peak.toFixed(1), drift.toFixed(2), score, isOutlier ? 'critical' : 'ok']
    })
    .sort((a, b) => (b[3] as number) - (a[3] as number))

  const N = 168
  const start = Date.UTC(2026, 4, 1, 0, 0, 0)
  const ts = timeAxis(start, N, 60 * 60_000)

  const traces = devices.map((d) => {
    const isOutlier = outliers.includes(d)
    const y = sineWithNoise(N, {
      baseline: 42 + (isOutlier ? 8 : 0),
      amplitude: isOutlier ? 12 : 4,
      period: 24,
      noise: 1.4,
      rng: seededRandom(d.charCodeAt(3) * 7 + 1),
    })
    return {
      type: 'scatter',
      mode: 'lines',
      name: d,
      x: ts,
      y,
      opacity: isOutlier ? 0.95 : 0.25,
      line: { width: isOutlier ? 1.8 : 1, color: isOutlier ? undefined : '#888' },
    }
  })

  dataRegistry.put('timeseries://fleet/thermal', traces)

  const heatmapZ: number[][] = devices.map((d) => {
    const isOutlier = outliers.includes(d)
    return Array.from({ length: 24 }, (_, h) => {
      const diurnal = 8 * Math.sin((h / 24) * 2 * Math.PI - Math.PI / 2)
      const base = 42 + diurnal + (isOutlier ? 9 : 0) + rng() * 2
      return Math.round(base * 10) / 10
    })
  })

  dataRegistry.put('heatmap://fleet/thermal-by-hour', [
    {
      type: 'heatmap',
      x: Array.from({ length: 24 }, (_, h) => `${h}:00`),
      y: devices,
      z: heatmapZ,
      colorscale: 'Hot',
    },
  ])

  return {
    blocks: [
      {
        type: 'markdown',
        content:
          '**4 devices** show abnormal thermal behavior this week. Outliers cluster on gateways deployed in the western rack row, suggesting an environmental rather than firmware cause.',
      },
      {
        type: 'table',
        title: 'Anomaly ranking',
        columns: ['device', 'peak °C', 'drift', 'score', 'status'],
        rows,
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Thermal traces — outliers highlighted',
        spec: {
          layout: {
            xaxis: { title: 'time' },
            yaxis: { title: '°C' },
            showlegend: false,
          },
        },
        dataRef: 'timeseries://fleet/thermal',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Hour-of-day heatmap',
        spec: {
          layout: {
            xaxis: { title: 'hour of day' },
            yaxis: { title: 'device', automargin: true },
          },
        },
        dataRef: 'heatmap://fleet/thermal-by-hour',
      },
      {
        type: 'markdown',
        content:
          '### Recommended next step\nInspect cooling on the western rack row (gw-04, gw-09, gw-17, gw-22). All four show identical 14:00–18:00 UTC peaks, consistent with shared HVAC.',
      },
    ],
    metadata: { engine: 'mock', confidence: 0.74, dataSources: ['timeseries://fleet/thermal'] },
  }
}
