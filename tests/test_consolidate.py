"""M3 整理层离线单测：状态迁移、去重合并、幂等、审计日志。

不碰真实数据、不调模型。核心验证：
  1. hot -> cold 只在「分数低 + 久未召回」时发生
  2. cold -> 待归档 只在「cold 够久」时发生
  3. 待归档绝不自动变 archived
  4. 召回后退回 hot
  5. 同一份数据跑两次，第二次零变化（幂等）
  6. 去重只合并同类型、够相似、窗口内的
  7. 审计日志每条都能回答"为什么"
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, REPO)
from memoryos import scoring as S
from memoryos import consolidate as C
from memoryos.audit import AuditLog

NOW = datetime(2026, 11, 1, 12, 0, 0)
FAIL = []


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def mem(mid, mem_type="episode", imp=3, emo=1.0, days_ago=0, state="hot", **kw):
    d = {"id": mid, "text": f"内容 {mid}", "type": mem_type,
         "importance": imp, "emotion": emo, "state": state,
         "created_at": ts(days_ago), "last_used_at": ts(days_ago),
         "used_count": 0, "valid_from": ts(days_ago), "valid_to": None,
         "source": "test"}
    d.update(kw)
    return d


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'} {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="m3test_")
AUDIT_PATH = os.path.join(TMP, "memory_audit.log")


def fresh_audit():
    if os.path.exists(AUDIT_PATH):
        os.remove(AUDIT_PATH)
    return AuditLog(AUDIT_PATH)


print("=== A) hot -> cold 的触发条件 ===")
audit = fresh_audit()
# episode tau=14: 100 天 -> 3*e^(-100/14)=0.0025 < 0.05 且 100>90 天 -> 该转 cold
items = [mem("a_old", days_ago=100), mem("a_new", days_ago=1)]
st = C.recompute_states(items, NOW, audit)
check("老条目转 cold", items[0]["state"] == "cold", items[0]["state"])
check("新条目仍是 hot", items[1]["state"] == "hot", items[1]["state"])
check("转了 1 条", st["hot_to_cold"] == 1, str(st["hot_to_cold"]))
check("写了 cold_since", bool(items[0].get("cold_since")), str(items[0].get("cold_since")))

print()
print("=== B) 只低分但不够久，不该转 cold ===")
audit = fresh_audit()
# M7-7：τ_episode 已从 14 改为 21，原来按 60 天算的这条会变成空断言
# （60 天 score=3*e^(-60/21)=0.172 > 0.05，压根进不到“低分”分支）。
# 改取 88 天：score=3*e^(-88/21)=0.0455 < 0.05，且 88 < 90 -> 才真正测到“低分但不够久”。
it = [mem("b_mid", days_ago=88)]
C.recompute_states(it, NOW, audit)
check("88 天低分仍是 hot（未满 90 天）", it[0]["state"] == "hot", it[0]["state"])
check("88 天确实已低于 COLD_SCORE（保证测到的是目标分支）",
      S.compute_score(it[0], NOW) < C.COLD_SCORE, f"{S.compute_score(it[0], NOW):.4f}")

print()
print("=== C) 高分但很久没召回，也不该转 cold ===")
audit = fresh_audit()
# identity tau=∞，1000 天 score 仍是 5 -> 不转
it = [mem("c_identity", mem_type="identity", imp=5, days_ago=1000)]
C.recompute_states(it, NOW, audit)
check("identity 永不转 cold", it[0]["state"] == "hot", it[0]["state"])

print()
print("=== D) cold 持续 180 天 -> 进待归档（但不变 archived）===")
audit = fresh_audit()
it = [mem("d_pending", days_ago=300)]
it[0]["state"] = "cold"
it[0]["cold_since"] = ts(200)          # 已冷 200 天
it[0]["last_used_at"] = ts(300)        # 期间未召回
st = C.recompute_states(it, NOW, audit)
check("进了待归档队列", it[0].get("pending_archive_since") is not None)
check("state 仍是 cold（不自动 archived）", it[0]["state"] == "cold", it[0]["state"])
check("统计 pending_archive=1", st["pending_archive"] == 1, str(st["pending_archive"]))

print()
print("=== E) cold 期间被召回 -> 退回 hot ===")
audit = fresh_audit()
it = [mem("e_revive", days_ago=300)]
it[0]["state"] = "cold"
it[0]["cold_since"] = ts(20)
it[0]["last_used_at"] = ts(1)          # 冷之后又被召回
st = C.recompute_states(it, NOW, audit)
check("退回 hot", it[0]["state"] == "hot", it[0]["state"])
check("清掉 cold_since", "cold_since" not in it[0])
check("统计 cold_to_hot=1", st["cold_to_hot"] == 1, str(st["cold_to_hot"]))

print()
print("=== F) archived 永不被自动改动 ===")
audit = fresh_audit()
it = [mem("f_arch", days_ago=2000, state="archived")]
st = C.recompute_states(it, NOW, audit)
check("archived 仍是 archived", it[0]["state"] == "archived", it[0]["state"])
check("统计里被跳过", st["skipped_archived"] == 1, str(st["skipped_archived"]))

print()
print("=== G) 幂等：同一份数据跑两次，数据完全不变 ===")
audit = fresh_audit()
items = [mem("g1", days_ago=100), mem("g2", days_ago=5),
         mem("g3", days_ago=300), mem("g4", mem_type="identity", imp=5, days_ago=500)]
items[2]["state"] = "cold"
items[2]["cold_since"] = ts(250)
items[2]["last_used_at"] = ts(300)
C.recompute_states(items, NOW, audit)
snap1 = json.dumps(items, sort_keys=True, ensure_ascii=False)
audit1_count = audit.count()
C.recompute_states(items, NOW, audit)
snap2 = json.dumps(items, sort_keys=True, ensure_ascii=False)
audit2_count = audit.count()
check("两次运行后数据完全一致", snap1 == snap2)
check("第二次没有再写状态变更日志", audit2_count == audit1_count,
      f"{audit1_count} -> {audit2_count}")

print()
print("=== H) 去重：同类型高相似才合并 ===")
audit = fresh_audit()
vec = [1.0, 0.0, 0.0]
a = mem("h_short", days_ago=1, text="短文本", page_embedding=vec)
b = mem("h_long", days_ago=1, text="这是一个更长更完整的文本", page_embedding=vec)
c = mem("h_other", mem_type="preference", days_ago=1, text="不同类型", page_embedding=vec)
items = [a, b, c]
st = C.dedupe(items, NOW, audit)
check("合并了 1 条", st["merged"] == 1, str(st["merged"]))
# a 是短文本、b 是长文本 → 应保留 b，被吸收的是 a
check("保留的是长文本（b 未失效、a 已失效）",
      b.get("valid_to") is None and a.get("valid_to") is not None,
      f"a.valid_to={a.get('valid_to')} b.valid_to={b.get('valid_to')}")
check("被吸收方没被删除（还在列表里）", len(items) == 3)
check("不同类型不合并", c.get("valid_to") is None)
check("被吸收方记录了 merged_into 指向保留方",
      a.get("merged_into") == "h_long" and b.get("merged_into") is None,
      f"a.merged_into={a.get('merged_into')}")

print()
print("=== I) 去重幂等 ===")
snap1 = json.dumps(items, sort_keys=True, ensure_ascii=False)
st2 = C.dedupe(items, NOW, audit)
snap2 = json.dumps(items, sort_keys=True, ensure_ascii=False)
check("第二次合并 0 条", st2["merged"] == 0, str(st2["merged"]))
check("数据未变", snap1 == snap2)

print()
print("=== J) 去重时间窗：超过 7 天的不参与 ===")
audit = fresh_audit()
x = mem("j_old1", days_ago=30, text="旧一", page_embedding=vec)
y = mem("j_old2", days_ago=30, text="旧二更长的文本", page_embedding=vec)
st = C.dedupe([x, y], NOW, audit)
check("窗口外不合并不候选", st["merged"] == 0 and st["candidates"] == 0, str(st))

print()
print("=== K) 审计日志可追溯 ===")
audit = fresh_audit()
it = [mem("k1", days_ago=100)]
C.recompute_states(it, NOW, audit)
rows = audit.read_all()
check("有日志", len(rows) == 1, str(len(rows)))
r = rows[0]
need = ["ts", "id", "action", "old_score", "new_score", "reason"]
check("六要素齐全", all(k in r for k in need), str(list(r.keys())))
check("id 正确", r["id"] == "k1", str(r.get("id")))
check("action 是 state_change", r["action"] == "state_change", str(r.get("action")))
check("reason 说得清", "分数" in r["reason"] and "未召回" in r["reason"], r["reason"])
check("带新旧状态", r.get("old_state") == "hot" and r.get("new_state") == "cold")

print()
print("=== L) dry_run 不落盘 ===")
audit = fresh_audit()
it = [mem("l1", days_ago=100)]
C.recompute_states(it, NOW, audit, dry_run=True)
check("dry_run 仍会改内存对象", it[0]["state"] == "cold", it[0]["state"])
check("dry_run 不写审计文件", audit.count() == 0, str(audit.count()))

print()
print("=== M) 报告结构完整 ===")
rep = C.ConsolidationReport()
rep.add("mid_term", {"merged": 2, "hot_to_cold": 1, "cold_to_hot": 0, "pending_archive": 3})
rep.add_tokens(prompt=100, completion=50, total=150, tag="daily_summary")
d = rep.to_dict()
check("有 summary", "summary" in d)
check("汇总数字对", d["summary"]["merged"] == 2 and d["summary"]["pending_archive"] == 3, str(d["summary"]))
check("有 token 统计", d["token_detail"]["total"] == 150, str(d["token_detail"]))
check("有计划单要求的五项", all(k in d["summary"] for k in
                                ("merged", "hot_to_cold", "cold_to_hot", "pending_archive")))

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)