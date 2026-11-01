"""M2 打分层自测：纯函数，不碰数据文件、不调模型。

覆盖：公式边界、τ 分类、identity 不衰减、三档阈值、缺失字段兜底、gist 文本。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import sys
from datetime import datetime, timedelta

sys.path.insert(0, REPO)
from memoryos import scoring as S

# M6 起 τ 已按新语料重新标定（episode 14->21、knowledge 30->90）。
# 本文件测的是**公式与三档机制**，与具体 τ 取值无关，所以固定成标定前的旧值，
# 让断言不随调参漂移；当前默认值由 test_m6_tau_defaults.py 单独覆盖。
S.TYPE_TAU_DAYS = {"episode": 14, "knowledge": 30, "preference": 90, "identity": None}

NOW = datetime(2026, 11, 1, 12, 0, 0)
FAIL = []


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def mem(mem_type="episode", imp=3, emo=1.0, days_ago=0, **kw):
    d = {"type": mem_type, "importance": imp, "emotion": emo,
         "last_used_at": ts(days_ago), "created_at": ts(days_ago)}
    d.update(kw)
    return d


def check(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"{'OK ' if ok else 'FAIL'} {name}: got={got} want={want}")
    if not ok:
        FAIL.append(name)


print("=== 1) 公式边界 ===")
check("fresh episode imp3 emo1 -> 3.0", S.compute_score(mem(), NOW), 3.0)
# exp(-14/14) = 0.367879
check("episode 14d -> 3*e^-1", S.compute_score(mem(days_ago=14), NOW), 3 * 0.36787944117, 1e-6)
check("episode 28d -> 3*e^-2", S.compute_score(mem(days_ago=28), NOW), 3 * 0.13533528323, 1e-6)
check("knowledge 30d -> 3*e^-1", S.compute_score(mem("knowledge", days_ago=30), NOW), 3 * 0.36787944117, 1e-6)
# P1：preference 不再随时间衰减（走 supersede 覆盖机制），3650 天与刚写入相同
check("preference 90d 不衰减 -> 3.0", S.compute_score(mem("preference", days_ago=90), NOW), 3.0)
check("preference 3650d 仍为 3.0", S.compute_score(mem("preference", days_ago=3650), NOW), 3.0)
check("preference 机制", S.mechanism_of("preference"), S.MECH_SUPERSEDE)
check("episode 机制", S.mechanism_of("episode"), S.MECH_DECAY)
check("identity 机制", S.mechanism_of("identity"), S.MECH_FREEZE)
check("identity+changing 机制", S.mechanism_of("identity", "changing"), S.MECH_SUPERSEDE)
# lifespan_days 在 decay 类里覆盖 type 级 τ：episode 默认 14，给 7 就该 7天折一半
check("lifespan_days 覆盖 τ", S.compute_score(mem(days_ago=7, lifespan_days=7), NOW), 3 * 0.36787944117, 1e-6)
check("lifespan_days 对 supersede 无效（仍不衰减）",
      S.compute_score(mem("preference", days_ago=3650, lifespan_days=30), NOW), 3.0)

print("=== 2) identity 永不衰减 ===")
check("identity 3650d 仍是 3.0", S.compute_score(mem("identity", days_ago=3650), NOW), 3.0)

print("=== 3) importance / emotion 作用 ===")
check("imp5 emo2 fresh -> 10.0", S.compute_score(mem(imp=5, emo=2.0), NOW), 10.0)
check("imp1 emo0 fresh -> 0.0", S.compute_score(mem(imp=1, emo=0.0), NOW), 0.0)

print("=== 4) 三档阈值 ===")
check("0.30 -> full", S.tier_of(0.30), "full")
check("0.2999 -> gist", S.tier_of(0.2999), "gist")
check("0.12 -> gist", S.tier_of(0.12), "gist")
check("0.1199 -> none", S.tier_of(0.1199), "none")

print("=== 5) 缺字段兜底 ===")
check("空 dict -> 3*1.0=3.0", S.compute_score({}, NOW), 3.0)
check("脏 importance -> 默认 3", S.compute_score({"importance": "abc"}, NOW), 3.0)
check("脏 type -> 按 episode", S.compute_score({"type": "???", "last_used_at": ts(14)}, NOW), 3 * 0.36787944117, 1e-6)
check("无时间戳 -> 按 0 天", S.compute_score({"importance": 3, "emotion": 1.0}, NOW), 3.0)
check("未来时间戳不算负龄", S.compute_score({"last_used_at": ts(-5)}, NOW), 3.0)

print("=== 6) 三档在真实时间轴上的落点（imp3 emo1）===")
for d in (0, 7, 14, 21, 30, 45, 60, 90):
    sc = S.compute_score(mem(days_ago=d), NOW)
    print(f"  episode {d:>3}d -> score {sc:.4f} -> {S.tier_of(sc)}")

print("=== 7) gist 文本 ===")
g = S.make_gist({"type": "episode", "text": "User: 我10岁考过了某乐器十级，是业余考级最高级。 Assistant: 记下了。",
                 "last_used_at": ts(40)}, NOW)
print("  title:", g["title"])
print("  summary:", g["summary"])
print("  text:", g["text"])
if not g["title"].startswith("模糊记忆") or "某乐器十级" not in g["summary"]:
    FAIL.append("gist 文本")

print("=== 8) 开关 ===")
import os
os.environ["USE_CUSTOM_SCORING"] = "0"
check("USE_CUSTOM_SCORING=0 -> False", S.is_enabled(), False)
os.environ["USE_CUSTOM_SCORING"] = "1"
check("USE_CUSTOM_SCORING=1 -> True", S.is_enabled(), True)
os.environ["USE_CUSTOM_SCORING"] = "true"
check("USE_CUSTOM_SCORING=true -> True", S.is_enabled(), True)
os.environ["USE_CUSTOM_SCORING"] = "off"
check("USE_CUSTOM_SCORING=off -> False", S.is_enabled(), False)
os.environ["USE_CUSTOM_SCORING"] = ""
check("USE_CUSTOM_SCORING 空串 -> False", S.is_enabled(), False)
del os.environ["USE_CUSTOM_SCORING"]
# 2026-11-11 墓碑：这一层已停用，安全默认必须落在关这边（读不到就是关）
check("未设置 -> False（默认关）", S.is_enabled(), False)

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)