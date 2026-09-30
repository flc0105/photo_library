import os
import re
import shutil
import subprocess
from pathlib import Path


EXIFTOOL_REQUIRED_VERSION = '13.55'
IMAGEMAGICK_REQUIRED_VERSION = '7.1.2-32'
IMAGEMAGICK_REQUIRED_QUANTUM = 'Q16-HDRI'

_EXIFTOOL_FALLBACK_PATHS = (
    '/opt/homebrew/bin/exiftool',
    '/usr/local/bin/exiftool',
)
_MAGICK_FALLBACK_PATHS = (
    '/opt/homebrew/bin/magick',
    '/usr/local/bin/magick',
    '/opt/imagemagick/bin/magick',
)


def probe_imagemagick(magick_path):
    if not magick_path:
        return {'version': '', 'quantum': '', 'banner': ''}
    try:
        result = subprocess.run(
            [str(magick_path), '-version'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {'version': '', 'quantum': '', 'banner': ''}
    if result.returncode != 0:
        return {'version': '', 'quantum': '', 'banner': ''}
    output = (result.stdout or result.stderr or '').strip()
    first_line = output.splitlines()[0].strip() if output else ''
    match = re.search(r'ImageMagick\s+([^\s]+)\s+(Q\d+(?:-HDRI)?)', first_line, re.IGNORECASE)
    return {
        'version': match.group(1) if match else '',
        'quantum': match.group(2).upper() if match else '',
        'banner': first_line,
    }


def resolve_imagemagick():
    """Return an ImageMagick executable, preferring the exact locked build."""
    candidates = [shutil.which('magick'), *_MAGICK_FALLBACK_PATHS]
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
        info = probe_imagemagick(path)
        if info['version']:
            installed.append((info, str(path)))

    for info, path in installed:
        if (
            info['version'] == IMAGEMAGICK_REQUIRED_VERSION
            and info['quantum'] == IMAGEMAGICK_REQUIRED_QUANTUM
        ):
            return path

    if not installed:
        return ''
    return max(installed, key=lambda item: _version_key(item[0]['version']))[1]


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
