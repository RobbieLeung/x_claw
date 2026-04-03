# xLLM EPLB 学习笔记

## 1. 这份笔记要回答什么

基于 `ralph/learn_xllm/0-overview.md` 和 `ralph/learn_xllm/1-structure.md` 已经建立的整体框架，我这次把 `EPLB`（Expert Parallel Load Balancing）当成一条独立主线认真拆开，重点回答下面这些问题：

1. `EPLB` 在 xLLM 整体架构里到底挂在哪一层。
2. 它到底在“平衡什么”，是请求级调度、batch 重排，还是专家权重分布。
3. `manager / policy / executor` 三个名字在源码里分别干了什么。
4. 负载是从哪里采集的，为什么 worker 需要把所有设备的结果都回传给 engine。
5. “准备新专家权重”和“切换到新专家权重”为什么被拆成两个阶段。
6. 动态专家表是怎么生成的，冗余专家是怎么放进去的。
7. EPLB 为什么是逐层更新，而不是一次性把所有 MoE 层都切换掉。
8. 它和 scheduler、engine、worker、模型层、NPU fused MoE 之间的边界到底是什么。

---

## 2. 结论先行

先给出我目前最核心的结论：

1. xLLM 的 EPLB 不是 scheduler 侧的“请求重排负载均衡”，而是 **执行面上的 MoE 专家分布动态重排机制**。
2. 它当前真正改动的主链节点主要是：

   `LLMEngine -> WorkerImpl / LLMWorkerImpl -> EplbExecutor -> CausalLM -> NPU DeepSeek/GLM MoE Decoder Layer -> ATB SparseMoe`

   而不是 `Scheduler -> Request -> Batch` 这条控制面主链。
3. EPLB 的核心不是“迁移请求”，而是：

   - 采集每个 worker 上每个本地专家槽位的 token 负载；
   - 汇总成“每层、每个原始 expert”的全局热度；
   - 重新生成一张新的“专家到设备槽位”的分布表；
   - 让 worker 异步预加载新专家权重；
   - 在确认所有设备都准备好之后，再原子切换某一层的专家路由与权重。

4. 这套机制之所以必须做成“逐层更新”，不是简单为了保守，而是因为 worker 侧真正用来准备新专家权重的 `ExpertBuffer` 是一个**单实例共享缓冲**，它天然只能安全地串行准备一层。
5. EPLB 的负载采集当前是 **decode 阶段专用** 的；源码里明确把 prefill 和 decode 分开，prefill 主要给 multi-stream parallel 用，EPLB 只在 decode MoE 路径里打开专家累计负载输出。
6. 当前完整、能闭环读通的 EPLB 实现主要集中在 NPU MoE 路径，尤其是：

   - `DeepSeekV2`
   - `DeepSeekV32`
   - `GlmMoeDsa`（内部直接复用 `DeepseekV32DecoderLayer`）

   其他模型虽然也暴露了 `prepare_expert_weight/update_expert_weight` 虚接口，但我没有在同等深度上看到完整的 routing-map / cumsum-output / shared-buffer 闭环。
7. 文档里提到“未来与调度层结合，通过 batch 重组做更好的负载均衡”，这和当前代码是吻合的：**现在的 EPLB 还没有改 scheduler 的 batch 组织逻辑**，它是执行侧增强，而不是调度侧增强。
8. 从代码实现看，EPLB 最适合、也最像是为 `ep_size == 实际设备数`、`expert_parallel_degree == 2` 的动态 EP 场景设计的。文档这么要求，源码里不少 shape/worker 数推导也在强化这一点。

---

## 3. 阅读文件

这次为了把逻辑读透，我实际对过下面这些文件：

- `ralph/learn_xllm/0-overview.md`
- `ralph/learn_xllm/1-structure.md`
- `ralph/learn_xllm/task.md`
- `docs/zh/features/eplb.md`
- `docs/en/features/eplb.md`
- `docs/zh/features/moe_params.md`
- `xllm/xllm.cpp`
- `xllm/core/common/global_flags.cpp`
- `xllm/core/common/options.h`
- `xllm/core/common/options.cpp`
- `xllm/core/common/types.h`
- `xllm/core/distributed_runtime/master.cpp`
- `xllm/core/distributed_runtime/llm_engine.h`
- `xllm/core/distributed_runtime/llm_engine.cpp`
- `xllm/core/runtime/forward_params.h`
- `xllm/core/runtime/params_utils.cpp`
- `xllm/core/runtime/forward_shared_memory_manager.cpp`
- `xllm/core/runtime/worker_impl.h`
- `xllm/core/runtime/worker_impl.cpp`
- `xllm/core/runtime/llm_worker_impl.cpp`
- `xllm/core/distributed_runtime/worker_service.cpp`
- `xllm/core/framework/eplb/eplb_manager.h`
- `xllm/core/framework/eplb/eplb_manager.cpp`
- `xllm/core/framework/eplb/eplb_policy.h`
- `xllm/core/framework/eplb/eplb_policy.cpp`
- `xllm/core/framework/eplb/eplb_policy_test.cpp`
- `xllm/core/framework/eplb/eplb_executor.h`
- `xllm/core/framework/eplb/eplb_executor.cpp`
- `xllm/core/framework/eplb/expert_buffer_manager.h`
- `xllm/core/framework/eplb/expert_buffer_manager.cpp`
- `xllm/core/framework/eplb/expert_weight_buffer_shm.h`
- `xllm/core/framework/eplb/expert_weight_buffer_shm.cpp`
- `xllm/models/llm/npu/deepseek_v2.h`
- `xllm/models/llm/npu/deepseek_v32.h`
- `xllm/models/llm/npu/glm5_moe.h`
- `xllm/core/layers/npu/npu_deepseek_v2_decoder_layer_impl.h`
- `xllm/core/layers/npu/npu_deepseek_v2_decoder_layer_impl.cpp`
- `xllm/core/layers/npu/npu_deepseek_v32_decoder_layer_impl.h`
- `xllm/core/layers/npu/npu_deepseek_v32_decoder_layer_impl.cpp`
- `xllm/core/layers/npu/loader/deepseek_v2_decoder_loader.h`
- `xllm/core/layers/npu/loader/deepseek_v2_decoder_loader.cpp`
- `xllm/core/layers/npu/loader/deepseek_v32_decoder_loader.h`
- `xllm/core/layers/npu/loader/deepseek_v32_decoder_loader.cpp`
- `third_party/xllm_atb_layers/models/deepseekv2/layer/decoder_layer.cpp`
- `third_party/xllm_atb_layers/operations/fusion/moe/sparse_moe.cpp`

