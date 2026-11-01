"""
M0 baseline 运行器 —— 纯原版 MemoryOS（memoryos-mcp），不改任何源码。
目的：采集计划单要求的三项基线指标：
  1) 召回准确率  2) 单次召回延迟(中位数)  3) 单次整理 token
用法：
  BAILIAN_URL=... BAILIAN_KEY=... python3 run_baseline.py [--phase seed|probe|all]
说明：
  - LLM 用百炼 qwen-plus；embedding 用原版默认本地 all-MiniLM-L6-v2。
  - 不改 repo：只在 Memoryos(...) 构造参数里传百炼配置。
  - token 统计：对 client.chat_completion 做透明包装，累计 response.usage。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import os, sys, json, time, statistics, argparse, importlib.util

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "results")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, REPO)
from memoryos.memoryos import Memoryos
from memoryos.utils import get_timestamp

LLM_MODEL = os.environ.get("M0_LLM_MODEL", "qwen-plus")
URL = os.environ["BAILIAN_URL"]
KEY = os.environ["BAILIAN_KEY"]

with open(os.path.join(HERE, "eval_questions.json"), encoding="utf-8") as f:
    EVAL = json.load(f)

# ---- token 累计包装（不改 repo，运行时 monkeypatch 实例方法）----
TOKEN_LEDGER = {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "by_tag": {}}

def wrap_client(client, tag_fn):
    orig = client.chat_completion
    def patched(model, messages, temperature=0.7, max_tokens=2000):
        tag = tag_fn()
        resp = client.client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens)
        u = getattr(resp, "usage", None)
        TOKEN_LEDGER["calls"] += 1
        rec = TOKEN_LEDGER["by_tag"].setdefault(tag, {"calls":0,"prompt":0,"completion":0,"total":0})
        if u:
            TOKEN_LEDGER["prompt"] += u.prompt_tokens
            TOKEN_LEDGER["completion"] += u.completion_tokens
            TOKEN_LEDGER["total"] += u.total_tokens
            rec["calls"] += 1
            rec["prompt"] += u.prompt_tokens
            rec["completion"] += u.completion_tokens
            rec["total"] += u.total_tokens
        from memoryos.utils import clean_reasoning_model_output
        return clean_reasoning_model_output(resp.choices[0].message.content.strip())
    client.chat_completion = patched
    return orig

TAG = {"cur": "unknown"}
def tag_fn():
    return TAG["cur"]

def build(mem_file_tag="m0"):
    m = Memoryos(
        user_id="user_m0",
        openai_api_key=KEY,
        openai_base_url=URL,
        data_storage_path=DATA_DIR,
        llm_model=LLM_MODEL,
        embedding_model_name="paraphrase-multilingual-MiniLM-L12-v2",
        short_term_capacity=10,
        mid_term_capacity=2000,
        retrieval_queue_capacity=7,
        mid_term_heat_threshold=5.0,
    )
    wrap_client(m.client, tag_fn)
    return m

def phase_seed():
    """灌入种子对话，测量'整理'阶段的 token。"""
    global TAG
    m = build()
    TAG["cur"] = "seed_organize"
    t0 = time.time()
    before = TOKEN_LEDGER["total"]
    for d in EVAL["seed_dialogs"]:
        m.add_memory(user_input=d["user"], agent_response=d["resp"], timestamp=get_timestamp())
    # 强制把中期热段分析跑一遍，确保'整理'链路被触发
    m.force_mid_term_analysis()
    dt = time.time() - t0
    print(json.dumps({
        "phase": "seed",
        "elapsed_s": round(dt,2),
        "tokens": TOKEN_LEDGER["total"],
        "by_tag": TOKEN_LEDGER["by_tag"],
    }, ensure_ascii=False, indent=2))
    with open(os.path.join(OUT_DIR,"seed_token.json"),"w",encoding="utf-8") as f:
        json.dump({"elapsed_s":dt,"ledger":TOKEN_LEDGER}, f, ensure_ascii=False, indent=2)

def phase_probe():
    """只测召回：retrieve_context，不调生成 LLM。测延迟中位数 + 召回命中率。"""
    global TAG
    m = build()
    latencies = []
    hits = {"accurate":0,"fuzzy":0,"expired":0,"not_recall":0}
    totals = {"accurate":0,"fuzzy":0,"expired":0,"not_recall":0}
    detail = []
    for item in EVAL["questions"]:
        q = item["q"]; expect = item["expect"]; want = item["should_hit"]
        totals[expect]+=1
        t0=time.time()
        res = m.retriever.retrieve_context(user_query=q, user_id=m.user_id)
        dt=time.time()-t0
        latencies.append(dt)
        # 汇总召回到的文本
        blob=[]
        for p in res.get("retrieved_pages",[]):
            blob.append(p.get("user_input",""))
            blob.append(str(p.get("meta_info","")))
        for k in res.get("retrieved_user_knowledge",[]):
            blob.append(k.get("knowledge",""))
        text=" ".join(blob)
        # 简易命中判定：should_hit 的关键片段是否出现
        keyfrags=[x for x in want.replace("已排除","").replace("已转一项动手类爱好","").split() ]
        # 针对中文，直接用若干代表字符判定
        probe_map={
            "某乐器十级10岁":["某乐器","十级"],
            "击球点低拍面朝上站位偏后":["击球点","杀球","拍面","站位"],
            "1100元小尺寸机型入门":["1100","小尺寸机型","一项动手类爱好"],
            "三年连级跳考":["另一门乐器","三年","连级"],
            "全市前3%某地":["前3","某地","中考"],
            "定点定高瓷砖":["VR","头显飞行","瓷砖","定点"],
            "Neo4j已排除":["Neo4j"],
            "固定翼已转一项动手类爱好":["固定翼"],
            "无此信息":[],
        }
        frags=probe_map.get(want,[])
        hit = bool(frags) and all(any(fr in b for b in blob) for fr in frags) if frags else False
        if expect=="not_recall":
            # 不该召回：命中的反面——若没检到相关信息即算正确
            hit = (len(text.strip())==0) or not any(k in text for k in ["门牌","小区","法语"])
        if hit: hits[expect]+=1
        detail.append({"q":q,"expect":expect,"want":want,"hit":hit,
                       "latency_s":round(dt,3),
                       "n_pages":len(res.get("retrieved_pages",[])),
                       "n_user_kn":len(res.get("retrieved_user_knowledge",[])),
                       "n_asst_kn":len(res.get("retrieved_assistant_knowledge",[]))})
        print(f"[{expect:10}] hit={str(hit):5} {round(dt*1000)}ms  {q[:24]}")
    summary={
        "phase":"probe",
        "latency_median_s":round(statistics.median(latencies),4),
        "latency_mean_s":round(statistics.mean(latencies),4),
        "latency_p95_s":round(sorted(latencies)[int(len(latencies)*0.95)-1],4) if latencies else None,
        "recall_by_class":{k:f"{hits[k]}/{totals[k]}" for k in totals},
        "overall_recall":f"{sum(hits.values())}/{sum(totals.values())}",
        "detail":detail,
    }
    print(json.dumps({k:v for k,v in summary.items() if k!="detail"}, ensure_ascii=False, indent=2))
    with open(os.path.join(OUT_DIR,"probe_metrics.json"),"w",encoding="utf-8") as f:
        json.dump(summary,f,ensure_ascii=False,indent=2)

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--phase",default="all",choices=["seed","probe","all"])
    a=ap.parse_args()
    if a.phase in("seed","all"): phase_seed()
    if a.phase in("probe","all"): phase_probe()