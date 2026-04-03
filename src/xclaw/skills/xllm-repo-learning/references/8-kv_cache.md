# xLLM KV Cache 详细学习笔记

## 0. 说明与阅读边界

- 用户问题中提到的 `overview.md` / `structure.md`，仓库内实际对应文件是：
  - `ralph/learn_xllm/0-overview.md`
  - `ralph/learn_xllm/1-structure.md`
- 本文承接这两份笔记里的大框架结论：xLLM 是一个“服务层 -> 调度层 -> 执行层 -> 模型层 -> 平台层”的分层系统，`KV Cache` 不是单独一个类，而是横跨 `scheduler / framework / runtime / distributed_runtime / kv_cache transfer` 的一整套机制。
- 本次重点分析 LLM 主链中的 KV Cache 逻辑，尤其关注：
  1. 设备侧 KV Cache 的容量估算与物理布局
  2. `Sequence` 上的逻辑 KV 状态如何推进
  3. `Block / PrefixCache` 如何做块级复用
  4. Host / Store / PD 远端传输如何接到主链里
- 我尽量只写源码直接支持的结论；若某处是结合代码行为做出的解释，我会显式写成“我的判断/推断”。

---

## 1. 先给结论：xLLM 的 KV Cache 其实是五层系统

我现在对 xLLM KV Cache 的整体理解可以概括为下面五层：

1. **逻辑状态层**：`Sequence` / `KVCacheState`
   - 记录“这个序列已经有多少 token 对应的 KV 是可复用的”。
   - 这里有两套状态：
     - `kv_state_`：设备侧（HBM/NPU 显存）KV 状态
     - `host_kv_state_`：主机侧（DRAM / store 预取）KV 状态

2. **块分配层**：`Block` / `BlockManagerImpl` / `BlockManagerPool`
   - 把 KV Cache 管理为 **固定大小 block**，而不是按 token 零散管理。
   - `Block` 既是物理块句柄，也是 prefix cache 的 key carrier。

3. **前缀复用层**：`PrefixCache`
   - 本质上缓存的是“前缀对应的 KV block”，不是 token 本身。
   - 粒度是 **full block**，并通过 **链式 XXH3-128 hash** 做最长前缀匹配。

4. **执行输入映射层**：`BatchInputBuilder`
   - 把逻辑块状态转换成模型执行真正需要的：
     - `new_token_slot_ids`
     - `block_tables`
     - `paged_kv_indices / indptr / last_page_len`
     - `kv_cache_tokens_nums`

5. **物理存储与传输层**：`WorkerImpl` / `KVCache` / `HierarchyKVCacheTransfer` / `KVCacheTransfer` / `KVCacheStore`
   - 真正分配每层的 K/V 张量。
   - 实现：
     - HBM 内部写入
     - HBM <-> Host DRAM
     - Host DRAM <-> Mooncake store
     - Prefill 节点 <-> Decode 节点的跨实例 KV 传输

一句话总结：

> xLLM 的 KV Cache 不是“一个大 tensor + 一个索引表”，而是“序列状态 + block allocator + prefix reuse + batch slot mapping + 多级存储/传输”共同组成的系统。

---

## 2. 本次直接阅读的核心文件

### 2.1 前置学习笔记

- `ralph/learn_xllm/0-overview.md`
- `ralph/learn_xllm/1-structure.md`
- `ralph/learn_xllm/2-prefix_cache.md`
- `ralph/learn_xllm/task.md`
- `docs/zh/features/global_kvcache.md`
- `docs/zh/features/prefix_cache.md`

### 2.2 KV/Block/Prefix 相关主文件

- `xllm/core/framework/request/sequence.h`
- `xllm/core/framework/request/sequence.cpp`
- `xllm/core/framework/request/sequence_kv_state.h`
- `xllm/core/framework/request/sequence_kv_state.cpp`
- `xllm/core/framework/block/block.h`
- `xllm/core/framework/block/block.cpp`
- `xllm/core/framework/block/block_manager.h`
- `xllm/core/framework/block/block_manager_impl.h`
- `xllm/core/framework/block/block_manager_impl.cpp`
- `xllm/core/framework/block/block_manager_pool.h`
- `xllm/core/framework/block/block_manager_pool.cpp`
- `xllm/core/framework/block/hierarchy_block_manager_pool.h`
- `xllm/core/framework/block/hierarchy_block_manager_pool.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache.h`
- `xllm/core/framework/prefix_cache/prefix_cache.cpp`
- `xllm/core/framework/prefix_cache/prefix_cache_with_upload.h`
- `xllm/core/framework/prefix_cache/prefix_cache_with_upload.cpp`

### 2.3 Batch / Scheduler / Engine / Worker 相关文件

- `xllm/core/framework/batch/batch_input_builder.h`
- `xllm/core/framework/batch/batch_input_builder.cpp`
- `xllm/core/framework/batch/batch.cpp`
- `xllm/core/scheduler/continuous_scheduler.cpp`
- `xllm/core/scheduler/chunked_prefill_scheduler.cpp`
- `xllm/core/scheduler/zero_eviction_scheduler.cpp`
- `xllm/core/scheduler/disagg_pd_scheduler.cpp`
- `xllm/core/scheduler/pd_ooc_scheduler.cpp`
- `xllm/core/distributed_runtime/engine.h`
- `xllm/core/distributed_runtime/llm_engine.h`
- `xllm/core/distributed_runtime/llm_engine.cpp`
- `xllm/core/runtime/options.h`
- `xllm/core/runtime/worker_impl.h`
- `xllm/core/runtime/worker_impl.cpp`
- `xllm/core/runtime/llm_worker_impl.cpp`

