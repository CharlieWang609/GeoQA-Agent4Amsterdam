# Notices

GeoQA Agent is distributed under `GPL-3.0-only`. The following materials retain their own copyright, attribution, and license terms.

## QuAnGIS Workflow

- Upstream project: [quangis/quangis-workflow](https://github.com/quangis/quangis-workflow)
- License: `GPL-3.0-only`
- Included files:
  - `src/geoqa_agent/cct.py`
  - `src/geoqa_agent/polytype.py`
  - `ontology/tools/abstract.ttl`
  - `ontology/tools/multi.ttl`
- Provenance and permitted local changes are recorded in the Python file headers. The ontology files are unmodified upstream data.

These files are vendored from the QuAnGIS workflow repository. Their upstream notices must not be replaced by GeoQA's project-owned source headers.

## TransForge

- Upstream project: [quangis/transforge](https://github.com/quangis/transforge)
- License: `GPL-3.0-or-later`
- Integration: in-process Python dependency used for CCT parsing and type inference
- Pinned source revision: `3b40e88e478a418109cc0fd2447bfd86ea4b6623`

The exact source reference is recorded in `pixi.lock`.

## Core Concept Data Ontology

- Work: Core Concept Data Ontology, version 1
- Creator: Simon Scheider
- Included file: `ontology/ccd.ttl`
- Canonical source: <http://geographicknowledge.de/vocab/CoreConceptData.rdf>
- Citation: Scheider et al. (2020), "Ontology of core concept data types for answering geo-analytical questions," *Journal of Spatial Information Science* 20, 167–201, <https://doi.org/10.5311/JOSIS.2020.20.55>

The ontology embeds its title, creator, citation, version, and license metadata. GeoQA redistributes it without replacing those statements.
