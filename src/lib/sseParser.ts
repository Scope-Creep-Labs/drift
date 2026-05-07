export type SseFrame = { event: string; data: string }

/**
 * Parse a Server-Sent Events stream. Yields one frame per `\n\n` boundary.
 * Multi-line `data:` fields are joined with `\n` per spec.
 */
export async function* parseSSE(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): AsyncGenerator<SseFrame> {
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) {
      if (buf.trim()) {
        const frame = parseChunk(buf)
        if (frame) yield frame
      }
      return
    }
    buf += decoder.decode(value, { stream: true })

    let idx: number
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const frame = parseChunk(chunk)
      if (frame) yield frame
    }
  }
}

function parseChunk(chunk: string): SseFrame | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const rawLine of chunk.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) {
      const v = line.slice(5)
      dataLines.push(v.startsWith(' ') ? v.slice(1) : v)
    }
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}
