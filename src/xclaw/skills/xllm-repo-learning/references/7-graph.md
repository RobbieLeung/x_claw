# xLLM 中 ACL Graph / CUDA Graph 详细学习笔记

## 0. 说明与阅读范围

这份笔记的目标，是**把 xLLM 里 ACL graph（NPU）和 CUDA graph（GPU）两条图执行主链彻底吃透**，不仅说明“有这个功能”，更要说明：

- 它们在整体架构里的位置是什么；
- 它们是怎么接入 `Worker -> Executor -> Model` 主链的；
- capture / replay 的边界在哪里；
- 动态 shape、attention planning、persistent buffer、memory pool 分别怎么处理；
- ACL graph 和 CUDA graph 的相同点、不同点、各自的工程取舍是什么。

> 说明：你问题里写的是 `overview.md` 和 `structure.md`，仓库里实际对应文件是 `ralph/learn_xllm/0-overview.md` 与 `ralph/learn_xllm/1-structure.md`。下面分析以这两个文件为入口，再对照图模式设计文档和源码实现。

本次主要阅读证据：

- 架构与学习上下文
  - `ralph/learn_xllm/0-overview.md:408`
  - `ralph/learn_xllm/0-overview.md:597`
  - `ralph/learn_xllm/1-structure.md:443`
  - `ralph/learn_xllm/1-structure.md:496`
  - `ralph/learn_xllm/task.md:303`
- Graph Mode 文档
  - `docs/zh/features/graph_mode.md:3`
  - `docs/zh/design/graph_mode_design.md:27`
  - `docs/zh/design/graph_mode_design.md:101`
  - `docs/zh/design/graph_mode_design.md:189`
  - `docs/zh/design/graph_mode_design.md:300`
- 主实现
  - `xllm/core/runtime/executor.cpp:22`
  - `xllm/core/runtime/executor_impl_factory.cpp:42`
  - `xllm/core/runtime/cuda_graph_executor_impl.h:46`
  - `xllm/core/runtime/cuda_graph_executor_impl.cpp:101`
  - `xllm/core/runtime/cuda_graph_executor_impl.cpp:1202`
  - `xllm/core/runtime/acl_graph_executor_impl.h:56`
  - `xllm/core/runtime/acl_graph_executor_impl.cpp:62`
  - `xllm/core/runtime/acl_graph_executor_impl.cpp:943`
- 相关辅助实现
  - `xllm/core/layers/common/attention_metadata_builder.cpp:24`
  - `xllm/core/layers/cuda/flashinfer_attention.cpp:173`
  - `xllm/core/layers/cuda/xattention.cpp:53`
  - `xllm/core/kernels/cuda/piecewise_graphs.cpp:18`
  - `xllm/core/kernels/cuda/global_capture_instance.cpp:43`
  - `xllm/core/kernels/cuda/attention_runner.cpp:28`
  - `xllm/core/layers/npu_torch/attention.cpp:111`
  - `xllm/core/kernels/npu/npu_ops_api.h:48`
  - `xllm/core/platform/shared_vmm_allocator.h:26`
  - `xllm/core/platform/shared_vmm_allocator.cpp:81`
  - `xllm/core/platform/cuda/device_capture_lock.h:30`
  - `xllm/core/platform/npu/device_capture_lock.h:29`

---

## 1. 先从 `overview` / `structure` 看 graph 在整体架构里的位置

### 1.1 `0-overview.md` 给出的架构定位

`0-overview.md` 已经把 graph mode 放在“执行/平台类”能力里，与 `xtensor_memory.md`、`multi_streams.md` 并列，说明它不是服务层能力，也不是 scheduler 策略，而是**设备执行路径上的平台优化能力**，见 `ralph/learn_xllm/0-overview.md:423`。

同时，它还明确指出运行时骨架里有一层专门的 graph executor：

- `Worker` 是门面；
- `WorkerImpl` 是设备执行骨架；
- `Executor` 是 batch 输入到 model forward 的桥梁；
- **graph executor 是平台特化执行器**。

这点在 `ralph/learn_xllm/0-overview.md:615` 到 `ralph/learn_xllm/0-overview.md:631` 里非常明确。

### 1.2 `1-structure.md` 给出的调用位置

`1-structure.md` 进一步把主链压缩成了：

`WorkerImpl -> Executor -> Model`

并且指出 `Executor` 会通过 `ExecutorImplFactory` 根据 backend / device / graph 选择具体实现，见：

- `ralph/learn_xllm/1-structure.md:496`
- `ralph/learn_xllm/1-structure.md:503`

这意味着：

1. graph 不是 scheduler 层决定 batch 的机制；
2. graph 也不是 model registry 层决定模型类型的机制；
3. graph 真正插入的位置，是**设备执行门面 `Executor` 下面**。

这个定位非常关键，因为后面你会看到：

- graph 的“分桶、capture、replay、persistent buffer、memory pool”都在 executor impl 内完成；
- scheduler / batch 只负责把本轮 forward 所需的 tokens、positions、kv info 送到这里；
- model 侧只需要保证 layer / attention 在 graph mode 下可以使用稳定地址和稳定语义。

---

## 2. 图模式从哪里接入主流程

### 2.1 从 `WorkerImpl::init_model()` 进入

图模式不是在每次 step 时临时选择 executor 类型，而是在 worker 初始化模型时就决定了 `Executor` 的具体实现：

- `LLMWorkerImpl::init_model()` 中创建 `model_executor_`：`xllm/core/runtime/llm_worker_impl.cpp:58`
- `Executor` 构造函数里根据 graph 开关与 backend 选择具体 executor impl：`xllm/core/runtime/executor.cpp:22`
- 具体实现由 `ExecutorImplFactory` 创建：`xllm/core/runtime/executor_impl_factory.cpp:42`

最关键的代码是 `Executor::Executor(...)`：

```cpp
std::string backend = (options.backend() != "vlm" && FLAGS_enable_graph)
                          ? Device::type_str()
                          : options.backend();
```

也就是说：

- 如果没有开 graph，则 backend 还是普通的 `llm` / `rec` / `vlm`；
- 如果开了 graph，并且不是 `vlm`，则 backend 被改成设备类型字符串：
  - CUDA 设备走 `"cuda"`
  - NPU 设备走 `"npu"`
  - MLU 设备走 `"mlu"`

然后再由 `REGISTER_EXECUTOR` 注册的具体类接住：

- `CudaGraphExecutorImpl` 注册为 `"cuda"`：`xllm/core/runtime/cuda_graph_executor_impl.h:367`
- `AclGraphExecutorImpl` 注册为 `"npu"`：`xllm/core/runtime/acl_graph_executor_impl.h:329`

### 2.2 每次 step 时真正调用的位置

在执行时，`LLMWorkerImpl::step_internal()` 调用：

- `model_executor_->forward(...)`：`xllm/core/runtime/llm_worker_impl.cpp:119`

而 `Executor::forward(...)` 最终会调用 impl 的 `run(...)`：

- `xllm/core/runtime/executor.cpp:37`

因此图模式真正生效的时刻是：

```text
Batch.prepare_forward_input
  -> WorkerImpl.prepare_work_before_execute
  -> Executor.forward
  -> CudaGraphExecutorImpl::run / AclGraphExecutorImpl::run
  -> model_->forward(...)
```

这里的关键理解是：

