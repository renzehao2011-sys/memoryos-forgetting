# TODO（延后项记录）

计划单第 12 节第 5 条：任何「顺手优化」一律不做，只记录。

## 来自 M2 执行过程

1. **τ 与阈值需要调**：真实打分数据下，45 天压缩 0.2%、120 天压缩 14%，三档几乎不触发。LLM 把 10 条里 8 条判成 `identity`(τ=∞) 或 `preference`(τ=90)。M4 调参范围，可能需要收紧 identity 判定或下调 τ。
2. **默认 importance 下遗忘太快**：importance 全默认 3 时，episode 60 天归零、knowledge 65 天归零，120 天老化数据召回从 5/10 掉到 2/10。
3. **打分 prompt 未做方差测试**：计划单 §11 要求同一批数据跑 3 次看方差，尚未做。
4. **`state` 迁移未做**：`hot → cold → archived`（§5.3）与审计日志（§5.4）属 M3。
5. **原 heat 淘汰仍在跑**：`compute_segment_heat` 与 M2 打分并存，M3 摘掉。
   → **M5a 已处理淘汰侧**（2026-11-01）：`mid_term.evict_lfu()` 不再 `sessions.pop`，改为打 `over_capacity` 标记 + 从热段堆摘除，state 仍只由整理层 §5.3 授权。
   → **heat 的升级触发侧有意保留**：`compute_segment_heat` 只用于"何时把中期摘要升进长期画像"，摘掉会让画像永不更新（与计划单 §2「不改三层结构与流转」冲突）。§6.2 字面上要求"整段摘出"，实际只摘了淘汰阈值那一半，**这是已知偏差，待确认**。
6. **gist 标题用 `type · 相对时间`**，没有真正的语义标题。原数据没有标题字段，做语义标题需要额外 LLM 调用，暂不做。
7. **打分批量上限 12 条**：为防 JSON 输出被 max_tokens 截断。记忆量大的场景需要确认这个上限是否够。

## 来自 M1 执行过程（仍未处理）

8. `used_count` 与 `N_visit` 会漂移：迁移时是快照拷贝，之后 recall 只累加 `N_visit`。M2 已让召回刷新 `used_count`，但历史数据的漂移不会自动修复。
9. 已迁移的旧数据里 `type` 多为 `episode`/`knowledge`，真实分类只有 M2 之后写入的数据才有。

## 来自 M4 执行过程（2026-11-01，结论见 `/home/m0_baseline/M4_REPORT.md`）
> 第 1、2 条已在 M4 给出结论：**不标定 τ 与阈值，冻结默认值（episode 14 天 / FULL 0.30 / GIST 0.12）**。理由不是衰减设计有误，而是语料成分、gist 对短文本的负收益、语料量三者共同封死了压缩率上限（实测 14.1%，理论上界 29.6%）。
14. **gist 对短条目是负收益**：`make_gist` = 标题「模糊记忆 · {type} · {相对时间}」+ ≤24 字首句，对 29–51 字的短记忆渲染出 49 字符，比原文更长或持平（实测 49 vs 51）。短条目应直接沿用原文，或按原文长度动态决定是否套标题。
   → **M5c-3 已处理**：`make_gist` 增加短条目保护，渲染长度超过原文时退化为「【模糊记忆·rel】+ 裁剪首句」，保证不比原文长且档位仍可辨认；实测 29 字条目由 49→29。
15. **`_persist_used()` 每次召回落盘 4 个文件**：稳定口径下占 13ms（M2 25ms vs baseline 17.5ms，屏蔽落盘后 12ms）。应改延迟写 / 合并写。
   → **M5c-1 已部分处理**：改为只写被 touch 的文件（mid / user / assistant），空列表跳过。实测该语料下仍要写 mid+user 两个文件，延迟从 25ms 未明显下降；要再降需接受「批量延迟落盘可能丢最后几次 used_count」，本次未做。
16. **identity 里混入会过期的事实**：实测 6 条 identity 中「某班 / 某地 / 升学考试前 3%」属阶段性事实却拿 τ=∞。应拆「不可变身份」与「阶段性事实」，后者给 knowledge 30 天或 preference 90 天。
17. **打分误判两类**：① 助手侧推理记录（`Assistant inferred skill transfer (instrument→instrument2)`）被判 `preference`，带 inferred 的推测内容入了库；② 含「最近」的状态句（一项球类运动）被判 `preference`(τ=90)。需在打分提示词里约束。
18. **召回侧不过滤 `valid_to`**：`retriever.py` 无任何 `valid_to` 判断，被 M3 合并掉的条目仍会被召回。
   → **M5c-1 已处理**：`retriever._apply_tiers` 开头过滤 `valid_to` 非空条目（仅打分开启时生效，`USE_CUSTOM_SCORING=0` 保持 baseline 等价）。端到端验证：同 id 双副本整理后每个 `page_id` 只召回 1 次。
