# SPDX-License-Identifier: GPL-3.0-only

"""Project RDF namespaces and small helpers.

Minimal project-owned replacement for quangis-workflow's quangis/namespace.py,
reduced to what this project actually uses. The vendored polytype.py imports
RDFS and n3 from here.
"""

from rdflib import Graph
from rdflib.namespace import Namespace, RDFS  # noqa: F401 (re-exported)
from rdflib.term import Node, URIRef

# Used by the vendored upstream tests.
EX = Namespace("https://example.com/#")

# QuAnGIS vocabularies (match the vendored TTL files under ontology/).
CCD = Namespace("http://geographicknowledge.de/vocab/CoreConceptData.rdf#")
ADA = Namespace("http://geographicknowledge.de/vocab/AnalysisData.rdf#")
TOOL = Namespace("https://quangis.github.io/vocab/tool#")
ABSTR = Namespace("https://quangis.github.io/tool/abstract#")
CCT = Namespace("https://quangis.github.io/vocab/cct#")

# Project-local namespace for our GeoPandas tool registry
# (ontology/tools/geopandas.ttl). Not dereferenceable.
GEOPANDAS = Namespace("https://geoqa-agent.org/tool/geopandas#")

_PREFIXES = {
    "ex": EX,
    "ccd": CCD,
    "ada": ADA,
    "tool": TOOL,
    "abstr": ABSTR,
    "cct": CCT,
    "gpd": GEOPANDAS,
}

_g = Graph()
for _prefix, _ns in _PREFIXES.items():
    _g.bind(_prefix, _ns)


def n3(node: Node) -> str:
    """Render an RDF node compactly using the project prefixes."""
    if isinstance(node, URIRef):
        return node.n3(_g.namespace_manager)
    return str(node)
