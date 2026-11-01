"""一次性回填：给已有 identity 条目补 volatility 字段（M6）。

背景：M6 把 identity 拆成 stable（永不衰减）/ changing（有限 τ=90 天）。
新写入的数据由打分器直接产出 volatility；历史数据没有这个字段，按
scoring.guess_volatility 的关键词规则回填。

规则与限制：
- 只动 type == "identity" 的条目；其它类型一律 stable（τ 由 type 决定）。
- 已有合法 volatility 的条目不动 → 可重复运行（幂等）。
- 不删除、不改 state/score/文本，只加一个字段。
- 回填是规则判断，可能有误判；`weekly_spotcheck.py` 会把这些条目列出来供人工核对。

用法：
  python3 backfill_volatility.py --data data/data --dry-run
  python3 backfill_volatility.py --data data/data
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import argparse
import os
import sys

HERE = REPO
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from memoryos import scoring as S
from memoryos import consolidate as C


def paths(data_dir, user_id, assistant_id):
    return {
        "mid": os.path.join(data_dir, "users", user_id, "mid_term.json"),
        "long_user": os.path.join(data_dir, "users", user_id, "long_term_user.json"),
        "long_asst": os.path.join(data_dir, "assistants", assistant_id, "long_term_assistant.json"),
    }


def iter_items(data, key):
    if key == "mid":
        return C.iter_mid_term_items(data)
    return C.iter_long_term_items(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--user-id", default="user_m0")
    ap.add_argument("--assistant-id", default="default_assistant_profile")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    total = {"identity": 0, "filled": 0, "stable": 0, "changing": 0, "kept": 0, "other": 0}
    details = []
    for key, path in paths(a.data, a.user_id, a.assistant_id).items():
        if not os.path.exists(path):
            continue
        data = C.load_json(path)
        changed = False
        for item in iter_items(data, key):
            typ = item.get("type")
            if typ != "identity":
                total["other"] += 1
                continue
            total["identity"] += 1
            if S._clean_volatility(item.get("volatility")) == item.get("volatility") and item.get("volatility"):
                total["kept"] += 1
                continue
            vol = S.guess_volatility(item)
            total[vol] += 1
            total["filled"] += 1
            changed = True
            details.append((key, str(item.get("id"))[:16], vol,
                            " ".join(str(item.get("text") or item.get("summary") or "").split())[:48]))
            if not a.dry_run:
                item["volatility"] = vol
        if changed and not a.dry_run:
            C.save_json(path, data)

    print(f"identity 条目 {total['identity']} 条：回填 {total['filled']}"
          f"（stable {total['stable']} / changing {total['changing']}），已有合法值 {total['kept']} 条；"
          f"非 identity {total['other']} 条未动")
    for key, mid, vol, text in details:
        print(f"  [{key}] {mid:<18} -> {vol:<9} {text}")
    if a.dry_run:
        print("（dry-run，未写盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())