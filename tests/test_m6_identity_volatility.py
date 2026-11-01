"""M6 单测：identity 拆 stable / changing（会过期的事实不再永不衰减）。

契约：
1. identity + stable -> 永不衰减（与原来一致）
2. P1 起 identity + changing -> supersede：不随时间衰减，只被 valid_to 替代
3. 非 identity 带 volatility=changing 不受影响（τ 仍由 type 决定）
4. tau_days_for 兼容旧签名
5. guess_volatility 关键词兜底（成就类 stable / 班级城市成绩 changing）
6. parse_scoring_payload 解析 volatility，脏值回落 stable
7. 打分提示词里确实写了 volatility 规则

运行：python3 test_m6_identity_volatility.py
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import datetime
import sys

REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from memoryos import scoring as S
from memoryos import prompts as P

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def mem(text, typ="identity", vol=None, days=0, imp=5, emo=1.3):
    now = datetime.datetime.now()
    ts = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    d = {"id": "x", "text": text, "type": typ, "importance": imp, "emotion": emo,
         "created_at": ts, "last_used_at": ts, "state": "hot"}
    if vol is not None:
        d["volatility"] = vol
    return d


def main():
    print("1. identity + stable / 缺省 -> 永不衰减")
    base = 5 * 1.3
    for days in (0, 365, 3650):
        s = S.compute_score(mem("我某乐器十级", days=days))
        check(f"{days} 天仍为 base({base})", abs(s - base) < 1e-9, str(s))
    s_vol = S.compute_score(mem("我某乐器十级", vol="stable", days=3650))
    check("显式 stable 也不衰减", abs(s_vol - base) < 1e-9, str(s_vol))

    print("2. P1：identity + changing -> supersede，不吃 τ")
    check("tau_days_for(identity)=None", S.tau_days_for("identity") is None)
    # P1：changing identity 改走 supersede（被新事实覆盖才失效），不再有 τ
    check("机制是 supersede", S.mechanism_of("identity", "changing") == S.MECH_SUPERSEDE)
    check("tau_days_for(identity, changing) = None（不再用 τ）",
          S.tau_days_for("identity", "changing") is None)
    for d in (180, 365, 3650):
        s = S.compute_score(mem("我在某班", vol="changing", days=d))
        check(f"changing {d} 天不衰减（仍为 base）", abs(s - base) < 1e-9, str(s))
    check("稳态 identity 机制是 freeze", S.mechanism_of("identity") == S.MECH_FREEZE)

    print("3. 非 identity 不受 volatility 影响")
    e14 = S.compute_score(mem("今天打球", typ="episode", vol="changing", days=14, imp=3, emo=1.0))
    e14b = S.compute_score(mem("今天打球", typ="episode", days=14, imp=3, emo=1.0))
    check("episode 带 volatility 结果不变", abs(e14 - e14b) < 1e-9, f"{e14} vs {e14b}")

    print("4. guess_volatility 关键词兜底")
    cases = [
        ("我10岁考过了某乐器十级，是业余考级最高级。", "stable"),
        ("另一门乐器我也考了十级，只用了三年。", "stable"),
        ("我中考成绩全市前3%，在某地读高中阶段，某班（重点班型）。", "changing"),
        ("我现在住A小区3栋201。", "changing"),
        ("我出生在某地。", "stable"),
    ]
    for text, want in cases:
        got = S.guess_volatility({"type": "identity", "text": text})
        check(f"{want}: {text[:18]}", got == want, got)
    check("非 identity 一律 stable",
          S.guess_volatility({"type": "episode", "text": "某班上课"}) == "stable")

    print("5. parse_scoring_payload 解析 volatility")
    raw = '[{"i":1,"importance":5,"emotion":1.3,"type":"identity","volatility":"changing"},' \
          '{"i":2,"importance":4,"emotion":1.0,"type":"preference","volatility":"garbage"}]'
    out = S.parse_scoring_payload(raw, 2)
    check("第 1 条 changing", out[0]["volatility"] == "changing", str(out[0]))
    check("脏值回落 stable", out[1]["volatility"] == "stable", str(out[1]))
    out_bad = S.parse_scoring_payload("not json", 2)
    check("解析失败全默认 stable", all(o["volatility"] == "stable" for o in out_bad))

    print("6. 提示词已含 volatility 规则")
    p = P.MEMORY_SCORING_USER_PROMPT
    check("写了 changing 定义", "changing" in p and "will expire" in p)
    check("示例输出含 volatility", '"volatility":"stable"' in p.replace(" ", ""))

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())