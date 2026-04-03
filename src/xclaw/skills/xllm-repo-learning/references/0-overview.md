# xLLM 学习结果沉淀

## 当前任务

- 任务 ID: `L02`
- 任务名称: 启动入口与配置体系梳理
- 当前结论: 已基于源码完成启动入口、配置分层、`master/engine/server` 创建时序的第一轮源码级梳理，并形成启动时序图、配置分层表与关键模块理解。

## 前置结论（承接 L01）

- xLLM 的主干架构仍然可以稳定概括为：服务接入层 -> 控制/调度层 -> 执行骨架层 -> 模型层 -> 平台/算子优化层。
- `xllm/xllm.cpp` 是统一启动入口，`server/api_service` 是服务边界，`distributed_runtime/scheduler/runtime/framework` 构成主执行链。
- NPU/国产芯片优化重点仍然落在图执行、xTensor、KV Cache、通信重叠、自定义算子与系统级调度能力上。

## 本轮阅读文件

1. `xllm/xllm.cpp`
2. `xllm/core/common/global_flags.h`
3. `xllm/core/common/global_flags.cpp`
4. `xllm/core/common/options.h`
5. `xllm/core/common/options.cpp`
6. `xllm/core/common/model_capability.h`
7. `xllm/core/common/model_capability.cpp`
8. `xllm/server/xllm_server_registry.h`
9. `xllm/server/xllm_server_registry.cpp`
10. `xllm/server/xllm_server.h`
11. `xllm/server/xllm_server.cpp`
12. `xllm/core/distributed_runtime/master.h`
13. `xllm/core/distributed_runtime/master.cpp`
14. `xllm/core/distributed_runtime/llm_master.h`
15. `xllm/core/distributed_runtime/llm_master.cpp`
16. `xllm/core/runtime/options.h`
17. `xllm/models/model_registry.h`
18. `xllm/models/model_registry.cpp`
19. `xllm/api_service/api_service.h`
20. `xllm/api_service/api_service.cpp`
21. `xllm/core/distributed_runtime/dist_manager.cpp`
22. `xllm/core/util/device_name_utils.h`
23. `xllm/core/util/device_name_utils.cpp`

## 本轮回答的关键问题

### 1. `model_type`、`backend`、`task` 是如何推导的

- `task` 最直接，来自 CLI flag `FLAGS_task`，默认值是 `generate`，在启动阶段直接写入 `Options::task_type`。
- `backend` 允许缺省；若用户不传 `--backend`，`run()` 会调用 `get_model_backend(model_path)` 自动推导：
  - 若模型目录存在 `model_index.json` 且包含 `_diffusers_version`，直接判定为 `dit`。
  - 否则读取 `config.json` 中的 `model_type` 或 `model_name`，再通过 `ModelRegistry::get_model_backend(model_type)` 反查为 `llm/vlm/rec`。
- `model_type` 本身不进入 `Options`，而是作为“派生能力判定输入”被立即使用：
  - 计算 `use_mla = ResolveUseMla(model_type, backend)`；
  - 自动选择 `tool_call_parser`；
  - 自动选择 `reasoning_parser`；
  - 校验 `enable_prefill_sp` 等 flag 是否与模型能力兼容。

### 2. 哪些配置停留在 CLI，哪些进入 `Options`，哪些进入 `runtime::Options`

- `FLAGS_*` 是最外层原始输入，但并不是所有 flag 都会进入 `Options`。
- `xllm::Options` 是启动阶段整理后的“控制面配置快照”，承载 `Master` 视角需要的配置，同时包含若干已经归一化后的派生值。
- `xllm::runtime::Options` 是 `Master` 构造 `Engine` 时进一步下沉的“执行面配置快照”，设备列表、共享内存大小、KV cache/store 相关参数、分布式拓扑等在这里进一步转成 worker/engine 可直接消费的格式。
- 仍停留在 CLI/全局 flag 空间的配置主要有三类：
  1. brpc server 自身参数，例如 `port/num_threads/rpc_idle_timeout_s`；
  2. 启动期一次性逻辑，例如 `enable_xtensor`、`enable_manual_loader`、`enable_rolling_load`；
  3. 尚未被 `Options` 吸收的全局状态，例如 `random_seed`。

### 3. 启动阶段何时创建 server，何时创建 master/engine

- `engine` 不是在 `master->run()` 里创建的，而是在 `Master` 构造函数里立刻根据 `Options` 创建。
- `master->run()` 对 `LLMMaster` 而言只是拉起 scheduler 推进线程，对 `LLMAssistantMaster` 则是拉起一个保活线程；它本身不负责创建 HTTP server。
- `APIService` 和 `HttpServer` 都是在 `master->run()` 之后才创建。
- `XllmServer::start(std::unique_ptr<APIService>)` 会直接 `RunUntilAskedToQuit()`，因此 API server 启动路径本身是阻塞的。

## 启动主流程（源码级）

```mermaid
sequenceDiagram
    participant CLI as CLI/gflags
    participant Main as main()
    participant Run as run()
    participant Opt as xllm::Options
    participant Master as Master/LLMMaster
    participant Engine as Engine
    participant API as APIService
    participant Server as XllmServer

    CLI->>Main: 传入 --model/--backend/--task/...
    Main->>Main: 预检查 --help
    Main->>Main: ParseCommandLineFlags()
    Main->>Run: run()
    Run->>Run: 校验 model 路径
    Run->>Run: 推导 model_id / backend / host / is_local
    Run->>Run: 读取 model_type, 推导 use_mla/parser
    Run->>Run: validate_flags()
    Run->>Opt: 组装 xllm::Options
    Run->>Run: 可选初始化 XTensor/PhyPagePool
    Run->>Master: create_master() / LLMAssistantMaster()
    Master->>Engine: 在 Master 构造函数中创建 Engine
    Run->>Master: master->run()
    Run->>API: 构造 APIService(master)
    Run->>Server: register_server(\"HttpServer\")
    Run->>Server: start(api_service)
    Server->>Server: RunUntilAskedToQuit()
```

