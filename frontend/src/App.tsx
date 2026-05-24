import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Inbox,
  KeyRound,
  LogOut,
  Mail,
  MailOpen,
  Play,
  RefreshCcw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Trash2,
  UserRoundX,
  UsersRound,
  X,
  type LucideIcon,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api";
import type { Account, AppEvent, Mailbox, MailMessage, RefreshJob, Summary } from "./types";

type View = "overview" | "accounts" | "mailboxes" | "history";

const emptySummary: Summary = {
  total_accounts: 0,
  error_accounts: 0,
  deactive_accounts: 0,
  refreshing_accounts: 0,
  mailbox_count: 0,
  recent_success: 0,
  recent_failed: 0,
};

function App() {
  const [authState, setAuthState] = useState<"checking" | "in" | "out">("checking");
  const [view, setView] = useState<View>("overview");
  const [summary, setSummary] = useState<Summary>(emptySummary);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]);
  const [jobs, setJobs] = useState<RefreshJob[]>([]);
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const loadAll = useCallback(async () => {
    const [nextSummary, nextAccounts, nextMailboxes, nextJobs, nextEvents] = await Promise.all([
      api.summary(),
      api.accounts(),
      api.mailboxes(),
      api.jobs(),
      api.events(),
    ]);
    setSummary(nextSummary);
    setAccounts(nextAccounts);
    setMailboxes(nextMailboxes);
    setJobs(nextJobs);
    setEvents(nextEvents);
  }, []);

  useEffect(() => {
    api
      .me()
      .then(() => {
        setAuthState("in");
        return loadAll();
      })
      .catch(() => setAuthState("out"));
  }, [loadAll]);

  useEffect(() => {
    if (authState !== "in") return;
    const timer = window.setInterval(() => {
      loadAll().catch(() => undefined);
    }, 12_000);
    return () => window.clearInterval(timer);
  }, [authState, loadAll]);

  const runAction = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    setNotice("");
    try {
      const result = await action();
      setNotice(
        result && typeof result === "object" && "message" in result && typeof result.message === "string"
          ? result.message
          : success,
      );
      await loadAll();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  if (authState === "checking") {
    return <BootScreen />;
  }

  if (authState === "out") {
    return (
      <LoginScreen
        onLogin={async (adminKey) => {
          await api.login(adminKey);
          setAuthState("in");
          await loadAll();
        }}
      />
    );
  }

  const navItems: Array<{ id: View; label: string; icon: LucideIcon }> = [
    { id: "overview", label: "概览", icon: Activity },
    { id: "accounts", label: "账号", icon: UsersRound },
    { id: "mailboxes", label: "邮箱", icon: Mail },
    { id: "history", label: "历史", icon: Clock3 },
  ];

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <ShieldCheck size={22} />
          </div>
          <div>
            <strong>AT Guardian</strong>
            <span>Sub2API</span>
          </div>
        </div>

        <nav className="nav">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={view === item.id ? "nav-item active" : "nav-item"}
                key={item.id}
                onClick={() => setView(item.id)}
                type="button"
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <button
          className="ghost-button"
          type="button"
          onClick={async () => {
            setBusy(true);
            try {
              await api.logout();
            } finally {
              setBusy(false);
              setAuthState("out");
            }
          }}
        >
          <LogOut size={17} />
          <span>退出</span>
        </button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">本机管理面板</p>
            <h1>{titleFor(view)}</h1>
          </div>
          <div className="topbar-actions">
            {notice ? <span className="notice">{notice}</span> : null}
            <button className="secondary-button" disabled={busy} type="button" onClick={() => runAction(api.sync, "同步完成")}>
              <RefreshCcw size={17} />
              <span>同步</span>
            </button>
          </div>
        </header>

        {view === "overview" ? (
          <Overview summary={summary} accounts={accounts} jobs={jobs} events={events} />
        ) : null}
        {view === "accounts" ? (
          <AccountsView accounts={accounts} busy={busy} onRefresh={(email) => runAction(() => api.refresh(email), "已创建刷新任务")} />
        ) : null}
        {view === "mailboxes" ? (
          <MailboxView
            mailboxes={mailboxes}
            busy={busy}
            onImport={(content, provider) => runAction(() => api.importMailboxes(content, provider), "导入完成")}
            onDelete={(id) => runAction(() => api.deleteMailbox(id), "已删除")}
          />
        ) : null}
        {view === "history" ? <HistoryView jobs={jobs} events={events} /> : null}
      </section>
    </main>
  );
}

