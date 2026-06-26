import { useCallback, useEffect, useState } from 'react'
import {
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  List,
  ListItem,
  ListItemText,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import TelegramIcon from '@mui/icons-material/Telegram'
import { apiBase } from '../lib/apiBase'

const API_BASE = apiBase()

type LinkCodePayload = {
  code: string
  expires_at: string
  expires_min: number
  deep_link?: string
  qr_data_uri?: string
}

type LinkedChat = {
  chat_id: string
  title: string | null
  linked_at: string
}

export function TelegramLinkModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const [code, setCode] = useState<LinkCodePayload | null>(null)
  const [issueLoading, setIssueLoading] = useState(false)
  const [issueError, setIssueError] = useState<string | null>(null)

  const [chats, setChats] = useState<LinkedChat[]>([])
  const [chatsLoading, setChatsLoading] = useState(false)
  const [chatsError, setChatsError] = useState<string | null>(null)

  const refreshChats = useCallback(async () => {
    setChatsLoading(true)
    setChatsError(null)
    try {
      const res = await fetch(`${API_BASE}/telegram/chats`, { credentials: 'include' })
      if (!res.ok) {
        const body = await res.text().catch(() => '')
        // 503 = feature disabled. Surface a friendly message instead of an error chip.
        if (res.status === 503) {
          throw new Error(
            'Telegram feature is not enabled on this CP — admin needs to set TELEGRAM_BOT_TOKEN.',
          )
        }
        throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
      }
      const rows = (await res.json()) as LinkedChat[]
      setChats(rows)
    } catch (e) {
      setChatsError((e as Error).message)
    } finally {
      setChatsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    void refreshChats()
    // Poll while open so a freshly redeemed code appears here without
    // the user having to close + reopen.
    const t = setInterval(refreshChats, 4000)
    return () => clearInterval(t)
  }, [open, refreshChats])

  // Drop the code when the modal closes so reopening starts fresh.
  useEffect(() => {
    if (!open) {
      setCode(null)
      setIssueError(null)
    }
  }, [open])

  const issueCode = async () => {
    setIssueLoading(true)
    setIssueError(null)
    try {
      const res = await fetch(`${API_BASE}/telegram/link/code`, {
        method: 'POST',
        credentials: 'include',
      })
      if (!res.ok) {
        const body = await res.text().catch(() => '')
        if (res.status === 503) {
          throw new Error(
            'Telegram feature is not enabled on this CP — admin needs to set TELEGRAM_BOT_TOKEN.',
          )
        }
        throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
      }
      setCode((await res.json()) as LinkCodePayload)
    } catch (e) {
      setIssueError((e as Error).message)
    } finally {
      setIssueLoading(false)
    }
  }

  const onCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // Insecure-context or browser refusal — no-op; user can long-press
      // the code text instead.
    }
  }

  const onUnlink = async (chat_id: string) => {
    try {
      const res = await fetch(`${API_BASE}/telegram/chats/${encodeURIComponent(chat_id)}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!res.ok && res.status !== 404) {
        const body = await res.text().catch(() => '')
        throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
      }
      await refreshChats()
    } catch (e) {
      setChatsError((e as Error).message)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="sm"
      fullWidth
      PaperProps={{ sx: { borderRadius: 1.5 } }}
    >
      <DialogTitle sx={{ pr: 1 }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="center" spacing={1}>
            <TelegramIcon fontSize="small" />
            <Typography variant="body1" sx={{ fontWeight: 600 }}>
              Link a Telegram chat
            </Typography>
          </Stack>
          <IconButton size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
        <Typography variant="caption" color="text.secondary">
          Bind a Telegram chat to your Drift account so you can ask the agent
          questions and receive alerts from your phone.
        </Typography>
      </DialogTitle>

      <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        {/* New-code section */}
        <Box>
          <Typography variant="subtitle2" gutterBottom>
            Generate a link code
          </Typography>

          {!code && (
            <Stack direction="row" spacing={1} alignItems="center">
              <Button
                variant="contained"
                size="small"
                onClick={issueCode}
                disabled={issueLoading}
                startIcon={issueLoading ? <CircularProgress size={14} /> : <TelegramIcon />}
              >
                {issueLoading ? 'generating…' : 'Generate code'}
              </Button>
              {issueError && (
                <Typography variant="caption" color="error">
                  {issueError}
                </Typography>
              )}
            </Stack>
          )}

          {code && (
            <Stack
              direction={{ xs: 'column', sm: 'row' }}
              spacing={2}
              alignItems={{ xs: 'stretch', sm: 'flex-start' }}
            >
              {/* QR */}
              {code.qr_data_uri && (
                <Box
                  component="img"
                  src={code.qr_data_uri}
                  alt="Telegram link QR code"
                  sx={{
                    width: 200,
                    height: 200,
                    borderRadius: 1,
                    bgcolor: 'common.white',
                    p: 1,
                    flexShrink: 0,
                    alignSelf: { xs: 'center', sm: 'flex-start' },
                  }}
                />
              )}
              <Stack spacing={1} sx={{ flex: 1, minWidth: 0 }}>
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    Code (or scan the QR)
                  </Typography>
                  <Stack direction="row" alignItems="center" spacing={0.5}>
                    <Typography
                      sx={{
                        fontFamily: 'monospace',
                        fontWeight: 700,
                        letterSpacing: 2,
                        fontSize: 24,
                      }}
                    >
                      {code.code}
                    </Typography>
                    <Tooltip title="Copy code">
                      <IconButton size="small" onClick={() => onCopy(code.code)}>
                        <ContentCopyIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </Stack>
                  <Typography variant="caption" color="text.secondary">
                    Expires in {code.expires_min} min.
                  </Typography>
                </Box>

                {code.deep_link && (
                  <Button
                    variant="outlined"
                    size="small"
                    component="a"
                    href={code.deep_link}
                    target="_blank"
                    rel="noopener noreferrer"
                    startIcon={<OpenInNewIcon />}
                  >
                    Open in Telegram
                  </Button>
                )}

                <Typography variant="caption" color="text.secondary">
                  Or message the bot manually with: <code>/link {code.code}</code>
                </Typography>
              </Stack>
            </Stack>
          )}
        </Box>

        {/* Linked chats */}
        <Box>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Typography variant="subtitle2">Your linked chats</Typography>
            {chatsLoading && <CircularProgress size={12} />}
          </Stack>
          {chatsError && (
            <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
              {chatsError}
            </Typography>
          )}
          {!chatsLoading && chats.length === 0 && !chatsError && (
            <Typography variant="caption" color="text.secondary">
              No linked chats yet — generate a code above to bind one.
            </Typography>
          )}
          <List dense disablePadding>
            {chats.map((c) => (
              <ListItem
                key={c.chat_id}
                disableGutters
                sx={{ borderTop: 1, borderColor: 'divider', py: 0.5 }}
                secondaryAction={
                  <Tooltip title="Unlink chat">
                    <IconButton size="small" onClick={() => onUnlink(c.chat_id)}>
                      <DeleteOutlineIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                }
              >
                <ListItemText
                  primary={c.title || `chat ${c.chat_id}`}
                  secondary={`linked ${new Date(c.linked_at).toLocaleString()}`}
                  primaryTypographyProps={{ sx: { fontSize: '0.86rem' } }}
                  secondaryTypographyProps={{ sx: { fontSize: '0.7rem' } }}
                />
              </ListItem>
            ))}
          </List>
        </Box>

        <Typography variant="caption" color="text.secondary">
          Need the bot username? It'll appear in the deep link above the first time
          the bot reaches Telegram.{' '}
          {/* `Link` to existing docs would go here once we add a Telegram setup page. */}
          {(!code?.deep_link && code) && (
            <Box component="span" sx={{ color: 'warning.main' }}>
              Bot username couldn't be resolved — check TELEGRAM_BOT_TOKEN on the CP.
            </Box>
          )}
        </Typography>
      </DialogContent>
    </Dialog>
  )
}