### 2.4 多级缓存 / 传输 / Store 相关文件

- `xllm/core/framework/kv_cache/kv_cache.h`
- `xllm/core/framework/kv_cache/kv_cache.cpp`
- `xllm/core/framework/kv_cache/hierarchy_kv_cache_transfer.h`
- `xllm/core/framework/kv_cache/hierarchy_kv_cache_transfer.cpp`
- `xllm/core/framework/kv_cache/kv_cache_transfer.h`
- `xllm/core/framework/kv_cache/kv_cache_transfer.cpp`
- `xllm/core/framework/kv_cache/kv_cache_store.h`
- `xllm/core/framework/kv_cache/kv_cache_store.cpp`
- `xllm/core/common/types.h`
- `xllm/core/framework/model/model_input_params.h`
- `xllm/core/util/hash_util.h`

---

## 3. 关键对象关系图

```text
Request
  └─ Sequence
      ├─ KVCacheState kv_state_        # 设备侧 KV block 与进度
      ├─ KVCacheState host_kv_state_   # host/store 侧 KV block 与进度
      └─ tokens_

KVCacheManager (接口)
  └─ BlockManagerPool
      ├─ BlockManagerImpl / ConcurrentBlockManagerImpl   # 设备 block 管理
      ├─ PrefixCache                                     # 设备 prefix 复用
      └─ HierarchyBlockManagerPool(子类)
          ├─ host_block_managers_                        # host block 管理
          ├─ host PrefixCache                            # host prefix 复用
          └─ transfer info queues                        # H2D / D2G / G2H

Engine / Worker
  ├─ LLMEngine::estimate_kv_cache_capacity()             # 算容量
  ├─ WorkerImpl::allocate_kv_cache()                     # 建每层 KV tensor
  ├─ HierarchyKVCacheTransfer                            # HBM<->DRAM<->Store
  └─ KVCacheTransfer                                     # Prefill/Decode 跨实例传输
```

---

## 4. `Sequence` 视角：KV Cache 的“逻辑真相”存在哪里

### 4.1 `Sequence` 同时维护 device 和 host 两套 KV 状态

`Sequence` 中最关键的两个成员是：

- `kv_state_`
- `host_kv_state_`

对应接口：

- `cached_tokens()` 返回 `[0, kv_state_.kv_cache_tokens_num())`
- `cached_host_tokens()` 返回 `[0, host_kv_state_.kv_cache_tokens_num())`
- `num_need_compute_tokens()` 返回：

```cpp
num_tokens_ - max(kv_state_.kv_cache_tokens_num(),
                  host_kv_state_.kv_cache_tokens_num())
```

这说明一个非常关键的设计点：

> xLLM 认为“已经可复用的前缀”不只来自设备显存，也可以来自 host 层已经预取好的 KV。

但这里还有一个 **非常容易忽略的不对称点**：

- `Sequence::stage()` 只看 `kv_state_.kv_cache_tokens_num()`
- `num_need_compute_tokens()` 看的是 `max(device, host)`

这意味着：

1. 一个序列即便在 host 上已经有更多前缀，也不代表它已经进入 decode 阶段；
2. 调度器可以因为 host 命中而减少需要计算的 token；
3. 但真正“进入 decode / 真正在设备上可直接读”的判定仍然由 device KV 决定。

这是我认为 xLLM 多级 KV 设计里很重要的语义边界。

### 4.2 `KVCacheState` 保存的不是 tensor，而是 block 句柄和逻辑游标

`KVCacheState` 的核心字段：

- `kv_cache_tokens_num_`
  - 当前已有多少 token 的 KV 可以被视为“已就绪”。
- `blocks_`
  - 当前序列持有的 block 句柄列表。
- `src_blocks_`
  - beam search / swap 场景下的源 block。
- `need_swap_`
  - beam search 是否需要为最后一个 block 触发 swap。
- `transfer_kv_info_`
  - PD 分离模式下的远端传输信息。
- `num_owned_shared_blocks_`
  - 这条序列前面有多少 block 是“shared prefix blocks”。

因此，`KVCacheState` 不是“真正的数据缓冲区”，它更像：

> “该序列的 KV block 拥有权 + 当前已经落到 KV 的 token 位置 + 传输元信息”。

### 4.3 slot 计算是由 `block_id * block_size + offset` 完成的

`KVCacheState::kv_cache_slots(pos_start, pos_end)` 的逻辑很直接：

```cpp
block_id = blocks_[i / block_size].id()
block_offset = i % block_size
slot = block_id * block_size + block_offset
```

所以：

- 对模型执行而言，token 的 KV 落点不是直接按 token 连续排布；
- 它先找到属于哪个 block，再加 block 内 offset；
- `block_id` 是逻辑块号，也是后续 H2D / D2G / prefix cache / PD transfer 的统一索引主键。

---

## 5. `Block` 视角：xLLM 把 KV Cache 当成“固定大小块池”来管理

### 5.1 `Block` 是 RAII 句柄，不是裸整数 ID

`Block` 里包含：

- `id_`
- `size_`
- `ref_count_`
- `manager_`
- `hash_value_`

其中最关键的语义是：

- `Block` 拷贝时会 `inc_ref_count()`
- `Block` 析构时会 `dec_ref_count()`
- 当 `ref_count` 归零时，才会调用 `manager_->free(id_)`

