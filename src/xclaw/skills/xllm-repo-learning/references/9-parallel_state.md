# xLLM 中 parallel_state 逻辑学习笔记

> 说明：用户问题里写的是 `ralph/learn_xllm/overview.md` 和 `ralph/learn_xllm/structure.md`，但仓库里对应的实际文件名是 `ralph/learn_xllm/0-overview.md` 和 `ralph/learn_xllm/1-structure.md`。本文以这两份学习笔记为入口，再结合源码系统梳理 `parallel_state` 的设计与运行逻辑。

## 1. 本次学习范围

这次我没有只盯着 `parallel_state.h/.cpp` 两个文件看，而是沿着“并行拓扑如何被创建、传递、消费”的整条链路往前后都追了一遍。实际重点阅读的文件包括：

- 学习入口
  - `ralph/learn_xllm/0-overview.md`
  - `ralph/learn_xllm/1-structure.md`
  - `ralph/learn_xllm/task.md`
- parallel_state 本体
  - `xllm/core/framework/parallel_state/parallel_args.h`
  - `xllm/core/framework/parallel_state/parallel_state.h`
  - `xllm/core/framework/parallel_state/parallel_state.cpp`
  - `xllm/core/framework/parallel_state/parallel_state_async.cpp`
  - `xllm/core/framework/parallel_state/process_group.h`
  - `xllm/core/framework/parallel_state/process_group.cpp`
  - `xllm/core/framework/parallel_state/collective_communicator.h`
  - `xllm/core/framework/parallel_state/collective_communicator.cpp`
  - `xllm/core/framework/parallel_state/cuda_process_group.h`
  - `xllm/core/framework/parallel_state/npu_process_group.h`
  - `xllm/core/framework/parallel_state/npu_process_group.cpp`
  - `xllm/core/framework/parallel_state/mapping_npu.h`
  - `xllm/core/framework/parallel_state/tests/parallel_state_tests.cpp`
- 创建与注入主链
  - `xllm/core/distributed_runtime/dist_manager.cpp`
  - `xllm/core/distributed_runtime/worker_server.cpp`
  - `xllm/core/distributed_runtime/spawn_worker_server/spawn_worker_server.cpp`
  - `xllm/core/runtime/worker_impl.cpp`
  - `xllm/core/runtime/llm_worker_impl.cpp`
  - `xllm/core/framework/model_context.h`
  - `xllm/core/framework/model_context.cpp`
- 代表性消费点
  - `xllm/core/layers/common/linear.cpp`
  - `xllm/core/layers/common/word_embedding_impl.cpp`
  - `xllm/core/layers/common/qwen2_vision_attention.cpp`
  - `xllm/core/layers/common/dp_utils.cpp`
  - `xllm/core/layers/npu_torch/fused_moe.cpp`
  - `xllm/core/layers/mlu/deepseek_v2_attention.cpp`
  - `xllm/core/layers/mlu/deepseek_v32_sp_context.h`
  - `xllm/models/llm/deepseek_v32.h`
- 本地引擎特例
  - `xllm/core/distributed_runtime/dit_engine.cpp`
  - `xllm/core/distributed_runtime/rec_engine.cpp`
  - `xllm/core/runtime/dit_worker.h`
  - `xllm/core/distributed_runtime/llm_engine.cpp`

---

## 2. 先给结论：parallel_state 在 xLLM 里到底是什么

### 2.1 它不是一个“全局并行状态单例”，而是一组明确分层的组件

读完源码后，我对 `parallel_state` 的核心判断是：

- **`parallel_state` 不是 Megatron-LM 那种“全局全局变量式”的并行状态中心**。
- xLLM 这里真正承载“并行状态”的对象是 **`ParallelArgs`**。
- `parallel_state` 这个名字，对应的其实是一个 **namespace**，里面放的是：
  1. 并行通信辅助函数；
  2. 进程组创建函数；
  3. 一个把 variable-length gather 拆成 launch/finish 的异步接口。

更准确地说，xLLM 的并行抽象被拆成了下面四层：

| 层次 | 代表对象 | 作用 |
| --- | --- | --- |
| 通信后端层 | `ProcessGroup` / `ProcessGroupImpl` | 包装 NCCL/HCCL 等后端通信能力 |
| 拓扑描述层 | `ParallelArgs` | 保存 rank/world_size/dp_size/ep_size 和各类 group 指针 |
| 拥有与构造层 | `CollectiveCommunicator` | 真正创建并拥有 process groups，再把裸指针塞进 `ParallelArgs` |
| 算子辅助层 | `xllm::parallel_state::*` | 提供 `gather/reduce/reduce_scatter/scatter` 等张量级 helper |

### 2.2 它的职责不是“做调度”，而是给模型执行提供分布式拓扑

结合 `0-overview.md` 和 `1-structure.md`，`parallel_state` 更准确的位置是：

```text
DistManager / WorkerServer
  -> CollectiveCommunicator
  -> ParallelArgs
  -> ModelContext
  -> Layer / Model / WorkerImpl / Executor 中的并行算子
```

也就是说：

- scheduler 决定“谁进 batch”；
- engine 决定“怎样 fan-out 到 worker”；
- `parallel_state` 负责的是“每个 worker 知道自己在哪个并行拓扑里，并且能做对应通信”。

### 2.3 这个模块当前的重点是 TP / DP-local / EP / SP，而不是 PP

`ParallelArgs` 里实际保存的 group 只有：

- `process_group_`：全局 world group
- `tp_group_`
- `single_rank_group_`
- `sp_group_`
- `dp_local_process_group_`
- `moe_tp_group_`
- `moe_ep_group_`

这里没有 `pp_group_`。而且在 NPU `MappingNPU::Options` 初始化里，`pp_size(1)`、`sp_size(1)` 都是写死/固定配置。这说明当前 `parallel_state` 的主要关注点是：

