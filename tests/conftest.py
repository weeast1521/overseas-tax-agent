"""테스트 공통: agent_trace·customer_method는 임시 SQLite에. 작업 디렉터리의 data/*.db를 건드리지 않는다."""
import pytest

from app import method_store, trace


@pytest.fixture(autouse=True)
def _tmp_trace_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_DB", str(tmp_path / "trace.db"))
    monkeypatch.setenv("METHOD_DB", str(tmp_path / "method.db"))  # 고객 방식 저장도 테스트마다 빈 DB
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "index"))  # 벡터 색인도 테스트마다 빈 폴더 (없으면 키워드 검색만)
    from app.rag import search, store
    from app.tools import sql_exec
    monkeypatch.delenv("TRADE_DB", raising=False)  # 거래 DB는 TRADES_FILE 로 메모리 생성
    trace.reset_store(); method_store.reset_store(); store.reset_store(); search.reset_searcher(); sql_exec.reset_executor()
    yield
    trace.reset_store(); method_store.reset_store(); store.reset_store(); search.reset_searcher(); sql_exec.reset_executor()
