# xLLM Scheduler 源码学习笔记

## 1. 这份笔记要回答什么

基于 `ralph/learn_xllm/0-overview.md` 和 `ralph/learn_xllm/1-structure.md` 已经建立的整体框架，我这次专门把 xLLM 的 `scheduler` 单独拆出来学习。你在问题里写的是 `overview.md` 和 `structure.md`，但当前仓库里实际对应的文件名是：

- `ralph/learn_xllm/0-overview.md`
- `ralph/learn_xllm/1-structure.md`

这份笔记重点回答下面这些问题：

1. xLLM 里到底有哪些不同的 scheduler，它们分别服务哪条主链。
2. `LLMMaster` / `RecMaster` / `DiTMaster` 分别是如何创建 scheduler 的。
3. `ContinuousScheduler` 的核心逻辑到底是什么：请求从哪里进来，waiting/running/pending 在代码里如何表示，batch 是怎么组出来的。
4. token budget、seq budget、KV block、latency budget、preemption 是怎样共同参与调度的。
5. `ChunkedPrefillScheduler`、`PrefillOnlyScheduler`、`MixScheduler`、`ZeroEvictionScheduler`、`DisaggPDScheduler`、`PDOOCScheduler` 和基础 `ContinuousScheduler` 的差异分别是什么。
6. `FixedStepsScheduler` 和 `DiTScheduler` 为什么属于“另一类 scheduler”，它们和 LLM 主线的 continuous batching 有什么本质不同。
7. 当前代码里有哪些设计边界、实现优先级和容易误解的点。

---

## 2. 结论先行

先给出我当前最核心的结论：

1. xLLM 的 scheduler 不是一套实现，而是至少三大类：
   - **LLM/VLM 主线的 continuous family**：`ContinuousScheduler` 及其多个变体。
   - **Rec 主线的 fixed-step family**：`FixedStepsScheduler`。
   - **DiT 主线的 request-batch family**：`DiTDynamicBatchScheduler`。

2. 对 LLM 主线来说，真正的“骨架调度器”是 `ContinuousScheduler`。它维护：
   - 新请求入口队列：`request_queue_`
   - 首次等待调度队列：`waiting_priority_queue_` / `waiting_priority_queue_offline_`
   - 已持有 KV、可继续 decode 的队列：`running_queue_` / `running_queue_offline_`
   - 当前 step 真正要执行的集合：`running_requests_` / `running_sequences_` / `running_sequences_budgets_`

3. 用户口语里说的 “pending / waiting / running” 三类请求，在代码里并不是三个等价容器：
   - `pending_requests_` **只是一个原子计数器**，表示仍在请求构造线程池里、尚未完全入调度器的请求数。
   - `waiting_priority_queue_*` 才是“等待 prefill / 等待重新进入”的实际队列。
   - `running_queue_*` 才是“已经持有 KV、后续可以 decode”的实际队列。

4. `ContinuousScheduler` 的调度粒度不是纯 request，也不是纯 sequence，而是三层混合：
   - 队列管理主要是 **Request 级**；
   - token budget / seq budget 的实际分配是 **Sequence 级**；
   - beam search 时又必须把 **SequencesGroup 级** 语义带进 `Batch`。

5. `ContinuousScheduler` 的基础策略是：
   - **prefill 优先于 decode**：只有本轮没有挑出任何 prefill sequence 时，才会进入 decode 调度。
   - **在线优先于离线**：在线 prefill / decode 都有机会抢占 offline decode。
   - **budget 与 block 双重约束**：不是有 token budget 就一定能进 batch，还必须拿得到 KV blocks。

6. `ChunkedPrefillScheduler` 不是简单“把长 prompt 切块”，而是明确采用了注释里写的 **decode-maximal batching**：
   - 先尽量把 decode / 已在 running_queue 中的请求排进去；
   - 再用剩余 budget 去塞 chunked prefill；
   - 最后如果还有余量，再给已经处于 prefill 阶段的 sequence 继续追加 chunk。

7. `PrefillOnlyScheduler` 虽然工厂触发条件和 chunked prefill 有关，但它的策略明显更偏 **prefill-first**：
   - 它会优先继续完成上一步未跑完的 prefill request；
   - 只有本轮没有 prefill sequence 被选中时，才会真正去调 decode。
   - 结合工厂条件看，它主要用于 `enable_prefill_sp` 或 speculative decode 场景。

8. `MixScheduler` 是一条更激进的统一队列路线：
   - 它不再把 prefill / decode 分成 waiting + running 两个世界，而是把二者混进一个 `std::list`；
   - 它会给请求打 `URGENT / NORMAL / STARVED` 紧迫度，再结合 `priority_strategy` 做排序；
   - 它明显是面向多优先级 / ProSched 这类更复杂策略的。

9. `ZeroEvictionScheduler` 的目标不是提升首 token，而是尽量避免“已经进入 decode 的请求被驱逐后又重新 prefill”。它通过 `BlockCapacityGuard` 做一个“未来多个 decode step 的块消耗模拟”，只有确认不会触发驱逐时才接纳新的 prefill request。

10. PD 家族并不是“换个 batch 规则”这么简单，而是把 scheduler 变成了 **跨实例协议参与者**：
    - `DisaggPDScheduler` 让 Prefill 节点先把请求元数据发给 Decode 节点预留资源，再本地 prefill，最后把首 token 和 KV 迁过去。
    - `PDOOCScheduler` 在此基础上再加入了 **online/offline co-location**、decode 侧拉取 offline 请求、以及基于 roofline/perf model 的 decode SLO 控制。

11. `FixedStepsScheduler` 和 `DiTDynamicBatchScheduler` 都不属于 continuous batching 那条逻辑：
    - `FixedStepsScheduler` 更像“收集请求 -> 做一次固定步执行 -> 异步回收”，主要服务 Rec 场景。
    - `DiTDynamicBatchScheduler` 则是最简单的“按 request 个数凑 batch”调度，没有 KV cache、没有 preemption、没有 continuous decode。

12. scheduler 选择不只看 `Options`，还受 **global flags** 直接影响。尤其要注意：
    - `FLAGS_use_mix_scheduler`
    - `FLAGS_use_zero_evict`
    - `FLAGS_enable_prefill_sp`

   这几个标志并不完全通过 `ContinuousScheduler::Options` 传递，而是被 `scheduler_factory.cpp` 直接读取，所以“调度器选择”并非完全是 `Options` 的纯函数。

---

## 3. 阅读文件

这次为了把 scheduler 读透，我实际对过下面这些文件：

