import type { PromptResponse } from '../../types/prompt'
import { dataRegistry } from '../registry'
import { seededRandom, sineWithNoise, timeAxis } from '../synth'

export function dispatchOptimization(): PromptResponse {
  const rng = seededRandom(42)
  const N = 96
  const start = Date.UTC(2026, 4, 6, 0, 0, 0)
  const ts = timeAxis(start, N, 15 * 60_000)

  const baselineSoc = sineWithNoise(N, {
    baseline: 55,
    amplitude: 22,
    period: 96,
    noise: 1.4,
    rng,
  })
  const optimizedSoc = sineWithNoise(N, {
    baseline: 60,
    amplitude: 18,
    period: 96,
    noise: 0.7,
    rng: seededRandom(43),
  })

  const baselineDispatch = baselineSoc.map((v, i) => Math.sin((i / 12) * Math.PI) * 35 + (v - 55) * 0.4)
  const optimizedDispatch = optimizedSoc.map((v, i) => Math.sin((i / 14) * Math.PI) * 28 + (v - 60) * 0.3)

  const thermalEnvelope = optimizedSoc.map((v) => 42 + (v - 50) * 0.18 + (rng() - 0.5))
  const thermalLimit = 55

  dataRegistry.put('timeseries://dispatch/soc', [
    {
      type: 'scatter',
      mode: 'lines',
      name: 'baseline SOC',
      x: ts,
      y: baselineSoc,
      line: { dash: 'dot', color: '#888', width: 1.4 },
    },
    {
      type: 'scatter',
      mode: 'lines',
      name: 'optimized SOC',
      x: ts,
      y: optimizedSoc,
      line: { color: '#7c9cff', width: 2 },
    },
  ])

  dataRegistry.put('timeseries://dispatch/power', [
    {
      type: 'bar',
      name: 'baseline kW',
      x: ts,
      y: baselineDispatch,
      marker: { color: 'rgba(180,180,180,0.5)' },
    },
    {
      type: 'bar',
      name: 'optimized kW',
      x: ts,
      y: optimizedDispatch,
      marker: { color: '#5ad1c1' },
    },
  ])

  dataRegistry.put('timeseries://dispatch/thermal', [
    {
      type: 'scatter',
      mode: 'lines',
      name: '°C',
      x: ts,
      y: thermalEnvelope,
      line: { color: '#ff9f43' },
      fill: 'tozeroy',
      fillcolor: 'rgba(255,159,67,0.12)',
    },
    {
      type: 'scatter',
      mode: 'lines',
      name: 'thermal limit',
      x: [ts[0], ts[N - 1]],
      y: [thermalLimit, thermalLimit],
      line: { dash: 'dash', color: '#ff6b6b' },
    },
  ])

  return {
    blocks: [
      {
        type: 'markdown',
        content:
          '**Optimized dispatch** reduces thermal stress by ~24% versus baseline while meeting the same energy delivery target. Two soft constraint touches at 11:15 and 18:30 — none breached.',
      },
      {
        type: 'metric',
        label: 'Thermal stress reduction',
        value: '24',
        unit: '%',
        trend: 'down',
      },
      {
        type: 'metric',
        label: 'Energy delivered',
        value: '412',
        unit: 'kWh',
        trend: 'flat',
      },
      {
        type: 'metric',
        label: 'Constraint violations',
        value: 0,
        trend: 'flat',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'SOC trajectory',
        spec: {
          layout: { xaxis: { title: 'time' }, yaxis: { title: 'SOC (%)' } },
        },
        dataRef: 'timeseries://dispatch/soc',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Dispatch (kW)',
        spec: {
          layout: { xaxis: { title: 'time' }, yaxis: { title: 'kW' }, barmode: 'group' },
        },
        dataRef: 'timeseries://dispatch/power',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Thermal envelope',
        spec: {
          layout: { xaxis: { title: 'time' }, yaxis: { title: '°C' } },
        },
        dataRef: 'timeseries://dispatch/thermal',
      },
      {
        type: 'markdown',
        content:
          '### Recommendation\nApply optimized schedule with a 2 °C safety margin on the 14:00–17:00 window. Re-run with updated forecast at T-15min.',
      },
    ],
    metadata: { engine: 'mock', confidence: 0.88 },
  }
}
