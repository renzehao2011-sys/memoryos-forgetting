"""M7 修复自测：守「不硬删除 / 可回退 / 标记真的生效 / 读失败不覆盖磁盘」。

覆盖本轮 6 处改动：
  A. long_term 容量超限不再静默丢条（追加 / 加载两条路径）
  B. long_term 读盘损坏：改名保留 + 拒绝写盘
  C. mid_term over_capacity：rebuild_heap 跳过 + clear 可恢复
  D. consolidate.restore_expired：撤销 valid_to（可回退）+ 审计 + 幂等
  E. retriever 过滤 state=archived
  F. consolidate_llm 冲突候选只在同层级内配对

运行：/usr/bin/python3 test_m7_fixes.py
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import datetime
import json
import os
import sys
import tempfile
import threading
from collections import defaultdict, deque

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from memoryos import long_term as LT
from memoryos import mid_term as MT
from memoryos import consolidate as C
from memoryos import consolidate_llm as CL
from memoryos.audit import AuditLog, ACTION_RESTORE
from memoryos.retriever import Retriever

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


# ---------------------------------------------------------------- 构造工具

def make_lt(path, cap=5):
    """不走 __init__（会加载 embedding 模型），手工装配一个可用的 LongTermMemory。"""
    lt = object.__new__(LT.LongTermMemory)
    lt.file_path = path
    lt.knowledge_capacity = cap
    lt.user_profiles = {}
    lt.knowledge_base = deque()
    lt.assistant_knowledge = deque()
    lt.embedding_model_name = "unused"
    lt.embedding_model_kwargs = {}
    lt.lock = threading.Lock()
    lt._load_failed = False
    return lt


def make_mid(path, sessions, cap=2):
    m = object.__new__(MT.MidTermMemory)
    m.file_path = path
    m.client = None
    m.max_capacity = cap
    m.sessions = sessions
    m.access_frequency = defaultdict(int)
    m.heap = []
    m.embedding_model_name = "unused"
    m.embedding_model_kwargs = {}
    m.lock = threading.Lock()
    m._load_failed = False
    return m


def main():
    tmp = tempfile.mkdtemp(prefix="m7_")
    LT.get_embedding = lambda text, **kw: [1.0, 0.0]   # 跳过模型

    # ------------------------------------------------ A. 容量不再丢条
    print("A) long_term 容量超限：不删、只标记")
    p = os.path.join(tmp, "lt.json")
    lt = make_lt(p, cap=5)
    for i in range(7):
        lt.add_user_knowledge(f"第{i}条知识")
    kept = [e["knowledge"] for e in lt.knowledge_base]
    check("7 条全在（第0条没被挤掉）", len(kept) == 7 and "第0条知识" in kept, str(kept))
    marked = [e for e in lt.knowledge_base if e.get("over_capacity_since")]
    check("超出部分被打 over_capacity 标记（2 条）", len(marked) == 2, str(len(marked)))
    check("被标记者仍是 hot（没动 state）",
          all((e.get("state") or "hot") == "hot" for e in marked))
    disk = json.load(open(p, encoding="utf-8"))
    check("落盘也保留 7 条（save 写的就是全量）", len(disk["knowledge_base"]) == 7,
          str(len(disk["knowledge_base"])))

    for i in range(7, 9):
        lt.add_user_knowledge(f"第{i}条知识")
    marked2 = [e for e in lt.knowledge_base if e.get("over_capacity_since")]
    check("继续追加不会越标越多（9 条 / cap 5 -> 恰好 4 条）", len(marked2) == 4, str(len(marked2)))

    print("A2) load 时不再截断")
    p2 = os.path.join(tmp, "lt2.json")
    entries = [{"knowledge": f"k{i}", "id": f"id{i}", "knowledge_embedding": [1.0, 0.0],
                "created_at": "2026-10-01 00:00:00", "last_used_at": "2026-10-01 00:00:00",
                "state": "hot", "type": "knowledge"} for i in range(7)]
    json.dump({"user_profiles": {}, "knowledge_base": entries, "assistant_knowledge": []},
              open(p2, "w", encoding="utf-8"), ensure_ascii=False)
    lt2 = make_lt(p2, cap=5)
    lt2.load()
    check("载入 7 条（旧实现会砍成 5 条）", len(lt2.knowledge_base) == 7, str(len(lt2.knowledge_base)))

    # ------------------------------------------------ B. 损坏文件
    print("B) long_term 读到损坏文件：保留原文件且不覆盖")
    p3 = os.path.join(tmp, "lt3.json")
    open(p3, "w", encoding="utf-8").write("{ this is not json")
    lt3 = make_lt(p3, cap=5)
    lt3.load()
    quarantined = [f for f in os.listdir(tmp) if f.startswith("lt3.json.corrupt-")]
    check("损坏文件被改名保留", len(quarantined) == 1, str(quarantined))
    check("原路径已被移除（内容还在 .corrupt 里）", not os.path.exists(p3))
    check("_load_failed 置位", lt3._load_failed is True)
    lt3.add_user_knowledge("读失败后写入")
    check("读失败后拒绝写盘：原路径仍不存在", not os.path.exists(p3))

    # ------------------------------------------------ C. over_capacity 真的生效
    print("C) mid_term over_capacity：堆里不再出现，可人工解除")
    mp = os.path.join(tmp, "mid.json")
    sessions = {
        "s0": {"id": "s0", "summary": "a", "details": [], "H_segment": 0.0, "state": "hot",
               "over_capacity_since": "2026-11-01 00:00:00"},
        "s1": {"id": "s1", "summary": "b", "details": [], "H_segment": 5.0, "state": "hot"},
    }
    m = make_mid(mp, sessions)
    m.rebuild_heap()
    check("rebuild_heap 跳过已标记会话", all(sid != "s0" for _, sid in m.heap), str(m.heap))
    check("未标记会话仍在堆里", any(sid == "s1" for _, sid in m.heap), str(m.heap))
    cleared = m.clear_over_capacity()
    check("clear_over_capacity 解除 1 个", cleared == 1, str(cleared))
    check("解除后重新进堆", any(sid == "s0" for _, sid in m.heap), str(m.heap))
    check("解除后标记字段被清掉", "over_capacity_since" not in m.sessions["s0"])

    # ------------------------------------------------ D. 可回退
    print("D) restore_expired：撤销 valid_to + 审计 + 幂等")
    audit = AuditLog(os.path.join(tmp, "audit.log"))
    items = [
        {"id": "x1", "valid_to": "2026-10-01 00:00:00", "merged_into": "x0",
         "state": "cold", "type": "knowledge", "created_at": "2026-09-01 00:00:00"},
        {"id": "x2", "valid_to": None, "state": "hot", "type": "knowledge"},
        {"id": "x3", "valid_to": "2026-10-01 00:00:00", "state": "archived", "type": "knowledge"},
    ]
    st = C.restore_expired(items, ["x1", "x2", "x3", "nope"], audit=audit, operator="review")
    check("撤销 1 条", st["restored"] == 1, str(st))
    check("valid_to 被清空", items[0]["valid_to"] is None, str(items[0]["valid_to"]))
    check("保留 merged_into 作为历史", items[0].get("merged_into") == "x0")
    check("记 restored_at / restored_by", bool(items[0].get("restored_at")) and items[0].get("restored_by") == "review")
    check("state 没被改动", items[0]["state"] == "cold")
    check("未失效的计入 not_expired", st["not_expired"] == 1, str(st))
    check("已归档的不复活", st["archived"] == 1 and items[2]["valid_to"] is not None)
    check("未知 id 计入 not_found", st["not_found"] == 1, str(st))
    rows = audit.read_all()
    check("审计写了 1 条 restore", len(rows) == 1 and rows[0]["action"] == ACTION_RESTORE, str(rows))
    st2 = C.restore_expired(items, ["x1"], audit=audit)
    check("重复撤销不再产生变化（幂等）", st2["restored"] == 0 and st2["not_expired"] == 1, str(st2))
    check("审计行数仍为 1", audit.count() == 1, str(audit.count()))

    # ------------------------------------------------ E. archived 不召回
    print("E) retriever：state=archived 不参与召回")
    os.environ["USE_CUSTOM_SCORING"] = "1"
    r = object.__new__(Retriever)
    r.include_faded = False
    now = datetime.datetime.now()
    fresh = now.strftime("%Y-%m-%d %H:%M:%S")
    live = {"page_id": "p_live", "user_input": "活着的", "agent_response": "", "type": "episode",
            "importance": 3, "emotion": 1.0, "created_at": fresh, "last_used_at": fresh,
            "state": "hot", "valid_to": None}
    arch = dict(live, page_id="p_arch", user_input="已归档的", state="archived")
    out = r._apply_tiers([live, arch], "page", now=now)
    ids = [o.get("page_id") for o in out]
    check("归档条目被过滤", "p_arch" not in ids, str(ids))
    check("未归档条目保留", "p_live" in ids, str(ids))
    os.environ["USE_CUSTOM_SCORING"] = "0"
    out0 = r._apply_tiers([live, arch], "page", now=now)
    ids0 = [o.get("page_id") for o in out0]
    check("P0：关开关后归档条目仍被过滤", ids0 == ["p_live"], str(ids0))
    check("P0：关开关不加档位字段", all("recall_tier" not in o for o in out0))
    os.environ["USE_CUSTOM_SCORING"] = "1"

    # ------------------------------------------------ F. 冲突候选同层级
    print("F) 冲突候选：不再跨层级配对")
    page = {"id": "pg1", "page_id": "pg1", "user_input": "住A小区", "agent_response": "",
            "created_at": "2026-10-01 00:00:00", "page_embedding": [1.0, 0.0]}
    # 向量要落在 CONFLICT_SIM_MIN(0.40) <= cos < CONFLICT_SIM_MAX(0.92) 之间，
    # 否则会被“视为重复、交给第 1 步去重”挡掉，测不到同层级配对。
    know = {"id": "kn1", "knowledge": "住B小区", "text": "住B小区",
            "created_at": "2026-10-05 00:00:00", "knowledge_embedding": [1.0, 0.0]}
    pairs_cross = CL.find_conflict_candidates([page], [know])
    check("page × knowledge 不再成为候选", pairs_cross == [], str(len(pairs_cross)))
    know2 = dict(know, id="kn2", knowledge="住C小区", text="住C小区",
                 created_at="2026-10-09 00:00:00", knowledge_embedding=[0.8, 0.6])  # cos=0.8
    pairs_same = CL.find_conflict_candidates([know], [know2])
    check("knowledge × knowledge 仍然是候选", len(pairs_same) == 1, str(len(pairs_same)))

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
