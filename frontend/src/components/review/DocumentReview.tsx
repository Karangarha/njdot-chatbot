'use client'

import { useRef, useState } from 'react'
import { createClient } from '@/lib/supabase/client'

// ── Types ──────────────────────────────────────────────────────────────────────

interface CheckItem {
  id: string
  category: string
  name: string
  status: 'pass' | 'warning' | 'fail'
  finding: string
  evidence: string
}

interface ReviewResult {
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
}

// ── Constants ──────────────────────────────────────────────────────────────────

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

// Maps category names from the API onto one of the four display sections.
const SECTION_CATEGORIES: Record<string, string[]> = {
  'Administrative & Milestones': [
    'Administrative Dates',
    'Completion Milestones',
    'Schedule Logic',
  ],
  'Environmental & Permit Restrictions': [
    'Environmental & Permit Restrictions',
    'Winter Restrictions',
  ],
  'Working Drawings & Materials': ['Working Drawings & Materials'],
  'Narrative Completeness': ['Narrative Completeness'],
}

const SECTION_ORDER = [
  'Administrative & Milestones',
  'Environmental & Permit Restrictions',
  'Working Drawings & Materials',
  'Narrative Completeness',
]

const MANUAL_REVIEW_ITEMS = [
  'Utility alignment with Key Map and Special Provisions',
  'Gas/water/electric utility restriction windows (confirm with Special Provisions)',
  'Environmental permit compliance beyond narrative',
  'Landscape and planting restrictions',
  'EDQ items review',
  'Multi-year funding check (SP 108.10)',
  'Other nearby construction projects (105.06)',
  'ITS testing and burn-in period',
  'Summer shutdown restrictions for shore routes',
]

// ── Helpers ────────────────────────────────────────────────────────────────────

