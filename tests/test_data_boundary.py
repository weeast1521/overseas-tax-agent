"""데이터 경계 자동 검증 (SCRUM-51). 실데이터(data/mine)가 Claude API 로 나가지 않음을 pytest 가 매번 증명한다. 외부 호출 없음.

세 겹 각각을 시험한다: (1) 구성 거부 (2) 송신 전 검사 (3) 단일 출구(소스 훑기).
실데이터 대신 tmp_path 에 '가짜 실데이터'를 만들고 MINE_TRADES_FILE 로 가리킨다 — 테스트는 진짜 data/mine 을 절대 읽지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from app import egress
from app.llm.base import Message
from app.llm.claude_client import ClaudeClient

ROOT = Path(__file__).resolve().parents[1]
FAKE_MINE = [
    {"trade_id": "MIRAE-20250312-000917", "customer_id": 1, "account_id": "MIRAE-55512345", "broker_code": "MIRAE", "ticker": "TSLA",
     "side": "BUY", "qty": "13", "price_usd": "247.35", "trade_date": "2025-03-10", "settle_date": "2025-03-12", "fx_settle": "1451.2"},
    {"trade_id": "NAMUH-20250620-004410", "customer_id": 1, "account_id": "NAMUH-77700321", "broker_code": "NAMUH", "ticker": "NVDA",
     "side": "SELL", "qty": "7.5", "price_usd": "141.9", "trade_date": "2025-06-18", "settle_date": "2025-06-20", "fx_settle": "1375.0"},
]


@pytest.fixture
def fake_mine(tmp_path, monkeypatch):
    p = tmp_path / "mine" / "trades.json"
    p.parent.mkdir()
    p.write_text(json.dumps(FAKE_MINE), encoding="utf-8")
    monkeypatch.setenv("MINE_TRADES_FILE", str(p))
    egress.reset_markers()
    yield p
    egress.reset_markers()


@pytest.fixture
def no_mine(monkeypatch, tmp_path):
    monkeypatch.setenv("MINE_TRADES_FILE", str(tmp_path / "none.json"))
    egress.reset_markers()
    yield
    egress.reset_markers()


class FakeTransport:
    """anthropic 클라이언트 흉내. messages.create 가 호출되면 기록 — 경계가 막으면 여기 도달하면 안 된다."""

    def __init__(self):
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        raise AssertionError("외부로 나가면 안 되는 요청이 전송됨")


def _fake_client(monkeypatch, kind="agent"):
    """ClaudeClient 를 만들되 실제 anthropic 대신 FakeTransport 에 경계 검사를 붙인다."""
    t = FakeTransport()
    monkeypatch.setattr(egress, "async_anthropic", lambda kind="llm", **kw: egress.guard(t, kind, is_async=True))
    return ClaudeClient(model="claude-haiku-5-5"), t


# ---------- (1) 구성 거부 ----------

@pytest.mark.parametrize("path", ["data/mine/trades.json", "/Users/x/Developer/overseas-tax-agent/data/mine/trades.json", "data/mine/2024/t.json"])
def test_real_data_config_cannot_build_any_claude_client(monkeypatch, path):
    monkeypatch.setenv("TRADES_FILE", path)
    with pytest.raises(egress.DataBoundaryError) as e:
        ClaudeClient()
    assert e.value.code == "E-DATA-BOUNDARY"
    from app.judge.claude_judge import ClaudeJudge
    with pytest.raises(egress.DataBoundaryError):
        ClaudeJudge()
    with pytest.raises(egress.DataBoundaryError):
        egress.anthropic("rewrite")


def test_synthetic_config_allowed(monkeypatch):
    monkeypatch.setenv("TRADES_FILE", "data/synthetic/trades.json")
    c, _ = _fake_client(monkeypatch)
    assert c.model == "claude-haiku-5-5"


# ---------- (2) 송신 전 검사 ----------

def test_markers_built_from_mine_file_only_in_memory(fake_mine):
    m = egress.markers()
    assert "MIRAE-55512345" in m["id"] and "NAMUH-20250620-004410" in m["id"]
    assert "TSLA|13|247.35" in m["combo"] and "NVDA|7.5|141.9" in m["combo"]
    # 마커 파일을 어디에도 쓰지 않는다
    assert not list(ROOT.glob("**/markers*.json"))


@pytest.mark.parametrize("text,label", [
    ("고객 계좌 MIRAE-55512345 의 거래를 조회", "식별자"),
    ("거래번호 NAMUH-20250620-004410 확인", "식별자"),
    ("TSLA 13주를 247.35달러에 매수", "조합"),
    ("NVDA sold qty 7.5 at 141.90", "조합"),
    ("주민번호 900101-1234567", "주민"),
    ("계좌 110-123-456789 로 이체", "실계좌"),
])
def test_outbound_with_real_data_trace_is_blocked_before_send(fake_mine, monkeypatch, text, label):
    monkeypatch.setenv("TRADES_FILE", "data/synthetic/trades.json")
    c, t = _fake_client(monkeypatch)
    with pytest.raises(egress.DataBoundaryError) as e:
        asyncio.run(c.complete([Message(role="user", content=text)], []))
    assert t.calls == [] and "차단" in str(e.value)
    assert "55512345" not in str(e.value) and "247.35" not in str(e.value)  # 사유에 값 자체는 안 남는다


def test_outbound_synthetic_passes(fake_mine, monkeypatch):
    monkeypatch.setenv("TRADES_FILE", "data/synthetic/trades.json")
    c, t = _fake_client(monkeypatch)
    syn = json.loads((ROOT / "data" / "synthetic" / "trades.json").read_text())[:30]
    body = "합성 거래: " + json.dumps(syn, ensure_ascii=False)
    with pytest.raises(AssertionError, match="전송됨"):  # 경계는 통과 → FakeTransport 까지 도달
        asyncio.run(c.complete([Message(role="user", content=body)], []))
    assert len(t.calls) == 1


def test_scan_checks_system_and_tools_too(fake_mine):
    assert egress.scan({"system": "계좌 MIRAE-55512345", "messages": []}) and egress.scan({"tools": [{"description": "NAMUH-77700321"}]})
    assert egress.scan({"messages": [{"role": "user", "content": [{"type": "tool_result", "content": "TSLA 13 247.35"}]}]})
    assert egress.scan({"messages": [{"role": "user", "content": "테슬라 거래 알려줘"}]}) == []


def test_no_mine_file_still_blocks_pii_patterns(no_mine):
    assert egress.markers() == {"id": set(), "combo": set()}
    assert egress.scan("주민번호 900101-1234567") == ["주민등록번호 꼴"]
    assert egress.scan("MIRAE-55512345 TSLA 13 247.35") == []  # 마커 없으면 식별자는 모른다 — 그래서 (1) 구성 거부가 먼저 있다


def test_judge_payload_goes_through_boundary(fake_mine, monkeypatch):
    """Judge 는 민원 문장만 보낸다(F02-3). 그 문장에 실계좌가 들어 있으면 Judge 호출도 막힌다."""
    monkeypatch.setenv("TRADES_FILE", "data/synthetic/trades.json")
    from app.judge.claude_judge import ClaudeJudge
    t = FakeTransport()
    j = ClaudeJudge(client=egress.guard(t, "judge", is_async=True))
    with pytest.raises(Exception) as e:
        asyncio.run(j.classify("MIRAE-55512345 계좌 세금 얼마예요"))
    assert isinstance(e.value, egress.DataBoundaryError) or "E-DATA-BOUNDARY" in str(e.value) or isinstance(e.value.__cause__, egress.DataBoundaryError)
    assert t.calls == []


def test_egress_log_records_sent_payloads(fake_mine, monkeypatch):
    monkeypatch.setenv("TRADES_FILE", "data/synthetic/trades.json")
    c, t = _fake_client(monkeypatch)
    n0 = len(egress.log())
    with pytest.raises(AssertionError):
        asyncio.run(c.complete([Message(role="system", content="S"), Message(role="user", content="합성 질문")], []))
    assert len(egress.log()) == n0 + 1 and egress.log()[-1]["kind"] == "agent"


# ---------- (3) 단일 출구 ----------

def test_no_direct_anthropic_client_outside_egress():
    """app/ 아래에서 anthropic 클라이언트를 직접 만드는 코드가 생기면 실패 — 경계를 우회하는 길을 소스 수준에서 막는다."""
    pat = re.compile(r"\b(Async)?Anthropic\(")
    offenders = []
    for f in (ROOT / "app").rglob("*.py"):
        if f.name == "egress.py":
            continue
        if pat.search(f.read_text(encoding="utf-8")):
            offenders.append(str(f.relative_to(ROOT)))
    assert offenders == [], f"egress 를 거치지 않는 클라이언트 생성: {offenders}"


def test_mine_dir_is_gitignored_and_absent_from_repo_tracking():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/mine/" in gi
    import subprocess
    tracked = subprocess.run(["git", "ls-files", "data/mine"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert tracked == "", f"git 에 실데이터가 추적됨: {tracked}"
