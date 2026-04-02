# xLLM framework 中 batch 逻辑学习笔记

> 说明：用户问题里写的是 `ralph/learn_xllm/overview.md` 和 `ralph/learn_xllm/structure.md`，但仓库里对应的实际文件名是 `ralph/learn_xllm/0-overview.md` 和 `ralph/learn_xllm/1-structure.md`。本文以这两份学习笔记为入口，再结合源码把 `framework/batch` 主链补全。

## 1. 本次学习范围

这次我重点追了 xLLM 里和 batch 直接相关的主链，目标不是只看 `batch.h/.cpp` 这两个文件，而是把下面这条链路吃透：

```text
RequestState
  -> Request
  -> SequencesGroup
  -> Sequence
  -> ContinuousScheduler 选中 running_sequences_ + budget
  -> BatchFactory 组 Batch
  -> Batch / BatchInputBuilder 组输入
  -> LLMEngine fan-out 到 worker
  -> RawForwardOutput / SampleOutput 回写 Batch
  -> Sequence / RequestOutput
```

本次实际阅读的核心文件：

- 学习入口
  - `ralph/learn_xllm/0-overview.md`
  - `ralph/learn_xllm/1-structure.md`
  - `ralph/learn_xllm/task.md`
- request / sequence / group
  - `xllm/core/framework/request/request.h`
  - `xllm/core/framework/request/request.cpp`
  - `xllm/core/framework/request/sequence.h`
  - `xllm/core/framework/request/sequence.cpp`
  - `xllm/core/framework/request/sequences_group.h`
  - `xllm/core/framework/request/sequences_group.cpp`
  - `xllm/core/framework/request/sample_slot.h`
- batch 主体
  - `xllm/core/framework/batch/batch.h`
  - `xllm/core/framework/batch/batch.cpp`
  - `xllm/core/framework/batch/batch_factory.h`
  - `xllm/core/framework/batch/batch_factory.cpp`
  - `xllm/core/framework/batch/batch_input_builder.h`
  - `xllm/core/framework/batch/batch_input_builder.cpp`
  - `xllm/core/framework/batch/batch_forward_type.h`
  - `xllm/core/framework/batch/mposition.h`
  - `xllm/core/framework/batch/mposition.cpp`
  - `xllm/core/framework/batch/rec_batch_input_builder.h`
  - `xllm/core/framework/batch/rec_batch_input_builder.cpp`
  - `xllm/core/framework/batch/dit_batch.h`
  - `xllm/core/framework/batch/dit_batch.cpp`
  - `xllm/core/framework/batch/batch_test.cpp`
- scheduler / engine / runtime 接口
  - `xllm/core/scheduler/continuous_scheduler.h`
  - `xllm/core/scheduler/continuous_scheduler.cpp`
  - `xllm/core/distributed_runtime/llm_engine.cpp`
  - `xllm/core/runtime/base_executor_impl.cpp`
  - `xllm/core/runtime/forward_params.h`
  - `xllm/core/runtime/params_utils.h`
  - `xllm/core/framework/model/model_input_params.h`

## 2. 先给结论：xLLM 里的 Batch 到底是什么

### 2.1 Batch 不是“请求队列”，而是“一次 step 的执行包”

从整体分层看，`Request` 是服务语义对象，`Sequence` 是真正的生成执行单元，而 `Batch` 是调度器把“这一轮要执行的 sequence + 本轮能执行多少 token + KV 相关元数据”打包后交给 engine 的桥接对象。

换句话说：

- `Request` 负责“这个业务请求是什么”。
- `Sequence` 负责“这条生成轨迹现在走到哪里了”。
- `Batch` 负责“本轮 forward 要喂模型什么输入，以及 forward 完以后把输出写回哪里”。

这和 `1-structure.md` 里的结论完全一致：真正被调度进 batch 的粒度是 `Sequence`，不是 `Request`；但是 batch 又不能完全脱离 request，因为 beam search、多序列输出、callback 回写都还是 request/group 级别的语义。

### 2.2 Batch 本质上是一个“非 owning 的视图对象”

`Batch` 自己并不拥有 request/sequence 的生命周期。它内部主要保存：

- `std::vector<Sequence*> sequences_`
- `std::vector<SequencesGroup*> sequence_groups_`
- `std::vector<uint32_t> allowed_max_tokens_`
- `std::vector<torch::Tensor> input_embeddings_vec_`
- `std::vector<MMData> mm_data_vec_`
- `std::vector<OutputTarget> output_targets_`
- `BatchForwardType batch_forward_type_`
- `batch_id_`
- swap block transfer 相关指针

所以 `Batch` 更像一次 step 的“运行时包装层”，而不是长期存在的数据对象。

### 2.3 Batch 有两个核心职责

从源码看，`Batch` 实际承担两大职责：

1. **输入侧职责**：把一组 `Sequence` 变成模型可吃的输入。
   - 包括 token flatten、position、KV slot/block table、sampling 索引、多模态数据、beam search 附加信息等。

2. **输出侧职责**：把 worker 返回的 sample/output 写回 `Sequence`。
   - 包括 append token、更新 logprob、处理 schedule overlap 的 fake token、beam search 合并、多模态 embedding 回写等。

也就是说，`Batch` 是 xLLM 里最关键的“执行前/执行后胶水层”。

## 3. Request / SequencesGroup / Sequence / Batch 的边界