- `prepare_inputs()` 只是把 batch 整理成 `ForwardInput`；
- **graph 的选图、capture、replay 并不发生在 `prepare_inputs()`，而是在 `run()` 里**。

这也是为什么图模式能被看作“执行期优化”，而不是“调度期策略”。

---

## 3. 配置层：哪些 graph 开关进了 `Options`，哪些没有

这是我这次读代码时特别留意的一点，因为 xLLM 的配置层是分层的：CLI/global flags -> `xllm::Options` -> `xllm::runtime::Options`。

### 3.1 Graph 相关 gflags

图模式的开关在 `xllm/core/common/global_flags.cpp:76` 开始定义：

- `enable_graph`
- `enable_graph_mode_decode_no_padding`
- `enable_prefill_piecewise_graph`
- `enable_graph_vmm_pool`
- `max_tokens_for_graph_mode`

文档说明见 `docs/zh/features/graph_mode.md:24`。

### 3.2 进入 `Options` / `runtime::Options` 的只有 `enable_graph`

从字段定义看：

- `xllm::Options` 有 `enable_graph`：`xllm/core/common/options.h:184`
- `xllm::runtime::Options` 也有 `enable_graph`：`xllm/core/runtime/options.h:213`
- `Master` 在构造运行时 options 时也把它传下去了：`xllm/core/distributed_runtime/master.cpp:342`

但是，下面这些 graph 子开关**并没有进入 `runtime::Options` 字段**，而是在执行器里直接读 `FLAGS_...`：

- `enable_graph_mode_decode_no_padding`
- `enable_prefill_piecewise_graph`
- `enable_graph_vmm_pool`
- `max_tokens_for_graph_mode`

证据：

- `CudaGraphExecutorImpl` 直接读这些 flag：
  - `xllm/core/runtime/cuda_graph_executor_impl.cpp:934`
  - `xllm/core/runtime/cuda_graph_executor_impl.cpp:1044`
  - `xllm/core/runtime/cuda_graph_executor_impl.cpp:1131`
  - `xllm/core/runtime/cuda_graph_executor_impl.cpp:1389`
- `AclGraphExecutorImpl` 直接读 `FLAGS_enable_graph_mode_decode_no_padding`：
  - `xllm/core/runtime/acl_graph_executor_impl.cpp:1072`

### 3.3 一个非常值得记住的细节

虽然 `enable_graph` 已经进了 `runtime::Options`，但 `Executor` 真正决定“是否选择 graph executor”时，仍然直接读取的是 **`FLAGS_enable_graph`**，不是 `options.enable_graph()`，见 `xllm/core/runtime/executor.cpp:26`。

这说明当前 graph 模式的配置实际上是“混合式”的：

- `enable_graph` 既进 options，也被全局 flag 直接消费；
- graph 的细分行为主要还是靠全局 flag 控制；
- graph executor 并不是纯 runtime option 驱动。

这个观察对后续理解很重要：**graph mode 目前仍有明显的“全局运行期开关”色彩。**

---

## 4. ACL Graph / CUDA Graph 的共性骨架

尽管后端差异很大，但两套实现有一条明显的共性骨架。

### 4.1 都有“persistent param + bucket graph cache”结构

CUDA 路径：

- `CudaGraphPersistentParam`：`xllm/core/runtime/cuda_graph_executor_impl.h:46`
- `graphs_`（decode 图缓存）和 `prefill_graphs_`（prefill piecewise 图缓存）：`xllm/core/runtime/cuda_graph_executor_impl.h:290`

ACL 路径：

- `GraphPersistentParam`：`xllm/core/runtime/acl_graph_executor_impl.h:58`
- `graphs_`（按 bucket 缓存 ACL graph）：`xllm/core/runtime/acl_graph_executor_impl.h:318`

这说明它们都采用了同一套总体思想：

1. 先为图执行准备一套**跨请求复用的持久化输入/输出缓冲区**；
2. 再按 `bucket_num_tokens` 做 lazy capture；
3. 命中 bucket 时直接 replay；
4. 未命中时 capture 一次并放入缓存。

### 4.2 都是“bucket 驱动”的 lazy capture

CUDA 的 `run()`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1202`

ACL 的 `run()`：`xllm/core/runtime/acl_graph_executor_impl.cpp:943`

都遵循这条模式：

```text
1. 根据本轮 tokens 数量求 bucket_num_tokens
2. 检查这个 bucket 是否已有图缓存
3. 有则 replay
4. 没有则 capture 并缓存
```

### 4.3 都把“动态输入更新”收口到 persistent param::update()

这是两条实现最核心的共同点。

不管是 CUDA 还是 ACL，在 replay 前都不会直接把“本轮请求新建的一堆 tensor”塞给 graph，而是：

1. 先把 tokens / positions / seq_lens / block_tables / new_cache_slots 等数据拷贝进持久化 buffer；
2. 再构造引用这些持久化 buffer 的 graph 参数；
3. 让 capture/replay 都只看到固定地址。

这是 Graph Mode 能成立的最根本工程基础。

---

## 5. CUDA Graph：详细逻辑拆解

下面进入最核心的 CUDA graph 逻辑。

## 5.1 CUDA graph 的对象分层

CUDA 这边不是一个类把所有事情全做了，而是三层：

### 5.1.1 `CudaGraphExecutorImpl`

负责执行器级逻辑：

- `prepare_inputs()`
- `run()`
- bucket 选择
- graph map 管理
- prefill / decode 分流
- VMM memory pool 管理

见：`xllm/core/runtime/cuda_graph_executor_impl.h:261`

### 5.1.2 `CudaGraph`

负责**单个 bucket 图实例**的 capture / replay。

它内部既可以持有：

- 一张完整的 `at::cuda::CUDAGraph`（decode full graph）
- 一组 `PiecewiseGraphs`（prefill piecewise graph）

见：`xllm/core/runtime/cuda_graph_executor_impl.h:202`

### 5.1.3 `CudaGraphPersistentParam`

负责所有持久化 buffer：

- `persistent_tokens_`
- `persistent_positions_`
- `persistent_new_cache_slots_`
- `persistent_block_tables_`
- `hidden_states_`
- `q_seq_lens_`
- `kv_seq_lens_`
- `persistent_embedding_`
- `persistent_paged_kv_indptr_`
- `persistent_paged_kv_indices_`
- `persistent_paged_kv_last_page_len_`
- `persistent_decode_qo_indptr_`
- `aux_hidden_states_`

见：

- 分配逻辑：`xllm/core/runtime/cuda_graph_executor_impl.cpp:101`
- 字段定义：`xllm/core/runtime/cuda_graph_executor_impl.h:175`

这个三层分工很漂亮：

- executor 管“什么时候用图”；
- graph instance 管“怎么 capture/replay”；
- persistent param 管“图看见的地址是什么”。

---

## 5.2 CUDA persistent buffer 到底保存了什么

`CudaGraphPersistentParam` 的构造函数会基于：

- `FLAGS_max_tokens_per_batch`
- `options.max_seqs_per_batch()`
- `args.max_position_embeddings()`

预分配一组“最大形状”的持久化张量，见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:107`。

### 5.2.1 输入缓冲区

包括：

- token ids：`persistent_tokens_`
- positions：`persistent_positions_`
- cache slot 映射：`persistent_new_cache_slots_`
- block tables：`persistent_block_tables_`
- q / kv seq lens：`q_seq_lens_`、`kv_seq_lens_`
- embedding（按需）：`persistent_embedding_`

