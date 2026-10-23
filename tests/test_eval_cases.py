"""SCRUM-42 평가셋 — 생성기 재현성과 검증기 통과."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))


def test_validator_passes_on_committed_cases():
    from validate_cases import load, validate
    cases = load(ROOT / "eval" / "cases.jsonl")
    errors, _ = validate(cases, ROOT / "data" / "synthetic" / "trades.json")
    assert errors == [], errors[:10]
    assert len(cases) == 200


def test_builder_is_deterministic(tmp_path):
    """생성기는 임시 경로로만 돌린다 — eval/cases.jsonl(Batch 반영본)을 절대 덮어쓰지 않는다.
    비교 대상은 템플릿 원본(cases.template.jsonl)."""
    src = ROOT / "eval" / "cases.template.jsonl"
    assert src.exists(), "cases.template.jsonl 없음 (rewrite merge가 만든다)"
    committed = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = tmp_path / "cases.jsonl"
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "build_cases.py"), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rebuilt = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [c["text"] for c in rebuilt] == [c["text"] for c in committed]


def test_batch_texts_keep_facts():
    """Batch로 다듬은 문장이 증권사·종목 이름을 잃지 않았고, 정답·말투 태그는 원본과 같다."""
    cases = [json.loads(l) for l in (ROOT / "eval" / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    tmpl = {c["case_id"]: c for c in (json.loads(l) for l in (ROOT / "eval" / "cases.template.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())}
    names = ["미래에셋", "키움", "KB증권", "카카오페이증권", "신한투자증권", "삼성증권", "한국투자증권", "대신증권", "토스증권", "나무",
             "애플", "마이크로소프트", "엔비디아", "테슬라", "아마존", "구글", "메타", "AMD", "팔란티어", "코인베이스", "루시드", "리비안", "인텔", "SOXL", "SCHD"]
    for c in cases:
        o = tmpl[c["case_id"]]
        assert c["expected"] == o["expected"] and c["style"] == o["style"] and c["customer_id"] == o["customer_id"]
        if c["text_source"] == "batch":
            for w in names:
                if w in o["text"]:
                    assert w in c["text"], (c["case_id"], w, c["text"])
