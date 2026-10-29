"""구성 B 로컬 모델 후보 비교 (SCRUM-49). 맥에서 Ollama 로 실행 — API 비용 0, 외부 호출 0.

    python scripts/compare_local_models.py                                   # 기본 후보 3개 × eval/local_smoke.txt (13건)
    python scripts/compare_local_models.py --models qwen2.5:7b exaone3.5:7.8b --only eval/local_smoke.txt
    python scripts/compare_local_models.py --report-only                     # 이미 돈 results/B_local_*.jsonl 로 표만

후보마다 `python -m eval.run --config B --tag local_<model>` 을 돌리고(LLM·LocalJudge 둘 다 그 모델), 결과를 표로 묶는다.
표의 열: final 도달 / 분류 정확도 / 숫자 정확도(figures) / 평균 스텝 / 평균·최대 시간 / 30초 초과 / 실패 코드.
README "모델 비교표" 에 그대로 붙인다.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = ["qwen2.5:7b", "llama3.1:8b", "exaone3.5:7.8b"]


def have_model(name: str) -> bool:
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return any(line.split()[0] == name or line.split()[0] == name + ":latest" for line in out.splitlines()[1:] if line.strip())


def warm_up(model: str) -> None:
    """콜드 로딩(수십 초)을 첫 케이스가 떠안지 않게. num_ctx 는 에이전트와 같게."""
    import json as _j
    import urllib.request
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    body = _j.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False,
                     "options": {"num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")), "num_predict": 1}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"{host}/api/chat", data=body, headers={"Content-Type": "application/json"}), timeout=180).read()
    except Exception as e:
        print(f"(워밍업 실패 — 계속: {e})")


def run_model(model: str, only: str, out_dir: str, cases: str, timeout: float | None, suffix: str) -> Path:
    tag = "local_" + model.replace(":", "_").replace("/", "_") + suffix
    env = {**os.environ, "OLLAMA_MODEL": model, "LOCAL_JUDGE_MODEL": model}
    env.pop("ANTHROPIC_API_KEY", None)  # 구성 B 는 키가 있어도 쓰지 않는다 — 아예 빼고 돈다
    warm_up(model)
    cmd = [sys.executable, "-m", "eval.run", "--config", "B", "--tag", tag, "--only", only, "--cases", cases, "--out-dir", out_dir]
    if timeout is not None:
        cmd += ["--timeout", str(timeout)]
    print(f"\n=== {model}: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=ROOT, env=env, check=False)
    return Path(out_dir) / f"B_{tag}.jsonl"


def summarize(path: Path, cases: dict[str, dict]) -> dict:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    n = len(rows)
    final = sum(1 for r in rows if r["status"] in ("AUTO", "REVIEW", "REJECTED") and r.get("error_code") not in ("E-LLM", "E-JUDGE", "E-LLM-FORMAT"))
    cls = [r for r in rows if cases.get(r["case_id"], {}).get("kind") != "REG"]  # REG 는 기대 유형이 없다 (eval.report 와 같은 기준)
    cat_ok = sum(1 for r in cls if r.get("category") == cases[r["case_id"]]["category"])
    with_fig = [r for r in rows if cases.get(r["case_id"], {}).get("expected", {}).get("figures") and r.get("figures")]
    fig_ok = sum(1 for r in with_fig if r["figures"].get("tax_krw") == cases[r["case_id"]]["expected"]["figures"].get("tax_krw"))
    lat = [r.get("latency_ms", 0) / 1000 for r in rows]
    steps = [r.get("steps", 0) for r in rows if r.get("steps")]
    errs = Counter(r.get("error_code") for r in rows if r.get("error_code") and r["error_code"] != "E-METHOD-UNCONFIRMED")  # 방식 미확정은 정상 REVIEW
    return {"n": n, "final": final, "cat_ok": cat_ok, "cls_n": len(cls), "fig_ok": fig_ok, "fig_n": len(with_fig),
            "steps": statistics.mean(steps) if steps else 0, "lat_avg": statistics.mean(lat) if lat else 0, "lat_max": max(lat) if lat else 0,
            "over30": sum(1 for x in lat if x > 30), "errs": dict(errs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--only", default="eval/local_smoke.txt")
    ap.add_argument("--cases", default="eval/cases.jsonl")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--pull", action="store_true", help="없는 모델은 ollama pull (망 열린 준비 단계에서만)")
    ap.add_argument("--timeout", type=float, help="동기 타임아웃 초. 실제 분포를 보려면 120 (태그에 _t<초> 가 붙는다)")
    a = ap.parse_args()
    suffix = f"_t{int(a.timeout)}" if a.timeout else ""
    cases = {json.loads(l)["case_id"]: json.loads(l) for l in (ROOT / a.cases).read_text(encoding="utf-8").splitlines() if l.strip()}

    results: dict[str, Path] = {}
    for m in a.models:
        tag = "local_" + m.replace(":", "_").replace("/", "_") + suffix
        p = Path(a.out_dir) / f"B_{tag}.jsonl"
        if a.report_only:
            if p.exists():
                results[m] = p
            continue
        if not have_model(m):
            if a.pull:
                subprocess.run(["ollama", "pull", m], check=False)
            else:
                print(f"건너뜀 {m}: ollama list 에 없음 (ollama pull {m} 또는 --pull)")
                continue
        results[m] = run_model(m, a.only, a.out_dir, a.cases, a.timeout, suffix)

    print("\n| 모델 | final 도달 | 분류 정확 | 숫자 정확 | 평균 스텝 | 평균/최대 시간 | 30초 초과 | 실패 코드 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for m, p in results.items():
        if not p.exists():
            print(f"| {m} | (결과 없음) | | | | | | |"); continue
        s = summarize(p, cases)
        errs = ", ".join(f"{k}×{v}" for k, v in s["errs"].items()) or "—"
        print(f"| {m} | {s['final']}/{s['n']} | {s['cat_ok']}/{s['cls_n']} | {s['fig_ok']}/{s['fig_n']} | {s['steps']:.1f} | {s['lat_avg']:.1f}s / {s['lat_max']:.1f}s | {s['over30']} | {errs} |")
    # E-LLM 원인 (trace 의 E-LLM 스텝 observation)
    for m, p in results.items():
        db = Path(a.out_dir) / f"trace_{p.stem}.db"
        if not db.exists():
            continue
        import sqlite3
        try:
            rows = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
                "SELECT observation, COUNT(*) FROM agent_step WHERE error_code = 'E-LLM' GROUP BY observation ORDER BY 2 DESC LIMIT 3").fetchall()
        except sqlite3.Error:
            rows = []
        for obs, n in rows:
            print(f"  {m} E-LLM×{n}: {(obs or '')[:160]}")
    print("\n수용 조건 (SCRUM-49): final 6/10 이상, 평균 시간 30초 안. 숫자 정확도는 모델과 무관하게 100% 여야 한다 (코드가 계산).")


if __name__ == "__main__":
    main()