### 分阶段展开

1. `main()` 先手动扫描 `--help/-h`，再调用 `google::ParseCommandLineFlags()`，最后检查 `--model` 是否存在。
2. `run()` 首先校验模型目录是否存在，并从模型路径自动补全 `model_id`。
3. 若未显式指定 `--backend`，则根据 `model_index.json` / `config.json` 自动推导 backend。
4. 若 `--host` 为空，则用本机 IP 回填；再通过 `FLAGS_master_node_addr` 与 `FLAGS_host` 对比推导 `is_local`。
5. 启动前会做一轮 flag 归一化：
   - `vlm` 强制关闭 `enable_prefix_cache` 和 `enable_chunked_prefill`；
   - 若 `max_tokens_per_chunk_for_prefill < 0`，回填为 `max_tokens_per_batch`；
   - 自动推导 `tool_call_parser`、`reasoning_parser`；
   - 自动推导 `use_mla`；
   - `validate_flags()` 处理平台约束与组合约束。
6. 随后组装 `xllm::Options`，这一步是 CLI flag -> 控制面配置对象的边界。
7. 若开启 `enable_xtensor`，在创建 `Master` 之前就初始化 `XTensorAllocator` 和 `PhyPagePool`，因此这是启动期的前置一次性逻辑，而不是 `Master` 内部逻辑。
8. 创建 `Master` 时，`node_rank != 0` 走 `LLMAssistantMaster`，`node_rank == 0` 走 `create_master(backend, options)`。
9. `Master` 构造函数内部会立刻创建 `Engine`，并把 `Options` 下沉成 `runtime::Options`。
10. `master->run()` 拉起主循环线程后，主节点（`node_rank == 0`）或 `enable_xtensor` 场景会继续创建 `APIService` 与 `HttpServer`。
11. `XllmServer::start()` 绑定路由并阻塞在 brpc 事件循环。

## 配置分层表

| 配置主题 | 来源 | `xllm::Options` | `runtime::Options` | 最终消费者/作用 |
|---|---|---|---|---|
| 模型路径 | `FLAGS_model` | `model_path` | `model_path` | `Master/Engine` 加载模型 |
| 模型标识 | `FLAGS_model_id`，为空则由路径名推导 | `model_id` | `model_id`（LLM 路径） | 服务侧模型名、engine 标识 |
| backend | `FLAGS_backend`，为空则从模型文件自动推导 | `backend` | `backend` | `create_master()`、engine 类型、APIService 分支 |
| task | `FLAGS_task`，默认 `generate` | `task_type` | `task_type` | embedding/generate 路径分流、worker 类型选择 |
| devices | `FLAGS_devices` 字符串 | `devices` | `devices`（解析成 `vector<torch::Device>`） | 本地/远端 worker 拓扑与设备绑定 |
| 多机拓扑 | `master_node_addr/nnodes/node_rank/dp_size/ep_size` | 对应字段全量保留 | 对应字段全量保留 | multi-node、PD、worker server 组织 |
| MLA 能力 | `model_type + backend` 派生 | `use_mla` | `use_mla` | engine/model 执行能力选择 |
| parser 自动选择 | `model_type` 派生 `tool_call_parser/reasoning_parser` | 保留在 `Options` | 不下沉 | API/请求解析能力 |
| prefix cache / chunked prefill | 原始 flags，`vlm` 会被启动入口强制改写 | `enable_prefix_cache` / `enable_chunked_prefill` | 同名字段 | scheduler / engine 行为 |
| `max_tokens_per_chunk_for_prefill` | `<0` 时回填为 `max_tokens_per_batch` | 已归一化写入 | 同名字段 | chunked prefill 调度 |
| service routing / cache upload | 原始 flags + 组合条件派生 | `enable_service_routing` / `enable_cache_upload` | 同名字段 | xservice、prefix cache 上报 |
| kvcache store | `enable_kvcache_store && enable_prefix_cache && host_blocks_factor > 1.0` | 已归一化写入 | 同名字段 | host-side KV cache store |
| shared memory | `enable_shm/input_shm_size/output_shm_size` | 原样进入 | 大小在 `Master` 中转换为字节 | 进程内共享内存执行路径 |
| brpc server 参数 | `host/port/num_threads/rpc_idle_timeout_s` | 不进入 | 不进入 | `XllmServer` 监听与线程数 |
| xtensor/manual loader/rolling load | `enable_xtensor/enable_manual_loader/enable_rolling_load` | 基本不进入 | 不进入当前 runtime 选项 | 启动前内存/权重加载模式初始化 |
| 随机种子 | `FLAGS_random_seed`，主节点缺省时随机生成 | 不进入 | 不进入 | 全局随机行为 |

## 关键模块理解

### 1. `xllm/xllm.cpp`: 启动编排器

这个文件不是简单的 `main + run`，而是整个系统启动语义的集中编排点。

