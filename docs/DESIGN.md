# xclaw 极简人类监督设计

## 1. 目标

`xclaw` 是一个面向单目标仓库、单活跃任务的本地 gateway service。

这一版设计的核心目标是：

- 用户只做五件事：提交任务、看进度、提建议、必要时确认计划、最终做交付验收
- 系统使用内部多角色协作能力
- 正式计划收敛到唯一工件 `plan.md`
- 人类只在需要做产品/业务判断时介入，而不是被迫参与所有计划微调

用户心智模型只有：

1. `xclaw start`：提交任务
2. `xclaw status`：看进度
3. `xclaw status --advise "..."`：中途提建议
4. `xclaw status --approve` / `--reject --comment "..."`：确认当前待审事项
5. `xclaw stop`：终止任务

## 2. 内部角色

内部正式角色为：

- `Product Owner`
- `Architect`
- `Developer`
- `Tester`
- `Human Gate`
- `Orchestrator`

原则如下：

- `Product Owner` 是唯一正式路由 owner
- `Architect` 负责需求调研、方案设计和子任务拆解
- `Developer` 与 `Tester` 只负责当前子任务执行
- `Human Gate` 是内部“人类确认”阶段名
- 对用户不暴露多角色细节，用户只看到进度和是否需要自己动作

## 3. 用户交互模型

### 3.1 命令面

用户可见命令只保留：

- `xclaw start`
- `xclaw status`
- `xclaw stop`

其中 `status` 同时承担“查看”和“监督输入”两个职责。

### 3.2 建议（advice）

中途建议采用非阻塞模型：

- 用户可以在任务运行期间随时提交建议
- 建议通过 `xclaw status --advise "..."` 追加到 `human_advice_log`
- 建议不会立即打断当前执行
- `Product Owner` 只会在正式路由边界统一吸收 pending advice

### 3.3 人类确认（review）

人工确认使用单一 `human_gate` 与单一 `waiting_approval`，通过 `review_kind` 区分两类确认：

- `plan`
  - 只在当前 `plan.md` 明确存在待人类确认事项时触发
  - 绑定当前 `plan_revision`
  - 用户确认的是业务/产品层面的计划问题，而不是所有内部计划微调
- `delivery`
  - 在交付完成后始终触发
  - 用户确认的是最终交付是否可接受

规则：

- `approve` 与 `reject` 都会把任务送回 `product_owner_dispatch`
- `plan` 审阅通过后进入执行阶段
- `delivery` 审阅通过后才允许 `closeout`

## 4. 状态与流程

### 4.1 Stage

内部正式阶段为：

```text
intake
-> product_owner_refinement
-> [architect_planning, optional]
-> product_owner_dispatch
-> [developer | tester | human_gate | closeout]
```

固定回流规则：

- `architect_planning` -> `product_owner_dispatch`
- `developer` -> `product_owner_dispatch`
- `tester` -> `product_owner_dispatch`
- `human_gate` 在收到人工审阅结果后 -> `product_owner_dispatch`
- `closeout` -> `completed`

典型路径：

```text
intake
-> product_owner_refinement
-> architect_planning
-> product_owner_dispatch
-> [human_gate(plan), optional]
-> product_owner_dispatch
-> developer
-> product_owner_dispatch
-> tester
-> product_owner_dispatch
-> ...（按子任务循环）
-> human_gate(delivery)
-> product_owner_dispatch
-> closeout
```

### 4.2 TaskStatus

正式任务状态为：

- `running`
- `waiting_approval`
- `completed`
- `failed`
- `terminated`

### 4.3 Route Decision

`Product Owner` 的正式路由工件为 `route_decision`，字段为：

- `- next_stage: <stage>|-`
- `- task_status: running|terminated`
- `- based_on_artifacts: <comma-separated artifact types>|-`
- `- human_advice_disposition: accepted|partially_accepted|rejected|none`
- `- review_kind_requested: plan|delivery|-`

约束：

