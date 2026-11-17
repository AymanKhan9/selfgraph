import pytest
from helpers import KUZU, fresh_graph, new_graph, run, t_entities

from selfgraph import Extraction, ValidationError, relation_id

PAYLOAD = {
    "entities": [
        {"id": "person:soham", "type": "Person", "name": "Soham"},
        {"id": "company:hydradb", "type": "Company", "name": "HydraDB"},
    ],
    "relations": [
        {
            "id": "relation:employment",
            "from_entity": "person:soham",
            "type": "WORKS_AT",
            "to_entity": "company:hydradb",
            "properties": {"role": "Founder"},
        }
    ],
}


def test_insert_facts_dict():
    g = new_graph()
    res = run(g.insert.facts(PAYLOAD, source_id="meeting-42"))
    assert len(res.entities) == 2
    assert len(res.relations) == 1
    assert res.source_id == "meeting-42"
    assert res.source_version is not None
    rel = res.relations[0]
    assert rel.source_count == 1
    assert [e.source_id for e in run(g.relations.evidence(rel.id)).items] == ["meeting-42"]


def test_insert_is_upsert_idempotent_and_keeps_updated_at():
    g = new_graph()
    run(g.insert.facts(PAYLOAD))
    first = run(g.entities.get("person:soham"))
    run(g.insert.facts(PAYLOAD))
    assert len(run(g.relations.versions("relation:employment"))) == 1
    assert run(g.entities.get("person:soham")).updated_at == first.updated_at
    page = run(g.entities.list())
    assert len([e for e in page.items if e.id in ("person:soham", "company:hydradb")]) == 2


def test_insert_changed_properties_creates_version():
    g = new_graph()
    run(
        g.insert.facts(
            {
                "entities": [{"id": "t:a", "type": "T", "name": "A"}, {"id": "t:b", "type": "T", "name": "B"}],
                "relations": [
                    {"id": "relation:1", "from_entity": "t:a", "type": "R", "to_entity": "t:b", "properties": {"k": 1}}
                ],
            }
        )
    )
    run(
        g.insert.facts(
            {
                "relations": [
                    {"id": "relation:1", "from_entity": "t:a", "type": "R", "to_entity": "t:b", "properties": {"k": 2}}
                ],
            }
        )
    )
    versions = run(g.relations.versions("relation:1"))
    assert [v.revision for v in versions] == [2, 1]
    assert run(g.relations.get("relation:1")).properties == {"k": 2}


def test_nested_property_type_changes_on_reinsert_and_update():
    g = new_graph()

    def facts(value):
        props = {"flags": [{"active": value}]}
        return {
            "entities": [
                {"name": "Ann", "type": "Person", "properties": props},
                {"name": "Acme", "type": "Company"},
            ],
            "relations": [
                {"from_entity": "person:ann", "type": "WORKS_AT", "to_entity": "company:acme", "properties": props}
            ],
            "preferences": [
                {"subject_entity": "person:ann", "key": "employer", "value_entity": "company:acme", "properties": props}
            ],
        }

    initial = run(g.insert.facts(facts(0)))
    entity, relation, pref = initial.entities[0], initial.relations[0], initial.preferences[0]
    records = [(g.entities, entity.id), (g.relations, relation.id), (g.preferences, pref.id)]

    run(g.insert.facts(facts(False)))
    for resource, record_id in records:
        assert run(resource.get(record_id)).properties["flags"][0]["active"] is False

    props = {"flags": [{"active": 0}]}
    run(g.entities.update(entity.id, properties=props))
    run(g.relations.update(relation.id, set_properties=props))
    run(g.preferences.update(pref.id, properties=props))
    for resource, record_id in records:
        value = run(resource.get(record_id)).properties["flags"][0]["active"]
        assert type(value) is int and value == 0


def test_entity_merge_rules():
    g = new_graph()
    run(
        g.insert.facts(
            {
                "entities": [
                    {"id": "person:e", "type": "Person", "name": "E", "aliases": ["x"], "properties": {"a": 1}},
                ]
            }
        )
    )
    run(
        g.insert.facts(
            {
                "entities": [
                    {"id": "person:e", "type": "Person", "name": "E", "aliases": ["y"], "properties": {"a": 2, "b": 3}},
                ]
            }
        )
    )
    e = run(g.entities.get("person:e"))
    assert set(e.aliases) == {"x", "y"}
    assert e.properties == {"a": 2, "b": 3}


def test_insert_facts_object_derives_ids_and_leaves_event_time_unset():
    g = new_graph()
    facts = Extraction.model_validate(
        {
            "entities": [{"type": "T", "name": "A"}, {"type": "T", "name": "B"}],
            "relations": [{"from_entity": "t:a", "type": "R", "to_entity": "t:b"}],
        }
    )
    res = run(g.insert.facts(facts))
    assert [e.id for e in res.entities] == ["t:a", "t:b"]
    assert res.relations[0].event_time is None


def test_nan_property_and_invalid_facts_are_rejected():
    g = new_graph()
    t_entities(g, "a", "b")
    with pytest.raises(ValidationError):
        run(g.insert.facts({"entities": [{"type": "Person"}]}))
    with pytest.raises(ValidationError):
        run(
            g.insert.facts(
                {
                    "relations": [
                        {"from_entity": "t:a", "type": "R", "to_entity": "t:b", "properties": {"score": float("nan")}}
                    ],
                }
            )
        )


def test_none_facts_are_rejected_like_other_non_objects():
    g = new_graph()
    for value in (None, [], 5):
        with pytest.raises(ValidationError, match=r"^invalid facts: expected object$"):
            run(g.insert.facts(value))


