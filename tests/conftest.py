"""테스트 공통: agent_trace는 임시 SQLite에. 작업 디렉터리의 data/trace.db를 건드리지 않는다."""
import pytest

from app import trace


@pytest.fixture(autouse=True)
def _tmp_trace_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_DB", str(tmp_path / "trace.db"))
    trace.reset_store()
    yield
    trace.reset_store()
