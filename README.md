# xclaw

`xclaw` is a local gateway service for the **`xllm` project**, designed around a **single target repository** and **a single active task**.

## Positioning

- Single target repository
- Single active task
- Local `codex` CLI only
- Human supervision through `status --advise`, `--approve`, and `--reject`
- Internal roles are `Product Owner`, `Architect`, `Developer`, `Tester`, `Human Gate`, and `Orchestrator`
- `Product Owner` is the only formal routing owner

For the full design rationale, see [docs/DESIGN.md](docs/DESIGN.md).

## Highlights

- Minimal command surface: only `start`, `status`, and `stop`
- Plan-first execution: the workflow revolves around a single formal `plan.md`
- Approval on demand: plan confirmation is requested only when the current plan has human decision points
- Mandatory final delivery review before `closeout`
- Workspace-first observability: `task.md`, `event_log.md`, `current/`, `history/`, and `runs/`

## Internal Flow

```text
intake
-> product_owner_refinement
-> [architect_planning, optional]
-> product_owner_dispatch
-> [developer | tester | human_gate | closeout]
```

Human review uses one `waiting_approval` state, and `review_kind` distinguishes:

- `plan`: confirm pending business/product decisions in the current `plan_revision`
- `delivery`: final human acceptance of the delivery

## Quick Start

### Start a task

```bash
xclaw start \
  --repo /path/to/target-repo \
  --task "Implement xxx and add the necessary tests"
```

Or bootstrap directly from an existing plan:

```bash
xclaw start \
  --repo /path/to/target-repo \
  --plan ./plan.md
```

`--plan` is treated as bootstrap context only. The formal `current/plan.md` is still produced by Product Owner after startup.

### Check status

```bash
xclaw status
```

Default status output includes:

- `active_task_id`
- `task_status`
- `user_summary`
- `latest_update`
- `next_step`
- `needs_human_review`
- `review_kind` while waiting for approval
- `plan_confirmation_summary` while waiting on a plan review
- `risks` when present
- `pending_advice_count` when present
- `latest_review_request_id` while waiting for approval

### Provide advice

```bash
xclaw status --advise "Keep the scope narrow and focus on the CLI path first"
```

Advice is appended to `human_advice_log` and absorbed by `Product Owner` at the next formal routing boundary.

### Approve or reject a review

```bash
xclaw status --approve --comment "Looks good"
xclaw status --reject --comment "The current plan changes product scope"
```

Both actions route the task back to `product_owner_dispatch`.

### Stop the active task

```bash
xclaw stop
```

## Formal Artifacts

### Main flow

- `plan`
- `dev_handoff`
- `test_handoff`
- `implementation_result`
- `test_report`
- `repair_ticket`
- `route_decision`
- `closeout`

### Supervision

- `progress`
- `human_advice_log`
- `review_request`
- `review_decision`

`plan.md` is the only formal planning artifact. It carries:

- `plan_revision`
- `human_confirmation_required`
- `human_confirmation_items`
- `active_subtask_id`
- task goals, scope, architecture decisions, test strategy, and subtask breakdown

## Role Prompts

Bundled prompt assets live under:

- `src/xclaw/agents/product_owner.md`
- `src/xclaw/agents/architect.md`
- `src/xclaw/agents/developer.md`
- `src/xclaw/agents/tester.md`

## Development

Run the test suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test*.py'
```

## References

- Design document: `docs/DESIGN.md`
- CLI entrypoint: `src/xclaw/cli.py`
- Gateway loop: `src/xclaw/gateway.py`
- Stage executor: `src/xclaw/executor.py`