先把四个对象的边界立住，否则 batch 很容易越看越乱。

| 对象 | 主要职责 | 生命周期 | 关键点 |
| --- | --- | --- | --- |
| `Request` | 持有请求级参数、callback、统计信息 | 整个请求级生命周期 | 对 scheduler 来说是管理单位 |
| `SequencesGroup` | 管理同一个 request 下的一组 sequence | 跟随 request | 主要承载 `n` / `best_of` / beam search 语义 |
| `Sequence` | 持有 token、stage、KV 状态、finish 状态 | 真正的执行单元 | scheduler 实际选的是它 |
| `Batch` | 一次 step 的执行打包与结果回写 | 单 step 临时对象 | scheduler 和 engine 的接口对象 |

### 3.1 Request 如何落到 Sequence

`Request` 构造时会把 `RequestState` 压缩成 `SequenceParams`，再创建 `SequencesGroup`。这里最重要的不是“构造了几个对象”，而是 batch 后续真正依赖的所有语义都已经被种进 sequence 里：

- `sampling_param`
- `stopping_checker`
- `sample_slots`
- `enable_schedule_overlap`
- `skip_special_tokens`
- `echo`
- `logprobs`
- `rec_type`

因此后面的 batch 逻辑基本不再需要回头看 HTTP/OpenAI 请求，它只看 sequence 当前状态。

### 3.2 Sequence 的 stage 是推导出来的，不是手动设置的

`Sequence::stage()` 不是一块单独状态位，而是由以下条件动态推导：

- `kv_cache_tokens_num == 0 且 < num_prompt_tokens` -> `PREFILL`
- `0 < kv_cache_tokens_num < num_prompt_tokens` -> `CHUNKED_PREFILL`
- `kv_cache_tokens_num >= num_prompt_tokens` -> `DECODE`

这点非常重要，因为 `BatchForwardType` 的判断、output target 的生成、append token 的合法性、chunked prefill 的特殊跳过逻辑，都依赖这个 stage 推导结果。

### 3.3 Batch 真正处理的是 Sequence*，但有时必须额外带上 SequencesGroup*

常规 LLM 路径里，batch 主要看 `sequences_`。但有两类情况必须额外保存 `sequence_groups_`：

1. **beam search**
   - 因为 beam merge/重排不是单条 sequence 能独立完成的，必须保留 group 级别上下文。

2. **Rec 路径**
   - `Batch` 可能根本不直接存 `sequences_`，而是只存 `sequence_groups_`，然后走 `prepare_rec_forward_input()` 专门分支。

## 4. Batch 在全链路中的位置

结合 `0-overview.md` 和 `1-structure.md`，以及源码实际调用链，可以把 batch 所处位置概括成下面这条主线：

```text
APIService
  -> LLMMaster
  -> ContinuousScheduler::prepare_batch
  -> BatchFactory::create_batches
  -> Batch::prepare_forward_input / BatchInputBuilder
  -> LLMEngine::prepare_inputs
  -> WorkerClient::step_async
  -> RawForwardOutput
  -> Batch::process_sample_output
  -> ContinuousScheduler::process_batch_output
  -> Request::generate_output
```

其中 batch 前后两个边界非常清楚：

- **前边界**：scheduler 决定“谁进 batch、每个 sequence 这轮给多少 token budget”。
- **后边界**：engine/worker 执行 forward，batch 负责把结果写回 sequence。

所以可以把 batch 理解成：

> scheduler 的决策落地层 + worker 结果回写层。

## 5. Batch 是怎么从 Request / Sequence 组出来的

### 5.1 scheduler 先决定 running_requests_ / running_sequences_ / running_sequences_budgets_

`ContinuousScheduler::prepare_batch()` 的关键不是直接 new `Batch`，而是先维护三份并行结构：

- `running_requests_`
- `running_sequences_`
- `running_sequences_budgets_`

这三者的关系是：

- `running_requests_` 记录这一轮被选中的 request。
- `running_sequences_` 记录这一轮真正要执行的 sequence。
- `running_sequences_budgets_` 记录每条 sequence 这一轮最多允许处理多少 token。

这正是 batch 的原材料。

### 5.2 prefill 调度时，budget 基本等于“这条 sequence 还没算完的 prompt token 数”

`handle_prefill_requests()` 对每个 sequence 会算：

- `num_tokens = prefill_sequence->num_need_compute_tokens()`

然后在 token budget / seq budget / latency budget / KV block 可分配性都满足时，把：

- sequence 指针塞进 `running_sequences_`
- `num_tokens` 塞进 `running_sequences_budgets_`

因此 prefill 场景下，budget 代表“这轮要把多少个未算 prompt token 喂进去”。

### 5.3 decode 调度时，budget 通常是 `min_speculative_tokens_required_ + 1`

`handle_decode_requests()` 里 decode sequence 的 budget 不是“全部剩余 token”，而是一轮 decode step 要用的 token 数：

- 普通 decode：通常就是 1 个真实 decode token 对应的 step 输入长度。
- speculative decode：会扩成 `min_speculative_tokens_required_ + 1`。

所以 decode 的 budget 含义是：

> 本轮 forward 允许这条 sequence 消耗多少 query token。

### 5.4 BatchFactory 才真正把这些原料组装成按 DP rank 切分的 Batch

`BatchFactory::create_batches()` 的关键逻辑有三层：