---

## 4. 放回整体架构里看，EPLB 在哪里

结合前两份总览笔记，我现在把 EPLB 放在下面这条横切链路上理解：

```text
CLI / gflags / Options
  -> Master
  -> LLMEngine(EplbManager)
  -> RawForwardInput(EplbInfo)
  -> WorkerImpl / LLMWorkerImpl(EplbExecutor)
  -> CausalLM::prepare_expert_weight / update_expert_weight
  -> NPU DeepSeek/GLM MoE Decoder Layer
  -> ATB SparseMoe(out_gmm_cumsum_list)
  -> ForwardOutput / RawForwardOutput(expert_load_data, prepared_layer_id)
  -> LLMEngine::process_eplb_data
  -> EplbManager / EplbPolicy
  -> 下一轮 EplbInfo
```

这条链路说明两件很关键的事：

### 4.1 EPLB 的主要落点不是 scheduler

`ContinuousScheduler`、`ChunkedPrefillScheduler` 这些模块负责的是：

- 哪些 request 进 batch；
- 当前 token budget / seq budget 怎么分配；
- prefill / decode / preemption 怎样推进。

而 EPLB 不直接改这些对象。它更像是：

- scheduler 仍然照常组 batch；
- engine / worker 在执行 batch 之前和之后，额外挂上一层“专家负载采集 + 专家分布更新”的闭环。

### 4.2 EPLB 是执行面增强

`1-structure.md` 里已经说过，图执行、xTensor、rolling load、KV transfer、EPLB 这类能力主要挂在 `WorkerImpl / Executor / ModelContext / 平台层`。EPLB 正是一个非常典型的例子：

- 它不重写服务入口；
- 不直接改 request 生命周期；
- 而是在 worker 和模型层之间插入一条“动态专家重配置”支线。

---

## 5. 配置、开关与前提条件

## 5.1 gflags 和 Options 是怎么传下来的

`xllm/xllm.cpp` 会把下面这些参数放进 `Options`：

- `enable_eplb`
- `redundant_experts_num`
- `eplb_update_interval`
- `eplb_update_threshold`
- `communication_backend`
- `rank_tablefile`
- `expert_parallel_degree`

然后 `Master` 在启动时又把这些 `Options` 字段重新写回全局 `FLAGS_*`。这说明 EPLB 在配置层虽然进入了 `Options`，但实际运行时核心逻辑仍然大量直接读 `FLAGS_enable_eplb`、`FLAGS_redundant_experts_num` 等全局 flag。

所以如果问“EPLB 配置是 runtime::Options 真正消费，还是全局 flag 真正消费”，我的结论是：

- **外部接口层面**：先进入 `Options`；
- **实际执行逻辑层面**：大量代码还是直接读全局 flag。

## 5.2 直接相关参数

当前直接相关的参数有：

- `enable_eplb`
  - 总开关。
- `redundant_experts_num`
  - 每个设备额外冗余的专家槽位数。
- `eplb_update_interval`
  - manager 触发重平衡计算的时间间隔。
- `eplb_update_threshold`
  - 当前负载和上一次负载的余弦相似度阈值；小于这个值才更新该层。
- `expert_parallel_degree`
  - EP 深度相关参数。文档要求 EPLB 场景设置为 `2`。
- `ep_size`
  - MoE 的 EP 规模。文档要求与实际设备数一致。
- `communication_backend`
  - NPU 通信后端。
- `rank_tablefile`
  - HCCL rank table。

## 5.3 文档要求和代码实现是怎样对齐的

文档明确写了：

- `enable_eplb=true`
- `expert_parallel_degree=2`
- `ep_size=实际卡数`

我认为这不是“文档随便写的建议”，而是和实现强相关：

1. NPU DeepSeek 层里 `expertParallelDegree == 2` 时会把 `param.isDynamicEp` 置为真。
2. EPLB 路径会打开 `enableEPWB` 和 `enableExpertCumSumOutput`，这明显是给动态 EP 的 MoE 执行路径准备的。
3. `LLMEngine::process_eplb_data()` 和 `WorkerImpl` 为 `expert_load_data` 计算 shape 时，直接按 `worker_clients_num_ / world_size` 来推每 worker 专家槽位数，而不是单独按 `ep_size` 做更复杂的映射。