- `ralph/learn_xllm/0-overview.md`
- `ralph/learn_xllm/1-structure.md`
- `ralph/learn_xllm/3-schedule_overlap.md`
- `ralph/learn_xllm/4-pd_disaggregate.md`
- `ralph/learn_xllm/5-batch.md`
- `ralph/learn_xllm/request.md`
- `docs/zh/features/continuous_scheduler.md`
- `docs/zh/features/chunked_scheduler.md`
- `docs/zh/features/disagg_pd.md`
- `docs/zh/features/overview.md`
- `docs/zh/features/prefix_cache.md`
- `docs/en/features/zero_evict_scheduler.md`
- `xllm/core/scheduler/scheduler.h`
- `xllm/core/scheduler/scheduler_factory.h`
- `xllm/core/scheduler/scheduler_factory.cpp`
- `xllm/core/scheduler/continuous_scheduler.h`
- `xllm/core/scheduler/continuous_scheduler.cpp`
- `xllm/core/scheduler/chunked_prefill_scheduler.h`
- `xllm/core/scheduler/chunked_prefill_scheduler.cpp`
- `xllm/core/scheduler/prefill_only_scheduler.h`
- `xllm/core/scheduler/prefill_only_scheduler.cpp`
- `xllm/core/scheduler/mix_scheduler.h`
- `xllm/core/scheduler/mix_scheduler.cpp`
- `xllm/core/scheduler/zero_eviction_scheduler.h`
- `xllm/core/scheduler/zero_eviction_scheduler.cpp`
- `xllm/core/scheduler/disagg_pd_scheduler.h`
- `xllm/core/scheduler/disagg_pd_scheduler.cpp`
- `xllm/core/scheduler/pd_ooc_scheduler.h`
- `xllm/core/scheduler/pd_ooc_scheduler.cpp`
- `xllm/core/scheduler/fixed_steps_scheduler.h`
- `xllm/core/scheduler/fixed_steps_scheduler.cpp`
- `xllm/core/scheduler/dit_scheduler.h`
- `xllm/core/scheduler/dit_scheduler.cpp`
- `xllm/core/scheduler/decode_priority_queue.h`
- `xllm/core/scheduler/async_response_processor.h`
- `xllm/core/scheduler/profile/profile_manager.h`
- `xllm/core/scheduler/profile/profile_manager.cpp`
- `xllm/core/scheduler/profile/time_predictor.h`
- `xllm/core/scheduler/perf_model.h`
- `xllm/core/scheduler/perf_model.cpp`
- `xllm/core/distributed_runtime/llm_master.cpp`
- `xllm/core/distributed_runtime/rec_master.cpp`
- `xllm/core/distributed_runtime/dit_master.cpp`
- `xllm/core/framework/request/request.h`
- `xllm/core/framework/request/request.cpp`
- `xllm/core/framework/request/request_state.h`
- `xllm/core/framework/request/sequence.h`
- `xllm/core/framework/request/sequence.cpp`
- `xllm/core/framework/request/sequences_group.h`
- `xllm/core/framework/request/priority_comparator.h`
- `xllm/core/framework/request/priority_comparator.cpp`
- `xllm/core/framework/batch/batch.h`
- `xllm/core/framework/batch/batch_factory.h`
- `xllm/core/framework/batch/batch_factory.cpp`
- `xllm/core/scheduler/continuous_scheduler_test.cpp`
- `xllm/core/scheduler/chunked_prefill_scheduler_test.cpp`
- `xllm/core/scheduler/fixed_steps_scheduler_test.cpp`

---

## 4. scheduler 在整体架构里的位置

结合 `0-overview.md` 和 `1-structure.md`，scheduler 处在这样一条主线上：

```text
APIService / *ServiceImpl
  -> Master / LLMMaster / RecMaster / DiTMaster
  -> Scheduler::add_request(...)
  -> Scheduler::step(...)
  -> prepare_batch / BatchFactory::create_batches(...)
  -> Engine::step(...)
  -> Batch::process_*_output(...)
  -> AsyncResponseProcessor
  -> callback / stream output
```

这里有三个边界最重要：

1. **Master 与 Scheduler 的边界**
   - `Master` / `LLMMaster` 负责把协议层请求转成内部 `Request`。
   - scheduler 不管 HTTP/OpenAI/Chat 协议细节，它只接 `Request`。

2. **Scheduler 与 Engine 的边界**
   - scheduler 负责决定“这一轮谁执行、每条 sequence 给多少 token budget”。
   - engine 负责把这个 batch 真正下发给 worker / model 执行。

3. **Scheduler 与 Request/Sequence 的边界**
   - scheduler 按 **Request** 管理队列；
   - 按 **Sequence** 分配 token 和 KV 资源；
   - 通过 **Batch** 把决策落地成一次 step 的输入。

因此可以把 scheduler 理解为：

> xLLM 控制面的中心执行器，它不直接做模型 forward，但它决定哪一批 sequence 在这一轮 forward。

---

## 5. 调度器总分类

### 5.1 按任务主线分类

| 分类 | 代表类 | 主要服务对象 | 核心特点 |
| --- | --- | --- | --- |
| LLM continuous family | `ContinuousScheduler` 及其变体 | 文本生成主线 | continuous batching、KV 约束、preemption、chunked/PD 等都在这里扩展 |
| Rec fixed-step family | `FixedStepsScheduler` | Rec/OneRec/LlmRec | 不走 continuous decode，而是按固定步/固定轮次执行 |
| DiT request-batch family | `DiTDynamicBatchScheduler` | DiT 图像生成 | 按 request 数量动态成批，不涉及 KV cache / decode 队列 |

### 5.2 按继承关系分类

```text
SchedulerBase
├── Scheduler
│   └── ContinuousScheduler
│       ├── ChunkedPrefillScheduler
│       │   └── MixScheduler
│       ├── DisaggPDScheduler
│       │   └── PDOOCScheduler
│       ├── ZeroEvictionScheduler
│       ├── PrefillOnlyScheduler
│       └── FixedStepsScheduler
└── DiTScheduler
    └── DiTDynamicBatchScheduler
```

注意两个点：

1. `FixedStepsScheduler` 虽然继承自 `ContinuousScheduler`，但逻辑上已经是 Rec 专用分支，并不沿用 continuous decode 主循环。
2. `PrefillOnlyScheduler` 不是 `ChunkedPrefillScheduler` 的子类，而是直接继承 `ContinuousScheduler`，说明它虽然在工厂选择上和 chunked prefill 强相关，但实现并不是“chunked 的一个小修补”。

---

## 6. 调度器是如何被选择出来的

### 6.1 `LLMMaster` 选择 continuous family

`LLMMaster` 构造时会组装一份 `ContinuousScheduler::Options`，然后调用：

- `create_continuous_scheduler(engine_.get(), scheduler_options)`

随后 `LLMMaster::run()` 启动后台线程，循环执行：

- `scheduler_->step(absl::Milliseconds(500))`

也就是说，LLM 主线的 scheduler 不是延迟创建的，而是在 `LLMMaster` 构造期间就已经定型了。

### 6.2 `RecMaster` 固定选择 `FixedStepsScheduler`

`RecMaster` 也会先组一份 `ContinuousScheduler::Options`，但最终调用的是：

- `create_fixed_steps_scheduler(engine_.get(), scheduler_options)`

所以 Rec 主线不会走 `ContinuousScheduler` / `ChunkedPrefillScheduler` / `DisaggPDScheduler` 那套选择矩阵，而是直接进入 `FixedStepsScheduler`。

### 6.3 `DiTMaster` 固定选择 `DiTDynamicBatchScheduler`

`DiTMaster` 使用的是另一套工厂：

- `create_dit_scheduler(engine_.get(), scheduler_options)`

当前实现固定返回：

- `DiTDynamicBatchScheduler`

这说明 DiT 调度目前没有 LLM 那种复杂变体分流。

### 6.4 LLM family 的具体选择矩阵

`scheduler_factory.cpp` 的选择顺序非常重要，因为它体现的是**优先级**，不是并列关系：

