"""P2 标注（一次性写死，写完之后才允许 --freeze 再跑评估）。

标注口径（先定规则，再逐条填，不许看完结果回头改）：
- mechanism：由 P1 的机制表决定 —— episode/knowledge=decay；preference=supersede；
  identity+changing=supersede；identity+stable=freeze。这里只做人肉复核，不另立标准。
- D（只对 decay 类）：**这条信息在多长时间之后不再值得被检索出来**（不是"人还记不记得"）。
  分档：
    * 一次性的琐事/情绪场景       → 7–21
    * 一段时间内的处境/当天的训练 → 21–45
    * 具体方法/单个事实           → 60–300
    * 通用操作知识/长期行情       → 300–500
- lifespan_days（只对 supersede 类）：保质期，到期只打 stale 标记、不降档。
    * 一学期内的身份状态 → 90–180；使用习惯/长期取舍 → 180–365。
- why：一句话，说明为什么这个 D / 这个保质期。三个月后的人靠它判断标注是否还成立。

注意：这张表是在**没有跑过任何 τ 结果**的情况下填的。填完 → --check → --emit → --freeze → 才跑评估。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))


import json
import os

DATA = "data/data_p2_real/users/user_m0/mid_term.json"
SHEET_IN = "data/tier_eval_p2/sheet_p2.tsv"
SHEET_OUT = "data/tier_eval_p2/sheet_p2_filled.tsv"

# p2real_id -> (D, lifespan_days, why)
LABELS = {
    # episode：该随时间糊
    "p2real_047": ("14", "", "一时新鲜的见闻，两周后只值得记得'有这么件事'"),
    "p2real_049": ("10", "", "当天的害羞瞬间，一周多就只剩模糊印象"),
    "p2real_063": ("30", "", "被同学嫌弃是这段时间的处境，一个月内仍影响我怎么练"),
    "p2real_064": ("45", "", "技术毛病在改掉之前一直相关，改掉后就没价值了"),
    "p2real_066": ("21", "", "一次尴尬的饭桌对话，三周后不值得被翻出来"),
    "p2real_107": ("30", "", "险些伤人的事故，一个月内要一直提醒自己限速"),
    "p2real_113": ("14", "", "当天训练情况（挂树、偏移），下次调机就没用了"),
    "p2real_114": ("7", "", "一次性的换桨计划，做完即失效"),
    "p2real_120": ("21", "", "换件两小时+电机异响，等售后处理完就没价值"),
    "p2real_146": ("14", "", "一时的疲惫和比较心，两周后不该再被翻出来"),
    "p2real_147": ("7", "", "那一周的作业量，过了这周就无关"),
    "p2real_039": ("45", "", "第一次月考的作文题，本学期的写作任务仍在用它"),
    # knowledge：比 episode 慢，但也该糊
    "p2real_036": ("300", "", "可复用的解题方法，高考范围内长期有用"),
    "p2real_037": ("120", "", "自编题型的思路，同一轮复习内有用"),
    "p2real_087": ("400", "", "ELRS 2.4G 通用性，属于长期有效的器材事实"),
    "p2real_089": ("200", "", "1100 档小白机的定位，选件/加购时仍要参考"),
    "p2real_108": ("500", "", "油门中点=悬停，是基础操作知识，基本不该忘"),
    "p2real_115": ("100", "", "备 0802 电机与头发丝卡机，是这轮维修的具体经验"),
    "p2real_119": ("250", "", "25000kv 是否通用，得机型换代才失效"),
    "p2real_121": ("400", "", "质保/换新政策，一年内一直可能用到"),
    "p2real_125": ("200", "", "0802 电机价格区间，做维修预算时用"),
    "p2real_151": ("200", "", "换掉 MemoryOS 遗忘算法这条路，是这个项目的方向认知"),
    "p2real_153": ("60", "", "计划单路径，本轮改造结束就没用了"),
    "p2real_155": ("150", "", "按阿里1格式写配置，换模型时还要照做"),
    # preference：supersede，不吃 τ，只给保质期
    "p2real_011": ("", "365", "记忆写入方式，长期有效但会被新决定覆盖"),
    "p2real_029": ("", "365", "NFC 留着开关空调，长期取舍"),
    "p2real_032": ("", "365", "浏览器习惯，长期有效"),
    "p2real_056": ("", "120", "对一项球类运动的兴趣强度会变，半年内需复核"),
    "p2real_085": ("", "365", "模型分工，换模型/换价目就会变"),
    "p2real_123": ("", "365", "摔机自费修的维修策略，长期有效"),
    "p2real_128": ("", "180", "花钱取舍（宁可自费买件），半年内需复核"),
    # identity / changing：supersede
    "p2real_051": ("", "180", "本学期的老师状况与提议进度，学期结束即过期"),
    "p2real_092": ("", "365", "对一项动手类爱好的认知，还会继续变"),
    "p2real_131": ("", "180", "后悔/接受的态度，半年内可能翻转"),
    "p2real_144": ("", "90", "能力进度，一个月内就会被新进度覆盖"),
    "p2real_148": ("", "120", "月考名次，下次考试即过期"),
    # identity / stable：freeze
    "p2real_020": ("", "", "GitHub 账号名，长期身份"),
    "p2real_028": ("", "", "有哪些设备，长期事实"),
    "p2real_054": ("", "", "初中背景与中考名次，已定型"),
    "p2real_132": ("", "", "AI 与一项动手类爱好的熟悉度差，稳定画像"),
    "p2real_149": ("", "", "初中英语最高名次，过去事实"),
    "p2real_103": ("", "", "一件校园小事，已发生的固定事实"),
}


def main():
    pages = []
    for _sid, sess in json.load(open(DATA, encoding="utf-8"))["sessions"].items():
        pages.extend(sess.get("details") or [])

    lines = open(SHEET_IN, encoding="utf-8").read().rstrip("\n").split("\n")
    head, rows = lines[0], lines[1:]
    out = [head]
    used = set()
    for ln in rows:
        cols = ln.split("\t")
        pid = pages[len(used)]["id"] if False else None
        # sheet 的 key 是 p001..，顺序与 data 里的页顺序一致
        idx = len(used)
        pid = pages[idx]["id"]
        d, ls, why = LABELS[pid]
        typ = pages[idx].get("type")
        mech = ("decay" if typ in ("episode", "knowledge")
                else "freeze" if (typ == "identity" and pages[idx].get("volatility") == "stable")
                else "supersede")
        # 列序：key type volatility mechanism lifespan_days D why text
        cols[3] = mech
        cols[4] = ls
        cols[5] = d
        cols[6] = why
        out.append("\t".join(cols))
        used.add(pid)

    missing = set(LABELS) - used
    if missing:
        raise SystemExit(f"有页没标：{missing}")
    open(SHEET_OUT, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print(f"写出 {SHEET_OUT}（{len(used)} 行）")


if __name__ == "__main__":
    main()