- 常规张量并行 TP；
- DP 下同一 TP slot 之间的局部 gather；
- MoE 的 TP/EP 两层 group；
- DeepSeek v32 prefill 场景的 sequence parallel（当前先借 TP group）。

所以如果问“parallel_state 是否覆盖 xLLM 全部分布式并行维度”，答案是：**不是。它覆盖的是当前模型执行主链里最常用的几类并行通信拓扑。**

---

## 3. 四个核心对象的职责边界

### 3.1 `ProcessGroup`：通信能力的统一门面

`ProcessGroup` 是最底层的通信抽象。它统一暴露：

- `allreduce`
- `allgather`
- `allgather_async`
- `reduce_scatter`

它本身不懂模型，也不懂 batch，只负责把张量 collectives 落到具体后端。

不同后端通过 `ProcessGroupImpl` 实现：

- CUDA 路径走 `c10d::ProcessGroupNCCL`
- NPU 路径有两种：
  - 一种走 `torch_npu` 的 `ProcessGroupHCCL`
  - 一种直接拿 `HcclComm` 做更轻的 HCCL 调用

因此 `ProcessGroup` 是一个很纯的“通信壳”。

### 3.2 `ParallelArgs`：并行拓扑描述对象

`ParallelArgs` 是整个模块最关键的数据结构。

它里面有两类信息：

1. **标量拓扑信息**
   - `rank`
   - `world_size`
   - `dp_size`
   - `ep_size`

2. **非 owning 的 group 指针**
   - `process_group_`
   - `dp_local_process_group_`
   - `tp_group_`
   - `single_rank_group_`
   - `sp_group_`
   - `moe_ep_group_`
   - `moe_tp_group_`

这里很关键的一点是，源码在 `parallel_args.h` 里明确写了：这些指针 **不是 `ParallelArgs` 自己拥有的**，真正 owner 是外部创建者（典型就是 `CollectiveCommunicator`）。

这意味着 `ParallelArgs` 更像一个：

- 轻量拓扑描述快照；
- 传给 `WorkerImpl` / `ModelContext` / `Layer` 的上下文对象；
- 不是通信资源的 owner。

### 3.3 `CollectiveCommunicator`：真正负责“建组”的对象

`CollectiveCommunicator` 才是 worker 侧主链里真正“把 group 建出来”的对象。

它做两件事：

1. 构造 `parallel_args_`；
2. 创建并持有各种 `ProcessGroup`，再把裸指针写回 `parallel_args_`。

所以 `CollectiveCommunicator` 的定位可以理解为：

- **builder + owner**。

而 `ParallelArgs` 是：

- **view + handle bundle**。

### 3.4 `parallel_state` namespace：张量通信 helper 集合

`parallel_state.cpp` / `parallel_state_async.cpp` 里的函数，本质上都是“拿到一个 `ProcessGroup*` 后怎么做 collectives”的薄封装，例如：

- `gather`
- `launch_gather`
- `finish_gather`
- `all_gather_interleaved`
- `reduce`
- `reduce_scatter`
- `scatter`
- `create_npu_process_groups`
- `create_local_process_groups`

它们不持有状态，不维护 registry，也不负责调度生命周期。

---

## 4. 主链控制流：parallel_state 是如何被创建并传进模型的

这一段是整个理解里最重要的一段，因为它回答的是：`ParallelArgs` 到底从哪里来，什么时候变成模型执行上下文的一部分。

### 4.1 分布式 worker 主链

LLM/VLM 主链里的并行状态创建流程，核心上是这样的：

```text
DistManager
  -> 为每个设备准备一个占位 ParallelArgs(rank, world_size, dp_size, nullptr, ep_size)
  -> 启 WorkerServer

WorkerServer
  -> 向 master node 同步地址与 rank
  -> 构造 CollectiveCommunicator(global_rank, world_size, dp_size, ep_size)
  -> create_process_groups(master_addr, device)
  -> 得到真正填好 group 指针的 ParallelArgs
  -> 构造 Worker(*parallel_args, ...)

WorkerImpl
  -> 保存 parallel_args_
  -> 构造 ModelContext(parallel_args_, ...)
  -> 各 layer / model 从 ModelContext 继续读取 parallel_args
```

### 4.2 `DistManager` 先传的是“占位拓扑”，不是最终 group

`dist_manager.cpp` 里，为每个 worker server 传入的 `ParallelArgs` 是：

- `rank`
- `world_size`
- `dp_size`
- `ep_size`
- `process_group == nullptr`

这一步还没有真正建组。它只是先把 **全局拓扑参数** 带下去。

所以这里要特别注意：

- `ParallelArgs` 可以先只有“数字层面的并行描述”；
- 后续再由 `CollectiveCommunicator` 把真正的通信对象补进去。

### 4.3 真正建组发生在 `WorkerServer`

`worker_server.cpp` 里主链是：

1. worker 向 master node 同步地址和 global rank；
2. 构造 `CollectiveCommunicator comm(worker_global_rank, world_size, dp_size, ep_size)`；
3. `const ParallelArgs* parallel_args = comm.parallel_args();`
4. `comm.create_process_groups(master_node_addr, device);`
5. 用 `*parallel_args` 构造 `Worker`。

这里有一个很容易忽略、但非常关键的点：

- `ParallelArgs` 内部保存的是 **裸指针**；
- 这些指针实际指向 `comm` 持有的 `unique_ptr<ProcessGroup>`；
- 而 `comm` 是 `WorkerServer` 启动函数里的局部对象；
- 之所以这不会立刻悬空，是因为后面 `worker_server->run()` 会阻塞住，使 `comm` 在整个 server 生命周期内保持存活。

所以 xLLM 这里的设计是：

- **生命周期靠控制流保证，不靠 shared_ptr 托管。**

### 4.4 `WorkerImpl` 再把并行状态放进 `ModelContext`

