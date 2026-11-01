"""三档评估器（M7 评估口径重做）

设计纪律（按用户 spec）：
1. **不加权**。判据是分级的：
   - 闸门（降档半）：标注为 gist / none 的格，实际档位若“降得不够”（漏降），
     该组参数直接不合格。**一条都不许漏。**
   - 过闸之后，才比较保持半的错误降率（该记得的 full 掉了，这种错贵）。
2. **两半分开报**：保持半报错误降率，降档半报漏降率；另单列超降率（降得比该降的还狠），
   只作参考、不进判定——它不贵，但要让成本可见。
3. **标注的是期望档位**，不是“记不记得”。三档召回就必须三档评估，否则中间那档永远测不出来。
4. **标注先冻结**：`tier_labels.json` 里有 `_frozen_sha`，任何改动都会让本脚本拒绝运行
   （确需改标注时加 `--i-know-labels-change`，并说明理由）。

标注策略（写死在 labels 文件里，不是每格凭感觉）：
    令 D = 该条信息的「语义寿命」（天），
    age <= 2D/3        -> full   （真相还在，细节也该在）
    2D/3 < age <= D    -> gist   （记得有这回事，不必记得原句）
    age > D            -> none   （已经不该被翻出来）
    D = null           -> full  （stable identity，永不衰减）

用法：
    python3 eval_tiers.py --freeze                 # 只算并写入 sha256
    python3 eval_tiers.py                          # 按冻结标注评估（默认参数 + 网格）
    python3 eval_tiers.py --i-know-labels-change   # 标注确实要改时
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))


import argparse
import datetime
import hashlib
import itertools
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(HERE, "tier_labels.json")
CORPUS = os.path.join(DATA_ROOT, "p2_real")
sys.path.insert(0, os.path.join(REPO, "memoryos"))
import scoring as S  # noqa: E402

TS_FMT = "%Y-%m-%d %H:%M:%S"
TIERS = ("none", "gist", "full")
# 存活度：none=0 最糊，full=2 最完整。数值越大 = 降得越少。
ALIVE = {"none": 0, "gist": 1, "full": 2}


# ---------------------------------------------------------------- 标注

def load_labels():
    with open(LABELS, encoding="utf-8") as f:
        return json.load(f)


def labels_digest(doc):
    body = {"ages": doc["_ages"], "policy": doc["_policy"], "items": doc["items"]}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def expect_tier(D, age, mechanism="decay"):
    # P1：非 decay 类不会被时间淡掉，期望恒为 full（要它们消失只能靠覆盖/人工）
    if mechanism != "decay" or D is None:
        return "full"
    if age <= 2.0 * D / 3.0:
        return "full"
    if age <= D:
        return "gist"
    return "none"


# ---------------------------------------------------------------- 语料

def _text_of(item):
    ui = str(item.get("user_input") or "")
    ar = str(item.get("agent_response") or "")
    if ui or ar:
        return f"{ui} {ar}"
    return str(item.get("knowledge") or item.get("text") or "")


def load_corpus():
    """只取 page 与 long_term 条目；session 摘要不参与（避免同一事实被数两次）。"""
    out = []
    mid = json.load(open(os.path.join(CORPUS, "users/user_m0/mid_term.json"), encoding="utf-8"))
    for _sid, sess in (mid.get("sessions") or {}).items():
        for page in sess.get("details", []) or []:
            out.append(("page", page))
    for f, key in (("users/user_m0/long_term_user.json", "knowledge_base"),
                   ("users/user_m0/long_term_assistant.json", "assistant_knowledge")):
        p = os.path.join(CORPUS, f)
        if not os.path.exists(p):
            continue
        data = json.load(open(p, encoding="utf-8"))
        for e in data.get(key, []) or []:
            out.append(("knowledge", e))
    return out


def resolve(items, labels):
    """按 match 子串把标注绑到具体条目上；必须恰好 1 条，否则报错（标注坏了）。"""
    bound, bad = [], []
    for lab in labels:
        hits = [it for _kind, it in items if lab["match"] in _text_of(it)]
        if len(hits) != 1:
            bad.append((lab["key"], lab["match"], len(hits)))
            continue
        item = hits[0]
        if (item.get("type") or "") != lab["type"]:
            bad.append((lab["key"], f"type 不符: 标注 {lab['type']} / 实际 {item.get('type')}", 0))
            continue
        bound.append((lab, item))
    return bound, bad


# ---------------------------------------------------------------- 打分

def set_tau(ep, kn, pref, chg):
    S.TYPE_TAU_DAYS = {"episode": ep, "knowledge": kn, "preference": pref, "identity": None}
    S.CHANGING_IDENTITY_TAU_DAYS = chg


def tier_at(item, age, base_dt):
    now = base_dt + datetime.timedelta(days=age)
    score = S.compute_score(item, now)
    return ("full" if score >= S.FULL_THRESHOLD
            else "gist" if score >= S.GIST_THRESHOLD else "none")


def base_dt_of(items):
    for _kind, it in items:
        for k in ("created_at", "last_used_at", "timestamp"):
            if it.get(k):
                return datetime.datetime.strptime(it[k], TS_FMT)
    raise SystemExit("语料里找不到时间戳")


# ---------------------------------------------------------------- 评估

def evaluate(bound, ages, base_dt):
    cells = []
    for lab, item in bound:
        for age in ages:
            cells.append({"key": lab["key"], "age": age,
                          "expect": expect_tier(lab.get("D"), age, lab.get("mechanism", "decay")),
                          "actual": tier_at(item, age, base_dt)})
    return cells


def _render_len(item, age, base_dt):
    """这条在某个老化档下实际会占多少字（none=0）。"""
    raw = _text_of(item)
    t = tier_at(item, age, base_dt)
    if t == "none":
        return 0, len(raw)
    if t == "full":
        return len(raw), len(raw)
    return len(S.make_gist(item, base_dt + datetime.timedelta(days=age))["text"]), len(raw)


def compression_rate(items, ages, base_dt):
    """压缩率 = 1 - 实际返回字数 / 全 full 字数。分母是全部语料，不是只有标注过的。"""
    shown = total = 0
    for _kind, it in items:
        for age in ages:
            a, b = _render_len(it, age, base_dt)
            shown += a
            total += b
    return (1.0 - shown / total) if total else 0.0


def rates(cells):
    keep = [c for c in cells if c["expect"] == "full"]
    deg = [c for c in cells if c["expect"] != "full"]
    # 漏降 = 实际比期望“更完整”（该糊没糊）；超降 = 实际比期望“更糊”
    miss = [c for c in deg if ALIVE[c["actual"]] > ALIVE[c["expect"]]]
    over = [c for c in deg if ALIVE[c["actual"]] < ALIVE[c["expect"]]]
    wrong_keep = [c for c in keep if c["actual"] != "full"]             # 该记的被降了
    return {
        "keep_n": len(keep), "keep_err": len(wrong_keep) / len(keep) if keep else 0.0,
        "deg_n": len(deg), "miss_n": len(miss), "over_n": len(over),
        "miss_rate": len(miss) / len(deg) if deg else 0.0,
        "over_rate": len(over) / len(deg) if deg else 0.0,
        "miss_cells": miss, "keep_cells": wrong_keep,
    }


def feasible_tau(item, D, ages, base_dt, mechanism="decay", lo=1.0, hi=1500.0, step=0.5):
    """单条标注在“单独给它一个 τ”时的可行区间（用扫描，简单可靠）。"""
    ok = []
    t = lo
    while t <= hi:
        set_tau(t, t, t, t)
        if all(tier_at(item, a, base_dt) == expect_tier(D, a, mechanism) for a in ages):
            ok.append(t)
        t += step
    return (min(ok), max(ok)) if ok else None


def fmt_interval(iv):
    return "无解" if iv is None else f"[{iv[0]:.0f}, {iv[1]:.0f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--i-know-labels-change", action="store_true", dest="force")
    args = ap.parse_args()

    doc = load_labels()
    digest = labels_digest(doc)
    if args.freeze:
        doc["_frozen_sha"] = digest
        json.dump(doc, open(LABELS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"已冻结标注：sha256={digest[:16]}…  items={len(doc['items'])}")
        return 0
    if doc.get("_frozen_sha") != digest:
        if not args.force:
            print("!! 标注文件与冻结哈希不一致 —— 拒绝运行。")
            print(f"   文件记录: {str(doc.get('_frozen_sha'))[:16]}…  当前计算: {digest[:16]}…")
            print("   边调边改标注就是自己给自己发答案；确需改请加 --i-know-labels-change")
            return 2
        print("!! 已强制忽略标注哈希漂移（本次结论不可与冻结版对比）")

    ages = doc["_ages"]
    items = load_corpus()
    bound, bad = resolve(items, doc["items"])
    print(f"标注 {len(doc['items'])} 条，成功绑定 {len(bound)} 条，失败 {len(bad)} 条")
    for b in bad:
        print("   !!", b)
    if bad:
        return 3
    # P1：先判“这条标注能不能被某个 τ 满足”，无解的直接剔除，不让系统去解无解题
    keep_bound, invalid = [], []
    for lab, item in bound:
        if lab.get("mechanism", "decay") != "decay":
            keep_bound.append((lab, item))
            continue
        if feasible_tau(item, lab.get("D"), doc["_ages"], base_dt_of(items), "decay") is None:
            invalid.append(lab["key"])
            continue
        keep_bound.append((lab, item))
    if invalid:
        print(f"剔除无效标注 {len(invalid)} 条（跨档 gist 带宽不够，单条无可行 τ）：{invalid}")
    bound = keep_bound
    base_dt = base_dt_of(items)
    print(f"老化档：{ages}　共 {len(bound) * len(ages)} 格\n")

    # ---- 交付态
    shipped = (21, 90, 90, 180)
    set_tau(*shipped)
    r = rates(evaluate(bound, ages, base_dt))
    print(f"=== 交付态 τ = {shipped} ===")
    print(f"  闸门(降档半)：漏降 {r['miss_n']}/{r['deg_n']} = {r['miss_rate']:.1%}"
          f"  -> {'不合格' if r['miss_n'] else '合格'}")
    print(f"  保持半错误降率：{len(r['keep_cells'])}/{r['keep_n']} = {r['keep_err']:.1%}")
    print(f"  参考·超降率：{r['over_n']}/{r['deg_n']} = {r['over_rate']:.1%}")
    near = compression_rate(items, [a for a in ages if a <= 45], base_dt)
    full_range = compression_rate(items, ages, base_dt)
    flag = "达标" if near >= 0.15 else "不达止损线"
    print(f"  近端(0/45d)压缩率：{near:.1%}  [P3 止损线 15% -> {flag}]")
    print(f"  全档(0..365)压缩率：{full_range:.1%}")
    if r["miss_cells"]:
        print("  漏降明细（该降没降）：")
        for c in r["miss_cells"]:
            print(f"    {c['age']:>3}d  {c['key']:<12} 期望 {c['expect']:<4} 实际 {c['actual']}")
    if r["keep_cells"]:
        print("  错误降明细（该记却降了）：")
        for c in r["keep_cells"]:
            print(f"    {c['age']:>3}d  {c['key']:<12} 期望 {c['expect']:<4} 实际 {c['actual']}")

    # ---- 网格
    grid = {"episode": [18, 21, 25, 28, 30, 35],
            "knowledge": [90, 105, 120, 135, 150, 180],
            "preference": [24, 28, 32, 36, 45, 60, 90],
            "changing": [48, 60, 72, 84, 90, 105, 120, 180]}
    rows = []
    for ep, kn, pref, chg in itertools.product(grid["episode"], grid["knowledge"],
                                               grid["preference"], grid["changing"]):
        set_tau(ep, kn, pref, chg)
        rr = rates(evaluate(bound, ages, base_dt))
        rows.append(((ep, kn, pref, chg), rr))
    passed = [(c, rr) for c, rr in rows if rr["miss_n"] == 0]
    print(f"\n=== 网格 {len(rows)} 组：过闸门 {len(passed)} 组 ===")
    if passed:
        passed.sort(key=lambda x: (x[1]["keep_err"], x[1]["over_rate"]))
        for c, rr in passed[:8]:
            print(f"  τ={c}  错误降 {rr['keep_err']:.1%}  超降 {rr['over_rate']:.1%}")
    else:
        rows.sort(key=lambda x: (x[1]["miss_n"], x[1]["keep_err"]))
        print("  没有一组满足“一条都不许漏降”。最接近的 5 组：")
        for c, rr in rows[:5]:
            print(f"  τ={c}  漏降 {rr['miss_n']}/{rr['deg_n']}  错误降 {rr['keep_err']:.1%}")
        best_c, best_rr = rows[0]
        print("")
        print(f"  最佳候选 tau={best_c} 的剩余漏降（就是它卡住的原因）：")
        seen = {}
        for c in best_rr["miss_cells"]:
            seen.setdefault(c["key"], []).append(f"{c['age']}d:{c['actual']}")
        for k, v in seen.items():
            print(f"    {k:<14} {'，'.join(v)}")

    # ---- 每条标注的可行 τ（诊断“为什么没有解”）
    print("\n=== 单条可行 τ 区间（诊断用：假设这条独占一个 τ）===")
    groups = {}
    for lab, item in bound:
        iv = feasible_tau(item, lab.get("D"), ages, base_dt, lab.get("mechanism", "decay"))
        g = (lab["type"], lab.get("volatility") or "-")
        groups.setdefault(g, []).append((lab["key"], iv))
        print(f"  {lab['type']:<10}{str(lab.get('volatility') or '-'):<9}{lab['key']:<14}{fmt_interval(iv)}")
    print("\n=== 按组求交（看“一个 type 一个 τ”能不能同时满足）===")
    for g, lst in groups.items():
        los = [iv[0] for _k, iv in lst if iv]
        his = [iv[1] for _k, iv in lst if iv]
        empty = [k for k, iv in lst if iv is None]
        if empty:
            print(f"  {g}: 有 {len(empty)} 条单条就无解 -> {empty}")
            continue
        lo, hi = max(los), min(his)
        print(f"  {g}: 交集 {'空' if lo > hi else f'[{lo:.0f}, {hi:.0f}]'}"
              f"   (各条下界 max={lo:.0f}, 上界 min={hi:.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())