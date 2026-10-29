"""SCRUM-45: 구성 A(Claude) vs B(로컬) 비교표 — 같은 케이스 집합으로 여러 결과를 나란히.

  python -m eval.compare results/A_t2sql6.jsonl results/B_local_qwen2.5_7b_t120.jsonl [results/B_colab_….jsonl …] [--md results/compare_AB.md]
  python -m eval.compare … --scores results/judge_scores_A_asm_haiku.jsonl results/judge_scores_B_….jsonl   # 문장 점수 열 추가

- 케이스 집합은 교집합으로 맞춘다 (B 가 13건만 돌았으면 A 도 그 13건만 집계).
- 열 이름은 meta(config · llm_model · judge) 에서, 없으면 파일 이름.
- 지표는 eval.report.compute 그대로 → 숫자가 보고서와 어긋나지 않는다.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from eval.report import compute, load_jsonl

ROOT = Path(__file__).resolve().parents[1]
ROWS = ["1 숫자 정확도 (TAX, figures 생성 건)", "1' 초안 숫자 충실 (TAX, draft 있음)", "2 분류 정확도 (TAX+OOS)", "3 안전 차단률 (OOS)",
        "5 툴 선택: sql_query+tax_calc 호출 (TAX)", "6 상태 일치 (TAX)", "8 사후 검증 실패 (draft 있는 건 중, F07)",
        "평균 스텝", "지연 p50 / p95 (초)", "30초 초과", "토큰 입력 / 출력 (합계)", "추정 비용 (에이전트, USD)", "중단·실패 사유 (FAILED/ERROR/AGENT_ABORTED)"]


def label(path: Path, meta: dict | None) -> str:
    if not meta:
        return path.stem
    j = meta.get("judge_model") or meta.get("judge_impl")
    return f"{meta.get('config')} · {meta.get('llm_model')} · judge {j}" + (f" · {int(meta.get('sync_timeout_sec', 30))}s" if meta.get("sync_timeout_sec", 30) != 30 else "")


def load_meta(p: Path) -> dict | None:
    m = p.with_name(p.stem + ".meta.json")
    return json.loads(m.read_text(encoding="utf-8")) if m.exists() else None


def scores_of(p: Path) -> dict[str, float]:
    rows = load_jsonl(p)
    out = {}
    for k in ("grounding", "readability", "compliance"):
        xs = [r[k]["score"] for r in rows if isinstance(r.get(k), dict) and "score" in r[k]]  # judge_scores 행: {case_id, grounding:{score,reason}, …}
        if xs:
            out[k] = statistics.mean(xs)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--scores", nargs="*", default=[], help="judge_scores_*.jsonl (results 와 같은 순서)")
    ap.add_argument("--md")
    a = ap.parse_args()
    cases = load_jsonl(Path(a.cases))
    runs = [(Path(p), load_jsonl(Path(p)), load_meta(Path(p))) for p in a.results]
    C = {c["case_id"]: c for c in cases}
    common = set.intersection(*[{r["case_id"] for r in rows if r["case_id"] in C and r.get("kind") == C[r["case_id"]]["kind"]} for _, rows, _ in runs])
    cols, reps = [], []
    for p, rows, meta in runs:
        sub = [r for r in rows if r["case_id"] in common]
        cols.append(label(p, meta)); reps.append(compute(sub, cases, meta))
    lines = [f"# 구성 비교 — 공통 케이스 {len(common)}건", "", "| 지표 | " + " | ".join(cols) + " |", "|" + " --- |" * (len(cols) + 1)]
    for name in ROWS:
        vals = [dict(rep["metrics"]).get(name, "—") for rep in reps]
        lines.append(f"| {name} | " + " | ".join(vals) + " |")
    if a.scores:
        sc = [scores_of(Path(s)) for s in a.scores] + [{}] * (len(cols) - len(a.scores))
        for k, kr in (("grounding", "문장: 근거"), ("readability", "문장: 가독성"), ("compliance", "문장: 금지 표현")):
            lines.append(f"| {kr} (1~5) | " + " | ".join(f"{s[k]:.2f}" if k in s else "—" for s in sc) + " |")
    lines += ["", "해석 규칙: 숫자 정확도는 구성과 무관하게 100% 여야 한다(코드가 계산). 구성 간 차이는 분류·상태 일치·검증 실패·지연·문장 점수에서 난다.",
              "로컬(B) 지연은 하드웨어 수치다 — M2 Pro(Metal) 와 GPU(Colab) 를 따로 적는다."]
    text = "\n".join(lines)
    print(text)
    if a.md:
        Path(a.md).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