因此我更倾向于认为：**当前 EPLB 代码的主目标场景，就是全设备参与的动态 EP 部署**。如果 `ep_size != 实际设备数`，我暂时不敢把它当成已经被完整验证过的通用场景。

这个判断属于“由代码推出来的工程意图”，不是文档显式声明，所以我把它标记为**有证据支撑的推断**。

---

## 6. xLLM 里的 EPLB 不只是三个模块，而是五层结构

官方文档把 EPLB 说成 `manager / executor / policy` 三个模块，这样说没错，但如果真要按源码理解，我觉得更完整的结构是五层：

### 6.1 `EplbManager`

负责：

- 接收 engine 收上来的 worker 负载；
- 聚合成全局 expert load；
- 按时间间隔触发 policy；
- 维护逐层准备/切换状态机；
- 生成下一轮下发给 worker 的 `EplbInfo`。

### 6.2 `EplbPolicy`

负责：

- 根据最新专家负载重新算分布表；
- 判断哪些层需要更新；
- 生成新的 `expert_distribution[layer][device][slot]`。

### 6.3 `EplbExecutor`

负责：

- 在 worker 侧执行“更新已准备好的层”；
- 异步准备“下一层的新专家权重”；
- 向上汇报某一层是否已经准备完成。

### 6.4 专家权重缓冲层

也就是：

- `ExpertBufferManager`
- `ExpertBufferShm`
- layer 里的 `ExpertBuffer::Instance()`

这层非常重要，因为 EPLB 真正切换的不是抽象概念，而是**具体的专家权重张量**。没有这层，`prepare/update` 只是空口号。

### 6.5 模型层 / fused MoE 执行层

也就是：

- `prepare_expert_weight()`
- `update_expert_weight()`
- `expert_routing_map_`
- `enableExpertCumSumOutput`
- `enableEPWB`
- ATB `SparseMoe` 的 `out_gmm_cumsum_list`

这一层决定了：

- expert load 到底怎么被采样；
- 新专家表到底怎么作用到实际 forward；
- 冗余专家怎么在执行图里被路由。

---

## 7. 初始专家分布是怎么建立的

我觉得理解 EPLB 的第一步，不是看“怎么更新”，而是先看“默认专家表是什么样”。

## 7.1 manager 里的初始分布

`EplbManager` 初始化时会创建：

- `state_.expert_load`：`[layer_num, experts_num]`
- `state_.expert_distribution`：`[layer_num, device_num, device_experts_num]`

其中：

- `device_experts_num = experts_num / device_num + redundant_experts_num`
- `device_route_experts_num = device_experts_num - redundant_experts_num`

初始化逻辑是：

1. 每个 device 先顺序持有自己那一段连续 expert；
2. 如果开了冗余槽位，就把**该设备分区最后一个 expert** 重复填进额外槽位。

这意味着初始状态不是“空冗余位”，而是“默认复制每个设备最后一个 expert”。

## 7.2 loader 里的初始设备专家表

DeepSeek 的 loader 里 `initialize_device_expert_list()` 做的事情和 manager 是对齐的：

1. 先把正常分配的 expert 顺序写入 `device_expert_list_`；
2. 如果开启 EPLB，再在每个 device 分区尾部附加若干个冗余槽位；
3. 冗余槽位初始也重复该分区最后一个 expert。

这点非常关键，因为它说明：

- manager 维护的“控制面专家分布表”；
- layer/loader 启动时实际装进去的“执行面专家分布”；

在初始状态是彼此一致的。

## 7.3 为什么这件事重要

如果两边默认分布不一致，那么 engine 收上来的 `expert_load_data` 根本无法正确回映到全局 expert id。现在这两套初始化逻辑对齐了，后面的聚合才站得住。

---

## 8. EPLB 依赖的关键数据结构

## 8.1 `EplbInfo`

这是 engine 发给 worker 的控制消息，包含三部分：

- `prepare_layer_id`
  - 哪一层要开始异步准备新权重。
- `expert_ids`
  - 新的专家分布表，按 `[device][slot]` flatten 后发给所有 worker。
- `update_layer_id`
  - 哪一层已经允许从“准备好的缓冲”切换成“当前活跃权重”。

我认为 `EplbInfo` 最关键的设计是：

- **prepare 和 update 分离**；
- 并且一条消息里**可以同时包含“更新上一层”和“准备下一层”**。

这意味着 EPLB 在时间上是有流水线的，而不是 prepare 完所有层再一起 update。

## 8.2 `ForwardOutput / RawForwardOutput`

worker 返回给 engine 的 EPLB 相关数据有两项：

- `expert_load_data`
  - 当前 worker 每层每个本地专家槽位的负载统计。
- `prepared_layer_id`
  - 当前 worker 最近完成准备的层 id。

为什么这两个字段必须一起回传？

因为 engine 既要知道：

1. “该怎么重新算负载表”；
2. “哪些层已经在所有 worker 上准备好了，可以切换”。

## 8.3 `expert_load_data_`

`WorkerImpl` 在模型加载完成后，如果 `enable_eplb` 为真，会分配：

