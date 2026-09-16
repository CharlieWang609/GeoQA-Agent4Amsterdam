# SPDX-License-Identifier: GPL-3.0-only

"""Enrich the current Catalog through the Administrator job."""

from __future__ import annotations

from catalog_stores import azure_store
from data_pipeline.azure_storage import AzureBlobObjectStore
from geoqa_agent.model_clients import annotation_client
from metadata_annotation import MetadataAnnotationJob


def main() -> None:
    """Run semantic enrichment without exposing a Sandbox API route."""
    store = azure_store()
    with annotation_client() as client:
        version = MetadataAnnotationJob(store, client).enrich_current()
    print(version)
    report_unresolved(store)


def report_unresolved(store: AzureBlobObjectStore) -> None:
    """Fail loudly when identity-field annotations did not resolve —
    grounding depends on them, so an operator must re-annotate."""

    from data_pipeline.catalog import CatalogReader
    from data_pipeline.models import AnnotationStatus

    unresolved = []
    for layer in CatalogReader(store).current().layers:
        if layer.enriched is None:
            unresolved.append(f"{layer.layer_id}: not enriched")
            continue
        attributes = {a.name: a for a in layer.enriched.attributes}
        for field in layer.source_identity_fields:
            attribute = attributes.get(field)
            if attribute is None:
                unresolved.append(f"{layer.layer_id}.{field}: missing")
                continue
            for kind, annotation in (
                ("semantic_label", attribute.semantic_label),
                ("measurement_scale", attribute.measurement_scale),
            ):
                if annotation.status is not AnnotationStatus.RESOLVED:
                    unresolved.append(f"{layer.layer_id}.{field}.{kind}")
    if unresolved:
        raise SystemExit(
            "Identity-field annotations left unresolved (grounding will "
            "reject them); run annotation again:\n  " + "\n  ".join(unresolved)
        )
    print("all identity-field annotations resolved")


if __name__ == "__main__":
    main()