### 5.2.2 输出缓冲区

包括：

- `hidden_states_`
- `aux_hidden_states_`（按需 lazy init）

### 5.2.3 FlashInfer / XAttention 的附加元数据缓冲区

包括：

- `persistent_paged_kv_indptr_`
- `persistent_paged_kv_indices_`
- `persistent_paged_kv_last_page_len_`
- `persistent_decode_qo_indptr_`

这说明 CUDA graph 并不是只持久化“简单输入张量”，而是把 attention runtime 需要的**索引与 plan 输入数据**也一并持久化了。

这与文档里“动态维度参数化”的设计完全一致：

- 不是只保存 tokens；
- 而是把 replay 前仍会变化、但又不能重新分配地址的关键元数据都固定到持久化 buffer 里。

文档依据：`docs/zh/design/graph_mode_design.md:140`。

---

## 5.3 CUDA `update()`：这是整条 graph 逻辑的核心入口

`CudaGraphPersistentParam::update(...)` 在 `xllm/core/runtime/cuda_graph_executor_impl.cpp:235`。

我认为这是整套 CUDA graph 里最关键的函数，因为它做了三件大事：

### 5.3.1 把本轮请求数据拷贝到持久化 buffer

它会把本轮真实请求的：

- `tokens`
- `positions`
- `q_seq_lens`
- `kv_seq_lens`
- `new_cache_slots`
- `block_tables`
- `input_embedding`
- `paged_kv_*`

拷贝到 persistent buffer 对应位置，见：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:276`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:343`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:548`

对于 padding bucket，还会把超出实际 token 的区域补零，例如：

- token padding：`xllm/core/runtime/cuda_graph_executor_impl.cpp:283`
- new cache slots padding：`xllm/core/runtime/cuda_graph_executor_impl.cpp:322`

这正对应设计文档里“capture/replay 共享最大 shape buffer，实际请求只写前缀”的思路：`docs/zh/design/graph_mode_design.md:366`。

### 5.3.2 构造并改写 `AttentionMetadata`

`update()` 先调用 `AttentionMetadataBuilder::build(params)` 构造 attention metadata，见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:249`。

然后马上做了一件非常关键的事：

- `attn_metadata->enable_cuda_graph = true;`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:255`

这意味着：

- 即使上游 `ModelInputParams.enable_cuda_graph` 默认是 false；
- 只要进入 CUDA graph executor，真正用于 capture/replay 的 metadata 会被改写成 graph mode。

随后 `update()` 还会让 metadata 里的：

- `q_cu_seq_lens`
- `kv_cu_seq_lens`
- `slot_mapping`
- `block_table`
- `paged_kv_indptr`
- `paged_kv_indices`
- `paged_kv_last_page_len`
- `qo_indptr`

都改为指向 persistent buffer，见：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:330`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:360`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:585`

这一步本质上是在做：

> 把 attention 看到的所有动态索引/长度/plan 输入，都切换到 graph 可复用的稳定地址版本。

### 5.3.3 更新 plan_info，而不是把旧 planning 结果硬复用

这是本次学习里非常重要的理解点。

`update()` 不只是 copy 数据，还会重新调用：

- `flashinfer::update_prefill_plan_info(...)`
- `flashinfer::update_chunked_prefill_plan_info(...)`
- `flashinfer::update_decode_plan_info(...)`
- 某些路径下还会更新 `xattention::update_xattention_plan_info(...)`

见：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:622`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:635`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:650`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:501`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:528`

这和 `docs/zh/design/graph_mode_design.md:161` 的设计完全吻合：

- bucket 只吸收 `num_tokens` 这个最主要的形态维度；
- 但 bucket 内剩余还能安全参数化的动态信息，例如 `plan_info`、`seq_lens`，仍然要在 replay 前更新；
- 不能把 capture 那一刻的 host planning 结果原封不动冻结后无限重放。

### 5.3.4 对 xAttention 两阶段 decode 的特殊支持

`update()` 里还有一大段 LLM Rec / xattention two-stage decode 的特殊逻辑，见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:389` 到 `xllm/core/runtime/cuda_graph_executor_impl.cpp:546`。

这段逻辑说明：

- CUDA graph 不只是支持最简单的 flashinfer decode；
- 它还专门为 xattention 两阶段 decode 维护 shared / unshared 两套 plan_info；
- 并且要求 shared/unshared cache、expanded paged_kv_* 等信息在 replay 前也能重新绑定。

这进一步说明 xLLM 的 CUDA graph 并不是“只包一层 `CUDAGraph` API”，而是已经深入到了 attention runtime 元数据组织层。

---

## 5.4 CUDA decode full graph 的 capture 过程

真正 capture 单张 decode graph 的逻辑在 `CudaGraph::capture(...)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:682`。

### 5.4.1 先对同卡 capture 做设备级独占保护

代码会在 capture 前拿 device 级写锁：

- `DeviceCaptureLock` 定义：`xllm/core/platform/cuda/device_capture_lock.h:30`
- capture 拿 write lock：`xllm/core/runtime/cuda_graph_executor_impl.cpp:697`

这表示 CUDA 路径的设计是：

- capture 需要独占；
- replay 可以共享；
- 因为 `cudaStreamCaptureMode...` 下，capture 窗口里不能让其它流在同卡上插入破坏 capture 语义的工作。

### 5.4.2 强制切到专用 capture stream

CUDA graph 会使用每线程一个高优先级 capture stream：

- `get_capture_stream(...)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1164`

capture 前会：

1. 取当前 stream；
2. 与 capture stream 互相同步；
3. 用 `CUDAStreamGuard` 切到 capture stream。

见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:709`。

这说明 capture 并不是在默认 compute stream 上随便做，而是有明确隔离的。

### 5.4.3 capture 前先执行 `persistent_param_.update(...)`

也就是先把输入和 attention metadata 全部刷新到 persistent buffer，再拿返回的 `graph_params_opt` 作为 capture 参数：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:722`

这一步很重要，因为 capture 真正看到的输入不是原始 batch tensor，而是“已经绑定到固定地址后的 graph 参数”。

### 5.4.4 decode full graph 的真正 capture

decode 路径会走普通 graph capture 分支：

- `graph_.capture_begin(pool, cudaStreamCaptureModeThreadLocal)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:807`
- 然后执行一次 `model->forward(...)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:812`
- 输出写回 `persistent_param_`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:819`
- 再 `capture_end()`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:826`

这里还有两个细节：

1. capture mode 用的是 `ThreadLocal` 而不是 global；
2. capture 时可以挂接 mempool（普通 graph pool 或 VMM pool）。

### 5.4.5 capture 成功后，第一次请求还会走一次 replay

`CudaGraphExecutorImpl::run()` 在 capture 成功后，并不直接把 capture 时 forward 的结果返回给用户，而是：

- 先把 graph 存入 `graphs_`；
- 再调用一次 `graphs_[bucket]->replay(...)`；
- 让首个请求和后续命中 graph 的请求走同一条执行语义。

证据：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1362`。

这个设计我认为很合理，因为它避免了：

- 第一次请求返回“capture forward 的结果”；
- 后续请求返回“replay 结果”；
- 两条路径行为不一致。

---

## 5.5 CUDA decode replay 过程

`CudaGraph::replay(...)` 在 `xllm/core/runtime/cuda_graph_executor_impl.cpp:844`。

### 5.5.1 replay 用 shared lock，不用 exclusive lock

replay 会拿 device 级 read lock：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:853`

