export type User = { name: string; actor?: string; role: string; tenant?: string }
export type Snapshot = { id: string; version: string; coverage: string; repository_id?: string }
export type Citation = { id: string; file: string; line: number; text: string; locator?: string }
export type SourceAnswer = { status: string; snapshot_id: string; answer_id?: string; answer: string; citations: Citation[]; unknowns?: string[] }
export type Manual = { id: number; page_id: string; release: string; title: string; content: string; status: string; digest: string }
export type Help = { release: string; knowledge_snapshot_id: number; pages: Array<{ page_id: string; title: string; content: string; content_html: string }> }
export type CustomerAnswer = { question_id: string; answer: string; status: string; release: string; citations: Array<{ page_id: string; title: string }> }
export type Execution = { id: string; status: string; result?: { summary?: { total: number } }; created_at?: number }
