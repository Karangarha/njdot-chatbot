export interface BDCAlertItem {
  bdc_id: string
  section_id: string
  effective_date?: string
  subject?: string
  implementation_code?: string
  change_type?: string
}

export interface CitationItem {
  document: string
  section: string
  page_printed: number
  page_pdf: number
  chunk_id: string
}

export interface QueryResponse {
  answer: string
  citations: CitationItem[]
  query_type: string
  response_time_ms: number
  bdc_alerts: BDCAlertItem[]
}

export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ConversationMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  citations: CitationItem[]
  bdc_alerts: BDCAlertItem[]
  created_at: string
}

export interface ReviewProject {
  id: string
  session_id: string | null
  project_name: string
  review_result: any
  created_at: string
  updated_at: string
}
