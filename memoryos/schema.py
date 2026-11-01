"""M1 数据模型迁移模块（计划单第 4 节 / 第 10 节 M1）。

设计约束（严格遵守计划单）：
1. 只补字段，不删旧字段。已有字段一概不覆盖。
2. 迁移幂等：同一份数据跑 N 次，结果与跑 1 次完全相同。
3. 不改变任何既有读写路径的行为；本模块只被各存储层的 load() 调用一次。
4. 生命周期中不存在 delete，只有 state=hot|cold|archived。

对外接口：
    migrate_session(session)            -> 补齐 12 个标准字段（session 级）
    migrate_page(page)                  -> 补齐 12 个标准字段（页/单条问答级）
    migrate_knowledge_entry(entry)      -> 补齐 12 个标准字段（知识条目级）
    migrate_mid_term(data)              -> 迁移整个 mid_term.json 结构
    migrate_long_term(data)             -> 迁移整个 long_term_user.json 结构
    migrate_short_term(data)            -> 迁移整个 short_term.json 结构
    audit_completeness(obj)             -> 返回缺失字段列表（用于验收）
"""

import hashlib

# ---------------------------------------------------------------- 常量
# 计划单 5.1：τ 按类型给，单位天。identity 用 None 表示 ∞（永不衰减）。
#
# M6 标定（2026-11-01，用 42 条语料 / 12 题，见 M6_REPORT.md）：
#   episode   14 -> 21 : 45 天档 τ=14 会让"上次作文用什么骨架"这类近期具体事
#                        提前变模糊（压缩 2% 的代价是掉 1 题），21 天更贴"三周后才糊"。
#   knowledge 30 -> 90 : **这是本轮最重要的改动**。原值 30 天意味着"学到的做法"
#                        三个月就归零，120 天档召回从 12/12 掉到 6/12（杀球怎么改、
#                        备件哪个最贵全忘）。改 90 天后 120 天档 11/12。
#   preference 90 保持 : 网格里 90/180 没有可测差异。
#   identity   ∞ 保持  : 由 volatility 再分 stable / changing（见 CHANGING_IDENTITY_TAU_DAYS）。
TYPE_TAU_DAYS = {
    "episode": 21,
    "knowledge": 90,
    "preference": None,   # P1：走 supersede（valid_to），不靠 τ
    "identity": None,   # None == ∞（仅 stable 子类）
}

MEMORY_TYPES = tuple(TYPE_TAU_DAYS.keys())
STATES = ("hot", "cold", "archived")

# 计划单 4：每条记忆至少包含的字段
STANDARD_FIELDS = (
    "id", "text", "type", "importance", "emotion",
    "created_at", "last_used_at", "used_count", "state",
    "valid_from", "valid_to", "source",
)

DEFAULT_IMPORTANCE = 3      # 计划单 7.4：总结条目 importance 默认 3
DEFAULT_EMOTION = 1.0       # 计划单 4：中性 = 1


# ---------------------------------------------------------------- 工具

