'use client'
import { useEffect, useState } from 'react'

export interface PDFViewerModalProps {
  url:           string          // GET endpoint, no #fragment, no query-string token
  page?:         number          // drives the #page=N fragment
  authToken?:    string          // set -> fetch+blob with Authorization header; unset -> iframe src=url directly (today's public path)
  headerLabel:   string
  headerDetail?: string          // e.g. "§ Section 104" or a narrative heading
  headerPage?:   string | number // e.g. printed page number
  onClose:       () => void
}

export default function PDFViewerModal({
  url, page, authToken, headerLabel, headerDetail, headerPage, onClose,
}: PDFViewerModalProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [error,   setError]   = useState<string | null>(null)
  const [loading, setLoading] = useState(!!authToken)

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  useEffect(() => {
    if (!authToken) return
    let cancelled = false
    let objectUrl: string | null = null
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true)
    setError(null)
    fetch(url, { headers: { Authorization: `Bearer ${authToken}` } })
      .then(res => { if (!res.ok) throw new Error(`Failed to load PDF (${res.status})`); return res.blob() })
      .then(blob => { if (!cancelled) setBlobUrl(objectUrl = URL.createObjectURL(blob)) })
      .catch(err => { if (!cancelled) setError(err.message ?? 'Failed to load PDF') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [url, authToken])

  const src = authToken ? (blobUrl ? `${blobUrl}#page=${page ?? 1}` : undefined)
                         : `${url}#page=${page ?? 1}`

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
         onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="flex flex-col bg-white rounded-2xl shadow-2xl w-[96vw] h-[92vh] max-w-5xl sm:w-[92vw] sm:h-[90vh]">
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#E8E8E8] shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <span className="text-[10px] font-bold uppercase tracking-widest text-[#1B3A6B] truncate">
              {headerLabel || 'NJDOT Document'}
            </span>
            {headerDetail && <span className="text-sm text-gray-500 truncate">{headerDetail}</span>}
            {headerPage && <span className="text-sm text-gray-400 shrink-0">p. {headerPage}</span>}
          </div>
          <button type="button" onClick={onClose} className="ml-4 shrink-0 rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700" aria-label="Close">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        {error ? (
          <div className="flex-1 flex items-center justify-center text-sm text-red-600">{error}</div>
        ) : loading ? (
          <div className="flex-1 flex items-center justify-center text-sm text-gray-400">Loading PDF…</div>
        ) : (
          <iframe src={src} className="flex-1 w-full rounded-b-2xl" title={`PDF: ${headerLabel}`} />
        )}
      </div>
    </div>
  )
}
