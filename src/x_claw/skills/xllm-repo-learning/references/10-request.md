# xLLM framework 中 request / sequence / sequences_group 逻辑学习笔记

> 说明：用户问题里写的是 `ralph/learn_xllm/overview.md` 和 `ralph/learn_xllm/structure.md`，但仓库里对应的实际文件名是 `ralph/learn_xllm/0-overview.md` 和 `ralph/learn_xllm/1-structure.md`。本文以这两份学习笔记为入口，再结合源码把 xLLM 里 `request / sequence / sequences_group` 这一条主链尽量完整地吃透。

## 1. 本次学习范围

这次学习不只是解释三个类的字面定义，而是把下面这条链路一起打通：

```text
外部 HTTP/brpc 请求
  -> RequestParams
  -> LLMMaster::handle_request()
  -> LLMMaster::generate_request()
  -> RequestState
  -> Request
  -> SequencesGroup
  -> Sequence
  -> ContinuousScheduler
  -> Batch / LLMEngine
  -> Sequence 回写
  -> RequestOutput
  -> service callback / stream callback
```

也就是说，本文重点回答下面几个问题：

1. xLLM 内部真正“被调度”的对象到底是什么。
2. `Request`、`Sequence`、`SequencesGroup` 为什么要拆成三层，而不是一个大对象。
3. 一个请求从服务层变成内部调度对象后，状态是如何一步一步收敛的。
4. 多路生成（`n` / `best_of`）、beam search、streaming、schedule overlap 等语义，是落在 request 级、sequence 级，还是 group 级。

## 2. 本次实际阅读的文件

### 2.1 学习入口

- `ralph/learn_xllm/0-overview.md`
- `ralph/learn_xllm/1-structure.md`
- `ralph/learn_xllm/plan.md`
- `ralph/learn_xllm/task.md`

### 2.2 服务入口与请求构造

- `xllm/api_service/completion_service_impl.cpp`
- `xllm/api_service/chat_service_impl.cpp`
- `xllm/core/framework/request/request_params.h`
- `xllm/core/distributed_runtime/llm_master.h`
- `xllm/core/distributed_runtime/llm_master.cpp`

### 2.3 request / sequence / group 主体

- `xllm/core/framework/request/request_base.h`
- `xllm/core/framework/request/request.h`
- `xllm/core/framework/request/request.cpp`
- `xllm/core/framework/request/request_state.h`
- `xllm/core/framework/request/request_state.cpp`
- `xllm/core/framework/request/request_output.h`
- `xllm/core/framework/request/sequence.h`
- `xllm/core/framework/request/sequence.cpp`
- `xllm/core/framework/request/sequences_group.h`
- `xllm/core/framework/request/sequences_group.cpp`
- `xllm/core/framework/request/stopping_checker.h`
- `xllm/core/framework/request/stopping_checker.cpp`
- `xllm/core/framework/request/finish_reason.h`
- `xllm/core/framework/request/finish_reason.cpp`
- `xllm/core/framework/request/sequence_kv_state.h`
- `xllm/core/framework/request/sequence_logprob_state.h`

### 2.4 调度、batch、执行与输出回写

- `xllm/core/scheduler/scheduler.h`
- `xllm/core/scheduler/continuous_scheduler.h`
- `xllm/core/scheduler/continuous_scheduler.cpp`
- `xllm/core/scheduler/async_response_processor.h`
- `xllm/core/scheduler/async_response_processor.cpp`
- `xllm/core/framework/batch/batch.h`
- `xllm/core/framework/batch/batch.cpp`
- `xllm/core/framework/batch/batch_factory.h`
- `xllm/core/framework/batch/batch_factory.cpp`
- `xllm/core/distributed_runtime/llm_engine.cpp`

---

## 3. 先给结论：三层对象到底是怎么分工的

如果只用一句话概括，我现在的结论是：

> **`Request` 是请求级控制对象，`Sequence` 是真正的执行轨迹对象，`SequencesGroup` 是同一请求下多条轨迹的组织器。**

再展开一点：

| 对象 | 主要角色 | 典型职责 | 是否真正进 batch |
| --- | --- | --- | --- |
| `RequestParams` | 服务层外部请求快照 | 承接 OpenAI/brpc/HTTP 请求参数 | 否 |
| `RequestState` | `Master` 构造出的内部请求快照 | 把 prompt、sampling、scheduler、callback 等归一化 | 否 |
| `Request` | 调度器管理对象 | 生命周期、取消、统计、输出聚合、持有 group | 间接 |
| `SequencesGroup` | 同一 request 下的多序列容器 | `best_of` / `n` / beam / 输出选优 | 间接 |
| `Sequence` | 真正的 token 生成单元 | token、KV、finish、decoder、logprob、阶段推进 | 是 |
| `Batch` | 一次 step 的执行包 | 把 sequence 变成 forward 输入，并把结果回写 | 是 |

这几个对象的关键分层可以概括成一句非常重要的话：

> **xLLM 以 `Request` 为管理单位，以 `Sequence` 为执行单位，以 `SequencesGroup` 为多轨迹语义单位。**

这也是 `0-overview.md` 和 `1-structure.md` 里“scheduler 收的是 request，但真正执行的是 sequence”那句话在源码里的具体落点。

---

## 4. 从外部请求到内部 `Request`：链路先立住

如果不先把“请求是怎么变成 `Request` 的”讲清楚，后面解释 `Sequence` 很容易只停留在类定义层面。

### 4.1 服务层先把协议请求压缩成 `RequestParams`

以 `CompletionServiceImpl::process_async_impl()` 和 `ChatServiceImpl::process_async_impl()` 为代表，服务层的主线是：

1. 根据 `model` 找到对应 `LLMMaster`。
2. 做 rate limit / sleep 状态检查。
3. 把协议请求转成 `RequestParams`。
4. 如果开启 routing，则补 `prompt_tokens` 与 `decode_address`。
5. 构造 callback。
6. 调用 `master->handle_request(...)`。

