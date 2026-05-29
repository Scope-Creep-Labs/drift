import { apiBase } from './apiBase'

// Pricing per 1M tokens, $/M. The runtime values come from the
// backend (`GET /api/models/pricing`), which derives them from
// LiteLLM's `model_cost` table on first request. The frozen
// `FALLBACK` map below is used until the fetch resolves (so the
// sidebar doesn't show $0 on first paint) and as a graceful
// degradation if the endpoint ever 4xx/5xxs.
//
// Sources for the fallback values (kept in sync with what LiteLLM
// snapshots at the time of writing this comment):
//   Anthropic  https://www.anthropic.com/pricing
//   OpenAI     https://openai.com/api/pricing
//   Gemini     https://ai.google.dev/pricing
type Price = { input: number; output: number; cacheWrite: number; cacheRead: number }

const FALLBACK: Record<string, Price> = {
  // Anthropic
  'claude-opus-4-7': { input: 15, output: 75, cacheWrite: 18.75, cacheRead: 1.5 },
  'claude-sonnet-4-6': { input: 3, output: 15, cacheWrite: 3.75, cacheRead: 0.3 },
  'claude-haiku-4-5': { input: 1, output: 5, cacheWrite: 1.25, cacheRead: 0.1 },
  // OpenAI — cacheRead = the cached-input price (auto cache, no opt-in)
  'gpt-4o': { input: 2.5, output: 10, cacheWrite: 0, cacheRead: 1.25 },
  'gpt-4o-mini': { input: 0.15, output: 0.6, cacheWrite: 0, cacheRead: 0.075 },
  'o1': { input: 15, output: 60, cacheWrite: 0, cacheRead: 7.5 },
  'o3-mini': { input: 1.1, output: 4.4, cacheWrite: 0, cacheRead: 0.55 },
  'gpt-5.4': { input: 2.5, output: 10, cacheWrite: 0, cacheRead: 1.25 },
  'gpt-5.4-mini': { input: 0.15, output: 0.6, cacheWrite: 0, cacheRead: 0.075 },
  // Gemini
  'gemini-2.5-pro': { input: 1.25, output: 5, cacheWrite: 0, cacheRead: 0.3125 },
  'gemini-2.5-flash': { input: 0.075, output: 0.3, cacheWrite: 0, cacheRead: 0.01875 },
}

const DEFAULT_MODEL = 'claude-opus-4-7'

// In-memory cache of the backend-derived pricing. Populated lazily on
// first cost computation. Modules that need pricing up-front (the
// usage display) call `prefetchPricing()` after auth so the sidebar
// doesn't flicker $0 → real-value on first paint.
let _live: Record<string, Price> | null = null
let _liveFetchPromise: Promise<void> | null = null

type PricingResponse = {
  models: Record<
    string,
    {
      input_per_mtok: number
      output_per_mtok: number
      cache_read_per_mtok: number
      cache_write_per_mtok: number
    }
  >
}

async function _fetchPricing(): Promise<void> {
  try {
    const res = await fetch(`${apiBase()}/models/pricing`, { credentials: 'include' })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = (await res.json()) as PricingResponse
    const next: Record<string, Price> = {}
    for (const [model, p] of Object.entries(data.models)) {
      next[model] = {
        input: p.input_per_mtok,
        output: p.output_per_mtok,
        cacheRead: p.cache_read_per_mtok,
        cacheWrite: p.cache_write_per_mtok,
      }
    }
    _live = next
  } catch {
    // Backend unreachable / unauthenticated / older version that doesn't
    // expose the endpoint — fall back silently. The static FALLBACK
    // table covers everything in the curated installer catalog.
    _live = { ...FALLBACK }
  }
}

/** Kick off the pricing fetch. Idempotent: subsequent calls return
 *  the same in-flight promise. Call after login so cost numbers in
 *  the sidebar are correct on first paint. */
export function prefetchPricing(): Promise<void> {
  if (_live !== null) return Promise.resolve()
  if (_liveFetchPromise === null) _liveFetchPromise = _fetchPricing()
  return _liveFetchPromise
}

// Strip an optional litellm provider prefix ("anthropic/" / "openai/" /
// "gemini/") so a config value like MODEL=anthropic/claude-opus-4-7
// still matches our table keys.
function _stripProvider(model: string): string {
  const slash = model.indexOf('/')
  return slash >= 0 ? model.slice(slash + 1) : model
}

function _priceFor(model: string): Price {
  const table = _live ?? FALLBACK
  // Try the model as-typed first (covers litellm provider prefixes
  // like `gemini/gemini-2.5-pro` which live in the backend's
  // model_cost dict verbatim).
  if (table[model]) return table[model]
  const stripped = _stripProvider(model)
  if (table[stripped]) return table[stripped]
  // Fall back to the default model's pricing so unknown models still
  // produce a non-zero estimate. Local models (Ollama, vLLM) report
  // $0 from the backend, which lands here as the FALLBACK's default
  // — slightly wrong but harmless and the operator will recognize the
  // model didn't get priced if they care.
  return table[DEFAULT_MODEL] ?? FALLBACK[DEFAULT_MODEL]
}

export type Usage = {
  input_tokens?: number
  output_tokens?: number
  cache_read_input_tokens?: number
  cache_creation_input_tokens?: number
}

export function costForUsage(usage: Usage | undefined, model: string = DEFAULT_MODEL): number {
  if (!usage) return 0
  const p = _priceFor(model)
  const fresh = usage.input_tokens ?? 0 // already excludes cache reads/writes
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

/** Sum cost across a per-model usage breakdown (the shape MeUsage.models
 *  returns from the backend). Each model is priced separately, results
 *  are added. Unknown models fall back to the default pricing. */
export function costForUsageByModel(byModel: Record<string, Usage>): number {
  let total = 0
  for (const [model, usage] of Object.entries(byModel)) {
    total += costForUsage(usage, model)
  }
  return total
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
