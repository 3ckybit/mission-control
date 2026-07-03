import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import pytest


@pytest.fixture
def temp_index(monkeypatch):
    import index
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # index._conn() creates it fresh
    monkeypatch.setattr(index, "DB_PATH", path)
    yield index
    if os.path.exists(path):
        os.unlink(path)


def test_fts_escape_extracts_terms():
    from index import _fts_escape
    result = _fts_escape("What is the Taxi Dramas budget?")
    assert result == '"what" OR "the" OR "taxi" OR "dramas" OR "budget"'


def test_fts_escape_empty_query():
    from index import _fts_escape
    assert _fts_escape("") == '""'


def test_index_fact_then_search_finds_it(temp_index):
    temp_index.index_fact("fact-1", "Alex prefers NVIDIA free models for routine tasks", "preference")
    results = temp_index.search("NVIDIA free models")
    assert len(results) == 1
    assert results[0]["doc_id"] == "fact-1"
    assert results[0]["kind"] == "fact"
    assert results[0]["category"] == "preference"


def test_index_fact_replaces_on_same_id(temp_index):
    temp_index.index_fact("fact-1", "original content about widgets", "general")
    temp_index.index_fact("fact-1", "updated content about gadgets", "general")
    results = temp_index.search("widgets")
    assert len(results) == 0
    results = temp_index.search("gadgets")
    assert len(results) == 1


def test_search_respects_kind_filter(temp_index):
    temp_index.index_fact("fact-1", "shared search term apple", "general")
    results = temp_index.search("apple", kind="vault")
    assert len(results) == 0
    results = temp_index.search("apple", kind="fact")
    assert len(results) == 1


def test_stats_reports_counts(temp_index):
    temp_index.index_fact("fact-1", "first fact content here", "general")
    temp_index.index_fact("fact-2", "second fact content here", "general")
    stats = temp_index.stats()
    assert stats["facts_indexed"] == 2
    assert stats["vault_chunks"] == 0