1. **按 `sequence->dp_rank()` 分桶**
   - 一条 sequence 在整个生命周期内要求落在同一个 DP rank。

2. **把 `running_sequences_budgets_` 作为 `allowed_max_tokens_` 填进 Batch**
   - 这里 budget 不会丢，后续 `BatchInputBuilder` 要用它来控制 `q_seq_len`。

3. **额外处理 beam search / swap block info**
   - 如果请求开启 beam search，不仅把 active sequence 加进 batch，还会把 `SequencesGroup*` 也加进去。
   - 如果有 swap block transfer info，会按 dp rank 挂到对应 batch 上。

### 5.5 BatchFactory 里有一个很容易忽略但很关键的点：budget 会同时被拆成 prompt 部分和 generated 部分

Factory 在创建 batch 时会统计：

- `remaining_prompt_tokens`
- `prompt_tokens = min(remaining_prompt_tokens, token_budget)`
- `generated_tokens = token_budget - prompt_tokens`

它这样做不是为了影响 batch 内容，而是为了：

- 打埋点
- 区分这轮 batch 到底是偏 prefill 还是偏 decode

这说明 xLLM 对 batch 的理解不是“序列数量”这么简单，而是明确把“本轮计算的是 prompt token 还是 generated token”当作核心统计维度。

## 6. Batch 对象本身有哪些关键内部状态

### 6.1 `sequences_`

这是 batch 最核心的主体，保存本轮真正要执行的 sequence 指针。

### 6.2 `allowed_max_tokens_`

这是最关键的运行时约束之一。它不是 sequence 自己的总上限，而是：

> 这条 sequence 在当前 step 中允许处理的最大 token 数。

后面 `BatchInputBuilder::process_single_sequence()` 会用它计算：

```text
q_seq_len = min(n_tokens - n_kv_cache_tokens, allowed_max_tokens_[seq_index])
```

也就是说，真正进入模型的 query 长度，是 sequence 当前未进入 KV cache 的 token 数和调度预算的最小值。

### 6.3 `batch_forward_type_`

这是 batch 的阶段标签，取值来自 `BatchForwardType`：

- `PREFILL`
- `CHUNKED_PREFILL`
- `DECODE`
- `MIXED`
- `EMPTY`

它不是外部配置，而是 batch 在 `add(sequence)` 过程中根据 sequence 的当前 stage 逐步推导出来的。

### 6.4 `output_targets_`

这是 batch 输出回写的关键中间态，也是最容易被忽略的设计点。

它不表示“所有 sequence 都会产出一个 token”，而是表示：

> 当前 forward 结束后，哪些 sequence 的哪些采样位需要消费模型输出。

这个设计很漂亮，因为它把“输入 flatten 顺序”和“输出回写目标顺序”显式固化下来，避免 batch 输出回写时再重新推导。

### 6.5 `sequence_groups_`

主要用于：

- beam search 后处理
- Rec 特化路径

普通单路 LLM decode 可以只靠 `sequences_`；但 beam/group 语义必须保留这一层。

### 6.6 `batch_id_`

batch id 不是普通 LLM 主链中最核心的概念，但它会被下传到 `RawForwardInput`，用于某些 block transfer / cache 管理 / Rec 路径场景。

## 7. BatchForwardType 是怎么推导出来的

### 7.1 初始值是 `EMPTY`

`Batch` 初始时 `batch_forward_type_` 为空。

### 7.2 `add(sequence)` 时增量更新

`Batch::update_forward_type()` 的逻辑非常值得记住，因为它直接决定模型输入的整体阶段语义：

- 当前为空，遇到第一条 sequence：
  - 直接把 `SequenceStage` 映射成 batch forward type。
- 当前是 `PREFILL`：
  - 再来 `CHUNKED_PREFILL` -> 升成 `CHUNKED_PREFILL`
  - 再来 `DECODE` -> 升成 `MIXED`
- 当前是 `CHUNKED_PREFILL`：
  - 再来 `DECODE` -> 升成 `MIXED`
- 当前是 `DECODE`：
  - 只要再来一个非 decode sequence -> 升成 `MIXED`

### 7.3 这里有一个很妙的实现细节

`SequenceStage` 的枚举值是：

- `PREFILL = 0`
- `CHUNKED_PREFILL = 1`
- `DECODE = 2`

而 `BatchForwardType` 的前三个枚举值恰好也是：

- `PREFILL = 0`
- `CHUNKED_PREFILL = 1`
- `DECODE = 2`

所以在 `EMPTY` 状态下，代码直接 `BatchForwardType(static_cast<int32_t>(stage))` 就完成了映射。这说明这两个枚举在设计上是故意对齐的。

## 8. Batch 的输入构建：`BatchInputBuilder` 才是 batch 逻辑的核心

如果说 `Batch` 是门面，那么 `BatchInputBuilder` 就是 batch 的真正“干活层”。

### 8.1 它负责把“面向对象的 Sequence 状态”压平成“面向模型的张量输入”

builder 内部维护的 `BuilderState` 很能说明问题，它收集的是：

- flatten tokens / positions
- mrope positions
- sampling params / selected token idxes / sample idxes
- unique token ids / counts / lens
- seq_lens / q_seq_lens / kv_cache_tokens_nums
- new_token_slot_ids
- block_tables_vec
- transfer_kv_infos
- embedding_ids / request_ids / extra_token_ids
- paged kv 结构
- beam search acc logprob

