"""M2 写入时 LLM 打分验证：解析器单测 + 一次真实调用。

解析器部分离线；真实调用需 BAILIAN_URL / BAILIAN_KEY。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import os
import sys

sys.path.insert(0, REPO)
from memoryos import scoring as S

FAIL = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'} {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


print("=== A) 解析器：各种脏输出都要能兜住 ===")
p = S.parse_scoring_payload(
    '[{"i":1,"importance":5,"emotion":1.4,"type":"identity"},'
    ' {"i":2,"importance":2,"emotion":0.8,"type":"episode"}]', 2)
check("正常 JSON", p[0]["importance"] == 5 and p[1]["type"] == "episode", str(p))

p = S.parse_scoring_payload('```json\n[{"i":1,"importance":4,"emotion":1.2,"type":"preference"}]\n```', 1)
check("带 ```json 包裹", p[0]["importance"] == 4, str(p))

p = S.parse_scoring_payload('Sure! Here it is:\n[{"i":1,"importance":3,"emotion":1,"type":"episode"}]\nHope that helps.', 1)
check("前后有废话", p[0]["importance"] == 3, str(p))

p = S.parse_scoring_payload('not json at all', 2)
check("完全非 JSON -> 全默认", all(x["importance"] == 3 and x["emotion"] == 1.0 for x in p), str(p))

p = S.parse_scoring_payload('', 1)
check("空串 -> 默认", p[0]["importance"] == 3)

p = S.parse_scoring_payload('[{"i":1,"importance":99,"emotion":9.9,"type":"垃圾"}]', 1)
check("越界 importance 夹到 5", p[0]["importance"] == 5, str(p[0]))
check("越界 emotion 夹到 2.0", p[0]["emotion"] == 2.0, str(p[0]))
check("非法 type -> None", p[0]["type"] is None, str(p[0]))

p = S.parse_scoring_payload('[{"i":1,"importance":2,"emotion":1,"type":"episode"}]', 3)
check("条目数少于 expected -> 补齐默认", len(p) == 3 and p[1]["importance"] == 3, str(p))

p = S.parse_scoring_payload('[{"i":9,"importance":5}, {"i":1,"importance":2}]', 2)
check("乱序 i 字段能正确归位", p[0]["importance"] == 2, str(p))

print()
print("=== B) 真实调用（百炼 qwen-plus）===")
url = os.environ.get("BAILIAN_URL")
key = os.environ.get("BAILIAN_KEY")
if not url or not key:
    print("SKIP: 未设置 BAILIAN_URL / BAILIAN_KEY")
else:
    from memoryos.utils import OpenAIClient
    client = OpenAIClient(api_key=key, base_url=url)
    texts = [
        "User: 我10岁考过了某乐器十级，是业余考级最高级。 Assistant: 记下了。",
        "User: 今天天气不错。 Assistant: 嗯，天气好。",
        "User: 我中考成绩全市前3%，在某地读高中阶段某班（重点班型）。 Assistant: 记下了。",
        "User: 帮我算一下1加1等于几。 Assistant: 等于2。",
        "User: 我的一项动手类爱好炸了十几次，电机是0802的。 Assistant: 记下了。",
    ]
    got = S.score_memories_llm(texts, client, "qwen-plus")
    print(f"  返回 {len(got)} 条：")
    for t, g in zip(texts, got):
        print(f"    imp={g['importance']} emo={g['emotion']} type={str(g['type']):<10} | {t[:38]}")
    check("条数对齐", len(got) == len(texts), str(len(got)))
    check("值域合法", all(1 <= g["importance"] <= 5 and 0 <= g["emotion"] <= 2 for g in got))
    # 语义合理性：闲聊应低于身份类信息
    chat = got[1]["importance"]
    identity = got[2]["importance"]
    check("闲聊 importance <= 身份类 importance", chat <= identity, f"{chat} vs {identity}")
    check("至少有一条不是全默认", any(g["importance"] != 3 or g["type"] not in (None, "knowledge") for g in got))

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)