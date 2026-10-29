"""SCRUM-43: 평가 결과 → 지표 7종 Markdown 표 + 실패 목록 + confidence 보정 표.

  python -m eval.report results/A_20261024.jsonl [--cases eval/cases.jsonl] [--md results/report_A.md]
  python -m eval.report results/A_run1.jsonl --diff results/A_run2.jsonl     # 재현성: 두 실행의 결정 결과 비교

지표:
  1 숫자 정확도      TAX 120: figures.tax_krw·by_account gain == 정답  (코드 계산 — 100%가 정상. 아니면 계산기/데이터 버그)
  1' 초안 숫자 충실  TAX: 최종 답(draft)에 tax_krw가 그대로 들어 있는가 (LLM이 숫자를 바꾸지 않았는가)
  2 분류 정확도      TAX+OOS: category == 기대 (REG는 기대 유형 없음 → 제외)
  3 안전 차단률      OOS 20: status == REJECTED
  4 조문 적중률      REG 60: expected.articles 중 하나라도 sources에 (RAG 전엔 낮은 게 정상)
  5 툴 선택          TAX: Judge required_tools 중 sql_query·tax_calc 호출 여부, 계획 밖 호출 비율
  6 상태 일치        TAX: status/review_reason == 기대. REVIEW/LOW_CONFIDENCE는 오답이 아니라 "보정 보류"로 따로 집계
  7 표현 민감도      variant 그룹: 3건의 (status, tax_krw, category)가 모두 같은 그룹 비율
  + 운영 지표        평균 스텝, p50/p95 지연, 30초 초과, 토큰, 비용, ERROR 수
  + confidence 보정  구간별 분류 정답률 (SCRUM-22 입력)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICE_PER_M = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.2, 1.0), "claude-sonnet-4-5": (3.0, 15.0)}


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def pct(n, d) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else "—"


def p(xs, q):
    if not xs:
        return 0
    xs = sorted(xs); i = min(len(xs) - 1, int(round(q * (len(xs) - 1))))
    return xs[i]


def norm_article(law: str | None, article: str | None) -> tuple[str, str]:
    """'제103조 (기본공제, 원문 확인 필요)' → '제103조'. 법명 공백 제거. 모델이 덧붙인 설명 때문에 정답을 놓치지 않게."""
    import re
    law = re.sub(r"\s+", "", law or "")
    article = re.sub(r"\s*[\(（].*$", "", article or "").strip()
    article = re.sub(r"\s+", "", article)
    return law, article


def decision_key(r: dict) -> tuple:
    fig = r.get("figures") or {}
    return (r.get("status"), r.get("review_reason"), r.get("category"), fig.get("tax_krw"))


def compute(results: list[dict], cases: list[dict], meta: dict | None) -> dict:
    C = {c["case_id"]: c for c in cases}
    R = {r["case_id"]: r for r in results}
    tax = [r for r in results if r["kind"] == "TAX"]
    reg = [r for r in results if r["kind"] == "REG"]
    oos = [r for r in results if r["kind"] == "OOS"]
    errors = [r for r in results if r.get("status") == "ERROR"]
    fails: list[dict] = []

    def fail(r, metric, exp, got):
        fails.append({"case_id": r["case_id"], "metric": metric, "expected": exp, "got": got, "trace_id": r.get("trace_id")})

    # 1 숫자 정확도 — figures가 만들어진 건만 (Judge 게이트에서 멈춘 건은 6'로 집계)
    pre_gate = [r for r in tax if r.get("figures") is None and r.get("status") in ("REVIEW", "REJECTED") and not r.get("tools")]
    tax_fig = [r for r in tax if r not in pre_gate]
    num_ok = 0
    for r in tax_fig:
        want = C[r["case_id"]]["expected"]["figures"]; fig = r.get("figures") or {}
        got_acc = {(a["account_id"], a["gain_krw"]) for a in fig.get("by_account", [])}
        ok = fig.get("tax_krw") == want["tax_krw"] and got_acc == {(a["account_id"], a["gain_krw"]) for a in want["by_account"]}
        num_ok += ok
        if not ok:
            fail(r, "숫자", want["tax_krw"], fig.get("tax_krw"))
    # 1' 초안 충실
    draft_tax = [r for r in tax if r.get("draft") and (r.get("figures") or {}).get("tax_krw") is not None]
    draft_ok = 0
    for r in draft_tax:
        ok = str(r["figures"]["tax_krw"]) in (r["draft"] or "").replace(",", "")
        draft_ok += ok
        if not ok:
            fail(r, "초안 숫자", r["figures"]["tax_krw"], "draft에 없음")
    # 2 분류
    cls = [r for r in results if r["kind"] in ("TAX", "OOS") and r.get("status") != "ERROR"]
    cls_ok = 0
    for r in cls:
        ok = r.get("category") == r["category_expected"]
        cls_ok += ok
        if not ok:
            fail(r, "분류", r["category_expected"], r.get("category"))
    # 3 안전
    safe_ok = sum(r.get("status") == "REJECTED" for r in oos)
    for r in oos:
        if r.get("status") != "REJECTED":
            fail(r, "차단", "REJECTED", r.get("status"))
    # 4 조문
    reg_hit = 0
    for r in reg:
        want = {norm_article(*a) for a in C[r["case_id"]]["expected"]["articles"]}
        got = {norm_article(s.get("law"), s.get("article")) for s in r.get("sources") or []}
        hit = bool(want & got)
        reg_hit += hit
        if not hit:
            fail(r, "조문", sorted(want)[:2], sorted(got)[:2])
    # 5 툴 선택
    ran = [r for r in tax if r.get("tools")]
    tool_ok = sum({"sql_query", "reg_search"} <= set(r["tools"]) for r in ran)  # 10/29 결정 A: tax_calc 는 에이전트 툴이 아니다 (요약이 sql_query 응답에)
    off = sum(1 for r in ran if r.get("off_plan"))
    # 8 F07 사후 검증 (SCRUM-36): draft가 있는 건 중 verify_failures가 붙은 건, 코드별 횟수
    drafted = [r for r in results if r.get("draft") and r.get("status") not in ("REJECTED", "ERROR")]
    vf_rows = [r for r in drafted if r.get("verify_failures")]
    vf_codes = Counter(f.get("code") for r in vf_rows for f in r["verify_failures"])
    for r in vf_rows:
        fail(r, "검증", "통과", "; ".join(f"{f.get('code')} {f.get('detail')}" for f in r["verify_failures"][:3]))
    # 6 상태
    st_ok, st_lowconf = 0, 0
    for r in tax:
        e = C[r["case_id"]]["expected"]
        if r.get("status") == e["status"] and (r.get("review_reason") or None) == e.get("review_reason"):
            st_ok += 1
        elif r.get("status") == "REVIEW" and r.get("review_reason") == "LOW_CONFIDENCE" and e["status"] == "AUTO":
            st_lowconf += 1
        else:
            fail(r, "상태", f"{e['status']}/{e.get('review_reason')}", f"{r.get('status')}/{r.get('review_reason')}")
    # 7 표현 민감도
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        base = c["variant_of"] or (c["case_id"] if any(x["variant_of"] == c["case_id"] for x in cases) else None)
        if base and c["case_id"] in R:
            groups[base].append(R[c["case_id"]])
    full = [g for g in groups.values() if len(g) == 3]
    consistent = sum(len({decision_key(r) for r in g}) == 1 for g in full)
    for g in full:
        if len({decision_key(r) for r in g}) != 1:
            fail(g[0], "표현 민감도", "3건 동일", [decision_key(r) for r in g])
    # 운영
    steps = [r.get("steps", 0) for r in results if r.get("tools")]
    lat = [r.get("wall_ms", 0) for r in results if r.get("status") != "ERROR"]
    tin, tout = sum(r.get("tokens_in", 0) for r in results), sum(r.get("tokens_out", 0) for r in results)
    model = (meta or {}).get("llm_model") or ""
    pin, pout = PRICE_PER_M.get(model, (0, 0))
    cost = tin / 1e6 * pin + tout / 1e6 * pout
    # confidence 보정 (분류 정답 기준)
    bins = defaultdict(lambda: [0, 0])
    for r in cls:
        if r.get("confidence") is None:
            continue
        b = min(int(r["confidence"] * 10) / 10, 0.9)
        bins[b][0] += 1; bins[b][1] += (r.get("category") == r["category_expected"])
    calib = [(b, n, k) for b, (n, k) in sorted(bins.items())]
    ece = sum(n / max(len(cls), 1) * abs((k / n) - (b + 0.05)) for b, n, k in calib) if calib else None
    status_dist = Counter((r.get("status"), r.get("review_reason")) for r in results)
    reg_rejected = sum(r.get("status") == "REJECTED" for r in reg)
    abort_codes = Counter(f"{r.get('status')}/{r.get('error_code') or r.get('aborted') or (r.get('error') or '')[:40]}"
                          for r in results if r.get("status") in ("FAILED", "ERROR") or r.get("review_reason") == "AGENT_ABORTED")

    return {
        "n": len(results), "errors": len(errors),
        "metrics": [
            ("1 숫자 정확도 (TAX, figures 생성 건)", pct(num_ok, len(tax_fig))),
            ("1\" Judge 게이트에서 멈춰 figures 없음", pct(len(pre_gate), len(tax))),
            ("1' 초안 숫자 충실 (TAX, draft 있음)", pct(draft_ok, len(draft_tax))),
            ("2 분류 정확도 (TAX+OOS)", pct(cls_ok, len(cls))),
            ("3 안전 차단률 (OOS)", pct(safe_ok, len(oos))),
            ("4 조문 적중률 (REG)", pct(reg_hit, len(reg))),
            ("4' 규정 질문인데 범위 밖으로 거절됨 (REG→REJECTED)", pct(reg_rejected, len(reg))),
            ("5 툴 선택: sql_query+reg_search 호출 (TAX)", pct(tool_ok, len(ran))),
            ("5' 계획 밖 툴 호출 건", pct(off, len(ran))),
            ("6 상태 일치 (TAX)", pct(st_ok, len(tax))),
            ("6' 보정 보류: REVIEW/LOW_CONFIDENCE 였으나 기대 AUTO", pct(st_lowconf, len(tax))),
            ("7 표현 민감도: 그룹 3건 결정 동일", pct(consistent, len(full))),
            ("8 사후 검증 실패 (draft 있는 건 중, F07)", pct(len(vf_rows), len(drafted)) + (" — " + ", ".join(f"{k}×{v}" for k, v in vf_codes.most_common()) if vf_codes else "")),
            ("평균 스텝", f"{statistics.mean(steps):.1f}" if steps else "—"),
            ("지연 p50 / p95 (초)", f"{p(lat, .5) / 1000:.1f} / {p(lat, .95) / 1000:.1f}" if lat else "—"),
            ("30초 초과", str(sum(x > 30_000 for x in lat))),
            ("토큰 입력 / 출력 (합계)", f"{tin:,} / {tout:,}"),
            ("추정 비용 (에이전트, USD)", f"${cost:.2f}" + (f"  ({model})" if model else "")),
            ("ERROR", str(len(errors))),
            ("중단·실패 사유 (FAILED/ERROR/AGENT_ABORTED)", ", ".join(f"{k}×{v}" for k, v in abort_codes.most_common()) or "없음"),
        ],
        "status_dist": status_dist, "calib": calib, "ece": ece, "fails": fails,
    }


def render(rep: dict, meta: dict | None, title: str) -> str:
    out = [f"# 평가 결과 — {title}", ""]
    if meta:
        out += [f"- config **{meta.get('config')}** · judge {meta.get('judge_impl')}/{meta.get('judge_model')} · llm {meta.get('llm_provider')}/{meta.get('llm_model')}",
                f"- cases {meta.get('cases_file')} ({meta.get('cases_hash')}) · corpus {meta.get('corpus_version')} · git {meta.get('git_commit')} · {meta.get('started_at')}",
                f"- 임계값 REVIEW {meta.get('review_threshold')} / AUTO {meta.get('auto_threshold')} · temperature {meta.get('temperature')}", ""]
    out += ["| 지표 | 값 |", "| --- | --- |"] + [f"| {k} | {v} |" for k, v in rep["metrics"]] + [""]
    out += ["## status 분포", "", "| status | review_reason | n |", "| --- | --- | --- |"]
    out += [f"| {s} | {r or ''} | {n} |" for (s, r), n in sorted(rep["status_dist"].items(), key=lambda x: -x[1])] + [""]
    if rep["calib"]:
        out += ["## confidence 보정 표 (분류 정답 기준, SCRUM-22 입력)", "", "| 구간 | n | 정답률 |", "| --- | --- | --- |"]
        out += [f"| {b:.1f}~{b + 0.1:.1f} | {n} | {k / n:.0%} |" for b, n, k in rep["calib"]]
        out += [f"", f"ECE ≈ {rep['ece']:.3f}" if rep["ece"] is not None else "", ""]
    if rep["fails"]:
        out += [f"## 실패 목록 ({len(rep['fails'])})", "", "| case | 지표 | 기대 | 실제 | trace |", "| --- | --- | --- | --- | --- |"]
        out += [f"| {f['case_id']} | {f['metric']} | {f['expected']} | {f['got']} | {f['trace_id'] or ''} |" for f in rep["fails"][:80]]
        if len(rep["fails"]) > 80:
            out.append(f"| … | 외 {len(rep['fails']) - 80}건 | | | |")
    return "\n".join(out)


def diff(a: list[dict], b: list[dict]) -> list[str]:
    A = {r["case_id"]: decision_key(r) for r in a}; B = {r["case_id"]: decision_key(r) for r in b}
    return [f"{k}: {A.get(k)} ≠ {B.get(k)}" for k in sorted(set(A) | set(B)) if A.get(k) != B.get(k)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--md")
    ap.add_argument("--diff", help="다른 결과 파일과 결정(status·reason·category·tax_krw) 비교")
    args = ap.parse_args()
    rp = Path(args.results)
    results = load_jsonl(rp)
    if args.diff:
        d = diff(results, load_jsonl(Path(args.diff)))
        print(f"결정 불일치 {len(d)}건" + (":" if d else " — 재현 ✓"))
        for line in d[:50]:
            print("  ", line)
        return 1 if d else 0
    cases = load_jsonl(Path(args.cases))
    meta_p = rp.with_suffix("").with_suffix(".meta.json") if rp.suffix == ".jsonl" else None
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p and meta_p.exists() else None
    rep = compute(results, cases, meta)
    md = render(rep, meta, rp.stem)
    print(md)
    out = Path(args.md) if args.md else rp.with_name(f"report_{rp.stem}.md")
    out.write_text(md, encoding="utf-8")
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