- 核心局部函数：
  - `get_model_type(model_path)`：读取 `config.json` 的 `model_type/model_name`。
  - `get_model_backend(model_path)`：先看 `model_index.json` 判 `dit`，否则通过 `ModelRegistry` 映射 backend。
  - `validate_flags(model_type)`：对平台能力、flag 组合与模型能力做启动前硬校验。
  - `run()`：完成所有派生、归一化、对象创建和 server 启动。
- 关键流程特征：
  - 同时承担“用户输入归一化”和“系统拓扑定型”两种职责。
  - 在这里就已经把 `backend`、`is_local`、`use_mla`、parser、`enable_cache_upload`、`enable_kvcache_store` 等关键语义定死。
  - `run()` 之后，后续模块基本假设这些语义已经稳定。

### 2. `global_flags.*`: 外部配置输入层

- `global_flags.cpp` 按服务、模型、scheduler、并行、PD、store、xtensor、beam search 等类别定义了大量 gflags。
- 这一层的职责不是表达业务语义，而是接收“未经整理的用户输入”。
- 重要观察：
  - 默认值相当激进，例如 `devices` 默认 `npu:0`、`task` 默认 `generate`。
  - 很多 flag 不会原封不动保留下来，而是先在 `xllm.cpp` 被归一化后再进入 `Options`。
  - server 监听与 brpc 线程池参数完全停留在这层，不进入 `Options`。

### 3. `Options`: 控制面配置对象

- `xllm::Options` 是 `Master` 视角的配置中心。
- 它不是 `FLAGS_*` 的简单镜像，而是“已经过启动入口整理后的语义配置”：
  - 既有原始参数，如 `backend/task_type/devices`；
  - 也有派生能力，如 `use_mla`；
  - 还有组合后的系统特性开关，如 `enable_cache_upload`、`enable_kvcache_store`。
- `Options::to_string()` 进一步说明它是启动日志与问题排查的核心配置快照。

### 4. `model_capability.*`: 模型能力派生层

- 这一层负责回答“给定 model type，系统应该自动打开哪些能力”。
- 当前已确认：
  - `ResolveUseMla(model_type, backend)` 是 MLA 判定中心。
  - `ResolveSpeculativeDraftModelType(...)` 负责 MTP 草稿模型类型映射。
  - `ResolveUseMlaFromModelPath(...)` 把“读模型配置”封装成独立能力函数。
- 额外观察：
  - `xllm.cpp` 里仍有一份局部 `get_model_type()` 逻辑，与 `model_capability.cpp` 的 `GetModelType()` 存在重复，说明该能力边界还没有完全收拢。

### 5. `ServerRegistry` + `XllmServer`: 服务壳层

- `ServerRegistry` 是一个非常薄的 singleton registry，职责只是按名字持有 `XllmServer` 实例。
- `XllmServer` 才是 brpc 封装主体：
  - `start(std::unique_ptr<APIService>)` 负责绑定 HTTP/RPC 路由；
  - 其他 `start(...)` 重载服务于 `DisaggPDService`、`WorkerService`、`CollectiveService`、`XTensorDistService`。
- 对 API 路径来说，`start(api_service)` 会直接阻塞在 `RunUntilAskedToQuit()`，说明“服务主循环”就是 brpc server 本身。

### 6. `Master` / `create_master()` / `runtime::Options`: 控制面到执行面的边界

- `Master` 构造函数内部就会：
  1. 打印启动 banner；
  2. 记录 `Options`；
  3. 处理少量全局 flag 回写；
  4. 解析设备字符串；
  5. 在必要时修正 `enable_service_routing`、`enable_schedule_overlap`；
  6. 把 `xllm::Options` 下沉成 `runtime::Options`；
  7. 直接构造 `Engine`。
- 这说明：
  - `Master` 既是控制中枢，也是启动期的执行面装配器。
  - `runtime::Options` 是“可执行配置”，比 `xllm::Options` 更贴近 worker/engine。
- `create_master()` 再根据 backend 选择 `LLMMaster/VLMMaster/DiTMaster/RecMaster`。

### 7. `LLMMaster`: 运行循环启动点

- `LLMMaster` 构造阶段会先 `CHECK(engine_->init(...))`，然后构造 scheduler、tokenizer、threadpool。
- `run()` 本身并不做重初始化，只是拉起 `scheduler_->step(timeout)` 的后台线程。
- 这进一步坐实了两个事实：
  1. `engine` 和 `scheduler` 在 `run()` 前已经准备好；
  2. API server 的创建时机晚于 master 运行循环。

### 8. `APIService`: 已运行 Master 之上的服务适配层

- `APIService` 只有在 `master->run()` 之后才会被创建。
- 它根据 `FLAGS_backend` 动态组装不同的 service impl：
  - `llm` 走 completion/chat/embedding/rerank；
  - `vlm` 走多模态 chat/mm embedding；
  - `dit` 走 image generation；
  - `rec` 走 rec completion。
- 因此 `APIService` 不是“驱动引擎启动”的入口，而是“复用已启动 master”的协议适配层。

## 类关系与数据流（L02 视角）

```text
CLI FLAGS
  -> xllm.cpp 归一化/派生
  -> xllm::Options
  -> Master(持有 Engine)
  -> runtime::Options
  -> Engine / Worker 侧

同时：
Master(run 已启动)
  -> APIService(master*)
  -> ServerRegistry
  -> XllmServer(brpc)
```

这里最关键的边界是：

- `FLAGS_*` 是外部输入，不等于内部事实。
- `Options` 是“控制面事实”。
- `runtime::Options` 是“执行面事实”。
- `APIService` 不是启动源头，而是运行中 `Master` 的上层服务封装。