这与 `DeviceCaptureLock` 的读写锁设计一致：

- capture 独占；
- replay 共享。

### 5.5.2 replay 前仍然会重新 update persistent param

这点特别关键：

- replay 不是“拿到图直接 replay”；
- 而是先 `persistent_param_.update(...)`，把本轮的真实 tokens、positions、seq_lens、plan_info 等更新进去；
- 然后再 `graph_.replay()`。

证据：`xllm/core/runtime/cuda_graph_executor_impl.cpp:909`。

这正是“动态维度参数化”的实质落点：

- 图的 launch 形态不变；
- 但同一 bucket 内可参数化的动态数据，仍然在 replay 前被刷新。

### 5.5.3 返回值只取实际 token 对应的 hidden states slice

因为 capture 常常按 bucket 的 padded 形状建图，所以 replay 完后返回的只是前 `actual_num_tokens` 的部分：

- `return ModelOutput(get_hidden_states(actual_num_tokens));`
- 见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:922`

这就是 bucket + padding 下结果回收的标准做法。

---

## 5.6 CUDA prefill 为什么不是 full graph，而是 piecewise graph

这个问题设计文档讲得很清楚：

- decode 下，`num_tokens` 与 `batch_size` 基本同步；
- chunked prefill 下，同一个 `num_tokens` 可以对应完全不同的 `batch_size / q_seq_lens / kv_seq_lens` 组合；
- attention 的任务划分、workspace、索引、plan_info 都会变化；
- 所以 attention 这一段执行形态并不适合只靠 `num_tokens` bucket 来复用 full graph。

文档证据：`docs/zh/design/graph_mode_design.md:203`。

### 5.6.1 prefill 进入 piecewise graph 的条件

在 `CudaGraphExecutorImpl::run()` 中：

- 只有 `is_prefill && enable_prefill_piecewise_graph_` 才会走 piecewise path；
- 还要求 `n_tokens <= max_tokens_for_graph_mode_`；
- 否则直接 eager。

见：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:1214`
- `xllm/core/runtime/cuda_graph_executor_impl.cpp:1217`

### 5.6.2 prefill piecewise graph 也按 bucket 懒加载

它维护的是另一张 map：

- `prefill_graphs_`：`xllm/core/runtime/cuda_graph_executor_impl.h:293`

这表示 decode full graph 和 prefill piecewise graph 是分开缓存的，而不是混在一个 map 里。

### 5.6.3 piecewise capture 前会先 warmup

在 `CudaGraph::capture(...)` 的 piecewise 分支里，capture 前会先执行一次普通 forward：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:744`

注释写得很明确：

- 这是为了初始化 cuBLAS handle 和其它不能在 capture 期间创建的 CUDA 资源。

这也意味着：

> prefill 首次 piecewise capture 的代价比 decode 首次 capture 更高。

### 5.6.4 piecewise capture 的底层组织：`GlobalCaptureInstance`

piecewise capture 不是直接一张 `CUDAGraph` 解决，而是：

1. `GlobalCaptureInstance::begin_capture(pool)` 开始总 capture：`xllm/core/kernels/cuda/global_capture_instance.cpp:43`
2. forward 执行时，遇到 attention 会临时结束当前 graph 片段；
3. attention 自己不进入图，而是记录成 `AttentionRunner`；
4. attention 之后再开启新的 graph 片段；
5. 最后 `end_capture()` 返回 `PiecewiseGraphs`。

相关实现：

- `PiecewiseGraphs`：`xllm/core/kernels/cuda/piecewise_graphs.h:23`
- `GlobalCaptureInstance`：`xllm/core/kernels/cuda/global_capture_instance.h:34`
- attention runner：`xllm/core/kernels/cuda/attention_runner.h:37`

### 5.6.5 `AttentionRunner` 到底做了什么

在 capture 期间，`AttentionRunner::run_capture(...)`：

- 先 `temporarily_end_graph()` 结束当前图片段；
- 把 attention 所需的 workspace、query/key/value/output、window、scale 等保存起来；
- **注意：capture 期间它并不执行 attention 计算本身**；
- 然后 `temporarily_begin_graph()` 开始后续图片段。

证据：`xllm/core/kernels/cuda/attention_runner.cpp:28`。

这就是 piecewise graph 的本质：

- 非 attention 的稳定部分进入图；
- attention 这类 shape 敏感部分被“抽出来”作为 runner；
- replay 时按原顺序把 graph 与 runner 串起来。

### 5.6.6 replay 时怎样恢复原始顺序

`PiecewiseGraphs::replay(...)` 按 capture 期间记录下来的 `instructions_` 顺序依次执行：

- graph -> `CUDAGraph::replay()`
- runner -> `AttentionRunner::run_replay(...)`

见：`xllm/core/kernels/cuda/piecewise_graphs.cpp:35`

而 `AttentionRunner::run_replay(...)` 会：

- 根据真实 `actual_num_tokens` 对 query/key/value/output 做 slice；
- 用**最新的** `plan_info`、`q_cu_seq_lens`、`kv_cu_seq_lens` 重新执行 attention；

见：`xllm/core/kernels/cuda/attention_runner.cpp:72`。

这与设计文档 `docs/zh/design/graph_mode_design.md:265` 完全一致。

---

## 5.7 CUDA attention 层为什么能配合 graph replay

这点也必须说明白，否则只看 executor 还不够。

### 5.7.1 FlashInfer attention 的行为

在 `flashinfer_attention.cpp` 中：

- 如果 `attn_metadata.enable_cuda_graph == true`，则不再重新更新 plan_info，只要求已有 plan_info 已经准备好；
- 如果是 eager，则在 layer 内临时 `update_*_plan_info(...)`。

见：

- prefill：`xllm/core/layers/cuda/flashinfer_attention.cpp:173`
- chunked prefill：`xllm/core/layers/cuda/flashinfer_attention.cpp:244`
- decode：`xllm/core/layers/cuda/flashinfer_attention.cpp:306`

这说明 CUDA graph 的分层很清晰：

- executor 负责在 replay 前准备好 plan_info；
- layer 看到 `enable_cuda_graph=true` 后就不再做额外 host planning。

### 5.7.2 XAttention 的行为

`xattention.cpp` 也是同样模式：

- graph 模式下检查 plan_info 已经存在；
- eager 模式下才重新 update。

见：

- single stage decode：`xllm/core/layers/cuda/xattention.cpp:53`
- shared/unshared 两阶段 decode：`xllm/core/layers/cuda/xattention.cpp:189`、`xllm/core/layers/cuda/xattention.cpp:258`
- prefill：`xllm/core/layers/cuda/xattention.cpp:348`

所以 CUDA graph 的真正落地并不是 executor 单独完成，而是：

> executor 负责图外准备动态元数据，attention layer 在 graph 模式下消费这些现成元数据，而不再自己重新做 planning。

---

## 5.8 CUDA 的分桶规则与 no padding 模式

分桶函数在 `CudaGraphExecutorImpl::get_bucket_num_tokens(...)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1386`。

规则是：

- `enable_graph_mode_decode_no_padding=true` 且不是 prefill 时，bucket 直接等于真实 `num_tokens`；
- 否则：
  - `1 -> 1`
  - `2 -> 2`
  - `3~4 -> 4`
  - `5~8 -> 8`
  - `>8` 时向上取整到 **16 的倍数**。

### 5.8.1 一个值得记录的小观察

头文件注释写的是“`num_tokens >= 8` 用 8 的倍数”，见 `xllm/core/runtime/cuda_graph_executor_impl.h:311`；

但实际实现是：

```cpp
return ((num_tokens + 15) / 16) * 16;
```

即**向上取整到 16 的倍数**，见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:1401`。

