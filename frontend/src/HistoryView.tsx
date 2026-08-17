import { Activity, AlertTriangle, ExternalLink, RefreshCcw, Trash2, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import {
  eventDurationBreakdown,
  eventDurationMs,
  formatElapsedDuration,
  timestampDurationMs,
} from "./durationPresentation";
import type { AccountExceptionRecord, AppEvent, RefreshJob } from "./domain";

type HistoryViewProps = {
  jobs: RefreshJob[];
  events: AppEvent[];
  exceptionRecords: AccountExceptionRecord[];
  busy: boolean;
  now: number;
  timeZone: string;
  formatDate: (value: string, timeZone: string) => string;
  onClear: () => void;
  onDeleteExceptionRecord: (id: number) => void;
  onLocateAccount: (record: AccountExceptionRecord) => void;
};

export function HistoryView({
  jobs,
  events,
  exceptionRecords,
  busy,
  now,
  timeZone,
  formatDate,
  onClear,
  onDeleteExceptionRecord,
  onLocateAccount,
}: HistoryViewProps) {
  const [subview, setSubview] = useState<"exceptions" | "jobs" | "events">("exceptions");
  const hasHistory = jobs.length > 0 || events.length > 0;
  const clearHistory = () => {
    if (!hasHistory || busy) return;
    if (window.confirm("确定要清空刷新任务和事件历史吗？")) {
      onClear();
    }
  };

  return (
    <div className="stack">
      <div className="history-toolbar">
        <div className="history-counts">
          <span><AlertTriangle size={14} />{exceptionRecords.length} 条异常</span>
          <span><RefreshCcw size={14} />{jobs.length} 条任务</span>
          <span><Activity size={14} />{events.length} 条事件</span>
        </div>
        <button className="danger-button" disabled={busy || !hasHistory} onClick={clearHistory} type="button">
          <Trash2 size={17} />
          <span>清空历史</span>
        </button>
      </div>

      <div className="history-tabs" role="tablist" aria-label="历史子界面">
        <HistoryTab active={subview === "exceptions"} count={exceptionRecords.length} icon={<AlertTriangle size={16} />} label="异常账号" onClick={() => setSubview("exceptions")} />
        <HistoryTab active={subview === "jobs"} count={jobs.length} icon={<RefreshCcw size={16} />} label="刷新任务" onClick={() => setSubview("jobs")} />
        <HistoryTab active={subview === "events"} count={events.length} icon={<Activity size={16} />} label="事件" onClick={() => setSubview("events")} />
      </div>

      {subview === "exceptions" ? (
        <section className="panel">
          <PanelTitle icon={<AlertTriangle size={18} />} title="异常账号记录" />
          <div className="history-list">
            {exceptionRecords.map((record) => {
              const relatedJob = latestErrorRefreshJobForRecord(record, jobs);
              const relatedJobDurationMs = relatedJob ? timestampDurationMs(relatedJob.started_at, relatedJob.finished_at, now) : null;
              const displayMessage = relatedJob?.reason || record.message || "-";
              const displayTime = relatedJob?.created_at ?? record.updated_at;
              const protocolSummary = relatedJob ? refreshJobProtocolSummary(relatedJob, events) : null;
              return (
                <article className="history-item exception-record-item" key={record.id}>
                  <div className="history-item-head">
                    <div className="history-meta">
                      <Badge tone={exceptionStatusTone(record.status)}>{exceptionStatusLabel(record.status)}</Badge>
                      <Badge tone="ink">{exceptionSourceLabel(record.source)}</Badge>
                      <strong className="mono history-email">{record.email || "unknown"}</strong>
                      {record.management_account_id ? <span className="memory-pill mono">{record.management_account_id}</span> : null}
                      {relatedJobDurationMs !== null ? <span className="memory-pill">耗时 {formatElapsedDuration(relatedJobDurationMs)}</span> : null}
                    </div>
                    <div className="history-record-actions">
                      <time>{formatDate(displayTime, timeZone)}</time>
                      <button aria-label="在账号界面定位此账号" className="icon-button" disabled={busy || (!record.email && !record.management_account_id)} onClick={() => onLocateAccount(record)} title="在账号界面定位此账号" type="button">
                        <ExternalLink size={16} />
                      </button>
                      <button aria-label="删除异常账号记录" className="icon-button history-dismiss-button" disabled={busy} onClick={() => onDeleteExceptionRecord(record.id)} title="删除这条异常账号记录" type="button">
                        <X size={16} />
                      </button>
                    </div>
                  </div>
                  <p className="history-message">{displayMessage}</p>
                  {protocolSummary ? <p className="history-message">{`协议链路：${protocolSummary}`}</p> : null}
                </article>
              );
            })}
            {!exceptionRecords.length ? <Empty label="暂无异常账号记录" /> : null}
          </div>
        </section>
      ) : null}

      {subview === "jobs" ? (
        <section className="panel">
          <PanelTitle icon={<RefreshCcw size={18} />} title="刷新任务" />
          <div className="history-list">
            {jobs.map((job) => {
              const protocolSummary = refreshJobProtocolSummary(job, events);
              const durationMs = timestampDurationMs(job.started_at, job.finished_at, now);
              return (
                <article className="history-item" key={job.id}>
                  <div className="history-item-head">
                    <div className="history-meta">
                      <Badge tone={job.status === "succeeded" ? "ok" : job.status === "running" ? "running" : "error"}>{statusLabel(job.status)}</Badge>
                      <strong className="mono history-email">{job.email}</strong>
                      {job.memory_peak_rss_bytes ? <span className="memory-pill">内存峰值 {formatBytes(job.memory_peak_rss_bytes)}</span> : null}
                      {durationMs !== null ? <span className="memory-pill">耗时 {formatElapsedDuration(durationMs)}</span> : null}
                    </div>
                    <time>{formatDate(job.created_at, timeZone)}</time>
                  </div>
                  <p className="history-message">{job.reason || "-"}</p>
                  {protocolSummary ? <p className="history-message">{`协议链路：${protocolSummary}`}</p> : null}
                </article>
              );
            })}
            {!jobs.length ? <Empty label="暂无刷新任务" /> : null}
          </div>
        </section>
      ) : null}

      {subview === "events" ? (
        <section className="panel">
          <PanelTitle icon={<Activity size={18} />} title="事件" />
          <div className="history-list">
            {events.map((event) => {
              const reasonLabel = eventReasonLabel(event);
              const modeLabel = eventModeLabel(event);
              const syncErrorCount = eventSyncErrorCount(event);
              const durationMs = eventDurationMs(event.details);
              const durationBreakdown = eventDurationBreakdown(event.details);
              return (
                <article className="history-item event-history-item" key={event.id}>
                  <div className="history-item-head">
                    <div className="history-meta">
                      <Badge tone={eventTone(event.kind)}>{eventKindLabel(event.kind)}</Badge>
                      {reasonLabel ? <Badge tone="ink">{reasonLabel}</Badge> : null}
                      {modeLabel ? <Badge tone="ink">{modeLabel}</Badge> : null}
                      {syncErrorCount !== null ? <Badge tone={syncErrorCount > 0 ? "error" : "ok"}>{`错误账号 ${syncErrorCount}`}</Badge> : null}
                      {durationMs !== null ? <Badge tone="ink">{`耗时 ${formatElapsedDuration(durationMs)}`}</Badge> : null}
                      <strong className="history-email">{event.email || "system"}</strong>
                    </div>
                    <time>{formatDate(event.created_at, timeZone)}</time>
                  </div>
                  <p className="history-message">{event.message}</p>
                  {durationBreakdown.length ? <p className="history-message">阶段耗时：{durationBreakdown.map((item) => `${item.label} ${formatElapsedDuration(item.durationMs)}`).join(" · ")}</p> : null}
                </article>
              );
            })}
            {!events.length ? <Empty label="暂无事件" /> : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function HistoryTab({ active, count, icon, label, onClick }: { active: boolean; count: number; icon: ReactNode; label: string; onClick: () => void }) {
  return <button aria-selected={active} className={active ? "history-tab active" : "history-tab"} onClick={onClick} role="tab" type="button">{icon}<span>{label}</span><strong>{count}</strong></button>;
}

function PanelTitle({ icon, title }: { icon: ReactNode; title: string }) {
  return <div className="panel-title">{icon}<h2>{title}</h2></div>;
}

function Badge({ children, tone }: { children: string; tone: string }) {
  return <span className={["badge", tone].filter(Boolean).join(" ")}>{children}</span>;
}

function Empty({ label }: { label: string }) {
  return <div className="empty">{label}</div>;
}

function statusLabel(status: string) {
  return ({ queued: "排队", running: "运行中", succeeded: "成功", skipped: "已跳过", failed: "失败", deactive: "封禁" }[status] || status);
}

function latestErrorRefreshJobForRecord(record: AccountExceptionRecord, jobs: RefreshJob[]) {
  const accountId = normalizeHistoryAccountId(record.management_account_id);
  if (accountId) {
    for (const job of jobs) {
      if (refreshJobHasErrorReason(job) && normalizeHistoryAccountId(job.management_account_id) === accountId) return job;
    }
  }
  const email = normalizeHistoryEmail(record.email);
  if (email) {
    for (const job of jobs) {
      if (refreshJobHasErrorReason(job) && normalizeHistoryEmail(job.email) === email) return job;
    }
  }
  return null;
}

function refreshJobHasErrorReason(job: RefreshJob) {
  return Boolean(job.reason && (job.status === "failed" || job.status === "deactive"));
}

function refreshJobProtocolSummary(job: RefreshJob, events: AppEvent[]) {
  const relatedEvents = events
    .filter((event) => parseEventJobId(event) === job.id)
    .filter((event) => Boolean(protocolEventSummary(event)))
    .reverse();
  const summaries = relatedEvents
    .map(protocolEventSummary)
    .filter((summary, index, list): summary is string => Boolean(summary) && list.indexOf(summary) === index);
  return summaries.length ? summaries.join(" -> ") : null;
}

function parseEventJobId(event: AppEvent) {
  const value = event.details?.job_id;
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string") {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return Math.trunc(parsed);
  }
  return null;
}

function protocolEventSummary(event: AppEvent) {
  switch (event.kind) {
    case "sub2api_protocol_refresh_failed": return protocolErrorHttpSummary(event, "/refresh", "sub2api /refresh 失败");
    case "sub2api_check_status_unavailable": return "sub2api /check-status 无结果";
    case "runtime_access_token_missing": return "只有 AT 标记，缺少真实 access_token";
    case "access_token_status_check_failed": return protocolErrorHttpSummary(event, "access_token 状态检查", "access_token 状态检查失败");
    case "chatgpt_protocol_refresh_failed": return protocolErrorHttpSummary(event, "ChatGPT 协议刷新", "ChatGPT 协议刷新失败");
    default: return null;
  }
}

function protocolErrorHttpSummary(event: AppEvent, fallbackLabel: string, defaultLabel: string) {
  const match = String(event.details?.error || "").match(/\bHTTP\s+(\d{3})\b/i);
  return match ? `${fallbackLabel} 返回 ${match[1]}` : defaultLabel;
}

function normalizeHistoryEmail(value: string | null | undefined) {
  return String(value || "").trim().toLowerCase() || null;
}

function normalizeHistoryAccountId(value: string | null | undefined) {
  return String(value || "").trim() || null;
}

function exceptionSourceLabel(source: string) {
  return ({ sync: "当前账号", refresh: "刷新任务", usage_refresh: "额度刷新", subscription_refresh: "订阅刷新" }[source] || source);
}

function exceptionStatusLabel(status: string) {
  return ({ failed: "失败", skipped: "已跳过", deactive: "封禁", error: "错误", missing_mailbox: "缺邮箱", recovery_disabled: "恢复关闭", auto_refresh_locked: "刷新锁定" }[status] || status);
}

function exceptionStatusTone(status: string) {
  return ({ failed: "error", deactive: "deactive", error: "error", missing_mailbox: "warn", recovery_disabled: "warn", auto_refresh_locked: "error", skipped: "warn" }[status] || "ink");
}

function eventReasonLabel(event: AppEvent) {
  return event.details?.reason === "scheduled" ? "自动" : event.details?.reason === "manual" ? "手动" : null;
}

function eventModeLabel(event: AppEvent) {
  return event.details?.force === true ? "强制刷新" : event.details?.force === false ? "缓存优先" : null;
}

function eventKindLabel(kind: string) {
  return ({
    sub2api_protocol_refresh_failed: "sub2api /refresh", sub2api_check_status_unavailable: "sub2api /check-status", runtime_access_token_missing: "运行态 AT", access_token_status_check_failed: "AT 状态检查", chatgpt_protocol_refresh_failed: "ChatGPT 协议", openai_oauth_refresh_token_failed: "OpenAI OAuth", refresh_failed: "刷新失败", refresh_started: "刷新开始", refresh_succeeded: "刷新成功", refresh_deactive: "刷新封禁", refresh_skipped_missing_mailbox: "缺少邮箱", manual_sync: "OAuth 账号同步", monitor_sync: "OAuth 账号清单同步", monitor_failed: "OAuth 账号清单同步失败", usage_refresh: "OAuth 用量窗口", usage_refresh_failed: "OAuth 用量窗口失败", usage_statistics_refresh: "用量窗口手动刷新", subscription_refresh: "订阅信息刷新", manual_upstream_sync: "API 账号同步", manual_api_key_inventory_sync: "API 账号清单同步", api_key_inventory_sync: "API 账号清单自动同步", api_key_inventory_sync_failed: "API 账号清单自动同步失败", upstream_sync: "上游自动探测", upstream_rate_sync_failed: "上游自动探测失败",
  }[kind] || kind);
}

function eventTone(kind: string) {
  if (kind.includes("failed")) return "error";
  if (kind === "sub2api_check_status_unavailable" || kind === "runtime_access_token_missing") return "warn";
  if (kind.includes("succeeded")) return "ok";
  return kind.includes("started") ? "running" : "ink";
}

function eventSyncErrorCount(event: AppEvent) {
  return parseEventCount(event.details?.error_seen) ?? parseEventCount(event.details?.sync_error_seen);
}

function parseEventCount(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, Math.trunc(value));
  if (typeof value === "string") {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return Math.max(0, Math.trunc(parsed));
  }
  return null;
}

function formatBytes(value: number) {
  const mib = value / 1024 / 1024;
  return mib < 1024 ? `${mib.toFixed(1)} MiB` : `${(mib / 1024).toFixed(2)} GiB`;
}