`WorkerImpl` 构造函数会把 `ParallelArgs` 保存到成员 `parallel_args_`。后续加载模型时，再构造：

```text
ModelContext(parallel_args_, model_args, quant_args, tensor_options, use_mla)
```

从这一刻开始：

- `parallel_args_` 就成为模型层、层实现、并行线性层、MoE 层、SP 上下文等模块的统一入口。

也就是说，**parallel_state 的最终落点不是 WorkerServer，而是 ModelContext。**

---

## 5. `ParallelArgs` 里每个字段的真实语义

下面这一节我建议作为以后继续读模型代码时的“查表”。

| 字段 | 语义 | 典型消费方 |
| --- | --- | --- |
| `rank` | 当前 worker 的全局 rank | `WorkerImpl` driver 判定、建组时的局部 rank 推导 |
| `world_size` | 当前并行世界总 worker 数 | TP/DP/EP 分组推导、权重加载线程控制 |
| `dp_size` | data parallel 复制数 | `WorkerImpl` 的 `dp_driver_`、DP gather 路径 |
| `ep_size` | expert parallel 规模 | MoE group 构造与 FusedMoE 行为 |
| `process_group_` | 全局 world group | 全局 allgather / gather_global_tokens |
| `tp_group_` | tensor parallel group | 线性层、embedding、attention 主并行维度 |
| `single_rank_group_` | 单 rank group | 某些权重不需要 TP 通信时避免无意义组通信 |
| `sp_group_` | sequence parallel group | DeepSeek v32 prefill SP |
| `dp_local_process_group_` | 同一 TP slot 上的 DP 局部 group | DP token gather，尤其是 DP+EP 路径 |
| `moe_tp_group_` | MoE 视角下的 TP group | `FusedMoEImpl` |
| `moe_ep_group_` | MoE 视角下的 EP group | `FusedMoEImpl` 输出 reduce |

### 5.1 `process_group_`：全局 world group

这是覆盖全部 worker 的组，group 大小就是 `world_size`。

它主要用于：

- 全局 allgather；
- 按 world rank 重组 token；
- 某些 DP/TP 上层逻辑里做全局 gather。

例如 `dp_utils.cpp` 里的 `gather_global_tokens()` 就是直接对 `args.process_group_` 做 `allgather`，然后按 `(dp_rank, tp_rank)` 顺序重建 token。

### 5.2 `tp_group_`：主干张量并行 group

这是最常用的一组。它服务的是：

- `WordEmbeddingImpl`
- `QKVParallelLinearImpl`
- 各类 `ColumnParallelLinear` / `RowParallelLinear`
- 常规 attention / MLP 的 TP 切分

例如：

- `word_embedding_impl.cpp` 里构造函数直接取 `tp_group_->rank()`、`tp_group_->world_size()`；
- forward 之后如果 `world_size_ > 1`，就对输出做 `parallel_state::gather(output, tp_group_)`；
- `linear.cpp` 里的 `QKVParallelLinearImpl::forward()` 在 `gather_output_` 为真时也走 `gather(..., tp_group_)`。

所以在 xLLM 里，**TP 是 `parallel_state` 最核心也最普遍的使用方式。**

### 5.3 `single_rank_group_`：当某些模块不需要 TP 通信时的“退化组”

这个字段很值得注意，因为它反映了 xLLM 对“并不是所有层都要做 TP 通信”的细粒度处理。

`CollectiveCommunicator` 的逻辑是：

- 如果 `tp_size > 1`，为每个 rank 单独创建一个 size=1 的 group；
- 如果 `tp_size == 1`，直接复用 `tp_group_`。

它的意义是：

- 某些层虽然整体模型在 TP 环境下运行；
- 但该层某些权重希望“按单 rank 复制”，不发生 TP 通信；
- 于是给它一个单 rank group，更明确地表达“这里不要跨 rank 并行”。

`mlu/deepseek_v2_attention.cpp` 就有一个很典型的例子：

- 如果开启了 replicated attention weights，且 `single_rank_group_ != nullptr`；
- 则 attention 线性层使用 `single_rank_group_` 作为 weight group；
- 否则才退回 `tp_group_`。

这说明 `single_rank_group_` 不是摆设，它对应的是 **“在 TP 世界里显式跳过 TP 通信”** 的需求。

### 5.4 `sp_group_`：当前只是 TP group 的别名，但语义上已经独立

源码注释写得很明确：

- 目前 SP 在 prefill attention 里与 TP 用同一组 rank；
- 但仍然单独保留 `sp_group_` 这个 handle；
- 这样 future policy 可以在不改 call site 的前提下演进。

`CollectiveCommunicator::create_process_groups()` 当前的实现是：

```text
parallel_args_->sp_group_ = tp_group_.get();
```

也就是说：

- **现在的 SP 并没有独立建组；**
- 只是把“sequence parallel 语义”与“tensor parallel 语义”在 API 上先拆开。

这个设计非常重要，因为它说明 xLLM 已经在接口层为更独立的 SP 拓扑预留了位置。

### 5.5 `dp_local_process_group_`：同一 TP slot 的 DP 局部组

这是 DP 相关逻辑里最容易读错的一组。

它不是 world group，也不是“所有 DP rank 在一起”的一整个大组，而是：

- 对每个 TP 槽位单独建一个 DP group；
- 也就是同一个 `tp_rank`，跨不同 DP replica 的 rank 聚在一起。

这组在下面这些场景里特别关键：

- DP+EP MoE 的 token gather；
- DP token 数不均时的 variable-length gather；
- 某些 attention/decoder 层里按 DP rank 切回本地 slice。

例如：

- `dp_utils.cpp` 的 `gather_dp_tokens()` 直接调用
  `parallel_state::gather(input, args.dp_local_process_group_, params.dp_global_token_nums)`；
