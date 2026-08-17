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
  code?: string | null;
  received_at: string | null;
};

export type MailboxImportResult = {
  message: string;
  imported: number;
  skipped: number;
  invalid_lines: number[];
};

export type MailboxCredentialDetail = {
  id: number;
  gpt_email: string;
  mailbox_email: string;
  provider: string;
  password: string | null;
  client_id: string | null;
  refresh_token: string | null;
  access_token: string | null;
  custom_fetch_url: string | null;
  proxy_url: string | null;
  import_line: string;
};

export type MailboxExportResult = {
  message: string;
  exported: number;
  content: string;
};

export type PhoneNumber = {
  id: number;
  phone_number: string;
  sms_url: string;
  sms_cdk: string | null;
  sms_recharge_url: string | null;
  account_emails: string[];
  bindings_count: number;
  sms_status: string | null;
  sms_error: string | null;
  sms_checked_at: string | null;
  created_at: string;
  updated_at: string;
};

export type PhoneImportResult = {
  message: string;
  imported: number;
  updated: number;
  skipped: number;
  invalid_lines: number[];
};

export type BulkDeleteResult = {
  message: string;
  requested_count: number;
  deleted_count: number;
};