所以 block 的生命周期是通过 C++ 对象引用数自动维护的。

### 5.2 `hash_value_` 不是附属字段，而是 prefix cache 的核心载体

`Block` 注释已经明确写了：

```cpp
hash_value_ = Hash(current_block, Hash(pre_block))
```

也就是说：

- 一个 block 自己带着“从序列开头到当前 block 的链式前缀 hash”；
- prefix cache 命中之后，这个 hash 可以继续复用，不必从头重算；
- Host/Store/PD 等扩展路径也可以只传 block + hash，而不必重复携带完整 token 序列。

### 5.3 block 0 被保留为 padding block

`BlockManagerImpl` 初始化时会：

- 建立自由块栈 `free_blocks_`
- 立刻 `allocate()` 一个 block 给 `padding_block_`
- 强制要求它的 `id == 0`

所以 block 0 在这个系统里是保留块，不参与普通回收。

---

## 6. `BlockManager` / `BlockManagerPool`：真正的块分配主链

### 6.1 `BlockManagerImpl` 管的是单个 DP rank 的 block 池

它负责：

- `allocate(num_blocks)`
- `deallocate(blocks)`
- `allocate_shared(tokens, existed_shared_blocks)`
- `cache(token_ids, blocks, existed_shared_blocks_num)`
- `has_enough_blocks()` + prefix eviction

而 `BlockManagerPool` 则负责：

- 多个 DP rank 的 block manager 组合
- 给 `Sequence` 分配 `dp_rank`
- 暴露 `KVCacheManager` 统一接口给 scheduler 使用

### 6.2 `allocate(sequence, num_tokens)` 只保证“块够用”，不会推进逻辑 token 游标

这是一个很容易误写错的点。

`BlockManagerPool::allocate(sequence, num_tokens)` 的行为是：

1. 如果当前还没有 block，先 `allocate_shared(sequence)`，尝试 prefix 命中。
2. 计算当前已持有 block 数 `num_blocks`。
3. 计算处理到 `num_tokens` 一共需要多少 block。
4. 如果现有 block 不够，再补分配增量 block。
5. **不会修改 `kv_cache_tokens_num_`。**

也就是说它只是“预留物理空间”，不等于“这些 token 已经写进 KV cache”。

这与 `try_allocate()` 不同。

### 6.3 `try_allocate(sequence)` 是特殊路径，会直接把逻辑游标推进到整段 token

`BlockManagerPool::try_allocate(sequence)` 的最后一步是：

```cpp
sequence->kv_state().incr_kv_cache_tokens_num(sequence->tokens().size());
```

这个接口不是 continuous 主路径，而是 **PD decode admission** 特殊路径使用的（`DisaggPDScheduler::try_allocate`）。

我的理解是：

> 这个路径用于 decode 节点接收一个“已经在别处做过 prefill 的请求”时，先把本地 block 与目标 token 范围一次性建立起来，方便后续远端 KV 注入。

### 6.4 `deallocate(sequence)` 并不是直接 free，而是先 `cache(sequence)`

普通 `BlockManagerPool::deallocate(sequence)` 的流程是：

1. `cache(sequence)`：把序列里已完成的 full blocks 插入 prefix cache
2. `block_manager->deallocate(sequence->kv_state().kv_blocks())`
3. `sequence->reset()`

所以从设计上看：

> xLLM 把“请求结束后这批 KV 是否还能作为 prefix 被后续复用”放在释放前最后一步处理。

---

## 7. Prefix Cache：xLLM 的本地块级前缀复用核心

### 7.1 prefix cache 缓存的不是 token，而是 block

`PrefixCache::Node` 里真正保存的是：

- `Block block`
- `last_access_time`
- LRU 链表指针

token 的作用只有两个：

1. 用来计算链式 hash key；
2. 用来证明这段 block 对应的是哪段前缀。

所以更准确地说：

> xLLM 的 prefix cache 本质上是“前缀 -> KV block 句柄”的映射。

### 7.2 粒度严格按 full block，对不齐尾巴不命中

`PrefixCache::match()` 一开始就：

```cpp
n_tokens = round_down(token_ids.size(), block_size_)
```

`insert()` 也只处理 full block 数量。

因此：

- 小于一个 block 的尾部 prompt 不会命中；
- 尾巴不满 block 的部分仍要重新算；
- 命中的最小单位是一个完整 block，而不是单 token。

这和设备侧真正按 block 存 KV 是完全一致的。

### 7.3 当前实现使用的是链式 `XXH3-128`，不是文档里写的 MurmurHash

`docs/zh/features/prefix_cache.md` 还写着 `mermer_hash`，但源码已经明确是：

- `XXH3_128bits_withSeed`
- 并使用 `FLAGS_xxh3_128bits_seed`

哈希规则是：

- 第一块：`H0 = XXH3(block0_tokens)`
- 后续块：`Hi = XXH3(Hi-1 || block_i_tokens)`

这意味着 key 表示的是：

> “从序列开头到当前 block 的完整前缀”，而不是“当前 block 的局部内容”。

因此只靠一个 hash map 就能做最长前缀匹配。

### 7.4 `match()` 支持“从已有 shared prefix 继续往后匹配”

`match(token_ids, existed_shared_blocks)` 有个很重要的优化：

- 如果序列已经持有前面若干个 shared blocks，新的匹配不需要从头重算哈希；
- 直接从 `existed_shared_blocks.back().hash_value()` 继续往后链式推进。

