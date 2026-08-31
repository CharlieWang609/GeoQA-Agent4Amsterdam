# SPDX-License-Identifier: GPL-3.0-only

"""Closed CCD vocabularies accepted by MVP semantic annotations."""

from enum import StrEnum


class LayerCCDMeaning(StrEnum):
    """Supported layer/entity meanings from the CCD ontology."""

    OBJECT = "ObjectDS"
    EVENT = "EventDS"
    NETWORK = "NetworkDS"
    POINT_MEASURES = "PointMeasuresDS"
    COVERAGE = "CoverageDS"
    LATTICE = "LatticeDS"
    PATCH = "PatchDS"
    CONTOUR = "ContourDS"


class AttributeCCDMeaning(StrEnum):
    """Supported attribute measurement meanings from the CCD ontology."""

    BOOLEAN = "BooleanA"
    NOMINAL = "NominalA"
    ORDINAL = "OrdinalA"
    INTERVAL = "IntervalA"
    RATIO = "RatioA"
    COUNT = "CountA"
    EXTENSIVE_RATIO = "ERA"
    INTENSIVE_RATIO = "IRA"