## 关键设计观察

### 1. `backend` 与 `model_type` 是两层概念

- `backend` 决定走哪条主执行骨架：`llm/vlm/dit/rec`。
- `model_type` 决定该骨架下的具体模型能力，例如是否使用 MLA、采用哪种 parser、是否支持某些优化。
- 这意味着 xLLM 启动时先定“任务骨架”，再定“模型细节能力”。

### 2. 启动入口承担了大量策略归一化职责

- 例如 `vlm` 强制关 prefix cache/chunked prefill；
- `enable_cache_upload` 和 `enable_kvcache_store` 都不是简单透传，而是被重新组合。
- 这说明启动入口本身就是一层重要策略控制点，而非简单 bootloader。

### 3. server 生命周期依附于已经启动的 master

- 主链不是“server 收请求后再懒初始化引擎”。
- 主链是“先把 master/engine/scheduler 起好，再把 APIService 挂到 brpc 上”。
- 这对后续理解 warmup、sleep/wakeup、fork_master 很关键。

### 4. 仍存在部分全局 flag 耦合

- `XllmServer` 直接读 `FLAGS_port/FLAGS_num_threads/...`；
- `APIService` 和其他模块也直接依赖 `FLAGS_backend` 等全局量；
- `Master` 构造函数还会反向写回部分 `FLAGS_*`。
- 说明系统虽然已经引入 `Options`，但还没有完全摆脱全局 flag 风格。

## 证据边界

### 已直接确认

- `backend` 缺省时可以从模型目录自动推导。
- `model_type` 当前主要用于派生能力而不是进入 `Options`。
- `Master` 构造阶段就会创建 `Engine`，不是在 `run()` 里。
- `APIService` 和 `HttpServer` 在 `master->run()` 后才创建。
- `runtime::Options` 是 `Master` 到 `Engine` 的显式下沉层。
- `enable_xtensor` 是启动前一次性初始化逻辑，不属于当前 `Options/runtime::Options` 主下沉链。

### 仍待后续任务验证

- `LLMMaster` 之外的 `VLMMaster/DiTMaster/RecMaster` 在 `run()` 语义上有哪些差异。
- `fork_master/sleep/wakeup` 如何修改已运行 master 的生命周期。
- 分布式服务、worker server、collective server、xtensor dist service 的完整启动时序。
- `task_type=embed/mm_embed` 在 request/scheduler/batch 内部的完整状态机差异。

## 对后续任务的输入

- `L03` 可以直接基于本轮结论继续看 `APIService`、`XllmServer` 与各类 service impl，重点关注“协议入口如何把请求转成 `Master::handle_request`”。
- `L04` 可以直接复用本轮已经坐实的边界：`Master` 负责装配与控制、`Engine` 负责执行、`APIService` 只是服务适配层。
- `L11` 再回头深挖 `enable_xtensor/enable_manual_loader/enable_rolling_load` 的执行层影响会更顺。

## 全仓级补充文档（索引版）

这一节不是替代后续 `L03-L15` 的源码深挖，而是先把全仓“地形图”补全。目标不是对 4 万多个文件逐行逐句解释，而是先把哪些是主仓源码、哪些是文档、哪些是第三方依赖、哪些是构建/生成物讲清楚，并给出继续深读时的路径。

### 1. 仓库文件普查与学习边界

#### 1.1 全仓文件规模

基于当前工作区的文件普查，仓库总文件数约为 `43776`。其中：

| 顶层目录 | 文件数 | 学习意义 |
|---|---:|---|
| `build/` | 18843 | 构建产物与中间文件，不适合作为源码学习主对象 |
| `third_party/` | 16343 | 第三方依赖与外部子仓，需要按“边界”理解 |
| `.git/` | 5337 | 版本库元数据，非学习对象 |
| `xllm/` | 1125 | 主仓核心源码，是学习主战场 |
| `kernel_meta/` | 1009 | 运行/编译生成的 kernel 元信息，不作为主线源码 |
| `docs/` | 87 | 官方设计说明、功能说明、部署说明 |
| `examples/` | 8 | 用户侧调用示例 |
| `ralph/` | 14 | 本轮学习计划、任务与沉淀文件 |

#### 1.2 建议的“全仓阅读”边界

从源码学习角度，仓库应拆成三层：

1. 主仓源码层
   - `xllm/`
   - `docs/`
   - `examples/`、`examples2/`
2. 关键第三方边界层
   - `third_party/Mooncake`
   - `third_party/brpc`
   - `third_party/xllm_ops`
   - `third_party/xllm_atb_layers`
3. 生成物/环境层
   - `build/`
   - `kernel_meta/`
   - `.git/`
   - `.local/`

这意味着“将所有文件都读完”如果按字面理解并不高效，因为生成物和版本元数据会淹没主线。更合理的做法是：

- 对主仓源码和文档做系统扫读；
- 对关键第三方做边界理解；
- 对生成物只做角色说明，不逐行深读。

### 2. 主仓源码总地图

#### 2.1 `xllm/` 顶层模块

`xllm/` 目录当前大致可以按下面的模块切：

