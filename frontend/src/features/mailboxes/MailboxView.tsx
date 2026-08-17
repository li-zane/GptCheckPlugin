import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ArrowUpDown,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CheckCircle2,
  CircleHelp,
  Clock3,
  Copy,
  Database,
  Download,
  ExternalLink,
  Globe2,
  Inbox,
  Image as ImageIcon,
  KeyRound,
  Link2,
  LogOut,
  Mail,
  MailOpen,
  Moon,
  PauseCircle,
  Pencil,
  Play,
  Plus,
  Radar,
  RefreshCcw,
  Save,
  Search,
  Send,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Smartphone,
  Sparkles,
  StickyNote,
  Sun,
  TimerReset,
  Trash2,
  Upload,
  UserRoundX,
  UsersRound,
  X,
  ZoomIn,
  type LucideIcon,
} from "lucide-react";
import { FormEvent, lazy, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, upstreamLegacyBindingCounts } from "../../shared/api";
import type {
  Account,
  AccountExceptionRecord,
  AccountLivenessModel,
  AccountLivenessTestResult,
  AccountNotes,
  AccountUsageEstimate,
  ApiKeyViewOperation,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  Mailbox,
  MailboxCredentialDetail,
  MailMessage,
  PhoneNumber,
  RefreshJob,
  SelectedAccountDeleteItem,
  Summary,
  UsageEstimate,
  UsageGroupRef,
  UsageLimitSamples,
  UsageLimitDefaultRanges,
  UsageLimitPlanRanges,
  UsageTokenHistory,
  UsageWindowAggregate,
  UsageWindowEstimate,
  UpstreamChangeLog,
  Upstream,
  UpstreamOverviewResponse,
} from "../../domain";
import { CopyTextButton, Empty, MailMessageDialog, PanelTitle, SearchBox, copyTextToClipboard, downloadTextFile, formatDate, textMatchesSearch, useDisplayTimeZone } from "../shared/LegacyDisplay";