function checksForSection(checks: CheckItem[], sectionName: string): CheckItem[] {
  const cats = SECTION_CATEGORIES[sectionName] ?? []
  return checks.filter(c => cats.includes(c.category))
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function UploadZone({
  label,
  file,
  inputRef,
  accept,
  acceptValidationText,
  onSelect,
  onRemove,
}: {
  label: string
  file: File | null
  inputRef: React.RefObject<HTMLInputElement | null>
  accept: string
  acceptValidationText: string
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
        {label} <span className="text-[#CC2529]">*</span>
      </p>

      {file ? (
        /* ── File selected state ── */
        <div className="flex items-center gap-3 rounded-xl border border-[#1B3A6B]/25 bg-[#EEF2FF] px-4 py-3.5">
          {/* PDF icon */}
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[#1B3A6B]/10">
            <svg className="h-5 w-5 text-[#1B3A6B]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
            </svg>
          </div>
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-[#1B3A6B]">
            {file.name}
          </span>
          <button
            onClick={onRemove}
            className="shrink-0 rounded-full p-1 text-gray-400 hover:bg-[#1B3A6B]/10 hover:text-[#1B3A6B] transition-colors"
            aria-label="Remove file"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      ) : (
        /* ── Empty drop zone ── */
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={`w-full cursor-pointer rounded-xl border-2 border-dashed px-4 py-8 text-center transition-colors ${
            dragging
              ? 'border-[#1B3A6B]/60 bg-[#EEF2FF]'
              : 'border-[#1B3A6B]/20 bg-white hover:border-[#1B3A6B]/40 hover:bg-[#1B3A6B]/2'
          }`}
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

      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="hidden"
        onChange={e => {
          const f = e.target.files?.[0]
          if (f) onSelect(f)
          e.target.value = ''
        }}
      />
    </div>
  )
}

function CheckCard({ check }: { check: CheckItem }) {
  const styles = {
    pass:    { border: 'border-green-500',  labelColor: 'text-green-600',  label: 'COMPLIANT',    iconBg: 'bg-green-100' },
    warning: { border: 'border-amber-500',  labelColor: 'text-amber-600',  label: 'ISSUES FOUND', iconBg: 'bg-amber-100' },
    fail:    { border: 'border-red-500',    labelColor: 'text-red-600',    label: 'MISSING',      iconBg: 'bg-red-100' },
  }[check.status]

  return (
    <div className={`flex items-start gap-3.5 rounded-xl border-l-4 ${styles.border} bg-white px-4 py-3.5 shadow-sm ring-1 ring-black/5`}>
      {/* Status icon */}
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

      {/* Content */}
      <div className="min-w-0 flex-1">
        <span className={`text-[10px] font-bold uppercase tracking-wider ${styles.labelColor}`}>
          {styles.label}
        </span>
        <p className="mt-0.5 text-sm font-semibold text-gray-800">{check.name}</p>
        <p className="mt-1 text-xs text-gray-600 leading-relaxed">{check.finding}</p>
        {check.evidence && (
          <p className="mt-1.5 text-[11px] text-gray-400 leading-relaxed italic">{check.evidence}</p>
        )}
      </div>
    </div>
  )
}

function CollapsibleSection({
  title,
  checks,
  expanded,
  onToggle,
}: {
  title: string
  checks: CheckItem[]
  expanded: boolean
  onToggle: () => void
}) {
  if (checks.length === 0) return null

  const passCount    = checks.filter(c => c.status === 'pass').length
  const warnCount    = checks.filter(c => c.status === 'warning').length
  const failCount    = checks.filter(c => c.status === 'fail').length

  return (
    <div className="rounded-2xl border border-[#E8E8E8] bg-white shadow-sm overflow-hidden">
      {/* Section header */}
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between px-5 py-4 text-left hover:bg-gray-50 transition-colors"
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="font-semibold text-sm text-[#1B3A6B]">{title}</span>
          <div className="flex items-center gap-1.5">
            {failCount > 0 && (
              <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700">
                {failCount} failed
              </span>
            )}
            {warnCount > 0 && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold text-amber-700">
                {warnCount} warning{warnCount !== 1 ? 's' : ''}
              </span>
            )}
            {failCount === 0 && warnCount === 0 && (
              <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-bold text-green-700">
                All passed
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0 ml-3">
          <span className="text-[11px] text-gray-400">{checks.length} checks</span>
          <svg
            className={`h-4 w-4 text-gray-400 transition-transform ${expanded ? 'rotate-180' : ''}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {/* Check cards */}
      {expanded && (
        <div className="space-y-2.5 border-t border-[#E8E8E8] px-4 py-4">
          {checks.map(check => (
            <CheckCard key={check.id} check={check} />
          ))}
        </div>
      )}
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function DocumentReview() {
  const [scheduleFile, setScheduleFile] = useState<File | null>(null)
  const [narrativeFile, setNarrativeFile] = useState<File | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ReviewResult | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>(
    Object.fromEntries(SECTION_ORDER.map(s => [s, true]))
  )

  const scheduleRef = useRef<HTMLInputElement>(null)
  const narrativeRef = useRef<HTMLInputElement>(null)

  const canSubmit = scheduleFile !== null && narrativeFile !== null && !isLoading

  const toggleSection = (name: string) =>
    setExpanded(prev => ({ ...prev, [name]: !prev[name] }))

  const reset = () => {
    setScheduleFile(null)
    setNarrativeFile(null)
    setResult(null)
    setError(null)
  }

  const runReview = async () => {
    if (!scheduleFile || !narrativeFile) return
    setIsLoading(true)
    setError(null)

    try {
      const sb = createClient()
      const { data: { session } } = await sb.auth.getSession()

      const formData = new FormData()
      formData.append('schedule_file', scheduleFile)
      formData.append('narrative_pdf', narrativeFile)

      const headers: HeadersInit = {}
      if (session?.access_token) {
        headers['Authorization'] = `Bearer ${session.access_token}`
      }

      const res = await fetch(`${API_BASE}/api/review`, {
        method: 'POST',
        headers,
        body: formData,
      })

      if (!res.ok) {
        let detail = `Request failed with status ${res.status}`
        try {
          const body = await res.json()
          if (typeof body.detail === 'string') detail = body.detail
        } catch { /* keep generic */ }
        throw new Error(detail)
      }

      const data = await res.json() as ReviewResult
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred.')
    } finally {
      setIsLoading(false)
    }
  }

  // ── Results view ─────────────────────────────────────────────────────────────

  if (result) {
    const { summary, checks, project_name, project_duration_days, model_used } = result
    const manualItems = result.manual_review_items?.length
      ? result.manual_review_items
      : MANUAL_REVIEW_ITEMS

    return (
      <div className="h-full overflow-y-auto bg-[#F5F5F5]">
        <div className="mx-auto max-w-3xl px-5 py-7">

          {/* ── Top bar ── */}
          <div className="mb-6 flex items-start justify-between gap-4">
            <div>
              <h2 className="text-lg font-bold text-[#1B3A6B]">Schedule Compliance Review</h2>
              {project_name && (
                <p className="mt-0.5 text-sm text-gray-600 font-medium">{project_name}</p>
              )}
              {project_duration_days > 0 && (
                <p className="text-xs text-gray-400">{project_duration_days} calendar days</p>
              )}
              {model_used && (
                <p className="mt-1 text-[11px] text-gray-400">Reviewed by {model_used}</p>
              )}
            </div>
            <button
              onClick={reset}
              className="shrink-0 flex items-center gap-1.5 rounded-lg border border-[#E8E8E8] bg-white px-3 py-2 text-xs font-semibold text-gray-600 shadow-sm hover:border-[#1B3A6B]/30 hover:text-[#1B3A6B] transition-colors"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
              Review Another Document
            </button>
          </div>

          {/* ── Summary bar ── */}
          <div className="mb-6 flex flex-wrap gap-2">
            <span className="flex items-center gap-1.5 rounded-full bg-green-100 px-3.5 py-1.5 text-xs font-bold text-green-700">
              <span className="h-2 w-2 rounded-full bg-green-500 inline-block" />
              {summary.passed} Passed
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-amber-100 px-3.5 py-1.5 text-xs font-bold text-amber-700">
              <span className="h-2 w-2 rounded-full bg-amber-500 inline-block" />
              {summary.warnings} Warning{summary.warnings !== 1 ? 's' : ''}
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-red-100 px-3.5 py-1.5 text-xs font-bold text-red-700">
              <span className="h-2 w-2 rounded-full bg-red-500 inline-block" />
              {summary.failed} Failed
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-gray-100 px-3.5 py-1.5 text-xs font-bold text-gray-600">
              <span className="h-2 w-2 rounded-full bg-gray-400 inline-block" />
              {summary.manual_review ?? manualItems.length} Manual Review
            </span>
          </div>

          {/* ── Collapsible result sections ── */}
          <div className="space-y-3 mb-6">
            {SECTION_ORDER.map(sectionName => (
              <CollapsibleSection
                key={sectionName}
                title={sectionName}
                checks={checksForSection(checks, sectionName)}
                expanded={expanded[sectionName]}
                onToggle={() => toggleSection(sectionName)}
              />
            ))}
          </div>

          {/* ── Manual Review section (always shown, always static) ── */}
          <div className="rounded-2xl border border-[#E8E8E8] bg-white shadow-sm overflow-hidden">
            <div className="px-5 py-4 border-b border-[#E8E8E8]">
              <div className="flex items-center gap-2.5">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-gray-100">
                  <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round"
                      d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 01-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9.06 9.06 0 011.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 00-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 01-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 00-3.375-3.375h-1.5a1.125 1.125 0 01-1.125-1.125v-1.5a3.375 3.375 0 00-3.375-3.375H9.75" />
                  </svg>
                </div>
                <div>
                  <p className="text-sm font-semibold text-gray-700">Manual Review Required</p>
                  <p className="text-[11px] text-gray-400">
                    These items cannot be automatically verified and require manual inspection
                  </p>
                </div>
              </div>
            </div>
            <div className="px-4 py-3 space-y-1.5">
              {manualItems.map((item, i) => (
                <div
                  key={i}
                  className="flex items-start gap-2.5 rounded-lg bg-gray-50 px-3 py-2.5"
                >
                  <span className="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-gray-400" />
                  <span className="text-xs text-gray-600">{item}</span>
                </div>
              ))}
            </div>
          </div>

        </div>
      </div>
    )
  }

  // ── Upload view ──────────────────────────────────────────────────────────────

  return (
    <div className="h-full overflow-y-auto bg-[#F5F5F5]">
      <div className="mx-auto max-w-2xl px-5 py-10">

        <div className="mb-1 flex items-center gap-2">
          <h2 className="text-lg font-bold text-[#1B3A6B]">Document Review</h2>
          <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700">
            Beta Testing
          </span>
        </div>
        <p className="mb-8 text-sm text-gray-500">
          Upload the CPM schedule (.xer) and designer narrative (PDF) to run an automated
          NJDOT compliance review.
        </p>

        {/* ── Upload zones ── */}
        <div className="mb-6 flex flex-col gap-4 sm:flex-row">
          <UploadZone
            label="Construction Schedule (.xer)"
            file={scheduleFile}
            inputRef={scheduleRef}
            accept=".xer"
            acceptValidationText=".XER only"
            onSelect={setScheduleFile}
            onRemove={() => setScheduleFile(null)}
          />
          <UploadZone
            label="Designer Narrative PDF"
            file={narrativeFile}
            inputRef={narrativeRef}
            accept="application/pdf"
            acceptValidationText="PDF only"
            onSelect={setNarrativeFile}
            onRemove={() => setNarrativeFile(null)}
          />
        </div>

        {/* ── Error banner ── */}
        {error && (
          <div className="mb-4 flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <svg className="mt-0.5 h-4 w-4 shrink-0 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
            <p className="text-xs text-red-700 leading-relaxed">{error}</p>
          </div>
        )}

        {/* ── Run Review button / loading ── */}
        {isLoading ? (
          <div className="flex items-center justify-center gap-3 rounded-xl border border-[#1B3A6B]/15 bg-white px-5 py-4 shadow-sm">
            <svg className="h-5 w-5 animate-spin text-[#1B3A6B]" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            <span className="text-sm font-medium text-[#1B3A6B]">
              Analyzing documents… this may take up to 30 seconds
            </span>
          </div>
        ) : (
          <button
            onClick={runReview}
            disabled={!canSubmit}
            className={`w-full rounded-xl px-5 py-3.5 text-sm font-bold transition-all ${
              canSubmit
                ? 'bg-[#CC2529] text-white shadow-sm hover:bg-[#a81e21] active:scale-[0.99]'
                : 'cursor-not-allowed bg-gray-200 text-gray-400'
            }`}
          >
            Run Review
          </button>
        )}

        {/* ── What gets checked preview ── */}
        {!isLoading && (
          <div className="mt-8 rounded-2xl border border-[#E8E8E8] bg-white p-5 shadow-sm">
            <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-400">
              What gets checked
            </p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {SECTION_ORDER.map(s => (
                <div key={s} className="rounded-lg bg-[#F5F5F5] px-3 py-2.5 text-center">
                  <p className="text-[11px] font-semibold text-gray-600 leading-tight">{s}</p>
                </div>
              ))}
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