也就是说，服务层此时还没有 `Request`、`Sequence` 这些对象。它持有的只是：

- prompt 或 messages
- 归一化后的 `RequestParams`
- 用于回写 brpc/HTTP 的 callback / `Call*`

### 4.2 `LLMMaster::handle_request()` 并不立即执行请求

`LLMMaster::handle_request()` 做的事情非常关键：

1. `scheduler_->incr_pending_requests(1)`
2. 把请求扔到 request-handling thread pool
3. 在线程池里：
   - `sp.verify_params(callback)`
   - 调 `generate_request(...)`
   - 成功后 `scheduler_->add_request(request)`

所以这里已经形成了明确分层：

- 服务层负责“外部协议请求 -> `RequestParams`”
- `LLMMaster` 负责“`RequestParams` -> `Request`”
- Scheduler 负责“`Request` -> `Batch`”

### 4.3 `LLMMaster::generate_request()` 才是内部请求对象的真正构造点

`generate_request()` 做的事情非常多，它不是简单地 new 一个对象：

1. prompt tokenize，得到 `local_prompt_tokens`
2. 检查 prompt 长度不能超过 `max_context_len`
3. 计算 `max_tokens` / `effective_max_tokens`
4. 计算 sequence capacity：
   - `prompt_tokens`
   - `max_tokens`
   - `speculative tokens`
   - bonus token
   - schedule overlap 额外空间
5. 构造 `RequestSamplingParam`
6. 构造 `SchedulerParam`
7. 构造 `StoppingChecker`
8. 检查 prompt 自己是否已经触发 stop/eos/length
9. 若 `best_of != n`，强制关闭 streaming
10. 构造 `RequestState`
11. 用 `RequestState` 构造 `Request`

这里其实已经能看出一个很重要的设计意图：

> **`Request` 不是直接保存服务层原始请求，而是保存一份已经“可调度化”的内部快照。**

---

## 5. `RequestParams`、`RequestState`、`Request` 三者的区别

这是我这次学习里最先要澄清的地方，因为很多时候容易把这三层混在一起。

### 5.1 `RequestParams`：服务层视角的请求

`RequestParams` 仍然明显带着“外部 API 语义”的痕迹，比如：

- `request_id`
- `service_request_id`
- `source_xservice_addr`
- `streaming`
- `max_tokens`
- `n`
- `best_of`
- `echo`
- `frequency_penalty`
- `temperature`
- `top_p`
- `top_k`
- `stop` / `stop_token_ids`
- `decode_address`
- `tools`
- `chat_template_kwargs`
- `offline`
- `ttft_slo_ms` / `tpot_slo_ms` / `ttlt_slo_ms`
- `beam_width`
- `sample_slots`

也就是说，`RequestParams` 还是“接口参数”层面的对象。

### 5.2 `RequestState`：内部调度/输出层视角的请求快照

`RequestState` 代表 `LLMMaster` 归一化之后的内部请求状态。它最大的特点是：

1. 它不再强调原始协议格式，而强调执行与输出需要什么；
2. 它是 `Request` 的核心载荷；
3. 它会被进一步压缩成 `SequenceParams`。

可以把 `RequestState` 看成四类信息的组合：

#### A. 生成控制信息

- `RequestSamplingParam sampling_param`
- `StoppingChecker stopping_checker`
- `size_t seq_capacity`
- `size_t n`
- `size_t best_of`
- `bool logprobs`

#### B. 调度控制信息

- `SchedulerParam scheduler_param`
- `bool enable_schedule_overlap`
- `bool preempted`
- `int32_t response_thread_id`
- `bool handle_last_token_done`

#### C. 输出与回调信息

- `bool stream`
- `bool echo`
- `bool skip_special_tokens`
- `OutputFunc output_func`
- `OutputsFunc outputs_func`
- `std::optional<Call*> call_`

#### D. 输入载荷信息

- `std::string prompt`
- `std::vector<int32_t> prompt_tokens`
- `torch::Tensor input_embedding`
- `MMData mm_data`
- `RecType rec_type`
- `int32_t bos_token_id`
- `std::vector<SampleSlot> sample_slots`

### 5.3 `Request`：真正进入 scheduler 管理域的对象

`Request` 继承自 `RequestBase`，在 `RequestBase` 里保存了：

- `request_id_`
- `service_request_id_`
- `source_xservice_addr_`
- `x_request_id_`
- `x_request_time_`
- `created_time_`

而 `Request` 自己真正新增的是：

- `RequestState state_`
- `std::unique_ptr<SequencesGroup> sequences_group_`
- `cancelled_`
- deadline / urgency / starved 等请求级调度辅助状态

所以 `Request` 本质上是：

> **“请求级身份信息 + 内部状态快照 + 多序列容器 + 请求级生命周期方法”**

---

## 6. `Request` 的职责：它不是生成器，而是“请求级控制器”

### 6.1 `Request` 构造时就会创建 `SequencesGroup`

`Request` 构造函数里最关键的一步是 `create_sequences_group()`。

这里它会把 `RequestState` 进一步压缩成 `SequenceParams`，然后构造 `SequencesGroup`。

压缩进去的字段包括：

- `seq_capacity`
- `skip_special_tokens`
- `echo`
- `logprobs`
- `n`
- `best_of`
- `streaming`
- `enable_schedule_overlap`
- `rec_type`
- `bos_token_id`
- `request_id`
- `sample_slots`
- `sampling_param`
- `stopping_checker`

这里有个非常值得注意的实现细节：

- `sample_slots`
- `sampling_param`
- `stopping_checker`

这三个并没有被深拷贝，而是以指针方式指向 `RequestState` 里的对象。

也就是说，`Sequence` 后续访问 sampling/stop/sample slot，本质上还是通过 `Request` 持有的 `RequestState` 来读。

这说明 xLLM 这里隐含了一个明确约束：

> **`Request` 必须始终活得比它内部的 `Sequence` 更久。**

从当前设计看，这是成立的，因为 scheduler 始终以 `shared_ptr<Request>` 管请求生命周期。

