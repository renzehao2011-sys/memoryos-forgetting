"""M3 归档确认测试：重点验证「绝不可能自动归档」。

不调模型、不碰真实数据。
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
from memoryos import archive as A
from memoryos import consolidate as C
from memoryos.audit import AuditLog

NOW = datetime(2026, 11, 1, 12, 0, 0)
FAIL = []


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def mem(mid, state="cold", pending=None, days_ago=400, **kw):
    d = {"id": mid, "text": f"内容 {mid}", "type": "episode", "importance": 3,
         "emotion": 1.0, "state": state, "created_at": ts(days_ago),
         "last_used_at": ts(days_ago), "used_count": 0,
         "valid_from": ts(days_ago), "valid_to": None, "source": "test"}
    if pending is not None:
        d["pending_archive_since"] = pending
    d.update(kw)
    return d


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'} {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="m3arch_")
AUDIT_PATH = os.path.join(TMP, "memory_audit.log")


def fresh_audit():
    if os.path.exists(AUDIT_PATH):
        os.remove(AUDIT_PATH)
    return AuditLog(AUDIT_PATH)


print("=== A) 模块里不存在任何自动归档入口 ===")
names = [n for n in dir(A) if not n.startswith("_")]
check("没有 auto_archive 之类函数",
      not any("auto" in n.lower() and "arch" in n.lower() for n in names),
      str(names))
check("只有 confirm_archive 会写 archived",
      "confirm_archive" in names)

print()
print("=== B) 整理流程跑完，绝不会产生 archived ===")
audit = fresh_audit()
items = [
    mem("b1", state="hot", days_ago=400),        # 会被转 cold
    mem("b2", state="cold", pending=ts(200), days_ago=500),  # 早已待归档
    mem("b3", state="hot", days_ago=1),
]
C.recompute_states(items, NOW, audit)
states = [i["state"] for i in items]
check("没有任何条目变成 archived", "archived" not in states, str(states))

print()
print("=== C) 待归档队列能列出来 ===")
pending = A.list_pending(items)
check("列出 1 条待归档", len(pending) == 1, str(len(pending)))
check("就是 b2", pending and pending[0]["id"] == "b2", str(pending[0]["id"] if pending else None))
check("带分数和文本", pending and "score" in pending[0] and "text" in pending[0])

print()
print("=== D) 人工确认后才会归档 ===")
audit = fresh_audit()
stats = A.confirm_archive(items, ["b2"], now=NOW, audit=audit, operator="user")
check("归档了 1 条", stats["archived"] == 1, str(stats))
check("b2 现在是 archived", items[1]["state"] == "archived", items[1]["state"])
check("写了 archived_at", bool(items[1].get("archived_at")))
check("记录了操作人", items[1].get("archived_by") == "user", str(items[1].get("archived_by")))
rows = audit.read_all()
check("审计日志有 archived 记录",
      any(r["action"] == "archived" and r["id"] == "b2" for r in rows), str([r["action"] for r in rows]))
check("日志里带操作人", any(r.get("operator") == "user" for r in rows))

print()
print("=== E) 归档后原文仍在（不是删除）===")
check("文本仍可读", items[1].get("text") == "内容 b2", str(items[1].get("text")))
check("条目还在列表里", len(items) == 3)

print()
print("=== F) 不能归档非待归档条目（防误操作）===")
audit = fresh_audit()
active = [mem("f1", state="hot", days_ago=1)]   # 活跃记忆，没有 pending 标记
stats = A.confirm_archive(active, ["f1"], now=NOW, audit=audit, operator="user")
check("拒绝归档活跃条目", stats["archived"] == 0 and stats["not_pending"] == 1, str(stats))
check("f1 仍是 hot", active[0]["state"] == "hot", active[0]["state"])

print()
print("=== G) 未知 id 被忽略 ===")
audit = fresh_audit()
stats = A.confirm_archive(items, ["不存在的id"], now=NOW, audit=audit)
check("not_found=1", stats["not_found"] == 1, str(stats))
check("没归档任何东西", stats["archived"] == 0)

print()
print("=== H) 重复确认是幂等的 ===")
audit = fresh_audit()
stats2 = A.confirm_archive(items, ["b2"], now=NOW, audit=audit, operator="user")
check("第二次计入 already", stats2["already"] == 1, str(stats2))
check("archived 不重复计数", stats2["archived"] == 0, str(stats2))
check("没写新的审计记录", audit.count() == 0, str(audit.count()))

print()
print("=== I) 可以驳回归档 ===")
audit = fresh_audit()
pend = [mem("i1", state="cold", pending=ts(10), days_ago=400)]
stats = A.reject_archive(pend, ["i1"], now=NOW, audit=audit, operator="user")
check("驳回成功", stats["rejected"] == 1, str(stats))
check("pending 标记被清掉", not pend[0].get("pending_archive_since"))
check("仍是 cold（没被删）", pend[0]["state"] == "cold", pend[0]["state"])
check("写了驳回日志", any(r["action"] == "archive_rejected" for r in audit.read_all()))

print()
print("=== J) dry_run 不改数据 ===")
audit = fresh_audit()
d = [mem("j1", state="cold", pending=ts(10), days_ago=400)]
snap = json.dumps(d, sort_keys=True, ensure_ascii=False)
A.confirm_archive(d, ["j1"], now=NOW, audit=audit, dry_run=True)
check("dry_run 后数据未变", json.dumps(d, sort_keys=True, ensure_ascii=False) == snap)
check("dry_run 不写日志", audit.count() == 0, str(audit.count()))

print()
print("=== K) 归档后的条目不再参与自动流转 ===")
audit = fresh_audit()
arch = [mem("k1", state="archived", days_ago=3000)]
C.recompute_states(arch, NOW, audit)
check("仍是 archived", arch[0]["state"] == "archived", arch[0]["state"])
check("没有写日志", audit.count() == 0, str(audit.count()))

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)