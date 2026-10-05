from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_FOLDER = PROJECT_ROOT / "storage"
UPLOAD_FOLDER = str(STORAGE_FOLDER / "uploads")
THUMBNAIL_FOLDER = str(STORAGE_FOLDER / "thumbnails")
COMPRESSED_FOLDER = str(STORAGE_FOLDER / "compressed")
LIBRARY_CACHE_FOLDER = str(STORAGE_FOLDER / "library_cache")

for _folder in (UPLOAD_FOLDER, THUMBNAIL_FOLDER, COMPRESSED_FOLDER, LIBRARY_CACHE_FOLDER):
    Path(_folder).mkdir(parents=True, exist_ok=True)



def original_stem_key(path: Path, *, is_jpg: bool) -> str:
    """Return the canonical Original stem used by validation and sync tools."""
    stem = Path(path).stem
    if is_jpg and stem.casefold().endswith('-dpp'):
        stem = stem[:-4]
    return stem.casefold()

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')


def list_top_level_images(directory: Path):
    if not directory.is_dir():
        return []
    # Target order in the original Qt worker was explicitly sorted.
    return sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )


def list_source_images_original_order(directory: Path):
    if not directory.is_dir():
        return []
    # Preserve the old os.listdir() tie behaviour for source candidates.
    import os
    names = os.listdir(directory)
    return [directory / name for name in names if (directory / name).is_file() and Path(name).suffix.lower() in IMAGE_EXTENSIONS]
