'use client'

import { KeyboardEvent, useEffect, useRef, useState } from 'react'
import Image from 'next/image'
import { useRouter } from 'next/navigation'
import { askQuestion } from '@/lib/api'
import { createClient } from '@/lib/supabase/client'
import type { BDCAlertItem, CitationItem, Conversation } from '@/lib/types'
import DocumentReview from '@/components/review/DocumentReview'

// ── Local types ────────────────────────────────────────────────────────────────

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations?: CitationItem[]
  bdc_alerts?: BDCAlertItem[]
  query_type?: string
  response_time_ms?: number
  isError?: boolean
}

interface ChatInterfaceProps {
  userId?: string
  userEmail?: string
}

// ── Constants ──────────────────────────────────────────────────────────────────

const SUGGESTED_QUESTIONS = [
  {
    title: 'Working Day Definition',
    question: "What is the official definition of a 'Working Day' in the 2019 book? Does it include Saturdays or holidays?",
  },
  {
    title: 'Contract Execution Timeline',
    question: 'How many days does a contractor have to execute the contract after award?',
  },
  {
    title: 'RCA Concrete Composition',
    question: 'What is the minimum concrete percentage in RCA composition?',
  },
  {
    title: 'Density Verification',
    question: 'How is required density determined and what range must verification specimens meet?',
  },
] as const

const COLLECTIONS = [
  { label: 'All Documents',           value: '' },
  { label: 'Standard Specifications', value: 'specs_2019' },
  { label: 'Material Procedures',     value: 'material_procs' },
  { label: 'Construction Scheduling', value: 'scheduling' },
] as const

// ── Helpers ────────────────────────────────────────────────────────────────────

function relativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 2)  return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24)  return `${hrs}h ago`
  if (hrs < 48)  return 'Yesterday'
  const days = Math.floor(hrs / 24)
  if (days < 7)  return `${days}d ago`
  return new Date(dateStr).toLocaleDateString()
}

