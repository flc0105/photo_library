import os
import re
import shutil
import subprocess
from pathlib import Path


EXIFTOOL_REQUIRED_VERSION = '13.55'
_EXIFTOOL_FALLBACK_PATHS = (
    '/opt/homebrew/bin/exiftool',
    '/usr/local/bin/exiftool',
)


def probe_exiftool_version(exiftool_path):
    if not exiftool_path:
        return ''
    try:
        result = subprocess.run(
            [str(exiftool_path), '-ver'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def _version_key(version):
    parts = [int(part) for part in re.findall(r'\d+', str(version or ''))]
    return tuple(parts) if parts else (-1,)


def resolve_exiftool():
    """Return the canonical ExifTool path, preferring the required project version."""
    candidates = [shutil.which('exiftool'), *_EXIFTOOL_FALLBACK_PATHS]
    installed = []
    seen = set()

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen or not path.is_file() or not os.access(path, os.X_OK):
            continue
        seen.add(key)
        version = probe_exiftool_version(path)
        if version:
            installed.append((version, str(path)))

    for version, path in installed:
        if version == EXIFTOOL_REQUIRED_VERSION:
            return path

    if not installed:
        return ''
    return max(installed, key=lambda item: _version_key(item[0]))[1]