这和 chunked prefill 的多阶段 rematch 是直接配套的。

### 7.5 `insert()` 会把 hash 写回 block 自身

`PrefixCache::insert(token_ids, blocks, ...)` 不只是把 `<hash, block>` 放进 map，
它还会对每个 block 调：

- `blocks[i].set_hash_value(token_hash_key.data)`

这使得 block 成为一个“自带前缀身份”的对象。

### 7.6 `add_shared_kv_blocks()` 有一个非常关键的“精确 prompt 命中回退”处理

`KVCacheState::add_shared_kv_blocks()` 里有个非常精细的逻辑：

- 如果共享 block 覆盖了 `current_total_num_tokens`
- 也就是这次请求的 token 和已有 prefix 完全一样
- 那么它会主动把最后一个 shared block 弹掉，并把 `kv_cache_tokens_num_` 回退到前一个 block 边界

源码注释写得很清楚：

> 完全一样的 prompt 再来一次时，仍然要给模型留出“继续前进”的空间。

这实际上是在避免“所有 token 都共享了，结果当前 step 无事可做”的问题。

### 7.7 淘汰策略是 LRU，但只淘汰当前没被活跃序列使用的块

`PrefixCache::evict()` 会遍历 LRU 链表，但如果：

- `iter_node->block.is_shared()`

就跳过，不会回收。

因此 prefix cache 不会把当前正在被请求使用的 block 淘汰掉。

### 7.8 `PrefixCacheWithUpload` 是“本地 prefix cache + 事件上传”扩展

当 `enable_cache_upload` 开启时，实际构造的是 `PrefixCacheWithUpload`。

它在 `insert()` / `evict()` 时异步维护一份双缓冲 `KvCacheEvent`：

- `stored_cache`
- `removed_cache`

这说明：

> xLLM 的“全局 KV Cache 管理”不是直接把所有本地 KV 自动变成远端可见状态，而是先在本地 prefix cache 层产出 block 级事件，再供上层服务（如 xservice）消费。

这部分我没有继续下钻 xservice 侧消费逻辑，因此这里只能确认到 xLLM 进程内侧。

---

## 8. prefix cache 何时写入：不是在 prefill 分配时立刻写，而是在“进入 decode 后第一次”写 prompt

这是我本次阅读里最重要、也最容易被忽略的一个时序点。

### 8.1 先看 `Sequence::append_token()`

当 prefill 结束、模型产出第一个生成 token 时，`append_token()` 会：

```cpp
cur_idx = num_tokens_++
kv_state_.set_kv_cache_tokens_num(cur_idx)
tokens_[cur_idx] = token_id
```

注意这里 `cur_idx` 是追加前的位置。

如果当前刚结束 prefill，那么：

- `cur_idx == num_prompt_tokens`
- 所以 `kv_cache_tokens_num` 被设成“prompt 长度”
- 新生成 token 本身还 **不计入已缓存 token 数**

### 8.2 再看 scheduler 的逻辑

`ContinuousScheduler::handle_decode_requests()` 在下一个 decode step 准备阶段里，如果：

```cpp
sequence->if_cache_block_for_prefill()
```

成立，就调用：

```cpp
kv_cache_manager_->cache(sequence)
```

而 `if_cache_block_for_prefill()` 的条件是：

- 之前没缓存过
- `num_tokens() > num_prompt_tokens()`

也就是“已经产生出第一个生成 token 之后”的那个时刻。

### 8.3 这就导出一个很关键的结论

在 continuous 主路径里：

> prompt 对应的 prefix blocks 并不是在 prefill block 分配时就立刻写进 prefix cache，
> 而是在请求已经跨过 prefill、真正拿到第一个生成 token 后，才以“纯 prompt 前缀”的形式插入 prefix cache。

这样做的好处是：

1. 可以保证插入的是“已经真正完成计算”的 prompt KV；
2. 插入时 `cached_tokens()` 仍然只覆盖 prompt，不会把刚生成的 token 混进去；
3. prefix cache 语义保持成“可被后续请求直接复用的 prompt 前缀”。

这个细节非常漂亮，也解释了为什么 `append_token()` 要把 `kv_cache_tokens_num` 设成追加前位置。

---

## 9. `BatchInputBuilder`：逻辑 KV 状态如何变成模型输入

### 9.1 每条序列本 step 处理多少 token，是由 `n_kv_cache_tokens` 决定的

`process_single_sequence()` 的核心变量：

- `n_tokens = sequence->tokens().size()`
- `n_kv_cache_tokens = sequence->kv_state().kv_cache_tokens_num()`
- `q_seq_len = min(n_tokens - n_kv_cache_tokens, allowed_max_tokens)`
- `seq_len = n_kv_cache_tokens + q_seq_len`

这说明：

- 设备上前面 `n_kv_cache_tokens` 个 token 视为已落在 KV 中；
- 当前 step 只需要处理 `[n_kv_cache_tokens, seq_len)` 这段 token；
- chunked prefill 本质上就是把 `q_seq_len` 限制成一段一段来推进。

### 9.2 `extract_tokens_and_positions()` 只展平本 step 新处理的 token

builder 会把：

- `token_ids[n_kv_cache_tokens : seq_len)`
- 对应 position `j`

展平成 batch 级 `flatten_tokens_vec / flatten_positions_vec`。

### 9.3 `setup_kv_cache_info()` 会在组 batch 时“提前推进逻辑游标”

这一步非常关键：

