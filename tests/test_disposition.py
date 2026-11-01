"""处置单自测：守「只列不处置」。

判据（每条都能用一句断言复现失败）：
  A. 被标 over_capacity 的 mid 会话、long 条目、archive 待归档，都会出现在单子上
  B. 跑完之后 4 个数据文件的字节内容完全不变（只读铁律）
  C. 不产生审计、不改 state、不删任何东西
  D. 没有待处置项时，单子明确写"本周没有待处置项"（不能静默给张空表）

运行：/usr/bin/python3 test_disposition.py
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import hashlib
import json
import os
import shutil
import subprocess
import sys

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(DATA_ROOT, "snapshot")
WORK = "/tmp/_disp_test"
PY = "/usr/bin/python3"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def snapshot(data_dir):
    out = {}
    for root, _dirs, files in os.walk(data_dir):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, data_dir)] = digest(p)
    return out


def main():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    shutil.copytree(SRC, WORK)
    u = os.path.join(WORK, "users/user_m0")
    a = os.path.join(WORK, "assistants/default_assistant_profile")

    # ---- 造三个待处置项
    mid_p = os.path.join(u, "mid_term.json")
    mid = json.load(open(mid_p, encoding="utf-8"))
    sid = next(iter(mid["sessions"]))
    mid["sessions"][sid]["over_capacity_since"] = "2026-11-01 00:00:00"
    mid["sessions"][sid]["over_capacity_reason"] = "测试用：会话数超限"
    json.dump(mid, open(mid_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    lt_p = os.path.join(u, "long_term_user.json")
    lt = json.load(open(lt_p, encoding="utf-8"))
    entry = lt["knowledge_base"][0]
    entry["over_capacity_since"] = "2026-11-01 00:00:00"
    entry["over_capacity_reason"] = "测试用：知识超限"
    entry_id = entry.get("id") or entry.get("knowledge", "")[:24]
    json.dump(lt, open(lt_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    pend_p = os.path.join(u, "long_term_user.json")
    lt2 = json.load(open(pend_p, encoding="utf-8"))
    pend = lt2["knowledge_base"][1]
    pend["pending_archive_since"] = "2026-10-01 00:00:00"
    pend_id = pend.get("id") or pend.get("knowledge", "")[:24]
    json.dump(lt2, open(pend_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    before = snapshot(WORK)

    # ---- A. 三个来源都上单
    out_md = os.path.join(WORK, "sheet.md")
    r = subprocess.run([PY, os.path.join(REPO, "run_disposition.py"), "--data", WORK,
                        "--out", out_md, "--json"], capture_output=True, text=True)
    check("A1 退出码 0", r.returncode == 0, r.stderr[-300:])
    sheet = open(out_md, encoding="utf-8").read() if os.path.exists(out_md) else ""
    check("A2 mid 会话上单", sid in sheet)
    check("A3 long 条目上单", entry_id in sheet)
    check("A4 待归档上单", pend_id in sheet)
    check("A5 单子上写明只列不处置", "只列不处置" in sheet)
    check("A6 留空的决定列", "我的决定" in sheet and "|  |" in sheet.replace("| |", "|  |"))
    try:
        summary = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        summary = {}
    check("A7 计数 >=3", summary.get("total", 0) >= 3, summary)

    # ---- B/C. 只读：除了自己写的 md，没有任何文件变化
    after = snapshot(WORK)
    after.pop("sheet.md", None)
    check("B1 数据文件字节不变", before == after,
          str({k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)}))
    check("B2 没写审计文件", not os.path.exists(os.path.join(WORK, "memory_audit.log")))
    check("B3 state 未被改", all(
        it.get("state") != "archived"
        for it in json.load(open(pend_p, encoding="utf-8"))["knowledge_base"]))

    # ---- D. 空单也要说话
    clean = "/tmp/_disp_clean"
    if os.path.exists(clean):
        shutil.rmtree(clean)
    shutil.copytree(SRC, clean)
    r2 = subprocess.run([PY, os.path.join(REPO, "run_disposition.py"), "--data", clean],
                        capture_output=True, text=True)
    check("D1 无待处置时明确说明", "本周没有待处置项" in r2.stdout)

    # ---- E. 冷启动：这张单每周跑一次，不许再拉整套 memoryos 包（约 30 秒）
    import time
    t0 = time.time()
    subprocess.run([PY, os.path.join(REPO, "run_disposition.py"), "--data", clean,
                    "--out", "/tmp/_disp_speed.md"], capture_output=True, text=True)
    dt = time.time() - t0
    check("E1 冷启动 < 5 秒（不走 memoryos 包 __init__）", dt < 5.0, f"{dt:.1f}s")

    print()
    print(f"共 {len(PASS) + len(FAIL)} 项；PASS {len(PASS)}，FAIL {len(FAIL)}")
    print("FAILED: " + (", ".join(FAIL) if FAIL else "none"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())