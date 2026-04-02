# xLLM Prefix Cache 学习笔记

## 1. 这份笔记要回答什么

基于 `ralph/learn_xllm/0-overview.md` 和 `ralph/learn_xllm/1-structure.md` 已经建立的整体框架，我这次把 `prefix cache` 当成一条独立主线认真拆开，重点回答下面这些问题：

1. `prefix cache` 在 xLLM 整体架构里到底处在什么位置。
2. 它缓存的到底是什么，是 token、prompt，还是 KV block。
3. 它的 key 是怎么生成的，为什么要按 block 粒度做链式哈希。
4. 新请求命中 cache 之后，scheduler、`KVCacheManager`、`Sequence`、`BatchInputBuilder` 分别会发生什么。
5. `match / insert / evict` 的具体时机、引用计数语义和 LRU 淘汰逻辑是什么。
6. `continuous / chunked / zero_evict / host-store / cache upload` 这些变体分别怎样使用它。

---

## 2. 结论先行

先给出我目前最核心的结论：

1. xLLM 的 `prefix cache` 不是“字符串前缀缓存”，而是“按 block 对齐的 KV block 复用索引”。
2. 它缓存的对象不是 token 文本本身，而是已经分配好的 `Block`；token 只是用来生成 block 的链式 hash key。
3. 这个 cache 的最关键边界不在 model 层，而在 `Scheduler -> KVCacheManager -> BlockManager -> PrefixCache -> Sequence/KVState` 之间。
4. 命中 prefix cache 后，并不是直接“跳过请求”，而是让 `Sequence::kv_state().kv_cache_tokens_num()` 变大，后续 `BatchInputBuilder` 只把未缓存的后缀 token 送进 forward。
5. 这个机制从实现上依赖 `Block` 的引用计数。`prefix cache` 自己持有一份 `Block`，活跃 sequence 再持有一份或多份，通过 `ref_count` 同时解决“共享”“不可淘汰”“何时回收”三个问题。
6. xLLM 当前真正稳定支持的 prefix cache 主路径，是 `ContinuousScheduler`、`ChunkedPrefillScheduler`、`ZeroEvictionScheduler` 这一系；官方文档明确要求 PD 分离场景关闭 `prefix cache`。
7. 文档里说 prefix cache 基于 `murmur_hash`，但当前源码已经改成 `XXH3 128-bit` 链式哈希，文档和实现这里已经出现偏差。

---

## 3. 阅读文件

这次为了把逻辑读透，我实际对过下面这些文件：

- `ralph/learn_xllm/0-overview.md`
- `ralph/learn_xllm/1-structure.md`
- `docs/zh/features/prefix_cache.md`
- `docs/zh/features/chunked_scheduler.md`
- `docs/zh/features/zero_evict_scheduler.md`
- `docs/zh/features/disagg_pd.md`
- `docs/zh/cli_reference.md`
- `xllm/xllm.cpp`
- `xllm/core/common/global_flags.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache.h`
- `xllm/core/framework/prefix_cache/prefix_cache.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache_with_upload.h`
- `xllm/core/framework/prefix_cache/prefix_cache_with_upload.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache_factory.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache_test.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache_benchmark.cpp`
- `xllm/core/framework/block/block.h`
- `xllm/core/framework/block/block.cpp`
- `xllm/core/framework/block/block_manager.h`
- `xllm/core/framework/block/block_manager_impl.h`
- `xllm/core/framework/block/block_manager_impl.cpp`
- `xllm/core/framework/block/block_manager_pool.h`
- `xllm/core/framework/block/block_manager_pool.cpp`
- `xllm/core/framework/block/hierarchy_block_manager_pool.cpp`
- `xllm/core/framework/block/concurrent_block_manager_impl.cpp`
- `xllm/core/framework/block/kv_cache_manager.h`
- `xllm/core/framework/request/sequence.h`
- `xllm/core/framework/request/sequence.cpp`
- `xllm/core/framework/request/sequence_kv_state.h`
- `xllm/core/framework/request/sequence_kv_state.cpp`
- `xllm/core/framework/request/request.h`
- `xllm/core/framework/request/request.cpp`
- `xllm/core/framework/request/sequences_group.cpp`
- `xllm/core/framework/batch/batch_factory.cpp`
- `xllm/core/framework/batch/batch_input_builder.cpp`
- `xllm/core/scheduler/continuous_scheduler.cpp`
- `xllm/core/scheduler/chunked_prefill_scheduler.cpp`
- `xllm/core/scheduler/zero_eviction_scheduler.cpp`
- `xllm/core/scheduler/prefill_only_scheduler.cpp`
- `xllm/core/scheduler/disagg_pd_scheduler.h`
- `xllm/core/scheduler/disagg_pd_scheduler.cpp`
- `xllm/core/scheduler/pd_ooc_scheduler.cpp`
- `xllm/core/scheduler/scheduler_factory.cpp`
- `xllm/core/distributed_runtime/llm_engine.cpp`
- `xllm/core/runtime/xservice_client.cpp`
- `xllm/core/framework/kv_cache/kv_cache_event.h`
- `xllm/core/util/hash_util.h`
- `xllm/core/util/double_buffer.h`

---

## 4. 放回整体架构里看，prefix cache 在哪里

结合前两份总览笔记，我现在把它放在下面这条边界上理解：

`Scheduler -> KVCacheManager -> BlockManagerPool -> BlockManagerImpl -> PrefixCache`

