import { comparePy } from "./internal/py.js";
import { epochMicros } from "./internal/time.js";
import { L_ENTITY } from "./labels.js";
import type { GraphDiff, Relation } from "./models.js";
import { relationOf } from "./ops.js";
import type { GraphPrimitives } from "./protocols.js";
import { decodeEntity } from "./serde.js";

const PAGE = 500;
const order = (a: Relation, b: Relation): number => comparePy(a.id, b.id) || a.revision - b.revision;

/**
 * Scan stored changes one page at a time, retaining only changes in the window.
 *
 * @param runner (GraphPrimitives): the graph reader.
 * @param ns (string): the namespace.
 * @param since (Date): exclusive known-time lower bound.
 * @param until (Date): inclusive known-time upper bound.
 * @return (GraphDiff): changes in id and revision order.
 */
export async function graphDiff(runner: GraphPrimitives, ns: string, since: Date, until: Date): Promise<GraphDiff> {
  const result: GraphDiff = {
    since,
    until,
    relations_added: [],
    relations_retired: [],
    relations_revised: [],
    entities_created: [],
    limitations: ["Deletes and merges are not visible."],
  };
  const within = (stamp: Date): boolean =>
    epochMicros(since) < epochMicros(stamp) && epochMicros(stamp) <= epochMicros(until);
  const retired = new Map<string, Relation>();
  for (let skip = 0; ; skip += PAGE) {
    const rows = await runner.matchEdges(ns, {
      from_label: L_ENTITY,
      to_label: L_ENTITY,
      current_only: false,
      skip,
      limit: PAGE,
    });
    for (const row of rows) {
      const relation = relationOf(row);
      if (within(relation.created_at)) {
        (relation.revision === 1 ? result.relations_added : result.relations_revised).push(relation);
      }
      const ended = relation.invalidated_known_at;
      if (ended !== null && within(ended) && epochMicros(relation.created_at) <= epochMicros(ended)) {
        const key = JSON.stringify([relation.id, epochMicros(ended)]);
        const previous = retired.get(key);
        if (!previous || previous.revision < relation.revision) retired.set(key, relation);
      }
    }
    if (rows.length < PAGE) break;
  }
  result.relations_added.sort(order);
  result.relations_revised.sort(order);
  result.relations_retired = [...retired.values()].sort(
    (a, b) => comparePy(a.id, b.id) || epochMicros(a.invalidated_known_at!) - epochMicros(b.invalidated_known_at!),
  );
  for (let skip = 0; ; skip += PAGE) {
    const rows = await runner.matchNodes(ns, L_ENTITY, { skip, limit: PAGE });
    for (const row of rows) {
      const entity = decodeEntity(row);
      if (within(entity.created_at)) result.entities_created.push(entity);
    }
    if (rows.length < PAGE) break;
  }
  result.entities_created.sort((a, b) => comparePy(a.id, b.id));
  return result;
}
