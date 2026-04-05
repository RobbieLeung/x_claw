# xclaw 极简人类监督设计

## 1. 目标

`xclaw` 是一个面向单目标仓库、单活跃任务的本地 gateway service。

这一版设计的目标很明确：

- 用户只做五件事：提交任务、看进度、提建议、做验收、必要时终止任务
- 系统继续保留内部多角色协作能力
- 人类不再参与细粒度状态机，只负责监督和最终审阅
- 正式接口、正式状态和正式工件全部围绕这个模型收敛

用户心智模型只有：

1. `xclaw start`：提交任务
2. `xclaw status`：看进度
3. `xclaw status --advise "..."`：中途提建议
4. `xclaw status --approve` / `--reject --comment "..."`：人工验收
5. `xclaw stop`：终止任务

## 2. 内部角色

内部仍保留以下角色：

- `Product Owner`
- `Project Manager`
- `Developer`
- `Tester`
- `QA`
- `Human Gate`
- `Orchestrator`

原则如下：

- `Product Owner` 仍是唯一正式路由 owner
- `Project Manager / Developer / Tester / QA` 只负责专业执行，不拥有流程路由权
- `Human Gate` 是内部“人工验收”阶段名
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
- `Product Owner` 只会在正式路由边界统一吸收 pending 建议
- 一次 `Product Owner` 路由会批量消费当前所有 pending 建议

### 3.3 人工验收（review）

人工验收采用显式请求模型：

- 只有当 `Product Owner` 把流程路由到 `human_gate` 时，任务才进入 `waiting_approval`
- 进入该状态后，用户通过 `xclaw status --approve` 或 `xclaw status --reject --comment "..."` 完成审阅
- `approve` 后，任务回到 `product_owner_dispatch` 继续推进
- `reject` 后，任务同样回到 `product_owner_dispatch`，由 `Product Owner` 重整方案并可再次请求验收
- 因此人工验收允许多轮往返，不是一次性终局按钮

## 4. 状态与流程

### 4.1 Stage

内部正式阶段为：

```text
intake
-> product_owner_refinement
-> [project_manager_research, optional]
-> product_owner_dispatch
-> [developer | tester | qa | human_gate | closeout]
```

固定回流规则：

- `project_manager_research` -> `product_owner_dispatch`
- `developer` -> `product_owner_dispatch`
- `tester` -> `product_owner_dispatch`
- `qa` -> `product_owner_dispatch`
- `human_gate` 在收到人工审阅结果后 -> `product_owner_dispatch`
- `closeout` -> `completed`

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

约束：

- `task_status: running` 时，`next_stage` 必须是当前 PO 边界允许的合法后继
- `task_status: terminated` 时，`next_stage` 必须是 `-`
- 如果存在 pending human advice，`human_advice_disposition` 不能是 `none`
- 如果不存在 pending human advice，`human_advice_disposition` 必须是 `none`

## 5. 正式工件

### 5.1 主流程工件

主流程工件包括：

- `requirement_spec`
- `execution_plan`
- `research_brief`
- `dev_handoff`
- `test_handoff`
- `implementation_result`
- `test_report`
- `qa_result`
- `repair_ticket`
- `route_decision`
- `closeout`

### 5.2 监督工件

正式监督工件包括：

- `progress`
  - 对用户可读的进度摘要
  - 包含固定 `Summary` bullets：`latest_update`、`current_focus`、`next_step`、`risks`、`needs_human_review`、`user_summary`
  - 包含 append-only `Timeline`
- `human_advice_log`
  - 中途建议队列
  - 每条 advice 带 `advice_id`、`submitted_at`、`status`、`source`、正文
- `review_request`
  - `Product Owner` 请求人工验收时发布
- `review_decision`
  - 用户 approve/reject 后发布

## 6. Gateway 行为

Gateway 负责：

- 在 worker 启动后确保监督工件存在
- 按合法路由推进阶段
- 维护 `progress`
- 接受 CLI 提交的监督动作结果
- 在 `waiting_approval` 时暂停自动推进
- 执行修复回路上限和终态保护

补充说明：

- 工作区初始化由 `xclaw start` 完成
- gateway worker 启动后会发布在线进度并开始自动推进

## 7. CLI 监督视图

`xclaw status` 默认输出以下精简监督字段：

- `active_task_id`
- `task_status`
- `user_summary`
- `latest_update`
- `next_step`
- `needs_human_review`
- `risks`（仅在存在明确风险时显示）
- `pending_advice_count`（仅在大于 0 时显示）
- `latest_review_request_id`（仅在等待人工验收时显示）

其中：

- `user_summary` 优先使用 `Product Owner` 写入的面向用户自然语言总结；缺省时由系统根据 `progress` 自动生成
- 其余用户可读监督信息来自 `progress`
- `task_workspace_path`、`worker_pid`、`current_stage`、`current_owner`、`active_step_id`、`current_focus`、`progress_path` 等详细信息不再默认显示，用户可直接查看任务工作区与运行日志