它和整体请求链的关系是：

`Request/Sequence`
-> scheduler 决定这一轮谁要跑
-> `KVCacheManager` 尝试为 sequence 复用 shared blocks 或分配新 blocks
-> `BatchInputBuilder` 根据 `kv_cache_tokens_num()` 只构造“未命中的那段 token”的 forward 输入
-> worker 执行后，sequence 的 KV 前缀继续增长
-> 某些时机再把这些 full blocks 回写进 `PrefixCache`

所以它不是一个“旁路优化”，而是直接改写了：

1. sequence 当前认为自己已经有多少 KV。
2. 当前 step 还需要 forward 多少 token。
3. 显存 block 的使用量、共享量和可淘汰量。
4. scheduler 的 token budget、memory budget 和 preemption 行为。

这也是为什么在 `0-overview.md` 里，`prefix_cache` 和 `kv_cache` 都被放在 framework 的“主链横切能力”里，而不是模型层目录里。

---

## 5. 启用条件、开关和限制

### 5.1 核心开关

直接相关的 gflags 在 `xllm/core/common/global_flags.cpp` 里有这些：

- `enable_prefix_cache`
- `enable_cache_upload`
- `xxh3_128bits_seed`
- `enable_chunked_prefill`
- `chunked_match_frequency`
- `use_zero_evict`
- `max_decode_token_per_sequence`
- `prefill_scheduling_memory_usage_threshold`
- `enable_kvcache_store`

其中最核心的是：

- `enable_prefix_cache=true/false`
  - 总开关。
- `enable_cache_upload`
  - 不是本地命中逻辑本身，而是“把 cache 事件上传给 xservice”的扩展能力。
- `enable_kvcache_store`
  - 不是基础 prefix cache，而是 host/store 侧分层缓存扩展。

### 5.2 启动阶段会进一步改写这些开关

`xllm/xllm.cpp` 里有几个很重要的归一化逻辑：

1. `backend == "vlm"` 时，强制：
   - `FLAGS_enable_prefix_cache = false`
   - `FLAGS_enable_chunked_prefill = false`

2. `enable_cache_upload` 不会直接照抄 flag，而是要求：
   - `(enable_service_routing || enable_disagg_pd) && enable_prefix_cache && enable_cache_upload`

3. `enable_kvcache_store` 也不是直接照抄 flag，而是要求：
   - `enable_kvcache_store && enable_prefix_cache && (host_blocks_factor > 1.0)`

这说明 prefix cache 相关能力在启动时已经被分成三层：

1. 基础本地 prefix cache
2. service routing/xservice 需要的 cache upload
3. host/store 分层缓存

### 5.3 Engine 初始化时还有一层限制

`xllm/core/distributed_runtime/llm_engine.cpp` 初始化 `BlockManagerPool::Options` 时又做了一次约束：

- 如果 `FLAGS_enable_xtensor` 为真，则把 `enable_prefix_cache` 强制改成 `false`

也就是说：

- `xtensor` 路径当前不走常规 prefix cache。

### 5.4 Scheduler 支持矩阵

从文档和工厂函数综合看：

- `ContinuousScheduler`
  - 支持，属于主路径。
- `ChunkedPrefillScheduler`
  - 支持，而且专门做了多阶段 rematch。
- `ZeroEvictionScheduler`
  - 支持，而且会在准入模拟里主动考虑 prefix cache。
- `PrefillOnlyScheduler`
  - 继承了 continuous 的 prefix cache 分配思路。
- `DisaggPDScheduler / PDOOCScheduler`
  - 代码里保留了一些 prefix 相关接口和注释，但官方文档 `docs/zh/features/disagg_pd.md` 明确要求把 `enable_prefix_cache=false`。

我的判断是：

- 生产/稳定主路径应以“PD 分离不支持 prefix cache”为准。
- 代码里仍存在部分 prefix-cache 钩子，说明这块可能有过尝试或后续演进空间，但我不把它当成当前稳定结论。

---

## 6. prefix cache 到底缓存了什么

### 6.1 不是缓存 token，而是缓存 `Block`

从 `PrefixCache::Node` 的定义可以直接看到，cache 里真正存的是：

- `Block block`
- `last_access_time`
- LRU 双向链表指针

也就是说，prefix cache 本体保存的是“某个 token 前缀对应的 KV block 句柄”。

token 的作用只有两个：

1. 把“某段前缀 token”映射成稳定 key。
2. 在命中时证明“这个 block 对应的前缀就是当前 sequence 需要的前缀”。

### 6.2 粒度是 full block，不是任意长度 token

这个实现始终按 block 边界对齐：

- `match()` 会先 `round_down(token_ids.size(), block_size_)`
- `insert()` 只处理 `token_ids.size() / block_size_` 个 full blocks
- `compute_hash_keys()` 也是按 block 粒度推进

这意味着：

1. 不能命中不足一个 block 的尾巴。
2. 尾部不满 block 的 prompt/token 必须继续算。
3. prefix cache 命中的最小单位不是 1 token，而是 1 block。

这和 KV cache 的物理布局完全一致，所以它本质上是“物理 block 级复用”。

### 6.3 它缓存的不一定只是不变的 prompt

`BlockManagerPool::cache(Sequence* sequence)` 传入的是：

- `sequence->cached_tokens()`

而 `cached_tokens()` 的定义是：

- 从序列开头到 `kv_cache_tokens_num()`

