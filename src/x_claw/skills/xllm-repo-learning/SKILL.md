---
name: xllm-repo-learning
description: Use when the user asks to understand, navigate, explain, or onboard into the xLLM repository, including startup flow, service/request flow, scheduler, batch, engine/worker runtime, KV cache, prefix cache, PD disaggregation, schedule overlap, graph mode, xTensor, parallel_state, or EPLB.
---

# xLLM Repo Learning

Use this skill when the task is to read or explain the xLLM codebase rather than make a narrowly scoped code edit with no architecture context.

## Quick Start

Treat xLLM as three main lines plus two boundaries:

- Startup line:
  `xllm/xllm.cpp -> xllm::Options -> Master -> Engine -> APIService -> XllmServer`
- Request line:
  `APIService -> *ServiceImpl -> LLMMaster::handle_request -> Scheduler -> Batch`
- Execution line:
  `Batch -> LLMEngine -> WorkerClient -> Worker -> WorkerImpl -> Executor -> CausalLM/Model`
- Config boundary:
  `CLI flags -> xllm::Options -> runtime::Options`
- Control vs execution boundary:
  `Master + Scheduler` vs `Engine + Worker`

Default baseline scenario for reasoning:

- single-node text generation
- OpenAI-style chat/completion request
- LLM backend

If the user asks a general “how does xLLM work” question, start with:

1. [references/0-overview.md](references/0-overview.md)
2. [references/1-structure.md](references/1-structure.md)

Do not load every reference file up front. Read only the topic files needed for the current question.

## Topic Map

- Startup, config, module map:
  [references/0-overview.md](references/0-overview.md)
- End-to-end architecture and main call chains:
  [references/1-structure.md](references/1-structure.md)
- Prefix cache:
  [references/2-prefix_cache.md](references/2-prefix_cache.md)
- Schedule overlap:
  [references/3-schedule_overlap.md](references/3-schedule_overlap.md)
- PD disaggregation:
  [references/4-pd_disaggregate.md](references/4-pd_disaggregate.md)
- Batch:
  [references/5-batch.md](references/5-batch.md)
- EPLB:
  [references/6-eplb.md](references/6-eplb.md)
- Graph mode:
  [references/7-graph.md](references/7-graph.md)
- KV cache:
  [references/8-kv_cache.md](references/8-kv_cache.md)
- Parallel state:
  [references/9-parallel_state.md](references/9-parallel_state.md)
- Request / Sequence / SequencesGroup:
  [references/10-request.md](references/10-request.md)
- Scheduler family:
  [references/11-scheduler.md](references/11-scheduler.md)
- xTensor:
  [references/12-xtensor.md](references/12-xtensor.md)

## Recommended Workflow

1. Identify whether the user is asking about startup, request admission, scheduling, execution, data objects, or platform optimization.
2. Read `0-overview` and `1-structure` first unless the task is already tightly scoped.
3. For a scoped question, load only the matching topic reference from the map above.
4. Verify key claims against current source code before stating them as facts, especially for flags, call order, defaults, and backend-specific behavior.
5. Answer with both:
   - the role of the module in the main flow
   - the exact code path or key classes/functions that implement it

## Fast Navigation

Prefer `rg` to jump into code:

- startup:
  `rg -n "int main|run\\(" xllm/xllm.cpp xllm/core/common`
- service entry:
  `rg -n "APIService|process_async|handle_request" xllm/api_service xllm/server xllm/core/distributed_runtime`
- scheduler:
  `rg -n "class .*Scheduler|prepare_batch|step\\(" xllm/core/scheduler`
- request/batch:
  `rg -n "class Request|class Sequence|class Batch|process_sample_output" xllm/core/framework`
- engine/worker:
  `rg -n "class LLMEngine|class WorkerImpl|step_internal|forward\\(" xllm/core/distributed_runtime xllm/core/runtime`
- KV/prefix:
  `rg -n "PrefixCache|KVCache|BlockManager|transfer_kv_info" xllm/core/framework xllm/core/runtime`
- graph/xTensor:
  `rg -n "enable_graph|GraphExecutor|XTensor|PhyPagePool|PageAllocator" xllm/core`

## Working Rules

- Always explain xLLM from the main chain first, then expand to variants.
- Distinguish clearly between:
  - framework-common logic
  - backend/model-specific logic
  - device/platform-specific optimization
- When notes and code differ, trust the code and call out the discrepancy explicitly.
- For advanced topics like PD, graph, xTensor, or EPLB, first state where they hook into the base flow before describing their own internals.
