"""M2 三档召回集成测试：直接用 Retriever._apply_tiers 验证裁剪行为。

不读真实数据、不调模型：构造内存对象 + 伪造 Retriever 的两个存储引用。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, REPO)
from memoryos import scoring as S

# M6 起 τ 已按新语料重新标定（episode 14->21、knowledge 30->90）。
# 本文件测的是**公式与三档机制**，与具体 τ 取值无关，所以固定成标定前的旧值，
# 让断言不随调参漂移；当前默认值由 test_m6_tau_defaults.py 单独覆盖。
S.TYPE_TAU_DAYS = {"episode": 14, "knowledge": 30, "preference": 90, "identity": None}
from memoryos.retriever import Retriever

NOW = datetime(2026, 11, 1, 12, 0, 0)
FAIL = []


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def page(text, days_ago, imp=3, emo=1.0, mem_type="episode"):
    return {"page_id": "pg_" + str(abs(hash(text)) % 4096),
            "user_input": text, "agent_response": "ok",
            "timestamp": ts(days_ago), "meta_info": "m",
            "text": text, "type": mem_type, "importance": imp, "emotion": emo,
            "created_at": ts(days_ago), "last_used_at": ts(days_ago),
            "used_count": 0, "state": "hot", "valid_from": ts(days_ago),
            "valid_to": None, "source": "mid_term"}


def knowledge(text, days_ago, imp=3, emo=1.0, mem_type="knowledge"):
    return {"knowledge": text, "timestamp": ts(days_ago),
            "text": text, "type": mem_type, "importance": imp, "emotion": emo,
            "created_at": ts(days_ago), "last_used_at": ts(days_ago),
            "used_count": 0, "state": "hot", "valid_from": None,
            "valid_to": None, "source": "long_term"}


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'} {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


# 只借用 _apply_tiers 需要的实例（不构造完整 Retriever，避免加载模型）
r = object.__new__(Retriever)
r.mid_term_memory = None
r.long_term_memory = None
r.assistant_long_term_memory = None
r.include_faded = False

print("=== A) 开关关闭：原样返回，一个字段都不加 ===")
os.environ["USE_CUSTOM_SCORING"] = "0"
items = [page("新鲜事", 0), page("三个月前的事", 90)]
out = r._apply_tiers(items, "page")
check("条数不变", len(out) == 2, f"{len(out)}")
check("没有 recall_tier 字段", all("recall_tier" not in x for x in out))
check("对象是同一引用（未拷贝）", out[0] is items[0])

print()
print("=== B) 开关开启：三档确实分叉 ===")
os.environ["USE_CUSTOM_SCORING"] = "1"
items = [
    page("今天刚说的：某乐器十级", 0),            # score 3.0 -> full
    page("45 天前：另一门乐器三年", 45),             # score 0.1206 -> gist
    page("半年没提过：某次闲聊", 180),           # score ~1e-4 -> none
]
orig_texts = [x["user_input"] for x in items]
out = r._apply_tiers(items, "page", now=NOW)
tiers = [x["recall_tier"] for x in out]
check("3 条进去、2 条出来（none 被丢弃）", len(out) == 2, f"{len(out)}")
check("full 在前", tiers[0] == "full", str(tiers))
check("gist 在后", tiers[-1] == "gist", str(tiers))
check("full 保留原文", out[0]["user_input"] == orig_texts[0])
check("gist 换成标题+一句话", out[1]["user_input"].startswith("【模糊记忆"), out[1]["user_input"][:40])
check("gist 的 agent_response 被清空", out[1]["agent_response"] == "")
check("gist 的 meta_info 被清空", out[1]["meta_info"] == "")
check("原始数据不被污染（原文还在）", items[1]["user_input"] == orig_texts[1])
check("原始数据不被污染（没被塞 recall_tier）", "recall_tier" not in items[1])
check("recall_score 有值", all(isinstance(x.get("recall_score"), float) for x in out))

print()
print("=== C) last_used_at 被刷新（召回即续命）===")
items = [page("今天的事", 0)]
before = items[0]["last_used_at"]
r._apply_tiers(items, "page", now=NOW)
check("last_used_at 被写成 now", items[0]["last_used_at"] == NOW.strftime(S.TS_FMT), items[0]["last_used_at"])
check("used_count +1", items[0]["used_count"] == 1, str(items[0]["used_count"]))

print()
print("=== D) knowledge 条目同样分档，且用 knowledge 字段 ===")
# knowledge tau=30：90d -> 3*e^-3=0.149 gist；200d -> 3*e^-6.67=0.0038 none
items = [
    knowledge("某乐器十级", 0),          # full
    knowledge("另一门乐器三年", 90),        # gist
    knowledge("很久以前", 200),         # none
]
out = r._apply_tiers(items, "knowledge", now=NOW)
check("2 条出来", len(out) == 2, str(len(out)))
check("full 保原文", out[0]["knowledge"] == "某乐器十级")
check("gist 用 knowledge 字段装摘要", out[1]["knowledge"].startswith("【模糊记忆"), out[1]["knowledge"][:40])

print()
print("=== E) identity 永不掉档 ===")
items = [page("我是用户A", 3650, imp=4, mem_type="identity")]
out = r._apply_tiers(items, "page", now=NOW)
check("10 年后仍是 full", out[0]["recall_tier"] == "full", str(out[0]["recall_score"]))

print()
print("=== G) 主动检索：include_faded=True 时 none 档以「已淡忘」占位返回 ===")
r.include_faded = True
items = [page("半年前的事", 180), knowledge("很久以前的知识", 400)]
out = r._apply_tiers(items, "page", now=NOW)
check("none 档不再被丢弃（2 条都在）", len(out) == 2, str(len(out)))
check("标注为 none", out[0]["recall_tier"] == "none", out[0].get("recall_tier"))
check("正文被替换为标记", out[0]["user_input"].startswith("【已淡忘"), out[0]["user_input"])
check("原文没有被改动", items[0]["user_input"] == "半年前的事")
check("none 档不计入 used_count", items[0]["used_count"] == 0, str(items[0]["used_count"]))
r.include_faded = False

print()
print("=== F) 空输入不炸 ===")
check("空列表 -> 空列表", r._apply_tiers([], "page", now=NOW) == [])
check("None -> None（原样返回）", r._apply_tiers(None, "page", now=NOW) is None)

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)