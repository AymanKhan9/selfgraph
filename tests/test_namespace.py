import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from helpers import make_store, new_graph, run, unique_ns

from selfgraph import Schema, SelfGraph, StoreError, SyncSelfGraph, TransientError, ValidationError


def test_namespaces_isolate_reads_and_writes():
    store = make_store()
    a = SelfGraph(store=store, namespace="tenant-a")
    b = SelfGraph(store=store, namespace="tenant-b")
    run(a.initialize())

    run(a.entities.create("Soham", "Person", id="person:soham"))
    assert run(a.entities.get("person:soham")) is not None
    assert run(b.entities.get("person:soham")) is None

    run(b.entities.create("Other", "Person", id="person:soham"))
    assert run(b.entities.get("person:soham")).name == "Other"
    assert run(a.entities.get("person:soham")).name == "Soham"

    a_list = run(a.entities.list())
    assert all(e.name == "Soham" for e in a_list.items)


def test_transaction_rolls_back_on_error():
    g = new_graph()
    run(g.entities.create("A", "T", id="t:a"))
    with pytest.raises(ValidationError):
        run(
            g.insert.facts(
                {
                    "entities": [{"id": "t:b", "type": "T", "name": "B"}],
                    "relations": [{"from_entity": "t:b", "type": "R", "to_entity": "t:ghost"}],
                }
            )
        )
    assert run(g.entities.get("t:b")) is None


def test_calls_after_close_raise_store_error():
    g = new_graph()
    run(g.close())
    run(g.close())
    for call in (g.entities.get("person:x"), g.entities.create("X", "Person"), g.initialize()):
        with pytest.raises(StoreError, match="closed") as err:
            run(call)
        assert not isinstance(err.value, TransientError)


def test_sync_graph_exposes_schema_and_raises_after_close():
    schema = {"entities": [{"name": "Person"}]}
    g = SyncSelfGraph(store=make_store(), schema=schema)
    assert g.schema == Schema.model_validate(schema)
    g.close()
    g.close()
    with pytest.raises(StoreError, match="closed"):
        g.entities.get("person:x")


@pytest.mark.parametrize("inside_loop", [False, True])
def test_sync_graph_serializes_calls_from_threads(inside_loop):
    with SyncSelfGraph(store=make_store(), namespace=unique_ns("sync-threads")) as g:
        entity = g.entities.create("Ann", "Person")
        entered, release, second_started = Event(), Event(), Event()
        original = g.store.get_node

        async def delayed(*args, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait)
            return await original(*args, **kwargs)

        g.store.get_node = delayed

        def get():
            if inside_loop:

                async def nested():
                    return g.entities.get(entity.id)

                return asyncio.run(nested())
            return g.entities.get(entity.id)

        def second_get():
            second_started.set()
            return get()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(get)
            try:
                assert entered.wait(2)
                second = pool.submit(second_get)
                assert second_started.wait(2)
                time.sleep(0.05)
                assert not second.done()
            finally:
                release.set()
            assert first.result().id == entity.id
            assert second.result().id == entity.id


def test_sync_graph_close_waits_for_a_call():
    g = SyncSelfGraph(store=make_store(), namespace=unique_ns("sync-close"))
    g.initialize()
    entity = g.entities.create("Ann", "Person")
    entered, release = Event(), Event()
    original = g.store.get_node

    async def delayed(*args, **kwargs):
        entered.set()
        await asyncio.to_thread(release.wait)
        return await original(*args, **kwargs)

    g.store.get_node = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(g.entities.get, entity.id)
        try:
            assert entered.wait(2)
            closing = pool.submit(g.close)
            time.sleep(0.05)
            assert not closing.done()
        finally:
            release.set()
        assert first.result().id == entity.id
        closing.result()
    with pytest.raises(StoreError, match="closed"):
        g.entities.get(entity.id)