`[num_moe_layers, num_local_experts + redundant_experts_num]`

的 `expert_load_data_` 张量，并且放到 device 上。这是 worker 侧长期复用的负载输出缓冲。

## 8.4 `state_.expert_distribution`

这是我认为 manager 里最重要的数据结构：

- 维度：`[layer, device, slot]`
- 值：每个槽位当前对应的**原始 global expert id**

注意这里存的不是“复制出来的虚拟 expert id”，而是原始 expert id。也就是说，冗余副本在表里表现为“同一个 expert id 出现多次”。

这一点很重要，因为后续：

- 负载聚合时，多个副本的 load 会重新加回同一个原始 expert；
- routing map 也是围绕原始 expert id 构造的。

---

## 9. 一轮 EPLB 的完整闭环

这是我认为最重要的一节。下面按“启动 -> 采集 -> 计算 -> 准备 -> 切换 -> 再采集”的顺序，把一轮完整 EPLB 闭环串起来。

## 9.1 启动阶段：Engine 和 Worker 先把地基搭好

### 9.1.1 `LLMEngine::init()` 创建 `EplbManager`

如果 `FLAGS_enable_eplb` 为真，`LLMEngine::init()` 会根据：

- `num_layers = n_layers - first_k_dense_replace`
- `num_experts = n_routed_experts`
- `device_num = worker_clients_num_`

创建一个 `EplbManager`。

这里减掉 `first_k_dense_replace` 很关键，因为前面的 dense 层不参与 MoE，自然也不参与 EPLB。

### 9.1.2 `LLMWorkerImpl::init_model()` 创建 `EplbExecutor`

worker 初始化模型时，如果开了 EPLB，会额外构造：

- `eplb_executor_`

它内部会起一个专门的 worker thread，并从设备的 stream pool 拿一条 stream 用来异步准备新专家权重。

### 9.1.3 `WorkerImpl` 分配 `expert_load_data_`

模型 load 完之后，`WorkerImpl` 会为 EPLB 分配长期复用的 device tensor `expert_load_data_`。这样每一轮 decode forward 都可以把专家负载直接写进去，不需要反复临时分配。

### 9.1.4 DeepSeek / GLM MoE 层打开 EPLB 专用执行参数

在 NPU DeepSeek 层里，如果当前层是 MoE 层，且开了 EPLB：

- `param.enableExpertCumSumOutput = decode ? true : false`
- `param.enableEPWB = true`
- `param.numOfRedundantExpert = ep_size * redundant_experts_num`

这说明：

1. EPLB 相关的专家累计输出只在 decode 阶段打开；
2. 底层 fused MoE 图需要知道“有多少冗余专家槽位”；
3. EPWB 这条路径是 EPLB 真正依赖的底层执行开关。

源码里还有一条很直接的注释：

> eplb used in decode stage, while multi stream parallel used in prefill stage

这和前面的配置逻辑是完全一致的。

## 9.2 每个 step 开始前：Engine 从 manager 取一份 `EplbInfo`

`LLMEngine::prepare_inputs()` 在准备 `RawForwardInput` 时，如果开启 EPLB，会调用：

- `eplb_manager_->get_eplb_info()`

然后把同一份 `EplbInfo` 填进所有 `dp_rank` 的输入。

这说明两件事：

1. EPLB 是“集群级统一控制”的，不是每个 dp rank 自己各算各的；
2. `expert_ids` 这张表本身已经是全局 flatten 过的设备-槽位表，所有 worker 拿到同一张表即可，各自按自己的 `ep_rank` 去切片使用。

## 9.3 worker 执行前置 EPLB 逻辑：先 update，再 prepare

`LLMWorkerImpl::step_internal()` 在真正执行模型之前，会先调用：

- `eplb_executor_->eplb_execute(input.eplb_info)`

`eplb_execute()` 的顺序是：

1. 如果 `update_layer_id != -1`，先同步调用 `model_->update_expert_weight(layer_id)`；
2. 如果 `prepare_layer_id != -1`，再把 `prepare_expert_weight(layer_id, expert_ids)` 丢到异步线程里做。

这个顺序我认为非常有讲究：

- “上一层已经准备好的内容”先切到活跃路径；
- “下一层要准备的内容”再异步开始；
- 当前 step 的 forward 正好可以和“下一层 prepare”重叠起来。

换句话说，EPLB 是一个两级流水：

```text
step t 开始前：update(layer A) + async prepare(layer B)
step t forward 执行中：layer B 在后台准备
step t 结束后：worker 回报是否已 prepared(layer B)
后续 step：当所有 worker 都 prepared(layer B)，manager 再允许 update(layer B)
```

## 9.4 worker 真正 forward：MoE 内核顺手把专家负载吐出来

`WorkerImpl::prepare_work_before_execute()` 在构造 `ModelInputParams` 时，如果开启 EPLB，会把 `expert_load_data_` 填进：

- `processed_input.input_params.expert_load_data`

NPU DeepSeek 层在 decode MoE 路径里又会把：

- `expert_routing_map_`
  作为输入张量传给 ATB fused MoE；
- `input_params.expert_load_data[layer_idx]`
  作为输出张量 `outTensor(1)` 绑定给 fused MoE。

第三方 ATB 层里对应的名字就是：

- `out_gmm_cumsum_list`

而且 shape 被明确声明成：

- `numOfDeviceExperts`
- `dtype = int64`

