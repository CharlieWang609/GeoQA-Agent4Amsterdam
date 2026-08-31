# SPDX-License-Identifier: GPL-3.0-only

"""Load the CCD ontology and its three semantic dimensions.

Minimal project-owned replacement for quangis-workflow's quangis/ccd.py,
pointed at this repo's vendored ontology/ccd.ttl.
"""

from pathlib import Path

from rdflib import Graph

from geoqa_agent.namespace import CCD
from geoqa_agent.polytype import Dimension

CCD_TTL = Path(__file__).resolve().parents[2] / "ontology" / "ccd.ttl"


class CoreConceptData(Graph):
    """The CCD ontology graph plus its three dimensions
    (core concept, layer/geometry, measurement scale)."""

    def __init__(self, path: Path = CCD_TTL):
        super().__init__()
        self.parse(path, format="ttl")
        self.dimensions: list[Dimension] = [
            Dimension(root, self, CCD)
            for root in (CCD.CoreConceptQ, CCD.LayerA, CCD.NominalA)
        ]


ccd = CoreConceptData()