### 6.2 `Request::finished()` 只是 group finished 的代理

`Request::finished()` 很薄，只是直接返回 `sequences_group_->finished()`。

这说明：

- 一个 request 是否结束，不是看某个单一标志；
- 它由 group 里所有 sequence 的完成情况共同决定。

所以 request 级 finished 的真正来源其实在 `SequencesGroup` 和 `Sequence`。

### 6.3 `Request::expand_sequences()` 不是“随时扩容”，而是受 group 规则控制

`Request::expand_sequences()` 直接委托给 `SequencesGroup::expand_sequences()`。

这一层很重要，因为它意味着：

- request 自己并不知道何时应该从 1 条 sequence 扩成 `best_of` 条；
- 真正理解多序列扩展语义的是 group。

### 6.4 `Request` 负责请求级取消和连接状态检查

`Request::set_cancel()` 会：

1. 设置 request 级 `cancelled_`
2. 对每个 sequence 调 `seq->set_cancel()`

`Request::update_connection_status()` 则会：

1. 检查 `state_.call_` 是否存在
2. 如果底层连接断开，则 `set_cancel()`

所以“客户端断开 -> 请求取消”这条链，是 request 级完成的，而不是 sequence 级自发知道的。

### 6.5 `Request` 负责请求级输出聚合

`Request::generate_output()` 并不自己 detokenize 每条序列，而是：

1. 先汇总 usage：
   - `num_prompt_tokens`
   - `num_generated_tokens`
   - `num_total_tokens`
2. 生成 `RequestOutput`
3. 调 `sequences_group_->generate_outputs(...)`

也就是说：

- usage 是 request 级聚合
- 真正每条输出如何组织，是 group / sequence 级逻辑

### 6.6 `Request` 还持有一批纯 request 级调度辅助状态

这一类字段说明 `Request` 还有“调度上的请求级身份”这一面：

- `offline()` / `priority()`
- `ttlt_slo_ms()` / `ttft_slo_ms()` / `tpot_slo_ms()`
- `urgency_`
- `starved_`
- `deadline_ms_`
- `elapsed_time_ms_`

这里可以看出一个很重要的设计：

> **调度器虽然最终执行的是 sequence，但很多优先级/SLO/在线离线属性仍然天然属于 request。**

所以 request 不能被简化掉。

---

## 7. `SequencesGroup` 的职责：多条 `Sequence` 的语义容器

如果说 `Request` 解决的是“这是哪个业务请求”，那么 `SequencesGroup` 解决的是：

> **“这个请求内部如果有多条生成轨迹，它们怎样被组织、扩展、选优和输出。”**

### 7.1 为什么需要 `SequencesGroup`

如果没有 group，那么下面这些需求都很难优雅表达：

- `n=1, best_of=4`：内部生成 4 条，最后只返回 1 条
- beam search：多个 beam 属于同一个请求语义集合
- request 级共享 prompt / input_embedding / mm_data
- request 级 finished 不是看某一条 sequence，而是看整个集合
- 非 stream 场景按 logprob 选 top-n

所以 `SequencesGroup` 不是多余的一层，它是“同一 request 下多轨迹语义”的显式边界。

### 7.2 `SequencesGroup` 只拥有 sequence，不拥有 prompt 本体

构造函数里保存的是：

- `const std::string& prompt_`
- `const std::vector<int32_t>& prompt_tokens_`
- `const torch::Tensor& input_embedding_`
- `const MMData& mm_data_`
- `SequenceParams sequence_params_`
- `std::vector<std::unique_ptr<Sequence>> sequences_`

这里前三项和 `mm_data_` 都是引用，真正 owner 仍然是 `RequestState` / `Request`。

所以 `SequencesGroup` 更像一个：

- 共享输入语义的载体
- 多条 sequence 的 owning container

### 7.3 group 初始化时只先放一条 sequence

构造函数会立即调用 `add()`，但 `add()` 每次只追加一条 sequence。

也就是说：

- request 刚创建时，group 里默认只有 1 条 sequence；
- 额外 sequence 是后续按需要扩出来的。

这和“先 prefill 一条，再根据 prefix sharing 扩展”这个设计是配套的。

### 7.4 `expand_sequences()` 是 group 最核心的方法之一

`SequencesGroup::expand_sequences(bool share_prefix)` 的逻辑非常关键：

1. 如果是 beam search，直接不扩。
2. 如果当前数量已经等于 `best_of`，不扩。
3. 如果当前数量大于 `best_of`，直接 fatal。
4. 如果 `share_prefix=false`，立即扩到 `best_of`。
5. 如果 `share_prefix=true`，只有当首条 sequence 的 `kv_cache_tokens_num >= num_prompt_tokens` 时才扩。

这里我认为是整个 request/group 设计里最值得注意的一点之一：

> **best_of 多序列不是一开始就盲目复制出来，而是可以等首条 sequence 的 prompt 前缀已经进 KV 后再扩，从而共享 prefix cache。**

这就是 group 存在的一个非常现实的工程价值。

### 7.5 group 的 finished 语义是“数量达标 + 全部结束”

`SequencesGroup::finished()` 逻辑是：

1. 如果 `sequences_.size() < best_of`，直接返回 false
2. 否则要求所有 sequence 都 `finished()`

这说明：

- request finished 不只是“现有 sequence 都结束”
- 还要求“你本来应该有的 sequence 已经都扩齐了”

因此 group 天然承担“多轨迹完整性”的职责。

### 7.6 group 负责最终输出的选优和整理

`SequencesGroup::generate_outputs()` 分了几条路径：

#### A. sample slot 路径

如果任意 sequence 带 `sample_slots()`，则会调用各 sequence 的 `generate_sample_outputs()`，并对输出按 `index` 做稳定排序。

#### B. rec multi-round beam 缓存路径

这是特殊场景，优先直接走缓存结果。

#### C. streaming 路径

如果 `sequence_params_.streaming`，它会把每条 sequence 的 `generate_output()` 推入输出。

这里要特别谨慎说明：

