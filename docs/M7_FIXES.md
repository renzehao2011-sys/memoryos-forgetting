# M7 修复记录（针对第三方审查的 6 个卡点）

日期：2026-11-01　分支：`m1-schema`　基线：`2f88a35`（M6 交付点）
状态：**未 commit**，工作区改动可直接 `git diff` 复核。

---

## 0. 一句话

把审查里「硬约束被违反／声明与代码不一致」的 6 处补上，并给「可回退」补了一个真正的入口。
参数（τ/阈值）**没动**——那部分我的结论是「证据只能证伪过短，不能证实 90」，属于要重标的事，不该顺手改。

---

## 1. 改动清单

| # | 位置 | 问题 | 改法 |
|---|---|---|---|
| M7-1 | `memoryos/long_term.py` | `deque(maxlen=knowledge_capacity)` + `save()` 写 `list(deque)` → 超过 100 条时**静默硬删除最旧一条**，无审计、无 state | deque 去掉 maxlen；超过容量改为打 `over_capacity_since` 标记（不删、不动 state）；`load()` 也不再按 maxlen 截断；新增 `clear_over_capacity(ids)` |
| M7-2 | `memoryos/consolidate.py`、`audit.py`、`run_consolidate.py` | `valid_to` 只写不清，merge / expire 之后**没有回退路径**（计划单 §0「每次淡化都能被回退」不成立） | 新增 `restore_expired(items, ids, ...)`：按显式 id 清 `valid_to`，保留 `merged_into`/`superseded_by` 作历史，记 `restored_at/by`，写审计 `action=restore`；CLI `--restore id1,id2` |
| M7-3 | `memoryos/mid_term.py` | `evict_lfu` 声称「从热段堆摘掉、不再触发画像升级」，但 `rebuild_heap()` 无条件全量重压堆，**一次检索就加回来**；且没有任何解除标记的路径 | `rebuild_heap()` 跳过 `over_capacity_since` 的会话；新增 `clear_over_capacity(ids)`；把注释里「交整理层判定」改成事实（整理层不读该标记，处置权在人 + 报告/抽检可见） |
| M7-4 | `memoryos/consolidate_llm.py` | 冲突候选是 `find_conflict_candidates(all_items, all_items)`，**跨层级**比较（mid_term page × long_term knowledge），相似度下限 0.40，审计 `level` 为空 | 只在同层级内配对（`level_of(old) != level_of(new)` 直接跳过）；审计补 `level=level or level_of(old)` |
| M7-5 | `mid/long/short_term.py` | `save()` 是 `open(path,"w")` 非原子；`load()` 遇 `JSONDecodeError` 静默「Initializing new memory」置空 → **下一次 save 就把空库写回** | `save()` 改 `tmp + fsync + os.replace`；损坏文件改名保留为 `*.corrupt-<时间戳>`（不删）；并置 `_load_failed=True`，此后 `save()` 一律拒绝写盘 |
| M7-6 | `memoryos/retriever.py` | 召回只过滤 `valid_to`，**不看 `state`**，archived 条目靠「分数低」侥幸躲开 | `_apply_tiers` 增加 `state == "archived"` 过滤（仍只在 `USE_CUSTOM_SCORING=1` 时生效，保持 baseline 等价） |
| M7-7 | `test_consolidate.py`、`scoring.py:4`、`TODO.md` | ①`test_consolidate` case B 的注释写 τ=14，τ 改 21 后 60 天 score=0.172>0.05，**这条断言已经测不到它声称的分支**；②`scoring.py` 模块 docstring 还写 14/30；③`TODO.md` 尾部有一段粘贴污染（24/25/26 重复 + 字面 `"` 与 `\n `） | case B 改 88 天（score=0.0455<0.05 且 88<90），并加一条断言「确实低于 COLD_SCORE」；docstring 与 TODO 修正 |

新增测试：`/home/m0_baseline/test_m7_fixes.py`（覆盖 M7-1/2/3/4/5/6，共 **31 项**）。

首轮实测抓到一处真 bug：`_mark_over_capacity` 把「本次新标记数」直接和 `excess` 比，没扣掉已标记的，
cap=5 加到第 7 条时会标 3 条（应 2 条），且随追加持续放大。已修为「先数已有、只补差额」，
并加了一条 `9 条 / cap 5 -> 恰好 4 条` 的断言锁住它。

