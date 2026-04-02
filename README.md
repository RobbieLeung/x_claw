# x_claw

`x_claw` is a local gateway service for the **`xllm` project**, designed around a **single target repository** and **a single active task**.

The current positioning is important:

- It is primarily built for `xllm` project workflows
- It currently supports only the `codex` CLI as its agent execution backend
- In the current workflow, all code implementation work is executed by `codex`
- Users only submit tasks, inspect progress, provide advice, and review results
- The system still keeps internal roles such as `Product Owner`, `Project Manager`, `Developer`, `Tester`, and `QA`
- `Product Owner` is the only formal routing owner
- Only one active task is allowed at a time

For the full design rationale, see `docs/DESIGN.md`.

## Highlights

- Minimal command surface: only `start`, `status`, and `stop`
- Single-task workspace model: each task gets its own `task.md`, `event_log.md`, `current/`, `history/`, and `runs/`
- Supervision-oriented interaction: humans participate through `status --advise`, `--approve`, and `--reject`
- Artifact-driven execution: requirements, plans, handoffs, test results, QA, and review all flow through formal artifacts
- Local agent execution: internal roles run through the local `codex` CLI
- Observable progress: `progress.md` maintains a user-readable summary and append-only timeline

## Design Principles

This version follows a few explicit constraints:

- Single target repository
- Single active task
- No human involvement in the fine-grained runtime state machine
- `waiting_approval` is the only formal human waiting state
- `Product Owner` absorbs human advice and decides formal routing at route boundaries

The internal stage flow is:

```text
intake
-> product_owner_refinement
-> [project_manager_research, optional]
-> product_owner_dispatch
-> [developer | tester | qa | human_gate | closeout]
```

## Requirements

- Python `>=3.10`
- A locally available `codex` CLI
- No support yet for agent backends other than `codex` CLI
- Optional: `git`, used to collect target repository context

## Installation

From the project root:

```bash
pip install -e .
```

Install development dependencies:

```bash
pip install -e .[dev]
```

Then verify the CLI is available:

```bash
x_claw --version
```

## Quick Start

### 1. Start a task

```bash
x_claw start \
  --repo /path/to/target-repo \
  --task "Add a README and a minimal usage guide"
```

Optionally specify a task ID and workspace root:

```bash
x_claw start \
  --repo /path/to/target-repo \
  --task "Fix the login flow" \
  --task-id task-login-fix \
  --workspace-root ./workspace
```

The command prints:

- `task_id`
- `task_workspace_path`
- `worker_pid`
- `progress_path`

### 2. Check status

```bash
x_claw status
```

`status` prints a fixed supervision view:

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

If there is no active task, `status` returns an `idle` view.

### 3. Provide mid-task advice

```bash
x_claw status --advise "Narrow the scope and focus on the CLI only"
```

Notes:

- Advice is appended to `human_advice_log`
- It does not interrupt the current stage immediately
- `Product Owner` absorbs pending advice at formal routing boundaries

### 4. Review a human-gate request

Human review is needed only after the flow enters `human_gate`.

Approve:

```bash
x_claw status --approve --comment "Looks good, proceed to closeout"
```

Reject:

```bash
x_claw status --reject --comment "Test coverage is still missing failure cases"
```

Notes:

- Both `approve` and `reject` route the task back to `product_owner_dispatch`
- Human review can happen in multiple rounds

### 5. Stop the task

```bash
x_claw stop
```

This marks the current active task as `terminated` and attempts to stop the gateway worker.

## CLI Overview

### `x_claw start`

```bash
x_claw start --repo REPO --task TEXT [--task-id TASK_ID] [--workspace-root PATH]
```

### `x_claw status`

```bash
x_claw status [--workspace-root PATH]
x_claw status --advise TEXT [--workspace-root PATH]
x_claw status --approve [--comment TEXT] [--workspace-root PATH]
x_claw status --reject --comment TEXT [--workspace-root PATH]
```

### `x_claw stop`

```bash
x_claw stop [--workspace-root PATH]
```

### Internal command

`gateway-worker` is an internal subcommand and normally should not be called directly.

## Workspace Layout

By default, the workspace root is `workspace/` under the current command invocation directory.

Each task gets its own directory:

```text
workspace/
  <task_id>/
    task.md
    event_log.md
    gateway.log
    current/
    history/
    runs/
```

Where:

- `task.md`: the main task record, including runtime state and current artifact pointers
- `event_log.md`: the event log
- `current/`: the currently active formal artifacts
- `history/`: archived artifact versions
- `runs/`: per-invocation prompt, response, and run log files

## Formal Artifacts

### Main flow artifacts

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

### Supervision artifacts

- `progress`
- `human_advice_log`
- `review_request`
- `review_decision`

## Task Status Model

Formal task statuses are:

- `running`
- `waiting_approval`
- `completed`
- `failed`
- `terminated`

Meaning:

- `running`: the gateway is actively advancing the workflow
- `waiting_approval`: the workflow is waiting for human review
- `completed`: the task has finished successfully
- `failed`: a stage execution failed
- `terminated`: the task was explicitly stopped

## Role Prompts and Execution

Role prompts are stored in:

- `src/x_claw/agents/product_owner.md`
- `src/x_claw/agents/project_manager.md`
- `src/x_claw/agents/developer.md`
- `src/x_claw/agents/tester.md`
- `src/x_claw/agents/qa.md`

At runtime, `x_claw` combines:

- the role prompt
- the current stage objective
- `task.md`
- the artifacts required for the stage

into a single agent invocation executed through the local `codex` CLI.

## Development and Testing

Run the test suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test*.py'
```

If `pytest` is installed locally, you can also run:

```bash
python -m pytest
```

## Current Limitations

- It is currently aimed at the `xllm` project, not yet a fully generalized multi-repo orchestration framework
- Only one active task is supported at a time
- It is single-repository only
- It currently supports only the local `codex` CLI and no other agent backend
- In the current workflow, code implementation is executed by `codex`
- `gateway-worker` is a local background process, not a remote service

## References

- Design document: `docs/DESIGN.md`
- CLI entrypoint: `src/x_claw/cli.py`
- Gateway loop: `src/x_claw/gateway.py`
- Stage executor: `src/x_claw/executor.py`
