"""
index.py — Ultra-Fast Local Index (SQLite FTS5)
~/mission-control/index.py

Millisecond full-text search over ALL knowledge (facts + vault mirror),
running entirely on the Pi. Zero API calls, zero tokens, zero cost.

Why FTS5 and not embeddings (for now):
  - FTS5 on a Pi: <5ms per query, no model needed, no API, works offline
  - Embeddings: better semantic matching, but needs either an API call per
    query (tokens/latency) or a local model (RAM the Pi doesn't have to spare)
  - Upgrade path is built in: the `search()` interface stays identical, so
    swapping FTS5 → embeddings later is a drop-in change (see bottom).

Token economics — the whole point:
  OLD: dump last-10 facts into every prompt   → ~800 tokens, mostly irrelevant
  NEW: retrieve top-5 RELEVANT snippets       → ~350 tokens, all useful
  Result: smarter answers AND fewer tokens per call. Knowledge can grow to
  thousands of facts without the prompt growing at all.
"""
import os
import re
import json
import sqlite3
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("INDEX_DB", os.path.expanduser("~/mission-control/index.db"))
VAULT_MIRROR = os.path.expanduser(os.environ.get("VAULT_MIRROR_PATH", "~/vault-mirror"))


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5(
            doc_id UNINDEXED,     -- fact uuid or vault file path
            kind UNINDEXED,       -- 'fact' | 'vault'
            category UNINDEXED,
            content,              -- the searchable text
            updated UNINDEXED
        )
    """)
    return con


# ── Write ─────────────────────────────────────────────────────────────────────
def index_fact(fact_id: str, content: str, category: str = "general"):
    con = _conn()
    con.execute("DELETE FROM knowledge WHERE doc_id = ?", (fact_id,))
    con.execute(
        "INSERT INTO knowledge (doc_id, kind, category, content, updated) VALUES (?,?,?,?,?)",
        (fact_id, "fact", category, content, datetime.now(timezone.utc).isoformat()),
    )
    con.commit(); con.close()


def index_vault(max_chars_per_file: int = 4000) -> int:
    """
    (Re)index the vault mirror markdown files. Run after each git pull.
    Chunks long files by heading so retrieval returns tight snippets.
    """
    root = Path(VAULT_MIRROR)
    if not root.exists():
        logger.warning(f"Vault mirror not found at {root} — skipping vault index")
        return 0

    con = _conn()
    con.execute("DELETE FROM knowledge WHERE kind = 'vault'")
    count = 0
    for md in root.rglob("*.md"):
        rel = str(md.relative_to(root))
        if rel.startswith((".git", "40-archive", ".obsidian", ".trash")):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")[:60000]
        except Exception:
            continue
        # chunk by ## headings for tight retrieval
        chunks = re.split(r"\n(?=## )", text)
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip()[:max_chars_per_file]
            if len(chunk) < 40:
                continue
            con.execute(
                "INSERT INTO knowledge (doc_id, kind, category, content, updated) VALUES (?,?,?,?,?)",
                (f"{rel}#{i}", "vault", "vault", chunk,
                 datetime.now(timezone.utc).isoformat()),
            )
            count += 1
    con.commit(); con.close()
    logger.info(f"Indexed {count} vault chunks")
    return count


def index_all_redis_facts() -> int:
    """
    Pull ALL facts from shared Redis memory into the index — including facts
    written by OTHER agents (Telegram bot, Claude, future agents) that never
    passed through this app's store_fact(). This closes the loop: anything
    any agent learns becomes retrievable by Janet within the hour.
    """
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url:
        return 0
    try:
        import redis as redis_lib
        r = redis_lib.from_url(url, password=token)
        ids = r.smembers("memory:pending_sync") | r.smembers("memory:synced")
        count = 0
        for fid in ids:
            fid = fid.decode() if isinstance(fid, bytes) else fid
            raw = r.get(f"memory:facts:{fid}")
            if raw:
                f = json.loads(raw)
                index_fact(fid, f["content"], f.get("category", "general"))
                count += 1
        logger.info(f"Indexed {count} Redis facts (all agents)")
        return count
    except Exception as e:
        logger.warning(f"Redis fact indexing failed: {e}")
        return 0


def refresh_vault_and_reindex():
    """git pull the mirror, reindex vault AND all Redis facts. Hourly timer."""
    if Path(VAULT_MIRROR).exists():
        subprocess.run(["git", "-C", VAULT_MIRROR, "pull", "--ff-only"],
                       capture_output=True, timeout=60)
    n_vault = index_vault()
    n_facts = index_all_redis_facts()
    return n_vault + n_facts


# ── Read ──────────────────────────────────────────────────────────────────────
def _fts_escape(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query of terms."""
    terms = re.findall(r"\w{3,}", query.lower())[:8]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


def search(query: str, k: int = 5, kind: str | None = None) -> list[dict]:
    """
    Millisecond retrieval. Returns [{doc_id, kind, category, content, score}].
    score is normalized 0..1 (rank-based approximation).
    """
    con = _conn()
    sql = "SELECT doc_id, kind, category, content, rank FROM knowledge WHERE knowledge MATCH ?"
    params: list = [_fts_escape(query)]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    params.append(k)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    results = []
    for i, (doc_id, knd, cat, content, _rank) in enumerate(rows):
        results.append({
            "doc_id": doc_id, "kind": knd, "category": cat,
            "content": content[:600],
            "score": round(1.0 - i * (0.7 / max(k, 1)), 2),  # rank → rough score
        })
    return results


def stats() -> dict:
    con = _conn()
    facts = con.execute("SELECT COUNT(*) FROM knowledge WHERE kind='fact'").fetchone()[0]
    vault = con.execute("SELECT COUNT(*) FROM knowledge WHERE kind='vault'").fetchone()[0]
    con.close()
    size_mb = round(os.path.getsize(DB_PATH) / 1e6, 2) if os.path.exists(DB_PATH) else 0
    return {"facts_indexed": facts, "vault_chunks": vault, "db_mb": size_mb}


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reindex":
        n = refresh_vault_and_reindex()
        print(f"✅ Reindexed vault: {n} chunks")
    elif len(sys.argv) > 2 and sys.argv[1] == "search":
        for r in search(" ".join(sys.argv[2:])):
            print(f"[{r['score']}] ({r['kind']}/{r['category']}) {r['doc_id']}")
            print(f"    {r['content'][:120]}...")
    else:
        print(json.dumps(stats(), indent=2))
        print("\nUsage: python index.py reindex | search <query>")

# ── Upgrade path (future-proofing, documented) ───────────────────────────────
#
# 1. EMBEDDINGS: NVIDIA NIM offers free embedding models (e.g. nv-embedqa).
#    To upgrade: add an `embedding BLOB` column, compute on index_fact(),
#    and make search() do cosine similarity + FTS5 hybrid. The public
#    interface (search(query, k) -> list[dict]) does not change, so
#    app.py and learn.py need ZERO modifications.
#
# 2. NEW MODELS: all model names live in one place (app.py MODEL_* consts).
#    Swapping Nemotron 70B → whatever ships next = one line.
#
# 3. NEW AGENTS: any agent that writes facts via memory_client.remember()
#    automatically feeds this index (app.py indexes on store). No schema
#    migration ever needed — FTS5 table is append/replace only.
