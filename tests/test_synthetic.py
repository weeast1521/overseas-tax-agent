"""F08-B 합성 데이터 — 커밋된 파일이 생성기와 일치하고, 전 고객이 계산 가능하며, API가 그 위에서 돈다."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, repo
from app.judge.rule_judge import RuleJudge
from app.llm.base import Completion, ToolCall

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "data" / "synthetic"


def test_committed_data_matches_generator(tmp_path):
    """같은 시드 → 같은 파일. 생성기를 고치면 데이터도 다시 만들어 커밋해야 한다."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_synthetic.py"), "--out", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    for name in ("trades.json", "customers.json", "expected.json", "fx_rate.csv"):
        assert (tmp_path / name).read_bytes() == (SYN / name).read_bytes(), f"{name} 불일치 — python scripts/gen_synthetic.py 로 재생성"


def test_shape_and_coverage():
    customers = json.loads((SYN / "customers.json").read_text(encoding="utf-8"))
    trades = json.loads((SYN / "trades.json").read_text(encoding="utf-8"))
    expected = json.loads((SYN / "expected.json").read_text(encoding="utf-8"))["customers"]
    assert len(customers) == 300 and len(expected) == 300
    assert len({t["broker_code"] for t in trades}) == 10 and len({t["ticker"] for t in trades}) == 15
    scenarios = {c["scenario"] for c in customers}
    assert {"SINGLE_FIFO", "SINGLE_AVG", "MULTI_BROKER", "TRANSFER", "NO_SALE", "LOSS_HEAVY"} <= scenarios
    # 이체 짝이 전부 맞는다
    outs = {t["transfer_id"] for t in trades if t["side"] == "TRANSFER_OUT"}
    ins = {t["transfer_id"] for t in trades if t["side"] == "TRANSFER_IN"}
    assert outs == ins and len(outs) >= 40
    # 실데이터와 겹치지 않는 id 대역
    assert min(c["customer_id"] for c in customers) >= 2001
    ex = list(expected.values())
    assert sum(e["tax_krw"] > 0 for e in ex) >= 60 and sum(e["total_gain_krw"] < 0 for e in ex) >= 60
    assert sum(bool(e["transferred_lot_sales"]) for e in ex) >= 40 and sum(bool(e["needs_confirmation"]) for e in ex) >= 30


@pytest.fixture
def syn_env(monkeypatch):
    monkeypatch.setenv("TRADES_FILE", str(SYN / "trades.json"))


def test_expected_matches_pipeline_figures(syn_env):
    expected = json.loads((SYN / "expected.json").read_text(encoding="utf-8"))["customers"]
    for cid in list(expected)[::10]:  # 30명 표본
        m = repo.resolve_methods(int(cid))
        fig, err = main.compute_figures(int(cid), 2025, m["methods"])
        assert err is None, (cid, err)
        assert fig["tax_krw"] == expected[cid]["tax_krw"] and fig["total_gain_krw"] == expected[cid]["total_gain_krw"]
        assert fig["transferred_lot_sales"] == expected[cid]["transferred_lot_sales"]
        assert m["needs_confirmation"] == expected[cid]["needs_confirmation"]


def test_api_runs_on_synthetic_customers(syn_env, monkeypatch):
    customers = json.loads((SYN / "customers.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setattr(main, "_llm", lambda: _FakeLLM())
    client = TestClient(main.app)
    picked = {}
    for c in customers:
        picked.setdefault(c["scenario"], c)
    for scen, c in picked.items():
        r = client.post("/v1/inquiries", json={"customer_id": c["customer_id"], "tax_year": 2025, "text": "세금 얼마예요"})
        j = r.json()
        assert r.status_code == 200 and j["status"] in ("AUTO", "REVIEW"), (scen, j)
        if scen == "NO_SALE":
            assert j["figures"]["tax_krw"] == 0
        if scen == "TRANSFER":
            assert j["figures"]["transferred_lot_sales"]


class _FakeLLM:
    name = "fake"

    def __init__(self):
        self.script = [
            Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"})),
            Completion(tool_call=ToolCall(id="2", name="tax_calc", args={})),
            Completion(text=json.dumps({"summary": "요약", "figures": {}, "sources": []})),
        ]

    async def complete(self, messages, tools):
        return self.script.pop(0)
