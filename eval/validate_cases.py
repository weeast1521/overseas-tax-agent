"""SCRUM-42: eval/cases.jsonl 검증. 평가 러너(SCRUM-43)를 돌리기 전에 항상 통과해야 한다.

  python eval/validate_cases.py [--cases eval/cases.jsonl]

검사:
  1. 스키마·필수 필드, case_id 유일, variant_of가 실제 case_id를 가리킴
  2. 분포: TAX 120(TRANSFER 40·MULTI 40·TAX 20·FX 10·OFFSET 10) / REG 55 / OOS 25 (범위 결정 a), variant 그룹 ≥ 20개 각 3건
  3. TAX: customer_id가 data/synthetic에 있고, expected.figures가 **현재 tax_calc** 결과와 일치 (계산기가 바뀌면 여기서 걸린다)
  4. REG: expected.articles 비어 있지 않음. 코퍼스(data/corpus)에 없는 조문은 경고로 나열 → SCRUM-31 수집 목록
  5. OOS: expected.status == REJECTED
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPECTED_DIST = {"TRANSFER_BASIS": 40, "MULTI_BROKER": 40, "TAX_BASIS": 20, "FX_BASIS": 10, "OFFSET": 10, "REG": 55, "OUT_OF_SCOPE": 25}
REQUIRED = {"case_id", "variant_of", "kind", "style", "customer_id", "text", "text_source", "category", "expected"}


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def validate(cases: list[dict], trades_file: Path) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    ids = [c["case_id"] for c in cases]
    if len(set(ids)) != len(ids):
        errors.append("case_id 중복")
    idset = set(ids)
    for c in cases:
        missing = REQUIRED - set(c)
        if missing:
            errors.append(f"{c.get('case_id')}: 필드 누락 {sorted(missing)}")
        if c["variant_of"] and c["variant_of"] not in idset:
            errors.append(f"{c['case_id']}: variant_of {c['variant_of']} 없음")
        if not c["text"] or len(c["text"]) < 5:
            errors.append(f"{c['case_id']}: text 비어 있음")

    dist = Counter(c["category"] for c in cases)
    if dist != Counter(EXPECTED_DIST):
        errors.append(f"분포 불일치: {dict(dist)} ≠ {EXPECTED_DIST}")
    groups = Counter(c["variant_of"] for c in cases if c["variant_of"])
    if len(groups) < 20 or any(v != 2 for v in groups.values()):
        errors.append(f"variant 그룹 {len(groups)}개, 크기 {set(groups.values())} (각 그룹 기준 1 + 변형 2여야 함)")
    for base, cnt in groups.items():
        members = [c for c in cases if c["case_id"] == base or c["variant_of"] == base]
        if len({m["customer_id"] for m in members}) != 1 or len({json.dumps(m["expected"], sort_keys=True) for m in members}) != 1:
            errors.append(f"variant 그룹 {base}: 고객 또는 expected가 서로 다름")
        if {m["style"] for m in members} != {"formal", "casual", "typo"}:
            errors.append(f"variant 그룹 {base}: 말투 3종(formal/casual/typo)이 아님")

    # TAX: 현재 계산기와 대조
    os.environ["METHOD_DB"] = ":memory:"  # 평가는 항상 저장된 고객 방식 없이 (시험지 기대값과 같은 조건). 데모용 data/method.db가 섞이지 않게
    os.environ["TRADES_FILE"] = str(trades_file)
    from app import main, repo
    known = {int(r["customer_id"]) for r in json.loads(trades_file.read_text(encoding="utf-8"))}
    cache: dict[int, tuple[dict | None, str | None, list[str]]] = {}
    for c in cases:
        if c["kind"] != "TAX":
            continue
        cid = c["customer_id"]
        if cid not in known:
            errors.append(f"{c['case_id']}: customer_id {cid} 가 {trades_file.name}에 없음")
            continue
        if cid not in cache:
            m = repo.resolve_methods(cid)
            fig, err = main.compute_figures(cid, 2025, m["methods"])
            cache[cid] = (fig, err, m["needs_confirmation"])
        fig, err, needs = cache[cid]
        exp = c["expected"]
        if err or fig is None:
            errors.append(f"{c['case_id']}: tax_calc 실패 {err}")
            continue
        want = exp.get("figures") or {}
        if fig["tax_krw"] != want.get("tax_krw") or fig["total_gain_krw"] != want.get("total_gain_krw"):
            errors.append(f"{c['case_id']}: figures 불일치 tax {fig['tax_krw']}≠{want.get('tax_krw')} / gain {fig['total_gain_krw']}≠{want.get('total_gain_krw')}")
        got_acc = {(a["account_id"], a["gain_krw"]) for a in fig["by_account"]}
        if got_acc != {(a["account_id"], a["gain_krw"]) for a in want.get("by_account", [])}:
            errors.append(f"{c['case_id']}: by_account 불일치")
        exp_status = "REVIEW" if (needs or fig["transferred_lot_sales"]) else "AUTO"
        if exp["status"] != exp_status:
            errors.append(f"{c['case_id']}: expected.status {exp['status']} ≠ 계산 결과 기준 {exp_status}")

    # REG
    corpus_articles = set()
    for ch in repo.load_corpus():
        corpus_articles.add((ch["law"], ch["article"]))
    missing_articles: Counter = Counter()
    for c in cases:
        if c["kind"] == "REG":
            arts = c["expected"].get("articles") or []
            if not arts:
                errors.append(f"{c['case_id']}: REG인데 articles 없음")
            for law, art in arts:
                if (law, art) not in corpus_articles:
                    missing_articles[(law, art)] += 1
        if c["kind"] == "OOS" and c["expected"].get("status") != "REJECTED":
            errors.append(f"{c['case_id']}: OOS인데 status≠REJECTED")
    if missing_articles:
        warnings.append("코퍼스에 아직 없는 조문 (SCRUM-31 수집 목록): " +
                        ", ".join(f"{law} {art}({n})" for (law, art), n in sorted(missing_articles.items(), key=lambda x: -x[1])))
    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--trades", default=str(ROOT / "data" / "synthetic" / "trades.json"))
    args = ap.parse_args()
    cases = load(Path(args.cases))
    errors, warnings = validate(cases, Path(args.trades))
    src = Counter(c["text_source"] for c in cases)
    print(f"{len(cases)}건  text_source={dict(src)}")
    for w in warnings:
        print("경고:", w)
    if errors:
        print(f"오류 {len(errors)}건:")
        for e in errors[:30]:
            print("  -", e)
        return 1
    print("검증 통과 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
