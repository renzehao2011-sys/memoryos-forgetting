"""P2 标注工作台：把真实记忆导成待标表 → 校验 → 生成 tier_labels.json

为什么要有这一步：P2 的核心纪律是「先冻结标注意图，再看结果」。
如果标注意图只活在脑子里，调参时一定会偷偷改标准。所以：

   1. --sheet  从真实数据导出一张表，一行一条记忆，机制/寿命留空等人填
   2. --check  校验：机制必填、decay 必须有 D、覆盖四类、identity 覆盖 stable/changing
   3. --emit   生成 tier_labels.json（**此时还没冻结**，必须再跑 eval_tiers.py --freeze）

用法：
   python3 make_label_sheet.py --sheet --data data/data --out /tmp/sheet.tsv
   python3 make_label_sheet.py --check /tmp/sheet.tsv
   python3 make_label_sheet.py --emit  /tmp/sheet.tsv
   python3 eval_tiers.py --freeze

表列（TSV，可用任意表格软件编辑）：
   key            自动生成，不要改
   type           episode / knowledge / preference / identity
   volatility     identity 才有：stable / changing
   mechanism      必填：decay / supersede / freeze
   lifespan_days  supersede 类建议填（保质期）；decay 类可空（空=用 type 级 τ）
   D              decay 类必填（语义寿命，天）
   why            一句话理由（会写进 labels，供三个月后的人判断）
   text           记忆原文（只读，别改，用来绑定条目）
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))


import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(HERE, "tier_labels.json")
COLS = ["key", "type", "volatility", "mechanism", "lifespan_days", "D", "why", "text"]
MECHS = ("decay", "supersede", "freeze")
args_force = False
IDENTITY_KINDS = ("identity/stable", "identity/changing")
REQUIRED_COVER = ("episode", "knowledge", "preference")


# ---------------------------------------------------------------- 导出

def _text_of(item):
    ui = str(item.get("user_input") or "")
    ar = str(item.get("agent_response") or "")
    if ui or ar:
        return f"{ui} {ar}".strip()
    return str(item.get("knowledge") or item.get("text") or "").strip()


def iter_items(data_dir):
    mid_p = os.path.join(data_dir, "users/user_m0/mid_term.json")
    lu_p = os.path.join(data_dir, "users/user_m0/long_term_user.json")
    la_p = os.path.join(data_dir, "assistants/default_assistant_profile/long_term_assistant.json")
    n = 0
    if os.path.exists(mid_p):
        mid = json.load(open(mid_p, encoding="utf-8"))
        for _sid, sess in (mid.get("sessions") or {}).items():
            for page in sess.get("details", []) or []:
                n += 1
                yield f"p{n:03d}", page
    for p, key in ((lu_p, "knowledge_base"), (la_p, "assistant_knowledge")):
        if not os.path.exists(p):
            continue
        data = json.load(open(p, encoding="utf-8"))
        for e in data.get(key, []) or []:
            n += 1
            yield f"k{n:03d}", e


def cmd_sheet(args):
    rows = []
    for key, it in iter_items(args.data):
        text = _text_of(it)
        if len(text) < 6:
            continue
        rows.append({
            "key": key,
            "type": it.get("type") or "",
            "volatility": it.get("volatility") or "",
            "mechanism": "",
            "lifespan_days": "",
            "D": "",
            "why": "",
            "text": text.replace("\t", " ").replace("\n", " ")[:300],
        })
    out = args.out or os.path.join(HERE, "sheet.tsv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in rows:
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")
    print(f"写出 {len(rows)} 行到 {out}")
    print("下一步：填 mechanism（decay/supersede/freeze）；decay 填 D；supersede 建议填 lifespan_days")
    return 0


# ---------------------------------------------------------------- 校验

def read_sheet(path):
    with open(path, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    header = lines[0].split("\t")
    if header != COLS:
        raise SystemExit(f"表头不对。期望 {COLS}\n实际 {header}")
    rows = []
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) < len(COLS):
            parts += [""] * (len(COLS) - len(parts))
        rows.append(dict(zip(COLS, parts)))
    return rows


def validate(rows):
    errs, warns = [], []
    seen_kind = set()
    for r in rows:
        k, mech = r["key"], (r["mechanism"] or "").strip()
        if not mech:
            errs.append(f"{k}: mechanism 未填")
        elif mech not in MECHS:
            errs.append(f"{k}: mechanism={mech} 不合法（应为 {'/'.join(MECHS)}）")
        d = (r["D"] or "").strip()
        ls = (r["lifespan_days"] or "").strip()
        if mech == "decay":
            if not d:
                errs.append(f"{k}: decay 类必须填 D")
            else:
                try:
                    if float(d) <= 0:
                        errs.append(f"{k}: D 必须 > 0")
                except ValueError:
                    errs.append(f"{k}: D 不是数字（{d}）")
        if mech == "supersede" and not ls:
            warns.append(f"{k}: supersede 类建议填 lifespan_days（作为保质期，超期只打 stale）")
        if mech == "freeze" and (d or ls):
            warns.append(f"{k}: freeze 类不需要 D / lifespan_days")
        kind = r["type"] + ("/" + r["volatility"] if r["type"] == "identity" else "")
        seen_kind.add(kind)
    missing = [t for t in REQUIRED_COVER if t not in seen_kind]
    if missing:
        errs.append(f"覆盖不足：缺少 {missing}（每类建议 ≥3 条）")
    for kind in IDENTITY_KINDS:
        if kind not in seen_kind:
            errs.append(f"覆盖不足：缺少 {kind}")
    # 重复文本会让 match 命中多条，resolve() 会直接报错；在这一步就拦住
    seen_text = {}
    for r in rows:
        t = (r["text"] or "").strip()
        if t:
            seen_text.setdefault(t, []).append(r["key"])
    for t, ks in seen_text.items():
        if len(ks) > 1:
            errs.append(f"{'/'.join(ks)}: 文本完全重复，match 无法唯一定位"
                        f"（先跑 dedupe，或从表里删掉多余行）")
    counts = {}
    for r in rows:
        counts[(r["mechanism"] or "未填")] = counts.get((r["mechanism"] or "未填"), 0) + 1
    return errs, warns, counts


def cmd_check(path):
    rows = read_sheet(path)
    errs, warns, counts = validate(rows)
    print(f"共 {len(rows)} 行；机制分布：{counts}")
    for w in warns:
        print("  警告", w)
    for e in errs:
        print("  错误", e)
    if errs:
        print(f"\n不通过：{len(errs)} 条错误。修完再 --emit。")
        return 1
    print("\n通过。可以 --emit 生成 tier_labels.json（之后必须 --freeze）。")
    return 0


# ---------------------------------------------------------------- 生成 labels

def cmd_emit(path, ages, out_labels):
    rows = read_sheet(path)
    errs, warns, _ = validate(rows)
    if errs:
        print("校验不通过，拒绝生成：")
        for e in errs:
            print("  错误", e)
        return 1
    items = []
    for r in rows:
        mech = r["mechanism"].strip()
        d = r["D"].strip()
        ls = r["lifespan_days"].strip()
        item = {
            "key": r["key"],
            "match": r["text"][:40],
            "type": r["type"],
            "mechanism": mech,
            "why": r["why"] or "",
        }
        if r["type"] == "identity":
            item["volatility"] = r["volatility"] or "stable"
        if mech == "decay":
            item["D"] = float(d)
        elif mech == "supersede" and ls:
            item["lifespan_days"] = float(ls)
        items.append(item)

    if os.path.exists(out_labels) and args_force is False:
        cur = json.load(open(out_labels, encoding="utf-8"))
        if cur.get("_frozen_sha"):
            print(f"拒绝覆盖：{out_labels} 已冻结（sha256={cur['_frozen_sha'][:16]}…）。")
            print("  要覆盖请加 --force（旧标注建议先 cp 备份）")
            return 1
    old = {}
    if os.path.exists(out_labels):
        old = json.load(open(out_labels, encoding="utf-8"))
    doc = {
        "_doc": "由 make_label_sheet.py 生成。真实验证集（P2）。",
        "_policy": old.get("_policy", ""),
        "_ages": ages,
        "_frozen_sha": "",          # 留空：必须再跑 eval_tiers.py --freeze
        "items": items,
    }
    json.dump(doc, open(out_labels, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已写 {out_labels}（{len(items)} 条），_frozen_sha 为空")
    print("下一步：cd tier_eval && python3 eval_tiers.py --freeze")
    for w in warns:
        print("  警告", w)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true", help="导出待标表")
    ap.add_argument("--data", default=os.path.join(DATA_ROOT, "snapshot"))
    ap.add_argument("--out", default="")
    ap.add_argument("--check", default="", help="校验已填的表")
    ap.add_argument("--emit", default="", help="从已填的表生成 tier_labels.json")
    ap.add_argument("--ages", default="0,45,90,120,180,240,300,365")
    ap.add_argument("--out-labels", default="", help="默认写 tier_eval/tier_labels.json")
    ap.add_argument("--force", action="store_true", help="覆盖已冻结的标注")
    a = ap.parse_args()
    ages = [int(x) for x in a.ages.split(",") if x.strip()]
    if a.sheet:
        return cmd_sheet(a)
    if a.check:
        return cmd_check(a.check)
    if a.emit:
        global args_force
        args_force = a.force
        return cmd_emit(a.emit, ages, a.out_labels or LABELS)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())