```cpp
sequence->kv_state().incr_kv_cache_tokens_num(q_seq_len)
```

也就是说：

> 在 batch 输入真正送进模型执行之前，xLLM 就已经把这一步即将写入 KV 的 token 范围，逻辑上记进了 `kv_cache_tokens_num`。

然后它会生成：

- `new_token_slot_ids`
  - 当前 step 新 token 对应的写入 slot
- `block_tables_vec`
  - 该序列完整 block id 表
- `paged_kv_indices / paged_kv_indptr / paged_kv_last_page_len`
  - paged attention / flashinfer 风格输入
- `transfer_kv_infos`
  - PD 远端传输附加信息

### 9.4 `slot_mapping` 的本质就是“这个 token 应写到哪个 block 的哪个 offset”

builder 调用的是：

```cpp
sequence->kv_state().kv_cache_slots(n_kv_cache_tokens, seq_len)
```

这会返回从旧游标到新游标之间所有 token 的落点。

所以 kernel 侧并不需要自己推断 token 应该写哪个 block；
这些映射在 batch 组装期就已经决定好了。

### 9.5 `kv_cache_tokens_nums`、`block_tables`、`paged_kv_*` 是连接逻辑层和 kernel 层的关键桥梁

这三个量分别回答了三个问题：

- `kv_cache_tokens_nums`
  - 每条序列当前已有多少历史 KV
- `block_tables`
  - 历史 KV 落在哪些 block 上
- `new_token_slot_ids`
  - 新 token 要写进哪些 slot

因此可以说：

> `Sequence/KVState` 定义了“状态是什么”，而 `BatchInputBuilder` 定义了“这些状态如何被模型执行消费”。

---

## 10. 容量估算：`LLMEngine` 如何决定一共能放多少 block

### 10.1 入口：`LLMEngine::estimate_kv_cache_capacity()`

它首先确定总可用 cache bytes：

- 普通模式：向每个 worker 询问可用显存，再取最小值
- xtensor 模式：直接使用物理页池大小

同时会考虑：

- `max_memory_utilization`
- `max_cache_size`

### 10.2 `slot_size` 的含义是“每个 token、每层、每 block slot 占多少字节”

#### 普通 attention

```cpp
slot_size = 2 * cache_dtype_size * head_dim * n_local_kv_heads
```

含义：

- K 一份
- V 一份
- 每份大小 = `head_dim * n_local_kv_heads * dtype_size`

#### MLA

MLA 不再是标准 K/V 头布局，而是按：

- `kv_lora_rank`
- `qk_rope_head_dim`

来算。

NPU 上 `deepseek_v3 + prefix_cache` 还会走特殊 NZ 对齐形态。

### 10.3 `index_slot_size` 和 `scale_slot_size`

- `index_slot_size`
  - lighting indexer 打开时额外分配 index cache，且始终使用模型原始精度。
- `scale_slot_size`
  - KV quant 打开时，额外给 scale tensor 预留每 token 字节数。

### 10.4 最终 block 数公式

```cpp
block_size_in_bytes = block_size * (slot_size + index_slot_size + scale_slot_size)
n_blocks = cache_size_in_bytes / (n_layers * block_size_in_bytes)
```

这说明 xLLM 的 `n_blocks` 本质上是：

> “单 worker、单层 block 体积”反推出来的逻辑块容量。

---

## 11. 物理布局：worker 端真正分配了什么 KV tensor

### 11.1 `LLMEngine::allocate_kv_cache()` 先确定 `kv_cache_shape`

#### 普通 attention

- K: `[n_blocks, block_size, n_local_kv_heads, head_dim]`
- V: `[n_blocks, block_size, n_local_kv_heads, head_dim]`

#### MLA

通常会分成两份：

- 一份对应 `kv_lora_rank`
- 一份对应 `qk_rope_head_dim`

#### lighting indexer

额外：

- `[n_blocks, block_size, 1, index_head_dim]`

#### 特化布局

- MLU / ILU 会调换部分维度顺序
- NPU 上 `deepseek_v3 + enable_prefix_cache` 会使用 NZ 对齐形状

### 11.2 `WorkerImpl::allocate_kv_cache()` 为每一层创建一个 `KVCache`

worker 端会构建：

- `std::vector<KVCache> kv_caches_`

每个 layer 一个 `KVCache`，每个 `KVCache` 内可能包含：

- `key_cache`
- `value_cache`
- `index_cache`
- `key_cache_scale`
- `value_cache_scale`

这意味着：

> xLLM 的 block id 是沿着每层 tensor 的第 0 维索引的。

也就是说同一个 `block_id` 在第 0 层、第 1 层……第 N 层分别指向该层对应 block 的切片。

---

## 12. 多级 KV：为什么还有 `HierarchyBlockManagerPool`

当满足下面任一条件时，`LLMEngine` 不再使用普通 `BlockManagerPool`，而是使用 `HierarchyBlockManagerPool`：

- `host_blocks_factor > 1.0`
- `enable_kvcache_store = true`

这时系统里就不只有设备 block manager，还有：

- `host_block_managers_`
- `load_block_transfer_infos_`
- `offload_block_pair_queues_`

也就是：

> 设备显存之外，还会维护一套 host block 池，并把 H2D / D2G / G2H 的动作纳入统一管理。

---

## 13. Host/Store 分层缓存的真实流程

### 13.1 Host 层也有自己的 prefix cache

`HierarchyBlockManagerPool` 不只是比普通池多了 host blocks，
它还给 host 侧也建了 `BlockManagerImpl / ConcurrentBlockManagerImpl`。