这说明 builder 做的事不是单纯拼 token，而是一次性把：

1. token 视图
2. KV cache 视图
3. sampling 视图
4. 多模态视图
5. DP / beam / transfer 附加视图

全部聚合成模型输入。

### 8.2 builder 的总入口很简单，但内部很重

两个主入口：

- `build_forward_input()`：构造本地 `ForwardInput`
- `build_raw_forward_input()`：构造可序列化的 `RawForwardInput`

两者共享前半段逻辑：

1. `process_sequences()`
2. 再把内部 `state_` 转成具体输出结构

这说明 xLLM 把“构建逻辑”和“最终载体格式”拆开了。

### 8.3 `process_sequences()` 支持多线程构建

当 sequence 数量大于等于 thread pool 大小时，会走 `process_sequences_multithreaded()`。

这里的设计重点不是“并行”，而是**并行后还能保证索引语义不乱**。因为 sampling 相关索引是基于 flatten 结果的，所以线程局部状态在 merge 时必须修正：

- `selected_token_idxes` 需要加上 flatten token 偏移
- `sample_idxes` 需要加上 sample 项偏移
- `seq_lens / q_seq_lens / paged_kv_indptr` 也要做前缀偏移拼接

这块不是细枝末节，而是 batch builder 是否正确的核心。`batch_test.cpp` 里专门有 `SampleRequestKeepsThreadedRawBuilderOffsetsStable` 来验证这一点。

## 9. 单条 Sequence 在 builder 中是怎么被处理的

`process_single_sequence()` 是整个 batch 输入构建最关键的函数。

### 9.1 先算四个长度变量

对每条 sequence，builder 会先拿到：

- `n_tokens = sequence->tokens().size()`
- `n_kv_cache_tokens = sequence->kv_state().kv_cache_tokens_num()`
- `q_seq_len = min(n_tokens - n_kv_cache_tokens, allowed_max_tokens_[seq_index])`
- `seq_len = n_kv_cache_tokens + q_seq_len`

这四个量的含义分别是：

- `n_tokens`：sequence 当前总 token 数
- `n_kv_cache_tokens`：已经被 KV cache 覆盖的 token 数
- `q_seq_len`：这轮真正要送模型算的 query token 数
- `seq_len`：这轮执行后，逻辑上将覆盖到的 token 边界

### 9.2 这里的约束非常严格

builder 会检查：

- `allowed_max_tokens_[seq_index] > 0`
- `current_max_tokens_capacity >= seq_len`
- `q_seq_len > 0`

这意味着：

> scheduler 不能把一条“这一轮根本没有 token 可算”的 sequence 塞进 batch。

### 9.3 builder 会先更新“本轮全局元数据”

例如：

- `max_seq_len`
- `q_max_seq_len`
- `kv_cache_tokens_nums`
- `seq_lens`
- `q_seq_lens`

注意这里的 `seq_lens / q_seq_lens` 在不同设备后端上布局不同：

- NPU/MUSA 用逐条长度向量
- CUDA/MLU/ILU 用 prefix-sum 风格 cu-seqlen

这说明 batch builder 本身已经承担了部分“设备相关输入布局适配”的工作。

## 10. token / position 是怎么从 Sequence 中抽出来的

### 10.1 只抽本轮尚未进入 KV cache 的 token

`extract_tokens_and_positions()` 会遍历：

```text
for j in [n_kv_cache_tokens, seq_len)
```

所以 batch 不会把整条 sequence 全量塞进模型，而只会抽本轮需要计算的那一段。这正是 continuous batching 成立的基础。

### 10.2 普通位置编码和 mrope 走两条路径

- 普通模型：直接把位置 `j` 放进 `flatten_positions_vec`
- mrope 模型：用 `MPositionHelper` 生成三路位置张量

其中 mrope 路径会结合 `MMData`、图像/视频网格、文本/视觉 token 交错结构来生成位置，因此 batch builder 不只是做 token flatten，它还承担了多模态位置编码准备工作。

### 10.3 `extra_token_ids` 是 chunked prefill / speculative decoding 的辅助输入

这部分很容易被忽略。逻辑是：

- 如果当前 `seq_len == n_tokens`，说明这是最后一个 prefill chunk 或 decode 步，填 `-1`
- 否则填 `token_ids[seq_len]`

也就是说，builder 会额外把“下一跳 token”或者“没有下一跳”的标记带下去。这是 chunked prefill / speculative decode 路径的辅助信息。

## 11. sampling 相关索引是怎么建立的

batch 逻辑里最容易读漏的一块，就是 `selected_token_idxes` / `sample_idxes` / `output_targets_` 三者的配合。

### 11.1 非 sample request：通常只对最后一个可采样位置建一个采样点

当 `sample_slots` 为空时：

- builder 只在 `j + 1 == n_tokens` 时调用 `handle_sampling_parameters()`

这背后的语义是：

> 普通生成请求只需要从“最后一个 token 对应的 hidden state”上取 logits，并采出下一 token。

所以非 sample request 通常一条 sequence 只会产生一个采样目标。

### 11.2 sample request：一条 sequence 可以在一轮里注入多个采样点

当有 `sample_slots` 时，builder 会把每个 slot 的 `token_position` 映射成 `sample_source_position`：

- `token_position == 0` -> 0
- 否则 -> `token_position - 1`

