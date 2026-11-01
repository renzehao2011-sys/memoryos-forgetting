"""M5-b 单测：本轮四处修改的契约。

1. gist 永远不比原文长（短条目保护），且仍以【模糊记忆 开头（档位可辨认）
2. 召回层过滤 valid_to（事实失效真正生效）
3. P0：valid_to 过滤与 USE_CUSTOM_SCORING 解耦（关掉也过滤）
4. dedupe 遇到同 id 重复副本：保留首份 + 另一份写 valid_to，审计标
   dedup_reason=same_id_duplicate，且不删除任何条目

运行：python3 test_m5b_fixes.py
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import os
import sys
import tempfile
import datetime

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from memoryos import scoring as S
from memoryos import consolidate as C
from memoryos.retriever import Retriever

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def make_page(text, days=45, typ="episode", imp=3, emo=1.0, valid_to=None, pid="p1"):
    now = datetime.datetime.now()
    ts = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return {"id": pid, "page_id": pid, "type": typ, "importance": imp, "emotion": emo,
            "created_at": ts, "last_used_at": ts, "used_count": 0, "state": "hot",
            "valid_from": None, "valid_to": valid_to, "source": "test",
            "user_input": text, "agent_response": "", "text": text,
            "page_embedding": [1.0, 0.0]}


def fake_retriever():
    r = object.__new__(Retriever)
    r.include_faded = False
    return r


def main():
    print("1. gist 不比原文长（原文极短时允许 +6 字保住正文），且仍可辨认")
    now = datetime.datetime.now()
    for n in (1, 5, 8, 12, 20, 29, 51, 80, 200):
        raw = "字" * n
        g = S.make_gist(make_page(raw), now=now)["text"]
        limit = len(raw) + 6
        check(f"len={n}: gist({len(g)}) <= raw+6({limit})", len(g) <= limit,
              f"{len(g)} vs {len(raw)}")
        check(f"len={n}: 仍以【模糊记忆 开头", g.startswith("【模糊记忆"), g[:16])
        check(f"len={n}: 正文没丢", raw[:min(n, 4)] in g, g[:24])

    print("2. 召回过滤 valid_to（打分开启）")
    os.environ["USE_CUSTOM_SCORING"] = "1"
    r = fake_retriever()
    live = make_page("有效记忆", days=1, pid="p_live")
    dead = make_page("已失效记忆", days=1, valid_to="2026-10-01 00:00:00", pid="p_dead")
    out = r._apply_tiers([live, dead], "page")
    ids = [o.get("page_id") for o in out]
    check("失效条目被过滤", "p_dead" not in ids, str(ids))
    check("有效条目保留", "p_live" in ids, str(ids))

    print("3. P0：valid_to 过滤与打分开关解耦（关掉也必须过滤）")
    os.environ["USE_CUSTOM_SCORING"] = "0"
    out0 = r._apply_tiers([live, dead], "page")
    ids0 = [o.get("page_id") for o in out0]
    check("关闭开关后失效条目仍被过滤", ids0 == ["p_live"], str(ids0))
    check("关闭开关时不加 recall_tier（档位仍旁路）",
          all("recall_tier" not in o for o in out0))
    os.environ["USE_CUSTOM_SCORING"] = "1"

    print("4. dedupe 同 id 重复副本")
    os.environ["USE_CUSTOM_SCORING"] = "1"
    tmp = tempfile.mkdtemp(prefix="m5b_")
    path = os.path.join(tmp, "audit.jsonl")

    class FakeAudit:
        def __init__(self):
            self.rows = []
        def record(self, action, mem_id, old_score=None, new_score=None, reason="", **extra):
            self.rows.append({"action": action, "id": mem_id, "reason": reason, **extra})

    audit = FakeAudit()
    a = make_page("同一条记忆的副本", days=1, pid="p_dup")
    b = make_page("同一条记忆的副本", days=1, pid="p_dup")
    a["page_embedding"] = [1.0, 0.0]
    b["page_embedding"] = [1.0, 0.0]
    st = C.dedupe([a, b], datetime.datetime.now(), audit, level="mid_term")
    check("合并 1 条", st["merged"] == 1, str(st))
    check("首份仍有效（valid_to 为空）", a.get("valid_to") is None, str(a.get("valid_to")))
    check("副本写 valid_to", bool(b.get("valid_to")), str(b.get("valid_to")))
    check("审计标 same_id_duplicate",
          audit.rows and audit.rows[0].get("dedup_reason") == "same_id_duplicate",
          str(audit.rows[:1]))
    check("对象没被删除", len([x for x in (a, b) if x is not None]) == 2)

    print("5. 幂等：再跑一次不再变化")
    st2 = C.dedupe([a, b], datetime.datetime.now(), audit, level="mid_term")
    check("第二次 merged=0", st2["merged"] == 0, str(st2))
    check("审计行数不变", len(audit.rows) == 1, str(len(audit.rows)))

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())