export function MailboxView({
  mailboxes,
  busy,
  onImport,
  onDelete,
  onDeleteMany,
  onNotice,
}: {
  mailboxes: Mailbox[];
  busy: boolean;
  onImport: (content: string, provider: string) => void;
  onDelete: (id: number) => void;
  onDeleteMany: (ids: number[]) => Promise<void> | void;
  onNotice: (message: string) => void;
}) {
  const timeZone = useDisplayTimeZone();
  const [content, setContent] = useState("");
  const [provider, setProvider] = useState("auto");
  const [selectedMailbox, setSelectedMailbox] = useState<Mailbox | null>(null);
  const [credentialMailbox, setCredentialMailbox] = useState<Mailbox | null>(null);
  const [credentialDetail, setCredentialDetail] = useState<MailboxCredentialDetail | null>(null);
  const [credentialLoading, setCredentialLoading] = useState(false);
  const [credentialError, setCredentialError] = useState("");
  const [credentialActionError, setCredentialActionError] = useState("");
  const [credentialExporting, setCredentialExporting] = useState(false);
  const [folder, setFolder] = useState<"inbox" | "junk">("inbox");
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [mailLoading, setMailLoading] = useState(false);
  const [mailError, setMailError] = useState("");
  const [mailboxSearch, setMailboxSearch] = useState("");
  const [selectedMailboxIds, setSelectedMailboxIds] = useState<Set<number>>(() => new Set());
  const selectAllMailboxesRef = useRef<HTMLInputElement>(null);
  const credentialRequestSequenceRef = useRef(0);
  const filteredMailboxes = useMemo(
    () =>
      mailboxes.filter((mailbox) =>
        textMatchesSearch(
          [
            mailbox.gpt_email,
            mailbox.mailbox_email,
            mailbox.provider,
            mailbox.disabled ? "停用 disabled" : "启用 enabled",
            mailbox.last_error,
            mailbox.last_success_at,
          ],
          mailboxSearch,
        ),
      ),
    [mailboxSearch, mailboxes],
  );
  const allVisibleMailboxesSelected = filteredMailboxes.length > 0
    && filteredMailboxes.every((mailbox) => selectedMailboxIds.has(mailbox.id));
  const someVisibleMailboxesSelected = filteredMailboxes.some((mailbox) => selectedMailboxIds.has(mailbox.id));

  const supportsJunk = selectedMailbox ? ["outlook", "hotmail", "gmail"].includes(selectedMailbox.provider) : false;
  const openMessages = (mailbox: Mailbox) => {
    setSelectedMailbox(mailbox);
    setFolder("inbox");
    setMessages([]);
    setMailError("");
  };
  const openCredentialDetail = (mailbox: Mailbox) => {
    const sequence = ++credentialRequestSequenceRef.current;
    setCredentialMailbox(mailbox);
    setCredentialDetail(null);
    setCredentialError("");
    setCredentialActionError("");
    setCredentialLoading(true);
    api.mailboxCredentials(mailbox.id)
      .then((detail) => {
        if (sequence === credentialRequestSequenceRef.current) setCredentialDetail(detail);
      })
      .catch((error) => {
        if (sequence === credentialRequestSequenceRef.current) {
          setCredentialError(error instanceof Error ? error.message : "读取邮箱凭据失败");
        }
      })
      .finally(() => {
        if (sequence === credentialRequestSequenceRef.current) setCredentialLoading(false);
      });
  };
  const closeCredentialDetail = () => {
    credentialRequestSequenceRef.current += 1;
    setCredentialMailbox(null);
    setCredentialDetail(null);
    setCredentialError("");
    setCredentialLoading(false);
  };
  const exportMailboxIds = async (mailboxIds: number[], fileName: string) => {
    if (!mailboxIds.length) return;
    setCredentialActionError("");
    onNotice("");
    setCredentialExporting(true);
    try {
      const result = await api.exportMailboxes(mailboxIds);
      downloadTextFile(fileName, result.content);
      onNotice(`已导出 ${result.exported} 个邮箱凭据`);
    } catch (error) {
      setCredentialActionError(error instanceof Error ? error.message : "导出邮箱凭据失败");
    } finally {
      setCredentialExporting(false);
    }
  };
  const copyMailboxIds = async (mailboxIds: number[]) => {
    if (!mailboxIds.length) return;
    setCredentialActionError("");
    onNotice("");
    setCredentialExporting(true);
    try {
      const result = await api.exportMailboxes(mailboxIds);
      await copyTextToClipboard(result.content);
      onNotice(`已按导入格式复制 ${result.exported} 个邮箱凭据`);
    } catch (error) {
      setCredentialActionError(error instanceof Error ? error.message : "复制邮箱凭据失败");
    } finally {
      setCredentialExporting(false);
    }
  };

  useEffect(() => {
    if (!selectedMailbox) return;
    const normalizedFolder = supportsJunk ? folder : "inbox";
    let alive = true;
    setMailLoading(true);
    setMailError("");
    api
      .mailboxMessages(selectedMailbox.id, normalizedFolder)
      .then((nextMessages) => {
        if (alive) setMessages(nextMessages);
      })
      .catch((error) => {
        if (alive) setMailError(error instanceof Error ? error.message : "读取邮件失败");
      })
      .finally(() => {
        if (alive) setMailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [folder, selectedMailbox, supportsJunk]);

  useEffect(() => {
    const availableIds = new Set(mailboxes.map((mailbox) => mailbox.id));
    setSelectedMailboxIds((current) => {
      const next = new Set([...current].filter((mailboxId) => availableIds.has(mailboxId)));
      return next.size === current.size ? current : next;
    });
    if (selectedMailbox && !availableIds.has(selectedMailbox.id)) setSelectedMailbox(null);
    if (credentialMailbox && !availableIds.has(credentialMailbox.id)) {
      credentialRequestSequenceRef.current += 1;
      setCredentialMailbox(null);
      setCredentialDetail(null);
      setCredentialError("");
      setCredentialLoading(false);
    }
  }, [credentialMailbox, mailboxes, selectedMailbox]);

  useEffect(() => {
    if (selectAllMailboxesRef.current) {
      selectAllMailboxesRef.current.indeterminate = someVisibleMailboxesSelected && !allVisibleMailboxesSelected;
    }
  }, [allVisibleMailboxesSelected, someVisibleMailboxesSelected]);

  const toggleVisibleMailboxes = () => {
    setSelectedMailboxIds((current) => {
      const next = new Set(current);
      if (allVisibleMailboxesSelected) filteredMailboxes.forEach((mailbox) => next.delete(mailbox.id));
      else filteredMailboxes.forEach((mailbox) => next.add(mailbox.id));
      return next;
    });
  };
  const toggleMailboxSelection = (mailboxId: number) => {
    setSelectedMailboxIds((current) => {
      const next = new Set(current);
      if (next.has(mailboxId)) next.delete(mailboxId);
      else next.add(mailboxId);
      return next;
    });
  };
  const deleteSelectedMailboxes = () => {
    const mailboxIds = [...selectedMailboxIds];
    if (!mailboxIds.length) return;
    if (window.confirm(`确定删除选中的 ${mailboxIds.length} 个邮箱吗？此操作不可撤销。`)) {
      void onDeleteMany(mailboxIds);
    }
  };

  return (
    <div className="stack">
      <section className="panel">
        <PanelTitle title="导入邮箱" icon={Mail} />
        <form
          className="import-form"
          onSubmit={(event) => {
            event.preventDefault();
            onImport(content, provider);
          }}
        >
          <div className="import-controls">
            <label>
              识别方式
              <select onChange={(event) => setProvider(event.target.value)} value={provider}>
                <option value="auto">按后缀自动</option>
                <option value="outlook">Outlook</option>
                <option value="hotmail">Hotmail</option>
                <option value="gmail">Gmail / Google Workspace</option>
                <option value="url">URL 取件</option>
                <option value="custom">Custom HTTP</option>
                <option value="manual">Manual</option>
              </select>
            </label>
            <button className="primary-button" disabled={busy || !content.trim()} type="submit">
              <Mail size={17} />
              <span>导入</span>
            </button>
          </div>
          <textarea
            onChange={(event) => setContent(event.target.value)}
            placeholder={"gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token\nurl@example.com----https://mail.example.com/messages/TOKEN/url%40example.com\naccount@example.com----inbox@example.com----https://mail.example.com/messages/TOKEN/inbox%40example.com"}
            rows={6}
            value={content}
          />
          <p className="form-hint">支持批量导入，一行一个；URL 取件支持“GPT 邮箱----取件 URL”或“GPT 邮箱----取件邮箱----取件 URL”。</p>
        </form>
      </section>

      <section className="panel">
        <div className="panel-toolbar">
          <PanelTitle title="邮箱凭据" icon={ShieldCheck} />
          <div className="toolbar-actions">
            <SearchBox
              count={filteredMailboxes.length}
              onChange={setMailboxSearch}
              placeholder="搜索 GPT / 取件邮箱"
              total={mailboxes.length}
              value={mailboxSearch}
            />
            <span className="resource-selection-count" aria-live="polite">已选 {selectedMailboxIds.size}</span>
            <button
              className="secondary-button resource-bulk-copy"
              disabled={busy || credentialExporting || selectedMailboxIds.size === 0}
              onClick={() => void copyMailboxIds([...selectedMailboxIds])}
              type="button"
            >
              <Copy size={16} />
              <span>复制已选</span>
            </button>
            <button
              className="secondary-button resource-bulk-export"
              disabled={busy || credentialExporting || selectedMailboxIds.size === 0}
              onClick={() => void exportMailboxIds([...selectedMailboxIds], "mailboxes.txt")}
              type="button"
            >
              <Download size={16} />
              <span>导出已选</span>
            </button>
            <button
              className="danger-button resource-bulk-delete"
              disabled={busy || credentialExporting || selectedMailboxIds.size === 0}
              onClick={deleteSelectedMailboxes}
              type="button"
            >
              <Trash2 size={16} />
              <span>删除已选</span>
            </button>
          </div>
        </div>
        {credentialActionError ? <p className="mail-error">{credentialActionError}</p> : null}
        <div className="table-wrap">
          <table className="mailbox-table">
            <colgroup>
              <col className="mailbox-col-select" />
              <col className="mailbox-col-gpt" />
              <col className="mailbox-col-address" />
              <col className="mailbox-col-provider" />
              <col className="mailbox-col-success" />
              <col className="mailbox-col-error" />
              <col className="mailbox-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th className="resource-select-cell">
                  <input
                    aria-label="全选当前筛选邮箱"
                    checked={allVisibleMailboxesSelected}
                    disabled={busy || filteredMailboxes.length === 0}
                    onChange={toggleVisibleMailboxes}
                    ref={selectAllMailboxesRef}
                    type="checkbox"
                  />
                </th>
                <th>GPT 邮箱</th>
                <th>取件邮箱</th>
                <th>类型</th>
                <th>最近成功</th>
                <th>错误</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filteredMailboxes.map((mailbox) => (
                <tr className={selectedMailboxIds.has(mailbox.id) ? "is-selected" : ""} key={mailbox.id}>
                  <td className="resource-select-cell">
                    <input
                      aria-label={`选择邮箱 ${mailbox.gpt_email}`}
                      checked={selectedMailboxIds.has(mailbox.id)}
                      disabled={busy}
                      onChange={() => toggleMailboxSelection(mailbox.id)}
                      type="checkbox"
                    />
                  </td>
                  <td>
                    <CopyTextButton className="mailbox-field-copy mono" middleEllipsis title="复制 GPT 邮箱" value={mailbox.gpt_email} />
                  </td>
                  <td>
                    <CopyTextButton className="mailbox-field-copy mono" middleEllipsis title="复制取件邮箱" value={mailbox.mailbox_email} />
                  </td>
                  <td>
                    <CopyTextButton className="mailbox-field-copy" middleEllipsis title="复制类型" value={mailbox.provider} />
                  </td>
                  <td>
                    <CopyTextButton
                      className="mailbox-field-copy"
                      displayValue={mailbox.last_success_at ? formatDate(mailbox.last_success_at, timeZone) : "-"}
                      middleEllipsis
                      title="复制最近成功时间"
                      value={mailbox.last_success_at || ""}
                    />
                  </td>
                  <td>
                    <CopyTextButton className="mailbox-field-copy" middleEllipsis title="复制错误信息" value={mailbox.last_error || "-"} />
                  </td>
                  <td className="right">
                    <div className="row-actions">
                      <button className="icon-button" disabled={busy} onClick={() => openMessages(mailbox)} title="查看邮件" type="button">
                        <MailOpen size={16} />
                      </button>
                      <button className="icon-button" disabled={busy} onClick={() => openCredentialDetail(mailbox)} title="查看凭据详情" type="button">
                        <KeyRound size={16} />
                      </button>
                      <button
                        className="icon-button"
                        disabled={busy || credentialExporting}
                        onClick={() => void copyMailboxIds([mailbox.id])}
                        title="按导入格式复制"
                        type="button"
                      >
                        <Copy size={16} />
                      </button>
                      <button
                        className="icon-button"
                        disabled={busy || credentialExporting}
                        onClick={() => void exportMailboxIds(
                          [mailbox.id],
                          `mailbox-${mailbox.gpt_email.replace(/[^a-z0-9@._-]/gi, "_")}.txt`,
                        )}
                        title="导出邮箱凭据"
                        type="button"
                      >
                        <Download size={16} />
                      </button>
                      <button className="icon-button danger" disabled={busy} onClick={() => onDelete(mailbox.id)} title="删除" type="button">
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!mailboxes.length ? <Empty label="暂无邮箱凭据" /> : null}
          {mailboxes.length > 0 && !filteredMailboxes.length ? <Empty label="没有匹配邮箱" /> : null}
        </div>
      </section>
      {selectedMailbox ? (
        <MailMessageDialog
          folder={supportsJunk ? folder : "inbox"}
          loading={mailLoading}
          mailbox={selectedMailbox}
          messages={messages}
          error={mailError}
          supportsJunk={supportsJunk}
          onClose={() => setSelectedMailbox(null)}
          onFolderChange={setFolder}
        />
      ) : null}
      {credentialMailbox ? (
        <MailboxCredentialDialog
          detail={credentialDetail}
          error={credentialError}
          exportError={credentialActionError}
          exporting={credentialExporting}
          loading={credentialLoading}
          mailbox={credentialMailbox}
          onClose={closeCredentialDetail}
          onExport={() => void exportMailboxIds(
            [credentialMailbox.id],
            `mailbox-${credentialMailbox.gpt_email.replace(/[^a-z0-9@._-]/gi, "_")}.txt`,
          )}
        />
      ) : null}
    </div>
  );
}

function MailboxCredentialDialog({
  mailbox,
  detail,
  loading,
  error,
  exportError,
  exporting,
  onClose,
  onExport,
}: {
  mailbox: Mailbox;
  detail: MailboxCredentialDetail | null;
  loading: boolean;
  error: string;
  exportError: string;
  exporting: boolean;
  onClose: () => void;
  onExport: () => void;
}) {
  const fields = detail ? [
    ["GPT 邮箱", detail.gpt_email],
    ["取件邮箱", detail.mailbox_email],
    ["类型", detail.provider],
    ["邮箱密码", detail.password],
    ["Client ID", detail.client_id],
    ["Refresh Token", detail.refresh_token],
    ["Access Token", detail.access_token],
    ["取件 URL", detail.custom_fetch_url],
    ["代理 URL", detail.proxy_url],
  ] as const : [];
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-modal="true" className="mail-dialog mailbox-credential-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">邮箱凭据详情</p>
            <h2>{mailbox.gpt_email}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        {loading ? <div className="mailbox-credential-loading">正在读取凭据...</div> : null}
        {error ? <p className="mail-error">{error}</p> : null}
        {exportError ? <p className="mail-error">{exportError}</p> : null}
        {detail ? (
          <div className="mailbox-credential-grid">
            {fields.map(([label, value]) => (
              <div className="mailbox-credential-field" key={label}>
                <span>{label}</span>
                {value ? (
                  <CopyTextButton
                    className="mailbox-credential-copy mono"
                    title={`复制${label}`}
                    value={value}
                  />
                ) : <strong>-</strong>}
              </div>
            ))}
            <div className="mailbox-credential-field mailbox-credential-import-line">
              <span>导入格式</span>
              <CopyTextButton
                className="mailbox-credential-copy mono"
                title="复制导入格式"
                value={detail.import_line}
              />
            </div>
          </div>
        ) : null}
        <footer className="mailbox-credential-actions">
          <button className="secondary-button" disabled={!detail || loading || exporting} onClick={onExport} type="button">
            <Download size={16} />
            <span>{exporting ? "正在导出..." : "导出凭据"}</span>
          </button>
        </footer>
      </section>
    </div>
  );
}
