"""M2 打分层（计划单第 5.1 / 5.2 节）。

**本层已于 2026-11-11 判定为负并停用**（见 `FORGETTING_BETA.md` 顶部墓碑）。
代码保留：一是可复现历史结论，二是给"重启判据"留靶子。生产默认关。

公式：score = importance * emotion * exp(-Δt / τ)，Δt = now - last_used_at（天）。
三档召回：
    score >= 0.30           -> full  完整原文
    0.12 <= score < 0.30    -> gist  只给标题 + 一句话
    score < 0.12            -> none  不召回

设计约束（严格遵守计划单）：
1. 本模块是纯计算：无 IO、无 LLM、无第三方依赖，可独立单测。
2. `is_enabled()` **默认 False**：`USE_CUSTOM_SCORING` 读不到、为空、非真值，一律按关。
   安全默认落在关这边，不靠"记得带环境变量"。显式 `=1` 才打开。
3. 本模块不写任何数据字段。state 迁移（cold / archived）与审计日志属 M3。
"""

import math
import os
from datetime import datetime

try:
    from .schema import TYPE_TAU_DAYS, DEFAULT_IMPORTANCE, DEFAULT_EMOTION
except ImportError:
    from schema import TYPE_TAU_DAYS, DEFAULT_IMPORTANCE, DEFAULT_EMOTION

# ---------------------------------------------------------------- 阈值

FULL_THRESHOLD = 0.30
GIST_THRESHOLD = 0.12

TIER_FULL = "full"
TIER_GIST = "gist"
TIER_NONE = "none"

TS_FMT = "%Y-%m-%d %H:%M:%S"

# 未知 type 一律按 episode 处理：宁可让冷门数据先变糊，也不要因为字段脏而永不衰减。
_FALLBACK_TYPE = "episode"

# identity 内部再分两档：stable（永不衰减）/ changing（会过期，用有限 τ）。
# 计划单 §4 的 type 枚举只有 4 个值，所以不新增 type，改用独立字段表达。
VOLATILITY_STABLE = "stable"
VOLATILITY_CHANGING = "changing"
VOLATILITIES = (VOLATILITY_STABLE, VOLATILITY_CHANGING)

# P1：遗忘不是一件事，是三种机制。用一条衰减曲线管四种东西，会逼着人
# “既让它糊又让它别糊”，最后只能靠调和参数互相抵消。
MECH_DECAY = "decay"          # 随时间变糊：episode / knowledge —— τ 只对这有效
MECH_SUPERSEDE = "supersede"  # 变旧=被新的替代，靠 valid_to 失效：preference / changing identity
MECH_FREEZE = "freeze"        # 冻结，只能人工改：identity(stable)


def _normalize_type(mem_type):
    return mem_type if mem_type in TYPE_TAU_DAYS else _FALLBACK_TYPE


