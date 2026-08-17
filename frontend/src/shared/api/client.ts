export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_EXPIRED_EVENT = "sub2api-at-auth-expired";
export const NO_FRONTEND_TIMEOUT = null;

export async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number | null = 30_000,
): Promise<T> {
  const controller = new AbortController();
  const { signal: externalSignal, headers: initHeaders, ...requestInit } = init;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = timeoutMs === null ? null : window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "sub2api-at-guardian",
      ...(initHeaders || {}),
    },
    ...requestInit,
    signal: controller.signal,
  }).catch((error) => {
    if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) {
      if (timedOut) throw new Error("请求超时，请稍后重试或检查后端和网络状态。");
      throw new DOMException("请求已取消", "AbortError");
    }
    throw error;
  }).finally(() => {
    if (timeout !== null) window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  });

  if (!response.ok) {
    let message = fallbackHttpErrorMessage(response);
    try {
      const data = await response.json();
      message = data.detail || data.message || message;
    } catch {
      // Keep the HTTP status message.
    }
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function fallbackHttpErrorMessage(response: Response) {
  const statusText = response.statusText.trim();
  const status = statusText ? `${response.status} ${statusText}` : `${response.status}`;
  return response.status >= 500
    ? `后端服务异常 (${status})，请查看后端日志。`
    : `请求失败 (${status})`;
}
