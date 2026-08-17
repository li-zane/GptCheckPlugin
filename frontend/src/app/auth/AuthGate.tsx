import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Moon, RefreshCcw, ShieldCheck, Sparkles, Sun } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useState, type FormEvent, type ReactNode } from "react";

import { api, AUTH_EXPIRED_EVENT } from "../../shared/api";
import { clearSessionStorageCaches } from "../queryInvalidation";
import { queryKeys } from "../queryKeys";

type AuthContextValue = { signOut: () => Promise<void> };
type Theme = "light" | "dark";

const AuthContext = createContext<AuthContextValue | null>(null);
const themeStorageKey = "sub2api-at-theme";

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [forcedOut, setForcedOut] = useState(false);
  const auth = useQuery({
    enabled: !forcedOut,
    queryFn: api.me,
    queryKey: queryKeys.auth,
    staleTime: Number.POSITIVE_INFINITY,
  });

  const clearAuthenticatedState = useCallback(async () => {
    await queryClient.cancelQueries();
    queryClient.clear();
    clearSessionStorageCaches();
    setForcedOut(true);
  }, [queryClient]);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      await clearAuthenticatedState();
    }
  }, [clearAuthenticatedState]);

  useEffect(() => {
    const onExpired = () => void clearAuthenticatedState();
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, [clearAuthenticatedState]);

  if (!forcedOut && auth.isPending) return <BootScreen />;
  if (forcedOut || auth.isError) {
    return (
      <LoginScreen
        onLogin={async (adminKey) => {
          const session = await api.login(adminKey);
          queryClient.setQueryData(queryKeys.auth, session);
          setForcedOut(false);
        }}
      />
    );
  }
  return <AuthContext.Provider value={{ signOut }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth 必须在 AuthGate 内使用");
  return context;
}

function LoginScreen({ onLogin }: { onLogin: (adminKey: string) => Promise<void> }) {
  const [adminKey, setAdminKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [theme, setTheme] = useState<Theme>(getInitialTheme);

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { window.localStorage.setItem(themeStorageKey, theme); } catch { /* Restricted storage is optional. */ }
  }, [theme]);

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

  const isDark = theme === "dark";
  const ThemeIcon = isDark ? Sun : Moon;
  return (
    <main className="login-page">
      <section className="login-panel">
        <div className="login-panel-head">
          <div className="login-emblem"><img alt="" onError={fallbackLogo} src="/api/settings/logo" /></div>
          <button aria-label={isDark ? "切换到浅色模式" : "切换到暗色模式"} className="secondary-button theme-toggle compact" onClick={() => setTheme(isDark ? "light" : "dark")} type="button"><ThemeIcon size={17} /><span>{isDark ? "浅色" : "暗色"}</span></button>
        </div>
        <p className="eyebrow">账号管理助手</p>
        <h1>控制台登录</h1>
        <form onSubmit={submit}>
          <label htmlFor="adminKey">管理密钥</label>
          <input autoComplete="current-password" autoFocus id="adminKey" name="adminKey" onChange={(event) => setAdminKey(event.target.value)} placeholder="APP_ADMIN_KEY" type="password" value={adminKey} />
          {error ? <p className="form-error">{error}</p> : null}
          <button className="primary-button wide" disabled={busy || !adminKey} type="submit"><ShieldCheck size={18} /><span>{busy ? "验证中" : "进入后台"}</span></button>
        </form>
      </section>
      <section className="login-signal"><div className="signal-line" /><div className="signal-tile"><Sparkles size={18} /><span>Token Recovery Loop</span></div></section>
    </main>
  );
}

function BootScreen() {
  return <main className="boot"><RefreshCcw className="spin" size={24} /><span>加载中</span></main>;
}

function getInitialTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(themeStorageKey);
    if (stored === "light" || stored === "dark") return stored;
  } catch { /* Restricted storage is optional. */ }
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function fallbackLogo(event: { currentTarget: HTMLImageElement }) {
  if (!event.currentTarget.src.endsWith("/logo.png")) event.currentTarget.src = "/logo.png";
}