| 模块 | 文件规模 | 角色 |
|---|---:|---|
| `xllm/api_service` | 43 | 协议适配与对外 API 实现 |
| `xllm/server` | 6 | brpc server 封装与服务注册 |
| `xllm/proto` | 20 | protobuf 协议定义 |
| `xllm/core/distributed_runtime` | 49 | Master/Engine/RemoteWorker/WorkerService 等控制面与分布式运行时 |
| `xllm/core/runtime` | 61 | Worker/WorkerImpl/Executor 等设备执行骨架 |
| `xllm/core/scheduler` | 35 | continuous batching、PD、chunked prefill、mix scheduler |
| `xllm/core/framework/request` | 57 | Request/Sequence/SequencesGroup/MM request 数据结构 |
| `xllm/core/framework/batch` | 20 | Batch 组装与结果回写 |
| `xllm/core/framework/kv_cache` | 23 | KV cache 传输、store、Mooncake 对接 |
| `xllm/core/framework/parallel_state` | 18 | ProcessGroup、Collective、DP/TP/EP 状态 |
| `xllm/core/framework/tokenizer` | 15 | tokenizers、fast tokenizer、sentencepiece、rec tokenizer |
| `xllm/core/framework/xtensor` | 26 | xTensor 页池、映射、全局权重与分布式服务 |
| `xllm/models` | 68 | LLM/VLM/DiT/Rec 模型族实现 |
| `xllm/core/layers` | 238 | 通用层与多硬件特化层 |
| `xllm/core/kernels` | 129 | 底层 kernel/ops 适配 |
| `xllm/core/platform` | 19 | 设备、stream、VMM、NUMA 等平台封装 |
| `xllm/processors` | 14 | 多模态输入预处理 |
| `xllm/function_call` | 36 | tool-call detector/parser |
| `xllm/parser` | 7 | reasoning detector/parser |
| `xllm/pybind` | 16 | Python 接口绑定 |
| `xllm/c_api` | 14 | C API |
| `xllm/cc_api` | 13 | C++ 动态库 API |

#### 2.2 xLLM 的主干调用链

当前更完整的主线可以写成：

```text
CLI / Python / C API / C++ API / HTTP / RPC
  -> xllm.cpp / pybind / api_service
  -> Master
  -> Scheduler
  -> Batch
  -> Engine
  -> WorkerClient
  -> Worker
  -> WorkerImpl
  -> Executor
  -> CausalLM / CausalVLM / DiTModel / RecModel
  -> layers / kernels / platform
```

而缓存/分布式旁路能力横切插入在：

```text
Scheduler <-> KVCacheManager / PrefixCache / XService
Engine <-> DistManager / RemoteWorker / WorkerService
WorkerImpl <-> KVCacheTransfer / Mooncake / XTensor / RollingLoad
```

### 3. 文档层地图

#### 3.1 `docs/zh/features` 能力地图

`docs/zh/features` 已经把仓库的主要能力主题列得很清楚，当前可以按以下主题理解：

- 调度类
  - `continuous_scheduler.md`
  - `chunked_scheduler.md`
  - `zero_evict_scheduler.md`
  - `async_schedule.md`
- 分离/缓存类
  - `disagg_pd.md`
  - `global_kvcache.md`
  - `prefix_cache.md`
- 执行/平台类
  - `graph_mode.md`
  - `xtensor_memory.md`
  - `multi_streams.md`
- 算法/模型类
  - `mtp.md`
  - `eplb.md`
  - `multimodal.md`
  - `moe_params.md`
- 核心基础类
  - `overview.md`
  - `basics.md`
  - `topk_topP.md`
  - `groupgemm.md`
  - `ppmatmul.md`
- 服务层类
  - `xllm_service_overview.md`

这说明文档已经隐含了一条很清晰的阅读顺序：

1. `overview/basics`
2. scheduler 与 PD/KV
3. graph/xtensor/platform
4. speculative/MoE/multimodal
5. service overview

#### 3.2 `docs/zh/getting_started` 的角色

这一组文档更多回答“如何部署与运行”：

- `quick_start.md`
- `launch_xllm.md`
- `online_service.md`
- `offline_service.md`
- `disagg_pd.md`
- `multi_machine.md`

对学习源码来说，这些文档的价值不在实现细节，而在于反推出：

- 系统有哪些标准启动姿势；
- 哪些模式是官方重点支持的；
- 哪些 CLI 组合是真正有生产语义的。

### 4. 服务与接口层全景

#### 4.1 `xllm/server`

这个目录很小，但非常关键：

- `xllm_server.h/.cpp`
  - 真正封装 brpc `Server`
  - 负责挂接 `APIService`、`WorkerService`、`DisaggPDService`、`XTensorDistService`
- `xllm_server_registry.*`
  - 只是持有 server 实例的注册表
- `health_reporter.h`
  - 暴露健康检查

关键认识：

- `server/` 不负责业务逻辑；
- `server/` 负责“把已有业务对象挂到 brpc 服务进程里”。

#### 4.2 `xllm/api_service`

这是对外协议适配最密集的目录，按功能可以分成几类：

1. 顶层入口
   - `api_service.h/.cpp`
   - `api_service_impl.h`
2. OpenAI/Anthropic 能力实现
   - `completion_service_impl.*`
   - `chat_service_impl.*`
   - `embedding_service_impl.*`
   - `image_generation_service_impl.*`
   - `rerank_service_impl.*`
   - `anthropic_service_impl.*`
   - `models_service_impl.*`
   - `sample_service_impl.*`
3. 请求/响应包装
   - `call.*`
   - `stream_call.h`
   - `non_stream_call.h`
   - `stream_output_parser.*`
4. 协议辅助
   - `chat_json_utils.h`
   - `mm_service_utils.h`
   - `embedding_output_builder.*`
5. 测试
   - `openai_service_test.cpp`
   - `anthropic_service_test.cpp`
   - `sample_service_impl_test.cpp`

这一层的边界是：

