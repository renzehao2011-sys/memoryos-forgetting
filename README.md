# MemoryOS 遗忘改造（已终止的实验）

> **一句话：给 MemoryOS 加过一套「按寿命衰减 + 三档召回」的遗忘层，做完了、测过了、最后用真实数据判它不合格，已经停用。**
>
> 这个仓库留下的不是一个可用的遗忘算法，而是三样经过验证的东西、一份带数字的判决书，以及一套「下次不要再犯同样错」的测试与工具。

---

## 1. 这是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 一个**打过补丁**的 MemoryOS MCP 子项目（上游 `BAI-LAB/MemoryOS`，Apache-2.0） | 上游官方版本 |
| 一套**有测试守着**的记忆底座：不硬删除、可回退、失效事实不召回、原子写 | 一个还在调参的项目 |
| 一份**真实数据判定为负**的实验记录（含读数、判据、失效原因） | 一个能直接用的"智能遗忘"方案 |
| 可复现的评估工具链（标注冻结 → 跑分 → 两半分开报） | 一个跑分排行榜 |

**当前默认行为：遗忘层是关的。** `is_enabled()` 在 `USE_CUSTOM_SCORING` 读不到时返回 `False`——
安全默认落在"关"这一侧，不靠"记得配环境变量"。

---

## 2. 判决（2026-11-11）

用 42 条**真实对话原文**（单月、单人，从 313 条真实发言里挑出，未改写）重标后，交付参数
（`episode=21 / knowledge=90`）的读数是：

| 项 | 数值 | 判定 |
|---|---|---|
| 闸门（降档半）漏降 | **35 / 132 = 26.5%** | **不合格**（一条都不许漏） |
| 保持半错误降 | 4 / 204 = 2.0%（decay 口径 4/60 = 6.7%） | 仅记录，闸门未过不比 |
| 近端(0–45d)压缩率 | **0.0%**（止损线 15%） | 不达标 |
| 网格 2016 组 | **过闸门 0 组** | — |

**为什么不是"再调调就好"**：评估器要求同一条标注同时满足"2D/3 还是 full、到 D 就该 none"，
留给 τ 的窗口只有 `[0.29D, 0.31D]`（约 7%）；而真实数据里同一 type 的语义寿命差 6–8 倍。
→ **"一个 type 一个 τ"在真数据上无解，是数学结论，不是调参失败。**

保留的资产只有三样：**底座**、**覆盖（`valid_to`）**、**处置单**。其余（τ / decay / 三档）停用。

想重启？先过一个新判据：**参数不超过五个的模型，能预测 42 条个体档位**。调参不算重启。
细节见 [`docs/FORGETTING_BETA.md`](docs/FORGETTING_BETA.md) 顶部与 §3.1 / §3.2。

---

## 3. 目录

```
.
├── memoryos/                 打过补丁的包（scoring / retriever / consolidate / archive …）
├── run_consolidate.py        每日整理任务：去重 → 事实失效 → 写小结 → 报告（四步）
├── run_disposition.py        每周人工处置单：容量超限 + 待归档，只列不处置
├── config.example.json       配置样例（不含任何密钥）
├── docs/
│   ├── VERIFIED_BASE.md      底座：每条都有测试守着
│   ├── FORGETTING_BETA.md    ★ 判决书：墓碑 + 真实数据读数 + 为什么判负
│   ├── M7_FIXES.md           改动清单（§5 是旧口径，已被推翻）
│   ├── TODO.md               延后项与明确不做项
│   └── HANDOFF-2026-11-01.md 交接文档原文（含踩过的坑）
├── tools/
│   ├── tier_eval/            评估器（M7 口径，历史）
│   ├── tier_eval_p2/         评估器（P2 真数据口径，判决用的就是它）
│   ├── p2/                   真数据建集 + 标注填充
│   ├── weekly_spotcheck.py   每周冷门记忆抽检
│   ├── make_aged_data.py     造老化档
│   └── m6_grid.py            历史网格（结论已过期，仅存档）
├── tests/                    13 套；不依赖 LLM 的 11 套可用 run_all.sh 跑
└── data/                     数据不入库，见 data/README.md
```

---

## 4. 快速开始

```bash
# 依赖（Python >= 3.10）
pip install -r requirements.txt

# 1) 整理任务（永远在数据副本上跑）
cp -r /path/to/data /tmp/_t
python3 run_consolidate.py --data /tmp/_t --steps dedupe,expire --no-llm

# 2) 人工处置单（只读，不改数据）
python3 run_disposition.py --data /tmp/_t --out 处置单.md

# 3) 测试
./tests/run_all.sh                 # 不依赖 LLM 的部分
python3 tests/test_m3_llm.py       # 需 DASHSCOPE_API_KEY
```

**LLM 相关**：`expire`（事实失效）与 `summary`（日小结）需要 OpenAI 兼容接口。密钥只走环境变量：