- `FusedMoEImpl::forward()` 在 `dp_size > 1 && ep_size > 1` 时，也先对 `dp_local_process_group_` 做 gather，再进行专家路由。

### 5.6 `moe_tp_group_` 和 `moe_ep_group_`：MoE 专用拓扑

MoE 场景下，xLLM 不完全复用普通 TP group，而是专门再建两组：

- `moe_tp_group_`
- `moe_ep_group_`

在 `FusedMoEImpl` 构造里：

- 默认 `tp_pg_ = parallel_args.tp_group_`；
- 但如果 `ep_size > 1`，则：
  - `ep_rank = parallel_args.moe_ep_group_->rank()`；
  - `tp_pg_ = parallel_args.moe_tp_group_`。

这说明在 MoE 视角里：

- 路由和专家分配要看 `moe_ep_group_`；
- 专家内的 TP 计算则看 `moe_tp_group_`；
- 它不一定等价于普通 attention/MLP 用的 `tp_group_`。

所以 `parallel_state` 不是只支持“一个 TP group 打天下”，而是已经允许不同子系统有自己的并行拓扑视图。

---

## 6. 建组算法：xLLM 是怎样把 world rank 切成这些 group 的

这里是 `parallel_state` 真正最值得慢慢啃的地方。

### 6.1 `get_group_rank()` 是所有分组的核心原语

`process_group.cpp` 里真正决定“一个 rank 属于哪个 group”的函数是：

- `get_group_rank(world_size, global_rank, split_size, trans)`

它有两种模式。

#### 模式 A：`trans == false`

逻辑是连续切块：

- group 大小 = `split_size`
- 当前 rank 属于第 `global_rank / split_size` 个组
- 组内成员是连续 rank 区间

例如：

- `world_size = 8`
- `split_size = 4`

则分组是：

- `[0,1,2,3]`
- `[4,5,6,7]`

这正好适合建：

- `tp_group_`
- `moe_tp_group_`

#### 模式 B：`trans == true`

逻辑是跨步抽取：

- `trans_group_size = world_size / split_size`
- 同组 rank 的步长是 `trans_group_size`

例如：

- `world_size = 8`
- `split_size = 2`

则分组是：

- `[0,4]`
- `[1,5]`
- `[2,6]`
- `[3,7]`

这正好适合建：

- `dp_local_process_group_`
- `moe_ep_group_`

也就是说：

- `false` 表示“连续切块”；
- `true` 表示“按列取 rank”。

### 6.2 `CollectiveCommunicator` 里的组创建顺序

`create_process_groups()` 的建组顺序是：

1. `process_group_`：world group
2. `tp_group_`
3. `single_rank_group_`
4. `sp_group_`（当前直接别名到 `tp_group_`）
5. `dp_local_process_group_`
6. `moe_tp_group_`
7. `moe_ep_group_`

这很能说明 xLLM 的优先级：

- 先建模型主干一定会用到的 world/TP；
- 再建 SP/single-rank 这种细粒度变体；
- 最后建 MoE 特化拓扑。

### 6.3 `tp_group_` 的计算规则

代码里：

```text
tp_size = world_size / dp_size
```

然后按 `trans=false` 连续分组。

因此如果：

- `world_size = 8`
- `dp_size = 2`

那么：

- `tp_size = 4`
- TP groups 是：
  - `[0,1,2,3]`
  - `[4,5,6,7]`

这意味着 xLLM 默认假设 rank 排布是：

- 先按一个 DP replica 内部连续放满 TP ranks；
- 再放下一个 DP replica。

### 6.4 `dp_local_process_group_` 的计算规则

代码里只有当 `dp_size > 1` 时才创建它。

其参数是：

- `split_size = dp_size`
- `trans = true`

并且端口偏移用的是：

```text
port_offset = global_rank % tp_size + 1
```

这说明：

- 同一个 `tp_rank` 的不同 DP replica 会落在同一组；
- 因为这些 rank 的 `global_rank % tp_size` 相同。

延续上面的例子：

- `world_size = 8`
- `dp_size = 2`
- `tp_size = 4`

则 DP-local groups 是：

- `[0,4]`
- `[1,5]`
- `[2,6]`
- `[3,7]`

这正是“同一个 TP 位置跨 DP 副本组成一组”。

### 6.5 `single_rank_group_` 的计算规则

当 `tp_size > 1` 时：

- 每个 rank 单独建一个 size=1 group；
- 端口偏移是 `port + dp_size + global_rank + 1`。

因此每个 rank 都有自己独立的 store/组实例。

当 `tp_size == 1` 时：

- 没必要再造一层单 rank group；
- 直接复用 `tp_group_`。

### 6.6 `sp_group_` 目前没有独立建组

当前代码只是：

- `parallel_args_->sp_group_ = tp_group_.get();`

对应的 owner 字段 `sp_group_` 在 `CollectiveCommunicator` 里也只是预留，没有被真正创建。

所以目前结论非常明确：

- **SP 语义独立；**
- **SP 通信实体暂时复用 TP。**

### 6.7 `moe_tp_group_` 和 `moe_ep_group_` 的计算规则

当 `ep_size > 1` 时：

- `moe_tp_size = world_size / ep_size`

然后：

- `moe_tp_group_` 用 `trans=false` 连续切块；
- `moe_ep_group_` 用 `trans=true` 跨步切分。

所以它的结构和普通 `tp_group_ + dp_local_process_group_` 很像，只是把“第二维”从 DP 换成了 EP。

### 6.8 端口偏移设计说明了“每个逻辑 group 都有自己单独的 store”

`CollectiveCommunicator::create_process_groups()` 里有很多 `port + offset`。这不是随便写的，而是在做一件很具体的事：

- 每个逻辑 group 实例都要通过 TCP store rendezvous；
- 同一个 group 内的 rank 必须用同一个 port；
- 不同 group 必须用不同 port，避免串线。