| 顺序 | 条件 | 结果 | 备注 |
| --- | --- | --- | --- |
| 1 | `FLAGS_use_mix_scheduler == true` | `MixScheduler` | 强依赖 `enable_chunked_prefill=true` |
| 2 | `options.enable_disagg_pd() && options.enable_pd_ooc()` | `PDOOCScheduler` | PD 分离 + OOC |
| 3 | `options.enable_disagg_pd()` | `DisaggPDScheduler` | 普通 PD 分离 |
| 4 | `options.enable_chunked_prefill() && (FLAGS_enable_prefill_sp || options.num_speculative_tokens() > 0)` | `PrefillOnlyScheduler` | prefill SP / speculative 相关 |
| 5 | `options.enable_chunked_prefill()` | `ChunkedPrefillScheduler` | 典型 chunked prefill |
| 6 | `FLAGS_use_zero_evict == true` | `ZeroEvictionScheduler` | 注意它排在 chunked 和 PD 之后 |
| 7 | 其它情况 | `ContinuousScheduler` | 默认 continuous batching |

这张表有三个很关键的源码结论：

1. **`MixScheduler` 优先级最高**。只要它开了，就会在工厂最先命中。
2. **`ZeroEvictionScheduler` 的优先级比 chunked / PD 低**。如果同时开了 `chunked_prefill`，工厂不会选 zero-evict。
3. **调度器选择不是纯 `Options` 决策**。`use_mix_scheduler`、`use_zero_evict`、`enable_prefill_sp` 都是直接从 global flags 读的。

---

## 7. `ContinuousScheduler`：主干调度骨架

这一节是整份笔记最重要的部分。因为其它大部分 LLM scheduler 本质上都是在它的基础上改。

### 7.1 对外接口非常小，但内部状态非常多

`ContinuousScheduler` 对外真正暴露的方法不多：

- `add_request(std::shared_ptr<Request>& request)`
- `step(const absl::Duration& timeout)`
- `generate()`
- `get_waiting_requests_num()`
- `get_latency_metrics(...)`
- `get_instance_info()`

但内部维护的状态远比接口复杂，尤其是下面这些容器：

| 字段 | 作用 | 语义 |
| --- | --- | --- |
| `request_queue_` | 新请求入口 | 线程安全 MPMCQueue，`add_request()` 只负责往里写 |
| `waiting_priority_queue_` | 在线 waiting 队列 | 等待首次 prefill 或被抢占后重进 |
| `waiting_priority_queue_offline_` | 离线 waiting 队列 | 离线任务版本的 waiting 队列 |
| `running_queue_` | 在线 decode 队列 | 已经持有 KV、未来还能继续 decode 的请求 |
| `running_queue_offline_` | 离线 decode 队列 | offline decode 请求 |
| `running_requests_` | 当前 step 被选中的 request | 只表示“这一轮要跑的 request” |
| `running_sequences_` | 当前 step 被选中的 sequence | 真正的调度执行单元 |
| `running_sequences_budgets_` | 每条 sequence 的 token budget | 本轮允许处理多少 token |
| `pending_requests_` | pending 计数器 | 不是队列，只是“仍在请求构造阶段”的原子计数 |
| `last_step_prefill_` | 上一轮是否是 prefill step | 影响 FCFS 队列回插位置 |
| `last_batch_` / `last_running_*` | schedule overlap 的缓存 | 用于前后两轮 step 管线化 |

### 7.2 `pending` 在源码里不是一个队列

这个点特别容易误解。

`pending_requests_` 并不是“第三个请求池”，它只是一个计数器，主要由 `LLMMaster::handle_request()` 控制：

1. 收到请求时，先 `scheduler_->incr_pending_requests(1)`。
2. 请求处理线程池里把 prompt / messages 转成 `Request`。
3. 无论成功失败，线程退出时都 `scheduler_->decr_pending_requests()`。

因此：

- `pending` = “还没完全完成 `Request` 构造并写入 scheduler 的请求数”
- `waiting` = “已经进入 scheduler，但还没开始/没继续 prefill 的请求”
- `running` = “已经进入 decode 生命周期，并在未来可能继续被调度的请求”

这和很多框架里“pending queue / waiting queue / running queue”三队列并列的设计并不一样。

### 7.3 调度粒度是 Request 管理 + Sequence 执行

`ContinuousScheduler` 的一个核心设计是：

1. 队列里放的是 `Request`。
2. 真正组 batch 时，拆出来的是 `Sequence*`。
3. `BatchFactory` 最终把 `running_sequences_ + running_sequences_budgets_` 压成 `Batch`。

所以它并不是纯 request-level scheduler，而是：

> Request 负责生命周期与优先级，Sequence 负责本轮真正消耗多少 token / block。

这也是为什么 `running_requests_` 和 `running_sequences_` 必须同时维护。

### 7.4 `Sequence` 的 stage 是推导出来的

调度里大量逻辑都依赖 `Sequence::stage()`：

- `PREFILL`
- `CHUNKED_PREFILL`
- `DECODE`

这个 stage 不是手工打标，而是由：

- `kv_state_.kv_cache_tokens_num()`
- `num_prompt_tokens()`
- `volatile_num_prompt_tokens_`

动态推导出来的。其核心规则是：

1. KV 里还没有覆盖到整个 prompt -> 仍在 prefill。
2. KV 里已经覆盖了一部分 prompt，但还没覆盖完 -> `CHUNKED_PREFILL`。
3. KV 里已覆盖完整 prompt -> `DECODE`。

这直接决定了：

- 本轮 token budget 怎么算；
- `append_token()` 是否合法；
- 是否应该走 chunked prefill 逻辑；
- latency metrics 是否跳过。

---

## 8. `ContinuousScheduler` 的主循环：从 add_request 到 process_batch_output

### 8.1 `add_request()`：只做入队，不做真正调度

`ContinuousScheduler::add_request()` 做的事情非常少：

1. 校验 request 非空、且已有至少一个 sequence。
2. 调用 `kv_cache_manager_->prefetch_from_storage(request)`。
3. 尝试写入 `request_queue_`。

因此 `add_request()` 的职责不是“立刻排进 waiting queue”，而是把它交给后续 `prepare_batch()` 消化。

这里一个容易漏掉的点是：

- 新请求一进 scheduler，就会尝试做 KV store 的预取准备；
- 真正调 prefill 时，还会调用 `update_prefetch_result(request, prefetch_timeout)` 检查预取是否完成；
- 如果还没完成，会把请求暂时放回 waiting queue，等下一轮再看。

也就是说，prefix cache / KV store 已经深度进入 scheduler 逻辑，而不是 engine 才开始关心。

### 8.2 `prepare_batch()` 阶段 1：吸入新请求

`prepare_batch()` 一开始会不断从 `request_queue_` 读请求。

对每个新请求：

1. 如果 prefix cache 关闭，立刻 `request->expand_sequences(false)`。
2. 若第一个 sequence 还没有任何 KV tokens：
   - 在线请求进 `waiting_priority_queue_`
   - 离线请求进 `waiting_priority_queue_offline_`
3. 若已有 KV tokens（例如来自 PD decode 侧迁移回来的请求），则直接塞进 `running_requests_`。

这里反映出一个重要事实：

- **waiting queue 主要承载“需要做 prefill 的 request”**；
- **running queue 主要承载“已经进入 decode 生命周期的 request”**。

### 8.3 `prepare_batch()` 阶段 2：处理上一轮留下的完成/取消请求

接下来 scheduler 会遍历上一轮的 `running_requests_`：

1. `request->update_connection_status()`
2. 如果 finished / cancelled：
   - `kv_cache_manager_->deallocate(request.get())`
   - 放进 `finished_requests`
   - 在 `running_requests_` 位置上置空

