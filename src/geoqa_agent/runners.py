# SPDX-License-Identifier: GPL-3.0-only

"""The in-process runtime: one runner per operation namespace."""

from __future__ import annotations

from typing import Mapping

import geopandas as gpd
import rasterio
import shapely

from geoqa_agent.execution import (
    ExecutionRuntimeProvenance,
    OperationRunResult,
    OperationRunner,
)
from geoqa_agent.gee_runner import GeeRunner
from geoqa_agent.geopandas_runner import GeoPandasRunner
from geoqa_agent.rasterio_runner import RasterioRunner


class NamespaceRunner:
    """Dispatch each operation to the runner of its id's namespace."""

    def __init__(self, runners: Mapping[str, OperationRunner]) -> None:
        self._runners = runners

    def run(
        self,
        algorithm_id: str,
        parameters: Mapping[str, object],
    ) -> OperationRunResult:
        namespace = algorithm_id.split(":", 1)[0]
        return self._runners[namespace].run(algorithm_id, parameters)


def default_runner() -> NamespaceRunner:
    return NamespaceRunner(
        {"geopandas": GeoPandasRunner(), "rasterio": RasterioRunner(), "gee": GeeRunner()}
    )


def runtime_provenance(code_commit: str) -> ExecutionRuntimeProvenance:
    """Version identity of the in-process runtime."""

    return ExecutionRuntimeProvenance(
        geopandas=gpd.__version__,
        shapely=shapely.__version__,
        rasterio=rasterio.__version__,
        gdal=rasterio.__gdal_version__,
        code_commit=code_commit,
    )
