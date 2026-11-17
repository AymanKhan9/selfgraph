from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

import pytest
from helpers import KUZU, run

from selfgraph import KuzuStore, SelfGraph, SyncSelfGraph, ValidationError
from selfgraph.stores.kuzu import _DATABASE_OPTIONS, _DATABASES, _LOCKS, _OPEN_STORES, _WRITE_LOCKS

MAX_ONE = {
    "entities": [{"name": "Person"}, {"name": "Company"}],
    "relations": [{"type": "WORKS_AT", "from": {"types": ["Person"], "max_count": 1}}],
}


def a_path() -> str:
    return str(Path(tempfile.mkdtemp()) / "graph.kuzu")


@pytest.mark.skipif(not KUZU, reason="two stores over one embedded database")
def test_two_clients_on_one_database_serialize_their_writes():
    path = a_path()
    first = SelfGraph(store=KuzuStore(path), namespace="two", schema=MAX_ONE, contradiction="supersede")
    second = SelfGraph(store=KuzuStore(path), namespace="two", schema=MAX_ONE, contradiction="supersede")
    run(
        first.insert.facts(
            {
                "entities": [
                    {"type": "Person", "name": "Ann"},
                    {"type": "Company", "name": "Acme"},
                    {"type": "Company", "name": "Globex"},
                ]
            }
        )
    )

    async def both_writes() -> None:
        await asyncio.gather(
            first.relations.create("person:ann", "WORKS_AT", "company:acme"),
            second.relations.create("person:ann", "WORKS_AT", "company:globex"),
        )

    run(both_writes())
    current = [r.to_entity for r in run(first.relations.list()).items]
    assert len(current) == 1, f"max_count 1 was violated: {current}"
    run(first.close())
    run(second.close())


@pytest.mark.skipif(not KUZU, reason="the lock caches are Kuzu's")
def test_the_shared_locks_are_dropped_with_the_database():
    path = a_path()
    store = KuzuStore(path)
    assert store._cache_key == path
    assert path in _LOCKS and path in _WRITE_LOCKS and path in _DATABASE_OPTIONS
    run(store.close())
    assert path not in _LOCKS, "the lock outlived the database"
    assert path not in _WRITE_LOCKS, "the write lock outlived the database"
    assert path not in _DATABASE_OPTIONS, "the open options outlived the database"


@pytest.mark.skipif(not KUZU, reason="the database cache is Kuzu's")
def test_a_second_store_must_pass_the_same_options():
    path = a_path()
    store = KuzuStore(path, compression=True)
    try:
        with pytest.raises(ValidationError, match="read_only False vs True"):
            KuzuStore(path, read_only=True)
        with pytest.raises(ValidationError, match="max_db_size"):
            KuzuStore(path, max_db_size=1024)
        same = KuzuStore(path, read_only=False)
        assert same._db is store._db
        run(same.close())
    finally:
        run(store.close())
    assert path not in _DATABASE_OPTIONS

    path = a_path()
    store = KuzuStore(path, max_num_threads=1)
    try:
        bare = KuzuStore(path)
        assert bare._db is store._db
        run(bare.close())
    finally:
        run(store.close())


@pytest.mark.skipif(not KUZU, reason="the database cache is Kuzu's")
def test_a_failed_connect_rolls_back_the_cache(monkeypatch):
    import kuzu

    def refuse(db):
        raise RuntimeError("connection refused")

    path = a_path()
    store = KuzuStore(path)
    monkeypatch.setattr(kuzu, "Connection", refuse)
    with pytest.raises(RuntimeError, match="connection refused"):
        KuzuStore(path)
    assert _OPEN_STORES[path] == 1
    assert store._db is _DATABASES[path]
    run(store.close())
    assert path not in _DATABASES

    path = a_path()
    with pytest.raises(RuntimeError, match="connection refused"):
        KuzuStore(path)
    monkeypatch.undo()
    for cache in (_DATABASES, _OPEN_STORES, _LOCKS, _WRITE_LOCKS):
        assert path not in cache
    store = KuzuStore(path)
    run(store.close())


@pytest.mark.skipif(not KUZU, reason="anonymous stores each get their own cache key")
def test_two_in_memory_stores_share_neither_data_nor_locks():
    first = KuzuStore(":memory:")
    second = KuzuStore(":memory:")
    assert first._lock is not second._lock
    assert first._write_lock is not second._write_lock
    run(first.initialize())
    run(second.initialize())
    run(first.upsert_node("mem", "Thing", "only-first", {"name": "one"}))
    assert run(second.get_node("mem", "Thing", "only-first")) is None
    first_key = first._cache_key
    second_key = second._cache_key
    assert first_key is not second_key
    run(first.close())
    run(second.close())
    for key in (first_key, second_key):
        assert key not in _LOCKS
        assert key not in _WRITE_LOCKS
        assert key not in _DATABASES
        assert key not in _DATABASE_OPTIONS
        assert key not in _OPEN_STORES


@pytest.mark.skipif(not KUZU, reason="one embedded database per path")
def test_a_second_event_loop_uses_the_path_after_the_first_closes():
    path = a_path()

    async def pair(tag: str) -> int:
        first = SelfGraph(store=KuzuStore(path), namespace="loops")
        second = SelfGraph(store=KuzuStore(path), namespace="loops")
        await first.initialize()
        await second.initialize()
        await asyncio.gather(
            first.insert.facts({"entities": [{"type": "Thing", "name": f"a-{tag}"}]}),
            second.insert.facts({"entities": [{"type": "Thing", "name": f"b-{tag}"}]}),
        )
        total = len((await first.entities.list()).items)
        await first.close()
        await second.close()
        return total

    assert asyncio.run(pair("one")) == 2
    assert asyncio.run(pair("two")) == 4


@pytest.mark.skipif(not KUZU, reason="one embedded database per path")
def test_two_threads_with_their_own_loops_write_once_each():
    path = a_path()
    done: list[str] = []
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            graph = SyncSelfGraph(store=KuzuStore(path), namespace="threads")
            graph.insert.facts({"entities": [{"type": "Thing", "name": name}]})
            done.append(name)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,), daemon=True) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "a thread hung: the path lock is not safe across loops"
    assert not errors, errors
    assert sorted(done) == ["alpha", "beta"]
    tally = SyncSelfGraph(store=KuzuStore(path), namespace="threads")
    assert len(tally.entities.list().items) == 2, "a thread wrote to a database the other one never saw"


@pytest.mark.skipif(not KUZU, reason="one embedded database per path")
def test_a_cancelled_waiter_does_not_unblock_the_waiter_behind_it():
    path = a_path()
    store = KuzuStore(path)
    lock = store._lock
    holding = asyncio.Event()
    released = asyncio.Event()
    entered = asyncio.Event()
    entered_while_holding = asyncio.Event()

    async def hold() -> None:
        async with lock:
            holding.set()
            await asyncio.sleep(0.3)
            released.set()

    async def wait() -> None:
        async with lock:
            pass

    async def wait_and_check() -> None:
        async with lock:
            entered.set()
            if holding.is_set() and not released.is_set():
                entered_while_holding.set()

    async def scenario() -> None:
        holder = asyncio.create_task(hold())
        await holding.wait()
        first = asyncio.create_task(wait())
        await asyncio.sleep(0.05)
        second = asyncio.create_task(wait_and_check())
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not entered.is_set(), "the waiter behind the cancelled one entered while the lock was held"
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not entered_while_holding.is_set(), "the second waiter took the lock before the holder released it"
        await second
        await holder

    run(scenario())
    run(store.close())
