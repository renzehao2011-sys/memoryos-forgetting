"""M6 τ / 阈值标定（用 42 条新语料，替代 M4 那份 18 条）。

流程：
1) 对每个老化档（0/45/120/365 天）取一次候选池：mode=0 检索，每题前恢复快照
   （避免 _touch_used 自刷新污染，见 M4 结论）；
2) 在内存里扫 τ_episode × GIST × FULL，全部确定性计算，不调 LLM；
3) 判据：召回不降（≥ baseline）且压缩率 > 20%。

identity 的 changing 子类 τ 固定 90 天（M6 新语义），stable 仍为 ∞。

用法：
  source /root/.bashrc; export HF_HUB_OFFLINE=1
  python3 m6_grid.py --collect
  python3 m6_grid.py --sweep
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import argparse
import copy
import datetime
import json
import os
import shutil
import sys

HERE = REPO
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from memoryos import scoring as S

DIRS = {"0": "data_corpus_m6", "45": "data_corpus_m6_45",
        "120": "data_corpus_m6_120", "365": "data_corpus_m6_365"}
KEEP = ("id", "page_id", "type", "importance", "emotion", "created_at", "last_used_at",
        "text", "user_input", "agent_response", "knowledge", "summary", "volatility")


def load_questions():
    with open(os.path.join(HERE, "corpus_m6_questions.json"), encoding="utf-8") as f:
        return json.load(f)["questions"]


def snapshot(m):
    lu, la = m.user_long_term_memory, m.assistant_long_term_memory
    return {"mid": copy.deepcopy(m.mid_term_memory.sessions),
            "lu_kb": copy.deepcopy(list(lu.knowledge_base)),
            "lu_ak": copy.deepcopy(list(lu.assistant_knowledge)),
            "la_kb": copy.deepcopy(list(la.knowledge_base)),
            "la_ak": copy.deepcopy(list(la.assistant_knowledge))}


def restore(m, snap):
    lu, la = m.user_long_term_memory, m.assistant_long_term_memory
    m.mid_term_memory.sessions.clear()
    m.mid_term_memory.sessions.update(copy.deepcopy(snap["mid"]))
    for mem, key, buf in ((lu, "knowledge_base", snap["lu_kb"]),
                          (lu, "assistant_knowledge", snap["lu_ak"]),
                          (la, "knowledge_base", snap["la_kb"]),
                          (la, "assistant_knowledge", snap["la_ak"])):
        dq = getattr(mem, key)
        dq.clear()
        dq.extend(copy.deepcopy(buf))


def collect():
    from memoryos.memoryos import Memoryos
    url = os.environ["BAILIAN_URL"]
    key = os.environ["BAILIAN_KEY"]
    qs = load_questions()
    out = {}
    for tag, dname in DIRS.items():
        src = os.path.join(HERE, dname)
        if not os.path.exists(src):
            print(f"跳过 {dname}")
            continue
        os.environ["USE_CUSTOM_SCORING"] = "0"
        tmp = os.path.join(HERE, f"_tmp_m6_{tag}")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        m = Memoryos(user_id="user_m0", openai_api_key=key, openai_base_url=url,
                     data_storage_path=tmp, llm_model="qwen-plus",
                     embedding_model_name="paraphrase-multilingual-MiniLM-L12-v2",
                     short_term_capacity=10, mid_term_capacity=2000,
                     retrieval_queue_capacity=7, mid_term_heat_threshold=5.0)
        m.retriever.retrieve_context(user_query="预热", user_id=m.user_id)
        snap = snapshot(m)
        now = datetime.datetime.now()
        rows = []
        for q in qs:
            restore(m, snap)
            res = m.retriever.retrieve_context(user_query=q["q"], user_id=m.user_id)
            pool = []
            for p in res.get("retrieved_pages", []):
                pool.append({"kind": "page", **{k: copy.deepcopy(p.get(k)) for k in KEEP}})
            for kn in res.get("retrieved_user_knowledge", []):
                pool.append({"kind": "knowledge", **{f: copy.deepcopy(kn.get(f)) for f in KEEP}})
            for kn in res.get("retrieved_assistant_knowledge", []):
                pool.append({"kind": "knowledge", **{f: copy.deepcopy(kn.get(f)) for f in KEEP}})
            rows.append({"q": q["q"], "expect": q["expect"],
                         "should_hit": q["should_hit"], "pool": pool})
        out[tag] = {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"{tag} 天档：{len(rows)} 题，平均每题 {sum(len(r['pool']) for r in rows) / len(rows):.1f} 条候选")
    path = os.path.join(HERE, "results", "m6_pool_cache.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("写入", path)


def render(item, tier, now):
    if item.get("kind") == "page":
        raw = str(item.get("user_input") or "") + str(item.get("agent_response") or "")
    else:
        raw = str(item.get("knowledge") or item.get("text") or "")
    if tier == "full":
        return raw
    if tier == "gist":
        return S.make_gist(item, now)["text"]
    return ""


def hit_of(expect, should_hit, blob):
    text = " ".join(blob)
    if expect == "not_recall":
        return not any(k in text for k in ["门牌", "吉他"])
    if not should_hit:
        return False
    return all(f in text for f in should_hit)


def evaluate(cache):
    now = datetime.datetime.strptime(cache["now"], "%Y-%m-%d %H:%M:%S")
    tot = hits = dropped = 0
    for r in cache["rows"]:
        blob = []
        for it in r["pool"]:
            tier = S.tier_of(S.compute_score(it, now=now))
            txt = render(it, tier, now)
            tot += len(txt)
            if txt:
                blob.append(txt)
            if tier == "none":
                dropped += 1
        if hit_of(r["expect"], r["should_hit"], blob):
            hits += 1
    return {"visible": tot, "hits": hits, "dropped": dropped}


def sweep(tau_ep, gist, full, tau_kn=30, tau_pref=90, tau_chg=90):
    S.TYPE_TAU_DAYS = {"episode": tau_ep, "knowledge": tau_kn,
                       "preference": tau_pref, "identity": None}
    S.CHANGING_IDENTITY_TAU_DAYS = tau_chg
    S.GIST_THRESHOLD = gist
    S.FULL_THRESHOLD = full


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    if a.collect:
        collect()
    if not a.sweep:
        return

    cache = json.load(open(os.path.join(HERE, "results", "m6_pool_cache.json"), encoding="utf-8"))
    # 单 type 单扫（看每个 type 的敏感度）+ 联合网格（找可行解）
    grid_tau = [14, 21, 30]
    grid_kn = [30, 90, 180]
    grid_pref = [90, 180]
    grid_chg = [90, 180]
    grid_gist = [0.12, 0.08]
    grid_full = [0.30, 0.20]
    summary = {}
    for tag in ("0", "45", "120", "365"):
        if tag not in cache:
            continue
        c = cache[tag]
        sweep(14, -1e9, -1e9)              # 基准 = 全部 full
        base = evaluate(c)
        id_chars = sum(len(render(it, "full", None))
                       for r in c["rows"] for it in r["pool"] if it.get("type") == "identity")
        changing_chars = sum(len(render(it, "full", None))
                             for r in c["rows"] for it in r["pool"]
                             if it.get("type") == "identity" and it.get("volatility") == "changing")
        print(f"\n=== {tag} 天 ===  候选文本 {base['visible']} 字，命中 {base['hits']}/{len(c['rows'])}；"
              f"其中 identity {id_chars} 字（changing {changing_chars} 字）→ "
              f"最大压缩 {100 * (1 - id_chars / base['visible']):.1f}%（identity 不再衰减，这是硬上限）")

        print("\n-- A. 只动 τ_episode（其余取默认 30/90/90，阈值 0.30/0.12）--")
        print(f"{'τ_ep':>5}{'可见':>8}{'压缩':>8}{'命中':>6}{'丢弃':>6}")
        sens_ep = []
        for tau_ep in grid_tau:
            sweep(tau_ep, 0.12, 0.30)
            r = evaluate(c)
            comp = 100 * (1 - r["visible"] / base["visible"])
            sens_ep.append({"tau_ep": tau_ep, "compression": round(comp, 2), "hits": r["hits"]})
            print(f"{tau_ep:>5}{r['visible']:>8}{comp:>7.1f}%{r['hits']:>6}{r['dropped']:>6}")

        print("-- B. 只动 τ_knowledge（ep14 / pref90 / chg90，阈值 0.30/0.12）--")
        print(f"{'τ_kn':>5}{'可见':>8}{'压缩':>8}{'命中':>6}{'丢弃':>6}")
        sens_kn = []
        for tau_kn in grid_kn:
            sweep(14, 0.12, 0.30, tau_kn=tau_kn)
            r = evaluate(c)
            comp = 100 * (1 - r["visible"] / base["visible"])
            sens_kn.append({"tau_kn": tau_kn, "compression": round(comp, 2), "hits": r["hits"]})
            print(f"{tau_kn:>5}{r['visible']:>8}{comp:>7.1f}%{r['hits']:>6}{r['dropped']:>6}")

        print("\n-- C. 联合网格（3×3×2×2×2×2=144 组）--")
        rows = []
        for tau_ep in grid_tau:
            for tau_kn in grid_kn:
                for tau_pref in grid_pref:
                    for tau_chg in grid_chg:
                        for gist in grid_gist:
                            for full in grid_full:
                                sweep(tau_ep, gist, full, tau_kn, tau_pref, tau_chg)
                                r = evaluate(c)
                                comp = 100 * (1 - r["visible"] / base["visible"])
                                rows.append({"tau_ep": tau_ep, "tau_kn": tau_kn,
                                             "tau_pref": tau_pref, "tau_chg": tau_chg,
                                             "gist": gist, "full": full,
                                             "visible": r["visible"],
                                             "compression": round(comp, 2),
                                             "hits": r["hits"], "dropped": r["dropped"]})
        ok = [r for r in rows if r["compression"] > 20 and r["hits"] >= base["hits"]]
        print(f"满足「压缩>20% 且召回不降（{base['hits']}/{len(c['rows'])}）」的组合：{len(ok)} 个")
        for r in sorted(ok, key=lambda x: -x["compression"])[:8]:
            print(f"   τ_ep={r['tau_ep']:<3} τ_kn={r['tau_kn']:<4} τ_pref={r['tau_pref']:<4} "
                  f"τ_chg={r['tau_chg']:<4} gist={r['gist']:<5} full={r['full']:<5} "
                  f"压缩 {r['compression']:5.1f}% 命中 {r['hits']}/{len(c['rows'])}")
        if not ok:
            near = sorted(rows, key=lambda x: (-x["hits"], -x["compression"]))[:5]
            print("   最接近的（先保召回，再看压缩）：")
            for r in near:
                print(f"   τ_ep={r['tau_ep']:<3} τ_kn={r['tau_kn']:<4} τ_pref={r['tau_pref']:<4} "
                      f"τ_chg={r['tau_chg']:<4} gist={r['gist']:<5} full={r['full']:<5} "
                      f"压缩 {r['compression']:5.1f}% 命中 {r['hits']}/{len(c['rows'])}")
        summary[tag] = {"baseline": base, "identity_chars": id_chars,
                        "changing_identity_chars": changing_chars,
                        "sens_episode": sens_ep, "sens_knowledge": sens_kn,
                        "viable": len(ok), "rows": rows}
    json.dump(summary, open(os.path.join(HERE, "results", "m6_grid.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n写入 results/m6_grid.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())