- 在主 HTTP/brpc streaming 路径里，真正生成增量文本的是 `AsyncResponseProcessor::process_stream_request()` 调 `Sequence::generate_streaming_output()`；
- `SequencesGroup::generate_outputs()` 的 streaming 分支不是主线流式输出路径。

所以这里更像是“组级输出兜底/特定场景输出”，而不是主 stream 增量路径。这个判断是**结合调用链得出的推断**。

#### D. 非 stream + `best_of > n`

如果有多条 sequence 且不是 beam search，则会：

1. 先按 `get_average_logprob()` 排序
2. 取 top-n
3. 重写输出 index 为 0..n-1

也就是说：

> **`best_of` 的最终选优逻辑是 group 级完成的，不是 request 级也不是 scheduler 级。**

#### E. 其它普通路径

普通情况下就对每条 sequence 调 `generate_output(tokenizer)`。

### 7.7 beam search 不是通过 `expand_sequences()` 做的

这是一个很容易看错的点。

非 beam 的多序列是通过 `expand_sequences()` 扩出来的；但 beam search 不是。

beam search 的核心在 `SequencesGroup::process_beam_search()`：

1. 遍历当前 sequences
2. 结合每条 sequence 的 top-logprobs / top-tokens 构造候选
3. 按总 logprob 选 top-k
4. 用 `Sequence` 拷贝构造函数复制源 sequence
5. 用 `update_token()` 改写最后 token
6. 拷贝 / 共享 KV block 来源关系
7. 用新 sequence 集替换旧 `sequences_`

所以 beam search 里的 sequence fork，实际上是 **sequence copy + token patch + KV 来源继承**。

这也是 `Sequence` 拷贝构造函数存在的重要原因。

---

## 8. `Sequence` 的职责：真正的生成轨迹对象

这是三层里最核心的一层。

我现在的判断是：

> **`Sequence` 不是“输出文本对象”，也不是“请求对象的一部分缓存”，它本质上是一个 token 轨迹状态机。**

它同时维护：

- token 序列本身
- prompt / generated 的边界
- KV cache 状态
- logprob 状态
- detokenize 增量状态
- finish 状态
- stream close 状态
- schedule overlap 相关临时状态

### 8.1 `Sequence` 构造时做了什么

普通 LLM 路径下，构造函数主要做了这些事：

1. 检查 prompt token 非空
2. 检查 `seq_capacity > prompt_token_ids.size()`
3. 设置 `num_prompt_tokens_`
4. 设置 `volatile_num_prompt_tokens_`
5. 分配 `tokens_` 容量
6. 初始化 `LogprobState`
7. 根据 penalty 设置 `need_unique_tokens_`
8. 把 prompt token 全部拷进 `tokens_`
9. 初始化 `token_to_count_map_`
10. 保存 `input_embedding_`
11. 设置 `cur_generated_token_idx_ = num_prompt_tokens_`

所以 `Sequence` 在出生时就已经是“带 prompt 的轨迹对象”，不是空轨迹。

### 8.2 `SequenceStage` 不是字段，而是推导值

`SequenceStage` 有三种：

- `PREFILL`
- `CHUNKED_PREFILL`
- `DECODE`

但注意：`Sequence` 并没有一个 `stage_` 成员变量。

它的 stage 是实时推导出来的：

```text
if kv_cache_tokens_num < max(volatile_num_prompt_tokens, num_prompt_tokens)
    if kv_cache_tokens_num > 0 -> CHUNKED_PREFILL
    else -> PREFILL
else
    -> DECODE
```

这个设计非常重要，因为它意味着：

1. stage 不是“人为切换标志”，而是根据 KV 覆盖进度推导；
2. 一个 sequence 是否完成 prompt prefill，本质看的是“有多少 token 已经进 KV”；
3. `volatile_num_prompt_tokens_` 允许 preemption / reset 之后把“历史已生成 token”临时当成下一轮 prompt 来看。

### 8.3 `Sequence` 里最核心的几类状态

#### A. token 轨迹状态

- `std::vector<int32_t> tokens_`
- `size_t num_tokens_`
- `size_t num_prompt_tokens_`
- `size_t volatile_num_prompt_tokens_`

这是最基础的轨迹本体。

#### B. KV 状态

- `KVCacheState kv_state_`
- `KVCacheState host_kv_state_`

这里至少有两个层面：

- device KV
- host KV / storage prefetch 相关 KV

`num_need_compute_tokens()` 直接取：

```text
num_tokens - max(device_kv_tokens, host_kv_tokens)
```

这说明 sequence 当前“还有多少 token 需要算”，不是单看 prompt 长度，而是看已有 KV 覆盖到哪里。

#### C. 解码状态

- `IncrementalDecoder decoder_`

它负责增量 detokenize，并通过 `output_offset()` 控制“已经输出到哪里了”。

#### D. logprob 状态

- `std::unique_ptr<LogprobState> logprob_state_`

负责保存：

- 每 token logprob
- top tokens / top logprobs
- 平均 logprob
- beam 相关累计 logprob

#### E. 完成状态

- `mutable bool finished_`
- `mutable bool finish_status_invalidated_`
- `mutable FinishReason finish_reason_`
- `bool closed_`

这里要注意：

- `finished_` 表示生成逻辑上已经结束；
- `closed_` 表示 streaming 语义上“finish reason 已经发出，不再继续发”。

所以 finished 和 closed 不是一回事。

#### F. schedule overlap 状态

- `uint32_t cur_generated_token_idx_`
- `std::queue<bool> is_pre_scheduled_step_prefill_`
- `std::atomic<bool> last_token_handled_`

这些字段都和 overlap 场景下“先放 fake token，再在下一步替换真实 token”的机制有关。

### 8.4 为什么 `Sequence` 才是“真正的执行单位”

从接口上就能看出来：

- Batch 里直接存的是 `Sequence*`
- Scheduler 的 `running_sequences_` 存的是 `Sequence*`
- `BatchFactory` 也是按 `Sequence*` 组 batch
- `Batch::append_token_for_sequence()` 最终回写的是 `Sequence`