- 它只把 HTTP/RPC/JSON/Proto 请求翻译成 `Master` 调用；
- 它不决定 batch，不决定 worker，不直接执行模型。

#### 4.3 `xllm/proto`

`proto/` 基本定义了所有“边界对象”：

- 业务请求协议
  - `completion.proto`
  - `chat.proto`
  - `embedding.proto`
  - `image_generation.proto`
  - `rerank.proto`
  - `rec.proto`
  - `sample.proto`
- 公共对象
  - `common.proto`
  - `tensor.proto`
  - `multimodal.proto`
  - `embedding_data.proto`
- 运行时/服务间通信
  - `worker.proto`
  - `xllm_service.proto`
  - `xservice.proto`
  - `disagg_pd.proto`
  - `xtensor_dist.proto`
  - `mooncake_transfer_engine.proto`

因此，`proto/` 本质上定义了三种边界：

1. 用户请求边界
2. 服务层与 Master 的边界
3. Engine 与远端 Worker/PD/XTensor 的边界

### 5. 控制面与执行面全景

#### 5.1 `xllm/core/distributed_runtime`

这个目录是 xLLM 的控制面中枢，主要可拆成：

1. 主控与执行入口
   - `master.*`
   - `llm_master.*`
   - `vlm_master.*`
   - `dit_master.*`
   - `rec_master.*`
   - `engine.h`
   - `llm_engine.*`
   - `vlm_engine.*`
   - `dit_engine.*`
   - `rec_engine.*`
   - `speculative_engine.*`
2. 多机/远端 worker 基础设施
   - `dist_manager.*`
   - `remote_worker.*`
   - `worker_service.*`
   - `worker_server.*`
   - `comm_channel.*`
   - `shm_channel.*`
3. PD 与扩展服务
   - `disagg_pd_service.*`
   - `disagg_pd_service_impl.*`
   - `pd_ooc_service.*`
   - `pd_ooc_service_impl.*`
4. spawn worker 机制
   - `spawn_worker_server/*`
5. collective 相关
   - `collective_service.*`

当前已经可以确认的分层关系是：

- `Master`：控制中枢与 Engine 装配者
- `Engine`：批执行调度结果的统一执行器
- `DistManager`：多机/多进程 worker 拓扑建立者
- `WorkerService/RemoteWorker`：远端调用闭环的两端

#### 5.2 `xllm/core/runtime`

这个目录是设备执行骨架层，文件多但结构其实很规整：

1. 抽象与门面
   - `worker.h/.cpp`
   - `worker_impl.h/.cpp`
   - `worker_client.h/.cpp`
   - `executor.h/.cpp`
   - `executor_impl.h`
   - `executor_impl_factory.*`
2. 各 worker 实现
   - `llm_worker_impl.*`
   - `vlm_worker_impl.*`
   - `embed_worker_impl.*`
   - `embed_vlm_worker_impl.*`
   - `mm_embed_vlm_worker_impl.*`
   - `rec_worker_impl.*`
   - `mtp_worker_impl.*`
   - `suffix_worker_impl.*`
   - `speculative_worker_impl.*`
   - `eagle3_worker_impl.*`
3. 图执行实现
   - `acl_graph_executor_impl.*`
   - `cuda_graph_executor_impl.*`
   - `mlu_graph_executor_impl.*`
4. 共享内存与辅助工具
   - `forward_shared_memory_manager.*`
   - `spec_input_builder.*`
   - `params_utils.*`
   - `xservice_client.*`

这里可以把职责记成一句话：

- `Worker` 是门面；
- `WorkerImpl` 是设备执行骨架；
- `Executor` 是 batch 输入到 model forward 的桥梁；
- graph executor 是平台特化执行器；
- `WorkerClient` 是本地/远端统一调用抽象。

#### 5.3 `xllm/core/scheduler`

这个目录是吞吐、时延、抢占和 PD 行为的核心：

- `scheduler.h`
  - 抽象接口
- `continuous_scheduler.*`
  - 主路径调度器
- `chunked_prefill_scheduler.*`
  - 大 prompt chunk 化策略
- `disagg_pd_scheduler.*`
  - PD 分离调度
- `mix_scheduler.*`
  - prefill/decode 统一处理策略
- `zero_eviction_scheduler.*`
  - zero evict 场景
- `prefill_only_scheduler.*`
  - prefill-only 场景
- `pd_ooc_scheduler.*`
  - 在线/离线共置
- `dit_scheduler.*`
  - DiT 专用调度器
- `async_response_processor.*`
  - 异步响应回写
- `profile/*`
  - 调度性能预测、latency profile

从 `continuous_scheduler.cpp` 可以直接看到它维护的核心集合：

- `request_queue_`
- `waiting_priority_queue_`
- `waiting_priority_queue_offline_`
- `running_queue_`
- `running_queue_offline_`
- `running_requests_`
- `running_sequences_`
- `preemptable_requests_`

这说明它不仅是“组 batch”，更是一个在线资源管理器。

### 6. framework 层全景

#### 6.1 `request` + `batch`

`framework/request` 与 `framework/batch` 是内部数据流最关键的两块：

- `Request`
  - 请求生命周期、SLO、优先级、cancel、输出生成
- `Sequence`
  - token 级生成单元
- `SequencesGroup`
  - beam/multi-sequence 组织
- `Batch`
  - request/sequence -> forward input 的桥
  - forward output -> request/sequence 回写的桥

当前一个很重要的认识是：

- scheduler 实际调度粒度正在 request 与 sequence 两层之间切换；
- `Batch` 则是把这种调度决策压成一次执行输入。