def _stable_id(prefix, seed_text):
    """为没有 id 的旧条目生成确定性 id，保证多次迁移不变。"""
    digest = hashlib.sha1(seed_text.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _set_if_absent(obj, key, value):
    """只增不删、不覆盖已有值。返回是否实际写入。

    注意：判据是「键是否存在」，不是「值是否为 None」。
    因为 valid_to / valid_from 的合法值就是 None（表示仍有效/未设置），
    若把 None 当缺失，迁移就会每次重写这三个字段，破坏幂等性。
    """
    if key in obj:
        return False
    obj[key] = value
    return True


def _apply_common_fields(obj, level, *, item_id, text, mem_type,
                         created_at, last_used_at=None, used_count=0,
                         state="hot", valid_from=None, valid_to=None,
                         source=None):
    """统一落地标准字段。返回补了几个字段。"""
    added = 0
    added += _set_if_absent(obj, "id", item_id)
    added += _set_if_absent(obj, "text", text)
    added += _set_if_absent(obj, "type", mem_type)
    added += _set_if_absent(obj, "importance", DEFAULT_IMPORTANCE)
    added += _set_if_absent(obj, "emotion", DEFAULT_EMOTION)
    added += _set_if_absent(obj, "created_at", created_at)
    added += _set_if_absent(obj, "last_used_at", last_used_at or created_at)
    added += _set_if_absent(obj, "used_count", used_count)
    added += _set_if_absent(obj, "state", state)
    added += _set_if_absent(obj, "valid_from", valid_from)
    added += _set_if_absent(obj, "valid_to", valid_to)
    added += _set_if_absent(obj, "source", source or level)
    # valid_from/valid_to 允许且通常是 None；_set_if_absent 只看"键是否存在"，
    # 所以 None 也只会被写一次，第二次迁移不会重复计入。
    return added


# ---------------------------------------------------------------- 三个层级

def migrate_session(session):
    """session 级：一个会话段落 = 一段 episode。

    映射依据（旧字段 -> 新字段）：
        id            <- session["id"]
        text          <- session["summary"]
        created_at    <- session["timestamp"]
        last_used_at  <- session["last_visit_time"]
        used_count    <- session["N_visit"]
    """
    if not isinstance(session, dict):
        return 0
    sid = session.get("id") or _stable_id("session", session.get("summary", ""))
    created = session.get("timestamp")
    return _apply_common_fields(
        session, "mid_term_session",
        item_id=sid,
        text=session.get("summary", ""),
        mem_type="episode",
        created_at=created,
        last_used_at=session.get("last_visit_time") or created,
        used_count=session.get("N_visit", 0),
        state="hot",
        valid_from=created,
        valid_to=None,
        source="mid_term",
    )


def migrate_page(page):
    """页级：一条 user/assistant 问答对 = 一条 episode。"""
    if not isinstance(page, dict):
        return 0
    user_input = page.get("user_input", "")
    agent_response = page.get("agent_response", "")
    combined = f"User: {user_input}\nAssistant: {agent_response}".strip()
    pid = page.get("page_id") or _stable_id("page", combined)
    ts = page.get("timestamp")
    return _apply_common_fields(
        page, "mid_term_page",
        item_id=pid,
        text=combined,
        mem_type="episode",
        created_at=ts,
        last_used_at=ts,
        used_count=0,
        state="hot",
        valid_from=ts,
        valid_to=None,
        source="mid_term",
    )


def migrate_knowledge_entry(entry):
    """知识条目级。

    type 默认 knowledge，但若文本已能明确判定为偏好/身份，
    M2 接 LLM 打分时再改；M1 阶段一律先落 knowledge，避免误判。
    """
    if not isinstance(entry, dict):
        return 0
    text = entry.get("knowledge", "")
    eid = entry.get("id") or _stable_id("know", text)
    ts = entry.get("timestamp")
    return _apply_common_fields(
        entry, "long_term",
        item_id=eid,
        text=text,
        mem_type="knowledge",
        created_at=ts,
        last_used_at=ts,
        used_count=0,
        state="hot",
        valid_from=None,
        valid_to=None,
        source="long_term",
    )


# ---------------------------------------------------------------- 整文件迁移

def migrate_mid_term(data):
    """迁移 mid_term.json: {"sessions": {sid: {...}}, "access_frequency": {...}}"""
    stats = {"sessions": 0, "pages": 0, "fields_added": 0, "sessions_touched": 0}
    if not isinstance(data, dict):
        return stats
    for _sid, session in (data.get("sessions") or {}).items():
        before = _count_standard(session)
        stats["fields_added"] += migrate_session(session)
        stats["sessions"] += 1
        if _count_standard(session) != before:
            stats["sessions_touched"] += 1
        for page in session.get("details", []) or []:
            stats["fields_added"] += migrate_page(page)
            stats["pages"] += 1
    return stats


def migrate_long_term(data):
    """迁移 long_term_user.json / long_term_assistant.json"""
    stats = {"knowledge": 0, "fields_added": 0}
    if not isinstance(data, dict):
        return stats
    for key in ("knowledge_base", "assistant_knowledge"):
        for entry in data.get(key, []) or []:
            stats["fields_added"] += migrate_knowledge_entry(entry)
            stats["knowledge"] += 1
    return stats


def migrate_short_term(data):
    """短时记忆是一条条 QA pair，同样补字段，便于日后统一流转。"""
    stats = {"items": 0, "fields_added": 0}
    if not isinstance(data, list):
        return stats
    for item in data:
        stats["fields_added"] += migrate_page(item)
        stats["items"] += 1
    return stats


# ---------------------------------------------------------------- 验收

def _count_standard(obj):
    if not isinstance(obj, dict):
        return 0
    return sum(1 for f in STANDARD_FIELDS if f in obj)


def audit_completeness(obj):
    """返回该对象缺失的标准字段列表；空列表表示合规。"""
    if not isinstance(obj, dict):
        return list(STANDARD_FIELDS)
    return [f for f in STANDARD_FIELDS if f not in obj]