也就是说，EPLB 的负载不是在 C++ 外层自己数 token，而是**底层 MoE fused op 执行时顺手把每个本地专家槽位的累计负载写出来**。

## 9.5 为什么 manager 一上来要对 `expert_load_data` 做差分

`EplbManager::aggregate_multi_layer_expert_loads()` 的第一步，不是直接聚合，而是对每个 worker 的 `expert_load_data` 做：

- 第一列保留；
- 后续列做相邻差分。

这是和 ATB 输出语义对应的：底层输出的是 `cum_sum`，不是已经拆开的 per-slot load。

所以 manager 在聚合前先把：

- “每个槽位之前的累计计数”

还原成：

- “每个槽位自己本轮承接了多少 token”。

这一点如果不注意，会完全看不懂为什么源码里要对列做减法。

## 9.6 worker 结束后：所有 worker 都必须回传 EPLB 数据

正常情况下，engine 只关心 driver worker 的 sample 输出，别的 worker 可以跳过，因为 token 采样结果只要 driver 那份就够了。

但 EPLB 打开以后，逻辑变了：

1. `LLMEngine::update_last_step_result()` 在 overlap 模式下会把 `stride` 改成 `1`；
2. `LLMEngine::step()` 在非 overlap 模式本来就收所有 worker 的 future；
3. `WorkerImpl::step_async()` 会在 `is_driver() || FLAGS_enable_eplb` 时都记录 `last_step_output_`；
4. `WorkerImpl::get_last_step_result()` 也在 `FLAGS_enable_eplb` 打开时强制返回 output；
5. `WorkerService` 在远端 RPC 返回时，即使 `next_tokens` 不存在，只要 `FLAGS_enable_eplb` 为真，也照样打包并返回结果。

这一整套改动的核心原因只有一个：

> EPLB 关心的不是“token 采样结果”，而是“每个 worker 上各自不同的专家负载”。

所以所有 worker 的输出都必须回到 engine。

## 9.7 engine 回收结果后：先恢复张量，再交给 manager

`LLMEngine::process_eplb_data()` 会从 `RawForwardOutput` 里取出：

- `expert_load_data`
- `prepared_layer_id`

把前者恢复成：

- `[num_layers, num_device_experts]`

的张量列表，再把后者组成 `layer_ids` 列表，最后做两件事：

1. `eplb_manager_->set_prepared_layer_ids(layer_ids)`
2. `eplb_manager_->update_expert_load(tensors)`

第一件事是状态推进，第二件事是负载聚合。

## 9.8 manager 聚合负载：把“槽位负载”重新还原成“原始 expert 负载”

这是 EPLB 里非常漂亮的一步。

manager 手里有：

- `expert_ids_list = state_.expert_distribution[layer][device][slot]`
- `expert_loads_list = worker 返回的每 device 每 slot 负载`

然后它对每一层做：

1. 取出每个 device 当前槽位表里的 expert id；
2. 取出每个 device 该层每个槽位的 load；
3. flatten 后对 `expert_load[layer]` 做 `scatter_add_`。

结果就是：

- 如果某个热 expert 被复制到了多个设备多个槽位；
- 这些副本的负载会重新加回到同一个“原始 expert id”上。

所以 policy 看到的不是“副本槽位热不热”，而是“这个原始 expert 总体热不热”。

这就是 EPLB 能在“保留冗余副本”的同时仍然做全局原始 expert 负载判断的关键。

## 9.9 policy 触发：不是每次都更新，而是按时间 + 相似度门限更新

`rebalance_experts_loop()` 不会每收到一次数据就立刻改分布表，而是：

1. 持续从 `expert_load_queue` 里取累计负载；
2. 只有当当前时间和上次记录时间差超过 `eplb_update_interval` 时，才真正调用 `policy->rebalance_experts()`。

进入 policy 后，每层会做：

1. 当前负载 `current_load` 和上次负载 `prev_load` 分别按最大值归一化；
2. 算余弦相似度；
3. 如果相似度小于 `eplb_update_threshold`，才标记这层需要更新。

这说明 EPLB 不是“负载一抖就切”，而是做了两层抑制：

- 时间抑制；
- 相似度抑制。

## 9.10 policy 真正算表：先复制热点专家，再做贪心分配

`EplbPolicy::compute_balanced_pack()` 的逻辑我拆成两步来理解。

### 第一步：`update_origin_weights()` 决定复制谁

它会重复 `device_num * redundant_experts_num` 次：

1. 找当前最热 expert；
2. 给它分配一个冗余副本；
3. 按 `count / (count + 1)` 这个动态比例把该 expert 的“有效负载权重”往下调。

这个动态公式的含义非常直观：

- 某个 expert 已经有越多副本，继续复制它带来的负载摊薄收益就越小；
- 所以下一轮最热 expert 可能会变成别的 expert。

### 第二步：`compute_balanced_pack()` 先放副本，再放主专家

接下来会做两轮贪心：

1. **先放冗余副本**：总是把副本放到当前总负载最小、且还有空槽位的 device；
2. **再放主专家**：按调整后的负载从高到低排序，再贪心放到当前最轻的可用 device。

最后得到：

- `device_assignments[device][slot] = original_expert_id`

注意这里即使是冗余副本，写进去的仍然是**原始 expert id**，不是虚拟副本 id。也就是说，“副本”在控制面上表现为“同一个 expert id 在多处重复出现”。

