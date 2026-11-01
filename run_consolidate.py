"""M3 每日整理任务入口（计划单 §7 / §11）。

一次性脚本，不常驻。退出即结束，失败不影响主流程。

用法：
    python3 run_consolidate.py --data /path/to/data [--dry-run] [--steps ...]
    python3 run_consolidate.py --data /path/to/data --list-pending
    python3 run_consolidate.py --data /path/to/data --confirm-archive id1,id2 --operator 用户A

整理步骤（2026-11-11 起，四步）：
    1. 去重合并
    2. 事实失效（需 LLM，可跳过）——这是覆盖机制，仍然有效
    3. 写小结（需 LLM，可跳过）
    4. 输出报告

**`recompute` 已从整理任务里移除。** 打分层（τ / decay / 三档）2026-11-11 经真实数据
判定为负并停用，见 `FORGETTING_BETA.md` 顶部墓碑。每天重算一遍分数既不改行为，
又往审计里灌一堆无意义记录，所以直接不让它作为整理步骤跑；
传 `--steps recompute` 会显式报错退出（不留"静默空跑"这种半死状态）。

幂等：同一份数据跑两次，第二次所有 "变化类" 计数都应为 0。
"""

# --- 导出仓可移植路径（build_export.py 注入）
import os
REPO = os.environ.get("MEMORYOS_REPO", os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.environ.get("MEMORYOS_DATA_ROOT", os.path.join(REPO, "data"))


import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memoryos import scoring as m2_scoring
from memoryos import consolidate as C
from memoryos import consolidate_llm as CL
from memoryos import archive as AR
from memoryos.audit import AuditLog, AUDIT_FILENAME
from memoryos.utils import OpenAIClient

TS_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- token 计量

class TokenMeter:
    """透明包装 client.chat_completion，累计用量（计划单要求整理任务打印 token）。"""

    def __init__(self):
        self.calls = 0
        self.prompt = 0
        self.completion = 0
        self.total = 0

    def wrap(self, client, tag):
        orig = client.chat_completion
        meter = self

        def patched(model, messages, temperature=0.7, max_tokens=2000):
            resp = client.client.chat.completions.create(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens)
            u = getattr(resp, "usage", None)
            meter.calls += 1
            if u:
                meter.prompt += u.prompt_tokens
                meter.completion += u.completion_tokens
                meter.total += u.total_tokens
            from memoryos.utils import clean_reasoning_model_output
            return clean_reasoning_model_output(resp.choices[0].message.content.strip())

        client.chat_completion = patched
        return orig

    def as_dict(self):
        return {"calls": self.calls, "prompt": self.prompt,
                "completion": self.completion, "total": self.total}


# ---------------------------------------------------------------- 主流程

def load_all(data_dir, user_id, assistant_id):
    from memoryos.memoryos import ensure_directory_exists  # noqa: F401  (只为保持路径语义一致)
    u = os.path.join(data_dir, "users", user_id)
    a = os.path.join(data_dir, "assistants", assistant_id)
    return {
        "short": (os.path.join(u, "short_term.json"), C.load_json(os.path.join(u, "short_term.json"))),
        "mid": (os.path.join(u, "mid_term.json"), C.load_json(os.path.join(u, "mid_term.json"))),
        "long_user": (os.path.join(u, "long_term_user.json"),
                      C.load_json(os.path.join(u, "long_term_user.json"))),
        "long_asst": (os.path.join(a, "long_term_assistant.json"),
                      C.load_json(os.path.join(a, "long_term_assistant.json"))),
    }


def save_all(bundle):
    for _, (path, data) in bundle.items():
        C.save_json(path, data)


def _make_summary_entry(text, now, day):
    """构造日小结条目（计划单 §7.4：episode，importance 默认 3）。"""
    import hashlib
    return {
        "knowledge": text,
        "timestamp": now.strftime(TS_FMT),
        "id": "know_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8],
        "text": text,
        "type": "episode",
        "importance": 3,
        "emotion": 1.0,
        "created_at": now.strftime(TS_FMT),
        "last_used_at": now.strftime(TS_FMT),
        "used_count": 0,
        "state": "hot",
        "valid_from": None,
        "valid_to": None,
        "source": CL.summary_source_for(day),
        "knowledge_embedding": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="data 根目录（含 users/ 与 assistants/）")
    ap.add_argument("--user-id", default="user_m0")
    ap.add_argument("--assistant-id", default="default_assistant_profile")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--steps", default="dedupe,expire,summary",
                    help="逗号分隔：dedupe,expire,summary（recompute 已于 2026-11-11 移除）")
    ap.add_argument("--no-llm", action="store_true", help="跳过所有 LLM 步骤")
    ap.add_argument("--list-pending", action="store_true", help="只列待归档，不动数据")
    ap.add_argument("--confirm-archive", default="", help="逗号分隔的 id，人工确认归档")
    ap.add_argument("--operator", default="", help="确认人标识，写进审计日志")
    ap.add_argument("--restore", default="",
                    help="逗号分隔的 id，撤销 valid_to（M7-2 可回退入口；不动 state、不删条目）")
    args = ap.parse_args()

    steps = set(s.strip() for s in args.steps.split(",") if s.strip())
    if "recompute" in steps:
        print("拒绝：recompute 已从整理任务中移除。")
        print("  打分层（τ / decay / 三档）2026-11-11 经真实数据判定为负并停用，"
              "见 FORGETTING_BETA.md 顶部墓碑。")
        print("  每天空跑一遍分数既不改行为，又往审计里灌无意义记录，所以这里不再提供这一步。")
        print("  要复现历史结论：直接调 consolidate.recompute_states，并显式 USE_CUSTOM_SCORING=1。")
        return 2
    data_dir = os.path.abspath(args.data)
    audit = AuditLog(os.path.join(data_dir, AUDIT_FILENAME))
    bundle = load_all(data_dir, args.user_id, args.assistant_id)

    # 统一收集所有条目，附带层级标记
    tiers = []
    for item in C.iter_mid_term_items(bundle["mid"][1]):
        tiers.append(("mid_term", item))
    for item in C.iter_long_term_items(bundle["long_user"][1]):
        tiers.append(("long_term_user", item))
    for item in C.iter_long_term_items(bundle["long_asst"][1]):
        tiers.append(("long_term_assistant", item))

    # ---- 只列待归档
    if args.list_pending:
        pending = AR.list_pending([it for _, it in tiers])
        print(json.dumps(pending, ensure_ascii=False, indent=2))
        return 0

    # ---- 人工确认归档
    if args.confirm_archive:
        ids = [x.strip() for x in args.confirm_archive.split(",") if x.strip()]
        all_items = [it for _, it in tiers]
        stats = AR.confirm_archive(all_items, ids, audit=audit,
                                   operator=args.operator, dry_run=args.dry_run)
        if stats["archived"] and not args.dry_run:
            save_all(bundle)
        print(json.dumps({"confirm_archive": stats}, ensure_ascii=False, indent=2))
        return 0

    # ---- 人工撤销失效（M7-2：可回退）
    if args.restore:
        ids = [x.strip() for x in args.restore.split(",") if x.strip()]
        all_items = [it for _, it in tiers]
        stats = C.restore_expired(all_items, ids, audit=audit,
                                  operator=args.operator, dry_run=args.dry_run)
        if stats["restored"] and not args.dry_run:
            save_all(bundle)
        print(json.dumps({"restore": stats}, ensure_ascii=False, indent=2))
        return 0

    # ---- 正常整理
    import datetime as _dt
    now = _dt.datetime.now()
    report = C.ConsolidationReport()
    meter = TokenMeter()

    client = None
    model = os.environ.get("CONSOLIDATE_LLM_MODEL", "qwen-plus")
    if not args.no_llm and ("expire" in steps or "summary" in steps):
        key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("BAILIAN_KEY")
        url = os.environ.get("BAILIAN_URL",
                             "https://dashscope.aliyuncs.com/compatible-mode/v1")
        if key:
            client = OpenAIClient(api_key=key, base_url=url)
            meter.wrap(client, "consolidate")
        else:
            print("警告：未找到 DASHSCOPE_API_KEY，跳过 LLM 步骤")
            steps -= {"expire", "summary"}

    # 第 1 步：去重合并
    if "dedupe" in steps:
        for tier_name in ("mid_term", "long_term_user", "long_term_assistant"):
            items = [it for t, it in tiers if t == tier_name]
            st = C.dedupe(items, now, audit, level=tier_name, dry_run=args.dry_run)
            report.add(f"dedupe:{tier_name}", st)

    # 第 2 步：事实失效
    if "expire" in steps and client is not None:
        all_items = [it for _, it in tiers]
        pairs = CL.find_conflict_candidates(all_items, all_items)
        conflicts = CL.detect_conflicts(pairs, client, model)
        st = CL.expire_old_facts(conflicts, now, audit, dry_run=args.dry_run)
        st["candidates"] = len(pairs)
        report.add("expire", st)

    # 第 3 步：写小结
    if "summary" in steps and client is not None:
        day = now.strftime("%Y-%m-%d")
        todays = [it for _, it in tiers
                  if (it.get("created_at") or "")[:10] == day]
        entries = list(C.iter_long_term_items(bundle["long_user"][1]))

        if CL.daily_summary_exists(entries, day):
            # 幂等：同一天已写过就不重复写，也不再调 LLM
            st = {"written": 0, "skipped": "already_exists", "today_entries": len(todays)}
        else:
            text = CL.build_daily_summary(todays, client, model)
            st = {"written": 0, "skipped": None, "summary_text": text,
                  "today_entries": len(todays)}
            if text:
                if not args.dry_run:
                    bundle["long_user"][1].setdefault("knowledge_base", []).append(
                        _make_summary_entry(text, now, day))
                    audit.record("daily_summary", CL.summary_source_for(day), None, 3.0,
                                 f"写入 {day} 的日小结")
                st["written"] = 1
            else:
                st["skipped"] = "no_text"
        report.add("summary", st)

    # 第 5 步：报告
    tok = meter.as_dict()
    report.tokens = tok
    report.by_tag = {"consolidate": tok} if tok["calls"] else {}

    if not args.dry_run:
        save_all(bundle)

    out = report.to_dict()
    out["dry_run"] = args.dry_run
    out["pending_archive_total"] = len(AR.list_pending([it for _, it in tiers]))
    # M5a：容量超限不再硬删，改为打 over_capacity 标记。这里把标记数摊到报告里，
    # 否则该标记只有 weekly_spotcheck.py 一个出口。
    _over = [it for _, it in tiers if it.get("over_capacity_since")]
    out["stale_total"] = len([it for _, it in tiers if it.get("stale_since")])
    out["over_capacity_total"] = len(_over)
    out["over_capacity_ids"] = [str(it.get("id")) for it in _over][:20]
    out["audit_lines"] = audit.count()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())