- `task_status: running` 时，`next_stage` 必须是当前 PO 边界允许的合法后继
- `task_status: terminated` 时，`next_stage` 必须是 `-`
- `next_stage` 不是 `human_gate` 时，`review_kind_requested` 必须是 `-`
- 如果存在 pending human advice，`human_advice_disposition` 不能是 `none`
- 如果不存在 pending human advice，`human_advice_disposition` 必须是 `none`

## 5. 正式工件

### 5.1 主流程工件

主流程工件包括：

- `plan`
- `dev_handoff`
- `test_handoff`
- `implementation_result`
- `test_report`
- `repair_ticket`
- `route_decision`
- `closeout`

### 5.2 `plan.md` 契约

`plan.md` 是唯一正式计划工件，至少要包含：

- `- plan_revision: ...`
- `- human_confirmation_required: yes|no`
- `- human_confirmation_items: item1 | item2 | ...` 或 `-`
- `- active_subtask_id: ...` 或 `-`

并清楚描述：

- 任务目标
- 范围与非目标
- 关键调研结论
- 架构决策
- 风险与假设
- 全局测试策略
- 子任务列表

每个子任务至少包含：

- `subtask_id`
- 目标
- 范围
- 依赖
- 开发要求
- 完成标志
- 测试检查点
- 状态

### 5.3 监督工件

正式监督工件包括：

- `progress`
  - 对用户可读的进度摘要
  - 包含固定 `Summary` bullets：`latest_update`、`current_focus`、`next_step`、`risks`、`needs_human_review`、`user_summary`
  - 包含 append-only `Timeline`
- `human_advice_log`
  - 中途建议队列
- `review_request`
  - `Product Owner` 请求人工确认时发布
  - 包含 `review_kind`，若为 `plan` 审阅还包含 `plan_revision`
- `review_decision`
  - 用户 approve/reject 后发布
  - 包含 `review_kind`，若为 `plan` 审阅还包含 `plan_revision`

## 6. 关键规则

### 6.1 计划确认规则

- `plan` 的内部性修改默认不需要人类重新介入
- 以下修改默认不要求重新确认：
  - 实现细节微调
  - 子任务进一步细分或顺序调整
  - 测试执行策略调整
  - 风险描述补充
- 只有当计划涉及产品目标、范围、验收标准、关键用户行为、显著成本/风险取舍或外部依赖决策变化时，才需要把 `human_confirmation_required` 设为 `yes`
- 若当前 `plan` 需要人类确认，但尚未拿到该 `plan_revision` 的正式 `approved`，不得进入 `developer` 或 `tester`

### 6.2 执行推进规则

- `Developer` 和 `Tester` 只围绕当前 `active_subtask_id` 工作
- `dev_handoff` 与 `test_handoff` 只能从当前 `plan` 派生
- 不允许 handoff 引入 `plan` 之外的新正式范围
- `repair_ticket` 可用于把测试回流压缩成可执行修复项

### 6.3 交付验收规则

- 交付完成后必须进入 `human_gate(delivery)`
- 只有 `delivery` 被正式 `approved` 后，`Product Owner` 才能路由到 `closeout`

## 7. Gateway 行为

Gateway 负责：

- 在 worker 启动后确保监督工件存在
- 按合法路由推进阶段
- 维护 `progress`
- 接受 CLI 提交的监督动作结果
- 在 `waiting_approval` 时暂停自动推进
- 执行修复回路上限和终态保护

## 8. CLI 监督视图

`xclaw status` 默认输出以下监督字段：

- `active_task_id`
- `task_status`
- `user_summary`
- `latest_update`
- `next_step`
- `needs_human_review`
- `review_kind`（仅在等待人工确认时显示）
- `plan_confirmation_summary`（仅在等待 `plan` 审阅时显示）
- `risks`（仅在存在明确风险时显示）
- `pending_advice_count`（仅在大于 0 时显示）
- `latest_review_request_id`（仅在等待人工确认时显示）

其中：

- `user_summary` 优先使用 `Product Owner` 写入的面向用户自然语言总结
- 人类只在真正需要产品/业务判断时看到 `plan` 审阅
- 所有交付都需要最终 `delivery` 验收