这说明当前代码里“注释和实现并不一致”，学习时应以实现为准。

### 5.8.2 为什么 prefill 不支持 no padding

实现里明确写了：

- `no_padding only works for decode, prefill requires padding for graph reuse`

见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:1388`。

这很好理解：

- decode full graph 的 shape 稳定性高；
- prefill 尤其 piecewise prefill 要靠 padding 和统一 bucket 来复用更多路径。

---

## 5.9 CUDA 的 VMM memory pool：这是多 shape 显存复用的关键实现

这是 CUDA 路径最有技术含量的一部分。

### 5.9.1 设计目标

设计文档已经说得很清楚：

- 不同 shape capture 不能简单复用同一虚拟地址；
- 否则会破坏 allocator 对地址的跟踪；
- 但又希望底层物理显存收敛到 `max(shape)` 而不是 `sum(shape)`。

见：`docs/zh/design/graph_mode_design.md:338` 以后。

### 5.9.2 xLLM 的落地对象

CUDA graph executor 里，VMM 相关核心对象是：

- `SharedVMMAllocator`：`xllm/core/platform/shared_vmm_allocator.h:26`
- `VMMTorchAllocator`：`xllm/core/platform/vmm_torch_allocator.h:1`
- executor 内的 `VmmPoolState`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:970`

### 5.9.3 每个 physical pool 对应一套共享物理内存

executor 把 prefill 和 decode 分成两个 physical pool：

- `kPhysicalPoolIdPrefill = 0`
- `kPhysicalPoolIdDecode = 1`

见：`xllm/core/runtime/cuda_graph_executor_impl.cpp:963`。

这说明 prefill 和 decode 的 capture 内存不会混在一套物理池里。

### 5.9.4 capture 前切换到新的虚拟地址空间

当启用 `FLAGS_enable_graph_vmm_pool` 时，capture 前会：

- `reset_vmm_allocator_offset(physical_pool_id)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1131`

而这个函数底层调用：

- `SharedVMMAllocator::switch_to_new_virtual_space()`：`xllm/core/platform/shared_vmm_allocator.cpp:130`

这个函数的核心逻辑正是：

1. 创建新的 virtual space；
2. 把已有 physical handles 全部映射到新的 virtual space；
3. 把当前 offset 归零；

见：`xllm/core/platform/shared_vmm_allocator.cpp:141`。

这正是设计文档里的“新虚拟地址空间 + 共享物理内存”方案。

### 5.9.5 shape 维度上的 mempool

每个 `(physical_pool_id, shape_id)` 对应一个 `c10::cuda::MemPool`，见：

- `get_or_create_vmm_mempool(...)`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1002`

所以：

- shape 有自己的 mempool 句柄；
- 但底层 allocator 指向的是同一个 `SharedVMMAllocator`；
- 因而 graph 看到的是各自稳定的地址视图，底层物理显存却能共享。

### 5.9.6 如果不用 VMM，会退化成什么

如果 `enable_graph_vmm_pool=false`，executor 就只使用一个普通的 `graph_pool_`：

- `graph_pool_ = at::cuda::graph_pool_handle()`：`xllm/core/runtime/cuda_graph_executor_impl.cpp:941`
- `get_mem_pool()` 直接返回这个 pool：`xllm/core/runtime/cuda_graph_executor_impl.cpp:1145`

这时不同 shape capture 更容易出现显存按 shape 累积增长。

### 5.9.7 测试已经明确验证了 VMM 的效果

`CudaGraphExecutorTest.GraphVmmPoolMemoryReuseAcrossMultiShape` 明确验证：

- 开启 VMM 时，多 shape prefill capture 的 memory usage 保持稳定；
- 关闭 VMM 时，memory usage 会增长。

见：`xllm/core/runtime/cuda_graph_executor_test.cpp:539`。

同时 `GraphVmmPoolEnabledPrefillCorrectness` 还验证了启用 VMM 后输出仍与 eager 对齐，见 `xllm/core/runtime/cuda_graph_executor_test.cpp:647`。

所以这个设计不是只停留在文档里，而是有测试证据支撑的。

---

## 5.10 CUDA 路径的失败策略

这里有几个重要的工程取舍。

### 5.10.1 prefill token 超限时：回 eager

如果 `n_tokens > max_tokens_for_graph_mode_`，piecewise prefill 直接回 eager：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:1217`

### 5.10.2 seq_len 不支持时：回 eager

decode 下如果 `kv_max_seq_len > args_.max_position_embeddings()`，也直接回 eager：

- `xllm/core/runtime/cuda_graph_executor_impl.cpp:1302`

### 5.10.3 一旦进入图模式但 capture 失败：直接 `LOG(FATAL)`

这点非常值得记住。

在 CUDA 路径里：

- piecewise capture 失败：`LOG(FATAL)`，见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:1284`
- decode capture 失败：`LOG(FATAL)`，见 `xllm/core/runtime/cuda_graph_executor_impl.cpp:1369`

注释解释得很清楚：

- 不希望进入 graph mode 后再默默退回 eager，因为这会掩盖 allocator / capture regressions，并让线上 latency 行为变得不确定。

这是典型的“显式失败优先于静默降级”的工程策略。

---

## 6. ACL Graph：详细逻辑拆解

ACL graph 的整体风格和 CUDA graph 很像，但实现重点明显不同。

如果说 CUDA graph 的核心难点是：

- FlashInfer / XAttention 的 plan_info 更新；
- prefill piecewise graph；
- 多 shape VMM 物理显存复用；

那么 ACL graph 的核心难点则是：

- NPU capture 对 stream / 同步更敏感；
- attention 的 planning/tiling 很大一部分在 ATB / CustomPagedAttention；
- replay 不能出现 `.to(kCPU)` 这类破坏 capture 的 host 参与操作。

---

## 6.1 ACL graph 也是三层，但偏 NPU/ATB 定制

### 6.1.1 `AclGraphExecutorImpl`

负责：

- `run()`
- bucket 选择
- lazy capture / replay

见：`xllm/core/runtime/acl_graph_executor_impl.h:293`

### 6.1.2 `AclGraph`

负责单个 bucket 对应的一张 `NPUGraph`：

- `capture()`
- `replay()`
- capture stream 初始化

见：`xllm/core/runtime/acl_graph_executor_impl.h:239`

### 6.1.3 `GraphPersistentParam`

负责 NPU 图执行的持久化数据和 planning 环境：

- persistent tensors
- 可选 attention mask
- tiling_data
- ATB context / operation / planning stream

见：`xllm/core/runtime/acl_graph_executor_impl.h:56`

---

## 6.2 ACL persistent param 的 NPU 特有内容

`GraphPersistentParam::GraphPersistentParam(...)` 在 `xllm/core/runtime/acl_graph_executor_impl.cpp:62`。

### 6.2.1 mRoPE position 的特殊形状

如果模型启用了 mRoPE，则 positions 不是 `[num_tokens]`，而是 `[3, num_tokens]`：

- `use_mrope_` 判断：`xllm/core/runtime/acl_graph_executor_impl.cpp:78`
- positions 分配：`xllm/core/runtime/acl_graph_executor_impl.cpp:93`

这说明 ACL graph 已经考虑了部分多模态/特殊位置编码模型的需要。

### 6.2.2 decode graph capacity 与 `num_decoding_tokens()` 绑定

ACL 这边用于分配 seq buffers 的 capacity 不是简单 `max_seqs_per_batch`，而是：

- 如果 `FLAGS_enable_atb_spec_kernel` 开启，则用 `max_seqs_per_batch()`；
- 否则用 `max_seqs_per_batch() * num_decoding_tokens()`。

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:51`

