# xclaw 使用文档

## 1. 你只需要做什么

这一版 `xclaw` 的用户动作非常少：

- 提交任务
- 看进度
- 中途提建议
- 最后做验收
- 必要时终止任务

对应命令只有：

- `xclaw start`
- `xclaw status`
- `xclaw stop`

其中 `status` 既是进度查看入口，也是监督入口。

## 2. 前置条件

开始前请确认：

- Python `>= 3.10`（当前实现目标版本）
- 本地可执行 `codex` CLI
- `codex exec` 已完成认证

安装：

```bash
python -m pip install -e .
```

如果需要开发依赖：

```bash
python -m pip install -e ".[dev]"
```

检查版本：

```bash
xclaw --version
```

## 3. 提交任务

```bash
xclaw start \
  --repo /abs/path/to/target_repo \
  --task "实现 xxx，并补充必要测试"
```

可选参数：

- `--task-id`
- `--workspace-root`

启动成功后会输出：

```text
task_id: task-20260401-101500-abcd1234
task_workspace_path: /abs/path/to/workspace/task-20260401-101500-abcd1234
worker_pid: 12345
progress_path: /abs/path/to/workspace/task-20260401-101500-abcd1234/current/progress.md
```

说明：

- `xclaw` 在同一 `workspace_root` 下只允许一个活跃任务
- 启动后 gateway 会自动推进内部流程
- 用户不需要再进入 attach 或手工编辑交互文件

## 4. 看进度

查看当前活跃任务：

```bash
xclaw status
```

如果用了自定义工作区：

```bash
xclaw status --workspace-root /abs/path/to/workspace_root
```

输出固定包含：

- `active_task_id`
- `task_workspace_path`
- `worker_pid`
- `task_status`
- `current_stage`
- `current_owner`
- `active_step_id`
- `latest_update`
- `current_focus`
- `next_step`
- `risks`
- `pending_advice_count`
- `needs_human_review`
- `latest_review_request_id`
- `progress_path`

无活跃任务时会显示 `idle`。

## 5. 中途提建议

如果你认为 AI 当前方案有问题，直接提交建议：

```bash
xclaw status --advise "请不要扩大范围，先把日志链路补齐。"
```

特点：

- 建议不会立即打断当前执行
- 建议会进入 `human_advice_log.md`
- `Product Owner` 会在下一个正式路由边界统一吸收 pending 建议
- 你可以继续通过 `xclaw status` 观察建议是否已被吸收

限制：

- 当任务已经进入人工验收阶段（`waiting_approval`）时，不接受 `--advise`
- 这时请直接使用 `--approve` 或 `--reject`

## 6. 最终验收

当 `xclaw status` 输出：

```text
needs_human_review: yes
```

说明当前正在等待你做人工验收。

### 6.1 通过

```bash
xclaw status --approve --comment "方案可接受，继续推进。"
```

`--comment` 对 `approve` 是可选的，不写时会使用默认说明。

### 6.2 驳回

```bash
xclaw status --reject --comment "回滚这个扩 scope，先只修核心链路。"
```

注意：

- `--reject` 必须带 `--comment`
- `reject` 不会直接把任务终止
- 任务会回到 `Product Owner`，系统继续重整方案，后续可再次请求你验收

## 7. 停止任务

```bash
xclaw stop
```

作用：

- 将任务状态改为 `terminated`
- 清空 `gateway_pid`
- 记录停止事件和 recovery note
- 向后台 worker 发送终止信号

## 8. 工作区结构

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

这一版常见 `current/*.md` 包括：

- `requirement_spec.md`
- `execution_plan.md`
- `research_brief.md`
- `dev_handoff.md`
- `test_handoff.md`
- `implementation_result.md`
- `test_report.md`
- `qa_result.md`
- `repair_ticket.md`
- `route_decision.md`
- `review_request.md`
- `review_decision.md`
- `progress.md`
- `human_advice_log.md`
- `closeout.md`

不再存在：

- `conversation.md`
- `human_input.md`
- `human_feedback.md`
- `approval.md`

## 9. 排障

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

## 10. 常见问题

### 10.1 为什么没有 `attach` 了？

这一版已经删除 `attach` 和交互模式概念。

用户统一通过：

- `xclaw status`
- `xclaw status --advise ...`
- `xclaw status --approve`
- `xclaw status --reject --comment ...`

完成监督。

### 10.2 为什么没有 `--interaction` 了？

旧的 `interactive` / `markdown` 模式已经废弃。

现在只有一种正式用户接口：命令式监督。

### 10.3 我提的建议什么时候会生效？

建议不会立即打断当前角色执行。

系统会在下一个 `Product Owner` 正式路由边界统一吸收 pending advice。

### 10.4 `reject` 之后任务会结束吗？

不会。

`reject` 的含义是“当前方案不接受，请继续整改”。
任务会回到 `Product Owner`，后续允许再次发起人工验收。
