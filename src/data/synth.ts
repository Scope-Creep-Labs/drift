export function seededRandom(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 0xffffffff
  }
}

export type Series = { ts: string[]; values: number[] }

export function timeAxis(
  startMs: number,
  count: number,
  stepMs: number,
): string[] {
  const out: string[] = new Array(count)
  for (let i = 0; i < count; i++) out[i] = new Date(startMs + i * stepMs).toISOString()
  return out
}

export function sineWithNoise(
  count: number,
  opts: {
    baseline?: number
    amplitude?: number
    period?: number
    noise?: number
    rng?: () => number
  } = {},
): number[] {
  const { baseline = 50, amplitude = 5, period = 60, noise = 1.5, rng = Math.random } = opts
  const out = new Array<number>(count)
  for (let i = 0; i < count; i++) {
    out[i] = baseline + amplitude * Math.sin((i / period) * 2 * Math.PI) + (rng() - 0.5) * 2 * noise
  }
  return out
}

export function randomWalk(
  count: number,
  opts: { start?: number; step?: number; drift?: number; rng?: () => number } = {},
): number[] {
  const { start = 50, step = 1, drift = 0, rng = Math.random } = opts
  const out = new Array<number>(count)
  let v = start
  for (let i = 0; i < count; i++) {
    v += drift + (rng() - 0.5) * 2 * step
    out[i] = v
  }
  return out
}

export function injectStepChange(
  values: number[],
  atIndex: number,
  delta: number,
): number[] {
  const out = values.slice()
  for (let i = atIndex; i < out.length; i++) out[i] += delta
  return out
}

export function injectAnomaly(
  values: number[],
  atIndex: number,
  spanLen: number,
  multiplier: number,
): number[] {
  const out = values.slice()
  const end = Math.min(out.length, atIndex + spanLen)
  for (let i = atIndex; i < end; i++) out[i] *= multiplier
  return out
}

export function ramp(
  values: number[],
  fromIndex: number,
  toIndex: number,
  delta: number,
): number[] {
  const out = values.slice()
  const span = Math.max(1, toIndex - fromIndex)
  for (let i = fromIndex; i < Math.min(out.length, toIndex); i++) {
    const t = (i - fromIndex) / span
    out[i] += delta * t
  }
  for (let i = toIndex; i < out.length; i++) out[i] += delta
  return out
}

export function percentile(values: number[], p: number): number {
  const sorted = values.slice().sort((a, b) => a - b)
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor((p / 100) * sorted.length)))
  return sorted[idx]
}