然后只要这个位置落在当前 `[n_kv_cache_tokens, seq_len)` 范围内，就会为它追加一个 sampling entry。

这意味着 sample request 的本质不是“多条 sequence”，而是：

> 同一条 sequence 上，同一轮可能要求多个位置的 hidden state 进入 sampler。

### 11.3 `handle_sampling_parameters()` 真正写入了什么

每命中一个采样位，builder 会写入：

- `selected_token_idxes`
- `sampling_params`
- `sample_idxes`
- `unique_token_ids_vec`
- `unique_token_counts_vec`
- `unique_token_lens_vec`

这里的 unique token 统计来自 `Sequence::token_to_count_map()`，说明 sampler 相关的 repetition/frequency/presence 类逻辑所需的序列 token 频次，已经在 batch 输入构建阶段准备好了。

### 11.4 `batch_test.cpp` 明确验证了 sample request 的这套语义

测试覆盖了：

- 一个 sequence 命中多个 sample slot
- 多线程 builder 合并后偏移仍然正确
- RawForwardOutput 少于 sample slot 数量时，会补 empty placeholder

这说明 sample request 不是顺手加上的边缘逻辑，而是 batch 层明确支持的一类一等公民路径。

## 12. KV cache 相关输入是怎么建立的

### 12.1 builder 在构建输入时就会前推 KV bookkeeping

`setup_kv_cache_info()` 一上来就调用：

```text
sequence->kv_state().incr_kv_cache_tokens_num(q_seq_len)
```

这意味着：

> batch 在发出 forward 前，就已经把“本轮会写入多少 KV token”登记进了 sequence 的 KV 状态。

这不是 bug，而是为了让后续 slot/block 计算、stage 推导、连续 step 调度保持一致。

### 12.2 new token slot / block table 是本轮最重要的 KV 输入

builder 会从 sequence 的 KV state 里取：

- `kv_blocks()`
- `kv_cache_slots(n_kv_cache_tokens, seq_len)`

然后构建：

- `new_token_slot_ids`
- `block_tables_vec`

可以把它理解成：

- `block_tables_vec`：这条 sequence 当前能访问哪些 KV blocks
- `new_token_slot_ids`：这轮新 token 应该写进哪些具体 slot

### 12.3 paged KV / flashinfer 所需元数据也在这里一起准备

builder 同时还会组：

- `paged_kv_indptr`
- `paged_kv_indices`
- `paged_kv_last_page_len`

这说明 batch builder 既服务于通用 block table 路径，也服务于 paged KV / flashinfer 风格后端。

### 12.4 transfer_kv_info / swap block 也挂在 batch 输入里

如果 sequence 的 KV state 里存在 `transfer_kv_info`，builder 会把它一并放进 `transfer_kv_infos`。

此外，如果 batch 上挂了 `swap_block_transfer_infos_`：

- 默认直接塞到 `swap_blocks`
- 如果开启 `enable_block_copy_kernel`，会重排成 `src_block_indices / dst_block_indices / cum_sum`

这说明 batch 输入不仅描述“正常 forward 读什么”，还可以描述“本轮前后伴随哪些 block copy / swap 行为”。

## 13. 为什么 chunked prefill 中间 chunk 不应该立刻产出 token

这是 batch 里一个非常容易读错、但非常关键的点。

### 13.1 output target 生成时，普通 sequence 只有在 `seq_len == n_tokens` 时才建输出目标

`refresh_output_targets()` 对没有 `sample_slots` 的 sequence，只在：

```text
if (seq_len == n_tokens)
```

时才 push 一个 target。

也就是说：

- 如果当前只是 chunked prefill 的中间块，`seq_len < n_tokens`
- 那么这一轮虽然会做 forward、会写 KV cache
- 但**不会把它视作“应该采样出一个新 token 的 step”**

### 13.2 这和 `Sequence::append_token()` 的合法性判断是配套的

`Sequence::append_token()` 对非 OneRec 路径有一个强约束：

- 不能对 prefill sequence append token

所以 batch 层必须保证：

> 只有真正处于可 decode 的时刻，或者 sample request 的合法采样位，才会消费模型输出并回写 token。

### 13.3 `update_sequence_state()` 进一步处理了 chunked prefill + overlap 的交互

当 `FLAGS_enable_chunked_prefill` 打开时：

- 第一阶段如果 sequence 仍处于 chunked prefill，会把一个 `true` 压入 `pre_scheduled_step_prefill_queue()` 并跳过 token 回写
- 第二阶段如果 `replace_fake_token == true` 且队列头为 `true`，也会直接 pop 并跳过

它的核心目的是：

> 在 chunked prefill 和 schedule overlap 叠加时，避免把“prefill 相关 forward”误当成真正的 decode token 产出。

## 14. `output_targets_` 是 batch 输出回写的总索引表

`refresh_output_targets()` 的意义可以总结成一句话：

> 在 forward 前，把“本轮输出应该写回哪些 sequence、以什么逻辑写回”预先算好。

`OutputTarget` 结构里有三个字段：

- `sequence`
- `sample_id`
- `from_sample_slot`

这里面最重要的是 `from_sample_slot`：

- `false`：普通生成语义
- `true`：sample request 的 slot 输出语义

这会直接影响后面的缺失输出处理和 finish/placeholder 逻辑。

## 15. Batch 输出回写：`process_sample_output()` 是第二个核心

batch 的另外一半灵魂在输出回写。

