export type RefreshJob = {
  id: number;
  email: string;
  management_account_id: string | null;
  status: string;
  reason: string | null;
  access_token_tail: string | null;
  memory_peak_rss_bytes: number | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type AppEvent = {
  id: number;
  kind: string;
  email: string | null;
  message: string;
  details: Record<string, unknown> | null;
  created_at: string;
};

export type AccountExceptionRecord = {
  id: number;
  email: string | null;
  management_account_id: string | null;
  source: string;
  status: string;
  message: string;
  details: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};