因此 host 侧同样具备：

- block 分配
- prefix 匹配
- prefix cache 插入
- LRU 淘汰

这意味着多级缓存不是“HBM miss 就直接去 store”，中间还有一层 **host prefix 复用**。

### 13.2 请求进入时，会先尝试从 store 预取到 host

`ContinuousScheduler::add_request()` 一开始就：

```cpp
kv_cache_manager_->prefetch_from_storage(request)
```

而在 `HierarchyBlockManagerPool::prefetch_from_storage()` 中，会做：

1. 先在 host prefix cache 上 `allocate_shared()`，命中已有 host 前缀块；
2. 为剩余可能的前缀块分配 host blocks；
3. 用 `PrefixCache::compute_hash_keys()` 给这些 host blocks 算好 hash；
4. 对除最后一个 block 外的块构造 `TransferType::G2H` 任务；
5. 调 `engine_->prefetch_from_storage()` 发到 worker。

这里“最后一个 block 不预取”的行为，源码直接可见，但**为什么故意少取最后一个 block** 没有直白注释。

我的判断是：

> 这是在保证请求至少保留一段需要本地计算的尾部，从而避免“所有 prompt 完全由预取覆盖、当前 step 没法向前推进”的情况。

这个判断与设备侧 exact-hit 回退一块的设计风格是吻合的，但这里我只能把它标成推断。

### 13.3 预取完成后，不会立刻把 host 块都算成“已命中”

`Sequence::update_prefetch_result()` 会在 future/poll 完成后：

- 统计各 worker 成功数的最小值 `success_cnt`
- `host_kv_state_.incr_kv_cache_tokens_num(success_cnt * block_size)`
- `host_kv_state_.incr_shared_kv_blocks_num(success_cnt)`

所以只有真正完成预取的 host blocks，才会体现在 `host_kv_state_` 中。

### 13.4 Host 命中后，后续 allocate 会把这些 block 再搬回设备

`HierarchyBlockManagerPool::allocate(sequence, num_tokens)` 里，如果：

- `host_cache_token_num > hbm_cache_token_num`

就会为缺失部分构造：

- `TransferType::H2D`

把 host block -> device block 的映射加入 `load_block_transfer_infos_`，并且把：

```cpp
sequence->kv_state().incr_kv_cache_tokens_num(host_cache_token_num - hbm_cache_token_num)
```

提前推进。

这说明在多级缓存模式里：

> device KV 游标可以被 host 层“追平”，从而让当前 batch 直接按更长前缀来准备执行输入。

### 13.5 请求结束时，设备块会尝试 offload 到 host/store

`HierarchyBlockManagerPool::deallocate(sequence)` 的流程比普通模式复杂很多：

1. 先 `BlockManagerPool::cache(sequence)`，把设备 full blocks 插入设备侧 prefix cache；
2. 检查 host_blocks 数量是否够覆盖 device_blocks；
3. 若不够，先补分配 host blocks；
4. 对需要 offload 的 block，复制其 hash 到 host block；
5. 组装 `OffloadBlockPair(device_block, host_block)` 丢进队列；
6. 最后分别 deallocate host/device 句柄并 reset sequence。

这里还有一个很有意思的细节：

```cpp
if (blocks->at(i).ref_count() != 2) continue;
```

这意味着只有 `ref_count == 2` 的 device block 才会被 offload。

结合前面先 `cache(sequence)` 的步骤，我的理解是：

- 一个刚插入 prefix cache 的 block，通常会同时被：
  - 当前 sequence 持有一次
  - prefix cache node 持有一次
- 因而 `ref_count == 2`

所以这个判断本质上是在筛：

> “已经成功进入 prefix cache、但当前 sequence 即将结束、适合继续下沉到 host/store 的 block”。

这个解释和代码行为是吻合的，我认为是高度可信的源码级结论。

### 13.6 真正的 D2H / H2D / Store 操作由 `HierarchyKVCacheTransfer` 完成

worker 侧会在需要时创建 `HierarchyKVCacheTransfer`，负责：

- `D2H`：device -> host 页锁内存
- `H2D`：host 页锁内存 -> device
- `G2H`：store -> host
- `D2G`：device -> host -> store

其中：

- `transfer_kv_blocks(H2D)`：异步排入 H2D 线程池
- `transfer_kv_blocks(D2G)`：同步执行 offload
- `transfer_kv_blocks(G2H)`：走 `KVCacheStore::batch_get`

### 13.7 Host 内存并不是普通小块 malloc，而是大块 page-aligned / mlock / from_blob

`HierarchyKVCacheTransfer::create_page_aligned_host_cache()` 会：

1. 按页大小对齐总内存
2. `mmap` 一整块匿名内存
3. `mlock` 锁住，避免换页
4. NPU 下 `aclrtHostRegister`
5. 用 `torch::from_blob` 切成一块块 host `KVCache`

也就是说 host 缓存实际上是：

> 一个大而连续的页锁定内存池，再在其上构造 block 视图。

这非常符合高带宽 H2D / D2H / RDMA 的需要。

### 13.8 Mooncake store 的 key 不是 block id，而是 `hash + tp_rank`

`KVCacheStore::batch_put / batch_get` 会构造：

```cpp
std::string key(reinterpret_cast<const char*>(hash_key), 16);
key.append(std::to_string(tp_rank));
```

所以 store 的键语义是：

- 这块前缀的 128-bit hash
- 再加当前 TP rank