```bash
export DASHSCOPE_API_KEY=...
export OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENAI_MODEL=qwen-plus        # 换模型前先验证它支持 tool call
```

---

## 5. 三条纪律（这项目是靠它们才没跑偏）

1. **不加权求和。** 降档半（漏降）是闸门，漏一条即不合格；保持半（错误降）是成本。两者不能相加成一个"准确率"。
2. **标注先冻结再跑。** `tier_labels.json` 带 sha256，改了哈希评估器直接拒绝运行（`--i-know-labels-change` 是逃生门，不是日常开关）。
3. **边调参边改标注 = 自己给自己发答案。** 要改标注就重新 `--freeze`，并写明理由。

另外两条结构性约束：**不许为不同寿命新增 type**（用 `lifespan_days` 字段）；
**不许用自动优化器搜 τ**（网格扫一遍，能解释比"分高"重要）。

---

## 6. 已验证的底座（不随遗忘层一起废）

- **不硬删除**：容量超限只打 `over_capacity` 标记，条目与页面全部保留
- **可回退**：`valid_to` 有 `--restore`，审计记 `action=restore`，重复撤销幂等
- **失效不召回**：P0 起与开关解耦，`USE_CUSTOM_SCORING` 关掉也照样过滤 `valid_to` / `archived`
- **故障不丢数据**：`tmp + fsync + os.replace` 原子写；读盘损坏改名保留并拒绝写盘
- **幂等与守恒**：整理任务连跑两次，数据文件逐字节一致、审计不增长
- **可审计**：每行 `ts / id / action / old_score / new_score / reason`

每一条都能用一句命令复现失败，明细见 [`docs/VERIFIED_BASE.md`](docs/VERIFIED_BASE.md)。

---

## 7. 复现那次判决（P2）

```bash
cd tools/tier_eval_p2
python3 make_label_sheet.py --sheet --data <你的数据目录> --out sheet_p2.tsv
# 人工填 mechanism / D / lifespan_days / why（decay 必须填 D）
python3 make_label_sheet.py --check sheet_p2_filled.tsv
python3 make_label_sheet.py --emit  sheet_p2_filled.tsv
python3 eval_tiers.py --freeze      # 冻结 sha256
python3 eval_tiers.py               # 读法：先看闸门，再看两半
```

`tools/p2/` 是当时从真实对话生成待标集的脚本（`p2_build_real.py`）与标注填充（`p2_fill_labels.py`）。
**注意：它们依赖的数据没有入库**（见下节）。

---

## 8. 什么没入库，为什么

**`data/` 为空是故意的。** 当时的评估集由真实个人对话构成（成绩、学校、家人、聊天原文），
公开等于把日记发到网上，所以：

- 不入库：`data_corpus_m6`、`data_p2_real`、`tier_labels*.json`、`sheet*.tsv`、审计日志、会话库备份、`make_corpus_m6.py`（内置语料原文）
- 入库：代码、文档、评估器、测试、标注规则（`make_label_sheet.py` 抬头，不含文本）

代价说清楚：**没有数据快照，测试跑不全**。读 `data/snapshot` 的用例会失败——这是取舍，不是 bug。
要复现请用自己的数据，规则见 [`data/README.md`](data/README.md)。

`docs/` 是从工作笔记收进来的，**导出时已统一匿名化**：姓名、城市、学校/班级、
升学成绩、具体爱好与设备都换成了占位词（`用户A / 某地 / 某班 / 一项动手类爱好` …）。
规则集中在 `build_export.py` 的 `SANITIZE` 表里，随导出重跑，不需要手改文档。

原始笔记留在本地，不在这个仓库里。

---

## 9. 已知边界（别当没这回事）

1. **评估器对 supersede / freeze 不可证伪**：这两类期望恒为 `full`，系统恒给 `full`，
   于是 42 条里 18 条（144 格）永远通过，把"保持半"的分母从 60 稀释到 204。
2. **gist 带宽只有约 0.92τ**，跨两个老化档的标注单条可能无解（评估器会剔除这类标注）。
3. **`recompute` 已从整理任务移除**：打分停了，每天重算只是空跑并往审计里灌记录；
   传 `--steps recompute` 会显式 `rc=2` 拒绝，不留静默空跑。
4. **处置单需要有人看**：每周工作流会生成一张单子，但"我的决定"那列永远留空——
   机制可以自动出单，不能替人点头。

---

## 10. 与上游的关系 / 许可

本项目基于 [BAI-LAB/MemoryOS](https://github.com/BAI-LAB/MemoryOS)（Apache-2.0）改造，
保留上游 `LICENSE`；改动范围见 [`NOTICE`](NOTICE) 与 [`docs/M7_FIXES.md`](docs/M7_FIXES.md)。
上游论文：arXiv 2506.06326。

> 一句话给三个月后的自己：**底座可以信，`docs/FORGETTING_BETA.md` 是判决书，不是说明书。**