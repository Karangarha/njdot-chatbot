'use client'

import { useEffect, useRef, useState } from 'react'
import MarkdownAnswer from '@/components/MarkdownAnswer'
import PDFViewerModal from '@/components/PDFViewerModal'
import { authHeaders } from '@/lib/api'

// ── Types ──────────────────────────────────────────────────────────────────────

interface Activity {
  taskId:  string | null
  name:    string | null
  start:   string | null
  finish:  string | null
}

interface Source {
  label:       string
  heading?:    string
  page_pdf?:   number
  section_id?: string
  similarity?: number
  doc_type?:   string
  activities?: Activity[]
}

interface Message {
  role:    'user' | 'assistant'
  content: string
  sources?: Source[]
}

interface ProgressEvent {
  status:      'queued' | 'parsing' | 'embedding' | 'storing' | 'ready' | 'error' | 'unknown'
  message:     string
  step?:       number
  total?:      number
  chunk_count?: number
}

interface SessionChatProps {
  sessionId:  string
  apiBase:    string
  authToken?: string
}

// Whether a citation can open the PDF viewer -- kept as one predicate so the
// pill's clickability and the modal-render guard can never drift apart.
const isPdfLinkable = (source: Source): boolean =>
  source.page_pdf != null && !!source.doc_type

// ── Progress bar ───────────────────────────────────────────────────────────────

