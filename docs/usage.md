# xclaw 使用文档

## 1. 你只需要做什么

`xclaw` 的用户动作很少：

- 提交任务
- 看进度
- 中途提建议
- 按需确认计划
- 最终确认交付
- 必要时终止任务
- 必要时恢复最近一次任务

对应命令只有：

- `xclaw start`
- `xclaw status`
- `xclaw stop`
- `xclaw resume`

## 2. 前置条件

- Python `>= 3.10`
- 本地可执行 `codex` CLI
- `codex exec` 已完成认证

安装：

```bash
python -m pip install -e .
```

开发依赖：

```bash
python -m pip install -e ".[dev]"
```

## 3. 提交任务

```bash
xclaw start \
  --repo /abs/path/to/target_repo \
  --task "实现 xxx，并补充必要测试"
```

或者直接从已有计划启动：

```bash
xclaw start \
  --repo /abs/path/to/target_repo \
  --plan ./plan.md
```

也可以同时显式指定任务描述：

```bash
xclaw start \
  --repo /abs/path/to/target_repo \
  --plan ./plan.md \
  --task "实现 xxx，并补充必要测试"
```

可选参数：

- `--plan`
- `--task-id`
- `--workspace-root`

说明：

- `--task` 和 `--plan` 至少提供一个
- `--task` 与 `--plan` 同时提供时，`--task` 作为显式任务描述
- 仅提供 `--plan` 时，系统会优先取第一个一级标题作为任务描述；没有一级标题时退化为首个非空文本
- 外部 `--plan` 只作为启动参考输入，不会直接成为正式 `current/plan.md`
- 使用 `--plan` 启动时，任务会直接从 `product_owner_dispatch` 开始，由 `Product Owner` 先整理出第一版正式 `plan.md`
- 在第一版正式 `plan.md` 产出前，请不要删除原始 `--plan` 文件；否则首次派发会失败

启动成功后会输出：

- `task_id`
- `task_workspace_path`
- `worker_pid`
- `progress_path`

## 4. 看进度

```bash
xclaw status
```

默认输出包含：

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

说明：

- `review_kind: plan` 表示当前在确认某个 `plan_revision` 的待确认事项
- `review_kind: delivery` 表示当前在做最终交付验收
- 更详细的内部信息默认不展示；需要时直接查看 `gateway.log`、`runs/` 和 `current/*.md`

## 5. 中途提建议

```bash
xclaw status --advise "请不要扩大范围，先把核心链路打通。"
```

特点：

- 建议进入 `human_advice_log.md`
- 不会立即打断当前阶段
- `Product Owner` 会在下一个正式路由边界统一吸收 pending advice

限制：

- 当任务处于 `waiting_approval` 时，不接受 `--advise`
- 这时请直接使用 `--approve` 或 `--reject`

## 6. 计划确认与交付验收

当 `xclaw status` 输出：

```text
needs_human_review: yes
```

说明当前正在等待你做正式确认。

### 6.1 通过

```bash
xclaw status --approve --comment "可以接受，进入下一步。"
```

### 6.2 驳回

```bash
xclaw status --reject --comment "当前计划改动了产品范围，先收紧。"
```

说明：

- `--reject` 必须带 `--comment`
- `reject` 不会直接终止任务
- 任务会回到 `Product Owner`
- 如果当前是 `plan` 审阅，系统会重整 `plan.md`
- 如果当前是 `delivery` 审阅，系统会整改后再次发起交付验收

## 7. 恢复最近任务

```bash
xclaw resume
```

作用：

- 仅在当前没有活跃 task 时可用
- 选择 `workspace/` 下最新的一个 task workspace（默认生成的 `task_id` 自带时间序）
- 把任务恢复到最近的 `Product Owner` 边界（通常是 `product_owner_dispatch`）
- 重新拉起 `gateway-worker`，让 `Product Owner` 先修复当前 workspace 再继续跑 pipeline

典型场景：

- 上一轮卡在 `failed` 或被手工 `terminated`
- 需要保留 `current/` 和 `runs/` 现场继续推进

## 8. 停止任务

```bash
xclaw stop
```

作用：

- 将任务状态改为 `terminated`
- 清空 `gateway_pid`
- 记录停止事件和 recovery note
- 向后台 worker 发送终止信号

## 9. 工作区结构

典型任务目录如下：

```text
workspace/<task_id>/
  task.md
  event_log.md
  gateway.log
  current/
  history/
  runs/
```

常见 `current/*.md` 包括：

- `plan.md`
- `dev_handoff.md`
- `test_handoff.md`
- `implementation_result.md`
- `test_report.md`
- `repair_ticket.md`
- `route_decision.md`
- `review_request.md`
- `review_decision.md`
- `progress.md`
- `human_advice_log.md`
- `closeout.md`

其中：

- `plan.md` 是唯一正式计划工件
- `task.md` 的 `bootstrap_plan_source_path` 会记录启动时传入的外部 `--plan` 路径（若存在）
- `review_request.md` / `review_decision.md` 会记录 `review_kind`
- `plan` 审阅还会记录对应的 `plan_revision`

## 10. 排障

优先查看：

```text
<task_workspace>/gateway.log
<task_workspace>/runs/<seq>_<role>/prompt.md
<task_workspace>/runs/<seq>_<role>/response.md
<task_workspace>/runs/<seq>_<role>/run.log
```

如果是用户监督相关问题，再看：

```text
<task_workspace>/current/progress.md
<task_workspace>/current/human_advice_log.md
<task_workspace>/current/review_request.md
<task_workspace>/current/review_decision.md
```

## 11. 常见问题

### 10.1 我提的建议什么时候会生效？

建议不会立即打断当前角色执行。

系统会在下一个 `Product Owner` 正式路由边界统一吸收 pending advice。

### 10.2 为什么有时改了 `plan` 不会再次要求我确认？

因为 `plan` 的内部性修改默认不需要人类重审。

只有当当前计划涉及产品目标、范围、验收标准、关键用户行为、显著成本/风险取舍或外部依赖决策变化时，才会再次触发 `plan` 审阅。

### 10.3 `reject` 之后任务会结束吗？

不会。

`reject` 的含义是“当前待审内容不接受，需要重新整改”。