19. **标定 τ 前需扩语料**：现状 18 条 / 4 个唯一事实 / 8 个 page 只有 4 种文本，36 组网格只产出 3 种结果，没有分辨率。建议 ≥30 条独立事实且覆盖多 type。
20. **测量口径两条硬坑（已修正工具，勿回退）**：① `compare_tiers.py` 逐题串行会被 `_touch_used` 刷新 `last_used_at`，必须每题恢复到快照（见 `compare_tiers_v2.py` 的 C 组）；② 延迟必须取进程充分预热后的稳定值，前几遍 baseline 会被冷启动拖到 190–230ms，旧 `M2_REPORT` 的「+25%」即由此而来（稳定值 baseline 17.5ms / M2 25ms / 屏蔽落盘 12ms）。

## 来自 M5 执行过程（2026-11-01）
21. **dedupe 会出现"自己和自己合并"**：`page_b2805b69` 这类同一 `page_id` 的两份副本进入同一 layer 池后，两两比较相似度 1.000，被吸收方的 `merged_into` 就写成它自己的 id（审计日志：`与 page_b2805b69 相似度 1.000，内容并入保留方`，`kept_id` 同 id）。数据没被删（数量守恒测试通过），但语义不对，且会让 `valid_to` 落到"看起来还该有效"的条目上。修法候选：去重前先按 `id` 归并（同 id 视为同一条，不互比），或用 `(id, index)` 区分实体。未动手。
   → **M5c-2 已处理**：同 id 视为重复副本，保留 pool 里首份、另一份写 `valid_to`（不删除），审计记 `dedup_reason=same_id_duplicate` 且 reason 写明「同 id 重复副本」。真实数据 merged=4、二次运行 merged=0（幂等）。
22. **`over_capacity` 标记目前不进整理报告**：M5a 只改了 `mid_term.py`（一次只改一个模块），`run_consolidate.py` 的报告里还没有这一项统计，出口暂时靠 `weekly_spotcheck.py` 的 C 栏。若要进报告需再改 `run_consolidate.py`。
   → **M5c-5 已处理**：`run_consolidate.py` 输出新增 `over_capacity_total` / `over_capacity_ids`。
23. **`weekly_spotcheck.py` 需要排期**：脚本已就绪（`/home/m0_baseline/weekly_spotcheck.py`，只读，输出 `results/spotcheck_YYYYWW.log`），但计划单 §11 的"每周一次"还没挂到任何定时器上。可选：复用已 disable 的无人值守工作流，或手动每周跑一次。
   → **已处理**：工作流「MemoryOS·每周冷门记忆抽检」（ID `22d2f518-6a60-434d-961f-ea5e07fb5f41`）已创建并启用，interval 7 天 + 手动触发，首次执行 SUCCESS。

## P3/P4（2026-11-01，明确不做 / 先说好）

31. **A/B 双写**：`run_consolidate` 仍无条件重写 4 个文件（只是每份都原子写）。**记录不做**——
    改它会动幂等测试口径，收益不足以抵消风险。
32. **dedupe 7 天窗口**：超过 7 天的同 id 重复副本不会被清。**记录不做**——这是原设计，
    “整理后每个 id 只召回一次”只在当次窗口内成立，已写进 FORGETTING_BETA §6。
33. **P3 止损线（还没到执行条件）**：真实记忆重标后，若近端（0–45 天）压缩率仍 <15%，
    就把 `USE_CUSTOM_SCORING=0`，只留底座。+42% token / +43% 延迟不为未验证收益长期付费。

## 环境侧

10. `numpy 2.5.4` 与 `requirements.txt` 声明的 `numpy==1.24.*` 不一致。
11. `fsspec` 被 FlagEmbedding 从 2026.9.0 降到 2026.7.0。
12. bge-m3 有 3.2GB 半成品缓存在 `/root/.cache/huggingface/hub/models--BAAI--bge-m3/`，未设 `HF_TOKEN`，未清理。
13. `/home` 已用 89%，M3 若引入审计日志需评估空间。

## M6 已处理（2026-11-01）
24. **identity 拆分 / 打分误判两类**（原 24 前半）：identity 已拆 stable / changing（`volatility` 字段，changing τ=180 天），
    打分提示词加了 volatility 两档并要求“拿不准选 changing”；“助手推理记录被判 preference”仍未动。
25. **扩语料再标 τ**（原 25）：已造 42 条语料 + 12 题，重标后 episode 14->21、knowledge 30->90，changing identity=180。
    依据见 `M6_REPORT.md` §3。旧语料（18 条）仍可用 `compare_tiers_v2.py` 复现 M4 数字。
26. **gist 极短条目**（原 26）：已改为“【模糊记忆】+ 原文”，膨胀上限 6 字，正文不丢。

## M6 后仍未处理（2026-11-01 复核）
27. **gist 评测判据偏严**：12 题里有 1 题要求三个关键词全中，而 gist 只给首句。属评测集问题，不是系统缺陷。
28. **`volatility` 历史回填靠关键词**：`班级/城市/成绩/住址` → changing，`最高级/出生/姓/母语` → stable。会误判，已用 `weekly_spotcheck.py` 抽样人工核。
29. **语料仍是脚本编的 42 条**：比 M4 的 18 条强，但不是真实使用分布。真实跑一段时间后应拿真实记忆重标一次。
30. **“助手推理记录被判 preference 并入库”**（原 24 后半）：提示词已加 volatility，但“推断内容不该入库”仍未约束。
