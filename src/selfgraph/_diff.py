from __future__ import annotations

from datetime import datetime
from typing import Any

from . import _ops
from ._serde import L_ENTITY, aware, decode_entity
from .models import GraphDiff, Relation


async def graph_diff(runner: Any, ns: str, since: datetime, until: datetime) -> GraphDiff:
    """Read stored creations, revisions, and retirements in the known-time window (since, until].

    @param runner (Any): the graph runner.
    @param ns (str): the namespace.
    @param since (datetime): exclusive lower bound.
    @param until (datetime): inclusive upper bound.
    @return (GraphDiff): changes in deterministic id and revision order.
    """
    result = GraphDiff(since=since, until=until)
    retired: dict[tuple[str, datetime], Relation] = {}
    skip = 0
    while True:
        rows = await runner.match_edges(
            ns,
            from_label=L_ENTITY,
            to_label=L_ENTITY,
            current_only=False,
            order_by="id",
            skip=skip,
            limit=_ops._PAGE,
        )
        for row in rows:
            relation = _ops._relation_of(row)
            if since < aware(relation.created_at) <= until:
                target = result.relations_added if relation.revision == 1 else result.relations_revised
                target.append(relation)
            ended = relation.invalidated_known_at
            if ended is not None and since < aware(ended) <= until and aware(relation.created_at) <= aware(ended):
                key = (relation.id, ended)
                previous = retired.get(key)
                if previous is None or previous.revision < relation.revision:
                    retired[key] = relation
        if len(rows) < _ops._PAGE:
            break
        skip += len(rows)
    result.relations_added.sort(key=lambda r: (r.id, r.revision))
    result.relations_revised.sort(key=lambda r: (r.id, r.revision))
    result.relations_retired = sorted(retired.values(), key=lambda r: (r.id, aware(r.invalidated_known_at)))
    skip = 0
    while True:
        rows = await runner.match_nodes(ns, L_ENTITY, order_by="id", skip=skip, limit=_ops._PAGE)
        for row in rows:
            entity = decode_entity(row)
            if since < aware(entity.created_at) <= until:
                result.entities_created.append(entity)
        if len(rows) < _ops._PAGE:
            break
        skip += len(rows)
    result.entities_created.sort(key=lambda e: e.id)
    return result