这说明 store 复用的本质仍然是 **prefix content identity**，不是本地 block 物理号。

---

## 14. Batch 执行前的 H2D 为什么还能和模型执行重叠

`HierarchyKVCacheTransfer::h2d_batch_copy()` 并不是简单 memcpy，它还会：

- 按 layer 分批拷贝
- 为每个批次记录事件 `aclrtRecordEvent`
- 创建 `NPULayerSynchronizerImpl`
- 按 `batch_id` 暂存到 `layer_wise_load_synchronizer_`

随后在 `WorkerImpl::step_async()` 中，会在真正模型执行前调：

- `hierarchy_kv_cache_transfer_->set_layer_synchronizer(input.input_params)`

于是 `input_params` 中就带上了：

- `layer_wise_load_synchronizer`
- `layers_per_bacth_copy`

这说明：

> xLLM 的 H2D 不是粗暴地“全部拷完再算”，而是准备了 layer-wise 的同步机制，允许后续执行侧按层等待数据就绪。

我这次没有继续深入具体 model/kernel 里如何消费这个 synchronizer，但从接口设计上，意图已经非常明确。

---

## 15. PD 分离里的 KV Cache：`KVCacheTransfer` 是另一条线，不要和 Host/Store 搞混

这里非常容易混淆的两个名字：

1. `HierarchyKVCacheTransfer`
   - 处理 **同一实例内部** 的 HBM / DRAM / Store 多级缓存搬运。

2. `KVCacheTransfer`
   - 处理 **Prefill 节点 <-> Decode 节点** 之间的跨实例 KV 传输。

这两者不是同一个层次的问题。

### 15.1 PD scheduler 会把远端 block 信息写进 `Sequence::transfer_kv_info`

`DisaggPDScheduler` / `PDOOCScheduler` 在收到 decode 节点返回后，会给每个 sequence 构造：

- `request_id`
- `remote_blocks_ids`
- `dp_rank`
- `remote_instance_info`
- 可选的 `dst_xtensor_layer_offsets`

然后写入：

```cpp
sequence->kv_state().set_transfer_kv_info(std::move(info))
```

### 15.2 `PUSH` 模式：prefill 节点主动推 KV

在 `LLMWorkerImpl::step_internal()` 里，如果：

- `kv_cache_transfer_mode == "PUSH"`
- `input.transfer_kv_infos` 非空

就会在 model forward 前异步调：

- `kv_cache_transfer_->push_kv_blocks_async(...)`

而 `BatchInputBuilder::setup_kv_cache_info()` 又会补：

- `local_blocks_ids`

它的做法是：

- 看本地 `blocks` 有多少
- 扣掉 `remote_blocks_ids.size()`
- 把新增本地 block id 填进 `local_blocks_ids`

所以 PUSH 语义是：

> Prefill 节点拿着“本地真实 block id + 远端目标 block id”信息，把 KV 推给 Decode 节点。

### 15.3 `PULL` 模式：decode 节点在收到首 token 后主动拉 KV

`DisaggPDScheduler` 在拿到首 token、把请求接入本地 decode 主链后，如果：

- `kv_cache_transfer_mode == "PULL"`

就会调用：

- `engine_->pull_kv_blocks(...)`

把远端 prefill 节点上的 block 拉到本地已分配 block 里。

所以 PULL 语义是：

> Decode 节点自己根据 `src_cluster_id / src_addr / src_k_cache_id / src_v_cache_id / src_block_ids / dst_block_ids` 远程取回 KV。

### 15.4 `try_allocate()` 在 PD decode admission 里很关键

`DisaggPDServiceImpl` 接到新请求时，会先：

- `scheduler_->try_allocate(sequence)`

这会通过 `BlockManagerPool::try_allocate()` 先给 decode 侧 sequence 把 block 空间搭好，
方便后续 PUSH/PULL 把远端 KV 注入这些 block。

---

## 16. `chunked prefill`、beam search、MLA、量化等分支逻辑

### 16.1 chunked prefill 对 prefix rematch 做了额外支持

`ChunkedPrefillScheduler::allocate_shared_blocks_for()` 里可以看到：

- 初次进入时会 `allocate_shared(sequence)`
- 在 chunked prefill 阶段，还会按 `FLAGS_chunked_match_frequency` 周期性重新做 prefix 匹配

再结合 `KVCacheState::add_shared_kv_blocks()` 的“替换已有独占 block 为 shared block”逻辑，可以看出：

> xLLM 的 chunked prefill 不是只在最开头做一次 prefix match，而是允许多阶段 rematch，并尽可能把已经算出来的独占块替换成共享块，回收 KV 占用。

### 16.2 beam search 会额外处理 block swap

`BlockManagerPool::process_beam_search()` 中：

- 若 `need_swap()` 且无新 block 附加，会额外申请一个新 block
- 把 `src_block_id -> new_block_id` 写入 `swap_block_transfer_infos_`

随后 `BatchFactory` / `BatchInputBuilder` 会把这些 swap 信息带给 worker。

这说明 beam search 不是简单共享整段 KV，而是支持 block 级 copy-on-write / swap。

### 16.3 MLA + prefix cache 有专门输入预处理

`WorkerImpl::prepare_mla_prefixcache_inputs()` 会构造：

- `history_compressed_kv`
- `history_k_rope`
- `ring_cur_seqlen`
- `ring_cache_seqlen`

这说明 MLA 不是简单复用普通 attention 的 KV 输入格式，而是有一套 prefix cache 相关的专门辅助输入。

