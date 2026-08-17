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
  filterUsageLimitSamples,
  sortUsageLimitSamples,
  usageSampleDatePresets,
  usageSampleDateRangeForPreset,
  type UsageSampleDatePreset,
  type UsageSampleSortDirection,
  type UsageSampleSortField,
} from "../../usageSampleSort";
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
import { Empty, PanelTitle, formatDate, formatMoney, formatPercent, usageLimitWindowKeys, useDisplayTimeZone } from "../shared/LegacyDisplay";

export function UsageLimitSamplesView({
  data,
  loading,
  error,
  onDelete,
  onDeleteMany,
  onRefresh,
}: {
  data: UsageLimitSamples | null;
  loading: boolean;
  error: string;
  onDelete: (sampleId: number) => Promise<void> | void;
  onDeleteMany: (sampleIds: number[]) => Promise<void> | void;
  onRefresh: () => Promise<unknown>;
}) {
  const timeZone = useDisplayTimeZone();
  const sampleWindowGroups = useMemo(
    () =>
      usageLimitWindowKeys
        .map((windowKey) => {
          const cohorts = (data?.windows || []).filter(
            (window) => window.window_key === windowKey && window.samples.length > 0,
          );
          return {
            windowKey,
            label: cohorts[0]?.label || windowKey,
            sampleCount: cohorts.reduce((total, window) => total + window.samples.length, 0),
            cohorts,
          };
        })
        .filter((group) => group.sampleCount > 0),
    [data?.windows],
  );
  const [selectedWindowKey, setSelectedWindowKey] = useState<string>("");
  useEffect(() => {
    if (!sampleWindowGroups.length) {
      if (selectedWindowKey) {
        setSelectedWindowKey("");
      }
      return;
    }
    if (!sampleWindowGroups.some((group) => group.windowKey === selectedWindowKey)) {
      setSelectedWindowKey(sampleWindowGroups[0].windowKey);
    }
  }, [sampleWindowGroups, selectedWindowKey]);
  const selectedWindowGroup = useMemo(
    () => sampleWindowGroups.find((group) => group.windowKey === selectedWindowKey) || sampleWindowGroups[0] || null,
    [sampleWindowGroups, selectedWindowKey],
  );
  const [selectedSubscriptionKey, setSelectedSubscriptionKey] = useState<string>("");
  useEffect(() => {
    const cohorts = selectedWindowGroup?.cohorts || [];
    if (!cohorts.length) {
      if (selectedSubscriptionKey) {
        setSelectedSubscriptionKey("");
      }
      return;
    }
    if (!cohorts.some((cohort) => cohort.plan_cohort === selectedSubscriptionKey)) {
      setSelectedSubscriptionKey(cohorts[0].plan_cohort);
    }
  }, [selectedSubscriptionKey, selectedWindowGroup]);
  const selectedSubscription = useMemo(
    () =>
      selectedWindowGroup?.cohorts.find((cohort) => cohort.plan_cohort === selectedSubscriptionKey) ||
      selectedWindowGroup?.cohorts[0] ||
      null,
    [selectedSubscriptionKey, selectedWindowGroup],
  );
  const [sampleSortField, setSampleSortField] = useState<UsageSampleSortField>("quota");
  const [sampleSortDirection, setSampleSortDirection] = useState<UsageSampleSortDirection>("asc");
  const [sampleStartDate, setSampleStartDate] = useState("");
  const [sampleEndDate, setSampleEndDate] = useState("");
  const [sampleDatePreset, setSampleDatePreset] = useState<UsageSampleDatePreset | null>(null);
  const [selectedSampleIds, setSelectedSampleIds] = useState<Set<number>>(() => new Set());
  const selectAllSamplesRef = useRef<HTMLInputElement>(null);
  const sampleDateRangeInvalid = Boolean(sampleStartDate && sampleEndDate && sampleStartDate > sampleEndDate);
  const filteredSamples = useMemo(
    () => sampleDateRangeInvalid
      ? []
      : filterUsageLimitSamples(
        selectedSubscription?.samples || [],
        sampleStartDate,
        sampleEndDate,
        timeZone,
      ),
    [sampleDateRangeInvalid, sampleEndDate, sampleStartDate, selectedSubscription?.samples, timeZone],
  );
  const sortedSamples = useMemo(
    () => sortUsageLimitSamples(filteredSamples, sampleSortField, sampleSortDirection),
    [filteredSamples, sampleSortDirection, sampleSortField],
  );
  const allVisibleSamplesSelected = sortedSamples.length > 0
    && sortedSamples.every((sample) => selectedSampleIds.has(sample.id));
  const someVisibleSamplesSelected = sortedSamples.some((sample) => selectedSampleIds.has(sample.id));
  useEffect(() => {
    setSelectedSampleIds(new Set());
  }, [sampleEndDate, sampleStartDate, selectedSubscriptionKey, selectedWindowKey]);
  useEffect(() => {
    const availableIds = new Set((selectedSubscription?.samples || []).map((sample) => sample.id));
    setSelectedSampleIds((current) => {
      const next = new Set([...current].filter((sampleId) => availableIds.has(sampleId)));
      return next.size === current.size ? current : next;
    });
  }, [selectedSubscription?.samples]);
  useEffect(() => {
    if (selectAllSamplesRef.current) {
      selectAllSamplesRef.current.indeterminate = someVisibleSamplesSelected && !allVisibleSamplesSelected;
    }
  }, [allVisibleSamplesSelected, someVisibleSamplesSelected]);
  const toggleSampleSort = (field: UsageSampleSortField) => {
    if (field === sampleSortField) {
      setSampleSortDirection((current) => current === "asc" ? "desc" : "asc");
      return;
    }
    setSampleSortField(field);
    setSampleSortDirection(field === "recorded_at" ? "desc" : "asc");
  };
  const applySampleDatePreset = (preset: UsageSampleDatePreset) => {
    const range = usageSampleDateRangeForPreset(preset, timeZone);
    setSampleStartDate(range.startDate);
    setSampleEndDate(range.endDate);
    setSampleDatePreset(preset);
  };
  const clearSampleDateFilter = () => {
    setSampleStartDate("");
    setSampleEndDate("");
    setSampleDatePreset(null);
  };
  const toggleVisibleSamples = () => {
    setSelectedSampleIds((current) => {
      const next = new Set(current);
      if (allVisibleSamplesSelected) {
        sortedSamples.forEach((sample) => next.delete(sample.id));
      } else {
        sortedSamples.forEach((sample) => next.add(sample.id));
      }
      return next;
    });
  };
  const toggleSampleSelection = (sampleId: number) => {
    setSelectedSampleIds((current) => {
      const next = new Set(current);
      if (next.has(sampleId)) next.delete(sampleId);
      else next.add(sampleId);
      return next;
    });
  };
  const deleteSelectedSamples = () => {
    const sampleIds = [...selectedSampleIds];
    if (!sampleIds.length) return;
    if (window.confirm(`确定删除选中的 ${sampleIds.length} 条额度样本吗？此操作不可撤销。`)) {
      void onDeleteMany(sampleIds);
    }
  };

  return (
    <div className="stack">
      <section className="panel usage-samples-hero">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="额度样本" icon={Radar} />
            <p className="panel-subtitle">
              展示本地保存、用于推断官方窗口额度的限流样本；样本持续累积，仅在手动选择后删除。
            </p>
          </div>
          <button className="secondary-button" disabled={loading} onClick={() => onRefresh().catch(() => undefined)} type="button">
            <RefreshCcw className={loading ? "spin" : ""} size={17} />
            <span>{loading ? "读取中" : "刷新样本"}</span>
          </button>
        </div>
        {error ? <div className="mail-error">{error}</div> : null}
        <div className="usage-samples-note">
          <span>
            触发阈值：5h ≥ {formatPercent(data?.five_hour_threshold_percent ?? data?.full_percent_threshold ?? null)} · 7d/月 ≥{" "}
            {formatPercent(data?.seven_day_threshold_percent ?? data?.full_percent_threshold ?? null)}
          </span>
          <span>按订阅类型与真实窗口分别统计</span>
          <span>样本数量 &lt; 10 条时使用默认区间</span>
          <span>样本数量 ≥ 10 条后使用 mean ± 3 sigma</span>
        </div>
      </section>

      {!data && loading ? <Empty label="正在读取额度样本" /> : null}
      {data && sampleWindowGroups.length ? (
        <>
          <div className="usage-sample-tabs" role="tablist" aria-label="额度样本视图切换">
            {sampleWindowGroups.map((group) => (
              <button
                key={group.windowKey}
                className={selectedWindowKey === group.windowKey ? "usage-sample-tab active" : "usage-sample-tab"}
                onClick={() => {
                  setSelectedWindowKey(group.windowKey);
                  setSelectedSubscriptionKey(group.cohorts[0]?.plan_cohort || "");
                }}
                role="tab"
                aria-selected={selectedWindowKey === group.windowKey}
                type="button"
              >
                <span className="usage-sample-tab-label">{group.label}</span>
                <span className="usage-sample-tab-metrics">
                  <small><strong>{group.sampleCount}</strong> 样本</small>
                  <small><strong>{group.cohorts.length}</strong> 订阅</small>
                </span>
              </button>
            ))}
          </div>
          {selectedWindowGroup ? (
            <div className="usage-sample-subscription-tabs" role="tablist" aria-label={`${selectedWindowGroup.label} 订阅类型切换`}>
              {selectedWindowGroup.cohorts.map((cohort) => (
                <button
                  className={selectedSubscription?.plan_cohort === cohort.plan_cohort ? "usage-sample-subscription-tab active" : "usage-sample-subscription-tab"}
                  key={`${cohort.window_key}:${cohort.plan_cohort}`}
                  onClick={() => setSelectedSubscriptionKey(cohort.plan_cohort)}
                  role="tab"
                  aria-selected={selectedSubscription?.plan_cohort === cohort.plan_cohort}
                  type="button"
                >
                  <span>{cohort.plan_label}</span>
                  <strong>{cohort.samples.length}</strong>
                </button>
              ))}
            </div>
          ) : null}
          {selectedSubscription ? (
            <section className="usage-sample-management" aria-label="样本日期筛选与批量管理">
              <div className="usage-sample-date-fields">
                <label>
                  <span>开始日期</span>
                  <input
                    aria-invalid={sampleDateRangeInvalid}
                    max={sampleEndDate || undefined}
                    onChange={(event) => {
                      setSampleStartDate(event.target.value);
                      setSampleDatePreset(null);
                    }}
                    type="date"
                    value={sampleStartDate}
                  />
                </label>
                <label>
                  <span>结束日期</span>
                  <input
                    aria-invalid={sampleDateRangeInvalid}
                    min={sampleStartDate || undefined}
                    onChange={(event) => {
                      setSampleEndDate(event.target.value);
                      setSampleDatePreset(null);
                    }}
                    type="date"
                    value={sampleEndDate}
                  />
                </label>
              </div>
              <div className="usage-sample-quick-filters" role="group" aria-label="快捷日期筛选">
                {usageSampleDatePresets.map((preset) => (
                  <button
                    aria-pressed={sampleDatePreset === preset.id}
                    className={sampleDatePreset === preset.id ? "usage-sample-filter-chip active" : "usage-sample-filter-chip"}
                    key={preset.id}
                    onClick={() => applySampleDatePreset(preset.id)}
                    type="button"
                  >
                    {preset.label}
                  </button>
                ))}
                {sampleStartDate || sampleEndDate ? (
                  <button className="usage-sample-filter-clear" onClick={clearSampleDateFilter} type="button">
                    <X size={14} />
                    <span>清除日期</span>
                  </button>
                ) : null}
              </div>
              <div className="usage-sample-selection-actions">
                <span>
                  显示 <strong>{sortedSamples.length}</strong> / 共 {selectedSubscription.samples.length} 条 · 已选 {selectedSampleIds.size} 条
                </span>
                <button
                  className="secondary-button"
                  disabled={loading || sortedSamples.length === 0}
                  onClick={toggleVisibleSamples}
                  type="button"
                >
                  {allVisibleSamplesSelected ? <X size={16} /> : <CheckCircle2 size={16} />}
                  <span>{allVisibleSamplesSelected ? "取消全选" : "全选当前"}</span>
                </button>
                <button
                  className="danger-button"
                  disabled={loading || selectedSampleIds.size === 0}
                  onClick={deleteSelectedSamples}
                  type="button"
                >
                  <Trash2 size={16} />
                  <span>删除已选</span>
                </button>
              </div>
              {sampleDateRangeInvalid ? <span className="form-error">开始日期不能晚于结束日期</span> : null}
            </section>
          ) : null}
          <div className="usage-samples-grid">
            {selectedSubscription ? (
              <section className="panel usage-sample-window" key={`${selectedSubscription.window_key}:${selectedSubscription.plan_cohort}`}>
                <div className="usage-sample-window-head">
                  <div>
                    <PanelTitle title={`${selectedSubscription.label} 样本 · ${selectedSubscription.plan_label}`} icon={TimerReset} />
                    <p className="panel-subtitle">
                      {selectedSubscription.calibration.source === "sigma" ? "当前使用统计区间" : "当前使用默认区间"} · 共 {selectedSubscription.samples.length} 条 ·
                      当前显示 {sortedSamples.length} 条 · 更新 {formatDate(data.updated_at, timeZone)}
                    </p>
                  </div>
                  <div className="usage-sample-calibration">
                    <strong>
                      {formatMoney(selectedSubscription.calibration.lower)} - {formatMoney(selectedSubscription.calibration.upper)}
                    </strong>
                    <span>
                      均值 {formatMoney(selectedSubscription.calibration.mean)} · sigma {formatMoney(selectedSubscription.calibration.sigma)}
                    </span>
                  </div>
                </div>
                <div className="table-wrap">
                  <table className="usage-sample-table">
                  <thead>
                    <tr>
                      <th className="usage-sample-select-cell">
                        <input
                          aria-label="全选当前筛选样本"
                          checked={allVisibleSamplesSelected}
                          disabled={loading || sortedSamples.length === 0}
                          onChange={toggleVisibleSamples}
                          ref={selectAllSamplesRef}
                          type="checkbox"
                        />
                      </th>
                      <th>#</th>
                      <th>套餐</th>
                      <th>邮箱</th>
                      <th aria-sort={sampleSortField === "quota" ? (sampleSortDirection === "asc" ? "ascending" : "descending") : "none"}>
                        <button
                          aria-label={`按额度${sampleSortField === "quota" && sampleSortDirection === "asc" ? "降序" : "升序"}排列`}
                          className={sampleSortField === "quota" ? "usage-sample-sort active" : "usage-sample-sort"}
                          onClick={() => toggleSampleSort("quota")}
                          title="切换额度排序"
                          type="button"
                        >
                          <span>窗口总额</span>
                          {sampleSortField !== "quota" ? <ArrowUpDown size={14} /> : sampleSortDirection === "asc" ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
                        </button>
                      </th>
                      <th>限流已用</th>
                      <th>官方百分比</th>
                      <th>重置</th>
                      <th aria-sort={sampleSortField === "recorded_at" ? (sampleSortDirection === "asc" ? "ascending" : "descending") : "none"}>
                        <button
                          aria-label={`按记录时间${sampleSortField === "recorded_at" && sampleSortDirection === "desc" ? "升序" : "降序"}排列`}
                          className={sampleSortField === "recorded_at" ? "usage-sample-sort active" : "usage-sample-sort"}
                          onClick={() => toggleSampleSort("recorded_at")}
                          title="切换记录时间排序"
                          type="button"
                        >
                          <span>记录时间</span>
                          {sampleSortField !== "recorded_at" ? <ArrowUpDown size={14} /> : sampleSortDirection === "asc" ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
                        </button>
                      </th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedSamples.map((sample, index) => (
                      <tr className={selectedSampleIds.has(sample.id) ? "is-selected" : ""} key={sample.id}>
                        <td className="usage-sample-select-cell">
                          <input
                            aria-label={`选择额度样本 ${sample.id}`}
                            checked={selectedSampleIds.has(sample.id)}
                            disabled={loading}
                            onChange={() => toggleSampleSelection(sample.id)}
                            type="checkbox"
                          />
                        </td>
                        <td className="mono muted">{index + 1}</td>
                        <td>{sample.plan_cohort}</td>
                        <td>
                          <div className="usage-sample-account">
                            <span className="mono">{sample.email || "-"}</span>
                            <span>{sample.management_account_id || sample.account_key}</span>
                          </div>
                        </td>
                        <td>{formatMoney(sample.observed_limit)}</td>
                        <td>{formatMoney(sample.raw_spent)}</td>
                        <td>{formatPercent(sample.used_percent)}</td>
                        <td>{sample.reset_at ? formatDate(sample.reset_at, timeZone) : "-"}</td>
                        <td>{formatDate(sample.updated_at || sample.created_at, timeZone)}</td>
                        <td>
                          <button
                            aria-label={`删除额度样本 ${sample.id}`}
                            className="icon-button usage-sample-delete"
                            disabled={loading}
                            onClick={() => {
                              if (window.confirm(`确定删除额度样本 #${sample.id} 吗？此操作不可撤销。`)) {
                                void onDelete(sample.id);
                              }
                            }}
                            title="删除此样本"
                            type="button"
                          >
                            <Trash2 size={15} />
                          </button>
                        </td>
                      </tr>
                    ))}
                    {!sortedSamples.length ? (
                      <tr>
                        <td className="usage-sample-filter-empty" colSpan={10}>
                          {sampleDateRangeInvalid ? "请调整日期范围" : "当前日期范围内没有样本"}
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                  </table>
                </div>
              </section>
            ) : null}
          </div>
        </>
      ) : null}
      {data && !sampleWindowGroups.length ? <Empty label="暂无额度样本" /> : null}
    </div>
  );
}