function getInitials(email?: string, meta?: { first_name?: string; last_name?: string }): string {
  if (meta?.first_name && meta?.last_name)
    return (meta.first_name[0] + meta.last_name[0]).toUpperCase()
  if (email) return email.slice(0, 2).toUpperCase()
  return '?'
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function ChatInterface({ userId, userEmail }: ChatInterfaceProps) {
  const [activeTab, setActiveTab]             = useState<'spec' | 'review'>('spec')
  const [sidebarOpen, setSidebarOpen]         = useState(false)
  const [userDropdownOpen, setUserDropdownOpen] = useState(false)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [userInitials, setUserInitials]       = useState(() => getInitials(userEmail))
  const [conversations, setConversations]     = useState<Conversation[]>([])
  const [currentConvId, setCurrentConvId]     = useState<string | null>(null)
  const [messages, setMessages]               = useState<Message[]>([])
  const [input, setInput]                     = useState('')
  const [collection, setCollection]           = useState('')
  const [isLoading, setIsLoading]             = useState(false)
  const [pdfModal, setPdfModal]               = useState<CitationItem | null>(null)
  const messagesEndRef                        = useRef<HTMLDivElement>(null)
  const router                                = useRouter()

  const latestAnswer = [...messages].reverse().find(m => m.role === 'assistant' && !m.isError)

  // ── Supabase helpers ─────────────────────────────────────────────────────────

  const loadConversations = async () => {
    if (!userId) return
    try {
      const sb = createClient()
      const thirtyDaysAgo = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString()
      const { data } = await sb
        .from('conversations')
        .select('id, title, updated_at')
        .gte('updated_at', thirtyDaysAgo)
        .order('updated_at', { ascending: false })
        .limit(10)
      if (data) setConversations(data as Conversation[])
    } catch { /* table may not exist yet */ }
  }

  const loadConversationHistory = async (convId: string) => {
    try {
      const sb = createClient()
      const { data } = await sb
        .from('messages')
        .select('id, role, content, citations, bdc_alerts, created_at')
        .eq('conversation_id', convId)
        .order('created_at')
      if (data) {
        setMessages(data.map(m => ({
          id:         m.id,
          role:       m.role as 'user' | 'assistant',
          content:    m.content,
          citations:  m.citations  ?? [],
          bdc_alerts: m.bdc_alerts ?? [],
        })))
        setCurrentConvId(convId)
      }
    } catch { /* silently ignore */ }
  }

  // ── Effects ───────────────────────────────────────────────────────────────────

  useEffect(() => {
    loadConversations()
    // Also get user metadata for better initials
    const sb = createClient()
    sb.auth.getUser().then(({ data }) => {
      if (data.user?.user_metadata) {
        setUserInitials(getInitials(userEmail, data.user.user_metadata as { first_name?: string; last_name?: string }))
      }
    })
  }, [userId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  useEffect(() => {
    if (!userDropdownOpen) return
    const close = (e: MouseEvent) => { if (!(e.target as Element).closest('#user-menu')) setUserDropdownOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [userDropdownOpen])

  // ── Handlers ──────────────────────────────────────────────────────────────────

  const handleNewChat = () => {
    setMessages([])
    setCurrentConvId(null)
    setInput('')
    setIsLoading(false)
    setPdfModal(null)
    setSidebarOpen(false)
    setUserDropdownOpen(false)
    setConfirmDeleteId(null)
    setActiveTab('spec')
  }

  const handleDeleteConversation = async (convId: string) => {
    const sb = createClient()
    await sb.from('conversations').delete().eq('id', convId).eq('user_id', userId ?? '')
    setConversations(prev => prev.filter(c => c.id !== convId))
    if (currentConvId === convId) {
      setMessages([])
      setCurrentConvId(null)
    }
    setConfirmDeleteId(null)
  }

  const handleSend = async (queryOverride?: string) => {
    const query = (queryOverride ?? input).trim()
    if (!query || isLoading) return

    const userMsg: Message = { id: crypto.randomUUID(), role: 'user', content: query }
    setMessages(prev => [...prev, userMsg])
    setInput('')
    setIsLoading(true)

    let convId = currentConvId
    const sb = createClient()

    try {
      // Create conversation on first message
      if (!convId && userId) {
        const title = query.slice(0, 40) + (query.length > 40 ? '…' : '')
        const { data: conv } = await sb
          .from('conversations')
          .insert({ user_id: userId, title })
          .select('id, created_at')
          .single()
        if (conv) {
          convId = conv.id
          setCurrentConvId(convId)
          // ── Optimistic sidebar update ──────────────────────────────────
          // Prepend immediately so RECENTS shows the new conversation
          // without waiting for the full LLM response to finish.
          const now = new Date().toISOString()
          setConversations(prev => [{
            id:         conv.id,
            title,
            created_at: (conv.created_at as string | null) ?? now,
            updated_at: now,
          }, ...prev])
        }
      }

      // Save user message
      if (convId) {
        await sb.from('messages').insert({
          conversation_id: convId, role: 'user',
          content: query, citations: [], bdc_alerts: [],
        })
      }

      // RAG query
      const data = await askQuestion(query, collection || undefined)

      // Save assistant message
      if (convId) {
        await sb.from('messages').insert({
          conversation_id: convId, role: 'assistant',
          content: data.answer,
          citations:  data.citations  ?? [],
          bdc_alerts: data.bdc_alerts ?? [],
        })
        // Bump updated_at so conversation floats to top of recents
        await sb.from('conversations')
          .update({ updated_at: new Date().toISOString() })
          .eq('id', convId)
      }

      setMessages(prev => [...prev, {
        id:               crypto.randomUUID(),
        role:             'assistant',
        content:          data.answer,
        citations:        data.citations,
        bdc_alerts:       data.bdc_alerts,
        query_type:       data.query_type,
        response_time_ms: data.response_time_ms,
      }])
    } catch (err) {
      setMessages(prev => [...prev, {
        id:      crypto.randomUUID(),
        role:    'assistant',
        content: err instanceof Error ? err.message : 'Something went wrong. Please try again.',
        isError: true,
      }])
    } finally {
      setIsLoading(false)
      await loadConversations()
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const handleSignOut = async () => {
    const sb = createClient()
    await sb.auth.signOut()
    router.push('/')
    router.refresh()
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-screen flex-col bg-[#F5F5F5]">

      {/* ══ TOP BAR ══════════════════════════════════════════════════════════════ */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-[#0F1E3C]/30 bg-[#1B2A4A] px-4 shadow-md z-10">

        {/* Left: logo + title */}
        <div className="flex items-center gap-3">
          <Image src="/njdot_logo.png" alt="NJDOT" width={30} height={30} className="shrink-0" />
          <span className="hidden font-semibold text-white sm:block text-sm">Department of Transportation</span>
        </div>

        {/* Center: tab navigation */}
        <nav className="flex items-center gap-1 rounded-xl bg-[#0F1E3C]/40 p-1">
          <button
            onClick={() => setActiveTab('spec')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition-all ${
              activeTab === 'spec'
                ? 'bg-white text-[#1B3A6B] shadow-sm'
                : 'text-white/60 hover:text-white'
            }`}
          >
            Smart Assistant
          </button>
          <button
            onClick={() => setActiveTab('review')}
            className={`rounded-lg px-4 py-1.5 text-xs font-semibold transition-all ${
              activeTab === 'review'
                ? 'bg-white text-[#1B3A6B] shadow-sm'
                : 'text-white/60 hover:text-white'
            }`}
          >
            Document Review
          </button>
        </nav>

        {/* Right: user avatar + dropdown */}
        <div id="user-menu" className="relative">
          <button
            onClick={() => setUserDropdownOpen(v => !v)}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-white text-[11px] font-bold text-[#1B2A4A] hover:opacity-85 transition-opacity"
            aria-label="User menu"
          >
            {userInitials}
          </button>
          {userDropdownOpen && (
            <div className="absolute right-0 top-10 z-30 w-52 rounded-xl border border-[#E8E8E8] bg-white py-1.5 shadow-xl">
              {userEmail && (
                <div className="border-b border-[#E8E8E8] px-4 pb-2 pt-1 mb-1">
                  <p className="truncate text-xs text-gray-500">{userEmail}</p>
                </div>
              )}
              <button
                onClick={handleSignOut}
                className="w-full px-4 py-2 text-left text-sm text-gray-700 hover:bg-gray-50 rounded-b-lg"
              >
                Sign Out
              </button>
            </div>
          )}
        </div>
      </header>

      {/* ══ BODY ═════════════════════════════════════════════════════════════════ */}
      <div className="flex min-h-0 flex-1 overflow-hidden relative">

        {/* ── Left-edge sidebar tab (visible only when sidebar is closed) ──────── */}
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            aria-label="Open sidebar"
            className="fixed left-0 z-40 flex items-center justify-center rounded-r-lg border border-l-0 border-[#E8E8E8] bg-white shadow-sm transition-colors hover:bg-gray-50"
            style={{ top: '50%', transform: 'translateY(-50%)', width: '22px', height: '48px' }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#1B3A6B"
                 strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <rect width="18" height="18" x="3" y="3" rx="2" />
              <path d="M9 3v18" />
            </svg>
          </button>
        )}

        {/* Sidebar backdrop */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 z-20 bg-black/30"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* ── SIDEBAR ─────────────────────────────────────────────────────────── */}
        <aside
          className={`
            flex w-[85vw] max-w-[280px] flex-col border-r border-[#E8E8E8] bg-white
            fixed top-14 bottom-0 left-0 z-30 shadow-xl transition-transform duration-200 ease-out
            ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          `}
        >
          {/* New Chat — minimal, text + icon, full width */}
          <div className="p-3">
            <button
              onClick={handleNewChat}
              className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 transition-colors"
            >
              <svg className="h-4 w-4 shrink-0 text-gray-500" fill="none" viewBox="0 0 24 24"
                   stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
              </svg>
              New Chat
            </button>
          </div>

          {/* Recents — only when conversations exist */}
          {conversations.length > 0 && (
            <>
              <div className="border-t border-[#E8E8E8]" />
              <div className="px-4 pt-3 pb-1">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">
                  Recents
                </span>
              </div>
              <nav className="flex-1 overflow-y-auto px-2 pb-3">
                {conversations.map(conv => (
                  <div key={conv.id} className="group relative rounded-lg">
                    {confirmDeleteId === conv.id ? (
                      /* Inline delete confirmation */
                      <div className="flex items-center justify-between rounded-lg bg-gray-50 px-3 py-2.5">
                        <span className="text-xs text-gray-600">Delete?</span>
                        <div className="flex items-center gap-3">
                          <button
                            onClick={() => handleDeleteConversation(conv.id)}
                            className="text-xs font-semibold text-[#CC2529] hover:underline"
                          >
                            Yes
                          </button>
                          <button
                            onClick={() => setConfirmDeleteId(null)}
                            className="text-xs font-medium text-gray-400 hover:text-gray-600"
                          >
                            No
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <button
                          onClick={() => { loadConversationHistory(conv.id); setSidebarOpen(false) }}
                          className={`w-full rounded-lg px-3 py-2 pr-8 text-left transition-colors cursor-pointer ${
                            currentConvId === conv.id
                              ? 'bg-[#1B3A6B]/10 text-[#1B3A6B]'
                              : 'text-gray-600 hover:bg-gray-100'
                          }`}
                        >
                          <p className="truncate text-sm font-medium leading-snug">{conv.title}</p>
                          <p className="mt-0.5 text-[10px] text-gray-400">{relativeTime(conv.updated_at)}</p>
                        </button>
                        {/* Trash icon — visible on row hover */}
                        <button
                          onClick={(e) => { e.stopPropagation(); setConfirmDeleteId(conv.id) }}
                          className="absolute right-1.5 top-1/2 -translate-y-1/2 flex h-6 w-6 items-center justify-center rounded text-gray-300 opacity-0 transition-all group-hover:opacity-100 hover:!text-[#CC2529]"
                          aria-label="Delete conversation"
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                               stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="m19 6-.867 12.142A2 2 0 0116.138 20H7.862a2 2 0 01-1.995-1.858L5 6" />
                            <path d="M10 11v6" />
                            <path d="M14 11v6" />
                            <path d="M9 6V4h6v2" />
                          </svg>
                        </button>
                      </>
                    )}
                  </div>
                ))}
              </nav>
            </>
          )}
        </aside>

        {/* ── MAIN CONTENT ──────────────────────────────────────────────────── */}
        <main className="min-w-0 flex-1 overflow-hidden">
          {activeTab === 'spec' ? (
            <SpecAssistantView
              messages={messages}
              input={input}
              collection={collection}
              isLoading={isLoading}
              latestAnswer={latestAnswer}
              messagesEndRef={messagesEndRef}
              onInputChange={setInput}
              onCollectionChange={setCollection}
              onSend={handleSend}
              onKeyDown={handleKeyDown}
              onViewPdf={setPdfModal}
            />

          ) : (
            <DocumentReview />
          )}
        </main>
      </div>

      {/* ── PDF Modal ─────────────────────────────────────────────────────────── */}
      {pdfModal && (
        <PDFViewerModal citation={pdfModal} onClose={() => setPdfModal(null)} />
      )}
    </div>
  )
}

// ── SPEC ASSISTANT VIEW ────────────────────────────────────────────────────────

interface SpecViewProps {
  messages: Message[]
  input: string
  collection: string
  isLoading: boolean
  latestAnswer: Message | undefined
  messagesEndRef: React.RefObject<HTMLDivElement | null>
  onInputChange: (v: string) => void
  onCollectionChange: (v: string) => void
  onSend: (override?: string) => void
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void
  onViewPdf: (c: CitationItem) => void
}

function SpecAssistantView({
  messages, input, collection, isLoading, latestAnswer,
  messagesEndRef, onInputChange, onCollectionChange,
  onSend, onKeyDown, onViewPdf,
}: SpecViewProps) {
  return (
    <div className="flex h-full">

      {/* ── Chat area ── */}
      <div className="flex min-w-0 flex-1 flex-col bg-[#F8F9FA]">

        {/* Message list */}
        <div className="flex-1 overflow-y-auto px-4 py-5 space-y-5">

          {/* Welcome state — only when chat is empty */}
          {messages.length === 0 && !isLoading && (
            <div className="flex min-h-full flex-col items-center justify-center py-8">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-[#EEF2FF] shadow-sm ring-1 ring-[#1B3A6B]/10">
                <svg width="30" height="30" viewBox="0 0 24 24" fill="none"
                     stroke="#1B3A6B" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 8V4H8" />
                  <rect width="16" height="12" x="4" y="8" rx="2" />
                  <path d="M2 14h2" />
                  <path d="M20 14h2" />
                  <path d="M15 13v2" />
                  <path d="M9 13v2" />
                </svg>
              </div>
            </div>
          )}

          {messages.map(msg => (
            <div
              key={msg.id}
              className={`flex items-end gap-2.5 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              {/* Assistant avatar — bot icon */}
              {msg.role === 'assistant' && (
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full" style={{ background: '#EEF2FF' }}>
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#1B3A6B" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 8V4H8" />
                    <rect width="16" height="12" x="4" y="8" rx="2" />
                    <path d="M2 14h2" />
                    <path d="M20 14h2" />
                    <path d="M15 13v2" />
                    <path d="M9 13v2" />
                  </svg>
                </div>
              )}

              {msg.role === 'user' ? (
                /* User bubble */
                <div className="max-w-[78%] rounded-2xl rounded-br-sm bg-[#1B3A6B] px-4 py-2.5 text-sm text-white shadow-sm">
                  {msg.content}
                </div>
              ) : (
                /* Assistant message */
                <div className="max-w-[82%]">
                  <div
                    className={`rounded-lg bg-white px-4 py-3 text-sm leading-relaxed shadow-sm ${
                      msg.isError ? 'text-red-600' : 'text-gray-800'
                    }`}
                    style={{ border: '1px solid #e2e8f0', borderLeft: '3px solid #CC2529' }}
                  >
                    <p className="whitespace-pre-wrap">{msg.content}</p>
                  </div>
                  {msg.response_time_ms != null && (
                    <p className="mt-1 pl-1 text-[10px] text-gray-400">
                      {msg.query_type} · {msg.response_time_ms} ms
                      {msg.citations && msg.citations.length > 0 && (
                        <> · {msg.citations.length} source{msg.citations.length > 1 ? 's' : ''}</>
                      )}
                    </p>
                  )}
                </div>
              )}
            </div>
          ))}

          {/* Typing indicator */}
          {isLoading && (
            <div className="flex items-end gap-2.5 justify-start">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full" style={{ background: '#EEF2FF' }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#1B3A6B" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 8V4H8" />
                  <rect width="16" height="12" x="4" y="8" rx="2" />
                  <path d="M2 14h2" />
                  <path d="M20 14h2" />
                  <path d="M15 13v2" />
                  <path d="M9 13v2" />
                </svg>
              </div>
              <div className="flex items-center gap-1 rounded-2xl rounded-bl-sm bg-white px-4 py-3 shadow-sm ring-1 ring-black/5">
                <span className="typing-dot h-2 w-2 rounded-full bg-[#1B3A6B]/40" />
                <span className="typing-dot h-2 w-2 rounded-full bg-[#1B3A6B]/40" />
                <span className="typing-dot h-2 w-2 rounded-full bg-[#1B3A6B]/40" />
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* ── Input bar ── */}
        <div className="shrink-0 border-t border-[#E0E4EA] bg-[#F8F9FA] px-4 pb-4 pt-3">

          {/* Suggestion pills — always visible above the input card */}
          <div className="mb-3 flex flex-wrap gap-2">
            {SUGGESTED_QUESTIONS.map((sq) => (
              <button
                key={sq.title}
                onClick={() => onSend(sq.question)}
                className="flex items-center gap-1.5 rounded-full border border-[#E0E4EA] bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm transition-all hover:-translate-y-0.5 hover:border-[#1B3A6B] hover:shadow-md"
              >
                {/* Small red question icon */}
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                     stroke="#CC2529" strokeWidth={2.3} strokeLinecap="round" strokeLinejoin="round"
                     className="shrink-0">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
                  <path d="M12 17h.01" />
                </svg>
                {sq.title}
              </button>
            ))}
          </div>

          {/* Input card — white surface with shadow */}
          <div className="rounded-2xl border border-[#E2E8F0] bg-white px-3 pb-3 pt-2.5 shadow-md">
            <div className="mb-2">
              <select
                value={collection}
                onChange={(e) => onCollectionChange(e.target.value)}
                className="rounded-lg border border-[#E8E8E8] bg-[#F5F5F5] px-3 py-1.5 text-xs text-gray-600 focus:border-[#1B3A6B] focus:outline-none focus:ring-1 focus:ring-[#1B3A6B]/20"
              >
                {COLLECTIONS.map(c => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </div>

            <div className="flex gap-2">
              <input
                type="text"
                value={input}
                onChange={(e) => onInputChange(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Ask about specifications, procedures, or scheduling…"
                disabled={isLoading}
                className="flex-1 rounded-xl border border-[#E8E8E8] bg-[#F5F5F5] px-4 py-2.5 text-sm text-gray-800 placeholder:text-gray-400 transition-colors focus:border-[#1B3A6B] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#1B3A6B]/10 disabled:cursor-not-allowed disabled:opacity-50"
              />
              <button
                onClick={() => onSend()}
                disabled={isLoading || !input.trim()}
                className={`flex items-center justify-center rounded-xl px-4 py-2.5 text-white transition-colors focus:outline-none disabled:cursor-not-allowed ${
                  input.trim() && !isLoading
                    ? 'bg-[#CC2529] hover:bg-[#a81e21] focus:ring-2 focus:ring-[#CC2529]/30'
                    : 'bg-gray-300 opacity-70'
                }`}
                aria-label="Send"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ── Citations panel ── */}
      <aside className="hidden w-80 shrink-0 flex-col border-l border-[#DDE3ED] bg-[#EEF2F7] md:flex">
        {/* Header */}
        <div className="shrink-0 border-b border-[#E8E8E8] bg-white px-4 py-3">
          <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
            Sources
            {latestAnswer?.citations && latestAnswer.citations.length > 0 && (
              <span className="rounded-full bg-[#1B3A6B] px-2 py-0.5 text-[10px] font-bold text-white normal-case tracking-normal">
                {latestAnswer.citations.length}
              </span>
            )}
          </h2>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-3">
          {(!latestAnswer?.citations || latestAnswer.citations.length === 0) ? (
            <p className="mt-3 text-center text-xs text-gray-400">
              Source citations appear here after your first question.
            </p>
          ) : (
            <>
              {/* BDC alerts */}
              {latestAnswer.bdc_alerts && latestAnswer.bdc_alerts.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
                  <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-amber-800">
                    <span>⚠️</span>
                    This answer reflects amended specifications
                  </p>
                  <div className="space-y-2">
                    {latestAnswer.bdc_alerts.map(alert => (
                      <BDCAlertBadge key={`${alert.bdc_id}-${alert.section_id}`} alert={alert} onViewPdf={onViewPdf} />
                    ))}
                  </div>
                </div>
              )}

              {/* Citation cards */}
              {latestAnswer.citations.map((cit, i) => (
                <CitationCard
                  key={`${cit.chunk_id}-${i}`}
                  citation={cit}
                  index={i}
                  onViewPdf={() => onViewPdf(cit)}
                />
              ))}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

// ── BDC ALERT BADGE ────────────────────────────────────────────────────────────

const CHANGE_TYPE_STYLES: Record<string, { bg: string; text: string }> = {
  changed:  { bg: '#FEF3C7', text: '#92400E' },
  added:    { bg: '#D1FAE5', text: '#065F46' },
  deleted:  { bg: '#FEE2E2', text: '#991B1B' },
  replaced: { bg: '#DBEAFE', text: '#1E40AF' },
}

function BDCAlertBadge({
  alert,
  onViewPdf,
}: {
  alert: BDCAlertItem
  onViewPdf: (c: CitationItem) => void
}) {
  const isUrgent = alert.implementation_code === 'U'
  const ct = alert.change_type?.toLowerCase()
  const ctStyle = ct ? CHANGE_TYPE_STYLES[ct] : undefined

  const handleViewBdc = () => {
    onViewPdf({
      document:      alert.bdc_id,
      section:       alert.bdc_id,
      page_printed:  1,
      page_pdf:      1,
      chunk_id:      '',
    })
  }

  return (
    <div className="rounded-lg border border-amber-200 bg-white px-3 py-2">
      {/* Header row: BDC ID + change_type badge + urgent/routine pill */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-xs font-semibold text-amber-900 shrink-0">{alert.bdc_id}</span>
          {ctStyle && (
            <span
              className="rounded-full px-1.5 py-0.5 text-[11px] font-medium leading-none shrink-0"
              style={{ background: ctStyle.bg, color: ctStyle.text }}
            >
              {alert.change_type!.charAt(0).toUpperCase() + alert.change_type!.slice(1)}
            </span>
          )}
        </div>
        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide ${
          isUrgent ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-500'
        }`}>
          {isUrgent ? 'Urgent' : 'Routine'}
        </span>
      </div>

      {/* Section + effective date */}
      <p className="mt-0.5 text-xs text-gray-600">
        <span className="font-medium">§{alert.section_id}</span>
        {alert.effective_date && (
          <span className="text-gray-400"> · effective {alert.effective_date}</span>
        )}
      </p>

      {/* Subject — 2-line clamp */}
      {alert.subject && (
        <p
          className="mt-0.5 text-[11px] text-gray-500 leading-snug"
          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
        >
          {alert.subject}
        </p>
      )}

      {/* View BDC link */}
      <div className="mt-2 border-t border-amber-100 pt-1.5 flex justify-end">
        <button
          type="button"
          onClick={handleViewBdc}
          className="text-xs font-medium text-[#1B3A6B] hover:underline"
        >
          View BDC →
        </button>
      </div>
    </div>
  )
}

// ── CITATION CARD ──────────────────────────────────────────────────────────────

function CitationCard({
  citation, index, onViewPdf,
}: { citation: CitationItem; index: number; onViewPdf: () => void }) {
  return (
    <div className="rounded-xl border border-[#E8E8E8] bg-white p-3.5 shadow-sm">
      <div className="mb-1 flex items-start justify-between gap-2">
        <span className="text-[9px] font-bold uppercase tracking-widest text-[#1B3A6B]">
          {citation.document || 'NJDOT Document'}
        </span>
        <span className="shrink-0 text-xs text-gray-400">p. {citation.page_printed}</span>
      </div>
      <p className="text-sm font-medium text-gray-800 leading-snug">{citation.section}</p>
      <div className="mt-2 flex items-center justify-between">
        <span className="text-xs text-gray-400">PDF p. {citation.page_pdf}</span>
        <button
          type="button"
          onClick={onViewPdf}
          className="text-xs font-medium text-[#1B3A6B] hover:underline"
        >
          View PDF →
        </button>
      </div>
    </div>
  )
}

// ── PDF VIEWER MODAL ───────────────────────────────────────────────────────────

function PDFViewerModal({ citation, onClose }: { citation: CitationItem; onClose: () => void }) {
  const apiBase = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'
  const src = `${apiBase}/api/pdf/${citation.document}#page=${citation.page_pdf}`

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="flex flex-col bg-white rounded-2xl shadow-2xl w-[96vw] h-[92vh] max-w-5xl sm:w-[92vw] sm:h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#E8E8E8] shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <span className="text-[10px] font-bold uppercase tracking-widest text-[#1B3A6B] truncate">
              {citation.document || 'NJDOT Document'}
            </span>
            {citation.section && (
              <span className="text-sm text-gray-500 truncate">§ {citation.section}</span>
            )}
            {citation.page_printed && (
              <span className="text-sm text-gray-400 shrink-0">p. {citation.page_printed}</span>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ml-4 shrink-0 rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <iframe src={src} className="flex-1 w-full rounded-b-2xl" title={`PDF: ${citation.document}`} />
      </div>
    </div>
  )
}