#### 6.2 `model_loader` + `model_context` + `parallel_state`

- `ModelLoader`
  - 负责从模型目录构造模型参数、量化参数、tokenizer 参数和 state dict
  - 当前主实现是 HuggingFace 风格模型加载器
- `ModelContext`
  - 组合 `ModelArgs`、`QuantArgs`、`ParallelArgs`、`TensorOptions`
  - 进一步派生内部 `OptimizationConfig`
- `ParallelArgs`
  - 描述 DP/TP/EP/SP 等通信拓扑与 process group 指针

这三者分别对应：

1. 模型文件世界
2. 模型运行上下文世界
3. 分布式执行拓扑世界

#### 6.3 `tokenizer` / `sampling` / `prefix_cache` / `kv_cache` / `xtensor`

这些目录共同构成“主链横切能力”：

- `tokenizer`
  - fast tokenizer、sentencepiece、rec tokenizer、Rust tokenizers 桥接
- `sampling`
  - sampler、beam search、rejection sampler、constrained decoding
- `prefix_cache`
  - prefix cache 与 upload 变体
- `kv_cache`
  - KV cache 对象、传输、Mooncake 集成、store、spec cache transfer
- `xtensor`
  - 物理页池、逻辑页映射、全局权重、分布式服务

因此，framework 不只是“数据结构层”，而是把一批模型无关但执行相关的核心能力集中起来。

### 7. 模型、层、kernel、platform 四层关系

#### 7.1 `xllm/models`

模型族目前至少覆盖四大类：

- `models/llm`
  - `qwen2/qwen3/qwen3_moe`
  - `deepseek_v2/v3/v32`
  - `glm5/glm5_mtp`
  - `joyai_llm_flash`
  - 对应还有 `npu/` 特化版本与 `musa/` 版本
- `models/vlm`
  - `qwen2_vl/qwen2_5_vl/qwen3_vl/qwen3_vl_moe`
  - 以及 embedding/mm embedding 变体
  - `npu/` 下还有 `glm4v`、`MiniCPMV`、`oxygen_vlm`
- `models/dit`
  - `pipeline_flux*`
  - `pipeline_longcat_image*`
  - `transformer_*`
  - `autoencoder_kl`
  - `clip_text_model`
  - `t5_encoder`
- `models/rec`
  - `onerec`
  - `rec_model_base`

这说明 xLLM 的定位已经不是“单纯的 CausalLM 推理器”，而是统一多任务推理框架。

#### 7.2 `xllm/core/layers`

`layers/` 比 `models/` 更靠近“网络组件实现”：

- `common/`
  - attention、rms norm、rotary embedding、dense mlp、fused moe、lm head 等通用层
- `npu/`
  - 最丰富的平台特化实现，包括各模型 decoder/vision encoder/lm head/block copy 等
- `cuda/`
  - attention、flashinfer、xattention 等
- `mlu/`、`ilu/`、`musa/`
  - 各平台特化层实现
- `npu_torch/`
  - NPU torch 路径下的特殊实现

层级关系可以理解为：

```text
models/*
  组合并实例化
layers/common/*
  提供跨平台层抽象
layers/{npu,cuda,mlu,...}/*
  提供硬件特化层实现
```

#### 7.3 `xllm/core/kernels`

`kernels/` 进一步比 `layers/` 更底层，接近算子 API：

- `cuda/`
  - 大量 `.cu`、attention runner、piecewise graphs、xattention、quant/matmul/norm
- `npu/`
  - attention、matmul、rope、grouped matmul、moe routing 等
- `mlu/`
  - attention、group_gemm、moe、topk/random sample
- `ilu/`、`musa/`
  - 对应平台 kernel
- `ops_api.*`
  - 上层统一 ops API

简化理解：

- `models` 决定“搭什么网络”
- `layers` 决定“这一层怎么实现”
- `kernels` 决定“底层算子怎么跑”
- `platform` 决定“设备、stream、内存、VMM 怎么管理”

#### 7.4 `xllm/core/platform`

这个目录主要处理平台通用运行时：

- `device.*`
- `stream.*`
- `shared_vmm_allocator.*`
- `vmm_api.*`
- `numa_utils.*`
- `npu/` 与 `cuda/` 平台小型特化目录

它是“底层运行环境抽象层”，不是模型逻辑层。

### 8. 语言绑定与用户侧接口

#### 8.1 `pybind`

`pybind/bind.cpp` 暴露的对象包括：

- `Options`
- `LLMMaster`
- `VLMMaster`
- `RequestParams`
- `RequestOutput`
- 多种输出和 logprob 结构
- `get_model_backend` 等辅助函数

再加上：

- `pybind/llm.py`
- `pybind/vlm.py`
- `pybind/embedding.py`
- `pybind/params.py`
- `pybind/args.py`

用户实际上更多通过 Python 高层封装使用，而不是直接接触 C++ `Master`。

#### 8.2 `c_api` / `cc_api`

这两层主要服务于“动态库 + 外部程序嵌入”：

- `c_api`
  - C 风格头文件与示例
  - 更底层、更 ABI 稳定导向
- `cc_api`
  - C++ 风格动态库接口
  - 支持单实例和多实例样例

这说明 xLLM 除了服务化部署，还支持“作为推理库被宿主程序嵌入”。

#### 8.3 `launch_xllm.py` 与 `examples/`

- `launch_xllm.py`
  - Python 层对本地 `xllm` 二进制的包装启动器
