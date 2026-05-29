import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  InputAdornment,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import VisibilityIcon from '@mui/icons-material/VisibilityOutlined'
import VisibilityOffIcon from '@mui/icons-material/VisibilityOffOutlined'
import { apiBase } from '../lib/apiBase'
import { formatUsd } from '../lib/pricing'

// Model of the admin /api/admin/llm-settings GET response.
type LlmSettings = {
  model: string
  effort: string
  max_tokens: number
  configured_keys: { anthropic: boolean; openai: boolean; gemini: boolean; ollama: boolean }
  ollama_api_base: string
  current_provider: 'anthropic' | 'openai' | 'gemini' | 'ollama' | 'unknown'
}

// Subset of /api/models/pricing the modal cares about.
type PricingEntry = {
  input_per_mtok: number
  output_per_mtok: number
  cache_read_per_mtok: number
  cache_write_per_mtok: number
}

// Hand-curated favorites that appear at the top of the picker. Anything
// LiteLLM knows about is still selectable from the full list below, but
// these are the names we'd recommend for first-time setup.
const FAVORITES = [
  'claude-opus-4-7',
  'claude-sonnet-4-6',
  'claude-haiku-4-5',
  'gpt-5.4',
  'gpt-5.4-mini',
  'gemini-2.5-pro',
  'gemini-2.5-flash',
]

function detectProvider(model: string): LlmSettings['current_provider'] {
  if (!model) return 'unknown'
  if (model.startsWith('ollama/') || model.startsWith('ollama_chat/')) return 'ollama'
  const bare = model.includes('/') ? model.split('/', 2)[1] : model
  if (bare.startsWith('claude-') || model.startsWith('anthropic/')) return 'anthropic'
  if (bare.startsWith('gpt-') || bare.startsWith('o1') || bare.startsWith('o3')) return 'openai'
  if (bare.startsWith('gemini-') || model.startsWith('gemini/')) return 'gemini'
  return 'unknown'
}

function priceCell(price: number | undefined): string {
  if (price === undefined || price === 0) return '—'
  return formatUsd(price)
}