---

## 2. 交付报告里需要同步改的两处措辞

**§4 规格表「硬删除」一行**，建议改为：

> 硬删除：自动流程中不允许。mid_term 容量超限 → `over_capacity` 标记 + 移出热段堆（`rebuild_heap` 持久跳过，可 `clear_over_capacity` 解除）；
> long_term 知识库同理（超过 `knowledge_capacity` 只打标记，**不再依赖 deque(maxlen) 静默丢弃**）。
> 读盘失败时原文件改名保留为 `*.corrupt-*` 并禁止写盘。整理层的 `valid_to` 有 `--restore` 撤销入口。

**§5.1 的 τ 结论**，建议把「标定得到 90」降级为：

> 网格只能证伪过短：τ_kn=30 在 120 天档崩到 6/12，≥60 后稳定在 11/12，**60 与 365 无法区分**，故 90 取「修好症状的最小整十值」，不是最优解。
> 另：365 天档里 `τ_kn=180 + τ_chg=180` 给 11/12 @41.9% 压缩，优于交付值的 6/12 @59.4%，该支配解需在下一轮真实数据标定时一并评估。
> episode 14→21 的唯一证据（45 天档「作文骨架」掉题）来自 `TODO#27` 已声明偏严的判据，**判定为不足以支撑改参**，本轮保留 21 但标注为待复核。

---

## 3. 前台跑法（推荐逐条看，别用后台队列）

```bash
cd /home/m0_baseline
for f in test_scoring.py test_tiers.py test_consolidate.py test_archive.py \
         test_m3_idempotent.py test_m5_no_hard_delete.py test_m5b_fixes.py \
         test_m6_identity_volatility.py test_m6_tau_defaults.py test_m7_fixes.py; do
  echo "===== $f"
  /usr/bin/python3 "$f" 2>&1 | tail -4
done
```

预期：9 套老测试保持不变（`test_consolidate` 从 36 → 37 项，因为 case B 补了一条断言）；
`test_m7_fixes.py` 全绿。

想只看失败的：

```bash
cd /home/m0_baseline
for f in test_*.py; do /usr/bin/python3 "$f" >/tmp/$(basename $f).log 2>&1 || echo "FAIL $f"; done
```

---

## 5. 评估口径重做（M7-E，2026-11-01）

> **本节数字是 P1 之前的旧口径，已被取代。** P1 把「遗忘」拆成三种机制之后，
> 49.4% 漏降大部分归因为标注错（把 supersede 类标成了 decay 类），闸门回到 0/37。
> 最新结论只在 `FORGETTING_BETA.md`；底座部分见 `VERIFIED_BASE.md`。

> **本节数字是 P1 之前的旧口径，已被取代。** P1 把「遗忘」拆成三种机制之后，
> 49.4% 漏降大部分归因为标注错（把 supersede 类标成了 decay 类），闸门回到 0/37。
> 最新结论只在 `FORGETTING_BETA.md`；底座部分见 `VERIFIED_BASE.md`。

原评估的四个毛病，逐条改掉（工具：`/home/m0_baseline/tier_eval/{eval_tiers.py,tier_labels.json}`）：

| 原口径 | 现口径 |
|---|---|
| 两个半指标**加权求和**，调参时互相抵消 | **分级**：降档半漏降一条即不合格（闸门）；过闸后才比保持半 |
| 只报一个“准确率” | 保持半报**错误降率**，降档半报**漏降率**，另单列超降率（不进判定） |
| 判“记不记得”（关键词在不在） | 判**期望档位**（full/gist/none），与三档输出粒度对齐 |
| 标注随时可改 | `tier_labels.json` 带 sha256，改动后脚本**拒绝运行**（除非 `--i-know-labels-change`） |

标注策略（写死，不是每格凭感觉）：`D` = 该条信息的语义寿命（天），
`age≤2D/3 → full`，`2D/3<age≤D → gist`，`age>D → none`，`D=null → full`。
29 条标注 × 8 个老化档（0/45/90/120/180/240/300/365）= 232 格，其中保持半 145 格、降档半 87 格。

### 结果

