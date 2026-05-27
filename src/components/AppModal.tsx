import {
  ChangeEvent,
  DragEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import CloseIcon from '@mui/icons-material/Close'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import DownloadIcon from '@mui/icons-material/Download'
import EditIcon from '@mui/icons-material/Edit'
import UploadFileIcon from '@mui/icons-material/UploadFile'
import { Menu, MenuItem } from '@mui/material'
import { deployApi } from '../lib/deployApi'
import { deployApiBase } from '../lib/apiBase'

export type AppModalMode =
  | { kind: 'create' }
  | { kind: 'edit'; appName: string }

type FileEntry = { name: string; content: string }

const COMPOSE_NAMES = ['compose.yaml', 'compose.yml', 'docker-compose.yml'] as const

const STARTER_FILES: FileEntry[] = [
  {
    name: 'compose.yaml',
    content: `services:
  example:
    image: hashicorp/http-echo:latest
    command: ["-text=hello from drift"]
    ports:
      - "9999:5678"
    restart: unless-stopped
`,
  },
]

export function AppModal({
  open,
  mode,
  onClose,
  onSaved,
}: {
  open: boolean
  mode: AppModalMode
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState('')
  const [files, setFiles] = useState<FileEntry[]>([])
  const [activeTab, setActiveTab] = useState(0)
  const [renaming, setRenaming] = useState<number | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadingFiles, setLoadingFiles] = useState(false)
  // Current revision metadata in edit mode (loaded with files). null in
  // create mode and during the load. Drives the "Editing v3" subtitle.
  const [currentVersion, setCurrentVersion] = useState<number | null>(null)
  // Anchor element for the download-format dropdown (tar.gz / zip).
  const [downloadMenuAnchor, setDownloadMenuAnchor] = useState<HTMLElement | null>(null)
  const startDownload = (format: 'tar.gz' | 'zip') => {
    if (mode.kind !== 'edit' || currentVersion == null) return
    // Trigger a normal browser download — the server's
    // Content-Disposition: attachment + filename will save with the
    // {app}-v{ver}-{ts} name automatically. <a download> is the
    // cleanest cross-browser path.
    const a = document.createElement('a')
    a.href =
      `${deployApiBase()}/apps/${encodeURIComponent(mode.appName)}` +
      `/revisions/${currentVersion}/download?format=${format}`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setDownloadMenuAnchor(null)
  }
  const filePickerRef = useRef<HTMLInputElement | null>(null)

  // Reset state when the modal opens/closes so create after edit doesn't
  // leak the prior app's files.
  useEffect(() => {
    if (!open) return
    setError(null)
    setLoading(false)
    setRenaming(null)
    setActiveTab(0)
    if (mode.kind === 'create') {
      setName('')
      setFiles(STARTER_FILES.map((f) => ({ ...f })))
      setCurrentVersion(null)
    } else {
      // Edit: fetch the latest revision's files into the editor.
      setName(mode.appName)
      setFiles([])
      setCurrentVersion(null)
      setLoadingFiles(true)
      deployApi
        .getRevision(mode.appName, 'latest')
        .then((rev) => {
          setCurrentVersion(rev.version)
          const entries = Object.entries(rev.files).map(([n, c]) => ({ name: n, content: c }))
          // Stable ordering: compose first, then alphabetical.
          entries.sort((a, b) => {
            const aIsCompose = COMPOSE_NAMES.includes(a.name as (typeof COMPOSE_NAMES)[number])
            const bIsCompose = COMPOSE_NAMES.includes(b.name as (typeof COMPOSE_NAMES)[number])
            if (aIsCompose && !bIsCompose) return -1
            if (!aIsCompose && bIsCompose) return 1
            return a.name.localeCompare(b.name)
          })
          setFiles(entries)
        })
        .catch((e: Error) => setError(`Could not load app: ${e.message}`))
        .finally(() => setLoadingFiles(false))
    }
  }, [open, mode])

  const hasCompose = useMemo(
    () => files.some((f) => COMPOSE_NAMES.includes(f.name as (typeof COMPOSE_NAMES)[number])),
    [files],
  )

  const canSubmit =
    !loading &&
    !loadingFiles &&
    files.length > 0 &&
    hasCompose &&
    files.every((f) => f.name.trim().length > 0) &&
    (mode.kind === 'edit' || name.trim().length > 0)

  const addFile = (entry: FileEntry) => {
    setFiles((prev) => {
      // Replace by filename rather than appending duplicates — matches what
      // a real file copy would do.
      const existingIdx = prev.findIndex((f) => f.name === entry.name)
      if (existingIdx >= 0) {
        const next = prev.slice()
        next[existingIdx] = entry
        return next
      }
      return [...prev, entry]
    })
  }

  const handleDrop = useCallback(async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setDragOver(false)
    const dropped = Array.from(e.dataTransfer.files)
    for (const f of dropped) {
      // Permissive size cap so a stray binary doesn't lock the UI. A real
      // compose / config file is well under 256KB.
      if (f.size > 256 * 1024) {
        setError(`File ${f.name} is too large (${(f.size / 1024).toFixed(0)} KB > 256 KB cap).`)
        continue
      }
      try {
        const text = await f.text()
        addFile({ name: f.name, content: text })
      } catch {
        setError(`Failed to read file: ${f.name}`)
      }
    }
    // Activate the last added tab if anything came through.
    setActiveTab((prev) => Math.max(prev, 0))
  }, [])

  const handlePicker = async (e: ChangeEvent<HTMLInputElement>) => {
    const fl = e.target.files
    if (!fl) return
    for (const f of Array.from(fl)) {
      if (f.size > 256 * 1024) {
        setError(`File ${f.name} is too large.`)
        continue
      }
      const text = await f.text()
      addFile({ name: f.name, content: text })
    }
    e.target.value = ''
  }

  const handleAddBlank = () => {
    // Pick a unique filename so the new tab is distinguishable.
    let n = 1
    let candidate = 'new-file.txt'
    while (files.some((f) => f.name === candidate)) {
      candidate = `new-file-${++n}.txt`
    }
    addFile({ name: candidate, content: '' })
    setActiveTab(files.length)
    // Jump straight into rename mode so the user names the file before typing.
    setRenaming(files.length)
    setRenameDraft(candidate)
  }

  const handleRemoveFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx))
    setActiveTab((prev) => (prev >= idx && prev > 0 ? prev - 1 : prev))
  }

  const commitRename = (idx: number) => {
    const target = renameDraft.trim()
    if (!target) {
      setRenaming(null)
      return
    }
    if (files.some((f, i) => i !== idx && f.name === target)) {
      setError(`A file named '${target}' already exists in the bundle.`)
      return
    }
    setFiles((prev) => prev.map((f, i) => (i === idx ? { ...f, name: target } : f)))
    setRenaming(null)
  }

  const handleSubmit = async () => {
    setLoading(true)
    setError(null)
    const fileMap: Record<string, string> = {}
    for (const f of files) fileMap[f.name] = f.content
    try {
      if (mode.kind === 'create') {
        await deployApi.createApp(name.trim())
        await deployApi.createRevision(name.trim(), fileMap)
      } else {
        await deployApi.createRevision(mode.appName, fileMap)
      }
      onSaved()
      onClose()
    } catch (e) {
      const msg = (e as Error).message
      setError(msg)
      setLoading(false)
    }
  }

  const updateFileContent = (idx: number, content: string) => {
    setFiles((prev) => prev.map((f, i) => (i === idx ? { ...f, content } : f)))
  }

  return (
    <Dialog open={open} onClose={loading ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="baseline" spacing={1}>
            <span>{mode.kind === 'create' ? 'New app' : `Edit ${mode.appName}`}</span>
            {mode.kind === 'edit' && currentVersion !== null && (
              <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.75rem' }}>
                editing v{currentVersion} · save creates v{currentVersion + 1}
              </Typography>
            )}
          </Stack>
          <Stack direction="row" alignItems="center" spacing={0.5}>
            {mode.kind === 'edit' && currentVersion !== null && (
              <>
                <IconButton
                  size="small"
                  title="Download this revision"
                  onClick={(e) => setDownloadMenuAnchor(e.currentTarget)}
                  disabled={loading || loadingFiles}
                >
                  <DownloadIcon fontSize="small" />
                </IconButton>
                <Menu
                  anchorEl={downloadMenuAnchor}
                  open={downloadMenuAnchor !== null}
                  onClose={() => setDownloadMenuAnchor(null)}
                >
                  <MenuItem onClick={() => startDownload('tar.gz')}>tar.gz</MenuItem>
                  <MenuItem onClick={() => startDownload('zip')}>zip</MenuItem>
                </Menu>
              </>
            )}
            <IconButton size="small" onClick={onClose} disabled={loading}>
              <CloseIcon fontSize="small" />
            </IconButton>
          </Stack>
        </Stack>
      </DialogTitle>

      <DialogContent dividers>
        {mode.kind === 'create' && (
          <TextField
            label="App name"
            placeholder="e.g. reporter, podnot, hello-world"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={loading}
            fullWidth
            size="small"
            sx={{ mb: 2 }}
            helperText="Lower-case letters, digits, hyphens. Must be unique across the fleet."
          />
        )}

        {loadingFiles ? (
          <Box sx={{ py: 6, display: 'flex', justifyContent: 'center' }}>
            <CircularProgress size={24} />
          </Box>
        ) : (
          <>
            <Box
              onDragOver={(e) => {
                e.preventDefault()
                setDragOver(true)
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              sx={{
                border: 2,
                borderStyle: 'dashed',
                borderColor: dragOver ? 'primary.main' : 'divider',
                borderRadius: 1,
                p: 1.5,
                mb: 2,
                bgcolor: dragOver ? 'rgba(99, 102, 241, 0.05)' : 'transparent',
                transition: 'all 120ms ease',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 1,
              }}
            >
              <Stack direction="row" spacing={1} alignItems="center" sx={{ color: 'text.secondary' }}>
                <UploadFileIcon fontSize="small" />
                <Typography variant="body2">
                  Drop files here, or pick / add manually
                </Typography>
              </Stack>
              <Stack direction="row" spacing={1}>
                <input
                  ref={filePickerRef}
                  type="file"
                  hidden
                  multiple
                  onChange={handlePicker}
                />
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => filePickerRef.current?.click()}
                  disabled={loading}
                  sx={{ textTransform: 'none' }}
                >
                  Choose files
                </Button>
                <Button
                  size="small"
                  variant="outlined"
                  onClick={handleAddBlank}
                  startIcon={<AddIcon fontSize="small" />}
                  disabled={loading}
                  sx={{ textTransform: 'none' }}
                >
                  Blank
                </Button>
              </Stack>
            </Box>

            {files.length === 0 ? (
              <Alert severity="info" variant="outlined">
                No files yet. Drop your <code>compose.yaml</code> (and any referenced files like
                <code> .env </code>or <code>prometheus.yml</code>) into the box above.
              </Alert>
            ) : (
              <>
                <Tabs
                  value={Math.min(activeTab, files.length - 1)}
                  onChange={(_, v) => setActiveTab(v)}
                  variant="scrollable"
                  scrollButtons="auto"
                  sx={{ borderBottom: 1, borderColor: 'divider', minHeight: 36 }}
                >
                  {files.map((f, i) => (
                    <Tab
                      key={`${i}-${f.name}`}
                      sx={{ textTransform: 'none', minHeight: 36, py: 0.4 }}
                      label={
                        renaming === i ? (
                          <TextField
                            value={renameDraft}
                            onChange={(e) => setRenameDraft(e.target.value)}
                            onClick={(e) => e.stopPropagation()}
                            onBlur={() => commitRename(i)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') {
                                e.preventDefault()
                                commitRename(i)
                              } else if (e.key === 'Escape') {
                                setRenaming(null)
                              }
                            }}
                            size="small"
                            autoFocus
                            sx={{ width: 200, '& input': { py: 0.2 } }}
                          />
                        ) : (
                          <Stack direction="row" alignItems="center" spacing={0.6}>
                            <span>{f.name}</span>
                            <Tooltip title="Rename">
                              <IconButton
                                size="small"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  setRenaming(i)
                                  setRenameDraft(f.name)
                                  setActiveTab(i)
                                }}
                                sx={{ p: 0.2 }}
                              >
                                <EditIcon sx={{ fontSize: 12 }} />
                              </IconButton>
                            </Tooltip>
                            <Tooltip title="Remove from bundle">
                              <IconButton
                                size="small"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleRemoveFile(i)
                                }}
                                sx={{ p: 0.2 }}
                              >
                                <DeleteOutlineIcon sx={{ fontSize: 14 }} />
                              </IconButton>
                            </Tooltip>
                          </Stack>
                        )
                      }
                    />
                  ))}
                </Tabs>

                {files[activeTab] && (
                  <TextField
                    multiline
                    minRows={14}
                    maxRows={28}
                    fullWidth
                    value={files[activeTab].content}
                    onChange={(e) => updateFileContent(activeTab, e.target.value)}
                    disabled={loading}
                    InputProps={{
                      sx: {
                        fontFamily: '"JetBrains Mono", "SF Mono", ui-monospace, monospace',
                        fontSize: '0.84rem',
                        mt: 1,
                      },
                    }}
                  />
                )}
              </>
            )}

            {!hasCompose && files.length > 0 && (
              <Alert severity="warning" variant="outlined" sx={{ mt: 2 }}>
                The bundle must contain a compose file: <code>compose.yaml</code>,{' '}
                <code>compose.yml</code>, or <code>docker-compose.yml</code>. Add one before saving.
              </Alert>
            )}
          </>
        )}

        {error && (
          <Alert severity="error" variant="outlined" sx={{ mt: 2 }}>
            {error}
          </Alert>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose} disabled={loading} sx={{ textTransform: 'none' }}>
          Cancel
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={!canSubmit}
          variant="contained"
          disableElevation
          sx={{ textTransform: 'none' }}
          startIcon={loading ? <CircularProgress size={14} color="inherit" /> : null}
        >
          {loading
            ? 'Saving…'
            : mode.kind === 'create'
              ? 'Create v1'
              : 'Save as new revision'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
