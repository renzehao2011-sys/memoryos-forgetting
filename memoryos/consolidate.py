"""M3 整理层（计划单 §5.3 淘汰规则 / §7 每日整理任务）。

设计约束（严格遵守计划单）：
1. **幂等**：同一份数据跑两次，第二次不产生任何数据变化（审计日志除外）。
2. **不硬删除**：任何情况下都不移除条目。淡出只用 state + valid_to 表达。
3. **待归档必须人工确认**：本模块只把条目放入待归档队列，绝不自动写 archived。
4. **只增字段**：新字段（cold_since / pending_archive_since）只补不删。
5. 确定性优先：能用 embedding 余弦判断的，不调 LLM（更便宜、更稳定、天然幂等）。

整理顺序（计划单 §7，不调换）：
    1. 去重合并
    2. 事实失效
    3. 重算分数 + 状态迁移
    4. 写小结
    5. 输出报告

本文件实现第 1、3 步与第 5 步的报告骨架；第 2、4 步需要 LLM，单独放在
`consolidate_llm.py`，以保证核心逻辑可以离线严格验证。
"""

import os
import json
import copy
from datetime import datetime

try:
    from . import scoring as m2_scoring
    from .audit import (AuditLog, ACTION_STATE_CHANGE, ACTION_PENDING_ARCHIVE,
                        ACTION_ARCHIVED, ACTION_MERGE, ACTION_RESTORE, ACTION_STALE)
    from .schema import STANDARD_FIELDS
except ImportError:
    import scoring as m2_scoring
    from audit import (AuditLog, ACTION_STATE_CHANGE, ACTION_PENDING_ARCHIVE,
                       ACTION_ARCHIVED, ACTION_MERGE, ACTION_RESTORE, ACTION_STALE)
    from schema import STANDARD_FIELDS


# ---------------------------------------------------------------- 阈值（计划单 §5.3）

COLD_SCORE = 0.05           # 分数 < 0.05
COLD_IDLE_DAYS = 90         # 且 90 天未被召回 -> cold
ARCHIVE_COLD_DAYS = 180     # cold 持续 180 天且仍未被召回 -> 待归档
MERGE_SIMILARITY = 0.92     # 去重合并的余弦阈值（同类型才比）
MERGE_WINDOW_DAYS = 7       # 只对近 7 天新增做去重

TS_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- 小工具

