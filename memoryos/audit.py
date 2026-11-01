"""M3 审计日志（计划单 §5.4）。

要求：`memory_audit.log`，每行一条：时间、id、动作、旧分、新分、原因。
任何一次状态变化都必须能回答"为什么"。

格式选择 JSONL：既是"每行一条"，又能直接被 jq/grep 检索，字段不会歧义。
"""

import json
import os
import threading

AUDIT_FILENAME = "memory_audit.log"

# 计划单 §5.3 / §7 出现的全部动作，集中定义便于校验
ACTION_STATE_CHANGE = "state_change"      # hot <-> cold
ACTION_PENDING_ARCHIVE = "pending_archive"  # 进入待归档队列（未确认）
ACTION_ARCHIVED = "archived"              # 人工确认后归档
ACTION_MERGE = "merge"                    # 去重合并（被吸收方写 valid_to）
ACTION_EXPIRE = "expire"                  # 事实失效（写 valid_to）
ACTION_DAILY_SUMMARY = "daily_summary"    # 写入日小结
ACTION_RESTORE = "restore"                # 撤销 valid_to（M7-2：可回退入口）
ACTION_STALE = "stale"                    # 超过 lifespan_days，待复核（P1）


class AuditLog:
    """追加式审计日志。只追加，不修改、不删除历史行。"""

    def __init__(self, path=None):
        self.path = path or os.path.join(os.getcwd(), AUDIT_FILENAME)
        self.lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def record(self, action, mem_id, old_score=None, new_score=None,
               reason="", level="", dry_run=False, **extra):
        """写一条。dry_run=True 时只在内存返回，不落盘。"""
        row = {
            "ts": _now(),
            "id": mem_id,
            "action": action,
            "old_score": _round(old_score),
            "new_score": _round(new_score),
            "reason": reason,
        }
        if level:
            row["level"] = level
        for k, v in extra.items():
            if v is not None:
                row[k] = v

        line = json.dumps(row, ensure_ascii=False)
        if not dry_run:
            with self.lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        return row

    def read_all(self, limit=None):
        """读回全部记录（用于测试和回查）。"""
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        if limit:
            out = out[-limit:]
        return out

    def count(self):
        return len(self.read_all())


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _round(v):
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None