这一步做完后，真正还活着的 running request 才会回流到 running queue。

### 8.4 `prepare_batch()` 阶段 3：把上一轮 running request 回插回 running queue

回插之前会先调用：

- `handle_running_requests(request)`

它做两件事：

1. 尝试 `request->expand_sequences()`，如果成功则把第一个 sequence 的 blocks cache 起来供共享。
2. 释放 request 内已经 finished 的 sequence 的 blocks。

然后才把 request 重新插回 `running_queue_` / `running_queue_offline_`。

#### FCFS 下为什么还要区分 `push_front` / `push_back`

这是 `last_step_prefill_` 的关键作用。

如果上一轮是 prefill step：

- 这些 request 要插回 queue **后部**；
- 因为它们虽然刚跑过，但仍然处在 prefill / 刚进入 decode 的过渡阶段，不能压过已经在 decode 阶段排队很久的 request。

如果上一轮是 decode step：

- 这些 request 要插回 queue **前部**；
- 因为它们本轮刚刚活跃过，应该仍然高于 queue 里尚未被选中的低优先级剩余请求。

这也是为什么 `FCFSQueue` 不是简单 `push_back/pop_front`，而是专门支持：

- `push(req, if_back)`

### 8.5 `prepare_batch()` 阶段 4：重置本轮预算

每一轮真正调度前，会先重置：

- `running_requests_`
- `running_sequences_`
- `running_sequences_budgets_`

然后初始化本轮预算：

- `latency_budget`
- `estimate_latency`
- `remaining_token_budget`
- `remaining_seq_budget`

在基础 `ContinuousScheduler` 中：

- prefill 阶段默认用 `max_global_ttft_ms`
- decode 阶段默认用 `max_global_tpot_ms`
- 初始 `estimate_latency` 来自 `profile_manager_->get_constant_overhead()`

### 8.6 `handle_prefill_requests()`：prefill 的核心调度逻辑

这是基础 continuous batching 最关键的函数之一。

它的主要逻辑是：

1. 只要 waiting queue 非空，且 token/seq/latency budget 都没耗尽，就尝试继续取 top request。
2. 若不是 PD 模式，还要检查 KV 利用率是否已经超过 `FLAGS_prefill_scheduling_memory_usage_threshold`。
3. 如果请求 finished/cancelled，直接释放并回调完成。
4. 如果 KV store 预取尚未准备好：
   - 把 request 弹出再压回 waiting queue；
   - 当前轮先不调它。
5. 对 request 里的每个未完成 sequence：
   - 计算 `num_need_compute_tokens()` 作为本轮 prefill token 需求；
   - 检查 token/seq budget；
   - 尝试分配 KV blocks。
6. 如果在线 prefill 遇到 block 不够，且 `enable_online_preempt_offline=true`，会尝试驱逐 offline decode 请求。
7. 如果启用了 latency-aware schedule，则还要用 `ProfileManager` 预测这条 prefill sequence 的 step latency，超预算就不进。
8. 整个 request 可被接受后，才会：
   - 从 waiting queue 弹出；
   - 放进 `running_requests_`；
   - 把对应 sequence 和 budget 放进 `running_sequences_` / `running_sequences_budgets_`。

#### 这里的 budget 含义是什么

prefill 时，budget 基本表示：

> 这条 sequence 还有多少 token 需要被真正计算。

而这个值不是简单的 `prompt_len`，而是：

- `num_tokens - max(kv_cache_tokens_num, host_kv_cache_tokens_num)`

也就是：

- 本地 KV + host KV 里已经覆盖掉的 token 不算；
- 只计算还没真正被 forward 的那部分。

#### prefill 是否会失败掉单个请求

会。

如果这一轮：

- `running_sequences_` 仍然为空；
- waiting queue 也不为空；
- running queue 又为空；
- 但当前 top request 还是因为 budget 不够或 block 不够无法进入；

那 scheduler 会直接把这个 request 视为“单请求也调不动”，返回 `RESOURCE_EXHAUSTED`。

### 8.7 `handle_decode_requests()`：decode 的核心调度逻辑

基础思路和 prefill 类似，但 decode 的单位不一样。

对普通 decode sequence 来说，本轮预算通常是：

- `1 + min_speculative_tokens_required_`

也就是：

- 普通 decode：本质上 1 token；
- speculative decode：要多带上 speculative tokens。

它的主要流程是：

1. 从 `running_queue` 顶部拿 request。
2. 对 request 内各个未完成 sequence：
   - 先做 latency-aware 检查；
   - 再看 token / seq budget；
   - 再尝试为 `updated_num_tokens = num_tokens + speculative_tokens` 分配 blocks。
3. 若成功：
   - request 弹出 running queue；
   - sequence 进入 `running_sequences_`；
   - 每条 sequence 的 budget 记为 `1 + speculative_tokens`。
4. 若 block 不够：
   - 在线请求优先尝试驱逐 offline decode；
   - 否则尝试驱逐当前 running queue 末尾的低优先级 request；
   - 被驱逐的 request 会 `set_preempted()` 并回到 waiting queue。

#### beam search 是特殊分支

如果 `request->check_beam_search()` 为真，decode 逻辑会进入一个更严格的分支：

1. 先收集所有未 finished 的 active sequences。
2. 预算判断是对整个 beam 一次性做的。
3. block 判断不是直接 allocate，而是先用 `estimate_decode_extra_blocks()` 估算需要额外的 blocks。
4. 只有当整组 beam 都能安全进入时，才统一 allocate。
5. 如果中途 allocation 失败，会对整个 request 做一次完整 deallocate，避免各 beam sequence 状态不一致。

这说明 beam search 在 scheduler 里不是“多条普通 sequence”，而是有明显更强的一致性约束。

### 8.8 `handle_abnormal_request()`：半成功与失败兜底

这是基础 scheduler 的一个很关键的兜底函数。

它处理两类异常场景：

1. **candidate_sequences 为空**
   - 说明这个 request 连一条 sequence 都塞不进当前 batch。
   - 如果连系统里都没有其它 running sequence，那么它会被视为单请求不可调度，直接失败。

2. **candidate_sequences 非空，但整个 request 塞不完整**
   - 对非 beam search，可以接受“部分 sequence 先进入本轮 batch”。
   - 这就是 request 级管理、sequence 级执行的一个直接体现。

### 8.9 `BatchFactory::create_batches()`：把 sequence 预算变成真正的 Batch

scheduler 自己并不直接 new 出完整执行输入，而是调用：

- `BatchFactory::create_batches(running_requests_, running_sequences_, running_sequences_budgets_, ...)`

这里做了三件事：

1. 依据 `running_sequences_budgets_` 计算：
   - 本轮 prompt tokens 数
   - 本轮 generated tokens 数
2. 按 `sequence->dp_rank()` 把 sequence 分发到不同 DP rank 的 `Batch`。
3. 如果是 beam search，再把 `SequencesGroup` 一并塞进 batch。

这一步体现出：

- scheduler 决定“谁 + 多少 token”
- `BatchFactory` 决定“按哪种执行维度把这些 sequence 压成 worker 能吃的 batch”

### 8.10 `schedule_request()`：空转等待，不忙轮询

`schedule_request(timeout)` 会不断调用 `prepare_batch()`，直到：

1. 拿到非空 batch；或
2. 超时。

如果所有 queue 都空，会按 10ms 粒度 sleep，而不是忙等。