## 9.11 manager 进入逐层状态机：prepare 和 update 被彻底分开

policy 返回后，manager 会更新：

- `state_.expert_distribution`
- `state_.enable_update_vec`
- `state_.to_be_prepared`

然后 `eplb_manager_loop()` 开始盯逐层状态：

1. 选出下一层 `to_be_prepared`；
2. 等所有 worker 都上报自己已经 `prepared_layer_id == to_be_prepared`；
3. 一旦全部准备完成，就把这层设成 `ready_layer_id`；
4. 再继续推进下一层的准备。

这就形成了一个严格的“逐层准备 -> 全局确认 -> 逐层切换”机制。

## 9.12 下一轮 step：engine 可能同时拿到“更新上一层 + 准备下一层”

`EplbManager::get_eplb_info()` 会：

- 把 `ready_layer_id` 填成 `update_layer_id`；
- 如果存在新的 `to_be_prepared`，并且还没发出去过，就把它填成 `prepare_layer_id`，同时把新专家表 flatten 成 `expert_ids`。

然后它把 `ready_layer_id` 清掉。

所以最理想的流水就是：

```text
时刻 1：下发 prepare(layer 3)
时刻 2：所有 worker 都 prepared(layer 3)
时刻 3：下发 update(layer 3) + prepare(layer 7)
时刻 4：所有 worker 都 prepared(layer 7)
时刻 5：下发 update(layer 7) + prepare(next)
```

这也是我为什么说 EPLB 不是“同步全量切换”，而是一个逐层 pipelined 的执行时序。

---

## 10. `EplbExecutor`：为什么 prepare 和 update 必须拆开

我觉得如果不理解 `EplbExecutor`，就很难理解 EPLB 的真正工程做法。

## 10.1 `update_expert_weight()` 是同步切换

当 `update_layer_id` 到来时，worker 直接在当前 step 开始前调用：

- `model_->update_expert_weight(layer_id)`

对于 DeepSeek 层来说，这一步会：

1. 把当前活跃权重和 `ExpertBuffer` 里的准备权重做 `swap`；
2. 更新 `atb_weight_tensors_`；
3. 重绑 `prefill_node_ / decode_node_ / decode_mla_node_` 的输入指针；
4. 更新该层的 `expert_routing_map_`。

也就是说，它没有重建整层模型，而是做了一次**原地张量指针切换**。这是非常典型的低延迟工程化做法。

## 10.2 `prepare_expert_weight()` 是异步后台准备

当 `prepare_layer_id` 到来时，`EplbExecutor` 不会立刻在主线程里做，而是：

1. 把任务塞进队列；
2. 唤醒 EPLB 专用 worker thread；
3. 在专用 stream 上调用 `model_->prepare_expert_weight(layer_id, expert_ids)`；
4. 同步该 stream；
5. 把 `ready_layer_id_` 置成当前层。

这说明 prepare 阶段的目标是：

- 尽量和当前正常 decode forward 重叠；
- 只在完成后上报“我准备好了”，但**不立即切换**。

## 10.3 为什么必须拆成两阶段

我认为根本原因有三个：

1. **prepare 很重**：需要从 shared memory 取权重、拼接 gate/up/down 权重、拷到 device buffer；
2. **update 必须很轻**：当前 step 开始前如果做重 IO，会直接拉高时延；
3. **全局一致性要求**：只有当所有 worker 都 prepared 某层之后，才能让 engine 发出 update 命令，避免部分设备切换、部分设备未切换的状态。

---

## 11. 共享内存权重缓冲：EPLB 真正切换的不是概念，而是张量

## 11.1 `ExpertBufferManager` 是按 expert 建 shared memory 的

`ExpertBufferManager` 会为每个 expert 创建一个 `ExpertBufferShm`，底层共享内存名是：

- `xllm_expert_{expert_id}`

也就是说，共享内存的粒度是：

- 每个 expert 一段共享内存；
- 每段共享内存里再按 layer 和 tensor_name 切分。

## 11.2 为什么 loader 不是每个进程都把所有 expert 都存一份

DeepSeek loader 里有个很关键的判断：

- `needs_eplb = enable_eplb && (rank % localWorldSize == expert_index % localWorldSize)`

这说明启用 EPLB 时，不是每个 rank 都把全量专家复制一份到自己私有内存，而是：

- 由 node 内某个满足模条件的 rank 负责把该 expert 写进共享内存；
- 其他本地 rank 通过同一个 shared memory 名字来读取。

我认为这是一个非常实用的优化：

- 避免每个进程都重复保留全量专家的 host 侧副本；
- 但又允许任何需要该 expert 的本地 worker 在 prepare 阶段读到它。

## 11.3 `prepare_expert_weight()` 真正做了什么

以 DeepSeekV2/V32 为例，prepare 阶段会：

1. 根据新的 `expert_list` 构建 `expert_routing_map_buffer_`；
2. 从 `shared_buffer->get_tensor(expert_id, layer_id, shm_key)` 把该层所需专家权重取出来；
3. 分别把：
   - `gate_proj`
   - `up_proj`
   - `down_proj`
   - offset / scale
   合并拷贝到 `ExpertBuffer::Instance()` 里；
4. 对需要的权重做 NPU format cast。

所以 prepare 阶段本质上是在做：