这说明 NPU decode graph 的容量估计与 speculative / decode token 组织方式是耦合的。

### 6.2.3 ACL 专有的 tiling planning 环境

这是 ACL 路径最特别的一点。

构造函数里如果 `head_dim != 0`，会初始化 paged attention plan context：

- `initialize_paged_attention_plan_context(device)`：`xllm/core/runtime/acl_graph_executor_impl.cpp:141`

里面会创建：

- `tiling_data_` 设备侧持久化 buffer：`xllm/core/runtime/acl_graph_executor_impl.cpp:328`
- `ATB` plan context：`xllm/core/runtime/acl_graph_executor_impl.cpp:335`
- 独立 planning stream：`xllm/core/runtime/acl_graph_executor_impl.cpp:340`
- custom paged attention operation：`xllm/core/runtime/acl_graph_executor_impl.cpp:351`

并把 launch mode 设为 `GRAPH_LAUNCH_MODE`：`xllm/core/runtime/acl_graph_executor_impl.cpp:346`。

这说明 ACL graph 的 planning 并不是纯粹在 executor 里用几个普通 tensor 完成的，而是依赖 NPU / ATB 体系的专门 planning 上下文。

---

## 6.3 ACL `update()`：除了 copy persistent buffer，还要准备 tiling_data

`GraphPersistentParam::update(...)` 在 `xllm/core/runtime/acl_graph_executor_impl.cpp:186`。

### 6.3.1 和 CUDA 一样，先拷贝本轮真实输入

包括：

- tokens
- positions（mRoPE 时按 dim=1 slice）
- q / kv seq lens
- new cache slots
- block tables
- input embedding
- q_cu_seq_lens（如果有）

见：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:199`
- `xllm/core/runtime/acl_graph_executor_impl.cpp:221`
- `xllm/core/runtime/acl_graph_executor_impl.cpp:250`

### 6.3.2 可选地更新 attention mask

如果 `need_update_attn_mask_` 为真，则还会更新 `persistent_mask_`：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:263`
- 具体生成逻辑：`xllm/core/runtime/acl_graph_executor_impl.cpp:666`

不过当前 `AclGraphExecutorImpl` 构造 `GraphPersistentParam` 时使用的是默认参数，没有显式打开这个开关：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:929`

所以**当前常规 ACL decode graph 主路径并不依赖这套 attn mask 更新机制**，但代码预留了能力。

### 6.3.3 更关键的是更新 `tiling_data_`

如果 `tiling_data_` 已经初始化，则 `update()` 会：

- 获取当前 NPU stream；
- 如果当前模型类型需要更新 attention plan，则调用 `plan_paged_attention_tiling(...)`；

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:268`。

而 `plan_paged_attention_tiling(...)` 会：

1. 构造 ATB tensor/variant pack；
2. 调用 custom paged attention op 的 `Setup(...)`；
3. 取出 host tiling buffer；
4. 再把 host tiling buffer 异步拷到设备侧的 `tiling_data_`。

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:569`。

我认为这是 ACL graph 和 CUDA graph 最本质的差异之一：

- CUDA 路径更像“更新 plan_info tensor / workspace 元数据”；
- ACL 路径更像“预先准备 NPU 自定义 paged attention 的 tiling data，并把它搬到设备上”。

### 6.3.4 capture 参数如何构造

如果 `return_capture_params=true`，ACL `update()` 会返回一份新的 `params_for_capture`，里面：

- `kv_seq_lens` / `q_seq_lens` 指向 persistent buffer；
- `kv_seq_lens_vec` / `q_seq_lens_vec` 被补齐到 `padded_num_tokens`；
- `num_sequences` 被改成 bucket 大小；
- `batch_forward_type` 被强制设为 `DECODE`；
- `new_cache_slots` / `block_tables` 指向 persistent buffer；
- `graph_buffer.tiling_data` 指向设备侧 tiling_data；
- 如有 embedding 也切到 persistent embedding。

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:279`。

这里要特别注意：ACL 路径把 `graph_buffer.tiling_data` 带进了 capture params。这个字段后面会在 attention metadata builder 和 NPU attention 实现里起关键作用。

---

## 6.4 ACL graph 如何接入 NPU attention 路径

这是 ACL graph 非常关键的一条证据链。

### 6.4.1 metadata builder 会识别 ACL graph 条件

`AttentionMetadataBuilder::build(...)` 在 NPU 下会判断：

- `FLAGS_enable_graph` 必须开；
- 必须是 decode（不是 prefill/mixed/chunked_prefill）；
- `params.graph_buffer.tiling_data` 必须存在；

如果满足，就把：

- `attn_metadata.paged_attention_tiling_data = params.graph_buffer.tiling_data`

见：`xllm/core/layers/common/attention_metadata_builder.cpp:66`。

也就是说，ACL graph 的“graph 模式 attention”不是靠 `enable_cuda_graph` 这个布尔标志驱动的，而是靠：

> metadata 里有没有 `paged_attention_tiling_data`。

### 6.4.2 NPU attention impl 根据 `paged_attention_tiling_data` 选执行路径

`xllm/core/layers/npu_torch/attention.cpp:127` 明确写了：

- 如果 `attn_metadata.paged_attention_tiling_data` 已定义，走 `batch_decode_acl_graph(...)`；
- 否则走普通 `batch_decode(...)`。

而 `npu_ops_api.h` 里对 `batch_decode_acl_graph(...)` 的注释也写得很直白：

- 它使用 `CustomPagedAttention`，目的是避免 `.to(kCPU)` 这种会打断 ACL graph capture 的操作。

见：`xllm/core/kernels/npu/npu_ops_api.h:48`。

这条证据链完整说明了 ACL graph 的 attention 适配思路：

```text
GraphPersistentParam::update
  -> graph_buffer.tiling_data
  -> AttentionMetadataBuilder 检测到 ACL graph 条件
  -> attn_metadata.paged_attention_tiling_data
  -> NPU attention 走 CustomPagedAttention 路径
```

这个链路非常关键，因为它说明 ACL graph 并不是简单“把整条 forward capture 一下”，而是已经深入改造到了 attention kernel 选路层。

---

## 6.5 ACL capture 过程

`AclGraph::capture(...)` 在 `xllm/core/runtime/acl_graph_executor_impl.cpp:773`。

### 6.5.1 capture 前会先同步 NPU

一开始就有：

- `torch::npu::synchronize()`：`xllm/core/runtime/acl_graph_executor_impl.cpp:789`

然后又拿当前 stream 做同步：

- `aclrtSynchronizeStream(stream)`：`xllm/core/runtime/acl_graph_executor_impl.cpp:816`

这说明 ACL graph capture 对“capture 开始前设备状态干净”要求很强。

