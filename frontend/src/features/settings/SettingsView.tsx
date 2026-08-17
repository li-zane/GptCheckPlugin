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
import {
  automationDurationDisplayValue,
  automationDurationSecondsValue,
  automationDurationUnits,
  preferredAutomationDurationUnit,
  type AutomationDurationUnit,
} from "../../automationDuration";
import { HelpPopover } from "../../HelpPopover";
import { MiddleEllipsisText } from "../../MiddleEllipsisText";
import {
  firstUnusedFallbackModel,
  MAX_FALLBACK_TEST_MODELS,
  moveFallbackModel,
  normalizeFallbackModelChain,
} from "../../fallbackModelChain";
import {
  normalizeChangeLogPageSizeOptions,
  parseChangeLogPageSizeOptions,
} from "../../changeLogPageSize";
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
import { SignalLine, cloneUsageLimitPlanRanges, defaultTimeZone, formatDate, normalizeSubscriptionType, sub2ApiApiPrefix, subscriptionTypeLabel, toSub2ApiInstanceUrl, usageLimitWindowKeys } from "../shared/LegacyDisplay";

const defaultSiteName = "账号管理助手";

const defaultUsageLimitSampleThresholdPercent = 99;

const coreSubscriptionTypes = new Set(["plus", "team", "pro", "free", "k12", "unknown"]);

const defaultUsageLimitPlanRanges: UsageLimitPlanRanges = {
  five_hour: { lower: 15, upper: 25 },
  seven_day: { lower: 100, upper: 140 },
  monthly: { lower: 400, upper: 560 },
};

const defaultUsageLimitRanges = Object.fromEntries(
  [...coreSubscriptionTypes].map((subscriptionType) => [
    subscriptionType,
    subscriptionType === "team"
      ? { ...cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges), monthly: { lower: 100, upper: 300 } }
      : cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges),
  ]),
) as UsageLimitDefaultRanges;

const timeZoneOptions = [
  { value: "Asia/Shanghai", label: "中国标准时间 · Asia/Shanghai" },
  { value: "UTC", label: "UTC" },
  { value: "Asia/Tokyo", label: "日本 · Asia/Tokyo" },
  { value: "Asia/Singapore", label: "新加坡 · Asia/Singapore" },
  { value: "Europe/London", label: "伦敦 · Europe/London" },
  { value: "America/New_York", label: "纽约 · America/New_York" },
  { value: "America/Los_Angeles", label: "洛杉矶 · America/Los_Angeles" },
];

function AutomationSettingRow({
  checked,
  description,
  interval,
  label,
  manual,
  onChange,
  threads,
}: {
  checked: boolean;
  description?: string;
  interval: ReactNode;
  label: string;
  manual?: ReactNode;
  onChange: (checked: boolean) => void;
  threads: ReactNode;
}) {
  return (
    <div className="automation-setting-row" role="group" aria-label={label}>
      <label className="automation-setting-toggle">
        <input checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
        <span className="automation-setting-label">
          <span className="settings-label-with-help">
            <strong>{label}</strong>
            {description ? <HelpPopover label={`查看${label}说明`}>{description}</HelpPopover> : null}
          </span>
        </span>
      </label>
      <div className="automation-setting-cell">
        <span className="automation-setting-mobile-label">线程数</span>
        {threads}
      </div>
      <div className="automation-setting-cell">
        <span className="automation-setting-mobile-label">自动执行间隔</span>
        {interval}
      </div>
      <div className="automation-setting-cell automation-setting-cell--manual">
        <span className="automation-setting-mobile-label">手动同步</span>
        {manual ?? <AutomationSettingInherited>不适用</AutomationSettingInherited>}
      </div>
    </div>
  );
}

function AutomationSettingManualCheckbox({
  checked,
  disabled = false,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="automation-setting-manual" title={label}>
      <input
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
      <span>{checked ? "执行" : "跳过"}</span>
    </label>
  );
}

function AutomationSettingNumber({
  ariaLabel,
  disabled = false,
  max,
  min,
  onChange,
  step,
  suffix,
  value,
}: {
  ariaLabel: string;
  disabled?: boolean;
  max: number;
  min: number;
  onChange: (value: string) => void;
  step?: number | "any";
  suffix?: string;
  value: string;
}) {
  return (
    <div className="automation-setting-number">
      <input
        aria-label={ariaLabel}
        disabled={disabled}
        max={max}
        min={min}
        onChange={(event) => onChange(event.target.value)}
        step={step}
        type="number"
        value={value}
      />
      {suffix ? <span>{suffix}</span> : null}
    </div>
  );
}

function AutomationSettingInherited({ children }: { children: ReactNode }) {
  return (
    <div aria-disabled="true" className="automation-setting-inherited">
      {children}
    </div>
  );
}

function scrollToSettingsSection(sectionId: string) {
  document.getElementById(sectionId)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

const settingsNavigation: ReadonlyArray<{ icon: LucideIcon; id: string; label: string }> = [
  { icon: Link2, id: "settings-connection", label: "基础连接" },
  { icon: RefreshCcw, id: "settings-oauth", label: "OAuth 账号" },
  { icon: Database, id: "settings-api-key-sync", label: "API 账号与上游" },
  { icon: ShieldCheck, id: "settings-api-key-policies", label: "可用性与暂停" },
  { icon: TimerReset, id: "settings-usage", label: "用量与订阅" },
  { icon: Database, id: "settings-data-management", label: "数据管理" },
  { icon: Activity, id: "settings-notifications", label: "通知" },
  { icon: Globe2, id: "settings-display-security", label: "界面偏好" },
];

function FallbackModelChainDialog({
  availableModels,
  enabled,
  models,
  onChange,
  onClose,
}: {
  availableModels: AccountLivenessModel[];
  enabled: boolean;
  models: string[];
  onChange: (models: string[]) => void;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const availableModelIds = useMemo(
    () => Array.from(new Set(availableModels.map((model) => model.id.trim()).filter(Boolean))),
    [availableModels],
  );
  const nextModel = firstUnusedFallbackModel(availableModelIds, models);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop settings-model-chain-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-labelledby="fallback-model-chain-title" aria-modal="true" className="mail-dialog settings-model-chain-dialog" ref={dialogRef} role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">API Key 可用性检测</p>
            <h2 id="fallback-model-chain-title">配置回退测试模型链</h2>
          </div>
          <button aria-label="关闭回退测试模型链配置" className="icon-button" onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>

        <p className="settings-model-chain-dialog-copy">
          按从上到下的顺序选择账号白名单中第一个存在的模型。最多可配置 {MAX_FALLBACK_TEST_MODELS} 个模型。
        </p>

        <div className="settings-model-chain-list settings-model-chain-dialog-list">
          {models.length ? models.map((selectedModel, index) => {
            const configuredModel = availableModels.find((model) => model.id === selectedModel);
            const options = configuredModel
              ? availableModels
              : [{ id: selectedModel, display_name: `${selectedModel}（当前配置）` }, ...availableModels];
            return (
              <div className="settings-model-chain-row" key={selectedModel}>
                <span className="settings-model-chain-order" aria-hidden="true">{index + 1}</span>
                <select
                  aria-label={`回退测试模型 ${index + 1}`}
                  disabled={!enabled}
                  onChange={(event) => {
                    const model = event.target.value;
                    onChange(models.map((item, itemIndex) => itemIndex === index ? model : item));
                  }}
                  value={selectedModel}
                >
                  {options.map((model) => (
                    <option
                      disabled={model.id !== selectedModel && models.includes(model.id)}
                      key={model.id}
                      value={model.id}
                    >
                      {model.display_name || model.id}
                    </option>
                  ))}
                </select>
                <button
                  aria-label={`上移回退测试模型 ${selectedModel}`}
                  className="icon-button settings-model-chain-action"
                  disabled={!enabled || index === 0}
                  onClick={() => onChange(moveFallbackModel(models, index, -1))}
                  title="上移"
                  type="button"
                >
                  <ArrowUp size={15} />
                </button>
                <button
                  aria-label={`下移回退测试模型 ${selectedModel}`}
                  className="icon-button settings-model-chain-action"
                  disabled={!enabled || index === models.length - 1}
                  onClick={() => onChange(moveFallbackModel(models, index, 1))}
                  title="下移"
                  type="button"
                >
                  <ArrowDown size={15} />
                </button>
                <button
                  aria-label={`删除回退测试模型 ${selectedModel}`}
                  className="icon-button danger settings-model-chain-action"
                  disabled={!enabled}
                  onClick={() => onChange(models.filter((_, itemIndex) => itemIndex !== index))}
                  title="删除"
                  type="button"
                >
                  <Trash2 size={15} />
                </button>
              </div>
            );
          }) : (
            <span className="settings-model-chain-empty">尚未配置回退测试模型</span>
          )}
        </div>

        <div className="settings-model-chain-dialog-actions">
          <button
            className="secondary-button settings-model-chain-add"
            disabled={!enabled || !nextModel || models.length >= MAX_FALLBACK_TEST_MODELS}
            onClick={() => {
              if (nextModel) onChange([...models, nextModel]);
            }}
            type="button"
          >
            <Plus size={15} />
            新增模型
          </button>
          <button className="primary-button" onClick={onClose} type="button">完成</button>
        </div>
      </section>
    </div>
  );
}

type DiscordSetupScreenshot = {
  alt: string;
  src: string;
  title: string;
};

function DiscordBotSetupScreenshotPreviewDialog({
  onClose,
  screenshot,
}: {
  onClose: () => void;
  screenshot: DiscordSetupScreenshot;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop discord-bot-screenshot-preview-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-labelledby="discord-bot-screenshot-preview-title" aria-modal="true" className="mail-dialog discord-bot-screenshot-preview-dialog" ref={dialogRef} role="dialog">
        <header className="mail-dialog-head">
          <h2 id="discord-bot-screenshot-preview-title">{screenshot.title}</h2>
          <button aria-label="关闭截图预览" className="icon-button" onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="discord-bot-screenshot-preview-body">
          <img alt={screenshot.alt} src={screenshot.src} />
        </div>
      </section>
    </div>
  );
}

function DiscordBotSetupGuideDialog({ onClose }: { onClose: () => void }) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const screenshotTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [previewScreenshot, setPreviewScreenshot] = useState<DiscordSetupScreenshot | null>(null);
  const screenshotPreviewOpenRef = useRef(false);
  screenshotPreviewOpenRef.current = previewScreenshot !== null;
  const closeScreenshotPreview = useCallback(() => {
    setPreviewScreenshot(null);
    window.requestAnimationFrame(() => screenshotTriggerRef.current?.focus());
  }, []);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (screenshotPreviewOpenRef.current) return;
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop settings-model-chain-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-labelledby="discord-bot-setup-guide-title" aria-modal="true" className="mail-dialog discord-bot-setup-dialog" ref={dialogRef} role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">Discord Bot 通知</p>
            <h2 id="discord-bot-setup-guide-title">配置指南</h2>
          </div>
          <button aria-label="关闭 Discord Bot 配置指南" className="icon-button" onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="discord-bot-setup-guide-body">
          <p className="discord-bot-setup-guide-intro">推荐且受支持的方式是“私有 Bot + 服务器安装 + 单一通知频道”。不要只做用户安装，也不要把私信频道 ID 填作通知频道。</p>
          <div className="discord-bot-security-note">
            <ShieldCheck size={18} />
            <div>
              <strong>私有安装</strong>
              <span>关闭 Public Bot 后，只有应用所有者或团队成员可以把 Bot 加入服务器；现有服务器安装不会因此失效。</span>
            </div>
          </div>
          <section aria-labelledby="discord-bot-install-modes-title" className="discord-bot-install-modes">
            <div className="discord-bot-install-modes-head">
              <h3 id="discord-bot-install-modes-title">安装方式与区别</h3>
              <p><code>Public / Private</code> 决定谁能安装，<code>User / Guild Install</code> 决定安装到哪里，两组设置互不替代。</p>
            </div>
            <div className="discord-bot-install-mode-grid">
              <div className="discord-bot-install-mode">
                <strong><code>Public Bot</code>（公开）</strong>
                <p>在 <code>Bot</code> 页面开启 <code>Public Bot</code>，为 <code>Guild Install</code> 配置安装链接后即可分享；任何拿到链接、且有目标服务器管理权限的人都能安装。公开不会直接暴露 Token，但会扩大滥用、垃圾交互和运维排查范围。</p>
              </div>
              <div className="discord-bot-install-mode">
                <strong><code>Private Bot</code>（私有，推荐）</strong>
                <p>关闭 <code>Public Bot</code>，只有应用所有者或团队成员可以安装。将 <code>Install Link</code> 设为 <code>None</code>，再通过 <code>OAuth2 / URL Generator</code> 生成自用链接。</p>
              </div>
              <div className="discord-bot-install-mode">
                <strong><code>User Install</code>（用户安装）</strong>
                <p>应用安装到个人账号或私信上下文，Bot 不会成为服务器成员，也无法按本插件要求访问服务器通知频道，因此不适用于本插件。</p>
              </div>
              <div className="discord-bot-install-mode">
                <strong><code>Guild Install</code>（服务器安装）</strong>
                <p>Bot 会真正加入所选服务器，可取得频道权限并注册服务器命令。本插件仅支持这种安装方式，安装者需要目标服务器的“管理服务器”权限。</p>
              </div>
            </div>
          </section>
          <ol className="discord-bot-setup-guide-steps">
            <li className="discord-bot-setup-step">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">1</span>
                <div>
                  <strong>创建私有 Bot 并取得 Token</strong>
                  <p>在 Developer Portal 创建或选择应用，进入 <code>Bot</code> 页面创建 Bot。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li>关闭 <code>Public Bot</code>，限制为应用所有者或团队成员安装。</li>
                    <li>本插件不需要 Message Content、Server Members 或 Presence 等特权 Intent，可保持关闭。</li>
                    <li>点击 <code>Reset Token</code> 取得 Token；Token 只填入本页，重新生成后必须同步更新。</li>
                  </ul>
                  <a className="discord-bot-setup-link" href="https://discord.com/developers/applications" rel="noreferrer" target="_blank">
                    打开 Developer Portal <ExternalLink size={14} />
                  </a>
                </div>
              </div>
              <button
                aria-label="打开创建 Bot 并取得 Token 的截图"
                className="discord-bot-setup-screenshot-button"
                onClick={(event) => {
                  screenshotTriggerRef.current = event.currentTarget;
                  setPreviewScreenshot({
                    alt: "Discord Developer Portal 的机器人页面，令牌区域位于用户名下方",
                    src: "/discord-bot-token-guide.png",
                    title: "创建 Bot 并取得 Token",
                  });
                }}
                type="button"
              >
                <img alt="Discord Developer Portal 的机器人页面，令牌区域位于用户名下方" className="discord-bot-setup-screenshot" loading="lazy" src="/discord-bot-token-guide.png" />
                <span aria-hidden="true" className="discord-bot-setup-screenshot-action"><ZoomIn size={16} />点击查看原图</span>
              </button>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">2</span>
                <div>
                  <strong>配置服务器安装范围</strong>
                  <p>进入 <code>Installation</code> 页面，将安装方式限定为服务器安装。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li><code>Installation Contexts</code> 只保留 <code>Guild Install</code>，取消 <code>User Install</code>。</li>
                    <li><code>Install Link</code> 选择 <code>None</code>。私有 Bot 使用 Discord 提供的默认链接会导致 <code>Public Bot</code> 无法保存为关闭。</li>
                  </ul>
                  <a className="discord-bot-setup-link" href="https://discord.com/developers/docs/tutorials/setting-up-a-bot-application" rel="noreferrer" target="_blank">
                    打开官方安装说明 <ExternalLink size={14} />
                  </a>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">3</span>
                <div>
                  <strong>生成服务器安装链接并安装</strong>
                  <p>进入 <code>OAuth2 / URL Generator</code>，生成一次只供自己使用的服务器安装链接。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li><code>Scopes</code> 勾选 <code>bot</code> 与 <code>applications.commands</code>。</li>
                    <li><code>Bot Permissions</code> 只勾选 <code>View Channels</code>、<code>Send Messages</code>、<code>Embed Links</code>，不要授予 <code>Administrator</code>。</li>
                    <li>复制页面底部生成的 URL，用应用所有者或团队成员账号打开，选择你管理的目标服务器并完成授权。</li>
                    <li>安装完成后，先在服务器成员列表确认 Bot 已出现。</li>
                    <li>如果服务器不可选，当前 Discord 账号需要该服务器的“管理服务器”权限。</li>
                  </ul>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">4</span>
                <div>
                  <strong>检查频道权限并复制频道 ID</strong>
                  <p>在目标服务器频道的权限设置中，确认 Bot 能查看频道、发送消息和嵌入链接。随后在 Discord 客户端的“高级设置”中启用开发者模式，右键该服务器频道并选择“复制频道 ID”。</p>
                  <a className="discord-bot-setup-link" href="https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID" rel="noreferrer" target="_blank">
                    打开官方开发者模式说明 <ExternalLink size={14} />
                  </a>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">5</span>
                <div>
                  <strong>保存插件设置</strong>
                  <p>回到本页填写 Bot Token 和服务器频道 ID，启用 Discord Bot 通知，选择需要的通知事件并保存。保存后，后端会在该服务器注册 <code>/balance</code> 与 <code>/quota</code>，通常数秒内出现。</p>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">6</span>
                <div>
                  <strong>发送测试并验证命令</strong>
                  <p>点击“发送测试通知”，再到已配置频道输入 <code>/balance</code> 或 <code>/quota</code>。频道不存在、Bot 未加入服务器和权限不足会分别显示具体提示。</p>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">7</span>
                <div>
                  <strong>现有安装是否需要重装</strong>
                  <ul className="discord-bot-setup-detail-list">
                    <li>Bot 已在目标服务器，且安装时已有 <code>bot</code> 与 <code>applications.commands</code>：关闭 Public Bot 后无需重装。</li>
                    <li>之前只按旧指南做了 User Install 或只装到私信：需要执行一次 Guild Install，让 Bot 真正加入目标服务器。</li>
                    <li>缺少命令 scope 时需要重新打开安装链接授权；仅修改频道权限时无需重装。</li>
                    <li>旧的用户安装可在 Discord“用户设置 / 已授权的应用”中移除。</li>
                  </ul>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">8</span>
                <div>
                  <strong>卸载或永久删除 Bot</strong>
                  <p>先确认需要移除的范围。仅停止本插件通知时，不需要删除整个 Discord 应用。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li><strong>从某个服务器卸载：</strong>先在本插件设置中关闭 Discord Bot 通知并保存，再到该服务器的成员列表，右键 Bot 并选择“移出服务器”或“踢出”。其他服务器中的安装不受影响。</li>
                    <li><strong>撤销旧的 User Install：</strong>打开 Discord“用户设置 / 已授权的应用”，找到该应用并选择“取消授权”。这不会自动移除服务器中的 Guild Install。</li>
                    <li><strong>永久删除：</strong>进入 <code>Developer Portal / 应用 / General Information / Delete App</code>，按提示确认。这里删除的是整个应用，不是单独删除 Bot；Token、所有服务器安装和命令都会失效，且不可恢复。</li>
                  </ul>
                </div>
              </div>
            </li>
          </ol>
          <p className="discord-bot-setup-guide-note">Public Bot 不会直接泄露 Token，但会允许其他人把应用安装到更多服务器，增加滥用、垃圾交互和运维排查面。此插件只需要一个私有服务器安装。不要把 Bot Token 发到聊天、截图或提交到代码仓库。</p>
        </div>
      </section>
      {previewScreenshot ? <DiscordBotSetupScreenshotPreviewDialog onClose={closeScreenshotPreview} screenshot={previewScreenshot} /> : null}
    </div>
  );
}