function LoginScreen({ onLogin }: { onLogin: (adminKey: string) => Promise<void> }) {
  const [adminKey, setAdminKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onLogin(adminKey);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-panel">
        <div className="login-emblem">
          <KeyRound size={28} />
        </div>
        <p className="eyebrow">Sub2API AT Guardian</p>
        <h1>控制台登录</h1>
        <form onSubmit={submit}>
          <label htmlFor="adminKey">管理密钥</label>
          <input
            autoFocus
            id="adminKey"
            name="adminKey"
            onChange={(event) => setAdminKey(event.target.value)}
            placeholder="APP_ADMIN_KEY"
            type="password"
            value={adminKey}
          />
          {error ? <p className="form-error">{error}</p> : null}
          <button className="primary-button wide" disabled={busy || !adminKey} type="submit">
            <ShieldCheck size={18} />
            <span>{busy ? "验证中" : "进入后台"}</span>
          </button>
        </form>
      </section>
      <section className="login-signal">
        <div className="signal-line" />
        <div className="signal-tile">
          <Sparkles size={18} />
          <span>Token Recovery Loop</span>
        </div>
      </section>
    </main>
  );
}

function BootScreen() {
  return (
    <main className="boot">
      <RefreshCcw className="spin" size={24} />
      <span>加载中</span>
    </main>
  );
}

function Overview({
  summary,
  accounts,
  jobs,
  events,
}: {
  summary: Summary;
  accounts: Account[];
  jobs: RefreshJob[];
  events: AppEvent[];
}) {
  const recentAccounts = accounts.slice(0, 6);
  const latestJob = jobs[0];
  const latestEvent = events[0];
  const stats = [
    { label: "账号", value: summary.total_accounts, icon: UsersRound, tone: "ink" },
    { label: "错误", value: summary.error_accounts, icon: AlertTriangle, tone: "warn" },
    { label: "恢复中", value: summary.refreshing_accounts, icon: RefreshCcw, tone: "teal" },
    { label: "停用", value: summary.deactive_accounts, icon: UserRoundX, tone: "danger" },
    { label: "邮箱", value: summary.mailbox_count, icon: Mail, tone: "blue" },
    { label: "24h 成功", value: summary.recent_success, icon: CheckCircle2, tone: "ok" },
  ];

  return (
    <div className="stack">
      <section className="stat-grid">
        {stats.map((stat) => {
          const Icon = stat.icon;
          return (
            <article className={`stat-card ${stat.tone}`} key={stat.label}>
              <Icon size={20} />
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
            </article>
          );
        })}
      </section>

      <section className="split-grid">
        <div className="panel">
          <PanelTitle title="最近账号" icon={UsersRound} />
          <div className="compact-list">
            {recentAccounts.length ? (
              recentAccounts.map((account) => <AccountRow account={account} key={account.email} compact />)
            ) : (
              <Empty label="暂无账号快照" />
            )}
          </div>
        </div>

        <div className="panel">
          <PanelTitle title="运行信号" icon={Activity} />
          <div className="signal-list">
            <SignalLine label="最近任务" value={latestJob ? `${latestJob.email} · ${statusLabel(latestJob.status)}` : "暂无"} />
            <SignalLine label="最近事件" value={latestEvent ? latestEvent.message : "暂无"} />
            <SignalLine label="24h 失败" value={`${summary.recent_failed}`} />
          </div>
        </div>
      </section>
    </div>
  );
}

function AccountsView({
  accounts,
  busy,
  onRefresh,
}: {
  accounts: Account[];
  busy: boolean;
  onRefresh: (email: string) => void;
}) {
  return (
    <section className="panel">
      <PanelTitle title="账号状态" icon={UsersRound} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>邮箱</th>
              <th>状态</th>
              <th>调度</th>
              <th>sub2api ID</th>
              <th>最近更新</th>
              <th>错误</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {accounts.map((account) => (
              <AccountRow account={account} busy={busy} key={account.email} onRefresh={onRefresh} />
            ))}
          </tbody>
        </table>
        {!accounts.length ? <Empty label="尚未同步账号" /> : null}
      </div>
    </section>
  );
}