所以端口偏移的写法实际上编码了“哪些 rank 应该连到同一个 group store”。

换句话说：

- group membership 不只是 `get_group_rank()` 决定；
- store 端口设计也在显式保证“该组的参与者碰到的是同一个 rendezvous 点”。

---

## 7. 张量级 collective helper 的具体语义

`parallel_state.cpp` 的几个 helper 看起来不复杂，但每个函数都有很明确的使用边界。

### 7.1 `gather(input, process_group, dim)`：等长 allgather + cat

这是最基础的 gather。

行为是：

1. 如果 `process_group == nullptr`，直接返回输入；
2. 如果 `world_size == 1`，直接返回输入；
3. 为每个 rank 准备一个 `empty_like(input)`；
4. 调 `process_group->allgather(input, tensors)`；
5. 沿着 `dim` 做 `torch::cat(...)`。

默认 `dim = -1`，所以很多 TP 场景都是沿最后一维把分片拼回来。

典型用法：

- `WordEmbeddingImpl::forward()`
- `QKVParallelLinearImpl::forward()`

### 7.2 `gather(input, process_group, token_num_list)`：不等长 token gather

这是 `parallel_state` 里最值得认真看的函数之一，因为它专门解决“不同 rank token 数不一样”的问题。

逻辑分两段：

#### 情况 A：各 rank token 数完全一样

如果 `token_num_list` 所有值相等：

- 直接退化成普通 `gather(input, process_group, 0)`。

#### 情况 B：token 数不一样

就走：

- `launch_gather(...)`
- `finish_gather(...)`

这条路径的核心思路是：

1. 先把每个 rank 的输入 pad 到本轮最大 token 数；
2. 用固定 shape 做一次 allgather；
3. gather 完后再按 `token_num_list` 把 padding 去掉，重新压紧。

这说明 xLLM 很清楚：

- 通信后端通常更擅长 fixed-shape collective；
- variable-length gather 需要框架侧先做 padding/trim。

### 7.3 `launch_gather` / `finish_gather`：为 overlap 留接口

`parallel_state_async.cpp` 只实现了一个函数：`launch_gather()`，然后在 `parallel_state.cpp` 里实现 `finish_gather()`。

`launch_gather()` 做的事是：

1. 校验 `token_num_list.size() == world_size`；
2. 校验本 rank 本地 tensor 的行数等于 `token_num_list[rank]`；
3. 计算最大 token 数 `max_num_tokens`；
4. 把本 rank 输入 pad 到 `max_num_tokens`；
5. 为每个 rank 预分配一份 `empty_like(padded_input)`；
6. 发起 `allgather_async()`，把 work handle、shards、token_num_list 打包进 `GatherAsyncCtx`。

`finish_gather()` 则：

1. `wait()` 异步 work；
2. 如果只是单 rank 并且 shape 本来就匹配，直接返回；
3. 否则调用 `assemble_gathered()`，按 `token_num_list` 去掉 padding，并把各 rank 的有效行压到一起。

这套接口的意义非常明确：

- 不是为了让 gather 本身更复杂；
- 而是为了给上层留下“发起通信后去做别的事，最后再收尾”的空间。

`deepseek_v32_sp_context.h` 里就已经把它真正用起来了：

- `launch_gather_padded()` 调 `parallel_state::launch_gather(...)`
- `finish_gather_padded()` 再调 `parallel_state::finish_gather(...)`

这说明这个异步接口不是摆设，而是已经进入 SP 主链。

### 7.4 `assemble_gathered()`：variable-length gather 的最后一公里

这个函数虽然是匿名 namespace 内部函数，但非常关键。

它做的事情是：

- 根据 `token_num_list` 计算总 token 数；
- 创建最终输出张量；
- 从每个 gathered shard 的前 `valid_tokens` 行拷贝到输出；
- 跳过 padding 部分。

所以整个 variable-length gather 的核心并不是 “allgather” 本身，而是：

- **padding + allgather + valid rows 重装配**。

### 7.5 `all_gather_interleaved()`：一个更偏特化的 gather

这个函数不是普通 gather 的替代品，而是一个 **QKV 特化重排 helper**。

它的做法是：

1. 先普通 allgather；
2. 假定最后一维按 3 段均匀切分；
3. 以“chunk 维优先、rank 维次之”的顺序重新排列；
4. 最后沿最后一维 concat。

这相当于把：

```text
[rank0: Q0 K0 V0], [rank1: Q1 K1 V1], ...
```

重排成：

```text
[Q0 Q1 ...][K0 K1 ...][V0 V1 ...]
```

它的直接调用点是：

- `xllm/core/layers/common/qwen2_vision_attention.cpp`

后者先 `all_gather_interleaved(qkv, tp_group_)`，再 `chunk(3)` 拿出 Q/K/V，再必要时 `scatter` 回每个 rank。

所以我的结论是：

- `all_gather_interleaved()` 是一个 **存在明确业务场景的专用 helper**；
- 不是 `parallel_state` 的主干接口。

### 7.6 `reduce(input, process_group)`：allreduce sum

这个函数很薄：

1. group 为空或 world_size=1 时直接返回；
2. 否则 `process_group->allreduce(input)`；
3. 返回原 tensor。

它是 in-place 语义的轻封装。

典型用法：

- `FusedMoEImpl` 对 TP 结果做 reduce；
- EP 结果再对 `moe_ep_group_` 做一次 reduce。

### 7.7 `reduce_scatter(input, process_group)`：沿 dim0 pad 后再 reduce-scatter

这个函数也非常值得注意。它当前只支持：

- `scatter_dim == 0`

逻辑是：

1. 看 `input.size(0)` 是否能整除 `world_size`；
2. 不能整除就 pad 到最近的整倍数；
3. 创建每 rank 对应的 output chunk；
4. 调 `process_group->reduce_scatter(padded_input, output)`；
5. 如果之前 pad 过，则按本 rank 的全局切片范围把多余部分裁掉。