### 16.4 KV Cache 量化当前只支持 MLU，且不支持 VLM / PD

`WorkerImpl::allocate_kv_cache()` 明确限制：

- `kv_cache_dtype == int8` 仅支持 MLU
- 不支持 VLM
- 不支持 PD disaggregation

量化时会额外为 K/V 分配 scale tensor。

### 16.5 xtensor 模式会关闭常规 prefix cache

承接前置学习结果与 `LLMEngine::allocate_kv_cache()` 里的 block manager 选项可确认：

- xtensor 模式下，`enable_prefix_cache` 会被强制关闭

因此 xtensor 走的是另一套全局页管理思路，而不是这里这套常规 `PrefixCache + BlockManagerImpl` 主路径。

---

## 17. 我认为最重要的 10 个结论

1. **xLLM 的 KV Cache 基础单位是 block，不是 token。**
2. **prefix cache 复用的是 full block，对齐不到 block 边界的尾巴不会命中。**
3. **prefix cache 当前实现使用链式 XXH3-128，而不是文档里旧写法的 MurmurHash。**
4. **`Block` 自身携带前缀 hash，是 prefix / host / store / PD 复用的统一身份载体。**
5. **`allocate()` 只分配 block，不推进 `kv_cache_tokens_num`；推进逻辑游标主要发生在 batch 组装和特殊 `try_allocate()` 路径。**
6. **continuous 主链里，prompt 写入 prefix cache 的时机不是 prefill 当下，而是产生首个生成 token 之后。**
7. **xLLM 同时维护 device 和 host 两套 KV 状态，`num_need_compute_tokens()` 用的是两者最大值。**
8. **`HierarchyBlockManagerPool + HierarchyKVCacheTransfer` 形成了 HBM -> Host DRAM -> Mooncake store 的多级缓存链。**
9. **PD 分离里的 `KVCacheTransfer` 是另一条跨实例传输线，不要与 host/store 多级缓存混为一谈。**
10. **整个系统的关键桥梁不是 KV tensor 本身，而是 `Sequence state + block tables + slot mapping + transfer metadata`。**

---

## 18. 几个最容易误解的点

### 18.1 “prefix cache 命中了，是不是就完全不用算了？”

不是。

- 命中只到 full block 边界；
- 精确全量命中时还会主动回退最后一个 block，保证请求能继续向前推进。

### 18.2 “请求结束时 block 直接 free 吗？”

不是。

- 先尝试插入 prefix cache；
- 多级缓存模式下还可能先下沉到 host/store；
- 真正 free 是引用计数归零后由 `BlockManager::free()` 完成。

### 18.3 “host 层有 KV，就说明 sequence 已经 decode 了吗？”

不是。

- `stage()` 只看 device `kv_state_`；
- host 只会减少 `num_need_compute_tokens()`，不会直接改变设备阶段判定。

### 18.4 “全局 KV Cache 就是 store 吗？”

也不是。

更准确地说，全局能力至少分三层：

1. 本地 prefix cache（block 级复用）
2. Host/store 多级缓存（HBM/DRAM/Mooncake）
3. xservice / etcd 参与的全局状态管理与路由

本文确认到了前两层在 xLLM 进程内的实现，以及第三层的事件上传入口；但没有继续追踪 xservice 端消费闭环。

---

## 19. 证据边界与后续建议

### 已确认到的范围

- 本地 block 分配、引用计数、prefix cache 哈希与 LRU
- `Sequence` / `KVCacheState` 的逻辑状态推进
- batch 输入如何把 block 映射成 slot / table / paged-kv
- `LLMEngine` 的 KV 容量估算与 shape 构造
- worker 端的 KV tensor 分配
- host/store 多级缓存的搬运链
- PD 分离下 PUSH/PULL 的挂载点

### 本次没有继续下钻的点

- xservice / etcd 如何消费 `KvCacheEvent`
- Mooncake client 内部的更底层传输细节
- 具体模型层/自定义算子如何消费 `layer_wise_load_synchronizer`
- 非 LLM backend（如 VLM/Rec）对 KV 的个别差异

### 我建议的后续阅读顺序

1. `xllm/core/runtime/params_utils.cpp`
   - 看 `TransferKVInfo / BlockTransferInfo` 在 RPC / SHM 里的序列化路径
2. `xllm/core/framework/kv_cache/mooncake_kv_cache_transfer.*`
   - 深挖 PD 远端传输在 NPU/Mooncake 侧的真正实现
3. `xllm/core/runtime/forward_params.h`
   - 把 batch 输入字段和 kernel 约定完全对齐
4. 具体模型 kernel / custom op
   - 看 `block_tables`、`new_token_slot_ids`、`paged_kv_*` 最终如何被 kernel 消费

---

## 20. 最后一段总结

如果只让我用一句话概括这次学习结果，我会这样说：

> xLLM 的 KV Cache 核心设计，不是“把历史 attention 的 K/V 存下来”这么简单，而是把历史上下文抽象成可共享、可淘汰、可迁移、可跨层级搬运的 `block`，再由 `Sequence` 的逻辑状态、`PrefixCache` 的前缀哈希、`BatchInputBuilder` 的 slot 映射，以及 `HierarchyKVCacheTransfer / KVCacheTransfer` 的多级传输机制共同驱动整个系统运行。

这也是为什么 xLLM 的 KV Cache 逻辑必须放在整个主链里理解，而不能只盯着某一个 `kv_cache.h` 文件看。
