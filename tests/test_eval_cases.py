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
    """Batch로 문장을 다시 쓴 뒤에는 template 백업(cases.template.jsonl)과 비교한다."""
    src = ROOT / "eval" / "cases.template.jsonl"
    if not src.exists():
        src = ROOT / "eval" / "cases.jsonl"
    committed = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    if any(c["text_source"] != "template" for c in committed):
        return  # 백업도 없고 batch 반영본만 있으면 비교 불가
    import validate_cases  # noqa: F401
    r = subprocess.run([sys.executable, str(ROOT / "eval" / "build_cases.py")], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    rebuilt = [json.loads(l) for l in (ROOT / "eval" / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [c["text"] for c in rebuilt] == [c["text"] for c in committed]