所以只要某一段 token 已经进入 KV cache 且达到 full-block 边界，这一段都可能被插入 prefix cache。

因此更准确的说法是：

- xLLM 复用的是“已写入 KV 的前缀 block”。

prompt 只是最常见的一种前缀，不是唯一前缀。

---

## 7. 哈希设计：为什么是链式 XXH3

### 7.1 当前实现已经是 XXH3，不是 MurmurHash

`docs/zh/features/prefix_cache.md` 里写的是：

- prefix cache 基于 `mermer_hash`

但源码 `xllm/core/framework/prefix_cache/prefix_cache.cpp` 已经明确使用：

- `XXH3_128bits_withSeed`

而且 `prefix_cache_benchmark.cpp` 还专门保留了：

- MurmurHash3_x64_128
- XXH3_128bits_withSeed
- 两者的 chain-hash 对比 benchmark

所以这部分我认为应该以源码为准：

- 当前实现是 `XXH3 128-bit` 链式哈希。

### 7.2 不是独立块 hash，而是“前缀链式 hash”

哈希规则非常关键：

第一个 block：

`H0 = XXH3(block_0_tokens)`

之后每个 block：

`Hi = XXH3(Hi-1 || block_i_tokens)`

这在代码里就是 `xxh3_128bits_hash(pre_hash_value, token_ids_slice, hash_value)`。

它的含义是：

1. 同一个当前 block，如果前面前缀不同，最终 hash 也不同。
2. 所以 cache key 表示的是“从开头到当前 block 的完整前缀”，不是“当前 block 内容本身”。
3. 这样不用显式维护树结构，只用一个 hash map 就能做最长前缀匹配。

### 7.3 `Block` 自身会记住这个链式 hash

`Block` 里有一个 `hash_value_` 字段，并且注释已经写明：

- `hash_value_ = Hash(current_block, Hash(pre_block))`

所以 block 一旦被 `insert()` 处理过，它自己就携带了“当前前缀链位置”的 hash 值。

这有两个直接好处：

1. 下次 rematch 时，已有 shared blocks 的最后一个 block 可以直接提供前缀 hash，不需要从头重算。
2. host/store/offload 路径可以只传 block，不必总带完整 token 序列。

---

## 8. 核心数据结构和职责

### 8.1 `Block`

`xllm/core/framework/block/block.h`

`Block` 是 prefix cache 能工作的根基，它同时承担三件事：

1. 标识物理 block
   - `id_`
   - `size_`

2. 维护生命周期
   - `ref_count_`
   - `manager_`

3. 维护 prefix cache key
   - `hash_value_`

最重要的点是：

- `Block` 拷贝时会 `inc_ref_count()`
- `Block` 析构时会 `dec_ref_count()`
- 当 `ref_count` 变成 0，`manager_->free(id_)` 会把 block id 还回 allocator

也就是说，prefix cache 不需要单独保存“内存释放回调”：

- 它只要删除自己的 `Node.block`
- `Block` 析构就会自动把底层 block 还给 `BlockManager`

### 8.2 `PrefixCache`

`xllm/core/framework/prefix_cache/prefix_cache.h`

`PrefixCache` 的核心状态只有两份：

1. `cached_blocks_`
   - `unordered_map<XXH3Key, Node*>`
   - 负责按 hash O(1) 查找 block

2. `lru_lst_`
   - 双向链表
   - 负责按访问顺序维护淘汰队列

`Node` 里保存：

- `Block block`
- `last_access_time`
- `prev/next`

所以这个实现本质上就是：

- hash map 做索引
- LRU list 做淘汰
- `Block.ref_count` 做共享与回收

### 8.3 `KVCacheState`

`xllm/core/framework/request/sequence_kv_state.*`

这是 prefix cache 和 sequence 状态真正接上的地方，最关键的字段有三个：

1. `kv_cache_tokens_num_`
   - 当前已经有 KV 的 token 数

2. `blocks_`
   - 当前 sequence 持有的 blocks

3. `num_owned_shared_blocks_`
   - 当前 sequence 前面有多少个 blocks 来自 prefix cache 共享

它不是单纯“记录一下命中了几个 block”，而是直接定义：

- sequence 之后 forward 从哪里开始算
- 哪些 blocks 是共享 prefix
- 哪些 blocks 是自己新分配的

### 8.4 `BlockManagerImpl` / `BlockManagerPool`

`PrefixCache` 自己不分配物理 block，它只是索引。

真正把 prefix cache 变成“可调度资源”的，是：

- `BlockManagerImpl`
- `BlockManagerPool`
- `HierarchyBlockManagerPool`

它们负责：

1. 什么时候先尝试 `match`
2. 什么时候为未命中的后缀分配新 blocks
3. 什么时候把 sequence 当前的 full blocks 插回 prefix cache
4. 什么时候从 prefix cache 淘汰 idle blocks

所以更准确的说：

- `PrefixCache` 只负责“索引和淘汰”
- `BlockManager*` 负责“把索引和真实内存记账/分配粘起来”

---

## 9. 一次命中的完整生命周期

下面按真实代码主链走一遍。

### 9.1 新请求进入 scheduler

请求进入 `ContinuousScheduler::prepare_batch()` 后，会先进入 waiting queue。

如果 prefix cache 关闭：

- scheduler 会立刻 `request->expand_sequences(false)`，把 `best_of/n` 需要的 sequence 全展开。

如果 prefix cache 开启：