所以 xLLM 不是把“request”直接送去执行，而是把“request 下选中的 sequences”送去执行。

这就是为什么 `Sequence` 才是实际执行粒度。

---

## 9. `Sequence` 的 token 推进逻辑：什么时候 append，什么时候 update

这部分是理解 sequence 状态机的关键。

### 9.1 普通路径：`append_token()`

`append_token()` 的约束非常明确：

1. 不能超出容量
2. 不能对 finished sequence 追加
3. 非 onerec 模型下，不能对 prefill sequence 追加 token

第三点尤其关键：

> **在普通 LLM 路径下，真正“新生成 token”只会发生在 decode 阶段，不会发生在 prefill 阶段。**

这也解释了为什么 stage 需要跟 KV 进度绑定。

`append_token()` 做的核心事情：

1. 设置 first-token 标志（非 overlap）
2. `record_first_token(token)`（供 disagg PD）
3. 把当前下标 `cur_idx = num_tokens_++`
4. 把 `kv_state_.kv_cache_tokens_num` 设为 `cur_idx`
5. 写入 token id
6. 如果 overlap 下 token_id < 0，则只做“fake token 占位”，不更新 logprob/token_count
7. 否则更新 token 计数、logprob
8. 置 `finish_status_invalidated_ = true`

这里有个非常重要的事实：

> **`append_token()` 同时推进了 token 序列和 KV 覆盖进度。**

### 9.2 overlap 路径：`update_last_step_token()`

当启用 `enable_schedule_overlap` 时，上一轮 forward 先追加的是 fake token，下一轮再用 `update_last_step_token()` 替换成真实 token。

它会：

1. 校验 overlap 已开启
2. 依据 `cur_generated_token_idx_` 判断 first token
3. 记录 first token
4. 如有 `token_offset`，处理 mtp/speculative 相关偏移
5. 把真实 token 写到 `tokens_[cur_generated_token_idx_]`
6. 更新 logprob 和 unique-token 统计
7. `++cur_generated_token_idx_`
8. 置 finish 失效

所以 overlap 下 sequence 的 token 生命周期是：

```text
本轮 step 回来：先 append 一个 fake token(-1)
下一轮 step 结果回来：update_last_step_token() 把它替换成真实 token
```

这也是后面 streaming / finish 处理比普通模式复杂的根本原因。

### 9.3 beam search 路径：`update_token()`

beam search 时并不是“继续 append 一个全新的尾 token”这么简单，而可能需要：

- 拷贝源 sequence
- 重写已有位置的 token

`update_token()` 负责的就是对指定位置进行 token 覆盖，并同步更新：

- token 计数
- logprob
- finish 失效标志

---

## 10. `Sequence` 的 finished 逻辑：停止条件到底怎么判断

### 10.1 finish 判断不是立即缓存死的，而是“失效后重算”

`Sequence::finished()` 先看 `finish_status_invalidated_`：

- 如果没有失效，直接返回缓存 `finished_`
- 如果失效了，重新调用 `StoppingChecker::check(...)`

这说明：

- finish 不是每次都全量计算
- 但每次 token 改动后都会失效，需要重算

### 10.2 `StoppingChecker` 检查的是“最后一个有效 token”

`StoppingChecker::check()` 非常值得注意的一点是：

它会先从尾部往前找最后一个 `>= 0` 的 token，原因是 overlap 模式下可能有 fake token `-1`。

然后按顺序检查：

1. `max_generated_tokens`
2. `max_context_len`
3. `ignore_eos`
4. `eos_token`
5. `stop_tokens`
6. `stop_sequences`

因此 sequence finish 的真正依据不是某个额外状态位，而是：

> **当前 token 轨迹 + stopping_checker 规则。**

### 10.3 embedding / mm embedding 是特殊 finished 路径

如果 `sampling_param->is_embeddings` 为真：

- `finished()` 默认不会因为 token 停止而 finished；
- 必须等 `update_embeddings()` 或 `update_mm_embeddings()` 被调用，才会手动置 `finished_=true` 和 `finish_reason_=STOP`。

这说明 sequence 设计并不只服务文本生成，也服务 embedding/MM embedding 任务。

### 10.4 `closed_` 是流式输出层面的结束，不是生成层面的结束

在 `AsyncResponseProcessor::process_stream_request()` 里：

1. 如果 seq `finished()`，会先把它加入本次输出候选
2. 然后 `seq->close()`

也就是说：

- finish 代表“生成完了”
- close 代表“finish reason 已经发给客户端了，以后别再发”

这是 streaming 场景必须区分的两个层次。

---

## 11. `Sequence` 的输出逻辑：streaming 和 non-stream 完全不是一回事

### 11.1 `generate_streaming_output()`：增量输出

它的核心逻辑是：

1. 先忽略尾部 fake token `-1`
2. 从 `decoder_.output_offset()` 记住起点
3. 调 `decoder_.decode(ids, tokenizer)` 得到 delta
4. 如果 delta 为空，返回 `nullopt`
5. 输出：
   - `index`
   - `text = delta`
   - `token_ids = [start, end)`
   - `logprobs`

这里最关键的点是：

- streaming 不是每次返回整段文本
- 而是返回“自上次输出偏移之后新增的增量”

### 11.2 `generate_output(tokenizer)`：一次性完整输出

非 stream 路径下：

1. 先处理 embeddings / mm embeddings 特殊分支
2. 过滤尾部 fake token
3. 对 onerec 走专门分支
4. 否则做文本 decode

文本 decode 时有一个非常细的实现：

- 它不是从头全量 decode 到尾；
- 而是从靠近尾部的位置开始，保留最多 6 个 token 的回看窗口，避免 UTF-8 byte sequence 不完整。

这说明 xLLM 在 non-stream 一次性输出场景里，也做了增量解码一致性的工程处理。

### 11.3 `generate_sample_outputs()`：sample slot 专用输出

如果 sequence 带了 `sample_slots()`，则会：

