"""每周人工处置单（容量驱动，不做预测）。

为什么要这个东西：M7 起，容量超限**不再删任何东西**，只打 `over_capacity_since` 标记。
标记打完就没人看，等于把问题从"悄悄删"换成"悄悄堆"。这份单子把标记搬成一张
**每周一张、只列不处置**的清单：人点头之前，一条都不动。

它不预测"什么时候该忘"——那一层（τ / decay / 三档）已被真实数据判死，见
`FORGETTING_BETA.md` §3.1。本单只回答一件事：**哪些东西因为容量超限而排队，等你处置。**

三个来源：
  1. mid_term 里被标 `over_capacity_since` 的会话（已经不进热段堆，但内容全在）
  2. long_term 里被标 `over_capacity_since` 的条目（仍在库里，不删不降档）
  3. archive 的待归档队列（`archive.list_pending`，本来就要人工 confirm）

用法：
    python3 run_disposition.py --data /path/to/data                  # 打印
    python3 run_disposition.py --data /path/to/data --out 处置单.md  # 写文件
    python3 run_disposition.py --data /path/to/data --format tsv

**只读**：本脚本不写任何数据文件、不写审计、不动 state。真正的处置仍走
`run_consolidate.py --confirm-archive id1,id2 --operator 你的名字` 或
`MidTermMemory.clear_over_capacity()` / `LongTermMemory.clear_over_capacity()`，
都要人显式点名。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "memoryos"))

# 直接按模块名导入，不走 `from memoryos import ...`：
# 包 __init__ 会拉进 Memoryos → sentence_transformers，冷启动约 30 秒。
# 这张单子是个每周定时任务，30 秒纯属浪费，且更容易撞超时。
import archive as AR            # noqa: E402  （内部会 import scoring，都是纯计算）


COLS = ["#", "来源", "标识", "位置", "标记时间", "原因", "规模", "现在的影响", "可以怎么做", "我的决定"]


def _load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _iter_mid_items(data):
    """与 consolidate.iter_mid_term_items 同口径：session 本身 + 里面的 page。"""
    for _sid, session in (data.get("sessions") or {}).items():
        yield session
        for page in session.get("details", []) or []:
            yield page


def _iter_long_items(data):
    for key in ("knowledge_base", "assistant_knowledge"):
        for entry in data.get(key, []) or []:
            yield entry


def collect(data_dir, user_id, assistant_id):
    u = os.path.join(data_dir, "users", user_id)
    a = os.path.join(data_dir, "assistants", assistant_id)
    rows = []

    mid = _load(os.path.join(u, "mid_term.json")) or {}
    for sid, sess in (mid.get("sessions") or {}).items():
        if not isinstance(sess, dict) or not sess.get("over_capacity_since"):
            continue
        pages = sess.get("details") or []
        rows.append({
            "来源": "mid_term 会话",
            "标识": sid,
            "位置": f"users/{user_id}/mid_term.json",
            "标记时间": sess.get("over_capacity_since", ""),
            "原因": (" ".join(str(sess.get("over_capacity_reason") or "").split()))[:120],
            "规模": f"{len(pages)} 页",
            "现在的影响": "已摘出热段堆（不会被正常召回），内容全部保留",
            "可以怎么做": "保留 / 解除标记（扩容后重新纳入） / 确认归档",
        })

    for label, path in ((f"users/{user_id}/long_term_user.json", os.path.join(u, "long_term_user.json")),
                        (f"assistants/{assistant_id}/long_term_assistant.json",
                         os.path.join(a, "long_term_assistant.json"))):
        doc = _load(path)
        if not doc:
            continue
        for key in ("knowledge_base", "assistant_knowledge"):
            for e in doc.get(key) or []:
                if not isinstance(e, dict) or not e.get("over_capacity_since"):
                    continue
                rows.append({
                    "来源": f"long_term 条目（{key}）",
                    "标识": e.get("id") or e.get("knowledge", "")[:24],
                    "位置": label,
                    "标记时间": e.get("over_capacity_since", ""),
                    "原因": (" ".join(str(e.get("over_capacity_reason") or "").split()))[:120],
                    "规模": "1 条",
                    "现在的影响": "仍在库里、仍可被召回（只打了标记，没降档、没删）",
                    "可以怎么做": "保留 / 解除标记 / 合并进同类条目",
                })

    tiers = []
    if mid:
        tiers.extend(("mid_term", it) for it in _iter_mid_items(mid))
    for path in (os.path.join(u, "long_term_user.json"), os.path.join(a, "long_term_assistant.json")):
        doc = _load(path)
        if doc:
            tiers.extend(("long_term", it) for it in _iter_long_items(doc))
    for p in AR.list_pending([it for _t, it in tiers]):
        rows.append({
            "来源": "archive 待归档",
            "标识": p.get("id", ""),
            "位置": f'{p.get("type", "")}/{p.get("state", "")}',
            "标记时间": p.get("pending_archive_since") or "",
            "原因": (" ".join(str(p.get("text") or "cold 到期，待人工确认").split()))[:120],
            "规模": "1 条",
            "现在的影响": "仍可召回（未归档）",
            "可以怎么做": "确认归档（--confirm-archive） / 驳回（reject_archive） / 先不动",
        })

    for i, r in enumerate(rows, 1):
        r["#"] = str(i)
        r["我的决定"] = ""
    return rows


def _render_md(rows, data_dir):
    out = [
        "# 记忆处置单（只列不处置）",
        "",
        f"- 数据目录：`{data_dir}`",
        "- 本单不预测遗忘、不改任何数据；每一行都要人写决定之后才动手。",
        "- 处置命令：`python3 run_consolidate.py --data <dir> --confirm-archive id1,id2 --operator 你的名字`；",
        "  解除容量标记：`MidTermMemory.clear_over_capacity()` / `LongTermMemory.clear_over_capacity()`。",
        "",
    ]
    if not rows:
        out.append("**本周没有待处置项。**")
        return "\n".join(out) + "\n"
    out.append("| " + " | ".join(COLS) + " |")
    out.append("|" + "---|" * len(COLS))
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")).replace("|", "/") for c in COLS) + " |")
    counts = {}
    for r in rows:
        counts[r["来源"]] = counts.get(r["来源"], 0) + 1
    out += ["", "小计：" + "，".join(f"{k} {v} 项" for k, v in counts.items())]
    return "\n".join(out) + "\n"


def _render_tsv(rows):
    head = "\t".join(COLS)
    body = ["\t".join(str(r.get(c, "")).replace("\t", " ") for c in COLS) for r in rows]
    return "\n".join([head] + body) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--user-id", default="user_m0")
    ap.add_argument("--assistant-id", default="default_assistant_profile")
    ap.add_argument("--out", default="", help="输出文件；不传则打印到 stdout")
    ap.add_argument("--format", default="md", choices=("md", "tsv"))
    ap.add_argument("--json", action="store_true", help="打印机器可读摘要")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data)
    rows = collect(data_dir, args.user_id, args.assistant_id)
    text = _render_md(rows, data_dir) if args.format == "md" else _render_tsv(rows)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"写出 {args.out}（{len(rows)} 项）")
    else:
        sys.stdout.write(text)

    if args.json:
        summary = {}
        for r in rows:
            summary[r["来源"]] = summary.get(r["来源"], 0) + 1
        print(json.dumps({"total": len(rows), "by_source": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