这意味着它解决了两个问题：

- reduce-scatter 本身要求分片均匀；
- 而上层真实 token 数往往不均匀。

`parallel_state/tests/parallel_state_tests.cpp` 专门测了这个 padding 场景：

- 如果输入 token 不能整除 world_size；
- 后面的 rank 可能拿到部分有效数据，甚至空张量；
- 单测里也显式校验了“padding case 下某些 rank 输出为空”的行为。

### 7.8 `scatter(input, process_group, dim)`：本地切分，不做通信

这点非常容易误读。

`scatter()` 的实现根本没有 collective 调用，它只是：

- 检查 `input.size(dim)` 可被 `world_size` 整除；
- `input.split(...)`；
- 返回当前 rank 对应切片。

所以它不是分布式通信意义上的 scatter，而是：

- **已经持有完整 tensor 后，按 rank 在本地切一块出来。**

典型用法就是：

- `Qwen2VisionAttentionImpl` 先把 QKV gather 成完整张量；
- 再用 `scatter()` 在本地按 rank 取回当前 rank 负责的那一片。

这个函数的语义一定要记清，不然后面看 attention 很容易误以为这里在发网络通信。

---

## 8. 上层是怎么真正消费这些 group 的

### 8.1 `WorkerImpl` 首先用它区分 driver / dp_driver

`WorkerImpl` 构造函数里，直接基于 `ParallelArgs` 做了两件基础判定：

- `driver_ = parallel_args.rank() == 0`
- `dp_driver_ = parallel_args.dp_size() > 1 && parallel_args.rank() % tp_size == 0`

这说明：

- rank 0 是全局 driver；
- 每个 DP replica 里 `tp_rank == 0` 的 worker 还是一个局部 DP driver。

换句话说，`ParallelArgs` 不只给 layer 用，它在 worker 控制逻辑里也直接决定角色分工。

### 8.2 权重加载阶段也会根据 TP world size 调整行为

`WorkerImpl` 在模型初始化后会读取：

- `tp_world_size = parallel_args_.tp_group_ ? parallel_args_.tp_group_->world_size() : parallel_args_.world_size()`

如果 `tp_world_size > 1`：

- 暂时把 ATen 线程数设为 1，再进行权重加载。

这背后的隐含逻辑是：

- TP 多卡加载权重时，线程过多会带来额外竞争；
- 所以并行拓扑信息会反过来影响 host 侧加载策略。

### 8.3 `ModelContext` 是并行状态进入模型层的正式入口

`ModelContext` 构造时直接复制 `ParallelArgs`。后续模型和层实现基本都通过：

- `context.get_parallel_args()`

来读 rank/world/group。

因此 `ModelContext` 是：

- 模型参数；
- 量化参数；
- 张量 dtype/device；
- 并行拓扑；

这几类上下文的统一交汇点。

### 8.4 TP 主链：embedding / linear / attention

这一层是 `parallel_state` 使用最广泛的地方。

#### Embedding

`WordEmbeddingImpl`：

- 构造时按 `tp_group_->world_size()` 切 embedding hidden dim；
- forward 后用 `gather(..., tp_group_)` 把分片拼回来。

#### Linear

`QKVParallelLinearImpl`：

- 构造时按 TP size 切输出特征；
- forward 后如果 `gather_output_ == true`，则对 TP group gather。

所以 TP 在这里既决定：

- 权重 shape；
- 又决定 forward 后输出如何重组。

### 8.5 DP + EP 主链：token gather 与 MoE

这块最能体现 `parallel_state` 不只是“普通 TP gather/reduce”。

#### 上游 token 数从哪里来

`LLMEngine::prepare_inputs()` 会为每个 DP micro-batch 计算：

- `dp_global_token_nums[dp_rank] = flatten_tokens_vec.size()`

然后把这整个向量回填到每个 `RawForwardInput` 里。

这意味着：

- 每个 worker 在执行时都能知道“所有 DP rank 这一轮各有多少 token”。

#### `dp_utils.cpp`

`dp_utils` 在上层提供了几类 DP 辅助：

- `get_reduce_scatter_tokens()`
- `reduce_scatter_attn_input()`
- `gather_global_tokens()`
- `gather_dp_tokens()`
- `get_dp_local_slice()`

其中：

- `gather_dp_tokens()` 实质上就是对 `dp_local_process_group_` 做 variable-length gather；
- `get_dp_local_slice()` 再按 `dp_rank` 把结果切回本地那段。

#### `FusedMoEImpl`

`FusedMoEImpl::forward()` 的逻辑非常典型：

1. 如果 `dp_size > 1 && ep_size > 1`：
   - 先按 `dp_local_process_group_` gather；
   - 这样本 TP 槽位下跨 DP 的 token 就被聚齐了。
2. 做 MoE gate / route / expert forward。
3. 如果启用了 EP：
   - 先对 `tp_pg_` reduce；
   - 再对 `moe_ep_group_` reduce。
4. 如果一开始做过 DP gather：
   - 末尾按 `dp_rank` 再 slice 回本地 token 区间。

这条链路很能说明 `parallel_state` 的价值：

- 它不只是“通信函数集合”；
- 它实际上编码了 **不同并行维度下 token 应该怎样聚、怎样还原** 的执行拓扑。

### 8.6 SP 主链：DeepSeek v32 prefill sequence parallel

这是当前 `sp_group_` 最明确的真实消费点。

`DeepseekV32ModelImpl` 中：

- 构造时保存 `sequence_parallel_group_ = context.get_parallel_args().sp_group_`；
- 只有在 `FLAGS_enable_prefill_sp` 且本轮是纯 prefill/no_decode 时，才尝试进入 SP 路径；
- 如果 `sp_group_ == nullptr` 或 `world_size <= 1`，就回退到普通 TP 路径；
- 如果 SP 可用，就构建 `DeepseekV32SPContext`。

