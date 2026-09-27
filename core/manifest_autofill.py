import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image


DATETIME_ORIGINAL_TAG = 36867
JPEG_EXTENSIONS = {'.jpg', '.jpeg'}


def _parse_exif_datetime(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='ignore')
    text = str(value).strip()
    if not text:
        return None
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _datetime_original_with_pillow(path):
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            return _parse_exif_datetime(exif.get(DATETIME_ORIGINAL_TAG))
    except (OSError, ValueError, TypeError):
        return None


def _datetime_original_with_exiftool(path):
    executable = shutil.which('exiftool')
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, '-s3', '-DateTimeOriginal', str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_exif_datetime(result.stdout)


def get_datetime_original(path):
    """Read EXIF DateTimeOriginal from a still image.

    JPGs normally work through Pillow. ExifTool is a fallback for files Pillow
    cannot decode cleanly or whose EXIF layout is unusual.
    """
    path = Path(path)
    return _datetime_original_with_pillow(path) or _datetime_original_with_exiftool(path)


def get_original_jpg_time_range(set_folder):
    """Return HH:mm for the earliest/latest DateTimeOriginal in 01_Original/JPG."""
    jpg_dir = Path(set_folder) / '01_Original' / 'JPG'
    if not jpg_dir.is_dir():
        return {'start_time': '', 'end_time': ''}

    timestamps = []
    try:
        photos = [
            path for path in jpg_dir.iterdir()
            if path.is_file() and path.suffix.lower() in JPEG_EXTENSIONS
        ]
    except OSError:
        photos = []

    for photo in photos:
        value = get_datetime_original(photo)
        if value is not None:
            timestamps.append(value)

    if not timestamps:
        return {'start_time': '', 'end_time': ''}

    return {
        'start_time': min(timestamps).strftime('%H:%M'),
        'end_time': max(timestamps).strftime('%H:%M'),
    }


def _nonempty(value):
    return value is not None and value != ''


def _manifest_paths(root):
    """Find manifests without descending into a Set once its manifest is found."""
    root = Path(root)
    found = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith('.')]
        if 'manifest.json' not in files:
            continue
        path = Path(current) / 'manifest.json'
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        found.append((mtime, path))
        # A manifest is owned by a Set. No useful manifest should exist below it,
        # and pruning avoids walking Original/RAW trees.
        dirs[:] = []
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def build_manifest_reference_index(roots):
    """Build reusable manifest suggestions by scanning enabled library roots.

    The filesystem manifests remain the only source of these candidates; no
    separate candidate table is created or maintained.
    """
    if isinstance(roots, (str, Path)):
        roots = [roots]

    manifest_rows = []
    seen = set()
    for root in roots or []:
        for manifest_path in _manifest_paths(root):
            try:
                resolved = manifest_path.resolve()
            except OSError:
                resolved = manifest_path
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                mtime = manifest_path.stat().st_mtime_ns
            except OSError:
                mtime = 0
            manifest_rows.append((mtime, manifest_path))
    manifest_rows.sort(key=lambda item: item[0], reverse=True)

    locations = {}
    sources = {}
    # Frequency and source-to-character candidates are derived per scan;
    # manifests remain the only stored source of truth.
    source_counts = {}
    source_characters = {}
    source_character_seen = {}

    # Newer manifests determine display spelling while every distinct value is
    # retained as an editor candidate for later reuse.
    value_keys = (
        'models',
        'environments',
        'weathers',
        'scenes',
        'theme_names',
        'genres',
        'source_types',
        'characters',
        'variants',
        'reference_types',
        'references',
        'outfits',
        'collaboration_types',
        'venue_fee_payers',
        'assistants',
        'primary_photographers',
        'subject_props',
        'set_props',
        'light_types',
        'roles',
        'fixtures',
        'modifiers',
        'positions',
        'lighting_notes',
    )
    values = {key: [] for key in value_keys}
    value_seen = {key: set() for key in value_keys}

    def add_value(key, value):
        if key not in values:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add_value(key, item)
            return
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        folded = text.casefold()
        if folded in value_seen[key]:
            return
        value_seen[key].add(folded)
        values[key].append(text)

    for _, manifest_path in manifest_rows:
        try:
            with manifest_path.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        add_value('models', data.get('model'))

        shoot = data.get('shoot') if isinstance(data.get('shoot'), dict) else {}
        add_value('environments', shoot.get('environment'))
        add_value('weathers', shoot.get('weather'))
        add_value('scenes', shoot.get('scene'))
        additional_sessions = shoot.get('additional_sessions') if isinstance(shoot.get('additional_sessions'), list) else []
        for additional in additional_sessions:
            if isinstance(additional, dict):
                add_value('weathers', additional.get('weather'))

        location = data.get('location') if isinstance(data.get('location'), dict) else {}
        location_name = str(location.get('name') or '').strip()
        if location_name:
            key = location_name.casefold()
            entry = locations.setdefault(key, {
                'name': location_name,
                'address': '',
                'lat': None,
                'lng': None,
            })
            for field in ('address', 'lat', 'lng'):
                if not _nonempty(entry.get(field)) and _nonempty(location.get(field)):
                    entry[field] = location.get(field)

        theme = data.get('theme') if isinstance(data.get('theme'), dict) else {}
        add_value('theme_names', theme.get('name'))
        add_value('genres', theme.get('genre'))
        add_value('source_types', theme.get('source_type'))
        add_value('characters', theme.get('character'))
        add_value('variants', theme.get('variant'))
        add_value('reference_types', theme.get('reference_type'))
        add_value('references', theme.get('reference'))
        add_value('outfits', theme.get('outfit'))

        source_title = str(theme.get('source_title') or '').strip()
        source_type = theme.get('source_type') or ''
        character = str(theme.get('character') or '').strip()
        if source_title:
            key = source_title.casefold()
            entry = sources.setdefault(key, {
                'title': source_title,
                'source_type': ''
            })
            if not entry.get('source_type') and source_type:
                entry['source_type'] = source_type

            source_counts[key] = source_counts.get(key, 0) + 1
            if character:
                seen = source_character_seen.setdefault(key, set())
                folded_character = character.casefold()
                if folded_character not in seen:
                    seen.add(folded_character)
                    source_characters.setdefault(key, []).append(character)

        production = data.get('production') if isinstance(data.get('production'), dict) else {}
        add_value('collaboration_types', production.get('collaboration_type'))
        add_value('venue_fee_payers', production.get('venue_fee_payer'))
        add_value('assistants', production.get('assistants'))
        add_value('primary_photographers', production.get('primary_photographer'))

        props = data.get('props') if isinstance(data.get('props'), dict) else {}
        add_value('subject_props', props.get('subject'))
        add_value('set_props', props.get('set'))

        lighting = data.get('lighting') if isinstance(data.get('lighting'), list) else []
        for light in lighting:
            if not isinstance(light, dict):
                continue
            add_value('roles', light.get('role'))
            add_value('light_types', light.get('light_type'))
            add_value('fixtures', light.get('fixture'))
            add_value('modifiers', light.get('modifier'))
            add_value('positions', light.get('position'))
            add_value('lighting_notes', light.get('note'))

    ordered_source_keys = sorted(
        sources,
        key=lambda key: (-source_counts.get(key, 0), sources[key]['title'].casefold()),
    )
    source_items = [
        {
            **sources[key],
            'characters': source_characters.get(key, []),
        }
        for key in ordered_source_keys
    ]

    return {
        'locations': sorted(locations.values(), key=lambda item: item['name'].casefold()),
        'sources': source_items,
        'values': values,
    }