1. 以 `slot.sample_id` 作为输出 index
2. 只抽取对应 token 位置的 token/logprob
3. 如果该位置还没有有效 token，则返回 `empty_logprobs` finish reason

这说明 sequence 不只是“生成完整文本”容器，也能做“采样点回传”。

---

## 12. `Request`、`SequencesGroup`、`Sequence` 三层的 ownership 关系

这层如果不看清楚，会很容易搞不明白谁在拥有谁，谁只是引用。

### 12.1 对象关系图

```text
Request(shared_ptr, owned by scheduler/runtime)
├─ RequestBase
│  ├─ request_id / service_request_id / x_request_id / x_request_time
│  └─ created_time
├─ RequestState state_
│  ├─ prompt / prompt_tokens
│  ├─ sampling_param / scheduler_param / stopping_checker
│  ├─ stream / echo / skip_special_tokens
│  ├─ output_func / outputs_func / call_
│  └─ input_embedding / mm_data / sample_slots
└─ unique_ptr<SequencesGroup> sequences_group_
   ├─ 引用 RequestState 中的 prompt / prompt_tokens / embedding / mm_data
   ├─ SequenceParams(内含指向 RequestState 中 sampling/stopping/sample_slots 的指针)
   └─ vector<unique_ptr<Sequence>>
      ├─ tokens / kv_state / decoder / logprob_state
      ├─ finish_reason / finished / closed
      └─ schedule overlap / beam / prefetch 等局部状态
```

### 12.2 这层设计的工程意义

这个 ownership 分层有几个明显好处：

1. `Request` 生命周期稳定，适合 scheduler 用 `shared_ptr` 管理。
2. `Sequence` 由 group 独占，适合做 beam fork / best_of 扩展。
3. prompt / sampling / stopping 这类 request 级共享语义，不用在每个 sequence 重复存一份大对象。
4. 但代价是：需要严格遵守 `Request` 生命周期必须覆盖 sequence 生命周期。

从当前实现看，这个约束是被满足的。

---

## 13. 为什么说 scheduler “管理 request，但执行 sequence”

这句话在源码里其实非常具体。

### 13.1 scheduler 收到的是 `shared_ptr<Request>`

`ContinuousScheduler::add_request()` 签名就是：

```text
bool add_request(std::shared_ptr<Request>& request)
```

而且进队之前还会做一件事：

```text
kv_cache_manager_->prefetch_from_storage(request)
```

这说明调度器管理请求时，天然还是 request 级别在思考。

### 13.2 `prepare_batch()` 先在 request 级移动队列

`prepare_batch()` 先做几件 request 级操作：

1. 从 `request_queue_` 读出 request
2. 根据 online/offline 放入不同 waiting queue
3. 扫描 `running_requests_`，处理 finished/cancelled request
4. 把还活着的 request 重新塞回 running queue

这一步都还是 request 级。

### 13.3 但真正进本轮 batch 的是 `running_sequences_`

无论 prefill 还是 decode，scheduler 在挑选本轮工作时，最终都会填充三样东西：

- `running_requests_`
- `running_sequences_`
- `running_sequences_budgets_`

其中：

- `running_requests_` 记录“本轮涉及了哪些 request”
- `running_sequences_` 记录“本轮实际要执行哪些 sequence”
- `running_sequences_budgets_` 记录“每条 sequence 本轮能处理多少 token”

这正是 request/sequence 双层表示的核心。

### 13.4 `handle_running_requests()` 体现了 request 级和 sequence 级的分工

`handle_running_requests()` 做两件事：

1. `request->expand_sequences()`：必要时从 1 条扩成 `best_of` 条
2. 释放已 finished 的 sequence 对应的 KV block

这里 request 仍然是调度入口；但操作的对象其实已经是 sequence 集合。

### 13.5 prefill / decode 选中的也是 sequence 列表

无论是 `handle_prefill_requests()` 还是 `handle_decode_requests()`：

- 都会先从 request 拿 `request->sequences()`
- 再从中挑出 candidate sequences
- 再把 candidate sequences 和 token budget 放进 `running_sequences_`

因此“真正执行粒度是 sequence”并不是抽象说法，而是直接体现在 scheduler 内部数据结构上。

---

## 14. `Batch` 如何把 `Sequence` 送去执行，再把结果写回来

虽然本文主角不是 batch，但 request/sequence/group 的逻辑必须放到 batch 里才能闭环。

### 14.1 `BatchFactory` 按 `running_sequences_` 组 batch

`BatchFactory::create_batches()` 会遍历：

- `running_requests_`
- `running_sequences_`
- `running_sequences_budgets_`

对每个 sequence：

1. 计算本轮 prompt token 和 generated token 的预算
2. 按 `sequence->dp_rank()` 放入对应 DP batch
3. 调 `batches[dp_rank].add(sequence, token_budget)`

如果是 beam search，还会额外把 request 的 `sequence_group()` 加入 batch。

这意味着：

> **batch 的主数据面是 sequence，group 只在 beam search / rec 等需要组语义的地方额外参与。**

### 14.2 `Batch` 自己不拥有 sequence 生命周期

`Batch` 里放的是：

- `std::vector<Sequence*> sequences_`
- `std::vector<SequencesGroup*> sequence_groups_`

所以它是一次 step 的非 owning 视图对象。

### 14.3 `Batch::refresh_output_targets()` 决定“本轮输出应该写回谁”

这是一个特别关键、但很容易被忽略的点。

`refresh_output_targets()` 会根据每个 sequence：

- 当前 `n_tokens`
- `n_kv_cache_tokens`
- `allowed_max_tokens`
- `sample_slots`

计算出本轮 forward 结果应该映射回哪些 target。

这说明 batch 并不是“worker 返回第 i 个结果就写回第 i 条 sequence”这么简单；它先建立了一层显式的输出映射。

### 14.4 `Batch::process_sample_output()` 最终回写到 `Sequence`

无论 worker 返回的是 `RawForwardOutput` 还是 `SampleOutput`，`Batch::process_sample_output()` 的落点都是 sequence：

