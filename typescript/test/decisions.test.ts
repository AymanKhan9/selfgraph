import { expect, it } from "vitest";
import { ids, ValidationError } from "../src/index.js";
import { forEachStore, freshGraphs, type StoreSpec } from "./helpers.js";

function suite(spec: StoreSpec): void {
  const open = freshGraphs(spec, "step-ids");
  for (const stepFirst of [false, true]) {
    it(`keeps decisions and steps separate with stepFirst=${stepFirst}`, async () => {
      const g = open();
      await g.entities.create("Ann", "Person");
      await g.entities.create("Bob", "Person");
      const rel = await g.relations.create("person:ann", "KNOWS", "person:bob");
      const withStep = () => g.decisions.create("With a step", { id: "decision:first", steps: [rel.id] });
      const other = () => g.decisions.create("Independent decision", { id: "decision:first:step:0" });
      if (stepFirst) {
        await withStep();
        await other();
      } else {
        await other();
        await withStep();
      }
      expect((await g.decisions.get("decision:first:step:0"))?.title).toBe("Independent decision");
      expect((await g.decisions.get("decision:first"))?.steps.map((s) => s.relation_id)).toEqual([rel.id]);
      await g.decisions.delete("decision:first:step:0");
      expect((await g.decisions.get("decision:first"))?.steps.map((s) => s.relation_id)).toEqual([rel.id]);
      expect((await g.relations.delete(rel.id)).deleted_decisions).toBe(1);
      expect(await g.decisions.get("decision:first")).toBeNull();
    });
    it(`reserves the internal step prefix with stepFirst=${stepFirst}`, async () => {
      const g = open();
      await g.entities.create("Ann", "Person");
      await g.entities.create("Bob", "Person");
      const rel = await g.relations.create("person:ann", "KNOWS", "person:bob");
      const withStep = () => g.decisions.create("With a step", { id: "decision:first", steps: [rel.id] });
      if (stepFirst) await withStep();
      const id = "decisionstep:decision:first:step:0";
      await expect(g.entities.create("Intruder", "Thing", { id })).rejects.toThrow(/reserved/);
      await expect(g.decisions.create("Intruder", { id })).rejects.toThrow(ValidationError);
      if (!stepFirst) await withStep();
      expect((await g.decisions.get("decision:first"))?.steps.map((s) => s.relation_id)).toEqual([rel.id]);
    });
  }
}
await forEachStore("decision step ids", suite);

it("round trips internal step ids", () => {
  for (const tid of ["decision:first", "decision:first:step:0", "decision:日本語"]) {
    for (const order of [0, 1, 99]) {
      const sid = ids.stepId(tid, order);
      expect(sid).toBe(`decisionstep:${tid}:step:${order}`);
      expect(ids.traceOfStep(sid)).toBe(tid);
    }
  }
});
