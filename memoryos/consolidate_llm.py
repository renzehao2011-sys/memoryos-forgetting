"""M3 整理层：需要 LLM 的两步（计划单 §7.2 事实失效 / §7.4 写小结）。

与 consolidate.py 分开的原因：
  - consolidate.py 是确定性逻辑，能离线严格验证幂等；
  - 本文件调 LLM，结果天然有波动，所以每一步都要求"可失败、可跳过、不阻塞"。

设计要点：
1. 事实失效（§7.2）：只对"高相似但内容冲突"的条目做判断。冲突判定必须调 LLM，
   但**只在候选对很少时才调**，避免每天烧 token。
2. 写小结（§7.4）：一句话总结今天发生了什么，作为 episode 入库，importance=3。
   同一天重复运行不应重复入库（否则幂等被破坏）——用 `daily_summary:YYYY-MM-DD`
   作为固定 source，先查再写。
"""

import json
import os
from datetime import datetime

try:
    from . import scoring as m2_scoring
    from .audit import ACTION_EXPIRE, ACTION_DAILY_SUMMARY
    from .consolidate import (COLD_SCORE, TS_FMT, _parse, _cosine,
                              _embedding_of, _days_between, level_of)
except ImportError:
    import scoring as m2_scoring
    from audit import ACTION_EXPIRE, ACTION_DAILY_SUMMARY
    from consolidate import (COLD_SCORE, TS_FMT, _parse, _cosine,
                             _embedding_of, _days_between, level_of)


# 冲突候选：相似度只用来「筛候选」，不用来「判冲突」。
#
# 实测标定（paraphrase-multilingual-MiniLM-L12-v2，11 组对照）：
#   真冲突（住址/机型/方案/价格/年级/作息变化）: 0.53 - 0.75，均值 0.63
#   非冲突（无关话题）: 0.08 - 0.29
#   非冲突（同主题加细节/不同属性）: 0.63 附近，与真冲突重叠
# 两类在 0.52-0.63 重叠，相似度无法单独判定冲突。
# 因此下限取 0.40：宁可多筛几条交给 LLM 判，也不要漏掉真冲突
# （初版取 0.60，实测漏掉了 0.5261 的「住址变化」）。
CONFLICT_SIM_MIN = 0.40
CONFLICT_SIM_MAX = 0.92          # >= 0.92 视为重复，由第 1 步去重处理
CONFLICT_CANDIDATE_LIMIT = 8      # 单次最多判断这么多对，控制 token

CONFLICT_SYSTEM_PROMPT = (
    "You are a memory conflict detector. Decide whether the NEW memory invalidates the OLD one. "
    "A conflict means they describe the same fact/attribute but with different values, "
    "so the old one is no longer true. Reply with JSON only."
)

CONFLICT_USER_PROMPT = """For each pair, decide if the NEW memory makes the OLD memory outdated.

Rules:
- conflict=true only when both describe the SAME attribute of the SAME subject with DIFFERENT values.
- conflict=false if they are about different subjects, different attributes, or mutually compatible.
- Adding detail is NOT a conflict.

Output JSON array, same order and count as input:
[{"i":1,"conflict":true,"reason":"short reason"}]

Pairs:
{pairs}
"""

DAILY_SUMMARY_SYSTEM_PROMPT = (
    "You write one extremely short diary line summarizing what happened today. "
    "Be factual and concrete. No greetings, no explanation."
)

DAILY_SUMMARY_USER_PROMPT = """Write ONE sentence (max 40 Chinese characters or 25 English words)
summarizing what the user did/discussed today, based on these memory entries.

Entries:
{entries}

Output only the sentence."""


# ---------------------------------------------------------------- 事实失效

def find_conflict_candidates(old_items, new_items, limit=CONFLICT_CANDIDATE_LIMIT):
    """找出「旧的仍有效、新的更新、内容可能冲突」的候选对。

    只用 embedding 相似度筛选，不做判断——判断交给 LLM。
    """
    pairs = []
    for old in old_items:
        if old.get("valid_to"):
            continue
        if (old.get("state") or "hot") == "archived":
            continue
        vec_old = _embedding_of(old)
        if not vec_old:
            continue
        t_old = _parse(old.get("created_at"))
        if t_old is None:
            continue

        for new in new_items:
            if new is old:
                continue
            # M7-4：只在同层级内判冲突。原实现把 mid_term 的 page 和 long_term 的 knowledge
            # 放在一起比（调用方传的是同一个 all_items），相似度下限又只有 0.40，
            # 一条 page 被判“被取代”就会写 valid_to，从此不再被召回。
            if level_of(old) != level_of(new):
                continue
            t_new = _parse(new.get("created_at"))
            if t_new is None or t_new <= t_old:
                continue
            vec_new = _embedding_of(new)
            if not vec_new:
                continue
            sim = _cosine(vec_old, vec_new)
            if sim is None:
                continue
            if CONFLICT_SIM_MIN <= sim < CONFLICT_SIM_MAX:
                pairs.append((sim, old, new))

    # 相似度高的优先，且限制数量控制 token
    pairs.sort(key=lambda x: x[0], reverse=True)
    return pairs[:limit]