它有两个重载：

- `process_sample_output(const RawForwardOutput&, bool replace_fake_token)`
- `process_sample_output(const SampleOutput&, bool replace_fake_token)`

含义分别是：

- `RawForwardOutput`：分布式/远程 worker 主链
- `SampleOutput`：本地张量路径

两者逻辑本质一致，都是按照 `output_targets_` 顺序把输出分发回 sequence。

### 15.1 回写流程的骨架

对每个 `output_target`：

1. 找到目标 sequence
2. 如果不是 sample slot 路径，先检查 sequence 是否 finished
3. 调用 `update_sequence_state()` 处理 chunked prefill / overlap 特例
4. 如果当前 output 缺失或为空：
   - 若来自 sample slot，则补一个 empty placeholder
   - 否则跳过
5. 如果 output 存在：
   - 构造 `Token`
   - 调 `append_token_for_sequence()` 或 overlap 下的 replace 逻辑
6. 必要时更新 embeddings

### 15.2 为什么 sample slot 缺输出时要补 placeholder

对 sample request，如果某个 slot 在这轮没有对应输出，batch 会调用：

- `make_empty_logprob_placeholder()`

它会构造一个占位 token，并让后续 `Sequence::generate_sample_outputs()` 能产出带 `kEmptyLogprobsFinishReason` 的输出。

这保证了 sample request 的输出索引不会乱，也不会因为部分 slot 缺失导致回写和最终返回数量对不上。

### 15.3 embeddings 和 mm_embeddings 也在 batch 层回写

这点很重要：batch 不只是处理 token。

- `raw_output.mm_embeddings` 会按 sequence 的 multimodal item 数拆回每条 sequence
- `raw_token.embeddings` 会更新 sequence 的 embedding 输出
- `SampleOutput.embeddings` 也会回写到 sequence

因此 batch 其实统一承接了：

- 生成 token 输出
- embedding 输出
- multimodal embedding 输出

## 16. schedule overlap 下，batch 为什么会先写 fake token 再替换成 real token

这是 xLLM batch 逻辑里最值得认真理解的高级点之一。

### 16.1 engine 第一次回写时总是传 `replace_fake_token = false`

在 `LLMEngine::step()` 里：

- 普通模式下，这意味着“直接 append 真 token”
- overlap 模式下，这意味着“先 append fake token”

`batch.cpp` 的注释已经明确写出：

> overlap 模式把 forward 分成两个阶段，第一阶段先填 fake token，第二阶段再用 real token 替换。

### 16.2 第二阶段由 `update_last_step_result()` 驱动

当开启 overlap 时：

- `ContinuousScheduler` 先调度下一批
- 然后 `engine_->update_last_step_result(last_batch_)`
- 这时 `Batch::process_sample_output(..., true)` 会用真实结果替换上轮 fake token

### 16.3 `append_token_for_sequence()` 对两个阶段的行为完全不同

- `replace_fake_token == false`
  - 直接 `seq->append_token(token)`
- `replace_fake_token == true`
  - 不新增 token，而是 `seq->update_last_step_token(token, token_idx)`

也就是说 overlap 并不是“多一次 append”，而是：

> 先占位，再原地修正。

### 16.4 为什么 `output_targets_` 在 overlap 第一阶段不能立刻清掉

batch 只有在 `replace_fake_token == true` 时才清空 `output_targets_`。

原因很直接：

> 第二阶段替换 real token 仍然需要依赖第一阶段已经建立好的 output target 映射。

`batch_test.cpp` 里的 `KeepTargetsForOverlapReplacement` 专门验证了这一点。

## 17. beam search 在 batch 层是怎么处理的

beam search 是 batch 逻辑里最复杂的分支之一。

### 17.1 为什么 beam search 需要 `sequence_groups_`

因为 beam search 的后处理不是单条 sequence 就能做完的，它必须知道：

- 同一组 beam 的其它 sequence 是谁
- 哪些 sequence 是从同一个源 sequence 派生的
- 哪些 KV block 可以复用，哪些要 swap

所以 `BatchFactory::create_batches()` 在发现 request 开启 beam search 时，会额外把 `SequencesGroup*` 放进 batch。

### 17.2 有两条 beam 路径：硬件 kernel 输出和软件 merge

1. **硬件 beam search kernel 路径**
   - `RawForwardOutput` 会携带 `src_seq_idxes / out_tokens / out_logprobs`
   - `Batch::process_beam_search_output()` 根据这些结果把 beam 回写回 base sequence

2. **软件 beam merge 路径**
   - `Batch::process_beam_search()` -> `SequencesGroup::process_beam_search()`
   - 在 group 内按 top-k 候选重组 sequence

### 17.3 partial finished beam group 会回退到软件 merge

如果一个 beam group 里已经部分 finished，`Batch::prepare_forward_input(const ModelArgs&, ThreadPool*)` 会检测到：

- `has_partial_finished_beam_group() == true`

然后把 `raw_input.acc_logprob_vec.clear()`。

这背后的语义是：

> 硬件 beam kernel 假设每组 beam width 固定且活跃度一致；一旦一组里只有部分 beam 还活着，就退回软件 merge，避免语义错乱。

### 17.4 beam merge 后为什么还需要 `refresh_sequences_from_groups()`

因为 `SequencesGroup::process_beam_search()` 在 group 内部可能会直接替换 `unique_ptr<Sequence>` 容器，导致 `Batch::sequences_` 里原来的裸指针失效。

