#!/usr/bin/env bash
# 跑不依赖 LLM 的测试套。需要 data/snapshot（见 data/README.md）。
# 用法：./tests/run_all.sh
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-python3}
fail=0

for f in test_scoring test_tiers test_consolidate test_archive \
         test_m3_idempotent test_m5_no_hard_delete test_m5b_fixes \
         test_m6_identity_volatility test_m6_tau_defaults test_m7_fixes \
         test_disposition; do
  printf '%-32s' "$f"
  out=$("$PY" "tests/$f.py" 2>&1 | tail -1)
  echo "$out"
  case "$out" in
    *"FAILED: none"*|*"0 项失败"*) ;;
    *) fail=$((fail + 1)) ;;
  esac
done

echo
echo "失败的套数：$fail"
echo "LLM 套（需 DASHSCOPE_API_KEY，未计入）：tests/test_m3_llm.py、tests/test_scoring_llm.py"
exit "$fail"