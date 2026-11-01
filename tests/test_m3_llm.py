"""M3 LLM 两步的真实验证：事实失效 + 写小结。

构造真冲突数据和"今天"的数据，验证：
  1. 冲突能被识别并写 valid_to（不删除）
  2. 同一份数据重复跑，不重复失效（幂等）
  3. 日小结能写入且一天只有一条（幂等）

需要 DASHSCOPE_API_KEY。
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
from datetime import datetime, timedelta

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = "/tmp/"
FAIL = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'} {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def run(extra=()):
    cmd = [sys.executable, os.path.join(REPO, "run_consolidate.py"),
           "--data", WORK] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


# ---------- 构造一份"有冲突 + 有今天条目"的数据 ----------
now = datetime.now()
today = now
last_week = now - timedelta(days=7)

if os.path.exists(WORK):
    shutil.rmtree(WORK)
os.makedirs(os.path.join(WORK, "users", "user_m0"), exist_ok=True)
os.makedirs(os.path.join(WORK, "assistants", "default_assistant_profile"), exist_ok=True)

# 旧事实：住 A 小区
old_residence = {
    "knowledge": "用户家住锦江区 A 小区 3 栋",
    "timestamp": ts(last_week),
    "knowledge_embedding": None,
    "id": "know_old_residence",
    "text": "用户家住锦江区 A 小区 3 栋",
    "type": "identity", "importance": 5, "emotion": 1.0,
    "created_at": ts(last_week), "last_used_at": ts(last_week),
    "used_count": 1, "state": "hot",
    "valid_from": ts(last_week), "valid_to": None, "source": "long_term",
}
# 新事实：搬到 B 小区（真冲突）
new_residence = {
    "knowledge": "用户上个月搬到高新区 B 小区 7 栋",
    "timestamp": ts(today),
    "knowledge_embedding": None,
    "id": "know_new_residence",
    "text": "用户上个月搬到高新区 B 小区 7 栋",
    "type": "identity", "importance": 5, "emotion": 1.0,
    "created_at": ts(today), "last_used_at": ts(today),
    "used_count": 0, "state": "hot",
    "valid_from": ts(today), "valid_to": None, "source": "long_term",
}
# 今天的一条普通记录（供写小结）
today_episode = {
    "page_id": "page_today_1",
    "user_input": "我今天在操场跑了三公里，然后去机房写代码。",
    "agent_response": "记下了，你今天跑步三公里后写代码。",
    "timestamp": ts(today),
    "page_embedding": [0.0] * 384,
    "page_keywords": [],
    "analyzed": True, "preloaded": False,
    "pre_page": None, "next_page": None, "meta_info": None,
    "id": "page_today_1", "text": "User: 我今天在操场跑了三公里，然后去机房写代码。 Assistant: 记下了。",
    "type": "episode", "importance": 3, "emotion": 1.0,
    "created_at": ts(today), "last_used_at": ts(today),
    "used_count": 0, "state": "hot",
    "valid_from": ts(today), "valid_to": None, "source": "mid_term",
}
today_session = {
    "id": "session_today_1",
    "summary": "User ran 3km and wrote code today.",
    "summary_embedding": [0.0] * 384,
    "summary_keywords": [],
    "timestamp": ts(today), "last_visit_time": ts(today),
    "H_segment": 1.0, "L_interaction": 1, "N_visit": 1,
    "R_recency": 1.0, "access_count_lfu": 1,
    "details": [today_episode],
    "text": "User ran 3km and wrote code today.",
    "type": "episode", "importance": 3, "emotion": 1.0,
    "created_at": ts(today), "last_used_at": ts(today),
    "used_count": 1, "state": "hot",
    "valid_from": ts(today), "valid_to": None, "source": "mid_term",
}

json.dump({"sessions": {"session_today_1": today_session}, "access_frequency": {"session_today_1": 1}},
          open(os.path.join(WORK, "users/user_m0/mid_term.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
json.dump({"user_profiles": {}, "knowledge_base": [old_residence, new_residence],
           "assistant_knowledge": []},
          open(os.path.join(WORK, "users/user_m0/long_term_user.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
json.dump([], open(os.path.join(WORK, "users/user_m0/short_term.json"), "w", encoding="utf-8"))
json.dump({"user_profiles": {}, "knowledge_base": [], "assistant_knowledge": []},
          open(os.path.join(WORK, "assistants/default_assistant_profile/long_term_assistant.json"),
               "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 需要真实 embedding 才能算相似度
print("=== 计算真实 embedding（供相似度筛选）===")
sys.path.insert(0, REPO)
from memoryos.utils import get_embedding, normalize_vector

for entry in (old_residence, new_residence):
    vec = get_embedding(entry["text"], model_name="paraphrase-multilingual-MiniLM-L12-v2")
    entry["knowledge_embedding"] = normalize_vector(vec).tolist()
for obj in (today_episode,):
    vec = get_embedding(obj["text"], model_name="paraphrase-multilingual-MiniLM-L12-v2")
    obj["page_embedding"] = normalize_vector(vec).tolist()
vec = get_embedding(today_session["text"], model_name="paraphrase-multilingual-MiniLM-L12-v2")
today_session["summary_embedding"] = normalize_vector(vec).tolist()

json.dump({"user_profiles": {}, "knowledge_base": [old_residence, new_residence],
           "assistant_knowledge": []},
          open(os.path.join(WORK, "users/user_m0/long_term_user.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
json.dump({"sessions": {"session_today_1": today_session}, "access_frequency": {"session_today_1": 1}},
          open(os.path.join(WORK, "users/user_m0/mid_term.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("  embedding 已写入")

print()
print("=== A) 第一次完整运行（含 LLM）===")
r1 = run()
if r1.returncode != 0:
    print(r1.stderr[-1500:])
check("退出码 0", r1.returncode == 0)
try:
    rep1 = json.loads(r1.stdout)
except ValueError:
    print("STDOUT 尾部:", r1.stdout[-500:])
    rep1 = {}
print("  expire:", json.dumps(rep1.get("sections", {}).get("expire", {}), ensure_ascii=False))
print("  summary:", json.dumps(rep1.get("sections", {}).get("summary", {}), ensure_ascii=False))

print()
print("=== B) 检查事实失效结果 ===")
lu = json.load(open(os.path.join(WORK, "users/user_m0/long_term_user.json"), encoding="utf-8"))
kb = lu["knowledge_base"]
old = next((e for e in kb if e.get("id") == "know_old_residence"), None)
new = next((e for e in kb if e.get("id") == "know_new_residence"), None)
check("旧记忆被写 valid_to", old and old.get("valid_to") is not None,
      str(old.get("valid_to") if old else "旧条目消失了！"))
check("旧记忆未被删除（还在列表里）", old is not None)
check("旧记忆文本仍可读", old and "A 小区" in (old.get("text") or ""))
check("新记忆仍有效", new and new.get("valid_to") is None)
if old and old.get("superseded_by"):
    check("记录了被谁取代", old["superseded_by"] == "know_new_residence", old["superseded_by"])

print()
print("=== C) 检查日小结 ===")
summaries = [e for e in kb if (e.get("source") or "").startswith("daily_summary:")]
check("写入了 1 条日小结", len(summaries) == 1, str(len(summaries)))
if summaries:
    s = summaries[0]
    print("  小结内容:", s.get("text"))
    check("类型是 episode", s.get("type") == "episode", str(s.get("type")))
    check("importance 是 3", s.get("importance") == 3, str(s.get("importance")))

print()
print("=== D) 第二次运行（幂等）===")
snap_before = json.dumps(json.load(open(os.path.join(WORK, "users/user_m0/long_term_user.json"),
                                      encoding="utf-8")), sort_keys=True, ensure_ascii=False)
r2 = run()
check("第二次退出码 0", r2.returncode == 0)
rep2 = json.loads(r2.stdout)
snap_after = json.dumps(json.load(open(os.path.join(WORK, "users/user_m0/long_term_user.json"),
                                      encoding="utf-8")), sort_keys=True, ensure_ascii=False)
check("第二次 expired == 0", rep2["sections"].get("expire", {}).get("expired", 0) == 0,
      str(rep2["sections"].get("expire", {}).get("expired")))
check("第二次 summary skipped=already_exists",
      rep2["sections"].get("summary", {}).get("skipped") == "already_exists",
      str(rep2["sections"].get("summary", {}).get("skipped")))
check("长期记忆数据未变", snap_before == snap_after)
kb2 = json.load(open(os.path.join(WORK, "users/user_m0/long_term_user.json"),
                     encoding="utf-8"))["knowledge_base"]
check("日小结仍是 1 条（没有重复写）",
      len([e for e in kb2 if (e.get("source") or "").startswith("daily_summary:")]) == 1)

print()
print("=== E) token 消耗 ===")
print("  第一次:", json.dumps(rep1.get("token_detail", {}), ensure_ascii=False))
print("  第二次:", json.dumps(rep2.get("token_detail", {}), ensure_ascii=False))

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)