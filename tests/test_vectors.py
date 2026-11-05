"""Search vectors in memory as the index stores them (16-bit): half the memory of before, the same results."""

import numpy as np

from techrag.library import MultiVectorIndex
from techrag.store import VectorIndex


def _index(n=3000, dim=256, seed=0, offset=0):
    rng = np.random.default_rng(seed)
    m = rng.standard_normal((n, dim), dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    ids = np.arange(offset, offset + n, dtype=np.int64)
    return VectorIndex(ids, m.astype(np.float16), np.array(["d"] * n, dtype=object), ids // 10)


def _reference(ix: VectorIndex, q: np.ndarray, k: int, mask=None):
    """What the search did before: the whole matrix in 32-bit, one product per query."""
    s = ix.matrix.astype(np.float32) @ q.astype(np.float32)
    if mask is not None:
        s = np.where(mask, s, -np.inf)
    top = np.argsort(-s, kind="stable")[:k]
    return [int(ix.ids[i]) for i in top if np.isfinite(s[i])]


def _same(a, b):
    """Same passages in the same order; scores equal up to float rounding (one query or several at a time)."""
    return [i for i, _ in a] == [i for i, _ in b] and np.allclose([s for _, s in a], [s for _, s in b], atol=1e-5)


def test_the_same_results_as_a_32_bit_matrix():
    ix = _index(n=VectorIndex.BLOCK * 3 + 17)          # several blocks and a partial one
    qs = np.random.default_rng(1).standard_normal((7, 256), dtype=np.float32)
    mask = ix.mask(None, list(range(0, 400, 3)))
    for masked in (None, mask):
        many = ix.search_many(qs, 50, masked)
        for q, hits in zip(qs, many):
            assert [i for i, _ in hits] == _reference(ix, q, 50, masked)
            assert _same(hits, ix.search(q, 50, masked))
    assert ix.search_many(np.zeros((2, 256), np.float32), 5, np.zeros(ix.size, bool)) == [[], []]


def test_half_the_memory():
    ix = _index(n=1000, dim=4096)
    assert ix.matrix.dtype == np.float16 and ix.matrix.nbytes == 1000 * 4096 * 2


def test_the_library_index_loads_as_stored(engine):
    store = engine.shared
    ix = store.vectors()
    assert ix.matrix.dtype == np.float16
    with store.connect() as con:
        rows = con.execute("SELECT id, doc_id, embedding FROM chunks ORDER BY id").fetchall()
    assert list(ix.ids) == [r[0] for r in rows] and list(ix.doc_ids) == [r[1] for r in rows]
    assert all(np.array_equal(ix.matrix[i], np.frombuffer(r[2], dtype=np.float16)) for i, r in enumerate(rows))
    q = engine.embedder.embed_queries(["DDR5 refresh tRFC"])[0]
    assert [i for i, _ in ix.search(q, 5)] == _reference(ix, q, 5)
    store.release_vectors()
    assert store._vectors is None and store.vectors().size == ix.size, "loaded again when needed"


def test_shared_and_personal_together():
    a, b = _index(seed=2), _index(seed=3, offset=10_000_000)
    multi = MultiVectorIndex([(a, 0), (b, 0)])
    qs = np.random.default_rng(4).standard_normal((3, 256), dtype=np.float32)
    for q, hits in zip(qs, multi.search_many(qs, 20)):
        merged = sorted(a.search(q, 20) + b.search(q, 20), key=lambda h: -h[1])[:20]
        assert _same(hits, merged) and _same(hits, multi.search(q, 20))


def test_too_little_memory_at_start_is_only_logged(cfg, engine, tmp_path, monkeypatch, caplog):
    """Loading the vectors at start may not fit on a computer with little free memory: it is logged and the first
    question tries again - no app-wide error line, no crash of the warm-up."""
    import logging
    import time

    from fastapi.testclient import TestClient

    from techrag.server import create_app

    monkeypatch.setenv("TECHRAG_SETTINGS", str(tmp_path / "settings.json"))
    calls = []

    def no_memory():
        calls.append(1)
        raise MemoryError("Unable to allocate 809. MiB")

    monkeypatch.setattr(engine.shared, "vectors", no_memory)
    with caplog.at_level(logging.WARNING, logger="techrag.server"):
        with TestClient(create_app(cfg, engine, warmup=True, desktop=True)) as client:
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.02)
            time.sleep(0.1)
            assert calls and client.get("/api/info").json()["error"] is None
    assert any("not loaded at start" in r.getMessage() for r in caplog.records)
