"""M5-a 单测：容量超限不得硬删除（计划单 §4 / §5.3）。

不加载 embedding 模型：直接用 object.__new__ 造一个最小可用的 MidTermMemory，
只测 evict_lfu 的行为契约。

运行：python3 test_m5_no_hard_delete.py
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import heapq
import json
import os
import sys
import tempfile
import threading
from collections import defaultdict

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from memoryos.mid_term import MidTermMemory

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def make_mem(n, cap, path, lfu=None):
    """手工构造 MidTermMemory；lfu 可指定每条会话的访问次数。"""
    m = object.__new__(MidTermMemory)
    m.file_path = path
    m.client = None
    m.max_capacity = cap
    m.sessions = {}
    m.access_frequency = defaultdict(int)
    m.heap = []
    m.embedding_model_name = "unused"
    m.embedding_model_kwargs = {}
    m.lock = threading.Lock()
    for i in range(n):
        sid = f"s{i}"
        m.sessions[sid] = {
            "id": sid,
            "summary": f"summary-{i}",
            "details": [{"page_id": f"p{i}", "user_input": "u", "agent_response": "a"}],
            "H_segment": float(i),
            "state": "hot",
        }
        m.access_frequency[sid] = (lfu[i] if lfu else i)
        m.heap.append((-float(i), sid))
    heapq.heapify(m.heap)
    return m


def main():
    tmpdir = tempfile.mkdtemp(prefix="m5_")
    path = os.path.join(tmpdir, "mid_term.json")

    print("1. 超容量时不删除，条目与页面全保留")
    m = make_mem(5, cap=3, path=path)
    before_ids = set(m.sessions)
    before_pages = sum(len(s["details"]) for s in m.sessions.values())
    ret = m.evict_lfu()
    check("返回 True", ret is True)
    check("会话数不变", len(m.sessions) == len(before_ids), f"{len(m.sessions)} vs {len(before_ids)}")
    check("会话 id 集合不变", set(m.sessions) == before_ids)
    check("页面总数不变", sum(len(s["details"]) for s in m.sessions.values()) == before_pages)

    print("2. 只打标记，不改 state")
    marked = [sid for sid, s in m.sessions.items() if s.get("over_capacity_since")]
    check("恰好标记 1 条", len(marked) == 1, str(marked))
    check("被标记者 state 仍为 hot", m.sessions[marked[0]].get("state") == "hot",
          str(m.sessions[marked[0]].get("state")))
    check("标记里带原因", bool(m.sessions[marked[0]].get("over_capacity_reason")))
    check("选中的是 LFU 最低的 s0", marked == ["s0"], str(marked))

    print("3. 从热段堆摘除，但 access_frequency 保留")
    check("heap 中不含被标记者", all(sid != "s0" for _, sid in m.heap), str(m.heap))
    check("heap 剩余 4 条", len(m.heap) == 4, str(len(m.heap)))
    check("access_frequency 仍保留 s0", "s0" in m.access_frequency)

    print("4. 再次超限会挑下一个未标记的")
    m.evict_lfu()
    marked2 = sorted(sid for sid, s in m.sessions.items() if s.get("over_capacity_since"))
    check("标记数变 2", len(marked2) == 2, str(marked2))
    check("新增的是 s1", marked2 == ["s0", "s1"], str(marked2))
    check("会话数仍为 5", len(m.sessions) == 5)

    print("5. 全部标记后返回 False 且不再改动（幂等）")
    for _ in range(3):
        m.evict_lfu()
    marked3 = sorted(sid for sid, s in m.sessions.items() if s.get("over_capacity_since"))
    check("5 条全部标记", len(marked3) == 5, str(marked3))
    snapshot = {sid: dict(s) for sid, s in m.sessions.items()}
    ret = m.evict_lfu()
    check("返回 False", ret is False)
    check("数据无变化",
          all(m.sessions[sid].get("over_capacity_since") == snapshot[sid].get("over_capacity_since")
              for sid in snapshot))
    check("会话数仍为 5", len(m.sessions) == 5)

    print("6. 标记已落盘（save 生效）")
    with open(path, encoding="utf-8") as f:
        disk = json.load(f)
    disk_marked = [sid for sid, s in disk["sessions"].items() if s.get("over_capacity_since")]
    check("磁盘上有 5 条标记", len(disk_marked) == 5, str(disk_marked))
    check("磁盘上会话数 5", len(disk["sessions"]) == 5, str(len(disk["sessions"])))

    print("7. 源码里不再有硬删除路径（只查可执行代码，不查注释/文档串）")
    import ast
    tree = ast.parse(open(os.path.join(REPO, "memoryos/mid_term.py"), encoding="utf-8").read())

    def _is_sessions(node):
        return isinstance(node, ast.Attribute) and node.attr == "sessions"

    pop_lines, del_lines = [], []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "pop" and _is_sessions(node.func.value)):
            pop_lines.append(node.lineno)
        if isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and _is_sessions(t.value):
                    del_lines.append(node.lineno)
    check("无 self.sessions.pop(...) 调用", not pop_lines, f"行 {pop_lines}")
    check("无 del self.sessions[...]", not del_lines, f"行 {del_lines}")

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())