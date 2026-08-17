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
import { Badge, Empty, PanelTitle, PhoneSourceDetails, SearchBox, formatDate, isManualPhoneSource, textMatchesSearch, useDisplayTimeZone } from "../shared/LegacyDisplay";

export function PhoneView({
  phones,
  accounts,
  busy,
  onImport,
  onExport,
  onRefreshStatuses,
  onUpdateBindings,
  onDelete,
  onDeleteMany,
}: {
  phones: PhoneNumber[];
  accounts: Account[];
  busy: boolean;
  onImport: (content: string) => Promise<void> | void;
  onExport: () => Promise<void>;
  onRefreshStatuses: () => Promise<void> | void;
  onUpdateBindings: (id: number, accountEmails: string[]) => Promise<void> | void;
  onDelete: (id: number) => Promise<void> | void;
  onDeleteMany: (ids: number[]) => Promise<void> | void;
}) {
  const timeZone = useDisplayTimeZone();
  const [content, setContent] = useState("");
  const [phoneSearch, setPhoneSearch] = useState("");
  const [selectedPhone, setSelectedPhone] = useState<PhoneNumber | null>(null);
  const [selectedPhoneIds, setSelectedPhoneIds] = useState<Set<number>>(() => new Set());
  const selectAllPhonesRef = useRef<HTMLInputElement>(null);
  const filteredPhones = useMemo(
    () =>
      phones.filter((phone) =>
        textMatchesSearch(
          [phone.phone_number, phone.sms_url, phone.sms_cdk, phone.sms_recharge_url, phone.account_emails.join(" "), phone.updated_at, phone.created_at],
          phoneSearch,
        ),
      ),
    [phoneSearch, phones],
  );
  const allVisiblePhonesSelected = filteredPhones.length > 0
    && filteredPhones.every((phone) => selectedPhoneIds.has(phone.id));
  const someVisiblePhonesSelected = filteredPhones.some((phone) => selectedPhoneIds.has(phone.id));

  useEffect(() => {
    const availableIds = new Set(phones.map((phone) => phone.id));
    setSelectedPhoneIds((current) => {
      const next = new Set([...current].filter((phoneId) => availableIds.has(phoneId)));
      return next.size === current.size ? current : next;
    });
    if (selectedPhone && !availableIds.has(selectedPhone.id)) setSelectedPhone(null);
  }, [phones, selectedPhone]);

  useEffect(() => {
    if (selectAllPhonesRef.current) {
      selectAllPhonesRef.current.indeterminate = someVisiblePhonesSelected && !allVisiblePhonesSelected;
    }
  }, [allVisiblePhonesSelected, someVisiblePhonesSelected]);

  const toggleVisiblePhones = () => {
    setSelectedPhoneIds((current) => {
      const next = new Set(current);
      if (allVisiblePhonesSelected) filteredPhones.forEach((phone) => next.delete(phone.id));
      else filteredPhones.forEach((phone) => next.add(phone.id));
      return next;
    });
  };
  const togglePhoneSelection = (phoneId: number) => {
    setSelectedPhoneIds((current) => {
      const next = new Set(current);
      if (next.has(phoneId)) next.delete(phoneId);
      else next.add(phoneId);
      return next;
    });
  };
  const deleteSelectedPhones = () => {
    const phoneIds = [...selectedPhoneIds];
    if (!phoneIds.length) return;
    if (window.confirm(`确定删除选中的 ${phoneIds.length} 个手机号吗？此操作不可撤销。`)) {
      void onDeleteMany(phoneIds);
    }
  };

  return (
    <div className="stack">
      <section className="panel">
        <PanelTitle title="导入手机号" icon={Smartphone} />
        <form
          className="import-form"
          onSubmit={(event) => {
            event.preventDefault();
            onImport(content);
          }}
        >
          <div className="import-controls">
            <div className="form-hint">格式: `17312739425----http://...`、`+13202952260----SMSRTPBXUZK5Y5TVU33` 或 `+13202952260----SMSRTPBXUZK5Y5TVU33----https://chongpt.xyz/recharge`；CDK 会标记为手动处理，自动 OAuth 不会请求接码。</div>
            <div className="toolbar-actions">
              <button className="secondary-button" disabled={busy || !phones.length} onClick={() => onExport().catch(() => undefined)} type="button">
                <Save size={17} />
                <span>导出</span>
              </button>
              <button className="primary-button" disabled={busy || !content.trim()} type="submit">
                <Smartphone size={17} />
                <span>导入</span>
              </button>
            </div>
          </div>
          <textarea
            onChange={(event) => setContent(event.target.value)}
            placeholder={"17312739425----http://qk.sms777.top/sms/api/get_orange_sms?app_id=480&phone=17312739425\n+17312739426----https://example.com/sms?id=2\n+13202952260----SMSRTPBXUZK5Y5TVU33\n+13202952261----SMSRTPBXUZK5Y5TVU34----https://chongpt.xyz/recharge"}
            rows={6}
            value={content}
          />
        </form>
      </section>

      <section className="panel">
        <div className="panel-toolbar">
          <PanelTitle title="手机号列表" icon={ShieldCheck} />
          <div className="toolbar-actions">
            <SearchBox
              count={filteredPhones.length}
              onChange={setPhoneSearch}
              placeholder="搜索手机号 / 链接 / 账号"
              total={phones.length}
              value={phoneSearch}
            />
            <span className="resource-selection-count" aria-live="polite">已选 {selectedPhoneIds.size}</span>
            <button
              className="danger-button resource-bulk-delete"
              disabled={busy || selectedPhoneIds.size === 0}
              onClick={deleteSelectedPhones}
              type="button"
            >
              <Trash2 size={16} />
              <span>删除已选</span>
            </button>
            <button className="secondary-button" disabled={busy || !phones.length} onClick={() => Promise.resolve(onRefreshStatuses()).catch(() => undefined)} type="button">
              <RefreshCcw size={17} />
              <span>检查接码</span>
            </button>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="resource-select-cell">
                  <input
                    aria-label="全选当前筛选手机号"
                    checked={allVisiblePhonesSelected}
                    disabled={busy || filteredPhones.length === 0}
                    onChange={toggleVisiblePhones}
                    ref={selectAllPhonesRef}
                    type="checkbox"
                  />
                </th>
                <th>手机号</th>
                <th>状态</th>
                <th>接码信息</th>
                <th>绑定账号</th>
                <th>绑定数</th>
                <th>最近更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filteredPhones.map((phone) => {
                const smsSummary = phoneSmsSummary(phone, timeZone);
                return (
                  <tr className={selectedPhoneIds.has(phone.id) ? "is-selected" : ""} key={phone.id}>
                    <td className="resource-select-cell">
                      <input
                        aria-label={`选择手机号 ${phone.phone_number}`}
                        checked={selectedPhoneIds.has(phone.id)}
                        disabled={busy}
                        onChange={() => togglePhoneSelection(phone.id)}
                        type="checkbox"
                      />
                    </td>
                    <td className="mono">{phone.phone_number}</td>
                    <td>
                      <div className="status-stack">
                        <Badge tone={smsSummary.tone}>{smsSummary.label}</Badge>
                        <span>{smsSummary.detail}</span>
                      </div>
                    </td>
                    <td className="phone-source-cell">
                      <PhoneSourceDetails smsCdk={phone.sms_cdk} smsRechargeUrl={phone.sms_recharge_url} smsUrl={phone.sms_url} />
                    </td>
                    <td>
                      <div className="account-chip-list">
                        {phone.account_emails.length
                          ? phone.account_emails.map((email) => (
                              <span className="history-metric-tag" key={email}>
                                {email}
                              </span>
                            ))
                          : <span className="muted">未绑定</span>}
                      </div>
                    </td>
                    <td>{`${phone.bindings_count}/3`}</td>
                    <td>{formatDate(phone.updated_at || phone.created_at, timeZone)}</td>
                    <td className="right">
                      <div className="row-actions">
                        <button className="icon-button" disabled={busy} onClick={() => setSelectedPhone(phone)} title="编辑绑定" type="button">
                          <Link2 size={16} />
                        </button>
                        <button className="icon-button danger" disabled={busy} onClick={() => onDelete(phone.id)} title="删除" type="button">
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!phones.length ? <Empty label="暂无手机号" /> : null}
          {phones.length > 0 && !filteredPhones.length ? <Empty label="没有匹配手机号" /> : null}
        </div>
      </section>
      {selectedPhone ? (
        <PhoneBindingDialog
          accounts={accounts}
          busy={busy}
          phone={selectedPhone}
          onClose={() => setSelectedPhone(null)}
          onSave={(accountEmails) => onUpdateBindings(selectedPhone.id, accountEmails)}
        />
      ) : null}
    </div>
  );
}

function PhoneBindingDialog({
  phone,
  accounts,
  busy,
  onClose,
  onSave,
}: {
  phone: PhoneNumber;
  accounts: Account[];
  busy: boolean;
  onClose: () => void;
  onSave: (accountEmails: string[]) => Promise<void> | void;
}) {
  const [content, setContent] = useState(phone.account_emails.join("\n"));
  const candidates = useMemo(() => [...new Set(accounts.map((account) => account.email))].sort(), [accounts]);
  const parsedEmails = useMemo(
    () => [...new Set(content.split(/\r?\n|,/).map((item) => item.trim().toLowerCase()).filter(Boolean))],
    [content],
  );
  const invalid = parsedEmails.length > 3;

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-modal="true" className="mail-dialog phone-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">手机号绑定</p>
            <h2>{phone.phone_number}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="phone-dialog-grid">
          <div className="phone-dialog-card">
            <strong>接码信息</strong>
            <PhoneSourceDetails smsCdk={phone.sms_cdk} smsRechargeUrl={phone.sms_recharge_url} smsUrl={phone.sms_url} />
          </div>
          <div className="phone-dialog-card">
            <strong>候选账号</strong>
            <div className="account-chip-list">
              {candidates.slice(0, 20).map((email) => (
                <span className="history-metric-tag" key={email}>{email}</span>
              ))}
            </div>
          </div>
        </div>
        <label>
          绑定账号
          <textarea onChange={(event) => setContent(event.target.value)} rows={6} value={content} />
        </label>
        <p className="form-hint">每行一个邮箱，最多 3 个。已绑定到其他手机号的账号会自动移动到当前手机号。</p>
        {invalid ? <div className="mail-error">一个手机号最多绑定 3 个账号。</div> : null}
        <div className="settings-actions">
          <div className="key-state">
            <Smartphone size={16} />
            <span>当前 {parsedEmails.length}/3 个账号</span>
          </div>
          <button
            className="primary-button"
            disabled={busy || invalid}
            onClick={() => {
              Promise.resolve(onSave(parsedEmails)).then(() => onClose()).catch(() => undefined);
            }}
            type="button"
          >
            <Save size={17} />
            <span>保存绑定</span>
          </button>
        </div>
      </section>
    </div>
  );
}

function phoneSmsSummary(phone: PhoneNumber, timeZone: string) {
  const status = String(phone.sms_status || "").trim().toLowerCase();
  const error = String(phone.sms_error || "").trim();
  const checkedAt = phone.sms_checked_at ? formatDate(phone.sms_checked_at, timeZone) : "未检查";
  if (isManualPhoneSource(phone.sms_url, phone.sms_cdk) || status === "manual") {
    return { label: "手动", tone: "violet", detail: phone.sms_cdk ? `CDK: ${phone.sms_cdk}` : error || checkedAt };
  }
  if (!status) {
    return { label: "未检查", tone: "ink", detail: checkedAt };
  }
  if (status === "ok") {
    return { label: "可用", tone: "ok", detail: checkedAt };
  }
  const loweredError = error.toLowerCase();
  if (error.includes("不可用") || loweredError.includes("sms url is unavailable")) {
    return { label: "不可用", tone: "error", detail: error || checkedAt };
  }
  if (error.includes("已过期") || loweredError.includes("expired")) {
    return { label: "已过期", tone: "error", detail: error || checkedAt };
  }
  if (status === "not_found") {
    return { label: "可访问", tone: "info", detail: error || checkedAt };
  }
  return { label: "检查失败", tone: "warn", detail: error || checkedAt };
}