所以 Rec/beam 路径需要显式 refresh，重新从 group 把最新 sequence 指针拉平出来。

## 18. DP 与 micro-batch 语义

### 18.1 BatchFactory 输出的是“按 DP rank 切开的多个 Batch”

`BatchFactory::create_batches()` 返回的是：

```text
std::vector<Batch> batches(dp_size)
```

也就是说，对 engine 来说，一轮 step 实际上不是一个 batch，而是一组按 DP rank 拆分后的 micro-batch。

### 18.2 `LLMEngine::prepare_inputs()` 会逐个 DP batch 调 `Batch::prepare_forward_input()`

这一步完成后，每个 dp rank 拿到一个 `RawForwardInput`。

之后 engine 还会补两类全局 DP 元数据：

- `dp_global_token_nums`
- `dp_is_decode`

这意味着 worker 侧不只知道“我这张卡本地要算什么”，还知道整个 DP 组的 token 分布和 decode 标记。

### 18.3 空 batch 的 forward type 会被对齐到首个非空 batch

engine 会扫描第一个非空 batch 的 `batch_forward_type`，并把空 batch 也对齐到同类型。

其中还有一个细节：

- 如果首个非空 batch 是 `CHUNKED_PREFILL`
- engine 会把它对齐成 `PREFILL`

这说明在 DP 级别的一些控制语义里，chunked prefill 被更接近地看作 prefill 大类。

## 19. NPU 特化：`dp_balance_shuffle_seqs()`

这是 batch 里一个很容易漏掉的硬件优化。

### 19.1 触发条件

只在下面条件同时成立时触发：

- `USE_NPU`
- `FLAGS_enable_customize_mla_kernel`
- `FLAGS_enable_dp_balance`
- `sequences_.size() > 24`

### 19.2 它做什么

它按 sequence 当前 `kv_cache_tokens_num()` 长短，把序列重新打散到 24 个 NPU core 上，采用“长短交错”的蛇形分布方式，以获得更均衡的核间负载。

这说明在 xLLM 里，batch 并不只是逻辑聚合对象，它还会针对特定硬件做 batch 内部重排优化。

## 20. 本地 executor 路径和远程 worker 路径

### 20.1 本地路径

`BaseExecutorImpl::prepare_inputs()` 直接调用：

- `batch.prepare_forward_input(num_decoding_tokens, min_decoding_batch_size, args_)`

它得到的是本地 `ForwardInput`，里边已经是 tensor 形式。

### 20.2 远程 worker 路径

`LLMEngine::prepare_inputs()` 调的是：

- `batch.prepare_forward_input(args_, threadpool_.get())`

它得到的是 `RawForwardInput`，随后会经由 `params_utils` 转成 protobuf，再通过 `RemoteWorker::step_async()` 送到 worker。

所以 `Batch` 的设计并没有把自己绑死在“本地 eager tensor”或者“远程 RPC”其中一种模式上，而是同时适配了两种执行形态。

## 21. Batch 最终如何回到 RequestOutput

Batch 并不会直接生成 `RequestOutput`，它只负责把结果回写进 `Sequence`。

真正生成 `RequestOutput` 的是后面的 request/group 层。

### 21.1 scheduler 的 `process_batch_output()` 主要负责两个动作

1. 更新 token latency / memory metrics
2. 决定哪些 request 需要交给 response processor 做 streaming 输出

也就是说 scheduler 在这一层仍然没有“重新拼结果”，它只是驱动回调时机。

### 21.2 `Request::generate_output()` 才真正把 sequence 状态转成返回值

它会：

- 汇总 usage
- 处理 overlap 模式下多算一步的问题
- 调 `sequences_group_->generate_outputs()`

### 21.3 sample request 最终返回不是 1 个 output，而是按 sample slot 展开的多个 output

`Sequence::generate_sample_outputs()` 会按 `sample_slots` 个数逐个生成 output，并把 `sample_id` 写进 `SequenceOutput.index`。

`SequencesGroup::generate_outputs()` 最后还会按 index 做稳定排序。

这就把 batch 阶段“一个 sequence 多个 output target”的语义完整延续到了最终 `RequestOutput`。

## 22. Rec / DiT 在 batch 层的特化

### 22.1 Rec 路径不是重用普通 `BatchInputBuilder`，而是走 `RecBatchInputBuilder`

当 `Batch` 发现：

- `sequences_.empty()`
- `!sequence_groups_.empty()`

就会走 `prepare_rec_forward_input()`，根据 `RecType` 选择：

- `OneRecBatchInputBuilder`
- `RecMultiRoundBatchInputBuilder`

这说明框架层对 “LLM sequence batch” 和 “Rec group batch” 明确分流。

### 22.2 `create_rec_batches()` 和普通 `create_batches()` 的差别

Rec 工厂不会像普通 LLM 那样先把 active sequence 全塞进去，而是更偏向 group 级组织，并且会设置 `batch_id`。

这说明 Rec 路径下 batch 的主处理对象更像 `SequencesGroup`，而不是普通 LLM 那种“裸 sequence 列表”。

### 22.3 DiTBatch 是另一种更轻的 batch 变体

`DiTBatch` 的语义和 LLM Batch 明显不同：

- 它持有的是 `std::shared_ptr<DiTRequest>`
- 主要做 prompt/image/latent/control image 等张量 stack
- forward output 回来后按 request 顺序直接 `handle_forward_output`

