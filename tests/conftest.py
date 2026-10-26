"""테스트 공통: agent_trace·customer_method는 임시 SQLite에. 작업 디렉터리의 data/*.db를 건드리지 않는다."""
import pytest

from app import method_store, trace


@pytest.fixture(autouse=True)
def _tmp_trace_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_DB", str(tmp_path / "trace.db"))
    monkeypatch.setenv("METHOD_DB", str(tmp_path / "method.db"))  # 고객 방식 저장도 테스트마다 빈 DB
    trace.reset_store(); method_store.reset_store()
    yield
    trace.reset_store(); method_store.reset_store()
