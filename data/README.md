# 数据目录（内容不入库）

要复现评估，把数据放成下面这个样子：

```
data/
  snapshot/                 MemoryOS 数据快照（users/ + assistants/ 四件套）
  corpus_m6/                M6 时期的语料（可选，跑 tools/tier_eval/ 需要）
  p2_real/                  P2 真数据评估集（可选，跑 tools/tier_eval_p2/ 需要）
  room_db/app_database      Operit 会话库（可选，跑 tools/p2/p2_build_real.py 需要）
```

也可以用环境变量指到别处（优先级高于本目录）：

| 变量 | 含义 | 默认 |
|---|---|---|
| `MEMORYOS_REPO` | 仓库根 | 脚本自己往上找 |
| `MEMORYOS_DATA_ROOT` | 数据根 | `<repo>/data` |
| `MEMORYOS_DATA` | 数据快照目录 | `<DATA_ROOT>/snapshot` |
| `MEMORYOS_CORPUS` | 评估语料目录 | `<DATA_ROOT>/corpus_m6` 或 `p2_real` |
| `MEMORYOS_ROOM_DB` | Operit 会话库 `app_database` | `<DATA_ROOT>/room_db/app_database` |
| `MEMORYOS_WORK` | 测试用临时工作目录 | `/tmp/_*` |

## 为什么空着

当时的评估集是真实个人对话（成绩、学校、家人、聊天原文）。公开仓库里放这些
等于把日记发到网上，所以数据、标注文件、审计日志一律不入库。

代价：`tests/` 里读 `snapshot` 的用例会失败。这是取舍，不是 bug。

## 怎么自己造一份

1. 用 MemoryOS 正常跑一段时间，得到 `users/user_*/` 与 `assistants/*/` 四个 json；
2. 复制到 `data/snapshot/`；
3. `cd tools/tier_eval_p2 && python3 make_label_sheet.py --sheet --data <你的目录>`；
4. 逐行填 `mechanism / D / lifespan_days / why`，然后 `--check` → `--emit` → `--freeze`。

标注规则见 `make_label_sheet.py` 抬头，以及 `docs/FORGETTING_BETA.md` §3.1。