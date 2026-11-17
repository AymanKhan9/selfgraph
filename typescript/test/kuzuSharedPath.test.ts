import { afterEach, describe, expect, it } from "vitest";
import type { Schema } from "../src/index.js";
import { KuzuStore, SelfGraph } from "../src/index.js";
import { tempKuzu, uniqueNs } from "./helpers.js";

const MAX_ONE: Schema = {
  entities: [{ name: "Person" }, { name: "Company" }],
  relations: [{ type: "WORKS_AT", from: { types: ["Person"], max_count: 1 } }],
};

const opened: KuzuStore[] = [];

function store(path: string): KuzuStore {
  const made = new KuzuStore(path);
  opened.push(made);
  return made;
}

async function twoClients(path: string): Promise<number> {
  const ns = uniqueNs("shared");
  const options = { namespace: ns, schema: MAX_ONE, contradiction: "supersede" as const };
  const first = new SelfGraph(store(path), options);
  const second = new SelfGraph(store(path), options);
  await first.initialize();
  await second.initialize();
  await first.insert.facts({
    entities: [
      { type: "Person", name: "Ann" },
      { type: "Company", name: "Acme" },
      { type: "Company", name: "Globex" },
    ],
  });
  await Promise.all([
    first.relations.create("person:ann", "WORKS_AT", "company:acme"),
    second.relations.create("person:ann", "WORKS_AT", "company:globex"),
  ]);
  const current = (await first.relations.list()).items;
  await first.close();
  await second.close();
  return current.length;
}

describe("two KuzuStore instances over one database", () => {
  afterEach(async () => {
    await Promise.all(opened.splice(0).map((s) => s.close().catch(() => undefined)));
  });

  it("serialize their writes, so max_count holds", async () => {
    expect(await twoClients(tempKuzu("selfgraph-shared-"))).toBe(1);
  });

  it("share one write queue, and drop it with the database", async () => {
    const path = tempKuzu("selfgraph-shared-");
    const options = { namespace: uniqueNs("shared"), schema: MAX_ONE };
    const first = new SelfGraph(store(path), options);
    const second = new SelfGraph(store(path), options);
    await first.initialize();
    await second.initialize();
    const shared = (first.store as KuzuStore).writeQueueKey();
    expect((second.store as KuzuStore).writeQueueKey()).toBe(shared);
    await first.close();
    await second.close();
    expect(store(path).writeQueueKey()).not.toBe(shared);
  });

  it("initialize the same fresh path from two stores at once", async () => {
    const path = tempKuzu("selfgraph-shared-");
    const options = { namespace: uniqueNs("shared"), schema: MAX_ONE };
    const first = new SelfGraph(store(path), options);
    const second = new SelfGraph(store(path), options);
    await Promise.all([first.initialize(), second.initialize()]);
    await first.insert.facts({ entities: [{ type: "Person", name: "Ann" }] });
    expect((await first.entities.list()).items.length).toBe(1);
    await first.close();
    await second.close();
  });
});