### 8.11 `step()`：普通模式、schedule overlap、PD OOC 三种执行路径

#### 普通模式

当 `enable_schedule_overlap=false`：

1. `schedule_request(timeout)` 取 batch。
2. 若 batch 非空：
   - 非 OOC 走 `engine_->step(batch)`；
   - OOC 走 `step_with_pd_ooc(batch)`。
3. reset transfer infos。
4. `process_batch_output(false)`。

#### schedule overlap 模式

当 `enable_schedule_overlap=true`：

1. 先为当前轮拿 batch。
2. 若当前轮 batch 非空，先 `engine_->step(batch)`，把下一轮 forward 预先发出去。
3. 然后对上一轮缓存的 `last_batch_` 调用：
   - `engine_->update_last_step_result(last_batch_)`
   - `process_batch_output(true)`
4. 最后把当前轮 batch 和 running 集合存入 `last_batch_ / last_running_*`，留给下一轮处理。

所以 overlap 本质上是：

> 当前轮负责提交 forward，下一轮负责回收上一轮结果。

#### OOC 特殊路径

`step_with_pd_ooc()` 主要额外记录 batch 长度并打印 step latency，用于 OOC 模式下的性能分析。

### 8.12 `process_batch_output()`：scheduler 到响应层的最后一跳

这一步做的不是“模型计算”，而是“结果回写 + 响应出流”：

1. 更新 token latency metrics：
   - TTFT
   - inter-token latency
2. 更新内存相关 metrics：
   - occupied slots
   - active activation memory
3. 对 stream request：
   - 未 finished 的直接进 `process_stream_requests()`；
   - overlap 模式下 finished 但 last token 还没处理的，会先 `handle_last_token()`。

因此 scheduler 并不是 batch 一发出就结束，它还要参与结果的时延统计和流式返回节奏。

---

## 9. `ContinuousScheduler` 里的优先级、抢占与比较器

### 9.1 waiting queue 和 running queue 用的不是完全同一套逻辑

`waiting_priority_queue_` 使用的是：

- `create_comparator(options.priority_strategy())`

这套比较器支持很多策略：

- `fcfs`
- `priority`
- `deadline`
- `sjf`
- `density`
- `decode_density`
- `urgency_density`
- `decode_urgency_density`
- `urgency_priority`
- `decode_deadline`

但是 `create_running_queue()` 里，基础 `ContinuousScheduler` 真正为 `running_queue_` 提供的只有：

- `fcfs`
- `deadline`
- `priority`

其余策略会直接 fallback 到 `FCFSQueue`。

这是一个非常值得强调的源码事实：

> 在基础 `ContinuousScheduler` 中，复杂策略未必同时作用在 waiting queue 和 running queue。

这也解释了为什么 `MixScheduler` 会单独维护自己的 `std::list` 并自己 sort —— 因为它需要更复杂的全局顺序控制。

### 9.2 抢占方向：在线可以抢离线，低优先级可以被踢回 waiting

基础连续调度里，preemption 的主要方向是：

1. **online prefill -> offline decode**
2. **online decode -> offline decode**
3. **高优先级 decode -> 同队列低优先级 decode**

被抢占后的 request 不会消失，而是：

- `set_preempted()`
- 回到 waiting queue

这意味着被抢占请求后续可能重新 prefill / 重新恢复，是典型的“用吞吐换时延”的在线优先策略。

### 9.3 `last_step_prefill_` 是 FCFS 行为的关键开关

`last_step_prefill_` 决定的不是“这轮是不是 prefill”，而是：

- 下一轮把上一轮 active request 回插回 running queue 时，该用前插还是后插。

因此它直接影响 FCFS 下 decode 请求的连续性和 prefill 插队程度。

---

## 10. `ChunkedPrefillScheduler`：decode-maximal batching

### 10.1 它和基础 continuous 的本质差异

`ChunkedPrefillScheduler` 的头文件注释已经把定位说得很清楚：

1. 它是基于 `ContinuousScheduler` 的 chunked prefill 变体。
2. 它遵循 **decode-maximal batching** 原则。
3. 先尽量塞 decode，再用剩余 token quota 塞 prefill chunk。

这和基础 `ContinuousScheduler` 的区别非常大：

- 基础 continuous：如果这轮有 prefill，就不会去调 decode。
- chunked prefill：允许 decode 和 chunked prefill 混在同一轮 batch 中。

### 10.2 `prepare_batch()` 的三步顺序

它的 `prepare_batch()` 逻辑可以概括成三步：

#### 第一步：优先处理 `running_queue_`

调用：

- `handle_running_queue_requests(...)`

这里既可能处理 decode request，也可能处理已经处于 `CHUNKED_PREFILL` 阶段的 request。

这一步体现的就是 decode-maximal：

- 先保证已经进入生命周期中的 request 尽量被继续推进；
- 不先把资源全部让给新到达的长 prompt。

#### 第二步：再接纳新的 waiting prefill request

只有在：

- `budget_exhausted == false`
- `blocks_exhausted == false`

时，才继续从 `waiting_priority_queue_` 和 `waiting_priority_queue_offline_` 接新请求。

而且这里还有一个很微妙的点：

- **新来的 prefill request 不能反过来抢 running queue 中的 request。**

#### 第三步：如果还有 budget，再给已在 prefill 阶段的 sequence 多分 chunk

最后调用：

- `handle_remaining_budget(...)`

它会把剩余 token budget 再补给当前轮已经选中的 `prefill_stage_sequences`，让它们尽可能多推进一点。

也就是说，chunked prefill 不是简单“每次只跑一个 chunk”，而是：

> 先保证 decode，次优保证新 prefill 能进来，最后再尽可能把当前已经在 prefill 的 sequence 向前推。

### 10.3 `allocate_blocks_for()`：chunk 的真正含义

`allocate_blocks_for(sequence, token_budget, &actual_tokens)` 的核心含义是：

1. 如果 sequence 还在 prefill：
   - 本轮最多处理 `min(kv_cache_tokens_num + token_budget, num_tokens)`。
2. 如果 sequence 已经在 decode：
   - 还要额外加上 speculative tokens。

最终输出给 batch 的 `actual_tokens`，表示：

- 这条 sequence 本轮真正前进了多少 token。

### 10.4 `allocate_shared_blocks_for()`：chunked 下的 prefix cache 不是只匹配一次

这是 chunked prefill 非常关键、也很容易忽略的一点。

它会在：

- 首次进入时 allocate shared blocks；
- chunked prefill 过程中按 `FLAGS_chunked_match_frequency` 控制的间隔，再次执行共享块分配。

这对应 `docs/zh/features/prefix_cache.md` 提到的：

- chunked scheduler 支持多阶段 chunked prefill 匹配。

也就是说，prefix cache 在 chunked 场景里不是“开局匹配一次就结束”，而是阶段性重匹配。

### 10.5 我对文档里“decode 优先 batch 策略”的判断

`docs/zh/features/overview.md` 里提到“prefill 优先和 decode 优先等 batch 策略”，但文档没有把类名一一列出来。

结合源码，我认为：

- `ChunkedPrefillScheduler` 基本可以视为 **decode 优先 / decode-maximal** 这一类。

这是**源码支持很强的推断**，依据主要来自：

1. 头文件注释明确写了 `decode-maximal batching`。
2. `prepare_batch()` 的顺序是先 `handle_running_queue_requests()`，再接新 prefill。

---

## 11. `PrefillOnlyScheduler`：更强的 prefill-first 变体

