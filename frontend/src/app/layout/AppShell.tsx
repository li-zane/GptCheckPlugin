import { Activity, Clock3, KeyRound, LogOut, Mail, Radar, RefreshCcw, Save, Settings2, Smartphone, TimerReset, UsersRound, X } from "lucide-react";
import { NavLink, Outlet, useLocation, useMatches } from "react-router-dom";
import type { ReactNode, RefObject } from "react";

import { cx } from "../../shared/lib/cx";
import styles from "./AppShell.module.css";

const navigation = [
  ["/overview", "概览", Activity], ["/accounts", "OAuth 账号", UsersRound],
  ["/api-keys/upstreams", "API 账号", KeyRound], ["/usage", "额度", TimerReset],
  ["/usage-samples", "样本", Radar], ["/mailboxes", "邮箱", Mail],
  ["/phones", "手机号", Smartphone], ["/history", "历史", Clock3], ["/settings", "设置", Settings2],
] as const;

type AppShellProps = {
  apiKeySyncAction: ReactNode;
  brandName: string;
  busy: boolean;
  logoUrl: string;
  notice: string;
  oauthSyncAction: ReactNode;
  onDismissNotice: () => void;
  onLogout: () => void | Promise<void>;
  onPreloadApiKeys: () => void;
  onRefresh: () => void;
  pageRefreshing: boolean;
  settingsSaveDisabled: boolean;
  sidebarRef: RefObject<HTMLElement | null>;
  themeAction: ReactNode;
  topbarRef: RefObject<HTMLElement | null>;
  workspaceRef: RefObject<HTMLElement | null>;
};

export function AppShell({
  apiKeySyncAction,
  brandName,
  busy,
  logoUrl,
  notice,
  oauthSyncAction,
  onDismissNotice,
  onLogout,
  onPreloadApiKeys,
  onRefresh,
  pageRefreshing,
  settingsSaveDisabled,
  sidebarRef,
  themeAction,
  topbarRef,
  workspaceRef,
}: AppShellProps) {
  const matches = useMatches();
  const location = useLocation();
  const meta = [...matches].reverse().find((match) => match.handle && typeof match.handle === "object")?.handle as { title?: string; showSave?: boolean } | undefined;
  return (
    <main className={cx(styles.shell, "shell")}>
      {notice ? (
        <div aria-live="polite" className="global-notice" role="status">
          <span>{notice}</span>
          <button aria-label="关闭提示" onClick={onDismissNotice} title="关闭" type="button"><X size={15} /></button>
        </div>
      ) : null}
      <aside className={cx(styles.sidebar, "sidebar")} ref={sidebarRef}>
        <div className={cx(styles.brand, "brand")}>
          <div className="brand-mark"><img alt="" src={logoUrl} /></div>
          <div><strong>{brandName}</strong></div>
        </div>
        <nav className={cx(styles.nav, "nav")}>
          {navigation.map(([to, label, Icon]) => {
            const apiKeyItem = to.startsWith("/api-keys/");
            return (
              <NavLink
                aria-label={label}
                className={({ isActive }) => cx(styles.navItem, "nav-item", (isActive || (apiKeyItem && location.pathname.startsWith("/api-keys/"))) && styles.active, (isActive || (apiKeyItem && location.pathname.startsWith("/api-keys/"))) && "active")}
                key={to}
                onFocus={apiKeyItem ? onPreloadApiKeys : undefined}
                onMouseEnter={apiKeyItem ? onPreloadApiKeys : undefined}
                title={label}
                to={to}
              >
                <Icon size={18} /><span>{label}</span>
              </NavLink>
            );
          })}
        </nav>
        <div className="sidebar-actions">
          {themeAction}
          <button aria-label="退出" className="ghost-button" disabled={busy} onClick={() => void onLogout()} title="退出" type="button"><LogOut size={17} /><span>退出</span></button>
        </div>
      </aside>
      <section className={cx(styles.workspace, "workspace", location.pathname.startsWith("/api-keys/") && "workspace--api-keys")} ref={workspaceRef}>
        <header className={cx(styles.topbar, "topbar")} ref={topbarRef}>
          <div><p className={cx(styles.eyebrow, "eyebrow")}>本机管理面板</p><h1>{meta?.title ?? "概览"}</h1></div>
          <div className={cx(styles.actions, "topbar-actions")}>
            <button aria-label="刷新当前页面数据" className="icon-button toolbar-page-refresh" disabled={pageRefreshing || busy} onClick={onRefresh} title="刷新当前页面数据" type="button"><RefreshCcw className={pageRefreshing ? "spin" : ""} size={17} /></button>
            {meta?.showSave ? <button className="primary-button toolbar-settings-save" disabled={settingsSaveDisabled} form="runtime-settings-form" type="submit"><Save size={17} /><span>保存设置</span></button> : null}
            {oauthSyncAction}
            {apiKeySyncAction}
          </div>
        </header>
        <div className={styles.content}><Outlet /></div>
      </section>
    </main>
  );
}