- `examples/`
  - `generate.py`
  - `generate_embedding.py`
  - `generate_vlm.py`
  - `generate_beam_search.py`
  - `sample.py`

这些例子对应的是用户视角，而不是框架内部视角，很适合做“最小验证闭环”。

### 9. function call、reasoning、processor 三条辅助主线

#### 9.1 `function_call`

这个目录实现了不同模型族的 tool-call 输出检测和解析：

- `qwen25/qwen3/qwen3_coder`
- `deepseekv3/deepseekv32`
- `glm45/glm47`
- `kimik2`

这说明 xLLM 把工具调用格式差异显式建模成 parser/detector，而不是散落在服务层里。

#### 9.2 `parser`

这个目录是 reasoning parser，对应：

- detector registry
- reasoning detector
- reasoning parser

与 function call 一样，都是“模型输出后处理能力”。

#### 9.3 `processors`

这里主要是多模态输入预处理：

- `clip_image_processor`
- `glm4v_image_processor`
- `minicpmv_image_processor`
- `qwen2_vl_image_processor`
- `pywarpper_image_processor`

它是“输入前处理层”，位于 service/request 之前、model 之前。

### 10. 第三方依赖边界

#### 10.1 `third_party/Mooncake`

从 README 可以确认，Mooncake 是以 KV Cache 为中心的分离式 LLM serving 架构，核心是：

- Transfer Engine
- Mooncake Store
- PD 分离与 KV cache 传输能力

对 xLLM 来说，Mooncake 的意义不是一般依赖库，而是：

- 为全局 KV cache、分离式 PD、远端缓存传输提供重要技术基础。

#### 10.2 `third_party/brpc`

brpc 是工业级 C++ RPC 框架，支持多协议、同步/异步请求、内建服务和高性能 server/client。

对 xLLM 的意义很清楚：

- `XllmServer`
- `WorkerService`
- `DisaggPDService`
- `XTensorDistService`

这些服务化能力都建立在 brpc 之上。

#### 10.3 `third_party/xllm_ops`

从其 README 可以确认这是一个面向国产芯片的大模型高性能算子库，强调：

- GroupMatmul 优化
- topK/topP 融合
- 高性能算子实现

它在 xLLM 中扮演的角色是“底层高性能自定义算子仓”，不是一般 util 库。

#### 10.4 `third_party/xllm_atb_layers`

这个目录的 README 明确说明它不是独立安装包，而是作为顶层 `xllm` 项目的一部分通过 `add_subdirectory(...)` 集成。

对当前认知来说，它更像：

- NPU / ATB 相关层与 operation 适配仓。

### 11. 一份更实用的学习路径

如果目标是“最后真能讲清楚 xLLM”，建议按下面顺序继续读，而不是按目录深搜：

1. `README_zh.md` + `docs/zh/dev_guide/code_arch.md` + `docs/zh/features/overview.md`
2. `xllm/xllm.cpp` + `global_flags` + `Options`
3. `server` + `api_service` + `proto`
4. `master` + `engine` + `dist_manager` + `worker_service` + `remote_worker`
5. `scheduler` 主链
6. `request` + `sequence` + `batch`
7. `worker` + `worker_impl` + `executor`
8. `model_loader` + `model_context` + `parallel_state`
9. `models` + `layers`
10. `platform` + `kernels` + `xtensor`
11. `Mooncake` / `xllm_ops` / `xllm_atb_layers`
12. `pybind` + `c_api` + `cc_api` + `examples`

### 12. 当前全仓级认知总结

站在全仓视角，xLLM 可以概括成一句话：

> 一个围绕企业级服务化、分布式调度、设备执行骨架、多任务模型适配与国产芯片优化而构建的统一推理系统。

如果再拆细一点：

- `docs` 告诉你“系统想解决什么问题”
- `xllm.cpp` 告诉你“系统如何定型启动”
- `server/api_service/proto` 告诉你“系统如何对外说话”
- `master/engine/scheduler/runtime` 告诉你“系统如何运行”
- `framework` 告诉你“系统靠哪些通用抽象把运行时串起来”
- `models/layers/kernels/platform` 告诉你“系统如何真正把模型跑到不同硬件上”
- `third_party` 告诉你“哪些能力来自外部生态，哪些是 xLLM 自身的核心实现”

### 13. 仍待深挖但已定位的重点目录

1. `xllm/core/framework/request/*`
   - 决定 request/sequence 状态机
2. `xllm/core/framework/batch/*`
   - 决定 batch 输入与结果回写
3. `xllm/core/scheduler/*`
   - 决定 continuous batching、抢占和 PD 行为
4. `xllm/core/runtime/*`
   - 决定设备执行骨架与 graph/overlap/sleep-wakeup
5. `xllm/core/framework/xtensor/*`
   - 决定权重与缓存内存管理
6. `xllm/core/framework/kv_cache/*`
   - 决定全局 KV cache 与 Mooncake 集成
7. `xllm/models/*` + `xllm/core/layers/*`
   - 决定模型族与平台特化路径

## 二次复核结论

- 本轮结果已经把 `L02` 的三个 DoD 问题落到了源码证据上：`backend/task` 推导、配置分层、server/master/engine 创建时序都已明确。
- 需要特别防止的误读有两个：
  1. `master->run()` 不是“创建 engine”，而是“启动循环”；
  2. `APIService` 不是启动源头，而是启动后的协议适配层。
- 当前文档已经适合作为下一轮 `L03/L04` 的直接输入，但涉及分布式子服务与更深层 worker 拓扑的部分，仍要继续往下追源码。