- embeddings -> `seq->update_embeddings()`
- mm_embeddings -> `seq->update_mm_embeddings()`
- token -> `append_token_for_sequence()`

而 `append_token_for_sequence()` 内部会根据 `replace_fake_token` 决定：

- 调 `seq->append_token()`
- 还是调 `seq->update_last_step_token()`

所以 sequence 才是 forward 结果真正的状态落点。

### 14.5 Engine 执行后也是先回写 `Batch`，再回写 `Sequence`

`LLMEngine::step()` 的关键路径是：

1. `prepare_inputs(batch)`
2. fan-out 到 worker
3. 收集 `RawForwardOutput`
4. 对每个 DP batch：
   - 非 beam：`batch[dp_rank].process_sample_output(result, false)`
   - beam：`batch[dp_rank].process_beam_search_output(result, false)`

也就是说 engine 自己并不直接改 request/sequence；它把回写责任交给了 `Batch`。

---

## 15. 输出是怎样从 `Sequence` 再回到 `RequestOutput` 的

### 15.1 流式请求：直接从 `Sequence` 增量生成输出

`AsyncResponseProcessor::process_stream_request()` 的流程是：

1. 遍历 request 里的 sequences
2. 找出：
   - `has_new_tokens_generated()` 的 sequence
   - 或者 `finished()` 的 sequence
3. 对这些 sequence 调 `generate_streaming_output(size, tokenizer)`
4. 把得到的 `SequenceOutput` 塞到一个临时 `RequestOutput`
5. 调 request 的 `output_func(req_output)`

所以 streaming 主线非常明确：

> **Sequence 状态 -> SequenceOutput 增量 -> 临时 RequestOutput -> service callback**

### 15.2 非流式请求：由 `Request::generate_output()` 做最终聚合

`AsyncResponseProcessor::process_completed_request()` 则会：

1. 调 `request->generate_output(*tokenizer_)`
2. `Request::generate_output()` 内部再调 `sequences_group_->generate_outputs(...)`
3. 形成最终 `RequestOutput`
4. 调 `request->state().output_func(req_output)`

所以非 stream 路径是：

> **Request 聚合 usage -> Group 选优/整理 -> Sequence 生成最终文本 -> RequestOutput -> callback**

### 15.3 为什么非 stream 需要 group，而 stream 更贴近 sequence

原因很直观：

- stream 关心的是“最新增量 token / delta text”
- non-stream 关心的是“最终输出集合长什么样，是否要 top-n 选优，usage 怎么统计”

因此：

- streaming 更贴近 sequence
- non-stream 更贴近 request + group

这是这套三层设计非常自然的一点。

---

## 16. 多序列语义：`n`、`best_of`、beam 三者如何落位

### 16.1 `n`

`n` 表示最终需要返回多少条输出。

它既体现在：

- `RequestState.n`
- `SequenceParams.n`

也体现在 group 的最终输出裁剪逻辑里。

### 16.2 `best_of`

`best_of` 表示内部最多要跑多少条候选序列。

它影响：

- `RequestState` 构造时检查 `best_of >= n`
- `SequencesGroup::finished()` 需要 sequence 数量达到 `best_of`
- `SequencesGroup::expand_sequences()` 最终扩到 `best_of`
- 非 stream 时按平均 logprob 选 top-n

也就是说：

> **`best_of` 是 group 的内部候选规模语义。**

### 16.3 beam search

beam search 不是 `best_of` 的另一个名字。它的落点是：

- `RequestSamplingParam.beam_width > 1`
- `Request::check_beam_search()` / `Sequence::check_beam_search()` / `SequencesGroup::check_beam_search()`
- `Batch::process_beam_search_output()`
- `SequencesGroup::process_beam_search()`

beam search 的 sequence 是通过“拷贝并改写”生成的，而不是通过 `expand_sequences()` 扩出来的。

### 16.4 为什么 `best_of != n` 时会禁用 streaming

`LLMMaster::generate_request()` 里有一条很关键的逻辑：

```text
if (best_of != sp.n) {
    stream = false;
}
```

这背后的含义非常合理：

- 如果内部跑了更多候选，最后还要 group 级选优；
- 那么在最终候选未确定前，流式地把中间每条候选都往外吐，语义会很混乱。

所以 xLLM 这里干脆把这种情况降级为 non-stream。

---

## 17. schedule overlap 对 `Sequence` 设计的影响

如果不把 overlap 加进来，会低估 sequence 复杂度。

### 17.1 overlap 的核心机制

`ContinuousScheduler::step_with_schedule_overlap()` 的主逻辑是：

1. 当前 batch 先 `engine_->step(batch)`
2. 然后处理上一轮 batch：
   - `engine_->update_last_step_result(last_batch_)`
   - `process_batch_output(true)`

这意味着系统会“预先把下一轮 schedule 和上一轮结果回写交叠起来”。

### 17.2 这要求 `Sequence` 能容忍 fake token

因此 sequence 里必须支持：

- token 序列尾部出现 `-1`
- finished 检查忽略 fake token
- streaming/non-stream 输出忽略 fake token
- 下一轮再把 fake token 替换成真实 token

所以你会看到：

- `StoppingChecker` 会跳过尾部 `< 0` token
- `generate_streaming_output()` 会跳过 fake token
- `generate_output()` 会跳过 fake token
- `append_token()` 特判 `token_id < 0`
- `update_last_step_token()` 再写真实 token

### 17.3 overlap 下 “最后一个 token 已处理” 要单独跟踪

因为最后一个 token 可能在不同 step 才真正稳定，所以 request/sequence 都有：

- `handle_last_token_done`
- `last_token_handled_`

这也是为什么 overlap 模式下 finished 请求的输出处理节奏和普通模式不完全一样。

---

## 18. 一个请求在三层对象里的典型生命周期

下面我用三个典型场景把状态变化串起来。

### 18.1 场景一：普通单序列文本生成

