import type { PromptResponse } from '../../types/prompt'
import { dataRegistry } from '../registry'
import { percentile, seededRandom, sineWithNoise, timeAxis } from '../synth'

export function v28Regression(): PromptResponse {
  const rng = seededRandom(208)
  const N = 240
  const stepMs = 60_000
  const before = Date.UTC(2026, 4, 4, 8, 0, 0)
  const tsBefore = timeAxis(before, N, stepMs)
  const tsAfter = timeAxis(before + N * stepMs, N, stepMs)

  const cpuBefore = sineWithNoise(N, { baseline: 38, amplitude: 5, period: 60, noise: 2, rng })
  const cpuAfter = sineWithNoise(N, { baseline: 51, amplitude: 7, period: 60, noise: 3, rng: seededRandom(209) })

  dataRegistry.put('timeseries://release/v2.8/cpu', [
    {
      type: 'scatter',
      mode: 'lines',
      name: 'before v2.8',
      x: tsBefore,
      y: cpuBefore,
      line: { color: '#5ad1c1', width: 1.6 },
    },
    {
      type: 'scatter',
      mode: 'lines',
      name: 'after v2.8',
      x: tsAfter,
      y: cpuAfter,
      line: { color: '#ff6b6b', width: 1.6 },
    },
  ])

  dataRegistry.put('histogram://release/v2.8/cpu-dist', [
    {
      type: 'histogram',
      name: 'before',
      x: cpuBefore,
      opacity: 0.7,
      marker: { color: '#5ad1c1' },
      nbinsx: 30,
    },
    {
      type: 'histogram',
      name: 'after',
      x: cpuAfter,
      opacity: 0.7,
      marker: { color: '#ff6b6b' },
      nbinsx: 30,
    },
  ])

  const p50b = percentile(cpuBefore, 50)
  const p50a = percentile(cpuAfter, 50)
  const p95b = percentile(cpuBefore, 95)
  const p95a = percentile(cpuAfter, 95)
  const p99b = percentile(cpuBefore, 99)
  const p99a = percentile(cpuAfter, 99)

  return {
    blocks: [
      {
        type: 'markdown',
        content:
          '**Yes** — release `v2.8` increased CPU usage by **~33%** at p50 and **~38%** at p95 across the fleet. The shift is consistent and persistent (no decay over 4h), so this is a regression, not a transient spike.',
      },
      {
        type: 'metric',
        label: 'p50 Δ',
        value: `${(p50a - p50b).toFixed(1)}`,
        unit: '%',
        trend: 'up',
      },
      {
        type: 'metric',
        label: 'p95 Δ',
        value: `${(p95a - p95b).toFixed(1)}`,
        unit: '%',
        trend: 'up',
      },
      {
        type: 'metric',
        label: 'p99 Δ',
        value: `${(p99a - p99b).toFixed(1)}`,
        unit: '%',
        trend: 'up',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Before / after CPU',
        spec: {
          layout: { xaxis: { title: 'time' }, yaxis: { title: 'CPU %' } },
        },
        dataRef: 'timeseries://release/v2.8/cpu',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Distribution comparison',
        spec: {
          layout: {
            barmode: 'overlay',
            xaxis: { title: 'CPU %' },
            yaxis: { title: 'count' },
          },
        },
        dataRef: 'histogram://release/v2.8/cpu-dist',
      },
      {
        type: 'table',
        title: 'Percentile shift',
        columns: ['percentile', 'before', 'after', 'delta'],
        rows: [
          ['p50', p50b.toFixed(1), p50a.toFixed(1), `+${(p50a - p50b).toFixed(1)}`],
          ['p95', p95b.toFixed(1), p95a.toFixed(1), `+${(p95a - p95b).toFixed(1)}`],
          ['p99', p99b.toFixed(1), p99a.toFixed(1), `+${(p99a - p99b).toFixed(1)}`],
        ],
      },
      {
        type: 'markdown',
        content:
          '### Suggested action\nBisect commits in `v2.8` against the worker-x scheduler change in commit `a3f1c8e`. Memory profile is unchanged, so the regression is CPU-only — points at scheduler hot path.',
      },
    ],
    metadata: { engine: 'mock', confidence: 0.91 },
  }
}
