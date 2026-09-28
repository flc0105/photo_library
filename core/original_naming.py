from pathlib import Path


def original_stem_key(path: Path, *, is_jpg: bool) -> str:
    """Return the canonical Original stem used by validation and sync tools."""
    stem = Path(path).stem
    if is_jpg and stem.casefold().endswith('-dpp'):
        stem = stem[:-4]
    return stem.casefold()
