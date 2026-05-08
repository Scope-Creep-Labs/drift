// Anthropic pricing per 1M tokens. Update if the public price list changes.
// Source: https://www.anthropic.com/pricing  (Claude API tab)
const PRICING_PER_MTOK: Record<string, { input: number; output: number; cacheWrite: number; cacheRead: number }> = {
  'claude-opus-4-7': { input: 15, output: 75, cacheWrite: 18.75, cacheRead: 1.5 },
  'claude-sonnet-4-6': { input: 3, output: 15, cacheWrite: 3.75, cacheRead: 0.3 },
  'claude-haiku-4-5': { input: 1, output: 5, cacheWrite: 1.25, cacheRead: 0.1 },
}

const DEFAULT_MODEL = 'claude-opus-4-7'

export type Usage = {
  input_tokens?: number
  output_tokens?: number
  cache_read_input_tokens?: number
  cache_creation_input_tokens?: number
}

export function costForUsage(usage: Usage | undefined, model: string = DEFAULT_MODEL): number {
  if (!usage) return 0
  const p = PRICING_PER_MTOK[model] ?? PRICING_PER_MTOK[DEFAULT_MODEL]
  const fresh = usage.input_tokens ?? 0 // already excludes cache reads/writes per Anthropic API
  const cached = usage.cache_read_input_tokens ?? 0
  const cacheWrite = usage.cache_creation_input_tokens ?? 0
  const out = usage.output_tokens ?? 0
  return (
    (fresh * p.input) / 1_000_000 +
    (cached * p.cacheRead) / 1_000_000 +
    (cacheWrite * p.cacheWrite) / 1_000_000 +
    (out * p.output) / 1_000_000
  )
}

export function totalTokens(usage: Usage | undefined): number {
  if (!usage) return 0
  return (
    (usage.input_tokens ?? 0) +
    (usage.output_tokens ?? 0) +
    (usage.cache_read_input_tokens ?? 0) +
    (usage.cache_creation_input_tokens ?? 0)
  )
}

export function formatUsd(amount: number): string {
  if (amount === 0) return '$0'
  if (amount < 0.01) return `$${amount.toFixed(4)}`
  if (amount < 1) return `$${amount.toFixed(3)}`
  return `$${amount.toFixed(2)}`
}

export function sumUsage(turns: { metadata?: { usage?: Usage } }[]): Usage {
  const acc: Required<Usage> = {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
  }
  for (const t of turns) {
    const u = t.metadata?.usage
    if (!u) continue
    acc.input_tokens += u.input_tokens ?? 0
    acc.output_tokens += u.output_tokens ?? 0
    acc.cache_read_input_tokens += u.cache_read_input_tokens ?? 0
    acc.cache_creation_input_tokens += u.cache_creation_input_tokens ?? 0
  }
  return acc
}
