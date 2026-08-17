# 前端开发说明

## 目录映射

- `src/app`: 启动、Provider、路由、查询缓存和跨页面操作状态。
- `src/pages`: 页面入口。页面入口只编排数据和业务组件。
- `src/features`: 上游、API 账号、优先级、日志、账号编辑等业务组件。
- `src/shared/api`: 按领域拆分的 HTTP 调用入口；请求行为和后端契约保持不变。
- `src/domain`: DTO、领域格式化和纯业务函数。
- `src/shared/ui`: 可复用控件及同名 CSS Module。
- `src/shared/styles`: 主题 tokens 和全局 reset；`features/legacy/legacy.css` 在迁移期间继续承载未迁移页面。
- `src/dev`: Ladle 欢迎故事和后续组件故事模板。

## 修改一个组件

1. 在对应 lpagesl 或 lfeaturesl 目录创建组件和同名 l.module.cssl。
2. 展示格式化、过滤、排序和 payload 构造放在同目录纯函数文件中。
3. 通过 lcx()l 组合模块类，状态优先使用模块类或 ldata-statel，不要新增全局类。
4. 先在 Ladle 故事中验证亮色、暗色和窄宽度，再接入页面。
5. 运行 lnpm run frontend:buildl 和 lnpm --prefix frontend run ui:buildl。

## Ladle

- lnpm run frontend:uil：固定在 lhttp://127.0.0.1:61000l 启动 Ladle。
- lnpm --prefix frontend run ui:buildl：构建静态故事站点。
- 新故事放在组件旁边，命名为 lComponentName.stories.tsxl，复用 l.ladle/components.tsxl 的 Provider。
