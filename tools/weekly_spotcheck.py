"""M5-b 每周抽检（计划单 §11：每周抽 5 条冷门人工检查）。

做两件事：
  1. 列出「最接近冷」的 N 条 hot 记忆 —— 人工判断它们是不是被误埋；
  2. 列出 over_capacity 标记 —— M5a 起容量超限不再硬删，改打标记，需要人来决定
     是扩容还是走归档确认。
只读：不写任何数据文件，只写一份报告到 results/spotcheck_YYYYWW.log。

用法：
  python3 weekly_spotcheck.py                 # 默认看 data/data
  python3 weekly_spotcheck.py --data <dir> --n 5
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import argparse
import datetime
import json
import os
import sys

HERE = REPO
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from memoryos import scoring as S
from memoryos import consolidate as C


def load_bundle(data_dir, user_id="user_m0", assistant_id="default_assistant_profile"):
    """返回 [(level, item)]，只读。路径结构与 run_consolidate.py 一致。"""
    mid_p = os.path.join(data_dir, "users", user_id, "mid_term.json")
    lu_p = os.path.join(data_dir, "users", user_id, "long_term_user.json")
    la_p = os.path.join(data_dir, "assistants", assistant_id, "long_term_assistant.json")
    out = []
    if os.path.exists(mid_p):
        mid = C.load_json(mid_p)
        out += [("mid_term", it) for it in C.iter_mid_term_items(mid)]
    if os.path.exists(lu_p):
        for it in C.iter_long_term_items(C.load_json(lu_p)):
            out.append(("long_term_user", it))
    if os.path.exists(la_p):
        for it in C.iter_long_term_items(C.load_json(la_p)):
            out.append(("long_term_assistant", it))
    return out


def head(item, n=60):
    t = " ".join(str(S._plain_text(item) or "").split())
    return (t[:n] + "…") if len(t) > n else t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    now = datetime.datetime.now()
    year, week, _ = now.isocalendar()
    out_path = a.out or os.path.join(HERE, "results", f"spotcheck_{year}W{week:02d}.log")

    items = load_bundle(a.data)
    if not items:
        print(f"没有可读条目：{a.data}")
        return 1

    rows = []
    for level, it in items:
        score = S.compute_score(it, now=now)
        age = S.age_days(it, now=now)
        rows.append({
            "level": level, "id": str(it.get("id")), "type": it.get("type", "?"),
            "importance": it.get("importance"), "emotion": it.get("emotion"),
            "state": it.get("state", "?"), "score": round(score, 4),
            "age_days": round(age, 1) if age is not None else None,
            "used_count": it.get("used_count"),
            "valid_to": it.get("valid_to"),
            "over_capacity_since": it.get("over_capacity_since"),
            "text": head(it),
        })

    def line(r):
        return (f"  {r['state']:<8}{r['type']:<11}imp={r['importance']} emo={r['emotion']:<4}"
                f"score={r['score']:<7.4f}age={r['age_days']:<7}d used={r['used_count']:<4}"
                f"{r['level']:<19}{r['id'][:14]:<16}{r['text']}")

    hot = sorted([r for r in rows if r["state"] == "hot"], key=lambda r: r["score"])
    cold = [r for r in rows if r["state"] == "cold"]
    over = [r for r in rows if r["over_capacity_since"]]
    merged = [r for r in rows if r["valid_to"]]

    buf = []
    buf.append(f"MemoryOS 每周抽检　{now:%Y-%m-%d %H:%M}　（ISO {year}W{week:02d}）")
    buf.append(f"数据目录：{a.data}")
    buf.append("")
    buf.append(f"== 统计 == 总条目 {len(rows)}：hot {len(hot)} / cold {len(cold)} / "
               f"archived {sum(1 for r in rows if r['state'] == 'archived')}；"
               f"已失效 valid_to {len(merged)}；over_capacity {len(over)}")
    buf.append(f"  type 分布：{ {t: sum(1 for r in rows if r['type'] == t) for t in ('episode','knowledge','preference','identity')} }")
    buf.append("")
    buf.append(f"== A. 最接近冷的 {a.n} 条 hot（人工判断是否被误埋）==")
    buf.append(f"{'  state':<10}{'type':<11}{'':<8}{'':<13}{'':<11}{'':<9}{'level':<19}{'id':<16}text")
    for r in hot[: a.n]:
        buf.append(line(r))
    if not hot:
        buf.append("  （没有 hot 条目）")
    buf.append("")
    buf.append("== B. 当前 cold（确认后走 run_consolidate.py --confirm-archive）==")
    for r in cold:
        buf.append(line(r))
    if not cold:
        buf.append("  （没有 cold 条目）")
    buf.append("")
    buf.append("== C. over_capacity 标记（M5a：容量超限未删除，等人决定扩容还是归档）==")
    for r in over:
        buf.append(line(r) + f"  since={r['over_capacity_since']}")
    if not over:
        buf.append("  （无。会话数未超过 max_capacity）")
    buf.append("")
    buf.append("== D. 已失效 valid_to（只读确认，不删）==")
    for r in merged:
        buf.append(line(r) + f"  valid_to={r['valid_to']}")
    if not merged:
        buf.append("  （无）")
    buf.append("")
    buf.append("人工判定栏：A 里若有『这条不该冷』的，记下 id，后续把它的 importance 手动上调；")
    buf.append("　　　　　　C 里若有『该扩容』的，调 Memoryos(mid_term_capacity=...) 而不是删数据。")

    text = "\n".join(buf)
    print(text)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    json.dump(rows, open(out_path.replace(".log", ".json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n已写入 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())