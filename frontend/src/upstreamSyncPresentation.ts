export type UpstreamDiscoveryCopy = {
  bulkLabel: string;
  bulkBusyLabel: string;
  channelAriaPrefix: string;
  channelTitle: string;
  allSuccess: string;
  allError: string;
  empty: string;
};

export function upstreamDiscoveryCopy(rateWritesEnabled: boolean): UpstreamDiscoveryCopy {
  if (rateWritesEnabled) {
    return {
      bulkLabel: "探测并应用全部渠道",
      bulkBusyLabel: "探测并应用中",
      channelAriaPrefix: "探测并应用渠道",
      channelTitle: "探测余额、分组与目标倍率，并应用账号计费倍率",
      allSuccess: "全部上游渠道探测并应用完成；账号状态与倍率日志已刷新。",
      allError: "批量探测并应用失败",
      empty: "暂无可探测并应用的上游渠道",
    };
  }
  return {
    bulkLabel: "探测全部渠道",
    bulkBusyLabel: "探测中",
    channelAriaPrefix: "探测渠道",
    channelTitle: "探测余额、分组与目标倍率",
    allSuccess: "全部上游渠道探测完成；余额、分组与目标倍率已刷新，未修改账号计费倍率。",
    allError: "批量探测失败",
    empty: "暂无可探测的上游渠道",
  };
}

export function channelDiscoverySuccessMessage(rateWritesEnabled: boolean, channelName: string) {
  return rateWritesEnabled
    ? `已完成「${channelName}」探测并应用；账号状态与倍率日志已刷新。`
    : `已探测「${channelName}」的余额、分组与目标倍率；未修改账号计费倍率。`;
}

export function channelDiscoveryErrorMessage(rateWritesEnabled: boolean, channelName: string) {
  return `${channelName} ${rateWritesEnabled ? "探测并应用" : "探测"}失败`;
}

export function apiAccountSyncMessage(
  result: { total: number; succeeded: number; failed: number },
  rateWritesEnabled: boolean,
) {
  const action = rateWritesEnabled ? "探测并应用" : "探测";
  if (result.total === 0) return "未在 sub2api 中发现可同步的 API Key 渠道。";
  if (result.failed > 0) {
    return `API 账号同步完成：${result.succeeded}/${result.total} 个渠道${action}成功，${result.failed} 个失败。`;
  }
  return `API 账号同步完成：${result.succeeded} 个渠道${action}成功。`;
}

export function accountRateStatusLabel(
  targetRate: unknown,
  wouldChange: boolean | null | undefined,
  rateWritesEnabled: boolean,
) {
  if (!isFiniteNumber(targetRate)) return "待计算";
  if (wouldChange === true) {
    return rateWritesEnabled ? "待自动同步" : "待应用（自动同步关闭）";
  }
  if (wouldChange === false) return "已同步";
  return "待确认当前倍率";
}

export function upstreamMutationControlsDisabled({
  liveDataValidated,
  loading,
  refreshing,
}: {
  liveDataValidated: boolean;
  loading: boolean;
  refreshing: boolean;
}) {
  return !liveDataValidated || loading || refreshing;
}

function isFiniteNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return false;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number);
}