- **从共享内存取新专家张量**
- **重组成当前层 fused MoE 实际需要的 device buffer 结构**

## 11.4 为什么 manager 必须逐层推进

这里我觉得是整个 EPLB 设计里最容易被忽略，但其实最本质的一点。

DeepSeek 层里用于 prepare 的 `ExpertBuffer` 是：

- `ExpertBuffer::Instance()`

也就是**进程级单实例**。

这意味着：

- 如果同时准备两层，它们会写同一套缓冲；
- 后准备的层会覆盖先准备的层；
- update 时根本分不清该切哪一层的准备结果。

所以 manager 采用“逐层 prepare / 逐层确认 / 逐层 update”的状态机，不只是策略保守，而是和 worker 侧缓冲设计强耦合的必然结果。

这一点是我这次读 EPLB 时最大的收获之一。

---

## 12. 模型层如何把“专家表”真正作用到 forward

## 12.1 `build_expert_routing_map()` 的作用

DeepSeek 层拿到一份 flatten 后的全局 `expert_list` 后，会构建：

- `expert_routing_map_`

其核心逻辑是：

1. 统计每个原始 expert id 在 `expert_list` 里出现在哪些槽位；
2. 如果某个 expert 被复制了多份，就按 `ep_rank % num_duplications` 选择一个槽位索引；
3. 生成 `expert_id -> 槽位索引` 的映射张量。

也就是说，routing map 解决的是：

> 当同一个 expert 出现在多个槽位（冗余副本）时，当前设备/当前 EP rank 在运行时应该把该 expert 路由到哪一个槽位去执行。

## 12.2 update 时不只换权重，还换 routing map

`update_expert_weight()` 不只是 swap 权重张量，它还会：

- `expert_routing_map_[layer] = expert_routing_map_buffer_`

这说明 EPLB 真正更新的是两样东西：

1. 本地活跃的专家权重张量；
2. fused MoE 在本层执行时使用的专家路由映射。

如果只换权重不换 map，执行图还会按旧表走；如果只换 map 不换权重，执行图会路由到错误的权重。所以这两个必须同时生效。

## 12.3 为什么负载采集只在 decode 打开

DeepSeek 层和 ATB graph 两边都对应得很清楚：

- prefill 时不打开 `enableExpertCumSumOutput`
- decode 时才把 `expert_load_data[layer]` 挂到 MoE 输出上

我理解这里的工程含义是：

1. prefill 阶段更重吞吐和多流；
2. decode 阶段是 MoE 长尾热点更持续、更值得动态平衡的阶段；
3. 采集累计负载本身也有额外代价，所以只在最需要的阶段打开。

---

## 13. `EplbManager` 的两条后台线程到底各自管什么

## 13.1 `rebalance_experts_loop()`：负载汇总 + policy 触发

这条线程负责：

1. 等 `expert_load_queue` 里有新数据；
2. 把每次 worker 回来的多设备负载聚合到 `state_.expert_load`；
3. 到时间了就调用 policy 重新算表；
4. 产出新的 `state_.expert_distribution` 和 `enable_update_vec`；
5. 找出下一层 `to_be_prepared`；
6. 唤醒状态机线程。

## 13.2 `eplb_manager_loop()`：逐层准备完成确认

这条线程不关心负载数值本身，它只关心：

- 某一层是不是已经在所有 device 上都准备好了。

它的逻辑是：

1. 等 `to_be_prepared != -1`；
2. 不断检查 `prepared_layer_id[device]` 是否都等于当前 `to_be_prepared`；
3. 一旦全部满足，就把它提升成 `ready_layer_id`；
4. 然后继续找下一层。

所以这两条线程的职责分离得很清楚：

- 一条算“该更新哪些层”；
- 一条盯“哪些层已经准备好可以切换”。

## 13.3 `expert_load = expert_load / 2` 的含义

每次 policy 触发后，manager 会做：

- `state_.expert_load = torch::div(state_.expert_load, 2, "trunc")`

源码没有明确注释这一步的语义，但从行为上看，我倾向于把它理解为：

- **一种非常轻量的历史衰减/平滑**；
- 避免旧历史负载永远累加，导致新分布变化迟钝。

这是我的**推断**，不是源码直接写出来的结论，所以我专门标记一下。

---

## 14. `EplbPolicy`：新专家表到底是怎么算出来的

如果把 EPLB 当成“高级黑盒”，最容易漏掉的就是 policy 的具体算法。其实当前实现并不复杂，但非常工程化。

## 14.1 第一层门：负载变化不明显就不更新

对于每一层：

1. 当前负载按最大值归一化；
2. 上一次负载也按最大值归一化；
3. 算余弦相似度；
4. 小于阈值才更新。

这意味着 EPLB 不是盯绝对值，而是盯**负载分布形状有没有明显变化**。

## 14.2 第二层逻辑：先决定谁值得复制

`update_origin_weights()` 每次找当前最热 expert，把它再复制一份，并把它的有效权重往下调整。

这个过程重复：

- `device_num * redundant_experts_num`

次，等于整个集群新增的总冗余槽位数。

## 14.3 第三层逻辑：贪心把副本和主专家塞进设备

分配规则非常朴素但有效：

1. 冗余副本优先放到当前总负载最轻的设备；
2. 然后再把所有主专家按调整后的热度从高到低往剩余空槽里填；
3. 每次都尽量让设备总负载更均衡。