- scheduler 会暂缓这件事，等第一条 sequence 的 prompt KV 真的建好后，再让后续 sequence 通过 prefix cache 共享前缀。

这就是 prefix cache 不只是“节省显存”，还直接改写 request 生命周期的一个例子。

### 9.2 prefill 之前，先尝试 `allocate_shared`

对于新 sequence，`BlockManagerPool::allocate(sequence, num_tokens)` 第一件事不是分配新 blocks，而是：

- 如果 `sequence->kv_state().num_kv_blocks() == 0`
- 先 `allocate_shared(sequence)`

而 `allocate_shared(sequence)` 又会走到：

- `block_managers_[dp_rank]->allocate_shared(sequence->tokens(), existed_shared_blocks)`

最后落到：

- `BlockManagerImpl::allocate_shared()`
- `PrefixCache::match()`

### 9.3 `match()` 命中后，sequence 先拿到 shared blocks

`PrefixCache::match()` 会：

1. 先把 token 数对齐到 full block。
2. 以 `existed_shared_blocks` 的最后一个 block hash 作为链式 hash 起点。
3. 逐 block 计算链式 hash。
4. 在 `cached_blocks_` 里查找。
5. 最长前缀连续命中就继续；一旦中断就停止。

返回的是：

- `std::vector<Block> matched_blocks`

注意这里返回的不是 block id，而是 `Block` 对象拷贝，所以 `ref_count` 会增加。

### 9.4 `Sequence` 不只是“记录命中结果”，而是直接改写自己的 KV 状态

`Sequence::add_shared_kv_blocks()` 最终进入：

- `KVCacheState::add_shared_kv_blocks()`

这里有三个很关键的语义：

#### 9.4.1 普通命中

如果之前完全没 blocks：

- `blocks_ = matched_blocks`
- `num_owned_shared_blocks_ = matched_blocks.size()`
- `kv_cache_tokens_num_ = matched_blocks.size() * block_size`

这意味着：

- sequence 从逻辑上已经“拥有”这段 KV 前缀了。

#### 9.4.2 chunked rematch 时，允许把自己之前算出来的 unique blocks 替换成 shared blocks

如果当前 sequence 已经有一些 blocks，但新一轮 `allocate_shared` 又匹配到了更多共享块：

- `try_replace_unique_blocks(...)`

会把前面的 owned blocks 替换成 shared blocks，并释放掉自己原先那份 block 引用。

这一步非常关键，因为 chunked prefill 不是“一次匹配定终身”：

- 它允许 sequence 在后续 chunk 里重新发现“原来这段 prefix 现在也能共享了”，然后回收自己原先占的 block。

#### 9.4.3 完全相同 prompt 的特殊回退

如果 `num_shared_tokens == current_total_num_tokens`，也就是：

- 整个 prompt 精确命中到了最后一个 full block

代码不会直接把 `kv_cache_tokens_num_` 设成“整个 prompt 都缓存好了”，而是会：

1. 把 shared token 数回退到前一个 block 边界
2. `num_owned_shared_blocks_--`
3. `blocks_.pop_back()`

源码注释解释得很清楚：

- 如果完全一样的 prompt 再来一次，需要把 KV 位置回退到上一个 block，给模型留出继续前进的空间。

我的理解是：

- 如果一点都不重算，某些后续执行路径会没有“当前 step 需要 forward 的 token”，无法自然衔接生成。
- 所以实现上故意保守一格，宁可少复用最后一个 full block，也要保持执行语义稳定。

这个细节非常重要，也很容易被忽略。

### 9.5 未命中的部分再分配新 blocks

共享块就位之后，`BlockManagerPool::allocate()` 才会计算：

- 目标总 token 需要多少个 blocks
- 当前 shared/owned blocks 已经覆盖了多少
- 还差多少新 blocks

然后再调用底层 `BlockManagerImpl::allocate(num_additional_blocks)`。

所以 prefix cache 并不是“命中了就不分配了”，而是：

- 先拿共享前缀
- 再只为 suffix 分配新增 blocks

### 9.6 BatchInputBuilder 只会构造“未命中的后缀”

真正让“少算了一段 prompt”的，不是 scheduler，而是：

- `BatchInputBuilder::process_single_sequence()`

它会读取：

- `n_kv_cache_tokens = sequence->kv_state().kv_cache_tokens_num()`

然后计算：

- `q_seq_len = n_tokens - n_kv_cache_tokens`

之后只把 `[n_kv_cache_tokens, seq_len)` 这一段 token 放进本轮 forward 输入。

这就是 prefix cache 命中如何真正转化成：

1. 少算 prefill token
2. TTFT 下降
3. prompt 处理开销下降

的核心落点。

---

## 10. `match / insert / evict` 具体是怎么写的

### 10.1 `match()`

`PrefixCache::match()` 的逻辑可以概括成：

1. 先对齐 full block。
2. 把已有 shared blocks 直接塞到返回结果开头。
3. 从 `existed_shared_blocks` 最后一个 block 的 hash 继续往后算。
4. 每个 block 算一次链式 hash 去 map 查。
5. 连续命中就继续，不连续立即停。
6. 所有命中的节点从 LRU 原位置移走，最后统一放回 LRU 尾部，表示“最近访问过”。

两个重要细节：

### 10.1.1 最长连续前缀匹配

它不是“把所有能命中的块都找出来”，而是：

- 从前往后连续匹配
- 中断即停止

所以它严格保证：

- 命中的 shared blocks 对应的是一个合法前缀。