### 11.1 它为什么会被选中

工厂里它的触发条件是：

- `enable_chunked_prefill == true`
- 且 `FLAGS_enable_prefill_sp == true` **或** `num_speculative_tokens > 0`

也就是说，它并不是“所有 chunked prefill 都走”，而是针对：

1. prefill sequence parallel
2. speculative decode

这类更特殊的场景。

### 11.2 它最核心的特点：继续完成上一步未跑完的 prefill

`PrefillOnlyScheduler` 相比基础 continuous，多了一个关键机制：

- `handle_last_step_prefill_requests(...)`

在 `prepare_batch()` 里，它会把上一轮仍处于 `CHUNKED_PREFILL` 的 request 单独挑出来，优先继续推进。

它的总体顺序是：

1. 先处理 `last_step_prefill_requests`
2. 再处理新的 waiting prefill request
3. **只有当本轮没有任何 prefill sequence 进入时**，才去调 decode request

这比基础 continuous 还要更偏向 prefill，因为它不仅“本轮 prefill 优先”，而且“上一轮没跑完的 prefill 还要优先续上”。

### 11.3 为什么名字叫 `PrefillOnly`

它其实并不是完全不调 decode，而是：

- 在有 prefill 可做的时候，decode 基本被压后；
- 只有本轮 `running_sequences_` 仍然为空时，decode 才会被调度。

所以这个名字更准确的理解应该是：

> prefill-first / prefill-dominant，而不是永远没有 decode。

### 11.4 我对文档里“prefill 优先 batch 策略”的判断

结合 `docs/zh/features/overview.md` 和实现，我认为：

- `PrefillOnlyScheduler` 大概率就是文档里“prefill 优先”那一类策略的主要代码落点。

这是**有根据的推断**，因为：

1. 名字就已经非常直接；
2. `prepare_batch()` 的执行顺序清楚体现了 prefill-first；
3. 它被放在 chunked + special mode 的工厂分支上，说明它是为更强的 prefill 诉求而生。

---

## 12. `MixScheduler`：统一队列 + 紧迫度排序

### 12.1 它和 chunked prefill 的关系

`MixScheduler` 继承自 `ChunkedPrefillScheduler`，而且工厂里强制要求：

- `FLAGS_use_mix_scheduler=true` 时必须 `enable_chunked_prefill=true`

所以它不是独立于 chunked prefill 的另一路，而是建立在 chunked prefill 之上的更复杂策略。

### 12.2 它最大的结构变化：不再区分 waiting 与 running 两个世界

`MixScheduler` 自己维护的是：

- `std::list<std::shared_ptr<Request>> running_queue_`

新 request 进来后，如果还没任何 KV tokens，会直接进入这个统一列表，而不是像基础 continuous 那样先放 waiting queue。

这说明 `MixScheduler` 的设计目标不是“先区分 prefill / decode 再决定谁先跑”，而是：

> 把所有 request 放到同一个有序空间里统一排序。

### 12.3 它会给 request 打紧迫度标签

`get_latency_budget_and_request_order()` 做了几件基础 continuous 没有的事：

1. 用 `ProfileManager` 给 sequence 估算 step latency。
2. 更新 request 的 elapsed time 与 deadline。
3. 根据 remaining_time / load_judge_func 给 request 打：
   - `URGENT`
   - `NORMAL`
   - `STARVED`
4. 最后用 `create_comparator(priority_strategy, true)` 对统一列表排序。

因此它用到的并不是简单 FCFS / strict priority，而是更接近：

- deadline-aware
- density-aware
- anti-starvation

这也是为什么 `priority_comparator.cpp` 里会有：

- `UrgencyDensityComparator`
- `UrgencyPriorityComparator`
- `DecodeUrgencyDensityComparator`

这些在基础 `ContinuousScheduler::create_running_queue()` 里根本没有完全落地的策略。

### 12.4 它还考虑了 copy blocks / host blocks 因素

`MixScheduler::allocate_blocks_for()` 比 chunked 多了：

- `needed_copy_blocks_num`

并且在 `FLAGS_host_blocks_factor > 1.0` 时，会走：

- `kv_cache_manager_->allocate(sequence, max_handle_num_tokens, needed_copy_blocks_num)`

这说明它不只是“更会排顺序”，还把 host block / copy latency 纳入了调度决策。

### 12.5 我对 `MixScheduler` 的定位

结合头文件注释“currently only for multi-priority scheduling algorithm ProSched”，我当前的理解是：

- `MixScheduler` 是 xLLM 里最接近“策略研究型 / QoS-aware / multi-priority”调度器的一条实现分支。

它不是默认主线，而是面向更复杂优先级控制的扩展实现。

---

## 13. `ZeroEvictionScheduler`：用未来块模拟换更少驱逐

### 13.1 它的核心目标不是 TTFT，而是减少驱逐

官方文档说得比较直白：

- zero-evict 的目标是最小化 request eviction，减少被驱逐请求重新 prefill 的成本，从而改善 TPOT。

源码里这个目标落在：

- `BlockCapacityGuard`

### 13.2 `BlockCapacityGuard` 到底在模拟什么

它会把三类 sequence 合并起来看：

1. 当前要接纳的 candidate prefill sequences
2. 当前 running queue 中未来可能继续 decode 的 sequences
3. 当前轮已经挑进 running_sequences_ 的 sequences

然后为每条 sequence 计算一个 `SequenceStatus`：

- `num_block_need_to_use_`
- `num_release_block_`

再做“未来若干 decode step”的块消耗模拟：

1. 先算接纳 candidate prefill 后还剩多少 blocks。
2. 再迭代未来每个 step：
   - 每条 active sequence 至少再消耗一个 block step；
   - 同时一部分 block 会随着进度推进被释放出来。
3. 只要在模拟过程中剩余 blocks 一直够用，才接受 candidate sequences。

这正是 zero-evict 这个名字的来源：

> 不再等真的 block 不够了才驱逐，而是在接纳新 prefill 前，先模拟未来是否会把自己逼到驱逐。

### 13.3 它改的其实只有 prefill 接纳策略

`ZeroEvictionScheduler` 主要重写的是：

- `handle_prefill_requests(...)`

以及内部辅助：

- `try_allocate_block_for(...)`

decode 侧并没有重新造一套逻辑，所以它可以理解成：

> 基础 continuous decode + 更保守的 prefill admission control。

### 13.4 `max_decode_token_per_sequence` 的作用

`BlockCapacityGuard::num_block_need_to_use_for()` 用：

- `FLAGS_max_decode_token_per_sequence`

来估算每条 sequence 未来最多还会吃掉多少 decode blocks。

因此 zero-evict 并不是“纯运行时自适应”，而是建立在一个**未来 decode token 上界**之上的。

---

## 14. `DisaggPDScheduler`：跨实例的 Prefill / Decode 调度器

### 14.1 它和基础 continuous 的最大差异：请求不再直接进本地 `request_queue_`

`DisaggPDScheduler::add_request()` 的逻辑和基础 continuous 完全不同：

1. 仍然会先 `prefetch_from_storage(request)`。
2. 但不会直接写本地 `request_queue_`。
3. 在线请求进入：
   - `prefill_request_queue_`
4. 离线请求进入：
   - `prefill_request_queue_offline_`

也就是说，PD 模式下 request 先进入的是：

- **prefill 到 decode 的分发通道**

而不是普通 continuous 的本地 waiting 队列。