### 6.5.2 先 update persistent param，再开始 capture

和 CUDA 路径一样，capture 前先：

- 取 k/v cache；
- 调 `persistent_param_.update(..., return_capture_params=true)`；
- 拿返回的 graph params 做 capture。

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:796`。

### 6.5.3 ACL capture 要求非默认流，并且要拿设备级 mutex

`initialize_capture_stream(...)` 会从高优先级 pool 取一个非默认 stream：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:875`

capture 时如果当前流还是 default stream，就切到 capture stream：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:833`

同时，它还会拿 `DeviceCaptureLock` 的 device 级互斥锁：

- 锁定义：`xllm/core/platform/npu/device_capture_lock.h:29`
- capture 拿锁：`xllm/core/runtime/acl_graph_executor_impl.cpp:827`

这点跟 CUDA 不一样：

- CUDA 用读写锁，capture 独占、replay 共享；
- ACL 直接用普通 mutex，强调 capture 与其他准备/同步操作不能同时发生。

### 6.5.4 为什么 ACL 路径对锁这么敏感

`WorkerImpl::prepare_work_before_execute()` 里有非常重要的注释：

- 即使 capture 和同步流不是同一个流，其他线程异步调度的数据更新流，也可能打断 ACL graph capture；
- 原因可能是 ACL capture 会使用附加辅助流，而这些辅助流可能与数据更新流重叠。

见：`xllm/core/runtime/worker_impl.cpp:505`。

这说明 ACL graph 的运行时约束比 CUDA graph 更严格，必须把 worker 侧 prepare 阶段和 graph capture 阶段一起协调起来。

### 6.5.5 真正的 NPUGraph capture

在拿锁、切流后：

- `graph_.capture_begin({0, 0}, ACL_MODEL_RI_CAPTURE_MODE_THREAD_LOCAL)`：`xllm/core/runtime/acl_graph_executor_impl.cpp:844`
- 然后执行 `model->forward(...)`：`xllm/core/runtime/acl_graph_executor_impl.cpp:847`
- 输出写回 persistent hidden_states / aux_hidden_states：`xllm/core/runtime/acl_graph_executor_impl.cpp:853`
- 最后 `capture_end()`：`xllm/core/runtime/acl_graph_executor_impl.cpp:859`

这里还有一个明显差异：

- CUDA capture 可以显式传 mempool id；
- ACL 这里是 `{0,0}`，注释写明“no mempool id, will create a new one”，见 `xllm/core/runtime/acl_graph_executor_impl.cpp:842`。

也就是说 ACL graph 当前没有像 CUDA 那样把多 shape memory reuse 做到 executor 内。

### 6.5.6 capture 完后会立即 replay 一次做验证

capture 结束后：

- `aclrtSynchronizeStream(stream)`
- `graph_.replay()`

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:866`。

这和 CUDA 的“capture 成功后由 executor 再 replay 一次”不一样：

- ACL 是在 `capture()` 内部就 replay 一次做验证；
- `run()` 捕获成功后直接返回 persistent hidden_states；
- 不会再单独 replay 第二次给首个请求。

因此 ACL 首次请求的行为可以理解为：

1. capture forward 一次；
2. 再 replay 一次验证；
3. 返回 persistent hidden_states。

---

## 6.6 ACL replay 过程

`AclGraph::replay(...)` 在 `xllm/core/runtime/acl_graph_executor_impl.cpp:887`。

### 6.6.1 replay 前同样先刷新 persistent buffer

与 CUDA 一样，ACL replay 前也不是直接 replay，而是先：

- `persistent_param_.update(..., return_capture_params=false)`

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:896`。

### 6.6.2 然后直接 `graph_.replay()`

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:911`。

最后返回的是实际 token 范围内的 hidden states slice：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:918`

### 6.6.3 ACL replay 不像 CUDA 那样区分 shared/exclusive replay 锁

ACL replay 代码里没有像 CUDA 那样的 device read lock；

它的并发保护更多发生在：

- capture 阶段的 device mutex；
- worker prepare 阶段对 capture 互斥的配合。

这再次说明两者在并发模型上的工程选择不同。

---

## 6.7 ACL graph 只覆盖 decode 主路径，不做 prefill piecewise graph

`AclGraphExecutorImpl::run()` 明确写了：

- 如果不是 decode，或者 `n_layers == 1`，直接 eager：`xllm/core/runtime/acl_graph_executor_impl.cpp:955`

所以 ACL graph 当前主路径是：

> **decode only**

这和 CUDA 路径形成鲜明对比：

- CUDA：decode full graph + prefill piecewise graph
- ACL：decode graph，prefill 不走同样的 piecewise 体系

这也能解释为什么 `docs/zh/design/graph_mode_design.md` 里对 piecewise graph 的主要论述更偏 CUDA/FlashInfer 一侧。

---

## 6.8 ACL 的分桶规则与 no padding

ACL 的 bucket 函数在：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:1070`

规则和 CUDA decode 很相似：

- 开 `enable_graph_mode_decode_no_padding` 则 bucket = `num_tokens`
- 否则 `1,2,4,8`，再向上取整到 **16 的倍数**

### 6.8.1 这里也存在“注释和实现不一致”

头文件注释写的是 “For num_tokens >= 8: use multiples of 8”，见：

- `xllm/core/runtime/acl_graph_executor_impl.h:324`

但实现仍然是：

