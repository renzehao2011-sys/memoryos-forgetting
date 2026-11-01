"""M3 归档确认（计划单 §5.3 末三条）。

硬性要求：
  - 待归档必须人工确认后才写 state=archived；
  - 确认记录写入审计日志；
  - 硬删除（真正 DELETE）完全不允许在自动流程中出现。

因此本模块只提供两个动作：
  list_pending(items)      -> 列出待归档条目（给人和 AI 看）
  confirm_archive(items, ids, ...) -> 对**显式指定 id** 的条目写 archived

注意：没有任何 "auto_archive" 函数。这是刻意的。
"""

import os
from datetime import datetime

try:
    from .audit import ACTION_ARCHIVED
    from . import scoring as m2_scoring
except ImportError:
    from audit import ACTION_ARCHIVED
    import scoring as m2_scoring

TS_FMT = "%Y-%m-%d %H:%M:%S"


def list_pending(items):
    """列出所有已进入待归档队列、但还没归档的条目。"""
    out = []
    for item in items:
        if (item.get("state") or "hot") == "archived":
            continue
        if not item.get("pending_archive_since"):
            continue
        out.append({
            "id": item.get("id"),
            "type": item.get("type"),
            "state": item.get("state"),
            "pending_archive_since": item.get("pending_archive_since"),
            "last_used_at": item.get("last_used_at"),
            "score": round(m2_scoring.compute_score(item), 4),
            "text": m2_scoring._plain_text(item)[:120],
        })
    out.sort(key=lambda x: x.get("pending_archive_since") or "")
    return out


def confirm_archive(items, confirmed_ids, now=None, audit=None,
                    operator="", dry_run=False):
    """对显式确认的 id 写 state=archived。

    参数：
        items          全部条目（会被就地修改）
        confirmed_ids  人工确认要归档的 id 列表（必须显式给出，不接受"全部"）
        audit          AuditLog 实例
        operator       确认人标识，写进审计日志（计划单要求"确认记录写入审计日志"）

    返回统计。未知 id 会被忽略并计入 not_found。
    """
    now = now or datetime.now()
    confirmed_ids = list(confirmed_ids or [])
    stats = {"archived": 0, "not_found": 0, "already": 0, "not_pending": 0}
    wanted = set(str(x) for x in confirmed_ids)

    for item in items:
        mid = str(item.get("id"))
        if mid not in wanted:
            continue
        wanted.discard(mid)

        if (item.get("state") or "hot") == "archived":
            stats["already"] += 1
            continue
        # 只允许归档"已进入待归档队列"的条目，防止误归档活跃记忆
        if not item.get("pending_archive_since"):
            stats["not_pending"] += 1
            continue

        score = m2_scoring.compute_score(item, now)
        if not dry_run:
            item["state"] = "archived"
            item["archived_at"] = now.strftime(TS_FMT)
            item["archived_by"] = operator or "manual"
        stats["archived"] += 1
        if audit is not None:
            audit.record(ACTION_ARCHIVED, mid, score, score,
                         f"人工确认归档（操作人：{operator or 'manual'}）",
                         dry_run=dry_run,
                         old_state=item.get("state"), new_state="archived",
                         operator=operator or "manual")

    stats["not_found"] = len(wanted)
    return stats


def reject_archive(items, rejected_ids, now=None, audit=None, operator=""):
    """人工驳回归档：清掉 pending_archive_since，让条目留在 cold。

    计划单没要求这个动作，但"待归档"如果只能确认不能驳回，
    就等于逼着人要么归档要么放着，不合理。这是最小补充。
    """
    now = now or datetime.now()
    stats = {"rejected": 0}
    wanted = set(str(x) for x in (rejected_ids or []))
    for item in items:
        if str(item.get("id")) not in wanted:
            continue
        if not item.get("pending_archive_since"):
            continue
        item.pop("pending_archive_since", None)
        stats["rejected"] += 1
        if audit is not None:
            audit.record("archive_rejected", item.get("id"), None, None,
                         f"人工驳回归档，退回 cold（操作人：{operator or 'manual'}）",
                         operator=operator or "manual")
    return stats