之后：

- `reorder_to_local_shard()` 把全局顺序重排成每个 SP rank 的本地 shard；
- 层执行使用本地 shard；
- 结束后通过 `gather_and_restore_global()` 把结果恢复回全局顺序。

而 `deepseek_v32_sp_context.h` 里的 `launch_gather_padded()` / `finish_gather_padded()` 又进一步用到了：

- `parallel_state::launch_gather()`
- `parallel_state::finish_gather()`

这说明：

- `parallel_state` 提供的 variable-length async gather，已经被真正接入 SP 场景；
- `sp_group_` 虽然当前别名 TP，但在语义上已经是“prefill SP”这条链路的独立入口。

### 8.7 `single_rank_group_` 真正服务的是“部分权重复制”

`mlu/deepseek_v2_attention.cpp` 里：

- 当开启 replicated attention weights 时；
- `weight_group` 会选 `single_rank_group_`，而不是 `tp_group_`。

这说明 `single_rank_group_` 的存在不是为了凑字段，而是为了表达：

- 某些模块虽然生活在 TP 环境里；
- 但其某些参数/算子不应该做跨 rank TP 通信。

这个细节很能反映 xLLM 的并行设计不是“一把梭”，而是允许层级内局部例外。

---

## 9. NPU 特殊路径：TORCH backend、ATB backend 和本地 HCCL fast path

NPU 是 `parallel_state` 里最特别的一块，因为这里不仅有 group，还掺杂了 ATB 的 mapping 语义。

### 9.1 `CollectiveCommunicator` 在 NPU 下先分成 TORCH 与 ATB 两条路

构造函数里的逻辑是：

#### 分支 A：`FLAGS_npu_kernel_backend == "TORCH"`

- 直接创建一个普通 `ParallelArgs(global_rank, world_size, dp_size, nullptr, ep_size)`；
- 不在构造阶段灌入 mapping；
- 后续 `create_process_groups()` 再正常走 torch/hccl process group 建组。

#### 分支 B：`FLAGS_npu_kernel_backend == "ATB"`

- 不走 torch distributed process group 主链；
- 直接构造 `MappingNPU::Options`；
- 填入 `dp_size / tp_size / moe_tp_size / moe_ep_size / pp_size(1) / sp_size(1)`；
- 从 `MappingNPU` 生成 `mapping_data`；
- 再创建 `atb_speed::base::Mapping`、初始化 global comm domain；
- 把 `mapping_data`、`mapping`、`dispatchAndCombinecommDomain`、`dispatchAndCombineHcclComm` 都塞进 `ParallelArgs`。

而 `create_process_groups()` 里一上来就判断：

- 如果是 ATB backend，直接 `return`。

所以 ATB 路径的结论非常清楚：

- **它不依赖这里创建 world/tp/dp/moe process groups；**
- **它依赖的是提前准备好的 mapping 和外部 comm domain。**

### 9.2 这意味着 `ParallelArgs` 在 NPU/ATB 下不只是“group 指针容器”

在普通路径下，`ParallelArgs` 主要保存：

- 标量拓扑；
- group 指针。

而在 NPU/ATB 下，`ParallelArgs` 还额外保存：

- `mapping_data`
- `mapping`
- `dispatchAndCombinecommDomain`
- `dispatchAndCombineHcclComm`

这说明 `ParallelArgs` 在 NPU 环境下已经不只是通信 group handle bundle，而是进一步承担了：

- **把高层并行拓扑翻译为底层 NPU/ATB 通信映射载体** 的职责。

### 9.3 `create_npu_process_groups()`：本地 NPU 多卡直连 helper

`parallel_state.cpp` 里还有一个专门的 helper：

- `create_npu_process_groups(devices)`

它的逻辑是：

1. 收集所有 device index；
2. 调 `HcclCommInitAll(world_size, device_idxs.data(), comms.data())`；
3. 为每个设备创建一个 `ProcessGroupImpl(rank, world_size, device, comm)`。

这个函数的调用点主要不是 LLM 的 `WorkerServer + CollectiveCommunicator` 主链，而是：

- `DiTEngine`
- `RecEngine` 的某些本地路径

所以我对它的定位是：

- 更像“本地单进程多设备引擎”的快速建组 helper；
- 而不是 LLM/VLM 主路径里那套更完整的多 group 拓扑构建器。

### 9.4 `create_local_process_groups()`：本地单机多卡 helper

这个 helper 的设计目标是：

- 给本地场景创建 process groups；
- 让 GPU/MLU/ILU 之类设备直接用 localhost 通信。

它的特点是：

- 从 `options.master_node_addr()` 里解析端口；
- 但 host 强制改成 `127.0.0.1`；
- 为每个本地设备创建一个覆盖全部设备的 group；
- group 名写成 `local_tp_group`。

当前源码内实际调用它的主要是：

- `RecEngine` 非 NPU 分支。

所以这条路径更像“本地特例支持”，不是分布式 worker 主链的建组方式。

---

## 10. `parallel_state` 设计里最值得记住的几个关键点

### 10.1 xLLM 的并行状态是“显式传递”的，不是隐式全局查询的

最核心的设计感受是：

- `ParallelArgs` 被显式创建；
- 显式放进 `Worker`；
- 显式放进 `ModelContext`；
- layer 再显式从 `context.get_parallel_args()` 取出来。

所以它是：

- **context-driven** 的并行状态传递；
- 不是某个全局 singleton API 到处 `get_tp_group()` 那种隐式依赖。

这让模型代码更可组合，也更利于以后为某个子模块替换局部 group。

### 10.2 xLLM 把“建组”和“做通信”明确分开了

这点体现在三个层次：