| 参数组 | 闸门（漏降） | 保持半错误降 | 超降（参考） |
|---|---|---|---|
| **交付态 21/90/90/180** | **43/87 = 49.4% → 不合格** | 8.3% | 13.8% |
| 网格最优 28/120/28/60 | 0/87 通过 | **3.4%** | 11.5% |

网格 2016 组（ep 6 × kn 6 × pref 7 × chg 8）：过闸门 72 组，可行域为
`ep∈{18..35}、kn∈{90,105,120}、pref∈{24,28}、chg∈{48,60}`。
72 组里保持半错误降最低 3.4%，且这 3.4% 全部落在**学年级的 changing identity**（升学考试/住址/任课老师），
是“一个 type 一个 τ”表达不了造成的，不是调参能消掉的残差。

### 两个结构性结论（比“哪个值更好”重要）

1. **交付态在降档半上一半没降**：43 格里 preference 全部“该糊没糊”（90 天仍然 full），
   changing identity 同样（180 天仍然 full）。这正是原报告里“365 天档 6/12 也符合直觉”掩盖掉的方向——
   那条只看了“还剩下什么”，没看“该糊的糊了没有”。
2. **changing identity 仍需再拆**：单条可行 τ 区间为 seat[56,74]、goal[49,64]、teacher[84,89]、
   residence[90,101]、exam[98,118]，**交集为空**（下界最大 98 > 上界最小 64）。
   即“学期级”（座位/目标）与“学年级”（班级/升学考试/住址/老师）不能共用一个 τ。
3. **gist 带只有约 0.92τ 宽**（0.30/0.12 = 2.5 倍，`ln2.5≈0.92`）。
   所以标注若要求相邻两个老化档都落在 gist，单条就可能无解——`badm_pref`（D=120，要求 90d 和 120d 都 gist）即如此。
   这不是标注写错，是**档位带宽本身太窄**，要么加密老化档，要么放宽 gist 上界。

### 处置

**没有改生产 τ。** 这轮的产出是“口径”，不是“参数”：`D` 表与 42 条语料都是自定的，
拿它直接覆盖计划单的 τ，等于重复上一轮被批评过的错误（用自造标准当证据）。
要动 `schema.TYPE_TAU_DAYS`，先确认这张 D 表（尤其 preference=120、changing=半年/一年半 两处判断），
并接受 changing identity 需要再拆一档（或把寿命下沉为字段）。

---

## 6. P0/P1 追加（2026-11-01）

| # | 改动 | 位置 |
|---|---|---|
| P0-1 | `valid_to` / `state=archived` 过滤**移出打分开关**，改为 `_drop_invalid()`，关开关也过滤 | `retriever.py` |
| P0-2 | `test_m3_llm.py`（五步端到端，真 LLM）进套件；无 key 记 SKIP，不冒充通过 | `test_m3_llm.py` |
| P1 | 遗忘拆三机制：decay / supersede / freeze；`mechanism_of()`；supersede 类 `tau=None` | `scoring.py` |
| P1 | `lifespan_days` 字段（寿命下沉，不再新增 type）；超期打 `stale_since` + 审计 `action=stale` | `scoring.py` / `consolidate.py` |
| P1 | cold 迁移只对 decay 类生效（supersede/freeze 不按时间淘汰） | `consolidate.py` |
| P4 | 文档分版：`VERIFIED_BASE.md`（已验证底座）/ `FORGETTING_BETA.md`（遗忘策略 beta） | 本目录 |

## 7. 仍然没做的（明确边界）

1. **τ 没重标**（口径已就绪，参数未动）。§5 的闸门可以证伪“降得太少”，也能用超降率看到“降得过头”，
   但 `D` 表与语料仍是自定的，所以只当“可行域”用，不当“证据”用。
2. **gist 档现在被度量了**（§5 的 232 格里 gist 是独立期望档），但由此暴露新问题：
   gist 带宽只有 0.92τ，跨档标注容易无解，见 §5 结论 3。
3. **`dedupe` 的 7 天窗口**没动：超过 7 天的同 id 重复副本不会被清，这是原有设计，改它会影响幂等测试的口径。
4. **A/B 双写**没做：`run_consolidate` 仍会无条件重写 4 个文件，只是现在每份都是原子写。