def detect_conflicts(pairs, client, model, report=None):
    """让 LLM 判定候选对是否真冲突。返回 [(old, new, reason)]。

    失败/解析不了时返回空列表：宁可这次不失效，也不要误删有效记忆。
    """
    if not pairs:
        return []

    listing = []
    for i, (sim, old, new) in enumerate(pairs, 1):
        old_text = m2_scoring._plain_text(old)[:200]
        new_text = m2_scoring._plain_text(new)[:200]
        listing.append(f'{i}. OLD: {old_text}\n   NEW: {new_text}')

    try:
        raw = client.chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": CONFLICT_SYSTEM_PROMPT},
                {"role": "user",
                 "content": CONFLICT_USER_PROMPT.replace("{pairs}", "\n".join(listing))},
            ],
            temperature=0.1,
            max_tokens=600,
        )
    except Exception as e:
        print(f"consolidate_llm: conflict detection failed ({e}); skipping expire step.")
        return []

    verdicts = _parse_json_array(raw, len(pairs))
    out = []
    for (sim, old, new), v in zip(pairs, verdicts):
        if v.get("conflict"):
            out.append((old, new, v.get("reason") or f"与更新记忆冲突（相似度 {sim:.2f}）"))
    return out


def expire_old_facts(conflicts, now, audit, level="", dry_run=False):
    """给冲突中较旧的一方写 valid_to（失效，但不删除、不改 state）。"""
    stats = {"expired": 0}
    stamp = now.strftime(TS_FMT)
    for old, new, reason in conflicts:
        if old.get("valid_to"):
            continue
        score_old = m2_scoring.compute_score(old, now)
        if not dry_run:
            old["valid_to"] = stamp
            old["superseded_by"] = new.get("id")
        stats["expired"] += 1
        audit.record(ACTION_EXPIRE, old.get("id"), score_old, 0.0,
                     f"被更新记忆 {new.get('id')} 取代：{reason}",
                     # M7-4：原本传的是空 level，审计里看不出是 page 还是 knowledge
                     level=level or level_of(old), dry_run=dry_run,
                     superseded_by=new.get("id"))
    return stats


# ---------------------------------------------------------------- 写小结

def build_daily_summary(entries, client, model):
    """让 LLM 把今天的条目压成一句话。失败返回 None。"""
    if not entries:
        return None
    texts = []
    for e in entries:
        t = m2_scoring._plain_text(e)
        if t:
            texts.append("- " + t[:150])
    if not texts:
        return None

    try:
        raw = client.chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": DAILY_SUMMARY_SYSTEM_PROMPT},
                {"role": "user",
                 "content": DAILY_SUMMARY_USER_PROMPT.replace("{entries}", "\n".join(texts[:40]))},
            ],
            temperature=0.3,
            max_tokens=200,
        )
    except Exception as e:
        print(f"consolidate_llm: daily summary failed ({e}); skipping.")
        return None

    line = (raw or "").strip().strip('"').strip()
    if not line or line.lower() in ("none", "null"):
        return None
    # 只取第一行，防止模型多话
    return line.split("\n")[0][:200]


def summary_source_for(day):
    """日小结的固定 source 标记，用于幂等判重。"""
    return f"daily_summary:{day}"


def daily_summary_exists(entries, day):
    tag = summary_source_for(day)
    for e in entries:
        if (e.get("source") or "") == tag:
            return True
    return False


def write_daily_summary(summary_text, entries, long_term_memory, day,
                        audit, dry_run=False):
    """把小结作为一条 episode 写入长期记忆。已存在同一天的则跳过（幂等）。"""
    if not summary_text:
        return {"written": 0, "skipped": "no_text"}
    if daily_summary_exists(entries, day):
        return {"written": 0, "skipped": "already_exists"}

    if dry_run:
        audit.record(ACTION_DAILY_SUMMARY, summary_source_for(day), None, None,
                     "dry-run：本应写入日小结", dry_run=True)
        return {"written": 1, "skipped": None}

    score = {"importance": 3, "emotion": 1.0, "type": "episode"}  # 计划单 §7.4
    long_term_memory.add_user_knowledge(summary_text, score=score)

    # 打上固定 source，供下次判重
    for e in reversed(list(entries)):
        if (e.get("source") or "") in ("long_term", ""):
            e["source"] = summary_source_for(day)
            break

    audit.record(ACTION_DAILY_SUMMARY, summary_source_for(day), None, 3.0,
                 f"写入 {day} 的日小结", new_state="hot")
    return {"written": 1, "skipped": None}


# ---------------------------------------------------------------- 解析

def _parse_json_array(raw, expected):
    """解析 LLM 的 JSON 数组，长度对齐 expected，失败给全 False。"""
    out = [{"conflict": False, "reason": ""} for _ in range(expected)]
    if not raw:
        return out
    body = raw.strip()
    lb, rb = body.find("["), body.rfind("]")
    if lb == -1 or rb == -1 or rb < lb:
        return out
    try:
        data = json.loads(body[lb: rb + 1])
    except (ValueError, TypeError):
        return out
    if not isinstance(data, list):
        return out
    for pos, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        idx = item.get("i")
        try:
            idx = int(idx) - 1 if idx is not None else pos
        except (TypeError, ValueError):
            idx = pos
        if 0 <= idx < expected:
            out[idx] = {"conflict": bool(item.get("conflict")),
                        "reason": str(item.get("reason") or "")[:120]}
    return out