```text
service request
  -> RequestParams
  -> RequestState
  -> Request(内含 1 个 SequencesGroup, 组内 1 条 Sequence)
  -> scheduler 把该 Request 放进 waiting queue
  -> prefill 时，Sequence stage = PREFILL / CHUNKED_PREFILL
  -> prompt 全部进入 KV 后，Sequence stage = DECODE
  -> 每轮 decode 追加 1 个 token
  -> StoppingChecker 命中 stop/length/eos
  -> Sequence finished = true
  -> Group finished = true
  -> Request finished = true
  -> AsyncResponseProcessor 聚合输出
```

### 18.2 场景二：`best_of > 1` 的非 beam 多候选生成

```text
Request 初始只有 1 条 Sequence
  -> 首条 Sequence 先 prefill
  -> 当满足 prefix share 条件时，expand_sequences() 扩到 best_of
  -> 多条 Sequence 各自 decode
  -> 所有 Sequence finished
  -> Group 按 average logprob 选 top-n
  -> Request 输出 top-n 结果
```

这里最关键的不是“多了几条 sequence”，而是：

- request 仍然只有一个；
- group 负责候选集合语义；
- sequence 各自独立推进；
- 最终由 group 做选优。

### 18.3 场景三：beam search

```text
Request 初始仍只有 1 条 Sequence
  -> decode 产生 beam 相关 top-k 候选
  -> SequencesGroup::process_beam_search()
     复制源 Sequence, 改写 token, 继承/交换 KV 来源
  -> group 内 sequences_ 被替换为新的 beam 集合
  -> 后续继续以多条 beam Sequence 推进
  -> 最终由 group 产出 beam 排序结果
```

这说明 beam search 是“组内 sequence 集动态重写”，而不是简单多开几条普通 sequence。

---

## 19. 这套拆分为什么合理：我的理解总结

读完源码后，我觉得 xLLM 这里把 request / group / sequence 拆开是很有章法的。

### 19.1 如果没有 `Request`

就很难优雅承接：

- callback
- rate limiter 生命周期
- x-request-id / service_request_id
- request 级统计
- request 级 SLO / priority
- 连接断开取消

### 19.2 如果没有 `SequencesGroup`

就很难优雅承接：

- `best_of`
- `n`
- beam search
- top-n 选优
- request finished 的“全体完成”语义

### 19.3 如果没有 `Sequence`

就无法把下列真正执行态干净地落下来：

- token 轨迹
- KV 状态
- stage 推导
- logprob
- detokenize offset
- finish / close
- fake token / overlap

因此三层不是过度设计，而是各自承接了不同维度的复杂度。

---

## 20. 我认为最值得记住的几个关键点

### 20.1 `Request` 不是“正在生成的文本”，而是“请求级控制对象”

它最重要的价值不是保存 tokens，而是保存：

- request 级身份
- request 级回调
- request 级调度属性
- 对 group 的拥有关系

### 20.2 `Sequence` 的 stage 是推导出来的，不是手动切状态

stage 由 KV 覆盖进度决定，这非常关键。

### 20.3 `best_of` 的多序列扩展是延迟进行的

特别是在 prefix cache 语义下，先 prefill 一条再扩，是很聪明的工程实现。

### 20.4 streaming 和 non-stream 的主链不同

- streaming：更贴近 `Sequence::generate_streaming_output()`
- non-stream：更贴近 `Request::generate_output()` + `SequencesGroup::generate_outputs()`

### 20.5 beam search 不是普通多序列扩容

beam 是“组内 sequence fork + token patch + KV 来源继承”的特殊逻辑。

### 20.6 overlap 让 `Sequence` 明显变复杂了

如果不开 overlap，sequence 已经不简单；开了 overlap 之后，还要额外支持：

- fake token
- 延迟替换真实 token
- last token handled
- pre-scheduled prefill 队列

---

## 21. 证据边界与仍需继续深挖的点

下面这些结论我认为是**源码直接支持的已确认事实**：

1. scheduler 接收的是 `Request`，但 batch 执行的直接对象是 `Sequence*`。
2. `Request` 构造时会立即创建 `SequencesGroup`。
3. group 初始只有一条 sequence，后续再按 `best_of`/beam 逻辑扩展或重写。
4. `Sequence` 的 stage 由 KV 覆盖进度推导，而不是字段存储。
5. stream 增量输出主链落在 `Sequence::generate_streaming_output()`。
6. non-stream 最终聚合主链落在 `Request::generate_output()`。

下面这些点我认为已经有较强证据，但我仍然标记为**推断/待继续深挖**：

1. `SequencesGroup::generate_outputs()` 的 streaming 分支，在主 HTTP streaming 路径里不是主角，更像某些特殊场景或兜底路径。
2. `Request`、`Sequence` 里若干 schedule-overlap / disagg-PD 字段，和 `3-schedule_overlap.md`、`4-pd_disaggregate.md` 里的专题逻辑是强耦合的，若要彻底吃透，还需要继续深读对应专题链路。
3. rec / oneRec 的 `Sequence` 特化逻辑已经能看出模式，但如果要完全解释 recommendation 任务的多轮语义，还需要再追 `rec_batch_input_builder.*`、`rec_engine.*`。

---

## 22. 最终总结

把这次学习压缩成一段话：

> xLLM 里的请求主链，并不是“服务层收到请求 -> 一个 request 对象一路往下跑到底”。真正的内部语义是：服务层先把外部协议压缩成 `RequestParams`，`LLMMaster` 再把它归一化成 `RequestState`，随后构造 `Request`。`Request` 负责请求级身份、调度属性、回调和统计；`SequencesGroup` 负责同一请求下的多序列组织、扩展和选优；`Sequence` 负责真正的 token/KV/logprob/finish 轨迹。Scheduler 在 request 级管理生命周期，在 sequence 级选择执行对象；Batch 再把 sequence 包装成一次 step 的 forward 输入，并把结果回写给 sequence；最后 streaming 更贴近 sequence 增量输出，non-stream 更贴近 request/group 最终聚合输出。这就是 xLLM 把请求语义、执行语义和多轨迹语义拆开的真正原因。

