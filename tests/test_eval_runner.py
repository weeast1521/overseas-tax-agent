"""SCRUM-43 러너·리포트 — 스텁으로 2회 실행 결과 동일, 리포트 지표 산출."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))


def run(tag, out_dir):
    r = subprocess.run([sys.executable, "-m", "eval.run", "--config", "stub", "--limit", "12", "--tag", tag, "--out-dir", str(out_dir)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return [json.loads(l) for l in (out_dir / f"stub_{tag}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def test_stub_runs_are_identical_and_report_computes(tmp_path):
    from report import compute, decision_key, diff, load_jsonl
    a, b = run("r1", tmp_path), run("r2", tmp_path)
    assert len(a) == len(b) == 12
    assert diff(a, b) == []
    assert all(r["trace_id"] and r["tools"] for r in a if r["kind"] == "TAX")
    meta = json.loads((tmp_path / "stub_r1.meta.json").read_text(encoding="utf-8"))
    assert meta["config"] == "stub" and meta["corpus_version"] and meta["cases_hash"] and meta["n_cases"] == 12
    cases = load_jsonl(ROOT / "eval" / "cases.jsonl")
    rep = compute(a, cases, meta)
    names = [k for k, _ in rep["metrics"]]
    assert any("숫자 정확도" in k for k in names) and any("표현 민감도" in k for k in names) and any("조문" in k for k in names)
    assert rep["metrics"][0][1].startswith(f"{sum(1 for r in a if r['kind']=='TAX' and r['figures'])}/")
    assert decision_key(a[0])[0] in ("AUTO", "REVIEW", "REJECTED")
