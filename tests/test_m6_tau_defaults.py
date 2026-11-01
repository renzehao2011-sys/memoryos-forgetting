"""M6 默认参数契约：当前生效的 τ 与阈值必须与标定结论一致。

为什么单独一个文件：test_scoring / test_tiers 为了稳定，把 τ 固定成了标定前的旧值
（它们测机制，不测参数）。参数本身由本文件守。

若以后重新标定，改 schema.TYPE_TAU_DAYS / scoring.CHANGING_IDENTITY_TAU_DAYS，
同时更新这里的期望值和 M6_REPORT.md 的依据。

运行：python3 test_m6_tau_defaults.py
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))

import sys

sys.path.insert(0, REPO)
from memoryos import schema
from memoryos import scoring as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def main():
    t = schema.TYPE_TAU_DAYS
    print("1. 四类 τ 与 M6 标定结论一致")
    check("episode = 21（原 14）", t["episode"] == 21, str(t["episode"]))
    check("knowledge = 90（原 30）", t["knowledge"] == 90, str(t["knowledge"]))
    check("preference = None（P1：走 supersede，不吃 τ）", t["preference"] is None, str(t["preference"]))
    check("identity = None（∞，仅 stable）", t["identity"] is None, str(t["identity"]))
    check("episode < knowledge", t["episode"] < t["knowledge"],
          f'{t["episode"]}/{t["knowledge"]}')

    print("2. P1：supersede / freeze 不吃 τ，decay 类可被 lifespan_days 覆盖")
    check("tau_days_for(preference) = None", S.tau_days_for("preference") is None)
    check("tau_days_for(identity, changing) = None",
          S.tau_days_for("identity", "changing") is None)
    check("tau_days_for(identity, stable) = None",
          S.tau_days_for("identity", "stable") is None)
    check("lifespan_days 覆盖 episode τ",
          S.tau_days_for("episode", None, 7) == 7.0, str(S.tau_days_for("episode", None, 7)))
    check("lifespan_days 不改变 supersede",
          S.tau_days_for("preference", None, 7) is None)

    print("3. 阈值未变（0.30 / 0.12）")
    check("FULL_THRESHOLD = 0.30", S.FULL_THRESHOLD == 0.30, str(S.FULL_THRESHOLD))
    check("GIST_THRESHOLD = 0.12", S.GIST_THRESHOLD == 0.12, str(S.GIST_THRESHOLD))

    print("4. schema 导出的枚举与 scoring 一致")
    check("MEMORY_TYPES 仍是 4 个", len(schema.MEMORY_TYPES) == 4, str(schema.MEMORY_TYPES))
    check("是 volatility 拆的不是新 type",
          "changing_identity" not in schema.MEMORY_TYPES, str(schema.MEMORY_TYPES))

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        print("失败项：" + "，".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())