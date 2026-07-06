export interface Principal {
  tenant_id: string;
  display_name: string;
  kind: "tenant";
  scopes: string[];
  expires_at: number;
}

export interface Digest {
  filename: string;
  timestamp: string;
  title: string;
  cn_title: string;
  keywords: string;
  journal: string;
  source: "rss" | "pdf" | string;
  preview: string;
  disliked: boolean;
  interested: boolean;
  is_read: boolean;
  relevance_score: number | null;
  novelty_score: number | null;
  final_score: number | null;
  recommendation_type: string;
}

export interface DigestContent {
  filename: string;
  title: string;
  source: string;
  created_at: string;
  content: string;
  original_url: string;
  pdf_available: boolean;
}

export interface TaskProgress {
  active: boolean;
  current: number;
  total: number;
  message: string;
}

export interface ProgressResponse {
  rss: TaskProgress;
  pdf: TaskProgress;
}

export interface Feed {
  title: string;
  url: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatResponse {
  reply: string;
  error: string;
  history_summary: string;
  context_compressed: boolean;
}

export interface UploadResponse {
  ok: boolean;
  uploaded: number;
  paths: string[];
  errors: Array<{ filename?: string; error?: string }>;
  complete?: boolean;
  next_index?: number;
}
