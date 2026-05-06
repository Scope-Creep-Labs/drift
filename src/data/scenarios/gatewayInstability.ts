import type { PromptResponse } from '../../types/prompt'
import { dataRegistry } from '../registry'
import {
  injectAnomaly,
  ramp,
  randomWalk,
  seededRandom,
  sineWithNoise,
  timeAxis,
} from '../synth'

export function gatewayInstability(): PromptResponse {
  const rng = seededRandom(17)
  const N = 360
  const stepMs = 60_000
  const start = Date.UTC(2026, 4, 5, 9, 0, 0)
  const ts = timeAxis(start, N, stepMs)
  const anomalyIdx = Math.floor(N * (5.5 / 6))

  let memory = randomWalk(N, { start: 38, step: 0.4, drift: 0.015, rng })
  memory = ramp(memory, Math.floor(N * 0.6), N, 22)
  memory = injectAnomaly(memory, anomalyIdx, 20, 1.18)

  const latency = sineWithNoise(N, { baseline: 120, amplitude: 14, period: 90, noise: 8, rng })
  const latencySpiked = injectAnomaly(latency, anomalyIdx, 25, 2.6)

  const restartIdx = [
    Math.floor(N * 0.62),
    Math.floor(N * 0.74),
    Math.floor(N * 0.83),
    Math.floor(N * 0.91),
    Math.floor(N * 0.97),
  ]

  dataRegistry.put('timeseries://gateway-17/memory', [
    {
      type: 'scatter',
      mode: 'lines',
      name: 'memory MB',
      x: ts,
      y: memory,
      line: { color: '#7c9cff', width: 1.6 },
    },
    {
      type: 'scatter',
      mode: 'markers',
      name: 'restarts',
      x: restartIdx.map((i) => ts[i]),
      y: restartIdx.map((i) => memory[i]),
      marker: { color: '#ff6b6b', size: 9, symbol: 'x' },
    },
  ])

  dataRegistry.put('timeseries://gateway-17/latency', [
    {
      type: 'scatter',
      mode: 'lines',
      name: 'p50 latency ms',
      x: ts,
      y: latencySpiked,
      line: { color: '#5ad1c1', width: 1.6 },
    },
  ])

  return {
    blocks: [
      {
        type: 'markdown',
        content:
          '**Gateway-17** became unstable starting around `' +
          new Date(ts[anomalyIdx]).toISOString().slice(11, 16) +
          ' UTC`. Memory grew steadily over the prior hour, then the worker process began restarting repeatedly, with latency spiking 2-3x baseline during each restart cycle.',
      },
      {
        type: 'metric',
        label: 'Restarts (last 1h)',
        value: restartIdx.length,
        unit: 'events',
        trend: 'up',
      },
      {
        type: 'metric',
        label: 'Peak memory',
        value: Math.round(Math.max(...memory)),
        unit: 'MB',
        trend: 'up',
      },
      {
        type: 'metric',
        label: 'p50 latency at incident',
        value: Math.round(latencySpiked[anomalyIdx + 5]),
        unit: 'ms',
        trend: 'up',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Memory usage — gateway-17',
        spec: {
          layout: {
            xaxis: { title: 'time' },
            yaxis: { title: 'memory (MB)' },
            shapes: [
              {
                type: 'rect',
                xref: 'x',
                yref: 'paper',
                x0: ts[anomalyIdx],
                x1: ts[Math.min(N - 1, anomalyIdx + 25)],
                y0: 0,
                y1: 1,
                fillcolor: '#ff6b6b',
                opacity: 0.08,
                line: { width: 0 },
              },
            ],
          },
        },
        dataRef: 'timeseries://gateway-17/memory',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Latency — gateway-17',
        spec: {
          layout: {
            xaxis: { title: 'time' },
            yaxis: { title: 'p50 latency (ms)' },
          },
        },
        dataRef: 'timeseries://gateway-17/latency',
      },
      {
        type: 'timeline',
        title: 'Process restart events',
        events: restartIdx.map((i, k) => ({
          ts: ts[i],
          label: `worker-x restart #${k + 1}`,
          severity: k >= 3 ? 'error' : 'warn',
        })),
      },
      {
        type: 'markdown',
        content:
          '### Likely cause\n' +
          'Memory leak in `worker-x`. Steady growth since 14:00 UTC reached the configured limit (~60 MB) at 14:35 UTC, triggering OOM-kill and supervised restart loops.\n\n' +
          '### Recommendations\n' +
          '1. Roll back the worker-x build deployed at 13:55 UTC (release `v2.8.1`).\n' +
          '2. Raise the per-process memory cap temporarily to break the restart loop.\n' +
          '3. Compare allocation profiles between `v2.8.0` and `v2.8.1`.',
      },
    ],
    metadata: {
      engine: 'mock',
      confidence: 0.82,
      dataSources: ['timeseries://gateway-17/memory', 'timeseries://gateway-17/latency'],
    },
  }
}