这不是最优全局算法，但优点是：

- 简单；
- 可解释；
- 在在线重平衡场景里代价低。

## 14.4 为什么副本仍然写回原始 expert id

policy 在 `redundancy_map` 里会产生一些“虚拟副本编号”，但真正写进 `device_assignments` 的依然是原始 expert id。

这说明虚拟编号只是 policy 内部用来“数副本次数”的中间手段，最终输出给系统其他部分的仍然是一张：

- 槽位 -> 原始 expert id

的表。

这样一来：

- routing map 好构建；
- 负载好回收；
- 不需要引入额外“副本 expert id”语义。

---

## 15. 为什么说当前 EPLB 不是 scheduler 负载均衡

这是我这次学习里特别想强调的一点。

## 15.1 当前 scheduler 没被 EPLB 改写

我这次没有看到 EPLB 去修改：

- waiting/running/pending 队列组织；
- token budget；
- request preemption；
- batch 重排；
- request 到 expert 的调度策略。

它真正改变的是：

- 同一个 expert 在不同 worker/device 上的副本分布；
- fused MoE 在 decode 时的专家路由表；
- worker 实际持有的专家权重集合。

## 15.2 文档的“未来工作”恰好证明当前边界

`docs/zh/features/eplb.md` 里明确写了未来工作之一是：

- 与调度层结合，通过请求 batch 的重组实现更好的负载均衡。

这和当前源码是高度一致的：

- **今天的 EPLB 还没有做到调度层重组 batch**；
- 它现在是执行层的 expert redistribution。

所以如果要用一句话概括当前 EPLB：

> 它是“通过动态复制并重映射热点 expert，来平衡 MoE 执行负载”的机制，而不是“通过重新组织请求”来平衡负载。

---

## 16. 我认为最关键的几个设计点

## 16.1 设计点一：prepare/update 两阶段 + 全局确认

这是保证一致性和时延的核心。

## 16.2 设计点二：逐层状态机不是保守，而是和单缓冲实现强耦合

worker 侧 `ExpertBuffer::Instance()` 是单实例，这直接决定了 manager 只能逐层推进。

## 16.3 设计点三：负载采集和分布重映射都围绕“原始 expert id”展开

副本只是执行层的多个槽位，不改变全局统计对象仍然是“原始 expert”。

## 16.4 设计点四：共享内存把 host 侧专家缓存做成 node-local 共享资源

这避免了每个进程重复持有全量专家 host 权重。

## 16.5 设计点五：EPLB 是可流水的

同一个 step 里：

- 可以 update 之前准备好的层；
- 同时 async prepare 下一层；
- forward 正常继续。

这让它更像一个持续运行的后台重平衡系统，而不是某种暂停式重配置。

---

## 17. 我目前确认的边界与疑问

## 17.1 已确认的事实

1. EPLB 当前主落点在 `Engine / Worker / Model Layer / ATB SparseMoe`，不是 scheduler。
2. 负载采集来自 decode 阶段 fused MoE 的累计输出，而不是外层自己统计 token。
3. worker 需要把所有设备结果都回传给 engine，driver-only 逻辑在 EPLB 场景下不够用。
4. policy 是“时间门控 + 相似度门控 + 冗余副本贪心分配”。
5. 逐层更新是 manager 的明确机制，不是文档抽象说法。
6. prepare 与 update 分离，并且可以在同一轮里形成流水。
7. 共享内存 + 单实例 ExpertBuffer 是权重更新真正发生的物理载体。

## 17.2 我认为有证据支撑的推断

1. 当前 EPLB 的最佳适配场景是 `expert_parallel_degree=2` 且 `ep_size == 实际设备数` 的动态 EP 路径。
2. `expert_load / 2` 很像是在做轻量历史衰减，而不是简单清零。

## 17.3 我还保留的一个时序疑问

有一个点我认为值得后续专门验证：

- manager 在 policy 计算出新分布后，会立即更新 `state_.expert_distribution`；
- 但 worker 真正切换到新分布，要等所有设备 prepare 完，再收到 `update_layer_id` 之后才生效。

从纯代码表面看，这里存在一个值得继续确认的时序问题：

- 在“新分布已经写进 manager，但某层还没真正 update 到 worker”的这段窗口期里，manager 后续聚合 `expert_load_data` 时，用的是哪张表作为“当前有效 expert 分布”的真值。

我目前不想贸然下结论说这是 bug，因为需要结合更细的运行时时序甚至日志验证；但从代码结构上看，这是我觉得最值得继续追的一处细节。

---

## 18. 一句话总结我现在对 EPLB 的理解

如果让我用一句话总结 xLLM 当前的 EPLB，我会这样说：

> xLLM 的 EPLB 本质上是一套挂在执行面上的、逐层流水化的动态 MoE 专家重分布系统：它在 decode 阶段从 fused MoE 内核收集每个专家槽位的累计负载，由 engine 汇总成全局 expert 热度，policy 重新生成“专家到设备槽位”的映射表，worker 在后台异步准备新专家权重，并在全局确认后原子切换该层的权重与 routing map，从而用“热点 expert 复制 + 动态重映射”的方式缓解 MoE 负载失衡。

这套机制已经相当完整，但当前主要还是 **执行层专家重排**，还不是 **调度层请求重组**。

