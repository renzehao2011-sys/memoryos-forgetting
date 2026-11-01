"""生成「老化数据」：把 last_used_at / created_at 往前推 N 天，模拟时间流逝。

用途：M2 三档召回的真实效果验证。现有评估集的 seed 数据全是"今天"写入的，
Δt≈0 时三档永远不分叉，无法验证衰减。

只改时间字段，不动任何内容字段；输出到独立目录，不覆盖原数据。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import json
import os
import sys
from datetime import datetime, timedelta

SRC = os.environ.get("SRC_DIR", os.path.join(DATA_ROOT, "snapshot"))
DST = os.environ.get("DST", "data/data_aged")
SHIFT_DAYS = int(os.environ.get("SHIFT_DAYS", "45"))
USER_DIR = os.path.join(SRC, "users", "user_m0")
ASSIST_DIR = os.path.join(SRC, "assistants", "default_assistant_profile")

os.makedirs(os.path.join(DST, "users", "user_m0"), exist_ok=True)
os.makedirs(os.path.join(DST, "assistants", "default_assistant_profile"), exist_ok=True)


def shift(ts, days=SHIFT_DAYS):
    if not ts:
        return ts
    try:
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts
    return (dt - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def walk(obj, keys):
    """递归把指定键的时间戳往前推。"""
    changed = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                obj[k] = shift(v)
                changed += 1
            else:
                changed += walk(v, keys)
    elif isinstance(obj, list):
        for it in obj:
            changed += walk(it, keys)
    return changed


TIME_KEYS = {"last_used_at", "created_at", "valid_from", "timestamp",
             "last_visit_time"}

total = 0
for name in ("short_term.json", "mid_term.json", "long_term_user.json"):
    src = os.path.join(USER_DIR, name)
    dst = os.path.join(DST, "users", "user_m0", name)
    data = json.load(open(src, encoding="utf-8"))
    n = walk(data, TIME_KEYS)
    json.dump(data, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"{name}: shifted {n} timestamps")
    total += n

src = os.path.join(ASSIST_DIR, "long_term_assistant.json")
dst = os.path.join(DST, "assistants", "default_assistant_profile", "long_term_assistant.json")
data = json.load(open(src, encoding="utf-8"))
n = walk(data, TIME_KEYS)
json.dump(data, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"long_term_assistant.json: shifted {n} timestamps")
total += n

print(f"\n共推移 {total} 个时间戳，整体往前 {SHIFT_DAYS} 天 -> {DST}")

# 预览：分档分布
sys.path.insert(0, REPO)
from memoryos import scoring as S

stats = {}
for name, key in (("mid_term.json", "pages"), ("long_term_user.json", "knowledge_base")):
    path = os.path.join(DST, "users", "user_m0", name)
    data = json.load(open(path, encoding="utf-8"))
    items = []
    if "sessions" in data:
        for s in data["sessions"].values():
            items += list(s.get("details", []))
    items += list(data.get("knowledge_base", []))
    for it in items:
        t = S.tier_of(S.compute_score(it))
        stats[t] = stats.get(t, 0) + 1
print("分档分布（45 天前）:", stats)