- `CollectiveCommunicator` 负责建组与拥有资源；
- `ParallelArgs` 只是描述和转交；
- `parallel_state::*` 只是拿 group 做张量通信。

这种拆法非常好，因为：

- 生命周期边界清楚；
- 上层模型代码只需要关心“我拿到了什么 group”；
- 不需要自己参与通信资源初始化。

### 10.3 xLLM 的 group 设计不是只有 TP，而是已经显式建模多维并行

从字段和调用点可以看出，xLLM 至少已经显式区分了：

- world
- TP
- SP（当前借 TP）
- DP-local
- MoE-TP
- MoE-EP
- single-rank fallback

这意味着框架已经准备好了：

- 同一个模型不同子路径看到不同拓扑；
- 甚至同一时刻 attention、MoE、SP 可以各用自己语义上的 group。

### 10.4 `parallel_state` 的很多复杂度，本质来自“token 数不均”

如果所有 rank 张量 shape 完全一致，那这个模块其实不复杂。

真正让它变复杂的是：

- DP 各 rank token 数不同；
- prefill SP 各 rank token 数不同；
- reduce_scatter 要求均匀切分，但真实 token 不均；
- MoE 前后还要按 rank/slot 重新聚和切。

所以这里的大量 helper，本质上都在解决一个问题：

- **通信原语通常偏 fixed-shape；模型运行却经常是 variable-length。**

### 10.5 `parallel_state` 的命名虽然像“状态”，但它更像“拓扑与 collective 工具箱”

如果只看名字，很容易误会这里有一个全局并行状态机。

实际上更准确的理解应当是：

- `ParallelArgs` 是 topology context；
- `CollectiveCommunicator` 是 topology builder/owner；
- `parallel_state::*` 是 collective helper toolbox。

这个理解方式会让后面继续读 `ModelContext`、`Layer`、`MoE`、`SP` 代码时清晰很多。

---

## 11. 我当前对主链与支线的区分

为了以后继续深入，我把和 `parallel_state` 有关的代码分成三类：

### 11.1 主链：必须先吃透

这些是我认为真正属于 xLLM 主干执行路径的：

- `ParallelArgs`
- `CollectiveCommunicator`
- `create_process_group`
- `gather/reduce/reduce_scatter/scatter`
- `WorkerServer -> WorkerImpl -> ModelContext` 注入链
- TP/DP-local/MoE/SP 的核心调用点

### 11.2 重要支线：已经进入真实模型，但不是所有模型都会走

- `all_gather_interleaved()`
- `single_rank_group_`
- `launch_gather/finish_gather` 的异步 gather 路径
- `DeepseekV32` 的 SP 路径

### 11.3 本地特例 / 平台特化：需要放在具体场景下读

- `create_npu_process_groups()`
- `create_local_process_groups()`
- NPU `ATB` mapping 路径

这些分支并不是不重要，而是如果不先把主链吃透，很容易在平台细节里迷路。

---

## 12. 证据边界与当前仍需谨慎的点

### 12.1 `get_dp_attn_parallel_args()` 当前仓内没有直接调用点

这个函数的语义大致是：

- 如果 `dp_size <= 1`，返回空；
- 如果 `dp_size == world_size`，返回一个“本地 world_size=1”的 attention 视图；
- 否则返回基于 `dp_local_process_group_` 的 `ParallelArgs`。

但我在仓内没有找到它的直接调用点，所以目前只能把它理解为：

- 一个为 attention/DP 特化预备的 helper；
- 但还不是当前仓内主路径的关键函数。

### 12.2 `sp_group_` 目前只是别名，不应过度解读为“独立 SP 通信系统已完成”

虽然 `DeepseekV32` 已经通过 `sp_group_` 进入 SP 路径，但目前：

- `sp_group_` 仍然直接别名 `tp_group_`；
- 没有独立建组逻辑。

所以更稳妥的说法是：

- **SP 的调用接口已经独立；**
- **SP 的底层通信拓扑暂时还没有与 TP 完全分家。**

### 12.3 `create_local_process_groups()` 不是分布式 LLM 主链

虽然它在 `parallel_state` 目录里，但当前仓内主要调用它的是本地 REC 场景。真正 LLM/VLM worker 主链仍然是：

- `CollectiveCommunicator + create_process_group`。

### 12.4 `all_gather_interleaved()` 是真实代码路径，但不是通用主干 primitive

它是一个有明确业务调用点的 helper，但只适用于类似 QKV 三段交织布局的情况。不要把它误看成“xLLM 的标准 gather 实现”。

---

## 13. 最终总结

如果让我用一句话总结当前对 `parallel_state` 的理解，我会这么说：

> xLLM 的 `parallel_state` 本质上不是一个全局状态中心，而是一套把“并行拓扑”显式建模为 `ParallelArgs`、由 `CollectiveCommunicator` 负责建组和持有资源、再通过 `ModelContext` 传给模型/层实现的分布式执行工具链；它真正解决的问题，是在 TP / DP-local / EP / SP 多种拓扑下，把不均匀 token 与多维并行通信可靠地接进模型执行主链。

再展开一点，就是四条最关键的结论：

1. **并行状态是显式上下文，不是隐式全局变量。**
2. **组创建与张量通信被刻意拆层。**
3. **xLLM 已经把 TP、DP-local、MoE-TP/EP、SP 这些维度显式区分。**
4. **模块的复杂度主要来自 variable-length token 在不同并行维度间的聚合与还原。**

这四点是我认为后续继续读下面这些主题时必须带着走的：

- `ModelContext`
- `WorkerImpl`
- 各类 parallel linear / attention
- DeepSeek v32 SP
- MoE / EPLB / dynamic EP

因为这些模块里很多“看起来只是一次 gather/reduce”的代码，背后其实都依赖 `parallel_state` 已经把 group 语义和 token 布局语义定义清楚了。
