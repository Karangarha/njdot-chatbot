'use client'

import { useEffect, useRef, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import jsPDF from 'jspdf'
import autoTable from 'jspdf-autotable'
import SessionChat from './SessionChat'
import ChecklistManager from './ChecklistManager'
import { getEffectiveChecks, toCheckSpecs } from '@/lib/checklist'
import type { ComplianceCheck, ReviewProject } from '@/lib/types'

// ── Types ──────────────────────────────────────────────────────────────────────

interface CheckItem {
  id: string
  category: string
  name: string
  reasoning?: string
  status: 'pass' | 'warning' | 'fail'
  finding: string
  evidence: string
}

interface ReviewResult {
  project_id: string
  project_name: string
  project_duration_days: number
  model_used: string
  summary: {
    passed: number
    warnings: number
    failed: number
    manual_review: number
  }
  checks: CheckItem[]
  manual_review_items: string[]
  schedule_file_path?: string | null
  narrative_pdf_path?: string | null
  special_provision_pdf_path?: string | null
}

interface DocumentReviewProps {
  userId?: string
  selectedProjectId?: string | null
  onProjectSaved?: (p: ReviewProject) => void
  onNewReview?: () => void
}

// ── Constants ──────────────────────────────────────────────────────────────────

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

// The checklist (backend/app/compliance/catalog.py) is mostly uncategorized —
// only checks the source NJDOT document actually nests under one parent line
// carry a `category`. Those four groups are the only ones the results view
// (like the checklist editor) shows under a collapsible header; every other
// check renders as a flat, unheaded list in catalog order.
const HEADER_SECTIONS = [
  'Utility Service Restrictions',
  'Weather & Paving Restrictions',
  'Material Fabrication Lead Times',
  "Designer's Narrative",
]

// Mirrors backend/app/compliance/catalog.py's MANUAL_REVIEW_KEYS — used only
// as a defensive fallback for older saved review results whose stored
// `summary` predates the `manual_review` field.
const MANUAL_REVIEW_KEYS = [
  'utility_alignment', 'environmental_permit', 'edq_items', 'traffic_control_staging',
  'summer_shutdown', 'required_activities_present', 'multi_year_funding', 'nearby_projects',
]

// Purely descriptive topic labels for the pre-review "What gets checked" teaser.
const WHAT_GETS_CHECKED = [
  'Administrative Dates', 'Environmental, Landscape & Utilities', 'Weather & Materials',
  'Schedule Logic', "Designer's Narrative", 'Manual Review',
]

// ── Helpers ────────────────────────────────────────────────────────────────────

/**
 * Groups checks into consecutive runs sharing the same non-empty category,
 * preserving catalog order — mirrors ChecklistManager.tsx's groupSequential
 * so the results view and the checklist editor group checks identically.
 */
function groupSequential(checks: CheckItem[]): { header: string | null; items: CheckItem[] }[] {
  const runs: { header: string | null; items: CheckItem[] }[] = []
  for (const c of checks) {
    const last = runs[runs.length - 1]
    if (c.category && last?.header === c.category) {
      last.items.push(c)
    } else if (!c.category && last?.header === null) {
      last.items.push(c)
    } else {
      runs.push({ header: c.category || null, items: [c] })
    }
  }
  return runs
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function UploadZone({
  label, file, inputRef, accept, acceptValidationText, optional, onSelect, onRemove,
}: {
  label: string
  file: File | null
  inputRef: React.RefObject<HTMLInputElement | null>
  accept: string
  acceptValidationText: string
  optional?: boolean
  onSelect: (f: File) => void
  onRemove: () => void
}) {
  const [dragging, setDragging] = useState(false)

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const f = e.dataTransfer.files[0]
    if (!f) return
    if (accept === '.xer') {
      if (f.name.toLowerCase().endsWith('.xer')) onSelect(f)
    } else {
      if (f.type === accept) onSelect(f)
    }
  }

  return (
    <div className="flex-1 min-w-0">
      <p className="mb-1.5 text-xs font-semibold text-gray-700">
        {label}{' '}
        {optional
          ? <span className="text-gray-400 font-normal">(optional)</span>
          : <span className="text-[#CC2529]">*</span>}
      </p>
      {file ? (
        <div className="flex items-center gap-3 rounded-xl border border-[#1B3A6B]/25 bg-[#EEF2FF] px-4 py-3.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[#1B3A6B]/10">
            <svg className="h-5 w-5 text-[#1B3A6B]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
            </svg>
          </div>
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-[#1B3A6B]">{file.name}</span>
          <button onClick={onRemove} aria-label="Remove file"
            className="shrink-0 rounded-full p-1 text-gray-400 hover:bg-[#1B3A6B]/10 hover:text-[#1B3A6B] transition-colors">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      ) : (
        <button type="button" onClick={() => inputRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={`w-full cursor-pointer rounded-xl border-2 border-dashed px-4 py-8 text-center transition-colors ${dragging
            ? 'border-[#1B3A6B]/60 bg-[#EEF2FF]'
            : 'border-[#1B3A6B]/20 bg-white hover:border-[#1B3A6B]/40 hover:bg-[#1B3A6B]/2'}`}
        >
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-[#1B3A6B]/8">
            <svg className="h-5 w-5 text-[#1B3A6B]/50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
            </svg>
          </div>
          <p className="mb-0.5 text-xs font-semibold text-gray-600">Click or drop file here</p>
          <p className="text-[11px] text-gray-400">{acceptValidationText} · max 50 MB</p>
        </button>
      )}
      <input ref={inputRef} type="file" accept={accept} className="hidden"
        onChange={e => { const f = e.target.files?.[0]; if (f) onSelect(f); e.target.value = '' }} />
    </div>
  )
}

function CheckCard({ check }: { check: CheckItem }) {
  const styles = {
    pass:    { border: 'border-green-500', labelColor: 'text-green-600', label: 'COMPLIANT',    iconBg: 'bg-green-100' },
    fail:    { border: 'border-red-500',   labelColor: 'text-red-600',   label: 'ISSUES FOUND', iconBg: 'bg-red-100' },
    warning: { border: 'border-amber-500', labelColor: 'text-amber-600', label: 'MISSING',      iconBg: 'bg-amber-100' },
  }[check.status]

  return (
    <div className={`flex items-start gap-3.5 rounded-xl border-l-4 ${styles.border} bg-white px-4 py-3.5 shadow-sm ring-1 ring-black/5`}>
      <div className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${styles.iconBg}`}>
        {check.status === 'pass' && (
          <svg className="h-3.5 w-3.5 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        )}
        {check.status === 'warning' && (
          <svg className="h-3.5 w-3.5 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
          </svg>
        )}
        {check.status === 'fail' && (
          <svg className="h-3.5 w-3.5 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <span className={`text-[10px] font-bold uppercase tracking-wider ${styles.labelColor}`}>{styles.label}</span>
        <p className="mt-0.5 text-sm font-semibold text-gray-800">{check.name}</p>
        <p className="mt-1 text-xs text-gray-600 leading-relaxed">{check.finding}</p>
        {check.reasoning && (
          <div className="mt-1.5 rounded bg-gray-50 p-2 text-[11px] text-gray-500 italic border border-gray-100">
            <strong>Reasoning:</strong> {check.reasoning}
          </div>
        )}
        {check.evidence && <p className="mt-1.5 text-[11px] text-gray-400 leading-relaxed italic">{check.evidence}</p>}
      </div>
    </div>
  )
}

function CollapsibleSection({ title, checks, expanded, onToggle }: {
  title: string; checks: CheckItem[]; expanded: boolean; onToggle: () => void
}) {
  if (checks.length === 0) return null
  const warnCount = checks.filter(c => c.status === 'warning').length
  const failCount = checks.filter(c => c.status === 'fail').length

  return (
    <div className="rounded-2xl border border-[#E8E8E8] bg-white shadow-sm overflow-hidden">
      <button onClick={onToggle}
        className="flex w-full items-center justify-between px-5 py-4 text-left hover:bg-gray-50 transition-colors">
        <div className="flex items-center gap-3 min-w-0">
          <span className="font-semibold text-sm text-[#1B3A6B]">{title}</span>
          <div className="flex items-center gap-1.5">
            {failCount > 0 && <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700">{failCount} failed</span>}
            {warnCount > 0 && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold text-amber-700">{warnCount} warning{warnCount !== 1 ? 's' : ''}</span>}
            {failCount === 0 && warnCount === 0 && <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-bold text-green-700">All passed</span>}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0 ml-3">
          <span className="text-[11px] text-gray-400">{checks.length} checks</span>
          <svg className={`h-4 w-4 text-gray-400 transition-transform ${expanded ? 'rotate-180' : ''}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>
      {expanded && (
        <div className="space-y-2.5 border-t border-[#E8E8E8] px-4 py-4">
          {checks.map(check => <CheckCard key={check.id} check={check} />)}
        </div>
      )}
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function DocumentReview({
  userId, selectedProjectId, onProjectSaved, onNewReview,
}: DocumentReviewProps) {
  const [scheduleFile,  setScheduleFile]  = useState<File | null>(null)
  const [narrativeFile, setNarrativeFile] = useState<File | null>(null)
  const [spFile,        setSpFile]        = useState<File | null>(null)
  const [isLoading,     setIsLoading]     = useState(false)
  const [error,         setError]         = useState<string | null>(null)
  const [result,        setResult]        = useState<ReviewResult | null>(null)
  const [sessionId,     setSessionId]     = useState<string | null>(null)
  const [authToken,     setAuthToken]     = useState<string | undefined>(undefined)
  const [activeTab,     setActiveTab]     = useState<'review' | 'chat'>('review')
  const [expanded,      setExpanded]      = useState<Record<string, boolean>>(
    Object.fromEntries(HEADER_SECTIONS.map(s => [s, true]))
  )
  const [effectiveChecks, setEffectiveChecks] = useState<ComplianceCheck[]>([])
  const [checklistOpen,   setChecklistOpen]   = useState(false)
  const [isRerunning,     setIsRerunning]     = useState(false)
  const [rerunError,      setRerunError]      = useState<string | null>(null)

  const scheduleRef       = useRef<HTMLInputElement>(null)
  const narrativeRef      = useRef<HTMLInputElement>(null)
  const spRef             = useRef<HTMLInputElement>(null)
  const currentProjectRef = useRef<string | null>(null)
  // Synchronous re-entrancy guards — refs update instantly (unlike state),
  // so they catch a fast double-click that fires both `click` events before
  // the isLoading/isRerunning state update has re-rendered the button away.
  const submittingRef = useRef(false)
  const rerunningRef  = useRef(false)

  // ── Load the user's effective checklist (built-ins or their own fork) ──────
  useEffect(() => {
    getEffectiveChecks(userId).then(setEffectiveChecks)
  }, [userId])

  // ── Load selected project from DB when parent changes selection ────────────
  useEffect(() => {
    if (!selectedProjectId) {
      setResult(null)
      setSessionId(null)
      setActiveTab('review')
      return
    }
    const sb = createClient()
    sb.from('review_projects')
      .select('*')
      .eq('id', selectedProjectId)
      .single()
      .then(({ data }) => {
        if (!data) return
        setResult(data.review_result as ReviewResult)
        setSessionId(data.session_id ?? null)
        setActiveTab('review')
        currentProjectRef.current = data.id
      })
  }, [selectedProjectId])

  const canSubmit = scheduleFile !== null && narrativeFile !== null && !isLoading

  const toggleSection = (name: string) => setExpanded(prev => ({ ...prev, [name]: !prev[name] }))

  const handleDownloadPdf = () => {
    if (!result) return
    const doc = new jsPDF()
    const { project_name, project_duration_days, model_used, summary, checks } = result
    doc.setFontSize(18); doc.text('Schedule Compliance Review', 14, 22)
    doc.setFontSize(11); doc.setTextColor(100)
    doc.text(`Project: ${project_name || 'Unknown'}`, 14, 30)
    doc.text(`Duration: ${project_duration_days} days`, 14, 36)
    if (model_used) doc.text(`Reviewed by: ${model_used}`, 14, 42)
    doc.setFontSize(12); doc.setTextColor(0); doc.text('Summary', 14, 52)
    doc.setFontSize(10)
    doc.text(`Passed: ${summary.passed}`, 14, 58)
    doc.text(`Warnings: ${summary.warnings}`, 14, 64)
    doc.text(`Failed: ${summary.failed}`, 14, 70)
    autoTable(doc, {
      startY: 78,
      head: [['Category', 'Check', 'Reasoning', 'Status', 'Finding', 'Evidence']],
      body: checks.map(c => [c.category, c.name, c.reasoning || '', c.status.toUpperCase(), c.finding, c.evidence]),
      theme: 'grid',
      styles: { fontSize: 8, cellPadding: 3 },
      headStyles: { fillColor: [27, 58, 107] },
      columnStyles: { 0: { cellWidth: 25 }, 1: { cellWidth: 30 }, 2: { cellWidth: 35 }, 3: { cellWidth: 15 }, 4: { cellWidth: 45 }, 5: { cellWidth: 35 } },
      didParseCell: (data: any) => {
        if (data.section === 'body' && data.column.index === 3) {
          if (data.cell.raw === 'PASS')    data.cell.styles.textColor = [21, 128, 61]
          if (data.cell.raw === 'WARNING') data.cell.styles.textColor = [180, 83, 9]
          if (data.cell.raw === 'FAIL')    data.cell.styles.textColor = [185, 28, 28]
        }
      },
    })
    doc.save('Schedule_Compliance_Report.pdf')
  }

  const runReview = async () => {
    if (!scheduleFile || !narrativeFile) return
    if (submittingRef.current) return   // guards a fast double-click race
    submittingRef.current = true

    const specs = toCheckSpecs(effectiveChecks)
    if (userId && effectiveChecks.length > 0 && specs.length === 0) {
      setError('No checks are selected to run. Open "Manage Checklist" and enable at least one.')
      submittingRef.current = false
      return
    }

    setIsLoading(true)
    setError(null)

    try {
      const sb = createClient()
      const { data: { session } } = await sb.auth.getSession()
      const headers: HeadersInit = {}
      if (session?.access_token) {
        headers['Authorization'] = `Bearer ${session.access_token}`
        setAuthToken(session.access_token)
      }

      const reviewForm = new FormData()
      reviewForm.append('schedule_file', scheduleFile)
      reviewForm.append('narrative_pdf', narrativeFile)
      if (spFile) reviewForm.append('special_provision_pdf', spFile)
      if (specs.length > 0) reviewForm.append('checks', JSON.stringify(specs))

      const sessionForm = new FormData()
      sessionForm.append('narrative_pdf', narrativeFile)
      sessionForm.append('xer_file', scheduleFile)
      if (spFile) sessionForm.append('special_provision_pdf', spFile)

      // ── Fire review + session upload in parallel ──────────────────────────
      const [reviewRes, sessionRes] = await Promise.all([
        fetch(`${API_BASE}/api/review`,         { method: 'POST', headers, body: reviewForm }),
        fetch(`${API_BASE}/api/session/upload`, { method: 'POST', headers, body: sessionForm }),
      ])

      if (!reviewRes.ok) {
        let detail = `Request failed with status ${reviewRes.status}`
        try { const b = await reviewRes.json(); if (typeof b.detail === 'string') detail = b.detail } catch {}
        throw new Error(detail)
      }

      const data = await reviewRes.json() as ReviewResult
      setResult(data)

      // ── Save review result to DB ──────────────────────────────────────────
      // The backend already uploaded the original files to Storage (when
      // signed in) and generated project_id — see /api/review's
      // backend-led upload. We just insert one row using that same id as
      // the primary key, so Neo4j's projectId === review_projects.id from
      // the very first run. No frontend Storage access, no second upload
      // of the same bytes, no follow-up PATCH.
      let savedProjectId: string | null = null
      if (userId) {
        const { data: saved } = await sb
          .from('review_projects')
          .insert({
            id:                          data.project_id,
            user_id:                     userId,
            project_name:                data.project_name || 'Untitled Project',
            review_result:               data,
            session_id:                  null,
            schedule_file_path:          data.schedule_file_path ?? null,
            narrative_pdf_path:          data.narrative_pdf_path ?? null,
            special_provision_pdf_path:  data.special_provision_pdf_path ?? null,
          })
          .select()
          .single()
        if (saved) {
          savedProjectId = saved.id
          currentProjectRef.current = saved.id
          onProjectSaved?.(saved as ReviewProject)
        }
      }

      // ── Link session_id once upload resolves ──────────────────────────────
      if (sessionRes.ok) {
        const { session_id } = await sessionRes.json()
        setSessionId(session_id)
        if (savedProjectId) {
          await sb.from('review_projects')
            .update({ session_id, updated_at: new Date().toISOString() })
            .eq('id', savedProjectId)
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred.')
    } finally {
      setIsLoading(false)
      submittingRef.current = false
    }
  }

  const handleRerun = async () => {
    const projectId = currentProjectRef.current
    if (!projectId || rerunningRef.current) return
    rerunningRef.current = true

    const specs = toCheckSpecs(effectiveChecks)
    if (effectiveChecks.length > 0 && specs.length === 0) {
      setRerunError('No checks are selected to run. Open "Manage Checklist" and enable at least one.')
      rerunningRef.current = false
      return
    }

    setIsRerunning(true)
    setRerunError(null)
    try {
      const sb = createClient()
      let { data: { session } } = await sb.auth.getSession()
      // getSession() should auto-refresh an expired token, but fall back to
      // an explicit refresh if it still came back empty rather than silently
      // sending the request with no Authorization header (guaranteed 401).
      if (!session?.access_token) {
        const refreshed = await sb.auth.refreshSession()
        session = refreshed.data.session
      }
      if (!session?.access_token) {
        throw new Error('Your session has expired. Please sign in again and retry.')
      }

      const form = new FormData()
      if (specs.length > 0) form.append('checks', JSON.stringify(specs))

      const res = await fetch(`${API_BASE}/api/review/${projectId}/rerun`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${session.access_token}` },
        body: form,
      })
      if (!res.ok) {
        let detail = `Request failed with status ${res.status}`
        try { const b = await res.json(); if (typeof b.detail === 'string') detail = b.detail } catch {}
        throw new Error(detail)
      }
      setResult(await res.json() as ReviewResult)
    } catch (err) {
      setRerunError(err instanceof Error ? err.message : 'An unexpected error occurred.')
    } finally {
      setIsRerunning(false)
      rerunningRef.current = false
    }
  }

  // ── Results view ──────────────────────────────────────────────────────────

  if (result) {
    const { summary, checks, project_name, project_duration_days, model_used } = result
    return (
      <>
      <div className="h-full flex flex-col bg-[#F5F5F5]">

        {/* Header */}
        <div className="shrink-0 border-b border-[#E8E8E8] bg-white px-5 py-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-base font-bold text-[#1B3A6B]">Schedule Compliance Review</h2>
              {project_name && <p className="text-xs text-gray-600 font-medium truncate">{project_name}</p>}
              {project_duration_days > 0 && (
                <p className="text-[11px] text-gray-400">
                  {project_duration_days} calendar days{model_used ? ` · ${model_used}` : ''}
                </p>
              )}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {currentProjectRef.current && (
                <button onClick={() => setChecklistOpen(true)} disabled={isRerunning}
                  className="flex items-center gap-1.5 rounded-lg border border-[#E8E8E8] bg-white px-3 py-2 text-xs font-semibold text-gray-600 shadow-sm hover:border-[#1B3A6B]/30 hover:text-[#1B3A6B] transition-colors disabled:opacity-50">
                  Manage Checklist
                </button>
              )}
              {currentProjectRef.current && (
                <button onClick={handleRerun} disabled={isRerunning}
                  className="flex items-center gap-1.5 rounded-lg border border-[#E8E8E8] bg-white px-3 py-2 text-xs font-semibold text-gray-600 shadow-sm hover:border-[#1B3A6B]/30 hover:text-[#1B3A6B] transition-colors disabled:opacity-50">
                  <svg className={`h-3.5 w-3.5 ${isRerunning ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
                  </svg>
                  {isRerunning ? 'Re-running…' : 'Re-run Review'}
                </button>
              )}
              <button onClick={handleDownloadPdf}
                className="flex items-center gap-1.5 rounded-lg bg-[#1B3A6B] px-3 py-2 text-xs font-semibold text-white shadow-sm hover:bg-[#12264a] transition-colors">
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                </svg>
                Download PDF
              </button>
              <button onClick={() => { setResult(null); setSessionId(null); setRerunError(null); onNewReview?.() }}
                className="flex items-center gap-1.5 rounded-lg border border-[#E8E8E8] bg-white px-3 py-2 text-xs font-semibold text-gray-600 shadow-sm hover:border-[#1B3A6B]/30 hover:text-[#1B3A6B] transition-colors">
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
                </svg>
                New Review
              </button>
            </div>
          </div>

          {rerunError && (
            <div className="mt-3 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2">
              <p className="text-[11px] text-red-700 leading-relaxed">{rerunError}</p>
            </div>
          )}

          {/* Summary pills */}
          <div className="mt-3 flex flex-wrap gap-2">
            <span className="flex items-center gap-1.5 rounded-full bg-green-100 px-3 py-1 text-[11px] font-bold text-green-700">
              <span className="h-1.5 w-1.5 rounded-full bg-green-500 inline-block" />{summary.passed} Passed
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-amber-100 px-3 py-1 text-[11px] font-bold text-amber-700">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500 inline-block" />{summary.warnings} Warning{summary.warnings !== 1 ? 's' : ''}
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-red-100 px-3 py-1 text-[11px] font-bold text-red-700">
              <span className="h-1.5 w-1.5 rounded-full bg-red-500 inline-block" />{summary.failed} Failed
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1 text-[11px] font-bold text-gray-600">
              <span className="h-1.5 w-1.5 rounded-full bg-gray-400 inline-block" />
              {summary.manual_review ?? checks.filter(c => MANUAL_REVIEW_KEYS.includes(c.id)).length} Manual Review
            </span>
          </div>

          {/* Tabs (only when session exists) */}
          {sessionId && (
            <div className="mt-3 flex gap-1 border-b border-[#E8E8E8] -mb-3">
              {(['review', 'chat'] as const).map(tab => (
                <button key={tab} onClick={() => setActiveTab(tab)}
                  className={`px-4 py-2 text-xs font-semibold border-b-2 transition-colors ${
                    activeTab === tab ? 'border-[#1B3A6B] text-[#1B3A6B]' : 'border-transparent text-gray-500 hover:text-gray-700'
                  }`}>
                  {tab === 'review' ? 'Compliance Results' : 'Document Q&A'}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Tab content */}
        {activeTab === 'review' || !sessionId ? (
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-3xl px-5 py-6 space-y-3">
              {groupSequential(checks).map((run, i) => (
                run.header ? (
                  <CollapsibleSection key={run.header} title={run.header}
                    checks={run.items}
                    expanded={expanded[run.header] ?? true}
                    onToggle={() => toggleSection(run.header!)} />
                ) : (
                  <div key={i} className="space-y-2.5">
                    {run.items.map(check => <CheckCard key={check.id} check={check} />)}
                  </div>
                )
              ))}
            </div>
          </div>
        ) : (
          <div className="flex-1 min-h-0">
            <SessionChat sessionId={sessionId} apiBase={API_BASE} authToken={authToken} />
          </div>
        )}
      </div>

      {checklistOpen && (
        <ChecklistManager
          userId={userId}
          onChange={setEffectiveChecks}
          onClose={() => setChecklistOpen(false)}
        />
      )}
      </>
    )
  }

  // ── Upload view ──────────────────────────────────────────────────────────────

  return (
    <div className="h-full overflow-y-auto bg-[#F5F5F5]">
      <div className="mx-auto max-w-2xl px-5 py-10">

        <div className="mb-1 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-bold text-[#1B3A6B]">Document Review</h2>
            <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700">
              Beta Testing
            </span>
          </div>
          <button onClick={() => setChecklistOpen(true)}
            className="flex items-center gap-1.5 rounded-lg border border-[#E8E8E8] bg-white px-3 py-1.5 text-xs font-semibold text-gray-600 shadow-sm hover:border-[#1B3A6B]/30 hover:text-[#1B3A6B] transition-colors">
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" />
            </svg>
            Manage Checklist
          </button>
        </div>
        <p className="mb-8 text-sm text-gray-500">
          Upload the CPM schedule (.xer), designer narrative, and optionally a Special Provision PDF
          to run an automated NJDOT compliance review and enable document Q&A.
        </p>

        {/* Row 1: XER + Narrative */}
        <div className="mb-4 flex flex-col gap-4 sm:flex-row">
          <UploadZone label="Construction Schedule (.xer)" file={scheduleFile} inputRef={scheduleRef}
            accept=".xer" acceptValidationText=".XER only"
            onSelect={setScheduleFile} onRemove={() => setScheduleFile(null)} />
          <UploadZone label="Designer Narrative PDF" file={narrativeFile} inputRef={narrativeRef}
            accept="application/pdf" acceptValidationText="PDF only"
            onSelect={setNarrativeFile} onRemove={() => setNarrativeFile(null)} />
        </div>

        {/* Row 2: Special Provision */}
        <div className="mb-6">
          <UploadZone label="Special Provision PDF" file={spFile} inputRef={spRef}
            accept="application/pdf" acceptValidationText="PDF only · ~200 pages"
            optional onSelect={setSpFile} onRemove={() => setSpFile(null)} />
          {spFile && (
            <p className="mt-1.5 text-[11px] text-gray-400">
              Special Provision will be indexed in the background for Document Q&A.
            </p>
          )}
        </div>

        {/* Error */}
        {error && (
          <div className="mb-4 flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <svg className="mt-0.5 h-4 w-4 shrink-0 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
            <p className="text-xs text-red-700 leading-relaxed">{error}</p>
          </div>
        )}

        {/* Run Review */}
        {isLoading ? (
          <div className="flex items-center justify-center gap-3 rounded-xl border border-[#1B3A6B]/15 bg-white px-5 py-4 shadow-sm">
            <svg className="h-5 w-5 animate-spin text-[#1B3A6B]" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            <span className="text-sm font-medium text-[#1B3A6B]">
              Analyzing documents… this may take up to 30 seconds
            </span>
          </div>
        ) : (
          <button onClick={runReview} disabled={!canSubmit}
            className={`w-full rounded-xl px-5 py-3.5 text-sm font-bold transition-all ${canSubmit
              ? 'bg-[#CC2529] text-white shadow-sm hover:bg-[#a81e21] active:scale-[0.99]'
              : 'cursor-not-allowed bg-gray-200 text-gray-400'}`}>
            Run Review
          </button>
        )}

        {/* What gets checked */}
        {!isLoading && (
          <div className="mt-8 rounded-2xl border border-[#E8E8E8] bg-white p-5 shadow-sm">
            <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-400">What gets checked</p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {WHAT_GETS_CHECKED.map(s => (
                <div key={s} className="rounded-lg bg-[#F5F5F5] px-3 py-2.5 text-center">
                  <p className="text-[11px] font-semibold text-gray-600 leading-tight">{s}</p>
                </div>
              ))}
            </div>
            <p className="mt-3 text-[11px] text-gray-400">
              After review, use Document Q&A to ask questions across the narrative,
              special provisions, schedule, and Construction Scheduling Manual.
            </p>
          </div>
        )}

      </div>

      {checklistOpen && (
        <ChecklistManager
          userId={userId}
          onChange={setEffectiveChecks}
          onClose={() => setChecklistOpen(false)}
        />
      )}
    </div>
  )
}
