# Ontology Files

Vendored, read-only files (upgraded only by re-copying upstream):

- `ccd.ttl` — the Core Concept Data types ontology (CCD). License: `CC-BY-3.0`.
  Source: Scheider et al. (2020), "Ontology of core concept data types for
  answering geo-analytical questions", Journal of Spatial Information
  Science 20, via the quangis-workflow repository.
- `tools/abstract.ttl` — expert-authored semantic tool signatures
  (`:Abstraction` entries with CCD-typed inputs/outputs and `cct:expression`s).
- `tools/multi.ttl` — offline reference for multi-step chain patterns; never
  loaded at runtime.

Project-owned files:

- `tools/geopandas.ttl` — the Abstraction → GeoPandas operation mapping
  maintained by this project (operation contracts live in
  `src/geoqa_agent/tool_registry.py`).

Attribution note: the CCD vocabulary definitions embedded in the
`catalog_annotation` prompt versions (v6 and later, under
`src/geoqa_agent/prompts/`) are condensed from the `rdfs:comment` entries of
`ccd.ttl` (Scheider et al. 2020, `CC-BY-3.0`).