def _parse(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.strptime(str(ts), TS_FMT)
    except (ValueError, TypeError):
        return None


def _days_between(later, earlier):
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds() / 86400.0


def _cosine(vec_a, vec_b):
    """两个已归一化向量的点积；任一缺失返回 None。"""
    if not vec_a or not vec_b:
        return None
    if len(vec_a) != len(vec_b):
        return None
    try:
        return float(sum(a * b for a, b in zip(vec_a, vec_b)))
    except TypeError:
        return None


def level_of(item):
    """判定条目所在层级：session / page / knowledge。

    去重只在同层级内比较：session 的 summary_embedding 与 page 的
    page_embedding 语义不同（一个是整段概括，一个是单条问答），
    放一起比会得到无意义的相似度。
    """
    if not isinstance(item, dict):
        return "unknown"
    if item.get("knowledge_embedding") is not None or "knowledge" in item:
        return "knowledge"
    if "summary_embedding" in item or "summary" in item:
        return "session"
    if "page_embedding" in item or "page_id" in item:
        return "page"
    return "unknown"


def _embedding_of(item):
    for key in ("page_embedding", "knowledge_embedding", "summary_embedding"):
        vec = item.get(key)
        if vec:
            return vec
    return None


# ---------------------------------------------------------------- 第 3 步：重算 + 状态迁移

def recompute_states(items, now, audit, level="", dry_run=False):
    """重算分数并迁移 state。返回统计。

    规则（计划单 §5.3）：
        分数 < 0.05 且 90 天未被召回            -> hot 变 cold，记 cold_since
        cold 已持续 180 天且仍未召回              -> 进入待归档队列（不写 archived）
        分数回升（被召回或类型改变）              -> cold 退回 hot，清 cold_since
        archived 永不自动变更（只由人工确认写入）

    幂等性来源：cold_since / pending_archive_since 一旦写入就不再改写；
    判据全部基于已有时间戳，不依赖"本次运行"的临时状态。
    """
    stats = {"scanned": 0, "hot_to_cold": 0, "cold_to_hot": 0,
             "pending_archive": 0, "skipped_archived": 0, "stale": 0}

    for item in items:
        stats["scanned"] += 1
        state = item.get("state") or "hot"

        # 已归档：不再参与任何自动流转（只有人工确认能改它）
        if state == "archived":
            stats["skipped_archived"] += 1
            continue

        score = m2_scoring.compute_score(item, now)
        idle_days = m2_scoring.age_days(item, now)

        # P1：stale 标记——条目自己声明了 lifespan_days 且已超期。
        # 只标记、不改 state、不删除，给人工复核用（“这条还成立吗”）。
        if m2_scoring.is_stale(item, now) and not item.get("stale_since"):
            item["stale_since"] = now.strftime(TS_FMT)
            stats["stale"] += 1
            audit.record(ACTION_STALE, item.get("id"), score, score,
                         f"已 {idle_days:.0f} 天，超过 lifespan_days="
                         f"{m2_scoring.lifespan_of(item):.0f}，待人工复核",
                         level=level, dry_run=dry_run, new_state="stale")

        # 1) hot -> cold（仅 decay 类；supersede / freeze 不靠时间淘汰）
        if state == "hot":
            if m2_scoring.mechanism_of(item.get("type"),
                                       item.get("volatility")) != m2_scoring.MECH_DECAY:
                continue
            if score < COLD_SCORE and idle_days >= COLD_IDLE_DAYS:
                item["state"] = "cold"
                item["cold_since"] = now.strftime(TS_FMT)
                stats["hot_to_cold"] += 1
                audit.record(ACTION_STATE_CHANGE, item.get("id"), score, score,
                             f"分数 {score:.4f} < {COLD_SCORE} 且 {idle_days:.0f} 天未召回",
                             level=level, dry_run=dry_run,
                             old_state="hot", new_state="cold")
            continue

        # 2) cold -> 待归档 / 退回 hot
        if state == "cold":
            cold_since = _parse(item.get("cold_since"))
            cold_days = _days_between(now, cold_since)

            # 被召回（last_used_at 晚于 cold_since）说明还活着，退回 hot
            last_used = _parse(item.get("last_used_at"))
            if cold_since and last_used and last_used > cold_since:
                item["state"] = "hot"
                item.pop("cold_since", None)
                stats["cold_to_hot"] += 1
                audit.record(ACTION_STATE_CHANGE, item.get("id"), score, score,
                             "冷归档期间被召回，恢复 hot",
                             level=level, dry_run=dry_run,
                             old_state="cold", new_state="hot")
                continue

            # cold 持续够久 -> 进待归档队列（只标记，不归档）
            if cold_days is not None and cold_days >= ARCHIVE_COLD_DAYS:
                stats["pending_archive"] += 1
                # 幂等关键：只在首次进入队列时写日志与时间戳，重复运行不再产生新变化
                if not item.get("pending_archive_since"):
                    item["pending_archive_since"] = now.strftime(TS_FMT)
                    audit.record(ACTION_PENDING_ARCHIVE, item.get("id"), score, score,
                                 f"cold 已持续 {cold_days:.0f} 天（>= {ARCHIVE_COLD_DAYS}）且未召回，等待人工确认",
                                 level=level, dry_run=dry_run, new_state="pending_archive")
            continue

    return stats


# ---------------------------------------------------------------- 第 2.5 步：撤销失效（可回退）

def restore_expired(items, ids, now=None, audit=None, operator="", dry_run=False):
    """M7-2：撤销 valid_to —— 计划单 §0「每一次淡化都能被查看和回退」的入口。

    原实现的缺口：valid_to 只写不清。dedupe 合并、expire 冲突失效之后，
    **没有任何代码能把它写回 None**，只能手改 JSON，所以“可回退”实际不成立。

    本函数只做一件事，且刻意保守：
      - 只处理显式给出的 id 列表（不接受“全部”），避免一键大面积复活；
      - 只对 valid_to 非空、且未归档的条目生效；
      - 清 valid_to，保留 merged_into / superseded_by 作为历史，另记 restored_at / restored_by；
      - 写审计（action=restore），不改 state、不删任何条目。

    注意：若被撤销的那条是重复副本且副本仍在，下一次 dedupe 会再次把它标失效——
    这是设计使然（数据确实重复）。要长期保留就先把重复内容合并掉。
    """
    from datetime import datetime as _dt
    now = now or _dt.now()
    wanted = set(str(x) for x in (ids or []))
    stats = {"restored": 0, "not_found": 0, "not_expired": 0, "archived": 0}
    if not wanted:
        return stats

    for item in items:
        mid = str(item.get("id"))
        if mid not in wanted:
            continue
        wanted.discard(mid)
        if (item.get("state") or "hot") == "archived":
            stats["archived"] += 1
            continue
        if not item.get("valid_to"):
            stats["not_expired"] += 1
            continue
        old_valid_to = item.get("valid_to")
        if not dry_run:
            item["valid_to"] = None
            item["restored_at"] = now.strftime(TS_FMT)
            item["restored_by"] = operator or "manual"
        stats["restored"] += 1
        if audit is not None:
            audit.record(ACTION_RESTORE, mid, None, None,
                         f"人工撤销失效（原 valid_to={old_valid_to}，操作人：{operator or 'manual'}）",
                         level=level_of(item), dry_run=dry_run,
                         old_valid_to=old_valid_to, operator=operator or "manual")

    stats["not_found"] = len(wanted)
    return stats


# ---------------------------------------------------------------- 第 1 步：去重合并

def dedupe(items, now, audit, level="", dry_run=False,
           similarity_threshold=MERGE_SIMILARITY, window_days=MERGE_WINDOW_DAYS):
    """近 window_days 天的新条目两两比对，高度相似的合并成一条。

    合并方向：保留文本更长的（信息更全），被吸收方写 valid_to 表示失效，
    但不删除、不改变其 state。source 合并到保留方。

    幂等性来源：被吸收方一旦有 valid_to 就不再参与比较。
    返回统计。
    """
    stats = {"candidates": 0, "merged": 0}

    # 只考虑：未失效、非归档、有 embedding、在时间窗口内
    pool = []
    for item in items:
        if item.get("valid_to"):
            continue
        if (item.get("state") or "hot") == "archived":
            continue
        vec = _embedding_of(item)
        if not vec:
            continue
        created = _parse(item.get("created_at")) or _parse(item.get("last_used_at"))
        age = _days_between(now, created)
        if age is None or age > window_days:
            continue
        pool.append((item, vec, level_of(item)))

    stats["candidates"] = len(pool)
    if len(pool) < 2:
        return stats

    absorbed = set()
    for i in range(len(pool)):
        item_a, vec_a, level_a = pool[i]
        if id(item_a) in absorbed:
            continue
        for j in range(i + 1, len(pool)):
            item_b, vec_b, level_b = pool[j]
            if id(item_b) in absorbed:
                continue
            # 只有同类型才合并（episode 和 preference 不该混）
            if item_a.get("type") != item_b.get("type"):
                continue
            # 只有同层级才合并（session 概括与 page 细节不是一回事）
            if level_a != level_b:
                continue
            # 同一逻辑条目（同 id）在真实数据里会以多副本形式出现（同一 page 被写进两个
            # 会话，或同一 session 的 details 里重复两次）。它们不是"两条相似记忆"，
            # 而是同一条被存了两份：保留 pool 里靠前的一份，另一份写 valid_to 表示
            # "这份是重复副本、不再参与召回"，**不删除**（§4 / §5.3）。
            # 注意：不能简单 continue 跳过——否则重复副本永远清不掉，召回里仍出现两次。
            id_a, id_b = item_a.get("id"), item_b.get("id")
            same_id_dup = bool(id_a) and id_a == id_b
            if same_id_dup:
                sim = 1.0
            else:
                sim = _cosine(vec_a, vec_b)
                if sim is None or sim < similarity_threshold:
                    continue
            # 保留文本更长的（信息更全）；长度相同保留 id 字典序小的，保证确定性。
            # 同 id 副本则固定保留 pool 里靠前那份，避免两份互相抢。
            text_a = m2_scoring._plain_text(item_a)
            text_b = m2_scoring._plain_text(item_b)
            if same_id_dup:
                keep, drop = item_a, item_b
                keep_vec, drop_vec = vec_a, vec_b
            elif (len(text_b), str(item_b.get("id"))) > (len(text_a), str(item_a.get("id"))):
                keep, drop = item_b, item_a
                keep_vec, drop_vec = vec_b, vec_a
            else:
                keep, drop = item_a, item_b
                keep_vec, drop_vec = vec_a, vec_b

            score_drop = m2_scoring.compute_score(drop, now)

            # 被吸收方：写 valid_to（失效，不删），记录被谁吸收
            if not dry_run:
                drop["valid_to"] = now.strftime(TS_FMT)
                drop["merged_into"] = keep.get("id")
                # 合并 source（计划单 §7.1：合并 source）
                keep_src = keep.get("source") or ""
                drop_src = drop.get("source") or ""
                if drop_src and drop_src not in keep_src.split("+"):
                    keep["source"] = (keep_src + "+" + drop_src).strip("+")

            absorbed.add(id(drop))
            stats["merged"] += 1
            if same_id_dup:
                reason = (f"同 id 重复副本（{id_a}），保留首份，本份不再召回"
                          f"（相似度 {sim:.3f}，计划单 §7.1 去重）")
            else:
                reason = (f"与 {keep.get('id')} 相似度 {sim:.3f} >= {similarity_threshold}，"
                          f"内容并入保留方")
            audit.record(ACTION_MERGE, drop.get("id"), score_drop, 0.0,
                         reason, level=level, dry_run=dry_run,
                         kept_id=keep.get("id"), similarity=round(sim, 4),
                         dedup_reason=("same_id_duplicate" if same_id_dup else "similar"))

    return stats


# ---------------------------------------------------------------- 第 5 步：报告

class ConsolidationReport:
    def __init__(self):
        self.started = datetime.now()
        self.sections = {}
        self.tokens = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}
        self.by_tag = {}

    def add(self, name, stats):
        self.sections[name] = stats

    def add_tokens(self, prompt=0, completion=0, total=0, tag="unknown"):
        self.tokens["calls"] += 1
        self.tokens["prompt"] += prompt
        self.tokens["completion"] += completion
        self.tokens["total"] += total
        rec = self.by_tag.setdefault(tag, {"calls": 0, "prompt": 0,
                                           "completion": 0, "total": 0})
        rec["calls"] += 1
        rec["prompt"] += prompt
        rec["completion"] += completion
        rec["total"] += total

    def to_dict(self):
        elapsed = (datetime.now() - self.started).total_seconds()
        merged = sum(s.get("merged", 0) for s in self.sections.values())
        hot_to_cold = sum(s.get("hot_to_cold", 0) for s in self.sections.values())
        cold_to_hot = sum(s.get("cold_to_hot", 0) for s in self.sections.values())
        pending = sum(s.get("pending_archive", 0) for s in self.sections.values())
        stale = sum(s.get("stale", 0) for s in self.sections.values())
        return {
            "phase": "consolidate",
            "elapsed_s": round(elapsed, 2),
            "tokens": self.tokens["total"],
            "token_detail": self.tokens,
            "by_tag": self.by_tag,
            "summary": {
                "merged": merged,
                "hot_to_cold": hot_to_cold,
                "cold_to_hot": cold_to_hot,
                "pending_archive": pending,
                "stale": stale,
            },
            "sections": self.sections,
        }

    def print_json(self):
        print(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 数据装载

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def iter_mid_term_items(data):
    """遍历 mid_term 结构里的全部标准条目（session + page）。"""
    for _sid, session in (data.get("sessions") or {}).items():
        yield session
        for page in session.get("details", []) or []:
            yield page


def iter_long_term_items(data):
    for key in ("knowledge_base", "assistant_knowledge"):
        for entry in data.get(key, []) or []:
            yield entry