### 10.1.2 支持增量 rematch

`match()` 的第二个参数是：

- `existed_shared_blocks`

这意味着对于 chunked prefill 或已经有部分 shared blocks 的 sequence：

- 不用从第 0 个 block 重新 hash 到当前位置
- 可以直接从最后一个共享块的 hash 值继续往后匹配

这就是它能支持“多阶段 chunked prefill 匹配”的根本原因。

### 10.2 `insert(token_ids, blocks, existed_shared_blocks_num)`

这个版本用于：

- sequence 还拿着 token 序列时，把 full blocks 按 token 前缀插入 cache

它会：

1. 按 block 对齐。
2. 从 `existed_shared_blocks_num` 之后开始处理。
3. 用链式 hash 给每个新 block 写入 `block.hash_value_`。
4. 如果 key 已存在，只更新已有 node 的 LRU 位置。
5. 如果 key 不存在，创建新 `Node(block)` 放进 map 和 LRU。

这里有一个很关键的设计：

- `Block` 自身会被写入 hash 值

于是后面 host/offload 路径就可以直接拿 blocks 走 `insert(blocks)`，不必每次再拿 token 重算。

### 10.3 `insert(blocks)`

这个版本要求：

- block 自己已经有 hash 值

典型使用场景：

1. host blocks 经过 `compute_hash_keys()` 之后再插入
2. D2H offload 后把 host blocks 直接插入 host prefix cache
3. prefetch from storage 成功后，把 host blocks 的 hash 直接写入 host cache

### 10.4 `evict(n_blocks)`

`evict()` 从 LRU 头部开始扫，也就是最久未使用节点优先。

但它不会无脑删，而是会跳过：

- `block.is_shared() == true`

因为 `is_shared()` 本质上是：

- `ref_count > 1`

而 prefix cache 自己始终持有一份 `Block`，所以：

- `ref_count == 1` 说明只有 cache 自己持有，当前没有活跃 sequence 在用，可以淘汰。
- `ref_count > 1` 说明至少还有一个 sequence 也在引用，不能淘汰。

一旦删除 node：

1. `cached_blocks_.erase(key)`
2. `delete del_node`
3. `Node.block` 析构
4. `Block::dec_ref_count()`
5. 如果 refcount 变成 0，`manager_->free(id_)`

所以 evict 的真正释放动作最终还是通过 `Block` 析构回到 allocator。

---

## 11. 引用计数和显存记账是怎么配合的

这是我这次阅读里觉得最核心、也最值得反复确认的一层。

### 11.1 为什么光有 `free_blocks_` 不够

如果没有 prefix cache，block 的使用量很好算：

- 分出去多少
- 还剩多少

但有了 prefix cache 以后，同一个物理 block 可能同时被：

1. prefix cache 持有一份
2. 一个 sequence 持有一份
3. 多个 sequence 共享持有多份

这时“free block 数”和“当前活跃请求真正占用的 block 数”就不再是一回事。

所以 `BlockManagerImpl` 单独维护了：

- `num_free_blocks_`
- `num_used_blocks_`

其中：

- `num_free_blocks_` 是 allocator 层面的空闲物理 block 数
- `num_used_blocks_` 是“当前有活跃 sequence 依赖的有效 block 数”

### 11.2 `allocate_shared()` 为什么只在 `ref_count <= 2` 时增加 `num_used_blocks_`

`BlockManagerImpl::allocate_shared()` 命中 prefix cache 后，会遍历返回的 shared blocks：

- 如果 `block.ref_count() <= 2`
- 则 `num_used_blocks_++`

这里的精髓在于：

1. idle cached block 原本只有 prefix cache 自己一份引用
   - 命中后变成“cache + 当前 sequence”
   - `ref_count` 通常是 2
   - 说明这块现在重新变成“活跃使用中”，应该记入 `num_used_blocks_`

2. 如果 block 原本已经被别的活跃 sequence 共享使用
   - 命中后 `ref_count` 会大于 2
   - 这并没有新增物理 block 占用
   - 所以不应该再增加 `num_used_blocks_`

### 11.3 `deallocate()` 为什么只在 `ref_count <= 2` 时减少 `num_used_blocks_`

`BlockManagerImpl::deallocate()` 在 prefix cache 开启时，不是简单按 block 数减，而是逐个判断：

- 如果 `block.ref_count() <= 2`
- 才 `num_used_blocks_--`

含义是：

1. 如果当前 sequence 释放的是“最后一个活跃引用”
   - 释放后只剩 prefix cache 那份
   - 这块物理 block 不再被活跃请求占用
   - 应该从 `num_used_blocks_` 里减掉

2. 如果释放后还有其他 sequence 在共享它
   - 这块仍然是“活跃使用中”
   - 不能减

### 11.4 我用一张表总结这层语义

| 场景 | 典型 ref_count | 物理 block 状态 | `num_used_blocks_` 应该怎么变 |
|---|---:|---|---|
| 新分配给一个 sequence，尚未进 cache | 1 | 活跃使用中 | 分配时增加 |
| sequence 插入 prefix cache 后，cache 和该 sequence 各持一份 | 2 | 活跃使用中 | 不额外增加 |
| sequence 结束，只剩 prefix cache 自己持有 | 1 | 空闲但可复用、也可淘汰 | deallocate 时减少 |
| 新 sequence 命中一个 idle cached block | 2 | 从 idle 重新变成活跃 | allocate_shared 时增加 |
| 多个活跃 sequence 共享同一 cached block | 3 及以上 | 活跃共享 | 后续命中不再增加 |
| LRU 淘汰一个仅 cache 自己持有的 block | 1 -> 0 | 真正释放回 allocator | `free_blocks_` 增加 |