def test_relation_endpoints_are_immutable():
    g = new_graph()
    t_entities(g, "a", "b", "c")
    run(g.insert.facts({"relations": [{"id": "relation:1", "from_entity": "t:a", "type": "R", "to_entity": "t:b"}]}))
    with pytest.raises(ValidationError):
        run(
            g.insert.facts({"relations": [{"id": "relation:1", "from_entity": "t:a", "type": "R", "to_entity": "t:c"}]})
        )


def test_missing_relation_endpoints_are_named():
    g = fresh_graph("q4")
    run(g.insert.facts({"entities": [{"type": "T", "name": "A"}]}))
    with pytest.raises(ValidationError, match=r"^relation endpoint not found \(to='t:zz'\);"):
        run(g.insert.facts({"relations": [{"from_entity": "t:a", "type": "R", "to_entity": "t:zz"}]}))
    with pytest.raises(ValidationError, match=r"^relation endpoints not found \(from='t:y', to='t:zz'\);"):
        run(g.insert.facts({"relations": [{"from_entity": "t:y", "type": "R", "to_entity": "t:zz"}]}))
    with pytest.raises(ValidationError, match=r"^relation endpoint not found \(from='t:y'\);"):
        run(g.relations.create("t:y", "R", "t:a"))


def test_insert_facts_event_time_accepts_iso_text():
    g = new_graph()
    res = run(g.insert.facts(PAYLOAD, event_time="2024-03-01T00:00:00Z"))
    assert res.relations[0].event_time.isoformat() == "2024-03-01T00:00:00+00:00"
    with pytest.raises(ValidationError):
        run(g.insert.facts(PAYLOAD, event_time="soon"))
    run(g.close())


@pytest.mark.parametrize("collision", ["stored", "batch", "generated", "generated-other-source"])
def test_conflicting_relation_id_does_not_write_any_facts(collision):
    g = fresh_graph("relation_identity")
    run(g.entities.create("Ada", "Person"))
    run(g.entities.create("Bob", "Person"))
    original = {"id": "relation:team", "from_entity": "person:ada", "type": "KNOWS", "to_entity": "person:bob"}
    conflicting = {**original, "to_entity": "person:cy"}
    if collision.startswith("generated"):
        original["id"] = relation_id("person:ada", "KNOWS", "person:cy")
        del conflicting["id"]
        if collision == "generated-other-source":
            original["from_entity"] = "person:bob"
    stored = collision != "batch"
    if stored:
        run(g.relations.create(original["from_entity"], "KNOWS", "person:bob", id=original["id"]))

    with pytest.raises(ValidationError, match="already exists with different endpoints/type"):
        run(
            g.insert.facts(
                {
                    "entities": [{"type": "Person", "name": "Cy"}],
                    "relations": [conflicting] if stored else [original, conflicting],
                },
                source_id="bad-import",
            )
        )

    assert run(g.entities.get("person:cy")) is None
    assert run(g.store.match_nodes(g.namespace, "SourceVersion")) == []
    relation = run(g.relations.get(original["id"]))
    if stored:
        assert relation.to_entity == "person:bob"
    else:
        assert relation is None


def test_facts_bare_years_and_string_strengths_are_rejected():
    g = new_graph()
    t_entities(g, "a", "b")
    joined = {"from_entity": "t:a", "type": "R", "to_entity": "t:b"}
    likes = {"subject_entity": "t:a", "key": "k", "value_entity": "t:b"}
    cases = [
        ({"relations": [{**joined, "event_time": "2021"}]}, 'relations[0].event_time: invalid ISO 8601 string: "2021"'),
        ({"relations": [{**joined, "event_time": 2021}]}, "relations[0].event_time: invalid input"),
        ({"relations": [{**joined, "event_time": True}]}, "relations[0].event_time: invalid input"),
        (
            {"relations": [{**joined, "evidence": [{"source_id": "s", "confidence": "0.5"}]}]},
            "relations[0].evidence[0].confidence: expected number",
        ),
        ({"preferences": [{**likes, "strength": "0.9"}]}, "preferences[0].strength: expected number"),
        ({"preferences": [{**likes, "strength": False}]}, "preferences[0].strength: expected number"),
        (
            {"preferences": [{**likes, "event_time": "20210304T"}]},
            'preferences[0].event_time: invalid ISO 8601 string: "20210304T"',
        ),
        ({"decisions": [{"title": "t", "event_time": 1700000000}]}, "decisions[0].event_time: invalid input"),
        (
            {"decisions": [{"title": "t", "event_time": "soon", "steps": []}]},
            'decisions[0].event_time: invalid ISO 8601 string: "soon"',
        ),
    ]
    for facts, message in cases:
        with pytest.raises(ValidationError) as exc:
            run(g.insert.facts(facts))
        assert str(exc.value) == f"invalid facts: {message}"
    assert run(g.relations.list()).items == []
    run(g.close())


@pytest.mark.skipif(not KUZU, reason="counts Kuzu statements")
def test_relation_id_preflight_batches_kuzu_reads(monkeypatch):
    g = fresh_graph("relation_id_batches")
    run(g.entities.create("Ada", "Person"))
    original = g.store._run
    batches = []
    single_reads = []

    async def capture(query, params=None, **kwargs):
        if "r.sid IN $ids" in query:
            batches.append(len(params["ids"]))
        if "r.sid = $sid" in query and "RETURN r.doc" in query:
            single_reads.append(query)
        return await original(query, params, **kwargs)

    monkeypatch.setattr(g.store, "_run", capture)
    entities = [{"type": "Person", "name": f"Contact {i}"} for i in range(257)]
    relations = [{"from_entity": "person:ada", "type": "KNOWS", "to_entity": f"person:contact-{i}"} for i in range(257)]
    result = run(g.insert.facts({"entities": entities, "relations": relations}))
    assert len(result.relations) == 257
    assert batches == [256, 1]
    assert single_reads == []
