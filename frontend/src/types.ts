export type Summary = {
  total_accounts: number;
  error_accounts: number;
  deactive_accounts: number;
  refreshing_accounts: number;
  mailbox_count: number;
  recent_success: number;
  recent_failed: number;
};

export type Account = {
  id: number;
  email: string;
  sub2api_account_id: string | null;
  platform: string | null;
  account_type: string | null;
  status: string | null;
  schedulable: boolean | null;
  deactive: boolean;
  refreshing: boolean;
  last_error: string | null;
  last_seen_at: string;
  updated_at: string;
};

export type Mailbox = {
  id: number;
  gpt_email: string;
  mailbox_email: string;
  provider: string;
  disabled: boolean;
  last_error: string | null;
  last_success_at: string | null;
  created_at: string;
  updated_at: string;
};

export type MailMessage = {
  id: string;
  folder: "inbox" | "junk";
  subject: string | null;
  sender_name: string | null;
  sender_address: string | null;
  body_preview: string | null;
  received_at: string | null;
};

export type MailboxImportResult = {
  message: string;
  imported: number;
  skipped: number;
  invalid_lines: number[];
};

export type RefreshJob = {
  id: number;
  email: string;
  sub2api_account_id: string | null;
  status: string;
  reason: string | null;
  access_token_tail: string | null;
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

export type SyncResult = {
  total_seen: number;
  error_seen: number;
  queued: number;
};
