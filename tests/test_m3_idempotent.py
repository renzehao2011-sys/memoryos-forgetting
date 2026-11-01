"""M3 真实数据幂等验证：连续跑两次，逐字节比对数据文件。

只通过 run_consolidate.py 的离线步骤（--no-llm）验证，避免 LLM 波动干扰。
LLM 步骤的幂等由 daily_summary_exists 与 valid_to 判据保证，单独看日志即可。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import json
import os
import shutil
import subprocess
import sys

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(DATA_ROOT, "snapshot")
WORK = "/tmp/"

FILES = [
    "users/user_m0/mid_term.json",
    "users/user_m0/long_term_user.json",
    "users/user_m0/short_term.json",
    "assistants/default_assistant_profile/long_term_assistant.json",
]

FAIL = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'} {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def run(work, extra=()):
    cmd = [sys.executable, os.path.join(REPO, "run_consolidate.py"),
           "--data", work, "--no-llm"] + list(extra)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    return r


def snapshot(work):
    out = {}
    for f in FILES:
        p = os.path.join(work, f)
        with open(p, encoding="utf-8") as fh:
            out[f] = json.dumps(json.load(fh), sort_keys=True, ensure_ascii=False)
    return out


def audit_lines(work):
    p = os.path.join(work, "memory_audit.log")
    if not os.path.exists(p):
        return 0
    with open(p, encoding="utf-8") as fh:
        return len([l for l in fh if l.strip()])


if os.path.exists(WORK):
    shutil.rmtree(WORK)
shutil.copytree(SRC, WORK)

print("=== A) 第一次运行 ===")
r1 = run(WORK, ["--steps", "dedupe"])
check("第一次退出码 0", r1.returncode == 0, r1.stderr[-300:] if r1.returncode else "")
snap1 = snapshot(WORK)
audit1 = audit_lines(WORK)
rep1 = json.loads(r1.stdout)
print("  第一次 summary:", json.dumps(rep1["summary"], ensure_ascii=False))
print("  审计行数:", audit1)

print()
print("=== B) 第二次运行（同一份数据）===")
r2 = run(WORK, ["--steps", "dedupe"])
check("第二次退出码 0", r2.returncode == 0, r2.stderr[-300:] if r2.returncode else "")
snap2 = snapshot(WORK)
audit2 = audit_lines(WORK)
rep2 = json.loads(r2.stdout)
print("  第二次 summary:", json.dumps(rep2["summary"], ensure_ascii=False))
print("  审计行数:", audit2)

print()
print("=== C) 幂等判定 ===")
changed = [f for f in FILES if snap1[f] != snap2[f]]
check("所有数据文件逐字节一致", not changed, "变化: " + ", ".join(changed) if changed else "")
check("第二次 merged == 0", rep2["summary"]["merged"] == 0, str(rep2["summary"]["merged"]))
check("第二次 hot_to_cold == 0（recompute 已移除，此项应为0或缺席）", rep2["summary"].get("hot_to_cold", 0) == 0, str(rep2["summary"].get("hot_to_cold", 0)))
check("第二次 cold_to_hot == 0", rep2["summary"].get("cold_to_hot", 0) == 0, str(rep2["summary"].get("cold_to_hot", 0)))
check("第二次没有再写审计日志", audit1 == audit2, f"{audit1} -> {audit2}")

print()
print("=== D) 第一次真实做了什么 ===")
check("第一次确实合并了条目", rep1["summary"]["merged"] > 0, str(rep1["summary"]["merged"]))
check("第一次写了审计日志", audit1 > 0, str(audit1))

print()
print("=== E) 审计日志内容抽样 ===")
ap = os.path.join(WORK, "memory_audit.log")
if os.path.exists(ap):
    with open(ap, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= 3:
                break
            print("  " + line.strip())

print()
print("=== F) 没有条目被删除（数量守恒）===")
def counts(work):
    mid = json.load(open(os.path.join(work, FILES[0]), encoding="utf-8"))
    lu = json.load(open(os.path.join(work, FILES[1]), encoding="utf-8"))
    la = json.load(open(os.path.join(work, FILES[3]), encoding="utf-8"))
    n_pages = sum(len(s.get("details", [])) for s in mid["sessions"].values())
    return (len(mid["sessions"]), n_pages,
            len(lu.get("knowledge_base", [])), len(la.get("assistant_knowledge", [])))

orig = counts(SRC)
now_c = counts(WORK)
check("条目数量完全守恒（没有任何删除）", orig == now_c,
      f"原始 {orig} -> 现在 {now_c}")

print()

print()
print("=== G) recompute 已从整理任务移除（不许静默空跑）===")
r3 = run(WORK, ["--steps", "recompute"])
check("传 recompute -> 退出码 2（显式拒绝）", r3.returncode == 2, str(r3.returncode))
check("传 recompute -> 有明确说明", "已从整理任务中移除" in (r3.stdout + r3.stderr))
check("传 recompute -> 不输出 JSON 报告", not (r3.stdout.strip().startswith("{")), r3.stdout[:80])

print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)