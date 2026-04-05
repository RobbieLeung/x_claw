# xclaw 使用文档

## 1. 你只需要做什么

`xclaw` 的用户动作非常少：

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
- 用户通过命令行查看进度、提交建议和完成验收

## 4. 看进度

查看当前活跃任务：

```bash
xclaw status
```

如果用了自定义工作区：

```bash
xclaw status --workspace-root /abs/path/to/workspace_root
```

默认输出包含：

- `active_task_id`
- `task_status`
- `user_summary`
- `latest_update`
- `next_step`
- `needs_human_review`
- `risks`（仅在存在明确风险时显示）
- `pending_advice_count`（仅在大于 0 时显示）
- `latest_review_request_id`（仅在等待人工验收时显示）

无活跃任务时会显示 `idle`。

补充说明：

- `user_summary` 优先显示 `Product Owner` 写入的面向用户总结；缺省时由系统根据当前 `progress` 自动生成
- 更详细的内部字段与路径信息默认不展示；需要时直接查看 `gateway.log`、`runs/` 和 `current/*.md`

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

常见 `current/*.md` 包括：

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

其中监督相关工件含义为：

- `progress.md`：固定 `Summary` 字段加 append-only `Timeline`
- `human_advice_log.md`：每条建议包含 `advice_id`、`submitted_at`、`status`、`source` 和正文
- `review_request.md`：系统进入人工验收时生成的正式审阅请求
- `review_decision.md`：用户 approve/reject 后生成的正式审阅结论

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

### 10.1 我提的建议什么时候会生效？

建议不会立即打断当前角色执行。

系统会在下一个 `Product Owner` 正式路由边界统一吸收 pending advice。

### 10.2 `reject` 之后任务会结束吗？

不会。

`reject` 的含义是“当前方案不接受，请继续整改”。
任务会回到 `Product Owner`，后续允许再次发起人工验收。
