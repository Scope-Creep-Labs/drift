import type { PromptResponse } from '../../types/prompt'
import { dataRegistry } from '../registry'
import { seededRandom } from '../synth'

export function latencyCorrelation(): PromptResponse {
  const rng = seededRandom(7)
  const N = 200

  const latency = Array.from({ length: N }, () => 100 + rng() * 80 + (rng() < 0.07 ? 200 + rng() * 200 : 0))
  const queueDepth = latency.map((l) => l * 0.6 + (rng() - 0.5) * 30)
  const cpu = latency.map((l) => Math.min(100, l * 0.18 + 25 + (rng() - 0.5) * 8))
  const gcPause = latency.map((l) => l * 0.25 + (rng() - 0.5) * 20)
  const netRtt = latency.map(() => 12 + rng() * 6)
  const memUsage = latency.map(() => 60 + (rng() - 0.5) * 8)

  const signals: Record<string, number[]> = {
    latency,
    'queue depth': queueDepth,
    'cpu %': cpu,
    'gc pause ms': gcPause,
    'net rtt ms': netRtt,
    'memory %': memUsage,
  }

  function pearson(a: number[], b: number[]): number {
    const n = a.length
    const meanA = a.reduce((s, v) => s + v, 0) / n
    const meanB = b.reduce((s, v) => s + v, 0) / n
    let num = 0
    let denomA = 0
    let denomB = 0
    for (let i = 0; i < n; i++) {
      const da = a[i] - meanA
      const db = b[i] - meanB
      num += da * db
      denomA += da * da
      denomB += db * db
    }
    return num / Math.sqrt(denomA * denomB)
  }

  const names = Object.keys(signals)
  const matrix = names.map((r) => names.map((c) => Math.round(pearson(signals[r], signals[c]) * 100) / 100))

  dataRegistry.put('matrix://latency/correlation', [
    {
      type: 'heatmap',
      x: names,
      y: names,
      z: matrix,
      zmin: -1,
      zmax: 1,
      colorscale: 'RdBu',
      reversescale: true,
    },
  ])

  dataRegistry.put('scatter://latency/queue-vs-latency', [
    {
      type: 'scatter',
      mode: 'markers',
      x: queueDepth,
      y: latency,
      marker: { color: '#7c9cff', size: 6, opacity: 0.6 },
      name: 'queue vs latency',
    },
  ])

  const ranked = names
    .filter((n) => n !== 'latency')
    .map((n) => ({ signal: n, r: pearson(signals[n], latency) }))
    .sort((a, b) => Math.abs(b.r) - Math.abs(a.r))

  return {
    blocks: [
      {
        type: 'markdown',
        content:
          '**Queue depth** correlates most strongly with latency spikes (r ≈ ' +
          ranked[0].r.toFixed(2) +
          '), followed by GC pause and CPU. Network RTT and memory show no meaningful relationship at this lag.',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Correlation matrix',
        spec: {
          layout: { xaxis: { automargin: true }, yaxis: { automargin: true } },
        },
        dataRef: 'matrix://latency/correlation',
      },
      {
        type: 'chart',
        renderer: 'plotly',
        title: 'Queue depth vs latency',
        spec: {
          layout: {
            xaxis: { title: 'queue depth' },
            yaxis: { title: 'latency (ms)' },
          },
        },
        dataRef: 'scatter://latency/queue-vs-latency',
      },
      {
        type: 'table',
        title: 'Ranked drivers',
        columns: ['signal', 'pearson r', 'strength'],
        rows: ranked.map((r) => [
          r.signal,
          r.r.toFixed(3),
          Math.abs(r.r) > 0.6 ? 'strong' : Math.abs(r.r) > 0.3 ? 'moderate' : 'weak',
        ]),
      },
      {
        type: 'markdown',
        content:
          '### Lag analysis\nQueue depth leads latency by ~30s — useful as an early-warning signal. Consider an alert at queue depth > 150 sustained for 60s.',
      },
    ],
    metadata: { engine: 'mock', confidence: 0.79 },
  }
}