function ProgressBar({ step, total }: { step?: number; total?: number }) {
  if (!total) return null
  const pct = Math.round(((step ?? 0) / total) * 100)
  return (
    <div className="mt-2 h-1.5 w-full rounded-full bg-[#1B3A6B]/10">
      <div
        className="h-1.5 rounded-full bg-[#1B3A6B] transition-all duration-300"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

// ── Source pill ────────────────────────────────────────────────────────────────

function SourcePill({ source, onOpenPdf }: { source: Source; onOpenPdf: (s: Source) => void }) {
  const tagColors: Record<string, string> = {
    'Designer Narrative':             'bg-blue-100 text-blue-700',
    'Special Provision':              'bg-purple-100 text-purple-700',
    'Key Map':                        'bg-teal-100 text-teal-700',
    'Estimate':                       'bg-rose-100 text-rose-700',
    'Schedule Activities':            'bg-amber-100 text-amber-700',
    'Construction Scheduling Manual': 'bg-green-100 text-green-700',
  }
  const color = tagColors[source.label] ?? 'bg-gray-100 text-gray-600'
  const clickable = isPdfLinkable(source)
  const detail = source.section_id
    ? source.section_id
    : source.heading
    ? source.heading
    : source.page_pdf != null
    ? `p.${source.page_pdf}`
    : ''

  if (!clickable) {
    return (
      <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${color}`}>
        {source.label}{detail ? ` · ${detail}` : ''}
      </span>
    )
  }

  return (
    <button
      type="button"
      onClick={() => onOpenPdf(source)}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${color} cursor-pointer hover:opacity-80`}
    >
      {source.label}{detail ? ` · ${detail}` : ''}
    </button>
  )
}

// ── Activity list ──────────────────────────────────────────────────────────────

function ActivityList({ source }: { source: Source }) {
  if (!source.activities?.length) return null
  return (
    <div className="rounded-lg px-2 py-1.5 text-[10px] bg-amber-50 text-amber-800">
      <div className="font-semibold uppercase tracking-wide mb-0.5">{source.label}</div>
      <ul className="space-y-0.5">
        {source.activities.map((a, i) => (
          <li key={a.taskId ?? i}>
            {a.taskId ?? '—'} · {a.name ?? '—'}
            {(a.start || a.finish) && ` · ${a.start ?? '?'} → ${a.finish ?? '?'}`}
          </li>
        ))}
      </ul>
    </div>
  )
}

// ── Message bubble ─────────────────────────────────────────────────────────────

function MessageBubble({ msg, onOpenPdf }: { msg: Message; onOpenPdf: (s: Source) => void }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[85%] ${isUser ? 'items-end' : 'items-start'} flex flex-col gap-1.5`}>
        <div
          className={`rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
            isUser
              ? 'bg-[#1B3A6B] text-white rounded-br-sm'
              : 'bg-white text-gray-800 shadow-sm ring-1 ring-black/5 rounded-bl-sm'
          }`}
        >
          {isUser ? msg.content : <MarkdownAnswer content={msg.content} />}
        </div>
        {msg.sources && msg.sources.length > 0 && (
          <div className="flex flex-wrap gap-1 px-1">
            {msg.sources.map((s, i) => (
              s.activities?.length ? (
                <ActivityList key={i} source={s} />
              ) : (
                <SourcePill key={i} source={s} onOpenPdf={onOpenPdf} />
              )
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function SessionChat({ sessionId, apiBase, authToken }: SessionChatProps) {
  const [progress,         setProgress]         = useState<ProgressEvent | null>(null)
  const [messages,         setMessages]         = useState<Message[]>([])
  const [historyLoading,   setHistoryLoading]   = useState(false)
  const [input,            setInput]            = useState('')
  const [isLoading,        setIsLoading]        = useState(false)
  const [error,            setError]            = useState<string | null>(null)
  const [pdfSource,        setPdfSource]        = useState<Source | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLTextAreaElement>(null)
  const authTokenRef = useRef(authToken)
  useEffect(() => { authTokenRef.current = authToken }, [authToken])

  // ── SSE: stream ingestion progress ────────────────────────────────────────
  useEffect(() => {
    if (!sessionId) return

    const url = `${apiBase}/api/session/status/${sessionId}`
    const es  = new EventSource(url)

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as ProgressEvent
        setProgress(data)
        if (data.status === 'ready' || data.status === 'error') {
          es.close()
        }
      } catch { /* ignore parse errors */ }
    }

    es.onerror = () => es.close()

    return () => es.close()
  }, [sessionId, apiBase])

  // ── Load message history when session becomes ready ────────────────────────
  // Reads authTokenRef (not the authToken prop) so a later token refresh
  // doesn't re-trigger this effect and clobber messages sent since the
  // initial load -- this should run once per session, not on every token
  // change.
  useEffect(() => {
    if (!sessionId || progress?.status !== 'ready') return

    setHistoryLoading(true)
    fetch(`${apiBase}/api/session/messages/${sessionId}`, { headers: authHeaders(authTokenRef.current) })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const loaded: Message[] = (data.messages ?? []).map((m: any) => ({
          role:    m.role,
          content: m.content,
          sources: m.sources ?? [],
        }))
        if (loaded.length > 0) setMessages(loaded)
      })
      .catch(() => { /* history load failure is non-fatal */ })
      .finally(() => setHistoryLoading(false))
  }, [sessionId, progress?.status, apiBase])

  // ── Auto-scroll on new message ─────────────────────────────────────────────
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  // ── Send question ──────────────────────────────────────────────────────────
  const sendQuestion = async () => {
    const q = input.trim()
    if (!q || isLoading || progress?.status !== 'ready') return

    setMessages(prev => [...prev, { role: 'user', content: q }])
    setInput('')
    setIsLoading(true)
    setError(null)

    try {
      const headers: HeadersInit = { 'Content-Type': 'application/json', ...authHeaders(authToken) }

      const res = await fetch(`${apiBase}/api/session/query`, {
        method:  'POST',
        headers,
        body:    JSON.stringify({ question: q, session_id: sessionId }),
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `Request failed (${res.status})`)
      }

      const data = await res.json()
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: data.answer, sources: data.sources ?? [] },
      ])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred.')
    } finally {
      setIsLoading(false)
      inputRef.current?.focus()
    }
  }

  const handleKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendQuestion()
    }
  }

  const isReady = progress?.status === 'ready'

  // ── Processing state ───────────────────────────────────────────────────────
  if (!isReady) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
        {progress?.status === 'error' ? (
          <>
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-red-100">
              <svg className="h-6 w-6 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </div>
            <div>
              <p className="font-semibold text-red-700">Processing failed</p>
              <p className="mt-1 text-xs text-gray-500">{progress.message}</p>
            </div>
          </>
        ) : (
          <>
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[#1B3A6B]/10">
              <svg className="h-6 w-6 animate-spin text-[#1B3A6B]" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            </div>
            <div className="w-full max-w-xs">
              <p className="text-sm font-medium text-[#1B3A6B]">
                {progress?.message ?? 'Preparing documents…'}
              </p>
              <ProgressBar step={progress?.step} total={progress?.total} />
            </div>
            <p className="text-xs text-gray-400">
              You can review compliance results above while documents are being prepared.
            </p>
          </>
        )}
      </div>
    )
  }

  // ── Ready state: chat UI ───────────────────────────────────────────────────
  return (
    <div className="flex h-full flex-col">

      {/* Source legend */}
      <div className="flex flex-wrap gap-1.5 border-b border-[#E8E8E8] bg-[#F5F5F5] px-4 py-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mr-1">Sources:</span>
        {[
          { label: 'Designer Narrative',             color: 'bg-blue-100 text-blue-700' },
          { label: 'Special Provision',              color: 'bg-purple-100 text-purple-700' },
          { label: 'Key Map',                        color: 'bg-teal-100 text-teal-700' },
          { label: 'Estimate',                       color: 'bg-rose-100 text-rose-700' },
          { label: 'Schedule Activities',            color: 'bg-amber-100 text-amber-700' },
          { label: 'Construction Scheduling Manual', color: 'bg-green-100 text-green-700' },
        ].map(s => (
          <span key={s.label} className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${s.color}`}>
            {s.label}
          </span>
        ))}
        <span className="ml-auto text-[10px] text-gray-400">
          {progress.chunk_count} chunks indexed
        </span>
      </div>

      {/* Message thread */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {historyLoading && (
          <div className="flex justify-center py-4">
            <span className="text-[11px] text-gray-400">Loading previous messages…</span>
          </div>
        )}
        {!historyLoading && messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-center py-10">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#1B3A6B]/10">
              <svg className="h-5 w-5 text-[#1B3A6B]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
              </svg>
            </div>
            <div>
              <p className="text-sm font-semibold text-gray-700">Documents ready</p>
              <p className="mt-0.5 text-xs text-gray-400">
                Ask anything about the narrative, special provisions, key map, or schedule.
              </p>
            </div>
          </div>
        )}
        {messages.map((msg, i) => (
          <MessageBubble key={i} msg={msg} onOpenPdf={setPdfSource} />
        ))}
        {isLoading && (
          <div className="flex justify-start">
            <div className="rounded-2xl rounded-bl-sm bg-white px-4 py-3 shadow-sm ring-1 ring-black/5">
              <div className="flex gap-1">
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:0ms]" />
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:150ms]" />
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:300ms]" />
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-4 mb-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Input row */}
      <div className="border-t border-[#E8E8E8] bg-white px-4 py-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Ask about the narrative, special provisions, or schedule…"
            rows={2}
            className="flex-1 resize-none rounded-xl border border-[#E8E8E8] bg-[#F5F5F5] px-3 py-2 text-sm text-gray-800 placeholder-gray-400 outline-none focus:border-[#1B3A6B]/40 focus:ring-2 focus:ring-[#1B3A6B]/10"
          />
          <button
            onClick={sendQuestion}
            disabled={!input.trim() || isLoading}
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors ${
              input.trim() && !isLoading
                ? 'bg-[#1B3A6B] text-white hover:bg-[#12264a]'
                : 'bg-gray-100 text-gray-300 cursor-not-allowed'
            }`}
            aria-label="Send"
          >
            <svg className="h-4 w-4 rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </button>
        </div>
        <p className="mt-1.5 text-[10px] text-gray-400">Enter to send · Shift+Enter for new line</p>
      </div>

      {/* PDF Viewer Modal */}
      {pdfSource && isPdfLinkable(pdfSource) && (
        <PDFViewerModal
          url={`${apiBase}/api/review/${sessionId}/pdf/${pdfSource.doc_type}`}
          page={pdfSource.page_pdf}
          authToken={authToken}
          requireAuth
          headerLabel={pdfSource.label}
          headerDetail={pdfSource.section_id ?? pdfSource.heading}
          onClose={() => setPdfSource(null)}
        />
      )}
    </div>
  )
}