这个记账模型是 prefix cache 能和 scheduler 的内存阈值、preemption、zero-evict 判断对上的关键。

---

## 12. prefix cache 是在什么时机被写回去的

这部分如果不仔细看 scheduler，很容易误以为“prefill 一结束就立刻写回”。

实际上写回有几个不同时机。

### 12.1 decode 路径里，第一次生成后触发一次“prefill cache”

`ContinuousScheduler::handle_decode_requests()` 在成功为 decode sequence 分配 block 后，会做：

- `if (sequence->if_cache_block_for_prefill()) { kv_cache_manager_->cache(sequence); }`

而 `if_cache_block_for_prefill()` 的触发条件是：

- 之前没缓存过
- 且 `num_tokens() > num_prompt_tokens()`

也就是：

- sequence 已经开始真正生成 token 之后，只触发一次

这一步的实际效果是：

- 把该 sequence 当前已经成为 full-block 的 cached prefix 插入 prefix cache

所以从逻辑上，它对应“prefill 已经完成，这段 prompt 前缀可以开始被别的请求复用了”。

### 12.2 `best_of/n` 扩容时，会立刻把第一条 sequence 写回 cache

`ContinuousScheduler::handle_running_requests()` 里有一段非常关键：

1. `if (request->expand_sequences())`
2. `kv_cache_manager_->cache(request->sequences()[0].get())`

含义是：

- 只有当第一条 sequence 的 prompt KV 已经够完整，request 才会展开更多 sibling sequences。
- sequence 扩出来之后，scheduler 会立刻把第一条 sequence 的 prefix 插入 cache。

这样新增 sequence 就能共享这段 prompt 前缀，而不是每条都重新 prefill。

这也是 prefix cache 直接参与 request 级 sequence 扩容策略的地方。

### 12.3 request/sequence 释放前，最后再做一次兜底 cache

`BlockManagerPool::deallocate(Sequence* sequence)` 会先：

- `cache(sequence)`

再：

- `block_managers_[dp_rank]->deallocate(sequence->kv_state().kv_blocks())`
- `sequence->reset()`

这意味着：

1. finish/cancel/preempt 之前，sequence 当前已经形成的 full blocks 还有机会被补进 prefix cache。
2. prefix cache 插入和 sequence 释放是严格先后顺序。

所以 prefix cache 并不只靠“第一次 decode 后写回”这一条路径。

### 12.4 host/store 路径还有额外写回

在 `HierarchyBlockManagerPool` 里，host-side prefix cache 还有两条额外写回路径：

1. `update_prefetch_result()`
   - 从 remote store 拉回的 host blocks 成功后，直接 `cache(host_blocks.slice(...))`

2. `transfer_blocks()`
   - D2H offload 成功后，对 host blocks 做 `cache(host_blocks)` 再释放

所以 host 层前缀缓存也是会被持续增量更新的。

---

## 13. ContinuousScheduler 怎样利用 prefix cache

### 13.1 prefill 调度阶段：先 match，再只算 suffix

对于 waiting queue 里的 prefill 请求，`ContinuousScheduler::handle_prefill_requests()` 会调用：

- `kv_cache_manager_->allocate(prefill_sequence.get())`

而 `allocate()` 内部已经把：

1. prefix match
2. shared block 接入
3. suffix block 新分配

都做完了。

所以 scheduler 表面上只是在“申请 KV blocks”，实际上里面已经隐含了 prefix cache 命中逻辑。

### 13.2 当前 token budget 估计是偏保守的

这里源码自己已经写了 `FIXME`：

- `num_tokens = prefill_sequence->num_need_compute_tokens();`
- 注释里明确说 prefix cache 开启时，这里当前仍然可能高估实际会处理的 token 数

原因是：

1. scheduler 先按 sequence 当前状态估算需要处理多少 token
2. 真正的 `allocate_shared()` 发生在 `kv_cache_manager_->allocate()` 里
3. 命中后 `kv_cache_tokens_num()` 才变大

所以当前 continuous prefill 的 token budget 估算，仍然是：

- 正确但偏保守

这不是 prefix cache 功能错误，但说明 scheduler 预算模型还没有完全把 prefix 命中前移到估算阶段。

### 13.3 decode 阶段：prefix cache 还会和 preemption 逻辑一起影响内存

decode 分配时，scheduler 会看每条 sequence 是否还需要额外 block。

如果不够，就可能：

- preempt 低优先级请求
- 或依赖 allocator 触发 prefix cache eviction

而 `check_if_enough_to_evict()` 在统计“可被回收的 blocks”时，还会专门区分：

- shared prefix blocks
- 非 shared blocks

并对 shared block 再看 `ref_count <= 2`

所以 prefix cache 不是只影响 prefill，它还直接参与 decode 阶段的“哪些 block 可以通过 preempt/evict 被腾出来”。

---

## 14. ChunkedPrefillScheduler 怎样利用 prefix cache

这是我这次最认真看的一段，因为它最容易把人绕晕。

### 14.1 chunked prefill 不是只 match 一次

`ChunkedPrefillScheduler::allocate_shared_blocks_for(sequence)` 明确支持：