function AccountRow({
  account,
  busy,
  compact = false,
  onRefresh,
}: {
  account: Account;
  busy?: boolean;
  compact?: boolean;
  onRefresh?: (email: string) => void;
}) {
  const tone = account.deactive ? "deactive" : account.refreshing ? "running" : account.last_error ? "error" : "ok";
  if (compact) {
    return (
      <div className="compact-row">
        <span className="mono">{account.email}</span>
        <Badge tone={tone}>{account.deactive ? "停用" : account.refreshing ? "刷新中" : account.status || "未知"}</Badge>
      </div>
    );
  }
  return (
    <tr>
      <td className="mono">{account.email}</td>
      <td>
        <Badge tone={tone}>{account.deactive ? "停用" : account.refreshing ? "刷新中" : account.status || "未知"}</Badge>
      </td>
      <td>{account.schedulable === null ? "未知" : account.schedulable ? "可用" : "暂停"}</td>
      <td className="mono muted">{account.sub2api_account_id || "-"}</td>
      <td>{formatDate(account.updated_at)}</td>
      <td className="truncate">{account.last_error || "-"}</td>
      <td className="right">
        <button
          className="icon-button"
          disabled={busy || account.refreshing || account.deactive}
          onClick={() => onRefresh?.(account.email)}
          title="刷新 AT"
          type="button"
        >
          <Play size={16} />
        </button>
      </td>
    </tr>
  );
}

