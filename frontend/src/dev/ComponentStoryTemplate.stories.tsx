import type { Story } from "@ladle/react";
import { useState } from "react";

import { Badge, Button, Panel, SearchInput, Tabs } from "../shared/ui";

/**
 * 复制这个文件到目标组件目录，再把示例状态替换成真实 props。
 * 每个故事只负责准备可编辑的展示状态，不调用后端接口。
 */
export const ChineseComponentTemplate: Story = () => {
  const [activeTab, setActiveTab] = useState("overview");
  const [query, setQuery] = useState("");

  return (
    <Panel title="组件故事模板" tools={<Badge tone="info">中文</Badge>}>
      <div style={{ display: "grid", gap: 12 }}>
        <Tabs
          active={activeTab}
          items={[{ id: "overview", label: "概览" }, { id: "details", label: "详情" }]}
          onChange={setActiveTab}
        />
        <SearchInput
          aria-label="搜索"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索组件状态"
          value={query}
        />
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button tone="primary">保存</Button>
          <Button onClick={() => setQuery("")}>清除</Button>
        </div>
        <small style={{ color: "var(--muted)" }}>
          当前视图：{activeTab === "overview" ? "概览" : "详情"}；筛选：{query || "全部"}
        </small>
      </div>
    </Panel>
  );
};

ChineseComponentTemplate.storyName = "中文组件故事模板";