1. 初次进入 prefill 时匹配一次
2. 如果已经在 `CHUNKED_PREFILL` 阶段，还会按频率重新做 `allocate_shared(sequence)`

核心参数是：

- `FLAGS_chunked_match_frequency`

代码逻辑是：

1. 先计算整个 prompt 会被切成多少个 chunk
2. 如果 chunk 总数本来就不大，小于 `chunked_match_frequency`
   - 那么每轮都可以尝试 rematch
3. 如果 chunk 很多
   - 就按 `prefix_cache_interval = ceil(total_chunked_size / chunked_match_frequency)` 控制 rematch 间隔

所以它不是“每个 chunk 都一定 rematch”，而是：

- 按全局 chunk 数和频率参数做节流

### 14.2 rematch 之后，旧 unique blocks 可能被 shared blocks 替换掉

chunked 场景里，sequence 已经可能算过前几个 chunk。

这时如果新一轮 rematch 发现：

- 有更多 prefix blocks 现在能共享了

`KVCacheState::add_shared_kv_blocks()` 会把前面那些 unique blocks 替换掉。

这一步的价值在于：

1. 不是只减少后续算力
2. 还会主动回收 sequence 自己之前已经占住的 KV blocks

因此 chunked + prefix cache 的收益同时来自：

1. 少算 token
2. 少占 block

### 14.3 代码里有两处值得特别注意

第一处是注释明确承认：

- chunked prefill 当前的 latency estimation 还没有完全把 prefix 命中后的实际 handle token 数分离干净

第二处是：

- `ChunkedPrefillScheduler` 里原本有直接 `kv_cache_manager_->cache(sequence.get())` 的位置，但现在是注释掉的

这说明当前 chunked 路径的 prefix cache 行为，至少在“每个 chunk 结束后是否立即写回 cache”这个点上，仍然带有演进痕迹。

我更保守的理解是：

- 当前 chunked 路径已经稳定支持“多阶段 rematch”
- 但“每一段 chunk 何时写回 cache 才最优”这件事，在代码中仍能看到继续优化的痕迹

---

## 15. ZeroEvictionScheduler 怎样利用 prefix cache

`ZeroEvictionScheduler` 的核心思想不是“尽量命中 cache”，而是：

- 在接受新 prefill 请求之前，先模拟未来若干轮，看会不会把别的请求挤到必须淘汰

而 prefix cache 在这里的作用是：

### 15.1 准入模拟前，先给候选请求做 shared allocation

`BlockCapacityGuard::prefix_cache_for_candidate_sequences()` 会先：

- 对候选 sequence 调用 `kv_cache_manager_->allocate_shared(sequence)`

这样后面的模拟，不是按“完全没有 cache 命中”去估算，而是按：

- 当前真实可共享前缀后的 block 需求

去估算。

### 15.2 zero-evict 关心的是“接受这个 prefill 后，未来是否会逼出淘汰”

它会计算：

1. 候选 prefill 需要保留多少 blocks
2. running sequences 后续 decode 还会逐轮吃掉多少 blocks
3. 哪些 blocks 会随着请求结束而释放
4. 当前 prefix cache 里哪些 blocks 只是缓存，不是活跃占用

所以 prefix cache 在 zero-evict 里的角色，不是简单“命中优化”，而是：

- 让准入模拟对真实 block 占用更准确

这就是为什么 zero-evict 和 prefix cache 是天然配套的。

---

## 16. Host / Store / Upload 扩展路径

基础 prefix cache 只讲 device block 还不够，xLLM 还把它扩到了 host/store 和 service routing。

### 16.1 `PrefixCacheWithUpload`

如果 `enable_cache_upload` 打开，`create_prefix_cache()` 不会返回普通 `PrefixCache`，而会返回：

- `PrefixCacheWithUpload`

它在 `insert()` / `evict()` 之后会异步记录：

- `stored_cache`
- `removed_cache`

具体实现是：

1. 保存一批 `XXH3Key`
2. 通过 threadpool 异步写进 `DoubleBuffer<KvCacheEvent>`
3. `get_upload_kvcache_events()` 读取时做一次 front/back swap

所以它并不改变本地命中逻辑，只是给外部系统提供“最近哪些 cache key 新增/删除了”的增量事件流。

### 16.2 xservice 心跳会带上这些 cache 事件

`xllm/core/runtime/xservice_client.cpp` 的 `heartbeat()` 会：

1. 从 `block_manager_pool_->get_merged_kvcache_event(&event)` 取出聚合事件
2. 把 `stored_cache` / `removed_cache` 序列化进 heartbeat request

所以 service routing 能知道：

- 这个实例当前大概拥有哪些 prefix cache key

这和 `0-overview.md` 里“service routing / cache upload”那行配置分层是完全对上的。

### 16.3 `HierarchyBlockManagerPool` 把 prefix cache 扩到了 host 层

如果：

- `host_blocks_factor > 1.0`
- 或 `enable_kvcache_store = true`

`LLMEngine` 初始化时会选择：

- `HierarchyBlockManagerPool`

这个类同时维护：

1. device block managers
2. host block managers

host manager 本身也有 prefix cache，因此会出现：

- host shared blocks
- host-side rematch
- prefetch from storage 后把 host blocks 插回 host prefix cache
- device offload 到 host 后再插回 host prefix cache

我的理解是：

- 这已经不是“单层 prefix cache”
- 而是“以同样的 hash/block 语义，把 prefix 复用扩展到 host/store 层”

### 16.4 `compute_hash_keys()` 的真正用途