def _to_positive_float(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def mechanism_of(mem_type, volatility=None):
    """这条记忆靠什么“消失”。（决定 τ 有没有意义）"""
    t = _normalize_type(mem_type)
    if t in ("episode", "knowledge"):
        return MECH_DECAY
    if t == "preference":
        return MECH_SUPERSEDE
    if t == "identity":
        return (MECH_SUPERSEDE
                if (volatility or "").strip().lower() == VOLATILITY_CHANGING
                else MECH_FREEZE)
    return MECH_DECAY


def lifespan_of(memory):
    """单条寿命（天）。P1 起寿命下沉成字段，不再靠新增 type 表达。"""
    if not isinstance(memory, dict):
        return None
    return _to_positive_float(memory.get("lifespan_days"))


def is_stale(memory, now=None):
    """已超过自己声明的寿命 -> 待复核（只打标记，不改 state、不删除）。"""
    ls = lifespan_of(memory)
    if ls is None:
        return False
    return age_days(memory, now) > ls


# ---------------------------------------------------------------- 开关

def is_enabled():
    """打分层开关。**默认关**：读不到变量、空串、任何非真值，一律按关处理。

    为什么默认关（2026-11-11 墓碑）：这一层经真实数据判定不合格、已停用。
    "安全默认"必须落在关这边——换机器、换启动方式、忘带环境变量时，
    不该让一个没人复核的打分悄悄复活。加进启动脚本只能算第二道防线。

    显式打开：USE_CUSTOM_SCORING=1（只用于复现历史结论、或验证重启判据）。
    """
    return os.environ.get("USE_CUSTOM_SCORING", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------- 基础

def _parse_ts(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.strptime(str(ts), TS_FMT)
    except (ValueError, TypeError):
        return None


def tau_days_for(mem_type, volatility=None, lifespan_days=None):
    """返回该条目的 τ（天）；None = 不随时间衰减。

    P1：τ 只对 decay 类有效。supersede / freeze 返回 None ——
    它们不靠时间淡出，靠 valid_to（被新事实覆盖）或人工修改。
    单条写了 lifespan_days 时，在 decay 类里覆盖 type 级 τ（寿命下沉成字段）。
    """
    if mechanism_of(mem_type, volatility) != MECH_DECAY:
        return None
    ls = _to_positive_float(lifespan_days)
    if ls is not None:
        return ls
    return TYPE_TAU_DAYS[_normalize_type(mem_type)]


# 已废弃（P1）：changing identity 改走 supersede 覆盖机制，不再有 τ。
# 保留常量只为兼容旧脚本的 import；score 计算不再读它。
CHANGING_IDENTITY_TAU_DAYS = 180.0

# 关键词兜底：LLM 没给 volatility、或对历史数据做回填时用。
# 只处理 identity，宁可多标 changing（会自然淡出），不可漏标 stable（永不消失）。
_VOLATILITY_HINTS = (
    "班", "年级", "学校", "就读", "城市", "住在", "住址", "小区", "门牌",
    "成绩", "分数", "排名", "前3", "前 3", "考了", "中考", "高考", "单位",
    "公司", "职位", "职务", "岗位", "现居", "目前",
)


def guess_volatility(memory):
    """给一条 identity 记忆猜 stable / changing。

    - 只对 identity 生效；其它类型一律 stable（τ 已按类型给定，不看这个字段）。
    - 命中关键词 → changing；否则 stable。
    - 命中 "十级 / 钢琴 / 出生 / 姓 / 母语" 这类明确不变的词时优先判 stable。
    """
    if (memory.get("type") or "") != "identity":
        return VOLATILITY_STABLE
    text = " ".join(str(memory.get("text") or memory.get("summary")
                        or memory.get("user_input") or memory.get("knowledge") or "").split())
    if not text:
        return VOLATILITY_STABLE
    for word in ("十级", "出生", "姓", "母语", "家乡", "血型", "从小"):
        if word in text:
            return VOLATILITY_STABLE
    for word in _VOLATILITY_HINTS:
        if word in text:
            return VOLATILITY_CHANGING
    return VOLATILITY_STABLE


def age_days(memory, now=None):
    """距 last_used_at 的天数；缺失则退回 created_at；两者皆缺失按 0 处理。"""
    ref = _parse_ts(memory.get("last_used_at")) or _parse_ts(memory.get("created_at"))
    if ref is None:
        return 0.0
    now = now or datetime.now()
    delta = (now - ref).total_seconds() / 86400.0
    return max(0.0, delta)


def compute_score(memory, now=None):
    """score = importance * emotion * exp(-Δt / τ)。"""
    if not isinstance(memory, dict):
        return 0.0
    try:
        importance = float(memory.get("importance", DEFAULT_IMPORTANCE))
    except (TypeError, ValueError):
        importance = float(DEFAULT_IMPORTANCE)
    try:
        emotion = float(memory.get("emotion", DEFAULT_EMOTION))
    except (TypeError, ValueError):
        emotion = float(DEFAULT_EMOTION)

    base = importance * emotion
    tau = tau_days_for(memory.get("type", _FALLBACK_TYPE), memory.get("volatility"),
                       memory.get("lifespan_days"))
    if tau is None:          # identity + stable：永不衰减
        return base
    dt = age_days(memory, now)
    return base * math.exp(-dt / float(tau))


def tier_of(score):
    if score >= FULL_THRESHOLD:
        return TIER_FULL
    if score >= GIST_THRESHOLD:
        return TIER_GIST
    return TIER_NONE


def relative_time(ts, now=None):
    """把时间戳说成人话，供 gist 档使用。"""
    ref = _parse_ts(ts)
    if ref is None:
        return "时间不详"
    now = now or datetime.now()
    days = (now - ref).total_seconds() / 86400.0
    if days < 1:
        return "今天"
    if days < 2:
        return "昨天"
    if days < 30:
        return f"{int(days)} 天前"
    if days < 365:
        return f"{int(days // 30)} 个月前"
    return f"{int(days // 365)} 年前"


def _plain_text(memory):
    """从三种存储结构里取可读原文。

    口径要与召回时模型实际看到的一致：page 记 user_input + agent_response，
    knowledge 记 knowledge 原文。**不要把 `text` 放第一位** —— page 的 `text`
    是 "User: … Assistant: …" 这种带前缀的拼接，用它算长度会让 gist 的
    "不比原文长"判断失真（M6 实测：33 字的页算出 44 字，gist 白膨胀）。
    """
    ui = (memory.get("user_input") or "").strip()
    ar = (memory.get("agent_response") or "").strip()
    if ui or ar:
        return f"{ui} {ar}".strip()
    if memory.get("knowledge"):
        return str(memory["knowledge"]).strip()
    if memory.get("text"):
        return str(memory["text"]).strip()
    return ""


GIST_SUMMARY_LIMIT = 24   # 一句话摘要的硬上限（字符）


def _strip_role_prefix(text):
    """去掉 "User: xxx Assistant: yyy" 里的角色前缀，gist 不该暴露对话结构。"""
    out = []
    for line in str(text).split("\n"):
        line = line.strip()
        for prefix in ("User:", "Assistant:", "用户:", "助手:"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if line:
            out.append(line)
    return " ".join(out)


def _first_sentence(text, limit=GIST_SUMMARY_LIMIT):
    """截到第一个句末标点；从句末标点之后全部丢掉。

    宁可短也不能是「原文换个壳」：gist 的语义是「想不起细节」。
    """
    text = " ".join(str(text).split())
    if not text:
        return ""
    for sep in ("。", "！", "？", "!", "?", ". "):
        idx = text.find(sep)
        if 0 < idx < limit:
            return text[: idx + 1].strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def make_gist(memory, now=None):
    """「好像有这么回事」档：只给标题 + 一句话，不给细节。

    不使用 LLM：gist 会出现在每次召回里，调模型会把延迟和 token 都拖起来。

    短条目保护（M5 补）：标题「模糊记忆 · type · 相对时间」本身就占 12–15 字，
    对 30–50 字的短记忆会把 gist 撑得比原文还长（实测 49 vs 51 只省 2 字，
    有的还更长）。这里保证 **gist 永远不比原文长**：撑不下就换成更短的标记 +
    首句；连首句也放不下（原文本来就极短）就直接返回原文，宁可不压缩也不膨胀。
    """
    kind = memory.get("type", _FALLBACK_TYPE)
    rel = relative_time(memory.get("last_used_at") or memory.get("created_at"), now)
    raw = _strip_role_prefix(_plain_text(memory))
    head = _first_sentence(raw)
    title = f"模糊记忆 · {kind} · {rel}"
    text = f"【{title}】{head}"
    if len(text) > len(raw):
        # 短条目：完整标题（12–15 字）+ 24 字首句会把 gist 撑得比原文还长。
        # 退化成「短标记 + 裁剪过的首句」，仍以【模糊记忆 开头（档位可辨认），
        # 但保证总长不超过原文。
        mark = f"【模糊记忆·{rel}】"
        budget = len(raw) - len(mark) - 1  # 给结尾的“…”留 1 格
        if budget >= 4:
            body = head[:budget].rstrip() + "…" if len(head) > budget else head
            text = mark + body
        else:
            # 原文极短（比标记还短）：宁可多几个字也要保住正文。
            # 「只知道有这么回事、但一个字都想不起来」对模型毫无价值，
            # 所以这里是唯一允许 gist 略长于原文的情形，膨胀上限 = 标记长度（6 字）。
            text = "【模糊记忆】" + raw
    return {"title": title, "summary": head, "text": text}


# ---------------------------------------------------------------- 写入时打分（LLM）

def _clamp_importance(v):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return DEFAULT_IMPORTANCE
    return max(1, min(5, v))


def _clamp_emotion(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return DEFAULT_EMOTION
    return max(0.0, min(2.0, round(v, 2)))


def _clean_type(v):
    return v if v in TYPE_TAU_DAYS else None


def parse_scoring_payload(raw, expected):
    """把 LLM 返回的 JSON 解析成 [{importance, emotion, type}]，长度对齐 expected。

    任何解析失败都退回默认值：打分是"锦上添花"，不能因为它挂掉写入流程。
    """
    out = [{"importance": DEFAULT_IMPORTANCE,
            "emotion": DEFAULT_EMOTION,
            "type": None,
            "volatility": VOLATILITY_STABLE} for _ in range(expected)]
    if not raw:
        return out

    import json
    import re

    body = raw.strip()
    # 去掉 ```json ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()

    # 截取第一个 [ 到最后一个 ]
    lb, rb = body.find("["), body.rfind("]")
    if lb == -1 or rb == -1 or rb < lb:
        return out
    try:
        data = json.loads(body[lb: rb + 1])
    except (ValueError, TypeError):
        return out
    if not isinstance(data, list):
        return out

    # 优先按 i 字段定位，其次按顺序
    for pos, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        idx = item.get("i")
        try:
            idx = int(idx) - 1 if idx is not None else pos
        except (TypeError, ValueError):
            idx = pos
        if not (0 <= idx < expected):
            continue
        out[idx] = {
            "importance": _clamp_importance(item.get("importance")),
            "emotion": _clamp_emotion(item.get("emotion")),
            "type": _clean_type(item.get("type")),
            "volatility": _clean_volatility(item.get("volatility")),
        }
    return out


def _clean_volatility(value):
    """volatility 只认 stable / changing；缺失或脏值一律按 stable（＝保持原 identity 语义）。"""
    v = str(value or "").strip().lower()
    return v if v in VOLATILITIES else VOLATILITY_STABLE


def score_memories_llm(texts, client, model, batch_size=12):
    """给一批文本打分。分批调用，单批失败只影响该批（退回默认值）。

    返回与 texts 等长的 [{importance, emotion, type}]。
    这是写入路径上唯一的额外 LLM 调用，因此必须批量、可失败、可关闭。
    """
    results = []
    if not texts:
        return results

    try:
        from . import prompts as _prompts
    except ImportError:
        import prompts as _prompts

    # 与模型无关的硬上限：一次最多 12 条，避免 prompt 过长导致 JSON 输出被截断
    batch_size = max(1, min(batch_size, 12))

    for start in range(0, len(texts), batch_size):
        chunk = texts[start: start + batch_size]
        listing = "\n".join(f"{i + 1}. {str(t)[:300]}" for i, t in enumerate(chunk))
        try:
            raw = client.chat_completion(
                model=model,
                messages=[
                    {"role": "system", "content": _prompts.MEMORY_SCORING_SYSTEM_PROMPT},
                    # 注意：模板里有 JSON 示例（{...}），不能用 str.format，否则会被当成格式化字段
                    {"role": "user",
                     "content": _prompts.MEMORY_SCORING_USER_PROMPT.replace("{memories}", listing)},
                ],
                temperature=0.2,
                max_tokens=800,
            )
            results.extend(parse_scoring_payload(raw, len(chunk)))
        except Exception as e:
            print(f"scoring: LLM scoring failed ({e}); falling back to defaults for {len(chunk)} item(s).")
            results.extend([{"importance": DEFAULT_IMPORTANCE,
                             "emotion": DEFAULT_EMOTION,
                             "type": None} for _ in chunk])
    return results