function MailboxView({
  mailboxes,
  busy,
  onImport,
  onDelete,
}: {
  mailboxes: Mailbox[];
  busy: boolean;
  onImport: (content: string, provider: string) => void;
  onDelete: (id: number) => void;
}) {
  const [content, setContent] = useState("");
  const [provider, setProvider] = useState("auto");
  const [selectedMailbox, setSelectedMailbox] = useState<Mailbox | null>(null);
  const [folder, setFolder] = useState<"inbox" | "junk">("inbox");
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [mailLoading, setMailLoading] = useState(false);
  const [mailError, setMailError] = useState("");

  const supportsJunk = selectedMailbox ? ["outlook", "hotmail"].includes(selectedMailbox.provider) : false;
  const openMessages = (mailbox: Mailbox) => {
    setSelectedMailbox(mailbox);
    setFolder("inbox");
    setMessages([]);
    setMailError("");
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
            placeholder={"gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token\nother@example.com----other@outlook.com----mail_password----client_id----refresh_token"}
            rows={6}
            value={content}
          />
          <p className="form-hint">支持批量导入，一行一个；空行和 # 开头的行会被忽略。</p>
        </form>
      </section>

      <section className="panel">
        <PanelTitle title="邮箱凭据" icon={ShieldCheck} />
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>GPT 邮箱</th>
                <th>取件邮箱</th>
                <th>类型</th>
                <th>最近成功</th>
                <th>错误</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {mailboxes.map((mailbox) => (
                <tr key={mailbox.id}>
                  <td className="mono">{mailbox.gpt_email}</td>
                  <td className="mono">{mailbox.mailbox_email}</td>
                  <td>
                    <Badge tone={mailbox.disabled ? "deactive" : "ok"}>{mailbox.provider}</Badge>
                  </td>
                  <td>{mailbox.last_success_at ? formatDate(mailbox.last_success_at) : "-"}</td>
                  <td className="truncate">{mailbox.last_error || "-"}</td>
                  <td className="right">
                    <div className="row-actions">
                      <button className="icon-button" disabled={busy} onClick={() => openMessages(mailbox)} title="查看邮件" type="button">
                        <MailOpen size={16} />
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
    </div>
  );
}

function MailMessageDialog({
  mailbox,
  messages,
  folder,
  loading,
  error,
  supportsJunk,
  onClose,
  onFolderChange,
}: {
  mailbox: Mailbox;
  messages: MailMessage[];
  folder: "inbox" | "junk";
  loading: boolean;
  error: string;
  supportsJunk: boolean;
  onClose: () => void;
  onFolderChange: (folder: "inbox" | "junk") => void;
}) {
  const title = useMemo(() => (folder === "junk" ? "垃圾箱" : "收件箱"), [folder]);
  return (
    <div className="modal-backdrop" role="presentation">
      <section aria-modal="true" className="mail-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">{mailbox.provider}</p>
            <h2>{mailbox.mailbox_email}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>

        <div className="mail-tabs" role="tablist">
          <button className={folder === "inbox" ? "mail-tab active" : "mail-tab"} onClick={() => onFolderChange("inbox")} type="button">
            <Inbox size={16} />
            <span>收件箱</span>
          </button>
          {supportsJunk ? (
            <button className={folder === "junk" ? "mail-tab active" : "mail-tab"} onClick={() => onFolderChange("junk")} type="button">
              <ShieldAlert size={16} />
              <span>垃圾箱</span>
            </button>
          ) : null}
        </div>

        <div className="mail-list-head">
          <strong>{title}</strong>
          <span>{loading ? "读取中" : `${messages.length} 封`}</span>
        </div>

        {error ? <div className="mail-error">{error}</div> : null}
        {!error && loading ? <Empty label="正在读取邮件" /> : null}
        {!error && !loading && !messages.length ? <Empty label="暂无邮件" /> : null}
        {!error && !loading && messages.length ? (
          <div className="mail-list">
            {messages.map((message) => (
              <article className="mail-item" key={message.id}>
                <div className="mail-item-main">
                  <strong>{message.subject || "无主题"}</strong>
                  <span>{message.body_preview || "无预览内容"}</span>
                </div>
                <div className="mail-item-meta">
                  <span>{message.sender_name || message.sender_address || "未知发件人"}</span>
                  <time>{message.received_at ? formatDate(message.received_at) : "-"}</time>
                </div>
              </article>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function HistoryView({ jobs, events }: { jobs: RefreshJob[]; events: AppEvent[] }) {
  return (
    <section className="split-grid">
      <div className="panel">
        <PanelTitle title="刷新任务" icon={RefreshCcw} />
        <div className="timeline">
          {jobs.map((job) => (
            <div className="timeline-item" key={job.id}>
              <Badge tone={job.status === "succeeded" ? "ok" : job.status === "running" ? "running" : "error"}>
                {statusLabel(job.status)}
              </Badge>
              <strong className="mono">{job.email}</strong>
              <span>{job.reason || "-"}</span>
              <time>{formatDate(job.created_at)}</time>
            </div>
          ))}
          {!jobs.length ? <Empty label="暂无刷新任务" /> : null}
        </div>
      </div>

      <div className="panel">
        <PanelTitle title="事件" icon={Activity} />
        <div className="timeline">
          {events.map((event) => (
            <div className="timeline-item" key={event.id}>
              <Badge tone={event.kind.includes("failed") ? "error" : "ink"}>{event.kind}</Badge>
              <strong>{event.email || "system"}</strong>
              <span>{event.message}</span>
              <time>{formatDate(event.created_at)}</time>
            </div>
          ))}
          {!events.length ? <Empty label="暂无事件" /> : null}
        </div>
      </div>
    </section>
  );
}

function PanelTitle({ title, icon: Icon }: { title: string; icon: LucideIcon }) {
  return (
    <div className="panel-title">
      <Icon size={18} />
      <h2>{title}</h2>
    </div>
  );
}

function Badge({ children, tone }: { children: string; tone: string }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

function Empty({ label }: { label: string }) {
  return <div className="empty">{label}</div>;
}

function SignalLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="signal-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function titleFor(view: View) {
  return {
    overview: "调度恢复概览",
    accounts: "GPT 账号状态",
    mailboxes: "验证码邮箱",
    history: "刷新历史",
  }[view];
}

function statusLabel(status: string) {
  return (
    {
      queued: "排队",
      running: "运行中",
      succeeded: "成功",
      failed: "失败",
      deactive: "停用",
    }[status] || status
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export default App;
