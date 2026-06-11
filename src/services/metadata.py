from __future__ import annotations

from src.models import NovelMetadata


def metadata_to_dict(metadata: NovelMetadata) -> dict[str, object]:
    return {
        "title": metadata.title,
        "translated": metadata.translated,
        "author": metadata.author,
        "source_url": metadata.source_url,
        "illustration_url": metadata.illustration_url,
        "site_name": metadata.site_name,
    }
