# 已验底座（VERIFIED BASE）

> 这一页只写**有测试守着**的部分。遗忘策略见 `FORGETTING_BETA.md`（beta，未验证）。
> 更新时间：2026-11-01　分支 `m1-schema`

判据：**下面每一条都能用一句命令复现失败**。没有测试的，不写在这里。

---

## 1. 不硬删除

| 场景 | 现在的行为 | 守住它的测试 |
|---|---|---|
| mid_term 会话数超限 | 打 `over_capacity_since` 标记，**条目与页面全部保留**；`rebuild_heap` 持久跳过它 | `test_m5_no_hard_delete.py`（22 项） |
| long_term 知识超限 | 打标记，不删；`load()` 也不再按 maxlen 截断 | `test_m7_fixes.py` A/A2 |
| 去重合并 | 被吸收方写 `valid_to`，不删；审计记 `dedup_reason` | `test_consolidate.py` H/I、`test_m5b_fixes.py` 4 |
| 归档 | 只认人工显式 id；模块内没有 `auto_archive` | `test_archive.py`（29 项） |

## 2. 可回退

- `valid_to` 有撤销入口：`restore_expired()` / `python3 run_consolidate.py --restore id1,id2`
- 保留 `merged_into` / `superseded_by` 作历史，另记 `restored_at` / `restored_by`
- 撤销写审计（`action=restore`），重复撤销幂等
- 测试：`test_m7_fixes.py` D（11 项）

## 3. 失效事实不会被召回

- **P0 起与 `USE_CUSTOM_SCORING` 解耦**：关掉开关也必须过滤 `valid_to` 与 `state=archived`
- 理由：关开关是排障用的，排障时看到的应该是真相，不是上个月的住址
- **2026-11-11 起开关默认关**：`USE_CUSTOM_SCORING` 读不到 / 为空 / 非真值一律按关（安全默认在关这边，不靠“记得带变量”）；显式 `=1` 才开
- 测试：`test_m5b_fixes.py` 3、`test_m7_fixes.py` E（含“关开关仍过滤”）

## 4. 故障不丢数据

- `save()` 全部改 `tmp + fsync + os.replace`（mid / long / short）
- 读盘损坏 → 原文件改名保留为 `*.corrupt-*`，并置 `_load_failed`，此后**拒绝写盘**
- 测试：`test_m7_fixes.py` B（4 项）

## 5. 幂等与守恒

- 整理任务连跑两次：4 个数据文件语义一致、审计不增长、条目数守恒
- 测试：`test_m3_idempotent.py`（13 项）、`test_consolidate.py` G/I
- **整理任务已摘掉 `recompute`**（2026-11-11）：四步 = 去重 / 事实失效 / 写小结 / 报告；
  传 `--steps recompute` 直接 rc=2 拒绝，不留“静默空跑”

## 6. 端到端（含真 LLM）

- dedupe → expire → summary → report 全跑通；第二次运行 `expired=0`、`skipped=already_exists`
  （recompute 已于 2026-11-11 退出整理任务；端到端在默认关（无环境变量）下通过）
- 测试：`test_m3_llm.py`（17 项，需 `DASHSCOPE_API_KEY`；无 key 时记 SKIP，不冒充通过）

## 7. 审计可追溯

- 每行 `ts / id / action / old_score / new_score / reason`（+ level / kept_id / similarity / dedup_reason / operator）
- 测试：`test_consolidate.py` K

## 8. 容量超限只出单、不动手

- `over_capacity_since` / 待归档队列 → `run_disposition.py` 每周一张处置单，**只列不处置**
- 单子每行留空 `我的决定`；人点头后才走 `--confirm-archive` / `clear_over_capacity()`
- 只读：跑完 4 个数据文件字节不变、不写审计、不改 `state`；没有待办时明说"本周没有待处置项"
- 测试：`test_disposition.py`（12 项，含冷启动 <5 秒）

---

## 当前测试账

| 套 | 项数 | 性质 |
|---|---|---|
| test_scoring / test_tiers / test_consolidate / test_archive | 30 / 26 / 37 / 29 | 机制 |
| test_m3_idempotent / test_m5_no_hard_delete / test_m5b_fixes | 13 / 22 / 38 | 数据与契约 |
| test_m6_identity_volatility / test_m6_tau_defaults | 23 / 14 | 类型与 lifespan |
| test_m7_fixes | 32 | M7 修复 |
| test_disposition | 12 | 处置单只读 + 冷启动 |
| test_m3_llm | 17 | 端到端（需 key） |

合计：**293 项**（不含 SKIP 情形）。跑法：`make test` 见 `M7_FIXES.md` §3。