所以在 xLLM 里，“batch”是一个框架概念，但不同任务类型有不同的数据骨架：

- LLM：Sequence-centric
- Rec：Group-centric
- DiT：Request-centric

## 23. 一次 step 前后，对象状态到底怎么变

这一节用一个更贴近运行时的方式，把 batch 主链重新串一次。

### 23.1 step 前

一个 sequence 可能处于三种状态之一：

```text
PREFILL           kv_cache_tokens_num == 0
CHUNKED_PREFILL   0 < kv_cache_tokens_num < num_prompt_tokens
DECODE            kv_cache_tokens_num >= num_prompt_tokens
```

scheduler 选出：

- 哪些 request 参与本轮
- 每个 request 下哪些 sequence 参与本轮
- 每条 sequence 本轮 budget 是多少

### 23.2 组 batch

`BatchFactory`：

- 按 dp rank 把 sequence 放进不同 batch
- 把 budget 写进 `allowed_max_tokens_`
- beam/rec 场景补 `sequence_groups_`

### 23.3 batch 组输入

`BatchInputBuilder`：

- 根据 `n_tokens / n_kv_cache_tokens / allowed_max_tokens` 算本轮 `q_seq_len`
- flatten token / position
- 更新 KV slot / block table
- 建 sampling 索引
- 生成 `ForwardInput` 或 `RawForwardInput`

### 23.4 worker 执行

engine 把按 dp 切开的 batch fan-out 到 worker，worker 返回：

- token 结果
- logprob / top logprobs
- embedding / mm embedding
- beam search 输出

### 23.5 batch 回写 sequence

`Batch::process_sample_output()`：

- 依赖 `output_targets_` 找回写目标
- 处理 fake token / real token 替换
- 更新 sequence 的 token、logprob、embedding、finish 状态

### 23.6 request 输出层消费 sequence 状态

后续 `Request::generate_output()` / response processor：

- 把 sequence 状态翻译成 streaming/non-stream 输出
- 最终返回给客户端

## 24. 我认为最值得记住的 12 个关键结论

1. `Batch` 不是长期对象，而是一次 step 的执行包装层。
2. xLLM 真正调度进入 batch 的粒度是 `Sequence`，不是 `Request`。
3. `allowed_max_tokens_` 是 batch 的核心约束，它直接决定本轮 `q_seq_len`。
4. `BatchForwardType` 不是配置项，而是由 sequence stage 动态推导出来的。
5. `BatchInputBuilder` 才是 batch 逻辑的重心，负责把对象状态压平成模型输入。
6. batch 只处理“本轮未进入 KV cache 的 token”，不会重复送全序列。
7. `output_targets_` 是 batch 输出回写的总索引表，是 sample request / overlap 正确性的关键。
8. chunked prefill 的中间 chunk 不应该直接产出 decode token，这在 output target 生成和回写逻辑中被显式保证。
9. overlap 模式不是多生成一个 token，而是“先写 fake token，再原地替换 real token”。
10. beam search 之所以复杂，是因为 batch 不得不同时维护 sequence 级执行语义和 group 级合并语义。
11. batch 已经内置了硬件/后端适配责任，例如 NPU 的 DP balance、paged KV、block copy kernel 数据布局。
12. batch 本身不生成 `RequestOutput`，它只负责把 worker 输出准确地落回 `Sequence`，再交给 request/group 层生成最终结果。

## 25. 一张总图

```text
RequestState
  -> Request
      -> SequencesGroup
          -> Sequence(真实执行单元)

ContinuousScheduler
  -> running_requests_
  -> running_sequences_
  -> running_sequences_budgets_

BatchFactory
  -> Batch(dp0)
  -> Batch(dp1)
  -> ...

Batch / BatchInputBuilder
  -> flatten_tokens
  -> positions / mrope_positions
  -> selected_token_idxes / sample_idxes
  -> kv slots / block tables / paged kv
  -> embeddings / mm_data / transfer_kv / beam info

LLMEngine
  -> prepare_inputs
  -> worker_clients.step_async

RawForwardOutput / SampleOutput
  -> Batch::process_sample_output
  -> Sequence token/logprob/embedding/finish 更新

ContinuousScheduler / ResponseProcessor
  -> Request::generate_output
  -> RequestOutput
```

## 26. 本次学习的边界与后续建议

这次我已经把 framework 中 batch 的通用主链、sample request、beam search、schedule overlap、KV 输入构建、DP 拆分这几条关键逻辑追清楚了。

但也要明确边界：

- `OneRecBatchInputBuilder` 和 `RecMultiRoundBatchInputBuilder` 我这次只追到了“入口分流与职责边界”，没有逐字段展开每个 Rec 专用 tensor 的布局细节。
- `DiTBatch` 也只做了对照阅读，没有展开到图像生成任务内部的全部 forward 参数语义。

如果后续继续学，我建议自然的下一步是：

1. 专门补一篇 `Request / Sequence / SequencesGroup` 的状态演化笔记。
2. 再单独深入 `ContinuousScheduler`，把 `running_sequences_budgets_`、preemption、latency-aware scheduling、chunked prefill 变体彻底吃透。
3. 如果任务重点偏 Rec，再专门拆 `OneRecBatchInputBuilder` / `RecMultiRoundBatchInputBuilder`。

