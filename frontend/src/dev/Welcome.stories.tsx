import type { Story } from "@ladle/react";

import { Badge, Button, Dialog, EmptyState, Panel, SearchInput, Tabs } from "../shared/ui";
import { useState } from "react";

export const Welcome: Story = () => {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [tab, setTab] = useState<"overview" | "settings">("overview");
  const [search, setSearch] = useState("");
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 760 }}>
      <Panel title="前端组件工作台" tools={<Badge tone="info">中文模板</Badge>}>
        <p style={{ color: "var(--muted)", marginTop: 0 }}>从共享组件开始修改，确认状态和响应式行为后，再迁移到具体页面目录。</p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          <Button tone="primary" onClick={() => setDialogOpen(true)}>打开弹窗</Button>
          <Button onClick={() => setSearch("")}>重置搜索</Button>
          <SearchInput aria-label="搜索组件" onChange={(event) => setSearch(event.target.value)} placeholder="搜索组件" value={search} />
        </div>
      </Panel>
      <Tabs active={tab} items={[{ id: "overview", label: "概览" }, { id: "settings", label: "设置" }]} onChange={setTab} />
      <EmptyState description={search ? `当前筛选：${search}` : "这里放置待预览的业务组件。"} title={`${tab === "overview" ? "概览" : "设置"}故事模板`} />
      <Dialog footer={<Button tone="primary" onClick={() => setDialogOpen(false)}>完成</Button>} onClose={() => setDialogOpen(false)} open={dialogOpen} title="中文弹窗模板">
        <p>Dialog 统一处理 Esc、焦点圈定、滚动锁定和关闭后焦点恢复。</p>
      </Dialog>
    </div>
  );
};

Welcome.storyName = "欢迎与中文故事模板";
