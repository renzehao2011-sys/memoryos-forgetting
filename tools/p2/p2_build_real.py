"""P2 真实数据建集：把 Operit 真实会话里的成对发言，原样搬成 MemoryOS 的 mid_term 页。

纪律：
- 不改写、不摘要：user_input / agent_response 都是真实原文（各截 400 字）。
- 时间戳统一置为 BASE_TS：评估是"假设老化"。若保留真实创建时间，
  期望档位（按老化档算）与实际年龄（按真实时间算）会错位，等于自己改题。
- importance / emotion 用生产默认值（3 / 1.0），与 data_corpus_m6 一致，便于对比。
- type / volatility 属于标注，在这里定下来，之后随标注一起冻结。

选条口径（可复现）：user 发言 >=24 字且含"我/咱/家里" → 156 条候选 → 按 PICKS 人工挑选。
挑的时候不看任何 τ 结果。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))


import json
import os
import sqlite3

DB = "/tmp/_rdb/app_database"
OUT = os.path.join(DATA_ROOT, "p2_real")
BASE_TS = "2026-11-01 00:00:00"
MAXLEN = 400

# cand 序号 -> (type, volatility, 为什么它算记忆 / 为什么是这个类型)
PICKS = {
    # ---- identity / stable：不可让渡、不该被时间改（freeze）
    20: ("identity", "stable", "GitHub 账号名是长期身份，不会因为时间变糊"),
    28: ("identity", "stable", "自己有哪些设备（备用机、某智能设备）属于长期事实"),
    54: ("identity", "stable", "初中数学特长校/尖子班/中考名次，是已发生的长期背景"),
    132: ("identity", "stable", "对 AI 的了解比一项动手类爱好深，是稳定的能力画像"),
    149: ("identity", "stable", "初中英语最高年级二十一，过去事实，不会再变"),
    103: ("identity", "stable", "暑假就把形象印在饭卡上，是固定下来的过去事实"),
    # ---- identity / changing：这学期/这阶段有效，会过期（supersede）
    51: ("identity", "changing", "数学老师是副校长、自己交了提议，学期内有效"),
    92: ("identity", "changing", "对一项动手类爱好的认知从'玩具'改成'要长期积累'，认知还会变"),
    131: ("identity", "changing", "后悔选了最难的社团、但还是决定练，态度会变"),
    144: ("identity", "changing", "当前能力进度（稳在一块瓷砖内、适应 VR），会随练习更新"),
    148: ("identity", "changing", "月考英语年级十三/班级第二，下次考试就变"),
    # ---- preference：会变，靠新事实覆盖（supersede）
    11: ("preference", "stable", "决定以后记忆写入都手动来、不开自动总结，是配置偏好"),
    29: ("preference", "stable", "NFC 要留着偶尔开关空调，是取舍偏好"),
    32: ("preference", "stable", "浏览器基本用 Chrome、偶尔必应，是使用习惯"),
    56: ("preference", "stable", "越来越喜欢一项球类运动、打完做题效率高，是偏好变化"),
    85: ("preference", "stable", "项目开发走阿里、角色扮演走 deepseek，是分工偏好"),
    123: ("preference", "stable", "以后摔机自费修、炸狠了买件付手工费，是维修策略"),
    128: ("preference", "stable", "宁可自费买件也不心惊胆战，是花钱取舍"),
    # ---- episode：具体发生的事，该随时间糊（decay）
    47: ("episode", None, "第一次近距离见到同性情侣，是具体场景"),
    49: ("episode", None, "被熟识女生摸肚子、自己摸她头发，是具体场景"),
    63: ("episode", None, "班里同学嫌我打球菜、不想带我玩，是当时的处境"),
    64: ("episode", None, "想打杀球但每次都打成又高又快，是当时的技术表现"),
    66: ("episode", None, "饭桌上讲结构性矛盾被同学打断，是具体场景"),
    107: ("episode", None, "空中横滚差点撞人、飞出学校被路人捡回，是具体事故"),
    113: ("episode", None, "飞机挂树上、电池重导致偏移，是当天训练情况"),
    114: ("episode", None, "决定换桨、第一次修这么精密的东西，是当时的行动"),
    120: ("episode", None, "换桨与摄像头罩花两小时、电机仍有异响，是当天结果"),
    146: ("episode", None, "比别人多花好几小时才一样稳，是当时的感受"),
    147: ("episode", None, "周末七张卷子四张题单六页练习册，是那一周的负担"),
    39: ("episode", None, "第一次月考作文写《谈青春之美》，是具体一次考试"),
    # ---- knowledge：学到的做法/事实，也会糊，但比 episode 慢（decay）
    36: ("knowledge", None, "区间整数个数的解法思路，是可复用的方法"),
    37: ("knowledge", None, "自己编的切线+双曲线综合题，是可复用的题型"),
    87: ("knowledge", None, "ELRS 2.4G 是相对通用的版本，是可复用事实"),
    89: ("knowledge", None, "1100 多的带保护套小白机定位，是可复用事实"),
    108: ("knowledge", None, "油门放中间才悬停（看说明书学到的），是可复用操作知识"),
    115: ("knowledge", None, "备一个 0802 电机、头发丝卡进电机，是可复用经验"),
    119: ("knowledge", None, "25000kv 电机是否通用，是可复用事实"),
    121: ("knowledge", None, "七天无理由 / 30 天意外换新 / 一年质保，是可复用事实"),
    125: ("knowledge", None, "0802 25000kv 电机不贵，是行情事实"),
    151: ("knowledge", None, "把 MemoryOS 遗忘算法换掉这条路，是可复用方案认知"),
    153: ("knowledge", None, "改造计划单在 /sdcard/Download/，是可复用路径信息"),
    155: ("knowledge", None, "按阿里1格式给多模型写配置，是可复用做法"),
}


def _clean(s, n=MAXLEN):
    return " ".join(str(s or "").split())[:n]


def main():
    con = sqlite3.connect(DB)
    q = ("select m.chatId, m.sender, m.content, m.timestamp, ch.title "
         "from messages m join chats ch on ch.id = m.chatId "
         "order by m.timestamp, m.orderIndex")
    all_rows = con.execute(q).fetchall()

    cand = []   # 与导出候选表同序
    for i, (cid, sender, ct, t, title) in enumerate(all_rows):
        if sender != "user":
            continue
        text = " ".join(str(ct or "").split())
        if len(text) < 24 or not any(k in text for k in ("我", "咱", "家里")):
            continue
        cand.append(i)

    pages = []
    for no, (typ, vol, why) in sorted(PICKS.items()):
        cid, _s, ct, t, title = all_rows[cand[no - 1]]
        rep = ""
        for j in range(cand[no - 1] + 1, len(all_rows)):
            if all_rows[j][0] != cid:
                break
            if all_rows[j][1] == "ai":
                rep = all_rows[j][2]
                break
        pages.append({
            "id": f"p2real_{no:03d}",
            "user_input": _clean(ct),
            "agent_response": _clean(rep),
            "type": typ,
            "volatility": vol or "stable",
            "importance": 3,
            "emotion": 1.0,
            "created_at": BASE_TS,
            "last_used_at": BASE_TS,
            "used_count": 0,
            "state": "hot",
            "valid_from": BASE_TS,
            "valid_to": None,
            "source": f"operit_room_db:{title}",
        })

    sessions = {}
    for p in pages:
        sid = "s_" + p["source"].split(":", 1)[1][:14]
        sessions.setdefault(sid, {"details": []})["details"].append(p)

    os.makedirs(os.path.join(OUT, "users/user_m0"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "assistants/default_assistant_profile"), exist_ok=True)
    json.dump({"sessions": sessions}, open(os.path.join(OUT, "users/user_m0/mid_term.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump({"knowledge_base": []}, open(os.path.join(OUT, "users/user_m0/long_term_user.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump({"assistant_knowledge": []},
              open(os.path.join(OUT, "assistants/default_assistant_profile/long_term_assistant.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump({"short_term": []}, open(os.path.join(OUT, "users/user_m0/short_term.json"), "w"),
              ensure_ascii=False, indent=1)

    print(f"写出 {len(pages)} 页 -> {OUT}")
    miss = [p["id"] for p in pages if len(p["agent_response"]) < 6]
    if miss:
        print("!! 没抓到 ai 回复：", miss)
    heads = [p["user_input"][:40] for p in pages]
    dup = sorted({h for h in heads if heads.count(h) > 1})
    if dup:
        print("!! 前 40 字重复：", dup)
    for p in pages:
        print(f'{p["id"]} {p["type"]:<10}{p["volatility"]:<9}{p["user_input"][:46]}')


if __name__ == "__main__":
    main()