export function LlmSettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [settings, setSettings] = useState<LlmSettings | null>(null)
  const [pricing, setPricing] = useState<Record<string, PricingEntry>>({})
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Form state mirrors what gets PUT. Each API key starts blank — the
  // backend's GET never returns existing values, so the "current" state
  // is "we know it's set, here's where you can rotate it." Empty submit
  // = no change for that field; explicit clearing requires the operator
  // to type a space (which the trim() server-side reduces to "").
  const [model, setModel] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [geminiKey, setGeminiKey] = useState('')
  const [ollamaBase, setOllamaBase] = useState('')
  const [showKeys, setShowKeys] = useState(false)

  // Fetch settings + pricing whenever the modal opens. Both are
  // single-shot — once loaded, the modal works against the cached
  // values until close.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      setSuccess(null)
      try {
        const [sRes, pRes] = await Promise.all([
          fetch(`${apiBase()}/admin/llm-settings`, { credentials: 'include' }),
          fetch(`${apiBase()}/models/pricing`, { credentials: 'include' }),
        ])
        if (!sRes.ok) throw new Error(`settings: HTTP ${sRes.status}`)
        const sData = (await sRes.json()) as LlmSettings
        const pData = pRes.ok ? ((await pRes.json()) as { models: Record<string, PricingEntry> }) : { models: {} }
        if (cancelled) return
        setSettings(sData)
        setPricing(pData.models)
        setModel(sData.model)
        setOllamaBase(sData.ollama_api_base)
      } catch (e) {
        if (!cancelled) setError((e as Error).message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open])

  // Group models for the picker: favorites first, then everything else
  // LiteLLM knows about, sorted by provider then by name. The whole list
  // is rendered as MenuItems inside a single Select for searchability
  // (MUI Autocomplete is the heavier alternative; for ~hundreds of
  // entries the native Select with grouping is fine).
  const modelOptions = useMemo(() => {
    const allKnown = new Set([...FAVORITES, ...Object.keys(pricing), settings?.model || ''])
    allKnown.delete('')
    const favs = FAVORITES.filter((m) => allKnown.has(m))
    const others = [...allKnown]
      .filter((m) => !FAVORITES.includes(m))
      .sort((a, b) => {
        const pa = detectProvider(a)
        const pb = detectProvider(b)
        if (pa !== pb) return pa.localeCompare(pb)
        return a.localeCompare(b)
      })
    return { favs, others }
  }, [pricing, settings?.model])

  const targetProvider = detectProvider(model)

  const apply = async () => {
    if (!settings) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const body: Record<string, unknown> = {}
      if (model !== settings.model) body.model = model
      if (ollamaBase !== settings.ollama_api_base) body.ollama_api_base = ollamaBase
      if (anthropicKey.trim() !== '') body.anthropic_api_key = anthropicKey
      if (openaiKey.trim() !== '') body.openai_api_key = openaiKey
      if (geminiKey.trim() !== '') body.gemini_api_key = geminiKey

      if (Object.keys(body).length === 0) {
        setSuccess('Nothing changed.')
        setSaving(false)
        return
      }
      const res = await fetch(`${apiBase()}/admin/llm-settings`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const txt = await res.text().catch(() => '')
        throw new Error(`HTTP ${res.status}${txt ? `: ${txt}` : ''}`)
      }
      const data = (await res.json()) as { restart_scheduled: boolean }
      setSuccess(
        data.restart_scheduled
          ? 'Saved. drift-agent will restart in ~5s to pick up the new values. The page will reload after it comes back.'
          : 'Saved. drift-agent restart was NOT scheduled (docker socket unreachable from inside the container). Restart manually.',
      )
      // Poll the SPA's own healthcheck so we can offer to reload once
      // the agent is back. Worst case the page is unresponsive for
      // ~10s during the recreate; this loop bounds the wait.
      if (data.restart_scheduled) {
        setTimeout(() => {
          window.location.reload()
        }, 8000)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const renderPriceRow = (m: string) => {
    const p = pricing[m]
    return (
      <Stack key={m} direction="row" spacing={1} alignItems="center" sx={{ fontSize: '0.7rem' }}>
        <Typography variant="caption" sx={{ flex: 1, fontFamily: 'monospace' }}>
          {m}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ width: 70, textAlign: 'right' }}>
          in {priceCell(p?.input_per_mtok)}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ width: 70, textAlign: 'right' }}>
          out {priceCell(p?.output_per_mtok)}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ width: 80, textAlign: 'right' }}>
          cache {priceCell(p?.cache_read_per_mtok)}
        </Typography>
      </Stack>
    )
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 1 }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Typography variant="h6" sx={{ fontSize: '1rem' }}>
            LLM model + API keys
          </Typography>
          <IconButton size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
        <Typography variant="caption" color="text.secondary">
          Persists to the installer's `.env`. drift-agent restarts to pick up changes.
        </Typography>
      </DialogTitle>

      <DialogContent dividers>
        {loading && (
          <Stack alignItems="center" sx={{ py: 4 }}>
            <CircularProgress size={20} />
          </Stack>
        )}

        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        {success && (
          <Alert severity="success" sx={{ mb: 2 }}>
            {success}
          </Alert>
        )}

        {!loading && settings && (
          <Stack spacing={2.5}>
            <Box>
              <TextField
                select
                fullWidth
                label="Model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                helperText={`Provider: ${targetProvider}. Pricing comes from LiteLLM.`}
                size="small"
                SelectProps={{
                  MenuProps: { sx: { maxHeight: 500 } },
                }}
              >
                {modelOptions.favs.length > 0 && (
                  <MenuItem disabled value="" sx={{ opacity: 1, fontSize: '0.7rem', color: 'text.secondary' }}>
                    Recommended
                  </MenuItem>
                )}
                {modelOptions.favs.map((m) => (
                  <MenuItem key={m} value={m}>
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ width: '100%' }}>
                      <Typography sx={{ fontFamily: 'monospace', fontSize: '0.85rem', flex: 1 }}>{m}</Typography>
                      <Chip size="small" label={detectProvider(m)} sx={{ height: 16, fontSize: '0.6rem' }} />
                      {pricing[m] && (
                        <Typography variant="caption" color="text.secondary">
                          {formatUsd(pricing[m].input_per_mtok)} / {formatUsd(pricing[m].output_per_mtok)} per 1M
                        </Typography>
                      )}
                    </Stack>
                  </MenuItem>
                ))}
                {modelOptions.others.length > 0 && (
                  <MenuItem disabled value="" sx={{ opacity: 1, fontSize: '0.7rem', color: 'text.secondary' }}>
                    All models LiteLLM recognizes
                  </MenuItem>
                )}
                {modelOptions.others.map((m) => (
                  <MenuItem key={m} value={m}>
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ width: '100%' }}>
                      <Typography sx={{ fontFamily: 'monospace', fontSize: '0.8rem', flex: 1 }}>{m}</Typography>
                      {pricing[m] && (
                        <Typography variant="caption" color="text.secondary">
                          {formatUsd(pricing[m].input_per_mtok)} / {formatUsd(pricing[m].output_per_mtok)} per 1M
                        </Typography>
                      )}
                    </Stack>
                  </MenuItem>
                ))}
              </TextField>
              {pricing[model] && (
                <Box sx={{ mt: 1.5, p: 1.5, bgcolor: 'background.paper', borderRadius: 1, border: 1, borderColor: 'divider' }}>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                    Pricing per 1M tokens (USD)
                  </Typography>
                  {renderPriceRow(model)}
                </Box>
              )}
            </Box>

            {/* API key fields — only the relevant provider's row is
                primary; the others stay visible but inactive-looking
                so the operator can rotate any of them without
                navigating away. */}
            <Box>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                API keys (leave blank to keep the existing value)
              </Typography>
              <Stack spacing={1}>
                <KeyField
                  label="Anthropic"
                  configured={settings.configured_keys.anthropic}
                  highlight={targetProvider === 'anthropic'}
                  value={anthropicKey}
                  onChange={setAnthropicKey}
                  show={showKeys}
                  toggleShow={() => setShowKeys((s) => !s)}
                />
                <KeyField
                  label="OpenAI"
                  configured={settings.configured_keys.openai}
                  highlight={targetProvider === 'openai'}
                  value={openaiKey}
                  onChange={setOpenaiKey}
                  show={showKeys}
                  toggleShow={() => setShowKeys((s) => !s)}
                />
                <KeyField
                  label="Gemini"
                  configured={settings.configured_keys.gemini}
                  highlight={targetProvider === 'gemini'}
                  value={geminiKey}
                  onChange={setGeminiKey}
                  show={showKeys}
                  toggleShow={() => setShowKeys((s) => !s)}
                />
              </Stack>
            </Box>

            {targetProvider === 'ollama' && (
              <TextField
                fullWidth
                size="small"
                label="Ollama API base"
                value={ollamaBase}
                onChange={(e) => setOllamaBase(e.target.value)}
                placeholder="http://host.docker.internal:11434"
                helperText="Reachable from inside the drift-agent container."
              />
            )}
          </Stack>
        )}
      </DialogContent>

      <DialogActions>
        <Button onClick={onClose} size="small" disabled={saving}>
          Cancel
        </Button>
        <Button onClick={apply} variant="contained" size="small" disabled={loading || saving || !settings}>
          {saving ? 'Saving…' : 'Save + restart'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function KeyField({
  label,
  configured,
  highlight,
  value,
  onChange,
  show,
  toggleShow,
}: {
  label: string
  configured: boolean
  highlight: boolean
  value: string
  onChange: (v: string) => void
  show: boolean
  toggleShow: () => void
}) {
  return (
    <TextField
      size="small"
      label={label}
      type={show ? 'text' : 'password'}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={configured ? '••• existing value •••' : '(not configured)'}
      InputProps={{
        endAdornment: (
          <InputAdornment position="end">
            <Tooltip title={show ? 'Hide' : 'Show'}>
              <IconButton size="small" onClick={toggleShow}>
                {show ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
              </IconButton>
            </Tooltip>
          </InputAdornment>
        ),
      }}
      sx={{
        ...(highlight && {
          '& .MuiOutlinedInput-notchedOutline': { borderColor: 'primary.main' },
        }),
      }}
    />
  )
}