```cpp
return ((num_tokens + 15) / 16) * 16;
```

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:1084`。

所以 ACL 和 CUDA 在这个点上都表现出同一个现象：

- 注释说 8 倍数；
- 真实代码是 16 倍数。

---

## 6.9 ACL 路径的失败策略

ACL 和 CUDA 在失败策略上差别很大。

### 6.9.1 不满足 graph 条件时：回 eager

例如：

- 非 decode；
- `n_layers == 1`；
- `kv_max_seq_len > max_position_embeddings`；

都会回 eager，见：

- `xllm/core/runtime/acl_graph_executor_impl.cpp:955`
- `xllm/core/runtime/acl_graph_executor_impl.cpp:978`

### 6.9.2 capture 失败时：记录错误并回 eager

如果 capture 失败，ACL 路径不是 `LOG(FATAL)`，而是：

- 打错误日志；
- 计数 eager；
- 再 `model_->forward(...)` 回 eager。

见：`xllm/core/runtime/acl_graph_executor_impl.cpp:1042`。

这和 CUDA 的“进入 graph mode 后失败就 fatal”是明显不同的工程哲学：

- CUDA 更强调稳定语义和显式失败；
- ACL 更强调失败时的可退化运行。

---

## 7. CUDA graph 与 ACL graph 的核心差异总结

下面用一个表把我认为最关键的差异压缩出来。

| 维度 | CUDA graph | ACL graph |
|---|---|---|
| 接入执行器 | `CudaGraphExecutorImpl` | `AclGraphExecutorImpl` |
| 覆盖阶段 | decode full graph + prefill piecewise graph | 主要是 decode graph |
| 图实例类型 | `CUDAGraph` / `PiecewiseGraphs` | `NPUGraph` |
| 动态元数据核心 | `plan_info`、paged_kv_*、q/kv cu seq lens | `tiling_data`、kv/q seq lens、可选 mask |
| attention graph 适配方式 | layer 在 `enable_cuda_graph=true` 时不再更新 plan_info，直接消费 executor 提前准备好的 metadata | builder 检测 `tiling_data`，attention 改走 `batch_decode_acl_graph()` / CustomPagedAttention |
| prefill 支持 | 有 piecewise graph | 当前主路径没有同等 piecewise 体系 |
| 多 shape 显存复用 | 有 `SharedVMMAllocator` + per-shape MemPool | 当前无同等 VMM 多 shape 方案 |
| capture 并发保护 | device 级读写锁，capture 独占、replay 共享 | device 级 mutex，配合 worker prepare 阶段一起互斥 |
| 首次请求行为 | capture 后再 replay，保证首轮与后续一致 | capture 内部 replay 一次验证，run 直接返回 persistent 输出 |
| capture 失败策略 | 进入 graph mode 后失败直接 `LOG(FATAL)` | capture 失败回 eager |

---

## 8. 我对两条实现的“本质理解”

如果不陷入实现细节，我现在对这两条图执行链的本质理解是：

### 8.1 CUDA graph 的本质

> **用 bucket + persistent buffer + plan_info 参数化 + piecewise graph + VMM memory pool，把 GPU 侧 graph mode 做成了一个可覆盖 decode 与部分 prefill 的统一运行时系统。**

它最强的地方不只是 capture/replay 本身，而是：

- 能把 FlashInfer / XAttention 的 planning 也纳入统一 runtime 组织；
- 能对 prefill 做 piecewise graph；
- 能对多 shape capture 做物理显存复用。

### 8.2 ACL graph 的本质

> **用 persistent buffer + tiling_data 设备化 + CustomPagedAttention 路径替换，把原本会破坏 NPU graph capture 的 host planning / host tensor 依赖尽量提前清理掉，从而让 decode attention 能稳定落在 NPUGraph replay 上。**

它的重点不是 piecewise，也不是多 shape VMM，而是：

- NPU capture 的 stream/同步约束；
- ATB planning 与 tiling buffer 的图外准备；
- 避免 `.to(kCPU)` 等 host 参与操作。

---

## 9. 结合设计文档，再回看 xLLM graph mode 的三大设计点

设计文档的三大关键词是：

1. 动态维度参数化
2. Piecewise Graph
3. 多 shape 复用显存池

结合源码以后，可以更精确地理解成：

### 9.1 动态维度参数化：主要落在 `persistent_param_.update()`

不是 scheduler 参数化，也不是 layer 自己参数化，而是 graph executor 在 replay 前：

- 刷新 persistent buffer；
- 刷新 attention metadata；
- 刷新 plan_info / tiling_data。

### 9.2 Piecewise Graph：当前主要是 CUDA prefill 方案

不是通用全后端方案，而是**围绕 CUDA / FlashInfer attention 的动态性**设计出来的工程解法。

### 9.3 多 shape 显存复用：当前主要是 CUDA VMM 方案

设计文档讲的是统一思路，但仓内真正完整落地的“physical memory 共享 + virtual address space 分离”实现，主要在 CUDA 路径上，证据就是：

- `SharedVMMAllocator`
- `VMMTorchAllocator`
- `CudaGraphExecutorImpl` 的 VMM pool 逻辑

ACL 路径当前没有对等实现。

---

## 10. 几个特别值得后续继续深挖的点

### 10.1 `enable_graph` 的配置分层还不够“纯 runtime 化`

当前既有：

- `options.enable_graph()`
- 又有 `FLAGS_enable_graph` 直接控制 executor 选路和 layer 分支

如果以后要把 graph mode 做得更“配置层次清晰”，这块值得继续收口。

### 10.2 ACL graph 的 `need_update_attn_mask_` 目前主路径没有明显打开

代码能力已经存在，但当前 `AclGraphExecutorImpl` 默认构造路径没有打开它。后续如果遇到更复杂的 chunked / multimodal NPU graph 场景，这块可能会变重要。

### 10.3 ACL graph 和 CUDA graph 的失败策略不一致

这不是 bug，但它是很明显的产品/工程选择差异：

- CUDA 强调 graph 语义稳定性；
- ACL 更强调可退化运行。

如果后续要统一 graph mode 的运维行为，这会是一个值得讨论的问题。

---

## 11. 最后给出一条我认为最重要的总链路

### 11.1 CUDA graph 总链路

```text
LLMWorkerImpl::step_internal
  -> Executor::forward
  -> CudaGraphExecutorImpl::run
      -> bucket_num_tokens
      -> persistent_param_.update
      -> (已有图 ? replay : capture + cache + replay)
      -> model->forward
          -> attention layer 读取 attn_metadata.plan_info / persistent buffers
          -> graph mode 下不再在 layer 内重新做 host planning
```

prefill 时则变成：

```text
CudaGraphExecutorImpl::run
  -> prefill_graphs_[bucket]
  -> CudaGraph::capture(is_piecewise=true)
      -> GlobalCaptureInstance begin/end
      -> graph piece + AttentionRunner 录制
  -> PiecewiseGraphs::replay
      -> graph -> runner -> graph -> ...
```

### 11.2 ACL graph 总链路

```text
LLMWorkerImpl::step_internal
  -> Executor::forward
  -> AclGraphExecutorImpl::run
      -> 仅 decode 才尝试 graph
      -> bucket_num_tokens
      -> persistent_param_.update
          -> 刷新 persistent buffers
          -> 刷新 tiling_data
      -> (已有图 ? replay : capture + cache)
      -> model->forward
          -> AttentionMetadataBuilder 识别 tiling_data
          -> NPU attention 走 batch_decode_acl_graph / CustomPagedAttention
```

---

## 12. 本次学习的最终结论

### 12.1 最核心结论

ACL graph 和 CUDA graph 在 xLLM 中都不是“简单调用后端 graph API”这么浅的一层，而是已经深入到：

- executor 选路
- persistent buffer 组织
- attention metadata 更新
- planning / tiling 数据准备
- memory pool / stream / lock 管理

它们本质上都是**运行时执行骨架的一部分**。

### 12.2 两条路径各自的代表性关键词

- CUDA graph：`plan_info`、piecewise graph、VMM memory pool、full graph decode、prefill graph
- ACL graph：`tiling_data`、CustomPagedAttention、NPUGraph、capture stream、device mutex

### 12.3 一句话总结

> xLLM 的 graph mode 不是“把 eager forward 包一层 capture/replay”，而是把**输入地址稳定化、attention 动态元数据设备化、后端 planning 图外化、capture/replay 生命周期管理**全部系统化之后，才真正落地成 ACL graph / CUDA graph 两套执行路径。

---

## 13. 证据边界

本笔记已经可以比较扎实地解释：

- graph mode 在架构中的位置；
- ACL / CUDA graph 的主执行链；
- capture/replay/persistent buffer 的实现；
- CUDA piecewise graph 和 VMM 方案；
- ACL tiling_data / CustomPagedAttention 方案。

但以下内容这次没有继续深挖到底：

- 具体模型（如 Qwen3 / GLM / DeepSeek）在每一层对 graph mode 的适配差异；
- NPU ATB / CustomPagedAttention 的更底层算子细节；
- graph 与 schedule overlap、xtensor、rolling load 的交互细节。

如果后续继续深入，最自然的下一步是：

1. 逐模型核对 graph 兼容路径；
2. 专门研究 NPU decoder layer 如何消费 tiling_data；
3. 继续把 graph mode 与 xtensor / overlap / PD 场景串起来。