### 14.2 Prefill 侧的第一阶段：先把请求元信息发给 Decode

`dispatch_requests()` 是 Prefill 侧真正的第一阶段。

它会：

1. 优先拉在线请求；超时后再尝试 offline 请求，避免 offline 长时间阻塞在线流量。
2. 校验 `decode_address` 是否存在。
3. 为目标 decode instance 建立 RPC channel。
4. 构造 `proto::DisaggRequests`，把下面这些元信息发给 decode：
   - request id
   - service request id
   - prompt / prompt_tokens
   - stream 标记
   - seq capacity
   - stopping checker 的 max tokens / max context / stop tokens / eos 等
5. 等 decode 回包，把 decode 侧预分配好的 block / dp rank / transfer 信息写回 request / sequence。
6. 最后才把 request 写入 Prefill 节点本地 `request_queue_`，进入真正的 prefill 执行。

所以 PD 模式不是“用户请求先进 Prefill scheduler 再说”，而是：

> Prefill 要先让 Decode 为这个请求准备好后续 decode 所需的资源布局，自己才开始本地 prefill。

### 14.3 Prefill 侧的第二阶段：prefill 完后把首 token 送给 Decode

`DisaggPDScheduler::step()` 本质上仍然调用：

- `ContinuousScheduler::step(timeout)`

但如果本实例不是 decode 且上一轮确实做了 prefill，就会接着调用：

- `prefill_send_first_generation()`

它会把那些刚刚完成 prefill、并且已经生成出第一个 token 的 request 拿出来：

1. 非 stream 请求在 Prefill 实例本地就先回一次输出。
2. 然后异步向 Decode 实例发送：
   - 首 token
   - TTFT
   - top tokens / top logprobs
   - KV 传输模式
   - 若为 `PULL`，还会附带源侧 block ids / cluster ids / addrs / cache ids / dp info
3. 发送完成后，Prefill 节点释放本地 request 的 KV blocks。

### 14.4 Decode 侧的第一阶段：接住“请求骨架”

Decode 侧通过：

- `decode_schedule(request, prefill_instance_name)`

先把收到的 request 存进：

- `received_request_map_`
- `instance_to_received_requests_map_`
- `request_to_instance_map_`

此时它还没有真正进 decode 主循环，而是先处于“等待首 token / 等待交接完成”的状态。

### 14.5 Decode 侧的第二阶段：收到首 token 后转入本地 decode 主循环

`decode_recv_first_generation(...)` 做的事情是：

1. 根据 req_id 从 `received_request_map_` 取回 request。
2. 把 first token 写回 sequence。
3. 更新 TTFT、latest_generate_time。
4. 若开启 `schedule_overlap`，先 append fake token，再 `update_last_step_token(first_token)`。
5. 如果 KV 传输模式是 `PULL`，就主动从 Prefill 节点拉对应 blocks。
6. 最后把 request 写入 Decode 节点本地 `request_queue_`，交给普通 continuous decode 流程继续处理。

所以 PD 模式的本质是：

> Prefill 执行 prompt，Decode 接管生成；scheduler 成了跨实例 handoff 的编排者。

---

## 15. `PDOOCScheduler`：PD + online/offline co-location

### 15.1 它在 `DisaggPDScheduler` 上又多做了什么

`PDOOCScheduler` 继承自 `DisaggPDScheduler`，但它并不只是多一个 RPC，而是把 decode 端的调度目标改成了：

1. 满足在线 decode 的 TPOT SLO；
2. 在有余力时从 Prefill 端“拉”一部分 offline 请求下来共用 decode 资源。

### 15.2 `step()` 会按实例角色分流

它的 `step(timeout)` 不再统一走一条逻辑，而是：

- Prefill 实例走 `prefill_step(timeout)`
- Decode 实例走 `decode_step(timeout)`

### 15.3 Prefill 实例做什么

`prefill_step()` 的核心顺序是：

1. `prepare_offline_dispatch_queue()`：根据 decode 侧发来的 pull signal，挑出适合迁移的 offline request。
2. `ContinuousScheduler::step(timeout)`：本地继续普通 prefill 调度。
3. `prefill_send_first_generation()`：发送首 token。
4. `prefill_send_multi_generations()`：在需要时发送多 token 迁移结果。

### 15.4 Decode 实例做什么

`decode_step()` 的核心顺序是：

1. 清空 `decode_step_global_batch_req_lens_`。
2. 走 `ContinuousScheduler::step(timeout)`。
3. 根据 decode 侧 perf model 估算出来的最近一步时延，判断是否还能继续“拉”更多 offline 请求。
4. 如果可以，就唤醒 `decode_send_pull_signal()` 线程。

### 15.5 它的 decode 调度不是简单 token budget，而是 perf model 约束

`PDOOCScheduler::handle_decode_requests()` 在 decode 节点会覆盖基础 continuous 的实现。

它会：

1. 把当前 batch 中各 request 的长度加入 `decode_step_global_batch_req_lens_`。
2. 周期性用：
   - `llm_flops_.decode(decode_step_global_batch_req_lens_)`

   去预测当前 decode batch 的 latency。
3. 如果预测时延接近或超过 `max_global_tpot_ms`，就停止往这个 batch 里继续塞请求。

这和基础 continuous 很不一样。

基础 continuous 更像：

- token budget + seq budget + block budget + 可选 profile 约束

而 PDOOC decode 更像：

- **直接把整批 decode latency 预测** 作为最重要的 admission control。

### 15.6 pull signal 机制：Decode 主动向 Prefill 要 offline request

`decode_send_pull_signal()` 会：

1. 等 decode step 线程唤醒。
2. 选一个 prefill instance。
3. 根据当前空闲 blocks 与最近 decode batch 形态，计算：
   - `max_total_len`
   - `preferred_req_len`
4. 通过 RPC 把这个 pull signal 发给选中的 prefill instance。

随后 Prefill 端的 `prepare_offline_dispatch_queue()` 会：

1. 读取 pull signal；
2. 在本地 `running_requests_` 里找最匹配长度、且目前还处于 offline decode 阶段的 request；
3. 把它移入 `offline_requests_to_dispatch_`，后续由 `dispatch_offline_requests()` 发给 decode instance。

所以 `PDOOCScheduler` 的额外价值在于：

> 不只是做 PD handoff，还会在运行时动态搬运 offline request，尽量把 decode 端的剩余能力吃满。

---

## 16. `FixedStepsScheduler`：Rec 主线的固定步调度

### 16.1 为什么它不属于 continuous batching 主线

`FixedStepsScheduler` 虽然继承自 `ContinuousScheduler`，但它的设计目标明显不同：

1. 它没有真正的 decode running queue 主循环。
2. `prepare_batch()` 只做一次 prefill-style 的 request 收集。
3. `step()` 会把结果交给线程池异步执行，允许多个 step 并发。

它更像：

> 收集一批 request，做一次固定轮次执行，然后异步回收。

### 16.2 它有三种 pipeline

`FixedStepsScheduler` 内部不是一套逻辑包打天下，而是根据 `RecType` 和 `is_rec_multi_round_mode()` 选择 pipeline：

1. `LlmRecSchedulerPipeline`
   - 需要 KV cache；
   - allocate 时会为 `prompt + max_generated_tokens` 预留空间；
   - batch 通过 `BatchFactory::create_batches()` 生成。

2. `OneRecSchedulerPipeline`
   - 不需要 KV cache；
   - batch 通过 `BatchFactory::create_rec_batches()` 生成。

