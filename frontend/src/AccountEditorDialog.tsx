import {
  AlertTriangle,
  CheckCircle2,
  Plus,
  RefreshCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./shared/api";
import type {
  Account,
  AccountEditConfiguration,
  AccountEditCurrent,
  AccountEditPreset,
  AccountEditPresetConfiguration,
  AccountEditor,
} from "./domain";

type Props = {
  accounts: Account[];
  onClose: () => void;
  onNotice: (message: string) => void;
  onUpdated: (message: string) => Promise<void> | void;
};

function accountConfiguration(form: AccountEditCurrent): AccountEditConfiguration {
  return {
    concurrency: form.concurrency,
    priority: form.priority,
    rate_multiplier: form.rate_multiplier,
    status: form.status,
    schedulable: form.schedulable,
    proxy_id: form.proxy_id,
    group_ids: [...form.group_ids],
    model_whitelist: [...form.model_whitelist],
    openai_ws_mode: form.openai_ws_mode,
    codex_image_tool_mode: form.codex_image_tool_mode,
    openai_passthrough: form.openai_passthrough,
    openai_long_context_billing: form.openai_long_context_billing,
    openai_compact_mode: form.openai_compact_mode,
    codex_cli_only: form.codex_cli_only,
    codex_cli_only_allow_app_server: form.codex_cli_only_allow_app_server,
    auto_pause_5h_disabled: form.auto_pause_5h_disabled,
    auto_pause_7d_disabled: form.auto_pause_7d_disabled,
    auto_pause_5h_threshold_percent: form.auto_pause_5h_threshold_percent,
    auto_pause_7d_threshold_percent: form.auto_pause_7d_threshold_percent,
  };
}

function presetConfiguration(form: AccountEditCurrent): AccountEditPresetConfiguration {
  return { ...accountConfiguration(form), account_type_scope: form.account_type };
}

function sortedPresets(presets: AccountEditPreset[]) {
  return [...presets].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
}

export function AccountEditorDialog({ accounts, onClose, onNotice, onUpdated }: Props) {
  const accountIds = useMemo(
    () => [...new Set(accounts.map((account) => account.management_account_id || "").filter(Boolean))],
    [accounts],
  );
  const accountId = accountIds[0] || "";
  const batchMode = accountIds.length > 1;
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const [editor, setEditor] = useState<AccountEditor | null>(null);
  const [form, setForm] = useState<AccountEditCurrent | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [selectedPresetId, setSelectedPresetId] = useState("");
  const [newPresetName, setNewPresetName] = useState("");
  const [modelSearch, setModelSearch] = useState("");

  const installEditor = useCallback((nextEditor: AccountEditor) => {
    setEditor(nextEditor);
    setForm({
      ...nextEditor.account,
      group_ids: [...nextEditor.account.group_ids],
      model_whitelist: [...nextEditor.account.model_whitelist],
    });
    setSelectedPresetId((current) => (
      nextEditor.presets.some((preset) => String(preset.id) === current) ? current : ""
    ));
  }, []);

  const loadEditor = useCallback(async (signal?: AbortSignal) => {
    if (!accountId) return;
    setLoading(true);
    setError("");
    try {
      installEditor(await api.accountEditor(accountId, signal));
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : "账号配置读取失败");
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [accountId, installEditor]);

  useEffect(() => {
    const controller = new AbortController();
    void loadEditor(controller.signal);
    return () => controller.abort();
  }, [loadEditor]);

  useEffect(() => {
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyAction) onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [busyAction, onClose]);

  const selectedPreset = useMemo(
    () => editor?.presets.find((preset) => String(preset.id) === selectedPresetId) || null,
    [editor?.presets, selectedPresetId],
  );
  const availableGroupIds = useMemo(() => new Set(editor?.groups.map((group) => group.id) || []), [editor?.groups]);
  const availableModelIds = useMemo(
    () => new Set(editor?.model_candidates.map((model) => model.id) || []),
    [editor?.model_candidates],
  );
  const invalidGroupIds = form?.group_ids.filter((groupId) => !availableGroupIds.has(groupId)) || [];
  const invalidModelIds = form?.model_whitelist.filter((modelId) => !availableModelIds.has(modelId)) || [];
  const invalidProxy = Boolean(form?.proxy_id && !editor?.proxies.some((proxy) => proxy.id === form.proxy_id));
  const supportsOpenAISettings = form?.platform === "openai" && ["oauth", "setup-token", "apikey"].includes(form.account_type);
  const supportsCodexCLISettings = form?.platform === "openai" && ["oauth", "setup-token"].includes(form.account_type);
  const accountTypeLabel = form?.account_type === "apikey" ? "API key" : form?.account_type === "oauth" ? "OAuth GPT" : form?.account_type || "账号";
  const normalizedModelSearch = modelSearch.trim().toLowerCase();
  const visibleModels = useMemo(
    () => (editor?.model_candidates || []).filter((model) => (
      !normalizedModelSearch
      || model.id.toLowerCase().includes(normalizedModelSearch)
      || model.display_name.toLowerCase().includes(normalizedModelSearch)
    )),
    [editor?.model_candidates, normalizedModelSearch],
  );

  const replacePreset = (preset: AccountEditPreset) => {
    setEditor((current) => current ? {
      ...current,
      presets: sortedPresets([...current.presets.filter((item) => item.id !== preset.id), preset]),
    } : current);
  };

  const runMutation = async (action: string, operation: () => Promise<void>) => {
    setBusyAction(action);
    setError("");
    onNotice("");
    try {
      await operation();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusyAction("");
    }
  };

  const loadTargetEditors = useCallback(async () => {
    const results = await Promise.all(accountIds.map(async (targetAccountId) => {
      if (targetAccountId === accountId && editor) return editor;
      return api.accountEditor(targetAccountId);
    }));
    return results;
  }, [accountId, accountIds, editor]);

  const saveAccount = (event: FormEvent) => {
    event.preventDefault();
    if (!form || !accountId) return;
    void runMutation("save", async () => {
      const configuration = accountConfiguration(form);
      const targetEditors = batchMode ? await loadTargetEditors() : [editor].filter(Boolean) as AccountEditor[];
      const results = await Promise.allSettled(targetEditors.map((targetEditor) => api.updateAccountEditor(
        targetEditor.account.account_id,
        {
          ...configuration,
          name: batchMode ? targetEditor.account.name : form.name.trim(),
          expected_identity_fingerprint: targetEditor.account.identity_fingerprint,
        },
      )));
      const failed = results.filter((result) => result.status === "rejected");
      if (failed.length) {
        await onUpdated("");
        throw new Error(`已写入 ${results.length - failed.length}/${results.length} 个账号，${failed.length} 个失败：${failed.map((result) => result.reason instanceof Error ? result.reason.message : "未知错误").join("；")}`);
      }
      await onUpdated(batchMode ? `已批量更新 ${results.length} 个 ${accountTypeLabel} 账号。` : "账号配置已写入管理站点并完成回读校验。");
      onClose();
    });
  };

  const createPreset = () => {
    if (!form || !newPresetName.trim()) return;
    void runMutation("create-preset", async () => {
      const preset = await api.createAccountEditPreset({
        name: newPresetName.trim(),
        platform: form.platform,
        configuration: presetConfiguration(form),
      });
      replacePreset(preset);
      setSelectedPresetId(String(preset.id));
      setNewPresetName("");
      onNotice("模板已保存。");
    });
  };

  const updatePreset = () => {
    if (!form || !selectedPreset) return;
    void runMutation("update-preset", async () => {
      const preset = await api.updateAccountEditPreset(selectedPreset.id, {
        name: selectedPreset.name,
        configuration: presetConfiguration(form),
      });
      replacePreset(preset);
      onNotice("模板已更新。");
    });
  };

  const deletePreset = () => {
    if (!selectedPreset || !window.confirm(`确定删除模板“${selectedPreset.name}”吗？`)) return;
    void runMutation("delete-preset", async () => {
      const result = await api.deleteAccountEditPreset(selectedPreset.id);
      setEditor((current) => current ? {
        ...current,
        presets: current.presets.filter((preset) => preset.id !== selectedPreset.id),
      } : current);
      setSelectedPresetId("");
      onNotice(result.message);
    });
  };

  const applyPreset = () => {
    if (!form || !selectedPreset || !accountId) return;
    void runMutation("apply-preset", async () => {
      const targetEditors = batchMode ? await loadTargetEditors() : [editor].filter(Boolean) as AccountEditor[];
      const results = await Promise.allSettled(targetEditors.map((targetEditor) => api.applyAccountEditPreset(
        selectedPreset.id,
        targetEditor.account.account_id,
        targetEditor.account.identity_fingerprint,
      )));
      const failed = results.filter((result) => result.status === "rejected");
      const sourceResult = results.find((result) => result.status === "fulfilled" && result.value.editor.account.account_id === accountId);
      if (sourceResult?.status === "fulfilled") installEditor(sourceResult.value.editor);
      setSelectedPresetId(String(selectedPreset.id));
      if (failed.length) {
        await onUpdated("");
        throw new Error(`模板已应用到 ${results.length - failed.length}/${results.length} 个账号，${failed.length} 个失败：${failed.map((result) => result.reason instanceof Error ? result.reason.message : "未知错误").join("；")}`);
      }
      await onUpdated(batchMode
        ? `模板“${selectedPreset.name}”已通过有效性检查并应用到 ${results.length} 个账号。`
        : `模板“${selectedPreset.name}”已通过有效性检查并应用。`);
    });
  };

  const setNumber = (field: "concurrency" | "priority" | "rate_multiplier", value: number) => {
    setForm((current) => current ? { ...current, [field]: value } : current);
  };

  const toggleGroup = (groupId: number, checked: boolean) => {
    setForm((current) => current ? {
      ...current,
      group_ids: checked
        ? [...new Set([...current.group_ids, groupId])]
        : current.group_ids.filter((value) => value !== groupId),
    } : current);
  };

  const toggleModel = (modelId: string, checked: boolean) => {
    setForm((current) => current ? {
      ...current,
      model_whitelist: checked
        ? [...new Set([...current.model_whitelist, modelId])]
        : current.model_whitelist.filter((value) => value !== modelId),
    } : current);
  };

  const liveMessage = error || (busyAction === "apply-preset" ? "正在重新校验模板配置" : "");

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busyAction) onClose();
      }}
      role="presentation"
    >
      <section
        aria-busy={loading || Boolean(busyAction)}
        aria-labelledby="account-editor-title"
        aria-modal="true"
        className="mail-dialog account-editor-dialog"
        role="dialog"
      >
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">{batchMode ? `${accountIds.length} 个 ${accountTypeLabel} 账号` : `管理站点 #${accountId || "-"}`}</p>
            <h2 id="account-editor-title">{batchMode ? "批量编辑账号" : "编辑账号"}</h2>
          </div>
          <div className="account-editor-head-actions">
            <button
              aria-label="重新检查可用配置"
              className="icon-button"
              disabled={loading || Boolean(busyAction)}
              onClick={() => void loadEditor()}
              title="重新检查可用配置"
              type="button"
            >
              <RefreshCcw className={loading ? "spin" : ""} size={17} />
            </button>
            <button
              aria-label="关闭账号编辑"
              className="icon-button"
              disabled={Boolean(busyAction)}
              onClick={onClose}
              ref={closeButtonRef}
              title="关闭"
              type="button"
            >
              <X size={17} />
            </button>
          </div>
        </header>

        <span aria-atomic="true" aria-live="polite" className="sr-only">{liveMessage}</span>
        {error ? <div className="mail-error" role="alert">{error}</div> : null}
        {loading && !form ? <div className="account-editor-loading"><RefreshCcw className="spin" size={18} />正在读取账号配置...</div> : null}

        {form && editor ? (
          <form className="account-editor-body" onSubmit={saveAccount}>
            <section className="account-editor-section account-editor-preset-section">
              <div className="account-editor-section-head">
                <div>
                  <h3>预设模板</h3>
                  <span>{editor.presets.length} 个</span>
                </div>
                <small>资源检查 {new Date(editor.resources_checked_at).toLocaleString("zh-CN", { hour12: false })}</small>
              </div>
              <div className="account-editor-preset-controls">
                <label>
                  <span>已有模板</span>
                  <select onChange={(event) => setSelectedPresetId(event.currentTarget.value)} value={selectedPresetId}>
                    <option value="">选择模板</option>
                    {editor.presets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
                  </select>
                </label>
                <button
                  className="secondary-button"
                  disabled={!selectedPreset || Boolean(busyAction)}
                  onClick={applyPreset}
                  type="button"
                >
                  {busyAction === "apply-preset" ? <RefreshCcw className="spin" size={16} /> : <CheckCircle2 size={16} />}
                  <span>{busyAction === "apply-preset" ? "校验中" : "应用"}</span>
                </button>
                <button className="icon-button" disabled={!selectedPreset || Boolean(busyAction)} onClick={updatePreset} title="用当前配置覆盖模板" type="button">
                  <Save size={16} />
                </button>
                <button className="icon-button danger" disabled={!selectedPreset || Boolean(busyAction)} onClick={deletePreset} title="删除模板" type="button">
                  <Trash2 size={16} />
                </button>
              </div>
              <div className="account-editor-preset-create">
                <input
                  aria-label="新模板名称"
                  maxLength={80}
                  onChange={(event) => setNewPresetName(event.currentTarget.value)}
                  placeholder="新模板名称"
                  value={newPresetName}
                />
                <button className="secondary-button" disabled={!newPresetName.trim() || Boolean(busyAction)} onClick={createPreset} type="button">
                  <Plus size={16} />
                  <span>保存模板</span>
                </button>
              </div>
            </section>

            <section className="account-editor-section">
              <div className="account-editor-section-head"><h3>基础与调度</h3></div>
              <div className={batchMode ? "account-editor-fields is-batch" : "account-editor-fields"}>
                {!batchMode ? (
                  <label className="account-editor-name-field">
                    <span>账号名称</span>
                    <input maxLength={100} onChange={(event) => setForm({ ...form, name: event.currentTarget.value })} required value={form.name} />
                  </label>
                ) : null}
                <label>
                  <span>并发数</span>
                  <input min={1} max={1000} onChange={(event) => setNumber("concurrency", Number(event.currentTarget.value))} required type="number" value={form.concurrency} />
                </label>
                <label>
                  <span>优先级</span>
                  <input min={0} onChange={(event) => setNumber("priority", Number(event.currentTarget.value))} required type="number" value={form.priority} />
                </label>
                <label>
                  <span>计费倍率</span>
                  <input min={0} max={1000} onChange={(event) => setNumber("rate_multiplier", Number(event.currentTarget.value))} required step="0.0001" type="number" value={form.rate_multiplier} />
                </label>
                <label className="account-editor-proxy-field">
                  <span>代理</span>
                  <select
                    onChange={(event) => setForm({ ...form, proxy_id: event.currentTarget.value ? Number(event.currentTarget.value) : null })}
                    value={form.proxy_id || ""}
                  >
                    <option value="">直连</option>
                    {invalidProxy ? <option disabled value={form.proxy_id || ""}>已失效 · #{form.proxy_id}</option> : null}
                    {editor.proxies.map((proxy) => (
                      <option key={proxy.id} value={proxy.id}>{proxy.name}{proxy.detail ? ` · ${proxy.detail}` : ""}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>账号状态</span>
                  <select
                    onChange={(event) => setForm({ ...form, status: event.currentTarget.value as AccountEditCurrent["status"] })}
                    value={form.status || "active"}
                  >
                    <option value="active">正常</option>
                    <option value="inactive">停用</option>
                    <option value="error">错误</option>
                  </select>
                </label>
                <label className="checkbox-line account-editor-schedule-toggle">
                  <input checked={form.schedulable} onChange={(event) => setForm({ ...form, schedulable: event.currentTarget.checked })} type="checkbox" />
                  <span>参与调度</span>
                </label>
              </div>
            </section>

            {supportsOpenAISettings ? (
              <section className="account-editor-section account-editor-advanced-section">
                <div className="account-editor-section-head">
                  <h3>OpenAI / Codex 高级设置</h3>
                  <span>{form.account_type}</span>
                </div>
                <div className="account-editor-advanced-fields">
                  <label>
                    <span>WS Mode</span>
                    <select
                      onChange={(event) => setForm({ ...form, openai_ws_mode: event.currentTarget.value as NonNullable<AccountEditCurrent["openai_ws_mode"]> })}
                      value={form.openai_ws_mode || "off"}
                    >
                      <option value="off">关闭 (off)</option>
                      <option value="ctx_pool">上下文池 (ctx_pool)</option>
                      <option value="passthrough">透传 (passthrough)</option>
                      <option value="http_bridge">HTTP 桥接 (http_bridge)</option>
                    </select>
                  </label>
                  <label>
                    <span>Codex 图片桥接</span>
                    <select
                      onChange={(event) => setForm({ ...form, codex_image_tool_mode: event.currentTarget.value as NonNullable<AccountEditCurrent["codex_image_tool_mode"]> })}
                      value={form.codex_image_tool_mode || "inherit"}
                    >
                      <option value="inherit">跟随上游</option>
                      <option value="enabled">启用 Hosted 桥接</option>
                      <option value="disabled">不注入 Hosted 工具</option>
                      <option value="block">移除客户端图片工具</option>
                    </select>
                  </label>
                  <label>
                    <span>Compact 模式</span>
                    <select
                      onChange={(event) => setForm({ ...form, openai_compact_mode: event.currentTarget.value as NonNullable<AccountEditCurrent["openai_compact_mode"]> })}
                      value={form.openai_compact_mode || "auto"}
                    >
                      <option value="auto">自动</option>
                      <option value="force_on">强制开启</option>
                      <option value="force_off">强制关闭</option>
                    </select>
                  </label>
                </div>
                <div className="account-editor-toggle-grid">
                  <label className="checkbox-line">
                    <input checked={form.openai_passthrough === true} onChange={(event) => setForm({ ...form, openai_passthrough: event.currentTarget.checked })} type="checkbox" />
                    <span>OpenAI 透传</span>
                  </label>
                  <label className="checkbox-line">
                    <input checked={form.openai_long_context_billing === true} onChange={(event) => setForm({ ...form, openai_long_context_billing: event.currentTarget.checked })} type="checkbox" />
                    <span>长上下文计费</span>
                  </label>
                  {supportsCodexCLISettings ? (
                    <label className="checkbox-line">
                      <input
                        checked={form.codex_cli_only === true}
                        onChange={(event) => setForm({
                          ...form,
                          codex_cli_only: event.currentTarget.checked,
                          codex_cli_only_allow_app_server: event.currentTarget.checked ? form.codex_cli_only_allow_app_server : false,
                        })}
                        type="checkbox"
                      />
                      <span>仅允许 Codex CLI</span>
                    </label>
                  ) : null}
                  {supportsCodexCLISettings ? (
                    <label className="checkbox-line">
                      <input
                        checked={form.codex_cli_only_allow_app_server === true}
                        disabled={form.codex_cli_only !== true}
                        onChange={(event) => setForm({ ...form, codex_cli_only_allow_app_server: event.currentTarget.checked })}
                        type="checkbox"
                      />
                      <span>放行 App Server</span>
                    </label>
                  ) : null}
                </div>
                <div className="account-editor-threshold-grid">
                  <label>
                    <span>5h 自动暂停阈值 (%)</span>
                    <input
                      disabled={form.auto_pause_5h_disabled === true}
                      max={100}
                      min={0}
                      onChange={(event) => setForm({ ...form, auto_pause_5h_threshold_percent: Number(event.currentTarget.value) })}
                      step="0.1"
                      type="number"
                      value={form.auto_pause_5h_threshold_percent ?? 0}
                    />
                  </label>
                  <label className="checkbox-line account-editor-threshold-toggle">
                    <input checked={form.auto_pause_5h_disabled === true} onChange={(event) => setForm({ ...form, auto_pause_5h_disabled: event.currentTarget.checked })} type="checkbox" />
                    <span>停用 5h 自动暂停</span>
                  </label>
                  <label>
                    <span>7d 自动暂停阈值 (%)</span>
                    <input
                      disabled={form.auto_pause_7d_disabled === true}
                      max={100}
                      min={0}
                      onChange={(event) => setForm({ ...form, auto_pause_7d_threshold_percent: Number(event.currentTarget.value) })}
                      step="0.1"
                      type="number"
                      value={form.auto_pause_7d_threshold_percent ?? 0}
                    />
                  </label>
                  <label className="checkbox-line account-editor-threshold-toggle">
                    <input checked={form.auto_pause_7d_disabled === true} onChange={(event) => setForm({ ...form, auto_pause_7d_disabled: event.currentTarget.checked })} type="checkbox" />
                    <span>停用 7d 自动暂停</span>
                  </label>
                </div>
              </section>
            ) : null}

            <section className="account-editor-section">
              <div className="account-editor-section-head">
                <h3>可用分组</h3>
                <span>{form.group_ids.length} 个</span>
              </div>
              {invalidGroupIds.length ? (
                <div className="account-editor-invalid" role="alert">
                  <AlertTriangle size={16} />
                  <span>已失效分组：{invalidGroupIds.map((value) => `#${value}`).join("、")}</span>
                </div>
              ) : null}
              <div className="account-editor-choice-grid account-editor-group-grid">
                {invalidGroupIds.map((groupId) => (
                  <label className="is-invalid" key={`invalid-${groupId}`}>
                    <input checked onChange={(event) => toggleGroup(groupId, event.currentTarget.checked)} type="checkbox" />
                    <span><strong>已失效</strong><small>#{groupId}</small></span>
                  </label>
                ))}
                {editor.groups.map((group) => (
                  <label key={group.id}>
                    <input checked={form.group_ids.includes(group.id)} onChange={(event) => toggleGroup(group.id, event.currentTarget.checked)} type="checkbox" />
                    <span><strong>{group.name}</strong><small>{group.detail || `#${group.id}`}</small></span>
                  </label>
                ))}
              </div>
            </section>

            <section className="account-editor-section account-editor-model-section">
              <div className="account-editor-section-head">
                <h3>可用模型白名单</h3>
                <span>{form.model_whitelist.length ? `${form.model_whitelist.length} 个` : "不限制"}</span>
              </div>
              {!editor.model_candidates_complete ? (
                <div className="account-editor-invalid" role="alert">
                  <AlertTriangle size={16} />
                  <span>当前管理站点未返回完整模型候选，含白名单的模板将停止应用。</span>
                </div>
              ) : null}
              <div className="account-editor-model-toolbar">
                <input aria-label="搜索模型" onChange={(event) => setModelSearch(event.currentTarget.value)} placeholder="搜索模型" value={modelSearch} />
                <button
                  className="secondary-button"
                  disabled={!visibleModels.length || Boolean(busyAction)}
                  onClick={() => setForm({ ...form, model_whitelist: [...new Set([...form.model_whitelist, ...visibleModels.map((model) => model.id)])] })}
                  type="button"
                >
                  全选当前
                </button>
                <button className="secondary-button" disabled={!form.model_whitelist.length || Boolean(busyAction)} onClick={() => setForm({ ...form, model_whitelist: [] })} type="button">
                  清空
                </button>
              </div>
              <div className="account-editor-choice-grid account-editor-model-grid">
                {invalidModelIds.map((modelId) => (
                  <label className="is-invalid" key={`invalid-${modelId}`}>
                    <input checked onChange={(event) => toggleModel(modelId, event.currentTarget.checked)} type="checkbox" />
                    <span><strong>{modelId}</strong><small>已失效</small></span>
                  </label>
                ))}
                {visibleModels.map((model) => (
                  <label key={model.id} title={model.id}>
                    <input checked={form.model_whitelist.includes(model.id)} onChange={(event) => toggleModel(model.id, event.currentTarget.checked)} type="checkbox" />
                    <span><strong>{model.display_name}</strong><small>{model.id}</small></span>
                  </label>
                ))}
              </div>
            </section>

            <footer className="account-editor-footer">
              <span>{form.platform} · {form.account_type}</span>
              <button className="primary-button" disabled={Boolean(busyAction) || (!batchMode && !form.name.trim())} type="submit">
                {busyAction === "save" ? <RefreshCcw className="spin" size={17} /> : <Save size={17} />}
                <span>{busyAction === "save" ? "保存中" : batchMode ? `保存 ${accountIds.length} 个账号` : "保存账号"}</span>
              </button>
            </footer>
          </form>
        ) : null}
      </section>
    </div>
  );
}