export function SettingsView({
  settings,
  logoUrl,
  subscriptionTypes,
  busy,
  onSave,
  onScan,
  onTestNotification,
  onValidityChange,
}: {
  settings: AppSettings;
  logoUrl: string;
  subscriptionTypes: string[];
  busy: boolean;
  onSave: (
    payload: AppSettingsUpdate,
    branding?: { logoFile: File | null; resetLogo: boolean },
  ) => Promise<void> | void;
  onScan: () => Promise<void> | void;
  onTestNotification: () => Promise<void> | void;
  onValidityChange: (invalid: boolean) => void;
}) {
  const [siteName, setSiteName] = useState(settings.site_name || defaultSiteName);
  const [instanceUrl, setInstanceUrl] = useState(toSub2ApiInstanceUrl(settings.management_site_base_url));
  const [recoveryEnabled, setRecoveryEnabled] = useState(settings.recovery_enabled);
  const [autoRecoverState, setAutoRecoverState] = useState(settings.management_site_auto_recover_state);
  const [automationPaused, setAutomationPaused] = useState(settings.automation_paused);
  const [oauthAccountSyncEnabled, setOauthAccountSyncEnabled] = useState(settings.oauth_account_sync_enabled ?? true);
  const [oauthLoginMode, setOauthLoginMode] = useState<"protocol" | "browser">(
    settings.oauth_login_mode ?? "protocol",
  );
  const [oauthStopOnPhoneVerification, setOauthStopOnPhoneVerification] = useState(
    settings.oauth_stop_on_phone_verification ?? false,
  );
  const [interval, setInterval] = useState(String(settings.monitor_interval_seconds));
  const [usageRefreshEnabled, setUsageRefreshEnabled] = useState(settings.usage_refresh_enabled);
  const [usageRefreshInterval, setUsageRefreshInterval] = useState(String(settings.usage_refresh_interval_seconds));
  const [usageRefreshMaxConcurrency, setUsageRefreshMaxConcurrency] = useState(
    String(settings.usage_refresh_max_concurrency ?? 20),
  );
  const [apiKeyAccountSyncEnabled, setApiKeyAccountSyncEnabled] = useState(settings.api_key_account_sync_enabled ?? true);
  const [apiKeyAccountSyncInterval, setApiKeyAccountSyncInterval] = useState(
    String(settings.api_key_account_sync_interval_seconds ?? 300),
  );
  const [upstreamSyncEnabled, setUpstreamSyncEnabled] = useState(settings.upstream_sync_enabled ?? false);
  const [upstreamSyncInterval, setUpstreamSyncInterval] = useState(
    String(settings.upstream_sync_interval_seconds ?? 900),
  );
  const [upstreamSyncMaxConcurrency, setUpstreamSyncMaxConcurrency] = useState(
    String(settings.upstream_sync_max_concurrency ?? 10),
  );
  const [upstreamRateSyncEnabled, setUpstreamRateSyncEnabled] = useState(settings.upstream_rate_sync_enabled ?? false);
  const [upstreamPrioritySyncEnabled, setUpstreamPrioritySyncEnabled] = useState(
    settings.upstream_priority_sync_enabled ?? true,
  );
  const [manualUpstreamRateEnabled, setManualUpstreamRateEnabled] = useState(
    settings.manual_upstream_sync_rate_enabled ?? true,
  );
  const [manualUpstreamPriorityEnabled, setManualUpstreamPriorityEnabled] = useState(
    settings.manual_upstream_sync_priority_enabled ?? true,
  );
  const [manualUpstreamHealthEnabled, setManualUpstreamHealthEnabled] = useState(
    settings.manual_upstream_sync_upstream_health_enabled ?? true,
  );
  const [manualUpstreamMonitorsEnabled, setManualUpstreamMonitorsEnabled] = useState(
    settings.manual_upstream_monitor_sync_enabled ?? true,
  );
  const [manualAccountAvailabilityEnabled, setManualAccountAvailabilityEnabled] = useState(
    settings.manual_upstream_sync_account_availability_enabled ?? false,
  );
  const [manualBalanceGuardEnabled, setManualBalanceGuardEnabled] = useState(
    settings.manual_upstream_sync_balance_guard_enabled ?? true,
  );
  const [manualRatePauseEnabled, setManualRatePauseEnabled] = useState(
    settings.manual_upstream_sync_rate_pause_enabled ?? true,
  );
  const [apiKeyAutoDisableEnabled, setApiKeyAutoDisableEnabled] = useState(
    settings.api_key_auto_disable_on_upstream_unavailable ?? false,
  );
  const [apiKeyNegativeBalancePauseEnabled, setApiKeyNegativeBalancePauseEnabled] = useState(
    settings.api_key_auto_pause_on_negative_balance_enabled ?? false,
  );
  const [apiKeyUpstreamMonitorPauseEnabled, setApiKeyUpstreamMonitorPauseEnabled] = useState(
    settings.api_account_auto_pause_on_upstream_monitor_unavailable_enabled ?? false,
  );
  const [apiKeyAvailabilityAllTestsMustSucceed, setApiKeyAvailabilityAllTestsMustSucceed] = useState(
    settings.api_key_availability_all_tests_must_succeed ?? false,
  );
  const [upstreamMonitorAutoProbeEnabled, setUpstreamMonitorAutoProbeEnabled] = useState(
    settings.upstream_monitor_auto_probe_enabled ?? true,
  );
  const [accountModelWhitelistSyncEnabled, setAccountModelWhitelistSyncEnabled] = useState(
    settings.account_model_whitelist_sync_enabled ?? settings.account_model_whitelist_sync_each_time ?? false,
  );
  const [accountModelWhitelistSyncInterval, setAccountModelWhitelistSyncInterval] = useState(
    String(settings.account_model_whitelist_sync_interval_seconds ?? 3600),
  );
  const [upstreamMonitorFallbackWithoutMonitorEnabled, setUpstreamMonitorFallbackWithoutMonitorEnabled] = useState(
    settings.upstream_monitor_fallback_without_monitor_enabled ?? false,
  );
  const [upstreamMonitorFallbackTestModels, setUpstreamMonitorFallbackTestModels] = useState<string[]>(() =>
    normalizeFallbackModelChain(
      settings.upstream_monitor_fallback_test_models,
      settings.upstream_monitor_fallback_test_model,
    ),
  );
  const [fallbackModelDialogOpen, setFallbackModelDialogOpen] = useState(false);
  const fallbackModelDialogTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeFallbackModelDialog = useCallback(() => {
    setFallbackModelDialogOpen(false);
    window.requestAnimationFrame(() => fallbackModelDialogTriggerRef.current?.focus());
  }, []);
  const [upstreamMonitorFallbackTestAttempts, setUpstreamMonitorFallbackTestAttempts] = useState(
    String(settings.upstream_monitor_fallback_test_attempts ?? 1),
  );
  const [upstreamMonitorRecoveryTestAttempts, setUpstreamMonitorRecoveryTestAttempts] = useState(
    String(settings.upstream_monitor_recovery_test_attempts ?? 1),
  );
  const [upstreamMonitorTestAttemptInterval, setUpstreamMonitorTestAttemptInterval] = useState(
    String(settings.upstream_monitor_test_attempt_interval_seconds ?? 0),
  );
  const [negativeBalanceBasis, setNegativeBalanceBasis] = useState<"wallet" | "recharge_adjusted">(
    settings.upstream_negative_balance_basis || "wallet",
  );
  const [balancePauseThreshold, setBalancePauseThreshold] = useState(
    String(settings.upstream_balance_pause_threshold ?? 0),
  );
  const [showStaleNegativeBalanceAlert, setShowStaleNegativeBalanceAlert] = useState(
    settings.show_stale_negative_balance_alert ?? true,
  );
  const [priorityAssignDisabledAccounts, setPriorityAssignDisabledAccounts] = useState(
    settings.priority_assign_disabled_api_key_accounts ?? false,
  );
  const [priorityShareSameCompositeMultiplier, setPriorityShareSameCompositeMultiplier] = useState(
    settings.priority_share_same_upstream_actual_multiplier ?? false,
  );
  const [discordNotificationsEnabled, setDiscordNotificationsEnabled] = useState(
    settings.discord_bot_notifications_enabled ?? false,
  );
  const [discordSetupGuideOpen, setDiscordSetupGuideOpen] = useState(false);
  const discordSetupGuideTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeDiscordSetupGuide = useCallback(() => {
    setDiscordSetupGuideOpen(false);
    window.requestAnimationFrame(() => discordSetupGuideTriggerRef.current?.focus());
  }, []);
  const [discordBotToken, setDiscordBotToken] = useState("");
  const [clearDiscordBotToken, setClearDiscordBotToken] = useState(false);
  const [discordChannelId, setDiscordChannelId] = useState(settings.discord_bot_channel_id || "");
  const [notifyAccountScheduling, setNotifyAccountScheduling] = useState(
    (settings.notify_oauth_account_disabled ?? false) || (settings.notify_account_enabled ?? false),
  );
  const [notifyApiKeyRateChanged, setNotifyApiKeyRateChanged] = useState(settings.notify_api_key_rate_changed ?? false);
  const [notifyUpstreamGroupChanged, setNotifyUpstreamGroupChanged] = useState(settings.notify_upstream_group_changed ?? false);
  const [notifyUpstreamBalanceLow, setNotifyUpstreamBalanceLow] = useState(settings.notify_upstream_balance_low ?? false);
  const [notifyUpstreamTokenInvalid, setNotifyUpstreamTokenInvalid] = useState(
    settings.notify_upstream_token_invalid ?? false,
  );
  const [upstreamRateLogRetentionDays, setUpstreamRateLogRetentionDays] = useState(
    String(settings.upstream_rate_log_retention_days || 90),
  );
  const [changeLogPageSize, setChangeLogPageSize] = useState(settings.change_log_page_size || 50);
  const [changeLogPageSizeOptionsInput, setChangeLogPageSizeOptionsInput] = useState(
    normalizeChangeLogPageSizeOptions(settings.change_log_page_size_options).join(", "),
  );
  const [upstreamUsageDataRetentionDays, setUpstreamUsageDataRetentionDays] = useState(
    String(settings.upstream_usage_data_retention_days ?? 90),
  );
  const [usageLimitSampleFiveHourThreshold, setUsageLimitSampleFiveHourThreshold] = useState(
    String(settings.usage_limit_sample_five_hour_threshold_percent ?? 0),
  );
  const [usageLimitSampleSevenDayThreshold, setUsageLimitSampleSevenDayThreshold] = useState(
    String(settings.usage_limit_sample_seven_day_threshold_percent ?? 0),
  );
  const subscriptionTypesKey = subscriptionTypes.join("\u0000");
  const [usageLimitDefaultRanges, setUsageLimitDefaultRanges] = useState<UsageLimitDefaultRanges>(() =>
    mergeUsageLimitDefaultRanges(settings.usage_limit_default_ranges, subscriptionTypes),
  );
  const [newSubscriptionType, setNewSubscriptionType] = useState("");
  const [protocolRefreshMaxConcurrency, setProtocolRefreshMaxConcurrency] = useState(
    String(settings.protocol_refresh_max_concurrency ?? 2),
  );
  const [browserRefreshMaxConcurrency, setBrowserRefreshMaxConcurrency] = useState(
    String(settings.browser_refresh_max_concurrency ?? 1),
  );
  const [browserMinAvailableMemoryMb, setBrowserMinAvailableMemoryMb] = useState(
    String(settings.browser_min_available_memory_mb ?? 500),
  );
  const [subscriptionRefreshBatchSize, setSubscriptionRefreshBatchSize] = useState(
    String(settings.subscription_refresh_batch_size || 3),
  );
  const [subscriptionRefreshMaxConcurrency, setSubscriptionRefreshMaxConcurrency] = useState(
    String(settings.subscription_refresh_max_concurrency ?? 3),
  );
  const [accountLivenessMaxConcurrency, setAccountLivenessMaxConcurrency] = useState(
    String(settings.account_liveness_max_concurrency ?? 3),
  );
  const [displayTimeZone, setDisplayTimeZone] = useState(settings.display_timezone || defaultTimeZone);
  const [xApiKey, setXApiKey] = useState("");
  const [clearXApiKey, setClearXApiKey] = useState(false);
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [logoPreviewUrl, setLogoPreviewUrl] = useState<string | null>(null);
  const [resetLogo, setResetLogo] = useState(false);
  const [logoError, setLogoError] = useState("");
  const settingsPageRef = useRef<HTMLDivElement | null>(null);
  const [activeSettingsSection, setActiveSettingsSection] = useState(settingsNavigation[0].id);

  useEffect(() => {
    if (!logoFile) {
      setLogoPreviewUrl(null);
      return;
    }
    const previewUrl = URL.createObjectURL(logoFile);
    setLogoPreviewUrl(previewUrl);
    return () => URL.revokeObjectURL(previewUrl);
  }, [logoFile]);

  useEffect(() => {
    const settingsPage = settingsPageRef.current;
    const workspace = settingsPage?.closest<HTMLElement>(".workspace--settings");
    const documentScrollRoot = document.scrollingElement as HTMLElement | null;
    if (!settingsPage) return undefined;

    const getScrollRoot = () => {
      const workspaceStyle = workspace ? window.getComputedStyle(workspace) : null;
      const workspaceOwnsScroll = Boolean(
        workspace
        && workspace.scrollHeight > workspace.clientHeight
        && ["auto", "overlay", "scroll"].includes(workspaceStyle?.overflowY || ""),
      );
      return workspaceOwnsScroll ? workspace : documentScrollRoot;
    };

    let animationFrame = 0;
    const updateActiveSettingsSection = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        const scrollRoot = getScrollRoot();
        if (!scrollRoot) return;
        const rootTop = scrollRoot === documentScrollRoot ? 0 : scrollRoot.getBoundingClientRect().top;
        const activationLine = rootTop
          + Math.min(220, scrollRoot.clientHeight * 0.3);
        const maxScrollTop = scrollRoot.scrollHeight - scrollRoot.clientHeight;
        const isAtBottom = maxScrollTop > 0 && scrollRoot.scrollTop >= maxScrollTop - 2;
        let nextSectionId = settingsNavigation[0].id;

        for (const item of settingsNavigation) {
          const section = settingsPage.querySelector<HTMLElement>(`#${item.id}`);
          if (!section) continue;
          if (section.getBoundingClientRect().top > activationLine) break;
          nextSectionId = item.id;
        }

        if (isAtBottom) {
          nextSectionId = settingsNavigation[settingsNavigation.length - 1].id;
        }
        setActiveSettingsSection((current) => current === nextSectionId ? current : nextSectionId);
      });
    };

    const scrollTargets: HTMLElement[] = [];
    if (workspace) scrollTargets.push(workspace);
    if (documentScrollRoot && documentScrollRoot !== workspace) scrollTargets.push(documentScrollRoot);
    scrollTargets.forEach((target) => target.addEventListener("scroll", updateActiveSettingsSection, { passive: true }));
    window.addEventListener("scroll", updateActiveSettingsSection, { passive: true });
    window.addEventListener("resize", updateActiveSettingsSection);
    updateActiveSettingsSection();

    return () => {
      scrollTargets.forEach((target) => target.removeEventListener("scroll", updateActiveSettingsSection));
      window.removeEventListener("scroll", updateActiveSettingsSection);
      window.removeEventListener("resize", updateActiveSettingsSection);
      window.cancelAnimationFrame(animationFrame);
    };
  }, []);

  useEffect(() => {
    const settingsPage = settingsPageRef.current;
    const nav = settingsPage?.querySelector<HTMLElement>(".settings-local-nav");
    const activeButton = nav?.querySelector<HTMLElement>('button[aria-current="location"]');
    if (!nav || !activeButton) return undefined;

    const navRect = nav.getBoundingClientRect();
    const buttonRect = activeButton.getBoundingClientRect();
    const edgePadding = 8;
    if (buttonRect.left < navRect.left + edgePadding) {
      nav.scrollTo({
        behavior: "smooth",
        left: nav.scrollLeft - (navRect.left + edgePadding - buttonRect.left),
      });
    } else if (buttonRect.right > navRect.right - edgePadding) {
      nav.scrollTo({
        behavior: "smooth",
        left: nav.scrollLeft + (buttonRect.right - (navRect.right - edgePadding)),
      });
    }
    return undefined;
  }, [activeSettingsSection]);

  useEffect(() => {
    setSiteName(settings.site_name || defaultSiteName);
    setInstanceUrl(toSub2ApiInstanceUrl(settings.management_site_base_url));
    setRecoveryEnabled(settings.recovery_enabled);
    setAutoRecoverState(settings.management_site_auto_recover_state);
    setAutomationPaused(settings.automation_paused);
    setOauthAccountSyncEnabled(settings.oauth_account_sync_enabled ?? true);
    setOauthLoginMode(settings.oauth_login_mode ?? "protocol");
    setOauthStopOnPhoneVerification(settings.oauth_stop_on_phone_verification ?? false);
    setInterval(String(settings.monitor_interval_seconds));
    setUsageRefreshEnabled(settings.usage_refresh_enabled);
    setUsageRefreshInterval(String(settings.usage_refresh_interval_seconds));
    setUsageRefreshMaxConcurrency(String(settings.usage_refresh_max_concurrency ?? 20));
    setApiKeyAccountSyncEnabled(settings.api_key_account_sync_enabled ?? true);
    setApiKeyAccountSyncInterval(String(settings.api_key_account_sync_interval_seconds ?? 300));
    setUpstreamSyncEnabled(settings.upstream_sync_enabled ?? false);
    setUpstreamSyncInterval(String(settings.upstream_sync_interval_seconds ?? 900));
    setUpstreamSyncMaxConcurrency(String(settings.upstream_sync_max_concurrency ?? 10));
    setUpstreamRateSyncEnabled(settings.upstream_rate_sync_enabled ?? false);
    setUpstreamPrioritySyncEnabled(settings.upstream_priority_sync_enabled ?? true);
    setManualUpstreamRateEnabled(settings.manual_upstream_sync_rate_enabled ?? true);
    setManualUpstreamPriorityEnabled(settings.manual_upstream_sync_priority_enabled ?? true);
    setManualUpstreamHealthEnabled(settings.manual_upstream_sync_upstream_health_enabled ?? true);
    setManualUpstreamMonitorsEnabled(settings.manual_upstream_monitor_sync_enabled ?? true);
    setManualAccountAvailabilityEnabled(
      settings.manual_upstream_sync_account_availability_enabled ?? false,
    );
    setManualBalanceGuardEnabled(settings.manual_upstream_sync_balance_guard_enabled ?? true);
    setManualRatePauseEnabled(settings.manual_upstream_sync_rate_pause_enabled ?? true);
    setApiKeyAutoDisableEnabled(settings.api_key_auto_disable_on_upstream_unavailable ?? false);
    setApiKeyNegativeBalancePauseEnabled(settings.api_key_auto_pause_on_negative_balance_enabled ?? false);
    setApiKeyUpstreamMonitorPauseEnabled(
      settings.api_account_auto_pause_on_upstream_monitor_unavailable_enabled ?? false,
    );
    setApiKeyAvailabilityAllTestsMustSucceed(
      settings.api_key_availability_all_tests_must_succeed ?? false,
    );
    setUpstreamMonitorAutoProbeEnabled(settings.upstream_monitor_auto_probe_enabled ?? true);
    setAccountModelWhitelistSyncEnabled(
      settings.account_model_whitelist_sync_enabled ?? settings.account_model_whitelist_sync_each_time ?? false,
    );
    setAccountModelWhitelistSyncInterval(String(settings.account_model_whitelist_sync_interval_seconds ?? 3600));
    setUpstreamMonitorFallbackWithoutMonitorEnabled(
      settings.upstream_monitor_fallback_without_monitor_enabled ?? false,
    );
    setUpstreamMonitorFallbackTestModels(normalizeFallbackModelChain(
      settings.upstream_monitor_fallback_test_models,
      settings.upstream_monitor_fallback_test_model,
    ));
    setUpstreamMonitorFallbackTestAttempts(String(settings.upstream_monitor_fallback_test_attempts ?? 1));
    setUpstreamMonitorRecoveryTestAttempts(String(settings.upstream_monitor_recovery_test_attempts ?? 1));
    setUpstreamMonitorTestAttemptInterval(String(settings.upstream_monitor_test_attempt_interval_seconds ?? 0));
    setNegativeBalanceBasis(settings.upstream_negative_balance_basis || "wallet");
    setBalancePauseThreshold(String(settings.upstream_balance_pause_threshold ?? 0));
    setShowStaleNegativeBalanceAlert(settings.show_stale_negative_balance_alert ?? true);
    setPriorityAssignDisabledAccounts(settings.priority_assign_disabled_api_key_accounts ?? false);
    setPriorityShareSameCompositeMultiplier(
      settings.priority_share_same_upstream_actual_multiplier ?? false,
    );
    setDiscordNotificationsEnabled(settings.discord_bot_notifications_enabled ?? false);
    setDiscordBotToken("");
    setClearDiscordBotToken(false);
    setDiscordChannelId(settings.discord_bot_channel_id || "");
    setNotifyAccountScheduling(
      (settings.notify_oauth_account_disabled ?? false) || (settings.notify_account_enabled ?? false),
    );
    setNotifyApiKeyRateChanged(settings.notify_api_key_rate_changed ?? false);
    setNotifyUpstreamGroupChanged(settings.notify_upstream_group_changed ?? false);
    setNotifyUpstreamBalanceLow(settings.notify_upstream_balance_low ?? false);
    setNotifyUpstreamTokenInvalid(settings.notify_upstream_token_invalid ?? false);
    setUpstreamRateLogRetentionDays(String(settings.upstream_rate_log_retention_days || 90));
    setChangeLogPageSize(settings.change_log_page_size || 50);
    setChangeLogPageSizeOptionsInput(
      normalizeChangeLogPageSizeOptions(settings.change_log_page_size_options).join(", "),
    );
    setUpstreamUsageDataRetentionDays(
      String(settings.upstream_usage_data_retention_days ?? 90),
    );
    setUsageLimitSampleFiveHourThreshold(String(settings.usage_limit_sample_five_hour_threshold_percent ?? 0));
    setUsageLimitSampleSevenDayThreshold(String(settings.usage_limit_sample_seven_day_threshold_percent ?? 0));
    setUsageLimitDefaultRanges(mergeUsageLimitDefaultRanges(settings.usage_limit_default_ranges, subscriptionTypes));
    setNewSubscriptionType("");
    setProtocolRefreshMaxConcurrency(
      String(settings.protocol_refresh_max_concurrency ?? 2),
    );
    setBrowserRefreshMaxConcurrency(String(settings.browser_refresh_max_concurrency ?? 1));
    setBrowserMinAvailableMemoryMb(String(settings.browser_min_available_memory_mb ?? 500));
    setSubscriptionRefreshBatchSize(String(settings.subscription_refresh_batch_size || 3));
    setSubscriptionRefreshMaxConcurrency(String(settings.subscription_refresh_max_concurrency ?? 3));
    setAccountLivenessMaxConcurrency(String(settings.account_liveness_max_concurrency ?? 3));
    setDisplayTimeZone(settings.display_timezone || defaultTimeZone);
    setXApiKey("");
    setClearXApiKey(false);
    setLogoFile(null);
    setResetLogo(false);
    setLogoError("");
  }, [
    settings.automation_paused,
    settings.account_liveness_max_concurrency,
    settings.api_key_account_sync_enabled,
    settings.api_key_account_sync_interval_seconds,
    settings.api_key_auto_disable_on_upstream_unavailable,
    settings.api_key_auto_pause_on_negative_balance_enabled,
    settings.api_account_auto_pause_on_upstream_monitor_unavailable_enabled,
    settings.api_key_availability_all_tests_must_succeed,
    settings.upstream_monitor_auto_probe_enabled,
    settings.manual_upstream_sync_rate_enabled,
    settings.manual_upstream_sync_priority_enabled,
    settings.manual_upstream_sync_upstream_health_enabled,
    settings.manual_upstream_monitor_sync_enabled,
    settings.manual_upstream_sync_account_availability_enabled,
    settings.manual_upstream_sync_balance_guard_enabled,
    settings.manual_upstream_sync_rate_pause_enabled,
    settings.account_model_whitelist_sync_enabled,
    settings.account_model_whitelist_sync_interval_seconds,
    settings.account_model_whitelist_sync_each_time,
    settings.upstream_monitor_fallback_without_monitor_enabled,
    settings.upstream_monitor_fallback_test_models,
    settings.upstream_monitor_fallback_test_model,
    settings.upstream_monitor_fallback_test_attempts,
    settings.upstream_monitor_recovery_test_attempts,
    settings.upstream_monitor_test_attempt_interval_seconds,
    settings.upstream_negative_balance_basis,
    settings.upstream_balance_pause_threshold,
    settings.show_stale_negative_balance_alert,
    settings.priority_assign_disabled_api_key_accounts,
    settings.priority_share_same_upstream_actual_multiplier,
    settings.discord_bot_notifications_enabled,
    settings.discord_bot_token_hint,
    settings.discord_bot_token_set,
    settings.discord_bot_channel_id,
    settings.notify_oauth_account_disabled,
    settings.notify_account_enabled,
    settings.notify_api_key_rate_changed,
    settings.notify_upstream_group_changed,
    settings.notify_upstream_balance_low,
    settings.notify_upstream_token_invalid,
    settings.browser_min_available_memory_mb,
    settings.browser_refresh_max_concurrency,
    settings.display_timezone,
    settings.monitor_interval_seconds,
    settings.oauth_account_sync_enabled,
    settings.oauth_login_mode,
    settings.oauth_stop_on_phone_verification,
    settings.protocol_refresh_max_concurrency,
    settings.recovery_enabled,
    settings.site_name,
    settings.site_logo_url,
    settings.site_logo_updated_at,
    settings.subscription_refresh_batch_size,
    settings.subscription_refresh_max_concurrency,
    settings.management_site_auto_recover_state,
    settings.management_site_base_url,
    settings.management_site_x_api_key_hint,
    settings.management_site_x_api_key_set,
    settings.usage_refresh_enabled,
    settings.usage_refresh_interval_seconds,
    settings.usage_refresh_max_concurrency,
    settings.upstream_rate_log_retention_days,
    settings.change_log_page_size,
    settings.change_log_page_size_options,
    settings.upstream_usage_data_retention_days,
    settings.upstream_rate_sync_enabled,
    settings.upstream_priority_sync_enabled ?? true,
    settings.upstream_sync_enabled,
    settings.upstream_sync_interval_seconds,
    settings.upstream_sync_max_concurrency,
    settings.usage_limit_sample_five_hour_threshold_percent,
    settings.usage_limit_sample_seven_day_threshold_percent,
    settings.usage_limit_default_ranges,
    subscriptionTypesKey,
  ]);

  const intervalNumber = Number(interval);
  const usageRefreshIntervalNumber = Number(usageRefreshInterval);
  const usageRefreshMaxConcurrencyNumber = Number(usageRefreshMaxConcurrency);
  const apiKeyAccountSyncIntervalNumber = Number(apiKeyAccountSyncInterval);
  const accountModelWhitelistSyncIntervalNumber = Number(accountModelWhitelistSyncInterval);
  const upstreamSyncIntervalNumber = Number(upstreamSyncInterval);
  const upstreamSyncMaxConcurrencyNumber = Number(upstreamSyncMaxConcurrency);
  const balancePauseThresholdNumber = Number(balancePauseThreshold);
  const upstreamRateLogRetentionDaysNumber = Number(upstreamRateLogRetentionDays);
  const upstreamUsageDataRetentionDaysNumber = Number(upstreamUsageDataRetentionDays);
  const changeLogPageSizeOptions = parseChangeLogPageSizeOptions(changeLogPageSizeOptionsInput);
  const visibleChangeLogPageSizeOptions = changeLogPageSizeOptions
    || normalizeChangeLogPageSizeOptions(settings.change_log_page_size_options);
  const usageLimitSampleFiveHourThresholdNumber = Number(usageLimitSampleFiveHourThreshold);
  const usageLimitSampleSevenDayThresholdNumber = Number(usageLimitSampleSevenDayThreshold);
  const protocolRefreshMaxConcurrencyNumber = Number(protocolRefreshMaxConcurrency);
  const browserRefreshMaxConcurrencyNumber = Number(browserRefreshMaxConcurrency);
  const browserMinAvailableMemoryMbNumber = Number(browserMinAvailableMemoryMb);
  const subscriptionRefreshBatchSizeNumber = Number(subscriptionRefreshBatchSize);
  const subscriptionRefreshMaxConcurrencyNumber = Number(subscriptionRefreshMaxConcurrency);
  const accountLivenessMaxConcurrencyNumber = Number(accountLivenessMaxConcurrency);
  const upstreamMonitorTestAttemptIntervalNumber = Number(upstreamMonitorTestAttemptInterval);
  const cleanSiteName = siteName.trim();
  const cleanDiscordChannelId = discordChannelId.trim();
  const discordConfigurationInvalid = discordNotificationsEnabled && (
    !cleanDiscordChannelId
    || cleanDiscordChannelId.length > 64
    || ((!settings.discord_bot_token_set || clearDiscordBotToken) && !discordBotToken.trim())
  );
  const usageLimitDefaultRangesInvalid =
    !usageLimitDefaultRanges.unknown ||
    Object.keys(usageLimitDefaultRanges).length > 100 ||
    Object.values(usageLimitDefaultRanges).some((planRanges) =>
      usageLimitWindowKeys.some((windowKey) => {
        const range = planRanges[windowKey];
        return !Number.isFinite(range.lower) || !Number.isFinite(range.upper) || range.lower < 0 || range.upper < range.lower || range.upper > 1_000_000_000;
      }),
    );
  const invalid =
    !cleanSiteName ||
    cleanSiteName.length > 80 ||
    !isSub2ApiInstanceUrl(instanceUrl) ||
    !Number.isInteger(intervalNumber) ||
    intervalNumber < 30 ||
    intervalNumber > 86_400 ||
    !Number.isInteger(usageRefreshIntervalNumber) ||
    usageRefreshIntervalNumber < 60 ||
    usageRefreshIntervalNumber > 86_400 ||
    !Number.isInteger(usageRefreshMaxConcurrencyNumber) ||
    usageRefreshMaxConcurrencyNumber < 0 ||
    usageRefreshMaxConcurrencyNumber > 100 ||
    !Number.isInteger(apiKeyAccountSyncIntervalNumber) ||
    apiKeyAccountSyncIntervalNumber < 30 ||
    apiKeyAccountSyncIntervalNumber > 86_400 ||
    !Number.isInteger(accountModelWhitelistSyncIntervalNumber) ||
    accountModelWhitelistSyncIntervalNumber < 60 ||
    accountModelWhitelistSyncIntervalNumber > 86_400 ||
    !Number.isInteger(upstreamSyncIntervalNumber) ||
    upstreamSyncIntervalNumber < 60 ||
    upstreamSyncIntervalNumber > 86_400 ||
    !Number.isInteger(upstreamSyncMaxConcurrencyNumber) ||
    upstreamSyncMaxConcurrencyNumber < 0 ||
    upstreamSyncMaxConcurrencyNumber > 50 ||
    upstreamMonitorTestAttemptInterval.trim() === "" ||
    !Number.isInteger(upstreamMonitorTestAttemptIntervalNumber) ||
    upstreamMonitorTestAttemptIntervalNumber < 0 ||
    upstreamMonitorTestAttemptIntervalNumber > 300 ||
    !Number.isFinite(balancePauseThresholdNumber) ||
    balancePauseThresholdNumber < -1_000_000_000 ||
    balancePauseThresholdNumber > 1_000_000_000 ||
    !Number.isInteger(upstreamRateLogRetentionDaysNumber) ||
    upstreamRateLogRetentionDaysNumber < 1 ||
    upstreamRateLogRetentionDaysNumber > 3650 ||
    !Number.isInteger(upstreamUsageDataRetentionDaysNumber) ||
    upstreamUsageDataRetentionDaysNumber < 1 ||
    upstreamUsageDataRetentionDaysNumber > 3650 ||
    changeLogPageSizeOptions === null ||
    !changeLogPageSizeOptions.includes(changeLogPageSize) ||
    !Number.isFinite(usageLimitSampleFiveHourThresholdNumber) ||
    usageLimitSampleFiveHourThresholdNumber < 0 ||
    usageLimitSampleFiveHourThresholdNumber > 100 ||
    !Number.isFinite(usageLimitSampleSevenDayThresholdNumber) ||
    usageLimitSampleSevenDayThresholdNumber < 0 ||
    usageLimitSampleSevenDayThresholdNumber > 100 ||
    !Number.isInteger(protocolRefreshMaxConcurrencyNumber) ||
    protocolRefreshMaxConcurrencyNumber < 0 ||
    protocolRefreshMaxConcurrencyNumber > 50 ||
    !Number.isInteger(browserRefreshMaxConcurrencyNumber) ||
    browserRefreshMaxConcurrencyNumber < 0 ||
    browserRefreshMaxConcurrencyNumber > 50 ||
    !Number.isInteger(browserMinAvailableMemoryMbNumber) ||
    browserMinAvailableMemoryMbNumber < 0 ||
    browserMinAvailableMemoryMbNumber > 1_048_576 ||
    !Number.isInteger(subscriptionRefreshBatchSizeNumber) ||
    subscriptionRefreshBatchSizeNumber < 1 ||
    subscriptionRefreshBatchSizeNumber > 100 ||
    !Number.isInteger(subscriptionRefreshMaxConcurrencyNumber) ||
    subscriptionRefreshMaxConcurrencyNumber < 0 ||
    subscriptionRefreshMaxConcurrencyNumber > 20 ||
    !Number.isInteger(accountLivenessMaxConcurrencyNumber) ||
    accountLivenessMaxConcurrencyNumber < 0 ||
    accountLivenessMaxConcurrencyNumber > 50 ||
    usageLimitDefaultRangesInvalid ||
    discordConfigurationInvalid ||
    Boolean(logoError);

  useEffect(() => {
    onValidityChange(invalid);
  }, [invalid, onValidityChange]);

  useEffect(() => () => onValidityChange(false), [onValidityChange]);

  const addSubscriptionType = () => {
    const normalized = normalizeSubscriptionType(newSubscriptionType);
    if (!normalized || normalized === "unknown") return;
    setUsageLimitDefaultRanges((current) => ({
      ...current,
      [normalized]: cloneUsageLimitPlanRanges(current[normalized] || current.unknown || defaultUsageLimitPlanRanges),
    }));
    setNewSubscriptionType("");
  };

  const updateUsageLimitRange = (
    subscriptionType: string,
    windowKey: (typeof usageLimitWindowKeys)[number],
    bound: "lower" | "upper",
    value: string,
  ) => {
    setUsageLimitDefaultRanges((current) => {
      const nextPlanRanges = {
        ...current[subscriptionType],
        [windowKey]: {
          ...current[subscriptionType][windowKey],
          [bound]: Number(value),
        },
      };
      return {
        ...current,
        [subscriptionType]: subscriptionType === "team" ? nextPlanRanges : deriveMonthlyUsageLimitRange(nextPlanRanges),
      };
    });
  };

  const selectLogoFile = (file: File | null) => {
    if (!file) {
      setLogoFile(null);
      setLogoError("");
      return;
    }
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setLogoFile(null);
      setLogoError("Logo 仅支持 PNG、JPEG 或 WebP");
      return;
    }
    if (file.size > 1024 * 1024) {
      setLogoFile(null);
      setLogoError("Logo 文件不能超过 1 MB");
      return;
    }
    setLogoError("");
    setResetLogo(false);
    setLogoFile(file);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (invalid) return;
    const fallbackTestModels = normalizeFallbackModelChain(upstreamMonitorFallbackTestModels);
    const payload: AppSettingsUpdate = {
      site_name: cleanSiteName,
      management_site_base_url: toSub2ApiInstanceUrl(instanceUrl),
      recovery_enabled: recoveryEnabled,
      management_site_auto_recover_state: autoRecoverState,
      automation_paused: automationPaused,
      oauth_account_sync_enabled: oauthAccountSyncEnabled,
      oauth_login_mode: oauthLoginMode,
      oauth_stop_on_phone_verification: oauthStopOnPhoneVerification,
      monitor_interval_seconds: intervalNumber,
      usage_refresh_enabled: usageRefreshEnabled,
      usage_refresh_interval_seconds: usageRefreshIntervalNumber,
      usage_refresh_max_concurrency: usageRefreshMaxConcurrencyNumber,
      api_key_account_sync_enabled: apiKeyAccountSyncEnabled,
      api_key_account_sync_interval_seconds: apiKeyAccountSyncIntervalNumber,
      upstream_sync_enabled: upstreamSyncEnabled,
      upstream_sync_interval_seconds: upstreamSyncIntervalNumber,
      upstream_sync_max_concurrency: upstreamSyncMaxConcurrencyNumber,
      upstream_rate_sync_enabled: upstreamRateSyncEnabled,
      upstream_priority_sync_enabled: upstreamPrioritySyncEnabled,
      manual_upstream_sync_rate_enabled: manualUpstreamRateEnabled,
      manual_upstream_sync_priority_enabled: manualUpstreamPriorityEnabled,
      manual_upstream_sync_upstream_health_enabled: manualUpstreamHealthEnabled,
      manual_upstream_monitor_sync_enabled: manualUpstreamMonitorsEnabled,
      manual_upstream_sync_account_availability_enabled: manualAccountAvailabilityEnabled,
      manual_upstream_sync_balance_guard_enabled: manualBalanceGuardEnabled,
      manual_upstream_sync_rate_pause_enabled: manualRatePauseEnabled,
      api_key_auto_disable_on_upstream_unavailable: apiKeyAutoDisableEnabled,
      api_key_auto_pause_on_negative_balance_enabled: apiKeyNegativeBalancePauseEnabled,
      api_account_auto_pause_on_upstream_monitor_unavailable_enabled: apiKeyUpstreamMonitorPauseEnabled,
      api_key_availability_all_tests_must_succeed: apiKeyAvailabilityAllTestsMustSucceed,
      upstream_monitor_auto_probe_enabled: upstreamMonitorAutoProbeEnabled,
      account_model_whitelist_sync_enabled: accountModelWhitelistSyncEnabled,
      account_model_whitelist_sync_interval_seconds: accountModelWhitelistSyncIntervalNumber,
      upstream_monitor_fallback_without_monitor_enabled: upstreamMonitorFallbackWithoutMonitorEnabled,
      upstream_monitor_fallback_test_models: fallbackTestModels,
      upstream_monitor_fallback_test_model: fallbackTestModels[0] || "",
      upstream_monitor_fallback_test_attempts: Math.max(1, Math.min(5, Number(upstreamMonitorFallbackTestAttempts) || 1)),
      upstream_monitor_recovery_test_attempts: Math.max(1, Math.min(5, Number(upstreamMonitorRecoveryTestAttempts) || 1)),
      upstream_monitor_test_attempt_interval_seconds: upstreamMonitorTestAttemptIntervalNumber,
      upstream_negative_balance_basis: negativeBalanceBasis,
      upstream_balance_pause_threshold: balancePauseThresholdNumber,
      show_stale_negative_balance_alert: showStaleNegativeBalanceAlert,
      priority_assign_disabled_api_key_accounts: priorityAssignDisabledAccounts,
      priority_share_same_upstream_actual_multiplier: priorityShareSameCompositeMultiplier,
      upstream_rate_log_retention_days: upstreamRateLogRetentionDaysNumber,
      change_log_page_size: changeLogPageSize,
      change_log_page_size_options: changeLogPageSizeOptions || visibleChangeLogPageSizeOptions,
      upstream_usage_data_retention_days: upstreamUsageDataRetentionDaysNumber,
      discord_bot_notifications_enabled: discordNotificationsEnabled,
      discord_bot_channel_id: cleanDiscordChannelId,
      notify_oauth_account_disabled: notifyAccountScheduling,
      notify_account_enabled: notifyAccountScheduling,
      notify_api_key_rate_changed: notifyApiKeyRateChanged,
      notify_upstream_group_changed: notifyUpstreamGroupChanged,
      notify_upstream_balance_low: notifyUpstreamBalanceLow,
      notify_upstream_token_invalid: notifyUpstreamTokenInvalid,
      usage_limit_sample_five_hour_threshold_percent: usageLimitSampleFiveHourThresholdNumber,
      usage_limit_sample_seven_day_threshold_percent: usageLimitSampleSevenDayThresholdNumber,
      usage_limit_default_ranges: usageLimitDefaultRanges,
      protocol_refresh_max_concurrency: protocolRefreshMaxConcurrencyNumber,
      browser_refresh_max_concurrency: browserRefreshMaxConcurrencyNumber,
      browser_min_available_memory_mb: browserMinAvailableMemoryMbNumber,
      subscription_refresh_batch_size: subscriptionRefreshBatchSizeNumber,
      subscription_refresh_max_concurrency: subscriptionRefreshMaxConcurrencyNumber,
      account_liveness_max_concurrency: accountLivenessMaxConcurrencyNumber,
      display_timezone: displayTimeZone,
    };
    if (xApiKey.trim()) {
      payload.management_site_x_api_key = xApiKey.trim();
    }
    if (clearXApiKey) {
      payload.clear_management_site_x_api_key = true;
    }
    if (clearDiscordBotToken) {
      payload.clear_discord_bot_token = true;
    } else if (discordBotToken.trim()) {
      payload.discord_bot_token = discordBotToken.trim();
    }
    await onSave(payload, { logoFile, resetLogo });
  };
  const automationSwitches = [
    oauthAccountSyncEnabled,
    apiKeyAccountSyncEnabled,
    recoveryEnabled,
    upstreamSyncEnabled,
    upstreamRateSyncEnabled,
    upstreamPrioritySyncEnabled,
    apiKeyAutoDisableEnabled,
    apiKeyNegativeBalancePauseEnabled,
    apiKeyUpstreamMonitorPauseEnabled,
    upstreamMonitorAutoProbeEnabled,
    accountModelWhitelistSyncEnabled,
    usageRefreshEnabled,
  ];
  const enabledAutomationCount = automationSwitches.filter(Boolean).length;

  return (
    <div className="settings-page" ref={settingsPageRef}>
      <nav aria-label="设置页面导航" className="settings-local-nav">
        <div className="settings-local-nav-heading">
          <span><Settings2 size={16} />设置分组</span>
          <small>{settingsNavigation.length} 个功能区</small>
        </div>
        <div className="settings-local-nav-items">
          {settingsNavigation.map(({ icon: Icon, id, label }) => (
            <button
              aria-current={activeSettingsSection === id ? "location" : undefined}
              key={id}
              onClick={() => scrollToSettingsSection(id)}
              type="button"
            >
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </div>
      </nav>

      <div className="settings-content">
        <section className="settings-runtime-bar" id="settings-runtime">
          <div className="settings-runtime-copy">
            <span>运行控制</span>
            <strong>{automationPaused ? "自动任务已暂停" : "自动任务运行中"}</strong>
            <small>已开启 {enabledAutomationCount}/{automationSwitches.length} 项自动任务</small>
          </div>
          <label className="checkbox-line settings-toggle settings-global-toggle">
            <input
              checked={automationPaused}
              onChange={(event) => setAutomationPaused(event.target.checked)}
              type="checkbox"
            />
            <span>暂停全部自动任务</span>
          </label>
        </section>

        <section className="settings-form-shell">
        <form className="settings-form" id="runtime-settings-form" onSubmit={submit}>
          <fieldset className="settings-section settings-section--connection" id="settings-connection">
            <legend>基础与连接</legend>
            <div className="settings-grid settings-main-grid settings-connection-grid">
            <label className="site-name-label">
              站点名
              <input
                maxLength={80}
                onChange={(event) => setSiteName(event.target.value)}
                placeholder={defaultSiteName}
                value={siteName}
              />
            </label>
            <label className="instance-url-label">
              管理站点地址
              <div className="url-input-group">
                <input
                  onBlur={() => setInstanceUrl(toSub2ApiInstanceUrl(instanceUrl))}
                  onChange={(event) => setInstanceUrl(event.target.value)}
                  placeholder="https://management.example.com"
                  value={instanceUrl}
                />
                <span className="url-suffix">{sub2ApiApiPrefix}</span>
              </div>
            </label>
            <div className="site-logo-setting">
              <div className="site-logo-preview">
                <img alt="站点 Logo 预览" onError={fallbackSiteLogo} src={resetLogo ? "/logo.png" : logoPreviewUrl || logoUrl} />
              </div>
              <div className="site-logo-controls">
                <span><ImageIcon size={15} />站点 Logo</span>
                <div>
                  <label className="secondary-button site-logo-upload">
                    <Upload size={16} />
                    <span>选择图片</span>
                    <input
                      accept="image/png,image/jpeg,image/webp"
                      onChange={(event) => selectLogoFile(event.target.files?.[0] || null)}
                      type="file"
                    />
                  </label>
                  <button
                    className="icon-button"
                    onClick={() => {
                      setLogoFile(null);
                      setLogoError("");
                      setResetLogo(true);
                    }}
                    title="恢复默认 Logo"
                    type="button"
                  >
                    <RefreshCcw size={16} />
                  </button>
                </div>
                {logoFile ? <small>{logoFile.name}</small> : resetLogo ? <small>保存后恢复默认 Logo</small> : null}
                {logoError ? <small className="form-error">{logoError}</small> : null}
              </div>
            </div>
            </div>

            <div className="settings-connection-tools">
              <div className="settings-connection-secret">
                <div className="settings-grid secret-grid">
                  <label>
                    管理站点 x-api-key
                    <input
                      autoComplete="new-password"
                      onChange={(event) => setXApiKey(event.target.value)}
                      placeholder={settings.management_site_x_api_key_set ? "留空保持当前密钥" : "输入管理站点 x-api-key"}
                      type="password"
                      value={xApiKey}
                    />
                  </label>
                  <label className="checkbox-line settings-toggle">
                    <input
                      checked={clearXApiKey}
                      onChange={(event) => setClearXApiKey(event.target.checked)}
                      type="checkbox"
                    />
                    <span>清空已保存密钥</span>
                  </label>
                </div>
                <div className="key-state">
                  <KeyRound size={16} />
                  {settings.management_site_x_api_key_set ? (
                    <span className="settings-secret-state">
                      <span>已保存</span>
                      {settings.management_site_x_api_key_hint ? (
                        <MiddleEllipsisText text={settings.management_site_x_api_key_hint} />
                      ) : null}
                    </span>
                  ) : <span>未设置</span>}
                </div>
              </div>

              <div className="settings-scan-inline" id="settings-scan">
                <div className="settings-subsection-heading">
                  <span><Radar size={16} /><strong>连接检测</strong></span>
                  <button className="secondary-button" disabled={busy} onClick={onScan} type="button">
                    <Radar size={16} />
                    <span>扫描管理站点</span>
                  </button>
                </div>
                <div className="settings-status settings-connection-status">
                  <SignalLine label="配置来源" value={sourceLabel(settings.management_site_base_url_source)} />
                  <SignalLine label="当前地址" value={settings.management_site_base_url} />
                  <SignalLine label="上次扫描" value={settings.last_scan_at ? formatDate(settings.last_scan_at, displayTimeZone) : "暂无"} />
                  <SignalLine label="扫描结果" value={settings.last_scan_message || "暂无"} />
                </div>
              </div>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--automation" id="settings-oauth">
            <legend>OAuth 账号</legend>
            <AutomationSettingsTable>
              <AutomationSettingRow
                checked={oauthAccountSyncEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="OAuth 账号同步间隔"
                    maxSeconds={86_400}
                    minSeconds={30}
                    onChange={setInterval}
                    value={interval}
                  />
                )}
                label="同步管理站点 OAuth 账号"
                onChange={setOauthAccountSyncEnabled}
                threads={<AutomationSettingInherited>无需设置</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={recoveryEnabled}
                interval={<AutomationSettingInherited>跟随 OAuth 同步</AutomationSettingInherited>}
                label="刷新 OAuth 账号凭证"
                onChange={setRecoveryEnabled}
                threads={(
                  <div className="automation-setting-split">
                    <label>
                      <span>协议</span>
                      <input
                        aria-label="OAuth 协议刷新线程数"
                        max={50}
                        min={0}
                        onChange={(event) => setProtocolRefreshMaxConcurrency(event.target.value)}
                        type="number"
                        value={protocolRefreshMaxConcurrency}
                      />
                    </label>
                    <label>
                      <span>浏览器</span>
                      <input
                        aria-label="OAuth 浏览器刷新线程数"
                        max={50}
                        min={0}
                        onChange={(event) => setBrowserRefreshMaxConcurrency(event.target.value)}
                        type="number"
                        value={browserRefreshMaxConcurrency}
                      />
                    </label>
                  </div>
                )}
              />
              <AutomationSettingRow
                checked={usageRefreshEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="OAuth 用量窗口同步间隔"
                    maxSeconds={86_400}
                    minSeconds={60}
                    onChange={setUsageRefreshInterval}
                    value={usageRefreshInterval}
                  />
                )}
                label="同步 OAuth 账号用量窗口"
                onChange={setUsageRefreshEnabled}
                threads={(
                  <AutomationSettingNumber
                    ariaLabel="OAuth 用量窗口同步线程数"
                    max={100}
                    min={0}
                    onChange={setUsageRefreshMaxConcurrency}
                    value={usageRefreshMaxConcurrency}
                  />
                )}
              />
            </AutomationSettingsTable>

            <div
              className="settings-auto-pause-policy settings-oauth-policy"
              role="group"
              aria-label="OAuth 重新登录策略"
            >
              <div className="settings-policy-heading">
                <span className="settings-label-with-help">
                  <strong>重新登录策略</strong>
                  <HelpPopover label="查看 OAuth 重新登录策略说明">
                    Refresh Token 刷新失败后，使用这里选定的方式重新登录 OpenAI OAuth；协议模式与无头浏览器模式不会互相回退。
                  </HelpPopover>
                </span>
                <span className={`api-key-chip api-key-chip--${recoveryEnabled ? "success" : "muted"}`}>
                  {recoveryEnabled ? "已启用" : "已关闭"}
                </span>
              </div>
              <div className="settings-oauth-policy-controls">
                <div className="settings-oauth-mode-field">
                  <span>重新登录方式</span>
                  <div className="api-key-segmented api-key-segmented--two" role="group" aria-label="OAuth 重新登录方式">
                    <button
                      aria-pressed={oauthLoginMode === "protocol"}
                      className={oauthLoginMode === "protocol" ? "active" : ""}
                      onClick={() => setOauthLoginMode("protocol")}
                      type="button"
                    >协议</button>
                    <button
                      aria-pressed={oauthLoginMode === "browser"}
                      className={oauthLoginMode === "browser" ? "active" : ""}
                      onClick={() => setOauthLoginMode("browser")}
                      type="button"
                    >无头浏览器</button>
                  </div>
                </div>
                <label className="checkbox-line settings-toggle settings-oauth-phone-stop">
                  <input
                    checked={oauthStopOnPhoneVerification}
                    onChange={(event) => setOauthStopOnPhoneVerification(event.target.checked)}
                    type="checkbox"
                  />
                  <span className="settings-toggle-copy">
                    <span className="settings-label-with-help">
                      <strong>遇到手机验证码时停止</strong>
                      <HelpPopover label="查看手机验证码停止策略说明">
                        开启后，重新 OAuth 遇到手机验证会立即终止；任务日志和账号错误标签会记录该原因。
                      </HelpPopover>
                    </span>
                  </span>
                </label>
              </div>
            </div>

            <div className="settings-grid settings-section-grid settings-oauth-resource-grid">
              <label>
                浏览器最低可用内存（MB）
                <input
                  max={1048576}
                  min={0}
                  onChange={(event) => setBrowserMinAvailableMemoryMb(event.target.value)}
                  type="number"
                  value={browserMinAvailableMemoryMb}
                />
              </label>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={autoRecoverState}
                  onChange={(event) => setAutoRecoverState(event.target.checked)}
                  type="checkbox"
                />
                <span>凭证刷新成功后恢复管理站点调度状态</span>
              </label>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--automation" id="settings-api-key-sync">
            <legend>API 账号与上游同步</legend>
            <AutomationSettingsTable>
              <AutomationSettingRow
                checked={apiKeyAccountSyncEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="API 账号同步间隔"
                    maxSeconds={86_400}
                    minSeconds={30}
                    onChange={setApiKeyAccountSyncInterval}
                    value={apiKeyAccountSyncInterval}
                  />
                )}
                label="同步管理站点 API 账号"
                onChange={setApiKeyAccountSyncEnabled}
                threads={<AutomationSettingInherited>无需设置</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={accountModelWhitelistSyncEnabled}
                description="开启后按独立间隔刷新已导入账号的可用模型白名单；关闭时仍会在首次导入或本地缺失白名单时补齐。"
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="账号可用模型白名单刷新间隔"
                    maxSeconds={86_400}
                    minSeconds={60}
                    onChange={setAccountModelWhitelistSyncInterval}
                    value={accountModelWhitelistSyncInterval}
                  />
                )}
                label="自动刷新账号可用模型白名单"
                onChange={setAccountModelWhitelistSyncEnabled}
                threads={<AutomationSettingInherited>无需设置</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={upstreamSyncEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="API 账号上游同步间隔"
                    maxSeconds={86_400}
                    minSeconds={60}
                    onChange={setUpstreamSyncInterval}
                    value={upstreamSyncInterval}
                  />
                )}
                label="同步 API 账号上游"
                onChange={setUpstreamSyncEnabled}
                threads={(
                  <AutomationSettingNumber
                    ariaLabel="API 账号上游同步线程数"
                    max={50}
                    min={0}
                    onChange={setUpstreamSyncMaxConcurrency}
                    value={upstreamSyncMaxConcurrency}
                  />
                )}
              />
              <AutomationSettingRow
                checked={upstreamRateSyncEnabled}
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="修改 API 账号计费倍率"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamRateEnabled}
                    disabled={!upstreamRateSyncEnabled}
                    label="手动同步时修改 API 账号计费倍率"
                    onChange={setManualUpstreamRateEnabled}
                  />
                )}
                onChange={setUpstreamRateSyncEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={upstreamMonitorAutoProbeEnabled}
                description="关闭后保留上次已保存的上游监控结果；打开上游状态弹窗不会触发请求。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="自动探测上游监控"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamMonitorsEnabled}
                    disabled={!upstreamMonitorAutoProbeEnabled}
                    label="手动同步时探测上游监控"
                    onChange={setManualUpstreamMonitorsEnabled}
                  />
                )}
                onChange={setUpstreamMonitorAutoProbeEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={upstreamPrioritySyncEnabled}
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="修改 API 账号优先级"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamPriorityEnabled}
                    disabled={!upstreamPrioritySyncEnabled}
                    label="手动同步时修改 API 账号优先级"
                    onChange={setManualUpstreamPriorityEnabled}
                  />
                )}
                onChange={setUpstreamPrioritySyncEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
            </AutomationSettingsTable>
          </fieldset>

          <fieldset className="settings-section settings-section--automation" id="settings-api-key-policies">
            <legend>可用性与暂停</legend>
            <AutomationSettingsTable>
              <AutomationSettingRow
                checked={apiKeyUpstreamMonitorPauseEnabled}
                description="自动检测与策略判定跟随上游同步任务执行；手动检测不受自动任务暂停或其他暂停原因限制。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="API 账号可用性监测与自动暂停"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualAccountAvailabilityEnabled}
                    disabled={!apiKeyUpstreamMonitorPauseEnabled}
                    label="手动同步时检测 API 账号可用性"
                    onChange={setManualAccountAvailabilityEnabled}
                  />
                )}
                onChange={setApiKeyUpstreamMonitorPauseEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={apiKeyAutoDisableEnabled}
                description="按每次上游同步的当前 Key / 分组状态即时判断；上游恢复后，仅自动恢复由本插件暂停的账号。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="上游 Key / 分组不可用时自动停用 API 账号"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamHealthEnabled}
                    disabled={!apiKeyAutoDisableEnabled}
                    label="手动同步时执行上游 Key 和分组状态暂停判定"
                    onChange={setManualUpstreamHealthEnabled}
                  />
                )}
                onChange={setApiKeyAutoDisableEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={apiKeyNegativeBalancePauseEnabled}
                description="仅暂停探测时已启用的账号；余额达到或高于阈值且其他暂停原因均解除后自动恢复。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="上游余额低于阈值时自动暂停 API 账号"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualBalanceGuardEnabled}
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    label="手动同步时执行上游余额暂停判定"
                    onChange={setManualBalanceGuardEnabled}
                  />
                )}
                onChange={setApiKeyNegativeBalancePauseEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
            </AutomationSettingsTable>
            <div className="settings-automation-policies">
              <div
                className="settings-auto-pause-policy"
                role="group"
                aria-label="上游监控不可用自动暂停策略"
              >
                <div className="settings-policy-heading">
                  <span className="settings-label-with-help">
                    <strong>API 账号可用性监测策略</strong>
                    <HelpPopover label="查看 API Key 可用性监测策略说明">
                      仅处理已绑定具体监控面板或已启用独立模型测试的账号。监控面板代表上游的单个分组或模型路由，不代表上游站点整体状态；面板状态为可用或降级时都直接判定账号可用，不进行回退模型测试，降级仅以黄色状态提示。面板缺失、读取失败、状态未知或不可用时才按账号白名单模型回退测试。自动检测跟随上游同步；存在余额、上游 Key、分组或倍率等其他暂停原因时保留上次结果并暂停自动连接测试，手动检测仍会先刷新监控面板及其状态详情并执行。启用账号的暂停判定使用“暂停判定测试次数”，因可用性监测而暂停的账号使用独立的“恢复判定测试次数”；{apiKeyAvailabilityAllTestsMustSucceed ? "全部连接测试均成功才判定可用，任意一次失败都会暂停或保持暂停。" : "任意一次连接成功即判定可用，全部失败才暂停或保持暂停。"}没有可用回退模型时不会据此暂停账号。
                    </HelpPopover>
                  </span>
                  <span className={`api-key-chip api-key-chip--${apiKeyUpstreamMonitorPauseEnabled ? "success" : "muted"}`}>
                    {apiKeyUpstreamMonitorPauseEnabled ? "已启用" : "已关闭"}
                  </span>
                </div>
                <div className="settings-auto-pause-thresholds">
                  <div className="settings-model-chain-field settings-model-chain-summary">
                    <span className="settings-label-with-help">
                      回退测试模型链
                      <HelpPopover label="查看回退测试模型链说明">
                        按从上到下的顺序选择账号白名单中第一个存在的模型。账号单独配置的测试模型优先于全局模型链，最多可配置 10 个模型。
                      </HelpPopover>
                    </span>
                    <span className="settings-model-chain-summary-value">
                      {upstreamMonitorFallbackTestModels.length
                        ? `已配置 ${upstreamMonitorFallbackTestModels.length} 个模型`
                        : "尚未配置"}
                    </span>
                    <button
                      className="secondary-button settings-model-chain-configure"
                      onClick={() => setFallbackModelDialogOpen(true)}
                      ref={fallbackModelDialogTriggerRef}
                      type="button"
                    >
                      <Settings2 size={15} />
                      配置
                    </button>
                  </div>
                  <label className="checkbox-line settings-toggle settings-fallback-without-monitor">
                    <input
                      checked={upstreamMonitorFallbackWithoutMonitorEnabled}
                      disabled={!apiKeyUpstreamMonitorPauseEnabled}
                      onChange={(event) => setUpstreamMonitorFallbackWithoutMonitorEnabled(event.target.checked)}
                      type="checkbox"
                    />
                    <span className="settings-toggle-copy">
                      <span className="settings-label-with-help">
                        <strong>未绑定监控面板时使用回退模型链</strong>
                        <HelpPopover label="查看未绑定面板回退说明">
                          关闭时，选择“绑定监控面板”但未选择具体面板的账号会显示为未配置，不发起连接测试，也不会因此自动暂停。已绑定面板被删除或报告不可用时仍会回退测试。
                        </HelpPopover>
                      </span>
                    </span>
                  </label>
                  <label className="checkbox-line settings-toggle settings-availability-success-policy">
                    <input
                      checked={apiKeyAvailabilityAllTestsMustSucceed}
                      onChange={(event) => setApiKeyAvailabilityAllTestsMustSucceed(event.target.checked)}
                      type="checkbox"
                    />
                    <span className="settings-toggle-copy">
                      <span className="settings-label-with-help">
                        <strong>全部连接测试成功才判定可用</strong>
                        <HelpPopover label="查看连续连接测试成功判定说明">
                          关闭时，连续测试中任意一次成功即可判定可用；开启时，会执行配置的全部测试次数，只有全部成功才判定可用，任意一次失败都会判定不可用。此策略同时用于暂停与恢复判定。
                        </HelpPopover>
                      </span>
                    </span>
                  </label>
                  <label>
                    <span className="settings-label-with-help">
                      暂停判定测试次数
                      <HelpPopover label="查看暂停判定测试次数说明">
                        对当前未被可用性监测暂停的账号发起 1 至 5 次连接测试，并按当前成功判定策略决定是否暂停账号。
                      </HelpPopover>
                    </span>
                    <AutomationSettingNumber
                      ariaLabel="账号暂停判定测试次数"
                      max={5}
                      min={1}
                      onChange={setUpstreamMonitorFallbackTestAttempts}
                      value={upstreamMonitorFallbackTestAttempts}
                    />
                  </label>
                  <label>
                    <span className="settings-label-with-help">
                      恢复判定测试次数
                      <HelpPopover label="查看恢复判定测试次数说明">
                        对因可用性监测而自动暂停的账号发起 1 至 5 次连接测试，并按当前成功判定策略决定是否恢复账号。
                      </HelpPopover>
                    </span>
                    <AutomationSettingNumber
                      ariaLabel="账号恢复判定测试次数"
                      max={5}
                      min={1}
                      onChange={setUpstreamMonitorRecoveryTestAttempts}
                      value={upstreamMonitorRecoveryTestAttempts}
                    />
                  </label>
                  <label className="settings-test-attempt-interval">
                    <span className="settings-label-with-help">
                      多次测试间隔
                      <HelpPopover label="查看多次测试间隔说明">
                        当暂停或恢复判定需要测试多次时，每次测试后等待此时间再开始下一次；设为 0 秒时连续测试。
                      </HelpPopover>
                    </span>
                    <AutomationSettingDuration
                      ariaLabel="账号多次连接测试间隔"
                      maxSeconds={300}
                      minSeconds={0}
                      onChange={setUpstreamMonitorTestAttemptInterval}
                      value={upstreamMonitorTestAttemptInterval}
                    />
                  </label>
                </div>
              </div>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={priorityAssignDisabledAccounts}
                  onChange={(event) => setPriorityAssignDisabledAccounts(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>停用的 API 账号也参与优先级分配</strong>
                    <HelpPopover label="查看停用账号优先级说明">
                      仅控制优先级计算，与上游状态停用、余额暂停及自动恢复无关。
                    </HelpPopover>
                  </span>
                </span>
              </label>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={priorityShareSameCompositeMultiplier}
                  onChange={(event) => setPriorityShareSameCompositeMultiplier(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>同上游实际倍率账号使用相同优先级</strong>
                    <HelpPopover label="查看同倍率账号优先级说明">
                      开启后，同一优先级区间内上游实际倍率相同的账号共用一个调度优先级；不同倍率档位仍按区间步长递增，账号卡片不再提供同倍率排位按钮。
                    </HelpPopover>
                  </span>
                </span>
              </label>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={manualRatePauseEnabled}
                  onChange={(event) => setManualRatePauseEnabled(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>手动同步时执行账号倍率暂停判定</strong>
                    <HelpPopover label="查看手动倍率暂停判定说明">
                      关闭后，手动同步仍会刷新分组倍率和上游实际倍率，但不会新增或解除账号的倍率暂停原因；自动上游同步不受影响。
                    </HelpPopover>
                  </span>
                </span>
              </label>
              <div className="settings-balance-basis" aria-disabled={!apiKeyNegativeBalancePauseEnabled}>
                <span>余额暂停判断口径与阈值</span>
                <div className="api-key-segmented" role="group" aria-label="上游负余额判断口径">
                  <button
                    aria-pressed={negativeBalanceBasis === "wallet"}
                    className={negativeBalanceBasis === "wallet" ? "active" : ""}
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    onClick={() => setNegativeBalanceBasis("wallet")}
                    type="button"
                  >上游钱包余额</button>
                  <button
                    aria-pressed={negativeBalanceBasis === "recharge_adjusted"}
                    className={negativeBalanceBasis === "recharge_adjusted" ? "active" : ""}
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    onClick={() => setNegativeBalanceBasis("recharge_adjusted")}
                    type="button"
                  >充值倍率后余额</button>
                </div>
                <label className="settings-balance-threshold">
                  <span>暂停阈值</span>
                  <input
                    aria-label="上游余额暂停阈值"
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    max={1_000_000_000}
                    min={-1_000_000_000}
                    onChange={(event) => setBalancePauseThreshold(event.target.value)}
                    step="any"
                    type="number"
                    value={balancePauseThreshold}
                  />
                  <b>{negativeBalanceBasis === "recharge_adjusted" ? "¥" : "$"}</b>
                </label>
              </div>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={showStaleNegativeBalanceAlert}
                  onChange={(event) => setShowStaleNegativeBalanceAlert(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>首页显示上次已知低余额提醒</strong>
                    <HelpPopover label="查看历史余额提醒说明">
                      最新探测失败时，继续按当前口径和阈值展示上次成功余额；不会使用过期余额暂停账号。
                    </HelpPopover>
                  </span>
                </span>
              </label>
            </div>
            <div className="settings-grid settings-section-grid settings-policy-resource-grid">
              <label>
                账号测活最大线程数
                <input
                  max={50}
                  min={0}
                  onChange={(event) => setAccountLivenessMaxConcurrency(event.target.value)}
                  title="账号页手动测活使用此线程数"
                  type="number"
                  value={accountLivenessMaxConcurrency}
                />
              </label>
            </div>
          </fieldset>

          <fieldset className="settings-section" id="settings-usage">
            <legend>用量与订阅</legend>
            <div className="settings-grid settings-section-grid">
            <label>
              默认 5h 用量阈值 (%)
              <input
                max={100}
                min={0}
                onChange={(event) => setUsageLimitSampleFiveHourThreshold(event.target.value)}
                step="0.1"
                title={`0 表示使用默认 ${defaultUsageLimitSampleThresholdPercent}%`}
                type="number"
                value={usageLimitSampleFiveHourThreshold}
              />
            </label>
            <label>
              默认 7d 用量阈值 (%)
              <input
                max={100}
                min={0}
                onChange={(event) => setUsageLimitSampleSevenDayThreshold(event.target.value)}
                step="0.1"
                title={`0 表示使用默认 ${defaultUsageLimitSampleThresholdPercent}%`}
                type="number"
                value={usageLimitSampleSevenDayThreshold}
              />
            </label>
            <label>
              单次订阅查询数量
              <input
                max={100}
                min={1}
                onChange={(event) => setSubscriptionRefreshBatchSize(event.target.value)}
                type="number"
                value={subscriptionRefreshBatchSize}
              />
            </label>
            <label>
              订阅查询最大同时数
              <input
                max={20}
                min={0}
                onChange={(event) => setSubscriptionRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={subscriptionRefreshMaxConcurrency}
              />
            </label>
            </div>
            <section className="quota-range-settings settings-subsection" id="settings-quota-ranges">
              <div className="settings-subsection-heading">
                <strong>订阅默认额度区间</strong>
                <span>{Object.keys(usageLimitDefaultRanges).length} 种订阅</span>
              </div>
            <div className="quota-range-list">
              {Object.entries(usageLimitDefaultRanges)
                .sort(([left], [right]) => subscriptionTypeSortRank(left) - subscriptionTypeSortRank(right) || left.localeCompare(right))
                .map(([subscriptionType, planRanges]) => (
                  <div className="quota-range-row" key={subscriptionType}>
                    <div className="quota-range-type">
                      <strong>{subscriptionTypeLabel(subscriptionType)}</strong>
                      <span>{subscriptionType}</span>
                    </div>
                    {usageLimitWindowKeys.map((windowKey) => (
                      <div className="quota-range-window" key={windowKey}>
                        <span>{usageLimitWindowLabel(windowKey)}</span>
                        <label>
                          下限
                          <input
                            aria-label={`${subscriptionTypeLabel(subscriptionType)} ${usageLimitWindowLabel(windowKey)} 下限`}
                            disabled={subscriptionType !== "team" && windowKey === "monthly"}
                            min={0}
                            onChange={(event) => updateUsageLimitRange(subscriptionType, windowKey, "lower", event.target.value)}
                            step="0.01"
                            type="number"
                            title={subscriptionType !== "team" && windowKey === "monthly" ? "自动使用 7d 下限的 4 倍" : undefined}
                            value={planRanges[windowKey].lower}
                          />
                        </label>
                        <label>
                          上限
                          <input
                            aria-label={`${subscriptionTypeLabel(subscriptionType)} ${usageLimitWindowLabel(windowKey)} 上限`}
                            disabled={subscriptionType !== "team" && windowKey === "monthly"}
                            min={0}
                            onChange={(event) => updateUsageLimitRange(subscriptionType, windowKey, "upper", event.target.value)}
                            step="0.01"
                            type="number"
                            title={subscriptionType !== "team" && windowKey === "monthly" ? "自动使用 7d 上限的 4 倍" : undefined}
                            value={planRanges[windowKey].upper}
                          />
                        </label>
                      </div>
                    ))}
                    <button
                      aria-label={`删除 ${subscriptionTypeLabel(subscriptionType)} 额度配置`}
                      className="icon-button quota-range-delete"
                      disabled={coreSubscriptionTypes.has(subscriptionType)}
                      onClick={() =>
                        setUsageLimitDefaultRanges((current) =>
                          Object.fromEntries(Object.entries(current).filter(([key]) => key !== subscriptionType)),
                        )
                      }
                      title={coreSubscriptionTypes.has(subscriptionType) ? "内置订阅类型" : "删除订阅类型"}
                      type="button"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                ))}
            </div>
            <div className="quota-range-add">
              <input
                maxLength={80}
                onChange={(event) => setNewSubscriptionType(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addSubscriptionType();
                  }
                }}
                placeholder="新增订阅类型"
                value={newSubscriptionType}
              />
              <button
                aria-label="新增订阅类型"
                className="secondary-button"
                disabled={!newSubscriptionType.trim() || normalizeSubscriptionType(newSubscriptionType) === "unknown"}
                onClick={addSubscriptionType}
                title="新增订阅类型"
                type="button"
              >
                <Plus size={17} />
                <span>新增</span>
              </button>
            </div>
            </section>
          </fieldset>

          <fieldset className="settings-section settings-section--api-key" id="settings-data-management">
            <legend>数据管理</legend>
            <div className="settings-grid settings-section-grid">
              <label>
                上游变化保留天数
                <input
                  max={3650}
                  min={1}
                  onChange={(event) => setUpstreamRateLogRetentionDays(event.target.value)}
                  type="number"
                  value={upstreamRateLogRetentionDays}
                />
              </label>
              <label>
                API 账号统计数据存储天数
                <input
                  max={3650}
                  min={1}
                  onChange={(event) => setUpstreamUsageDataRetentionDays(event.target.value)}
                  type="number"
                  value={upstreamUsageDataRetentionDays}
                />
                <small>每日明细到期后会清理；各上游的累计成本和收入始终保留。</small>
              </label>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--notifications" id="settings-notifications">
            <legend>
              <span>Discord Bot 通知</span>
              <button
                aria-label="查看 Discord Bot 配置指南"
                className="icon-button discord-bot-setup-guide-trigger"
                onClick={() => setDiscordSetupGuideOpen(true)}
                ref={discordSetupGuideTriggerRef}
                title="查看 Discord Bot 的创建、安装和通知频道配置步骤"
                type="button"
              >
                <CircleHelp size={16} />
              </button>
            </legend>
            <label className="checkbox-line settings-toggle settings-global-toggle">
              <input
                checked={discordNotificationsEnabled}
                onChange={(event) => setDiscordNotificationsEnabled(event.target.checked)}
                type="checkbox"
              />
              <span>启用 Discord Bot 通知</span>
            </label>
            <div className="settings-grid settings-section-grid notification-channel-grid">
              <label>
                Bot Token
                <input
                  autoComplete="new-password"
                  onChange={(event) => {
                    const token = event.target.value;
                    setDiscordBotToken(token);
                    if (token) setClearDiscordBotToken(false);
                  }}
                  placeholder={settings.discord_bot_token_set ? `已保存 ${settings.discord_bot_token_hint || ""}` : "输入 Discord Bot Token"}
                  type="password"
                  value={discordBotToken}
                />
              </label>
              <label>
                Channel ID
                <input
                  maxLength={64}
                  onChange={(event) => setDiscordChannelId(event.target.value)}
                  placeholder="Discord 频道 ID"
                  value={discordChannelId}
                />
              </label>
            </div>
            <label className="checkbox-line settings-toggle">
              <input
                checked={clearDiscordBotToken}
                disabled={!settings.discord_bot_token_set && !discordBotToken}
                onChange={(event) => {
                  setClearDiscordBotToken(event.target.checked);
                  if (event.target.checked) {
                    setDiscordBotToken("");
                    setDiscordNotificationsEnabled(false);
                  }
                }}
                type="checkbox"
              />
              <span>清空已保存 Bot Token</span>
            </label>
            <div className="settings-notification-events">
              <label className="checkbox-line settings-toggle">
                <input checked={notifyAccountScheduling} onChange={(event) => setNotifyAccountScheduling(event.target.checked)} type="checkbox" />
                <span className="settings-label-with-help">
                  <span>账号调度</span>
                  <HelpPopover label="查看账号调度通知范围">
                    包含 OAuth 与 API 账号的停用、启用和自动恢复；自动恢复会保留此前的暂停原因。
                  </HelpPopover>
                </span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyApiKeyRateChanged} onChange={(event) => setNotifyApiKeyRateChanged(event.target.checked)} type="checkbox" />
                <span>倍率变化</span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyUpstreamGroupChanged} onChange={(event) => setNotifyUpstreamGroupChanged(event.target.checked)} type="checkbox" />
                <span>上游分组变化</span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyUpstreamBalanceLow} onChange={(event) => setNotifyUpstreamBalanceLow(event.target.checked)} type="checkbox" />
                <span>上游余额不足</span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyUpstreamTokenInvalid} onChange={(event) => setNotifyUpstreamTokenInvalid(event.target.checked)} type="checkbox" />
                <span>上游令牌失效</span>
              </label>
            </div>
            <div className="settings-notification-actions">
              <button
                className="secondary-button"
                disabled={busy || !discordNotificationsEnabled || !settings.discord_bot_token_set || !cleanDiscordChannelId}
                onClick={onTestNotification}
                title="使用已保存的 Discord 配置发送测试消息"
                type="button"
              >
                <Send size={16} />
                <span>发送测试通知</span>
              </button>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--display-security" id="settings-display-security">
            <legend>界面偏好</legend>
            <div className="settings-grid time-zone-grid">
              <label>
                显示时区
                <select onChange={(event) => setDisplayTimeZone(event.target.value)} value={displayTimeZone}>
                  {timeZoneOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <div className="time-zone-preview">
                <Globe2 size={16} />
                <span>{formatDate(new Date().toISOString(), displayTimeZone)}</span>
              </div>
            </div>

            <div className="settings-grid settings-section-grid settings-change-log-pagination-grid">
              <label>
                变化记录默认每页条数
                <select
                  onChange={(event) => setChangeLogPageSize(Number(event.target.value))}
                  value={changeLogPageSize}
                >
                  {visibleChangeLogPageSizeOptions.map((size) => (
                    <option key={size} value={size}>{size} 条</option>
                  ))}
                </select>
                <small>统一应用到三个变化记录页面，并随服务端配置保存。</small>
              </label>
              <label>
                每页条数可选项
                <input
                  aria-invalid={changeLogPageSizeOptions === null}
                  inputMode="numeric"
                  onChange={(event) => {
                    const value = event.target.value;
                    setChangeLogPageSizeOptionsInput(value);
                    const options = parseChangeLogPageSizeOptions(value);
                    if (options?.length) {
                      setChangeLogPageSize((current) => options.includes(current) ? current : options[0]);
                    }
                  }}
                  placeholder="20, 50, 100, 200"
                  value={changeLogPageSizeOptionsInput}
                />
                <small>使用逗号或空格分隔，最多 20 项；每项为 1 至 200 的整数。</small>
              </label>
            </div>
          </fieldset>
        </form>
        </section>
      </div>

      {fallbackModelDialogOpen ? (
        <FallbackModelChainDialog
          availableModels={settings.available_test_models || []}
          enabled={apiKeyUpstreamMonitorPauseEnabled}
          models={upstreamMonitorFallbackTestModels}
          onChange={setUpstreamMonitorFallbackTestModels}
          onClose={closeFallbackModelDialog}
        />
      ) : null}
      {discordSetupGuideOpen ? <DiscordBotSetupGuideDialog onClose={closeDiscordSetupGuide} /> : null}
    </div>
  );
}

function AutomationSettingsTable({ children }: { children: ReactNode }) {
  return (
    <div className="automation-settings-table">
      <div className="automation-settings-head">
        <span>功能开关</span>
        <span className="settings-label-with-help">
          线程数
          <HelpPopover label="查看线程数说明">
            填 0 表示本批任务不限并发，取得账号或上游清单后会同时发起。
          </HelpPopover>
        </span>
        <span>自动执行间隔</span>
        <span className="settings-label-with-help">
          手动同步
          <HelpPopover label="查看手动同步说明">
            勾选后，点击“同步 API 账号”或上游卡片的同步按钮时会执行该项；取消勾选可只刷新基础上游数据，减少等待和连接测试消耗。
          </HelpPopover>
        </span>
      </div>
      {children}
    </div>
  );
}

function AutomationSettingDuration({
  ariaLabel,
  disabled = false,
  maxSeconds,
  minSeconds,
  onChange,
  value,
}: {
  ariaLabel: string;
  disabled?: boolean;
  maxSeconds: number;
  minSeconds: number;
  onChange: (value: string) => void;
  value: string;
}) {
  const [unit, setUnit] = useState<AutomationDurationUnit>(() => preferredAutomationDurationUnit(value));
  const displayValue = automationDurationDisplayValue(value, unit);

  return (
    <div className="automation-setting-duration">
      <input
        aria-label={ariaLabel}
        disabled={disabled}
        max={Number(automationDurationDisplayValue(String(maxSeconds), unit))}
        min={Number(automationDurationDisplayValue(String(minSeconds), unit))}
        onChange={(event) => onChange(automationDurationSecondsValue(event.target.value, unit))}
        step="any"
        type="number"
        value={displayValue}
      />
      <select
        aria-label={`${ariaLabel}单位`}
        disabled={disabled}
        onChange={(event) => setUnit(event.target.value as AutomationDurationUnit)}
        value={unit}
      >
        {automationDurationUnits.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </div>
  );
}

function fallbackSiteLogo(event: { currentTarget: HTMLImageElement }) {
  const image = event.currentTarget;
  if (image.getAttribute("src") === "/logo.png") return;
  image.src = "/logo.png";
}

function sourceLabel(source: string) {
  return (
    {
      env: ".env",
      auto: "自动扫描",
      manual: "手动设置",
    }[source] || source
  );
}

function subscriptionTypeSortRank(value: string) {
  return ({ plus: 10, team: 20, pro: 30, free: 40, k12: 50, unknown: 100 } as Record<string, number>)[
    normalizeSubscriptionType(value)
  ] || 80;
}

function deriveMonthlyUsageLimitRange(value: UsageLimitPlanRanges): UsageLimitPlanRanges {
  const ranges = cloneUsageLimitPlanRanges(value);
  ranges.monthly = {
    lower: Math.min(ranges.seven_day.lower * 4, 1_000_000_000),
    upper: Math.min(ranges.seven_day.upper * 4, 1_000_000_000),
  };
  return ranges;
}

function mergeUsageLimitDefaultRanges(value: UsageLimitDefaultRanges, detectedTypes: string[]): UsageLimitDefaultRanges {
  const merged = Object.fromEntries(
    Object.entries({ ...defaultUsageLimitRanges, ...(value || {}) }).map(([subscriptionType, ranges]) => [
      normalizeSubscriptionType(subscriptionType),
      normalizeSubscriptionType(subscriptionType) === "team"
        ? cloneUsageLimitPlanRanges(ranges)
        : deriveMonthlyUsageLimitRange(ranges),
    ]),
  ) as UsageLimitDefaultRanges;
  const fallback = merged.unknown || cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges);
  for (const rawType of detectedTypes) {
    const subscriptionType = normalizeSubscriptionType(rawType);
    if (subscriptionType !== "unknown" && !merged[subscriptionType]) {
      merged[subscriptionType] = deriveMonthlyUsageLimitRange(fallback);
    }
  }
  merged.unknown = deriveMonthlyUsageLimitRange(fallback);
  return merged;
}

function usageLimitWindowLabel(windowKey: (typeof usageLimitWindowKeys)[number]) {
  return windowKey === "five_hour" ? "5h" : windowKey === "seven_day" ? "7d" : "月";
}

function isSub2ApiInstanceUrl(value: string) {
  const instanceUrl = toSub2ApiInstanceUrl(value);
  if (!instanceUrl) return false;
  const candidate = instanceUrl.includes("://") ? instanceUrl : `http://${instanceUrl}`;
  try {
    return Boolean(new URL(candidate).hostname);
  } catch {
    return false;
  }
}