3. `RecMultiRoundSchedulerPipeline`
   - 也不需要 KV cache；
   - 但 batch 仍通过 `create_batches()` 走多轮模式。

### 16.3 `step()` 支持异步并发执行

这是它和 continuous family 最大的运行时差异之一。

`FixedStepsScheduler::step()` 在拿到非空 batch 后，会把真正的 `engine_->step(batches)` 封装成一个函数，然后：

- 如果 `rec_worker_max_concurrency > 1`：
  - 用 `counting_semaphore` + thread pool 控制并发度；
  - 当前 `step()` 返回后，下一次 `step()` 还可以继续推进。
- 否则就同步执行。

因此 `FixedStepsScheduler` 并不是“必须前一轮完全处理完才能进入下一轮”的串行设计。

---

## 17. `DiTDynamicBatchScheduler`：最简单的 request 数量凑批

### 17.1 它的接口都和 LLM scheduler 不一样

DiT 这条线根本不复用 `Request`，而是：

- `DiTRequest`
- `DiTBatch`
- `DiTScheduler`

这说明 DiT 主线在 scheduler 抽象层就已经分开了。

### 17.2 它的调度规则非常简单

`DiTDynamicBatchScheduler::prepare_batch()` 的逻辑可以概括为：

1. 从 `request_queue_` 里读请求；
2. 最多读到 `max_request_per_batch` 个；
3. 全部塞进一个 `DiTBatch`；
4. 调 `engine_->step(batches)`；
5. 全部异步回调完成；
6. 清空 `running_requests_`。

这里没有：

- KV block 管理
- waiting/running queue 分离
- preemption
- token budget
- seq budget
- chunked prefill
- schedule overlap

所以它只是一个“动态 request batching”，而不是 LLM 那种 stateful continuous scheduler。

---

## 18. 这些 scheduler 的差异矩阵

| 调度器 | 主要任务 | 是否 continuous batching | 是否有 KV/preemption | 是否跨实例 | 核心策略 |
| --- | --- | --- | --- | --- | --- |
| `ContinuousScheduler` | LLM 主线默认 | 是 | 是 | 否 | prefill 优先，必要时 decode，在线可抢离线 |
| `ChunkedPrefillScheduler` | 长 prompt 优化 | 是 | 是 | 否 | decode-maximal batching + chunked prefill |
| `PrefillOnlyScheduler` | prefill SP / speculative 场景 | 是 | 是 | 否 | 继续未完成 prefill，prefill-first |
| `MixScheduler` | 多优先级/QoS 场景 | 是 | 是 | 否 | 单统一队列 + urgency/deadline/density 排序 |
| `ZeroEvictionScheduler` | 减少驱逐 | 是 | 是 | 否 | 预模拟未来 block 使用，保守接纳 prefill |
| `DisaggPDScheduler` | PD 分离 | 是 | 是 | 是 | Prefill / Decode 跨实例 handoff |
| `PDOOCScheduler` | PD + online/offline 共置 | 是 | 是 | 是 | decode perf model + pull offline requests |
| `FixedStepsScheduler` | Rec | 否（不是 continuous decode） | 视 pipeline 而定 | 否 | 固定步执行，可并发 step |
| `DiTDynamicBatchScheduler` | DiT | 否 | 否 | 否 | 按 request 个数凑批 |

---

## 19. 我认为最值得记住的几个源码细节

### 19.1 xLLM 的 scheduler 不只是排队器，而是资源编排器

它同时要管：

- token budget
- sequence budget
- KV blocks
- prefix cache / KV store 预取
- latency budget
- online/offline QoS
- stream 回包节奏
- PD handoff

所以它已经不是“把请求排个序”这么简单，而是控制面里最核心的资源编排模块之一。

### 19.2 `pending_requests_` 很容易被误读

再次强调：

- 它不是 pending queue；
- 它只是“请求构造线程池尚未完成”的计数器。

这个点如果不先立住，后面读 waiting/running 很容易全乱。

### 19.3 基础 continuous 的复杂 priority strategy 支持并不完全对称

waiting queue 支持的策略比 running queue 多。

这意味着如果把 `priority_strategy` 设成更复杂的 `urgency_*` / `density_*`，在基础 `ContinuousScheduler` 下，它的效果未必覆盖整个生命周期。

真正更充分利用这些 comparator 的，是 `MixScheduler`。

### 19.4 `ChunkedPrefillScheduler` 和 `PrefillOnlyScheduler` 不是一回事

虽然两者都和 chunked prefill 相关，但：

- `ChunkedPrefillScheduler` 更像 decode-maximal；
- `PrefillOnlyScheduler` 更像 prefill-first。

这点如果只看名字很容易混淆。

### 19.5 PD family 已经不是“本地 scheduler 变体”，而是“分布式协议调度器”

一旦进入 `DisaggPDScheduler` / `PDOOCScheduler`，scheduler 已经直接参与：

- instance discovery
- RPC server
- request migration
- KV transfer mode
- cross-instance latency handoff

它和普通 continuous family 的复杂度不是一个量级。

---

## 20. 证据边界与仍需注意的点

### 20.1 我确认无误的部分

下面这些结论是源码直接支持的：

1. LLM / Rec / DiT 三条 scheduler 主线的分流关系。
2. `scheduler_factory.cpp` 的具体选择优先级。
3. `ContinuousScheduler` 的容器、budget、preemption、schedule overlap 主流程。
4. `ChunkedPrefillScheduler` 的 decode-maximal batching。
5. `PrefillOnlyScheduler` 的 prefill-first 执行顺序。
6. `MixScheduler` 的 unified queue + urgency sort。
7. `ZeroEvictionScheduler` 的 block simulation admission control。
8. `DisaggPDScheduler` / `PDOOCScheduler` 的跨实例 handoff 与 pull signal 机制。
9. `FixedStepsScheduler` / `DiTDynamicBatchScheduler` 与 continuous family 的本质不同。

### 20.2 明确属于“有根据推断”的部分

下面两点我认为判断很稳，但严格说属于“源码 + 文档共同支持的推断”：

1. 文档里说的“decode 优先 batch 策略”，其主要落点应当是 `ChunkedPrefillScheduler`。
2. 文档里说的“prefill 优先 batch 策略”，其主要落点应当是 `PrefillOnlyScheduler`。

原因是文档没有把类名一一绑定，但源码注释和执行顺序非常吻合。

### 20.3 后续如果继续深挖，建议顺着这三条线读

1. **Request / Sequence / Batch 状态机**
   - 对应 `ralph/learn_xllm/request.md`
   - 对应 `ralph/learn_xllm/5-batch.md`

2. **schedule overlap 的 fake token / 两阶段回写**
   - 对应 `ralph/learn_xllm/3-schedule_overlap.md`

3. **PD handoff 的 RPC / KV transfer / service routing**
   - 对应 `ralph/learn_xllm/4-pd_disaggregate.md`

---

## 21. 最后一句总结

如果只用一句话概括我这轮对 xLLM scheduler 的理解，那就是：

> xLLM 的 scheduler 不是单一算法，而是一组围绕 `ContinuousScheduler` 骨架不断分化出来的控制面编排器：基础版负责 continuous batching，chunked / prefill-only / mix / zero-evict 负责不同 batch 与资源策略，PD family 负责跨实例 handoff，Rec 和 DiT 则各自发展出了更适合自己任务形态的专用调度器。
