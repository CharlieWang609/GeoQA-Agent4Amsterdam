// SPDX-License-Identifier: GPL-3.0-only

import { describe, expect, it } from "vitest";

import { retrievedData } from "./retrievedData";
import { passDraft } from "./test/fixtures";

describe("retrievedData", () => {
  it("attributes identity fields and expression columns to the bindings that supply them", () => {
    const bindings = retrievedData(passDraft());

    expect(bindings.map((binding) => binding.layer)).toEqual(["buurten", "openbaresportplek"]);
    expect(bindings[0].attributes).toEqual([
      "begin_geldigheid", "eind_geldigheid", "identificatie", "sports_count", "volgnummer",
    ]);
    expect(bindings[1].attributes).toEqual(["id"]);
    expect(bindings.map((binding) => binding.geometry)).toEqual(["Polygon", "Point"]);
  });
});