`PrefixCache::compute_hash_keys()` 在基础命中主路径里不显眼，但在 host/store 路径里很重要。

它的作用是：

1. 已经知道 token 序列
2. 也已经有一批 blocks
3. 但这些 blocks 可能还没被走过常规 `insert(token_ids, blocks)` 路径

这时就先把每个 block 的链式 hash 填进去，后面：

- 可以直接 `cache(blocks)`
- 可以直接带 hash 做传输

这让 prefix cache 的“key 语义”从 device path 扩展到了 host/store path。

---

## 17. 我认为最重要的几个细节

### 17.1 prefix cache 命中并不意味着 sequence 直接进入 decode

命中只会提高：

- `kv_cache_tokens_num()`

但 sequence 是否已经 prefill 完成，还要看：

- 是否覆盖到了 `num_prompt_tokens()`
- 是否还剩尾部 partial block
- 是否触发了“完全相同 prompt 需要回退一个 block”的特殊逻辑

所以正确理解应该是：

- prefix cache 缩短 prefill，而不是替代 prefill 语义本身。

### 17.2 cache key 是“前缀位置相关”的，不是“块内容相关”的

同一段 block token：

- 如果它出现在不同前缀上下文里
- hash 不一样

所以 prefix cache 天然要求：

- 从序列起点开始的完整前缀一致

这也解释了为什么它更适合做 prefix reuse，而不是任意子串 reuse。

### 17.3 `best_of/n` 扩容和 prefix cache 是强绑定的

如果 prefix cache 关闭：

- request 一进来就把 sequences 全展开

如果 prefix cache 开启：

- request 会等第一条 sequence 的前缀 ready 后再扩

这说明在 xLLM 里，prefix cache 已经不是“纯底层优化”，而是 request 级调度策略的一部分。

### 17.4 当前实现是 block-aligned，尾巴永远会留一点重算空间

哪怕 prompt 大部分命中了，只要尾部不满一个 block：

- 这一小段也得重算

再加上“完全相同 prompt 回退一个 block”的特殊逻辑，说明这套系统的设计偏向：

- 保守地保证执行语义正确
- 再尽量多复用

而不是极限追求“一个 token 都不重算”。

---

## 18. 两个我特别记录下来的代码观察

这里我不把它们写成“结论”，而是写成“后续值得复查的实现观察”。

### 18.1 文档和实现已经不一致：文档说 Murmur，代码已经是 XXH3

这个我前面已经提过一次，但值得单独记。

如果以后有人继续沿文档学习 prefix cache，很容易误以为：

- 当前线上实现还在用 MurmurHash

但源码和 benchmark 已经说明：

- 当前主实现是 `XXH3_128bits_withSeed`

### 18.2 `ConcurrentBlockManagerImpl::allocate_shared()` 没有把 `existed_shared_blocks` 继续传下去

`ConcurrentBlockManagerImpl` 的签名虽然也接收：

- `const Slice<Block>& existed_shared_blocks`

但真正调用基类时只写了：

- `BlockManagerImpl::allocate_shared(tokens_ids);`

没有把 `existed_shared_blocks` 传下去。

这意味着：

- 并发 block manager 这一支目前没有吃到“基于已有 shared prefix 继续增量 match”的优化参数

它会不会影响到：

1. chunked prefill 的增量 rematch 效果
2. host/store 路径的增量匹配充分性

我觉得值得后续单独验证。

我先把它记成：

- 一处实现层面的可疑不对齐点

而不是直接写成 bug 结论。

---

## 19. 最后的整体理解

现在如果让我用一句比较准确的话描述 xLLM 的 prefix cache，我会这么说：

> xLLM 的 prefix cache，本质上是一层“按 block 对齐、以链式前缀哈希为 key、以 `Block` 引用计数为生命周期基础”的 KV 前缀复用索引；它不直接参与模型计算，但会通过 `kv_cache_tokens_num`、block 分配/共享/淘汰、scheduler 预算和 batch 输入裁剪，深度影响 prefill 的算力开销和显存占用。

再把它放回主链里，就是：

`Sequence tokens`
-> `PrefixCache::match()` 产出 shared blocks
-> `KVCacheState` 更新“已有多少 KV”
-> `BatchInputBuilder` 只构造未命中 suffix
-> `BlockManager` 用 refcount/LRU 维护共享与回收
-> scheduler 继续在这个状态上做 batch、preempt、zero-evict、chunked rematch

因此，prefix cache 在 xLLM 里绝不是“一个小优化点”，而是：

- 连接调度、KV 内存、请求生命周期、分层缓存和服务路由的一条横切主线。

---

## 20. 当前结论的边界

这份笔记已经能比较完整解释：

1. 单机/device 侧 prefix cache 主路径
2. `continuous / chunked / zero_evict` 三个主要 scheduler 的联动
3. `cache_upload` 和 `host/store` 两条扩展路径

但我仍然保留下面两个边界：

1. PD 分离路径虽然残留了一些 prefix-cache 相关接口，但官方文档明确要求关闭，我没有把它当成稳定能力去展开。
2. `ConcurrentBlockManagerImpl` 和 chunked path 里仍有一些实现痕迹值得后续专门做验证，尤其是“增量 rematch 是否完整生效”这一点。

如果后面继续深入，我建议下一步单独做两个专题：

1. `prefix cache + chunked prefill` 的逐 step 时序图
2. `prefix cache + host/store + service routing` 的跨层缓存传播图
