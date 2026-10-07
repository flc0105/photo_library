import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from PIL import Image
from flask import Blueprint, jsonify, request
from core.external_tools import probe_exiftool_version, resolve_exiftool
from core.filesystem import original_stem_key
from features.smart.runtime import resolve_shoot_time


SMART_ALBUM_DB_FILENAME = 'smart_albums.db'

SMART_ALBUM_QUERY_TIMEOUT_SECONDS = 10

SMART_ALBUM_EXIF_BATCH_SIZE = 25  # Smaller batches keep index progress visibly granular without changing query semantics.

SMART_ALBUM_INDEX_STAGE_DEFS = (
    ('base_edit', '02_Base_Edit'),
    ('model_edit', '03_Model_Edit'),
    ('revision', '04_Revision'),
    ('final', '05_Final'),
)

SMART_ALBUM_CAPTURE_METADATA_SOURCE = 'asset'

_SET_FOLDER_RE = re.compile(r'^\d{8}-.+-.+$')

_DISPLAY_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}

_RAW_EXTENSIONS = {'.cr3', '.cr2', '.dng', '.nef', '.arw', '.raf', '.rw2', '.orf'}

_STAGE_DEFS = SMART_ALBUM_INDEX_STAGE_DEFS

_SKIP_DIR_NAMES = {'deleted', 'discards', 'intermediates'}

_CAPTURE_METADATA_MODES = {'asset', 'original_jpg', 'original_jpg_raw'}


class SmartAlbumIndexCancelled(RuntimeError):
    pass


def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


def _index_policy_signature():
    """Invalidate only the Smart Album cache when indexing policy changes."""
    return json.dumps({
        'stages': list(_STAGE_DEFS),
        'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
    }, ensure_ascii=False, sort_keys=True)


def _connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _init_smart_db(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS smart_albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            python_code TEXT NOT NULL,
            last_result_count INTEGER,
            last_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS smart_album_assets (
            photo_id TEXT PRIMARY KEY,
            source_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            set_path TEXT NOT NULL,
            set_name TEXT NOT NULL,
            stage TEXT NOT NULL,
            logical_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            file_size INTEGER,
            file_mtime_ns INTEGER,
            file_mtime TEXT,
            width INTEGER,
            height INTEGER,
            aspect_ratio REAL,
            orientation TEXT,
            image_mode TEXT,
            color_space TEXT,
            color_space_status TEXT,
            bit_depth_json TEXT,
            exif_json TEXT NOT NULL DEFAULT '{}',
            capture_exif_json TEXT NOT NULL DEFAULT '{}',
            capture_donor_relative_path TEXT,
            capture_time TEXT,
            capture_camera TEXT,
            capture_lens TEXT,
            capture_focal_length_mm REAL,
            capture_iso REAL,
            capture_gps_lat REAL,
            capture_gps_lng REAL,
            indexed_at TEXT NOT NULL,
            UNIQUE(source_id, relative_path)
        );

        CREATE INDEX IF NOT EXISTS idx_smart_album_assets_source
            ON smart_album_assets(source_id);
        CREATE INDEX IF NOT EXISTS idx_smart_album_assets_set
            ON smart_album_assets(source_id, set_path);
        CREATE INDEX IF NOT EXISTS idx_smart_album_assets_stage
            ON smart_album_assets(stage);
        CREATE INDEX IF NOT EXISTS idx_smart_album_assets_logical
            ON smart_album_assets(logical_id);

        CREATE TABLE IF NOT EXISTS smart_album_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    ''')
    conn.commit()
    conn.close()


def _meta_get(conn, key, default=''):
    row = conn.execute('SELECT value FROM smart_album_meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _meta_set(conn, key, value):
    conn.execute(
        '''INSERT INTO smart_album_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
        (key, str(value)),
    )


def _enabled_sources(main_db_path):
    conn = _connect(main_db_path)
    rows = conn.execute(
        'SELECT id, name, root_path FROM library_sources WHERE enabled=1 ORDER BY sort_order, id'
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _selected_enabled_sources(main_db_path, source_ids):
    """Resolve the explicit Smart Album index Source selection."""
    sources = _enabled_sources(main_db_path)
    if not isinstance(source_ids, (list, tuple, set)):
        raise ValueError('source_ids must be an array of Source IDs')
    try:
        requested = {int(source_id) for source_id in source_ids}
    except (TypeError, ValueError):
        raise ValueError('source_ids must contain integer Source IDs only')
    if not requested:
        raise ValueError('Select at least one Source')
    enabled_ids = {int(source['id']) for source in sources}
    invalid_ids = sorted(requested - enabled_ids)
    if invalid_ids:
        raise ValueError('Disabled or missing Sources: ' + ', '.join(str(source_id) for source_id in invalid_ids))
    return [source for source in sources if int(source['id']) in requested]

def _library_states(main_db_path, enabled_source_ids):
    if not enabled_source_ids:
        return {}
    placeholders = ','.join('?' for _ in enabled_source_ids)
    conn = _connect(main_db_path)
    rows = conn.execute(
        f'''SELECT source_id, relative_path, is_favorited, description
            FROM library_image_states
            WHERE source_id IN ({placeholders})''',
        tuple(enabled_source_ids),
    ).fetchall()
    conn.close()
    return {
        (row['source_id'], row['relative_path']): {
            'favorite': bool(row['is_favorited']),
            'description': row['description'] or '',
        }
        for row in rows
    }


def _is_set_name(name):
    return bool(_SET_FOLDER_RE.fullmatch(name or ''))


def _discover_sets(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return []
    found = []
    for current_root, dir_names, _ in os.walk(root, followlinks=False):
        current = Path(current_root)
        dir_names[:] = [
            name for name in dir_names
            if not name.startswith('.') and name.casefold() not in _SKIP_DIR_NAMES
        ]
        if current != root and _is_set_name(current.name):
            found.append(current)
            dir_names[:] = []
    return sorted(found, key=lambda path: path.as_posix().casefold())


def _walk_files(directory, extensions, *, skip_dirs=True):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    files = []
    for current_root, dir_names, file_names in os.walk(directory, followlinks=False):
        if skip_dirs:
            dir_names[:] = [
                name for name in dir_names
                if not name.startswith('.') and name.casefold() not in _SKIP_DIR_NAMES
            ]
        else:
            dir_names[:] = [name for name in dir_names if not name.startswith('.')]
        root = Path(current_root)
        for name in file_names:
            if name.startswith('.') or name.startswith('._'):
                continue
            path = root / name
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() in extensions:
                files.append(path)
    return sorted(files, key=lambda path: path.as_posix().casefold())


def _set_candidates(set_dir):
    entries = []
    for stage, relative_dir in _STAGE_DEFS:
        stage_dir = set_dir / relative_dir
        for path in _walk_files(stage_dir, _DISPLAY_IMAGE_EXTENSIONS):
            entries.append((stage, path))
    return entries


def _logical_id(path):
    return Path(path).stem.split('-', 1)[0]


def _original_indexes(set_dir, *, include_raw=True):
    jpg_files = _walk_files(set_dir / '01_Original' / 'JPG', {'.jpg', '.jpeg'})
    raw_files = _walk_files(set_dir / '01_Original' / 'RAW', _RAW_EXTENSIONS) if include_raw else []
    jpg_index = {}
    raw_index = {}
    for path in jpg_files:
        jpg_index.setdefault(original_stem_key(path, is_jpg=True), []).append(path)
    for path in raw_files:
        raw_index.setdefault(original_stem_key(path, is_jpg=False), []).append(path)
    return jpg_files, raw_files, jpg_index, raw_index


def _choose_donor(candidates, logical_id):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    exact = [path for path in candidates if path.stem.casefold() == logical_id.casefold()]
    return exact[0] if len(exact) == 1 else None


def _run_exiftool_records(exiftool_path, files, *, progress_callback=None, cancel_callback=None):
    if not exiftool_path or not files:
        return {}
    records_by_path = {}
    unique = []
    seen = set()
    for path in files:
        key = str(Path(path).resolve())
        if key not in seen:
            seen.add(key)
            unique.append(Path(path))
    total = len(unique)
    for offset in range(0, total, SMART_ALBUM_EXIF_BATCH_SIZE):
        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index rebuild cancelled')
        chunk = unique[offset:offset + SMART_ALBUM_EXIF_BATCH_SIZE]
        command = [
            exiftool_path,
            '-j', '-a', '-G1', '-s', '-charset', 'filename=UTF8',
            *[str(path) for path in chunk],
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
            check=False,
        )
        if not result.stdout.strip():
            detail = result.stderr.strip() or f'ExifTool exited with code {result.returncode}'
            raise RuntimeError(f'Smart Album ExifTool scan failed: {detail}')
        batch = json.loads(result.stdout)
        if not isinstance(batch, list):
            raise RuntimeError('Invalid Smart Album ExifTool output')
        for record in batch:
            source = record.get('SourceFile')
            if source:
                records_by_path[str(Path(source).resolve())] = record
        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index rebuild cancelled')
        if progress_callback:
            progress_callback(min(offset + len(chunk), total), total)
    return records_by_path


def _text(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _record_first(record, tag_names):
    for tag_name in tag_names:
        if tag_name in record and _text(record.get(tag_name)).strip():
            return record.get(tag_name)
    wanted = {str(tag).casefold() for tag in tag_names}
    wanted_terminal = {str(tag).rsplit(':', 1)[-1].casefold() for tag in tag_names}
    for key, value in record.items():
        key_cf = str(key).casefold()
        terminal_cf = str(key).rsplit(':', 1)[-1].casefold()
        if key_cf in wanted or terminal_cf in wanted_terminal:
            if _text(value).strip():
                return value
    return None


def _record_first_text(record, tag_names):
    return _text(_record_first(record, tag_names)).strip()


def _is_srgb_label(value):
    normalized = re.sub(r'[^a-z0-9]+', '', str(value).casefold())
    return 'nonsrgb' not in normalized and ('srgb' in normalized or 'iec6196621' in normalized)


def _color_space_analysis(record):
    profile = _record_first_text(record, [
        'ICC_Profile:ProfileDescription', 'ICC_Profile:ProfileName',
        'ProfileDescription', 'ProfileName',
    ])
    if profile:
        if _is_srgb_label(profile):
            return 'sRGB', 'srgb'
        return profile, 'non_srgb'

    if _record_first_text(record, ['PNG:SRGBRendering', 'PNG:sRGBRendering', 'SRGBRendering']):
        return 'sRGB', 'srgb'

    interop = _record_first_text(record, ['InteropIFD:InteropIndex', 'EXIF:InteropIndex', 'InteropIndex']).upper()
    if interop.startswith('R98'):
        return 'sRGB', 'srgb'
    if interop.startswith('R03'):
        return 'Adobe RGB', 'non_srgb'

    explicit_srgb = False
    explicit_non_srgb = []
    for key, value in record.items():
        if str(key).rsplit(':', 1)[-1].casefold() != 'colorspace':
            continue
        text = _text(value).strip()
        if not text:
            continue
        if _is_srgb_label(text):
            explicit_srgb = True
            continue
        lower = text.casefold()
        if any(token in lower for token in (
            'adobe rgb', 'wide gamut rgb', 'display p3', 'dci-p3',
            'prophoto', 'romm', 'rec.2020', 'bt.2020',
        )):
            explicit_non_srgb.append(text)
    if explicit_non_srgb and not explicit_srgb:
        return explicit_non_srgb[0], 'non_srgb'
    if explicit_srgb and not explicit_non_srgb:
        return 'sRGB', 'srgb'
    return None, 'unknown'


def _bit_depth(record):
    value = _record_first(record, [
        'File:BitsPerSample', 'PNG:BitDepth', 'JPEG:BitsPerSample',
        'IFD0:BitsPerSample', 'ExifIFD:BitsPerSample', 'BitsPerSample', 'BitDepth',
    ])
    if value is None:
        return None
    values = [int(item) for item in re.findall(r'(?<![.\d])\d+(?![.\d])', _text(value))]
    if not values:
        return None
    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else unique


def _number(value):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return value
    match = re.search(r'-?\d+(?:\.\d+)?', str(value))
    return float(match.group(0)) if match else None


def _parse_capture_time(value):
    text = _text(value).strip()
    if not text:
        return None
    candidates = [text]
    if len(text) >= 19 and re.match(r'^\d{4}:\d{2}:\d{2}', text):
        candidates.append(text[:10].replace(':', '-') + text[10:])
    for candidate in candidates:
        cleaned = candidate.strip()
        cleaned = re.sub(r'([+-]\d{2}:\d{2})$', r'\1', cleaned)
        try:
            return datetime.fromisoformat(cleaned.replace(' ', 'T')).isoformat(timespec='seconds')
        except ValueError:
            pass
        for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(cleaned[:19], fmt).isoformat(timespec='seconds')
            except ValueError:
                continue
    return None


def _smart_album_capture_sort_time(manifest, capture_time, set_name):
    """Derive the display sort time without changing the Python query result."""
    resolved = resolve_shoot_time(manifest, capture_time, set_name)
    return resolved.isoformat(timespec='seconds') if resolved is not None else None


def _gps_number(value):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    # ExifTool commonly renders GPS as DMS text unless numeric output is
    # explicitly requested. Preserve the human-readable full EXIF record while
    # deriving a decimal Python value here.
    parts = [float(item) for item in re.findall(r'-?\d+(?:\.\d+)?', text)]
    if not parts:
        return None
    if 'deg' in text.casefold() and len(parts) >= 2:
        degrees = abs(parts[0])
        minutes = parts[1] if len(parts) >= 2 else 0.0
        seconds = parts[2] if len(parts) >= 3 else 0.0
        result = degrees + minutes / 60.0 + seconds / 3600.0
        if parts[0] < 0 or re.search(r'\b[SW]\b', text, re.IGNORECASE):
            result = -result
        return result
    result = parts[0]
    if result >= 0 and re.search(r'\b[SW]\b', text, re.IGNORECASE):
        result = -result
    return result


def _capture_fields(record):
    capture_time = _parse_capture_time(_record_first(record, [
        'EXIF:DateTimeOriginal', 'ExifIFD:DateTimeOriginal', 'DateTimeOriginal',
        'QuickTime:CreateDate', 'CreateDate',
    ]))
    camera = _record_first_text(record, ['IFD0:Model', 'EXIF:Model', 'Model']) or None
    lens = _record_first_text(record, ['ExifIFD:LensModel', 'EXIF:LensModel', 'LensModel']) or None
    focal = _number(_record_first(record, ['ExifIFD:FocalLength', 'EXIF:FocalLength', 'FocalLength']))
    iso = _number(_record_first(record, ['ExifIFD:ISO', 'EXIF:ISO', 'ISO']))
    lat = _gps_number(_record_first(record, ['Composite:GPSLatitude', 'EXIF:GPSLatitude', 'GPSLatitude']))
    lng = _gps_number(_record_first(record, ['Composite:GPSLongitude', 'EXIF:GPSLongitude', 'GPSLongitude']))
    return {
        'time': capture_time,
        'camera': camera,
        'lens': lens,
        'focal_length_mm': focal,
        'iso': iso,
        'gps_lat': lat,
        'gps_lng': lng,
    }


def _int_value(value):
    number = _number(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


def _record_image_mode(record):
    """Derive a common Pillow-like mode without decoding image pixels."""
    color_type = _record_first_text(record, ['PNG:ColorType', 'ColorType']).casefold()
    if color_type:
        if 'rgba' in color_type or ('rgb' in color_type and 'alpha' in color_type):
            return 'RGBA'
        if 'rgb' in color_type:
            return 'RGB'
        if 'grayscale' in color_type and 'alpha' in color_type:
            return 'LA'
        if 'grayscale' in color_type or 'greyscale' in color_type:
            return 'L'
        if 'palette' in color_type:
            return 'P'

    components = _int_value(_record_first(record, ['File:ColorComponents', 'ColorComponents']))
    if components == 1:
        return 'L'
    if components == 3:
        return 'RGB'
    if components == 4:
        return 'CMYK'
    return None


def _image_facts(path, record=None):
    """Read geometry cheaply; never transpose/decode the full image for the index."""
    record = record or {}
    width = _int_value(_record_first(record, [
        'File:ImageWidth', 'PNG:ImageWidth', 'JPEG:ImageWidth',
        'ExifIFD:ExifImageWidth', 'EXIF:ExifImageWidth', 'ImageWidth',
    ]))
    height = _int_value(_record_first(record, [
        'File:ImageHeight', 'PNG:ImageHeight', 'JPEG:ImageHeight',
        'ExifIFD:ExifImageHeight', 'EXIF:ExifImageHeight', 'ImageHeight',
    ]))
    mode = _record_image_mode(record)
    orientation_text = _record_first_text(record, [
        'IFD0:Orientation', 'EXIF:Orientation', 'Orientation',
    ]).casefold()

    if width is None or height is None or mode is None:
        try:
            # Header fallback only. Avoid ImageOps.exif_transpose(), which may
            # decode/copy the full image for every indexed asset.
            with Image.open(path) as image:
                if width is None or height is None:
                    width, height = image.size
                    try:
                        orientation_value = image.getexif().get(274)
                        if orientation_value in {5, 6, 7, 8}:
                            width, height = height, width
                    except Exception:
                        pass
                if mode is None:
                    mode = image.mode
        except Exception:
            pass
    elif width and height and ('90' in orientation_text or '270' in orientation_text):
        width, height = height, width

    ratio = (float(width) / float(height)) if width and height else None
    if width and height:
        if width > height:
            orientation = 'landscape'
        elif height > width:
            orientation = 'portrait'
        else:
            orientation = 'square'
    else:
        orientation = None
    return width, height, ratio, orientation, mode


def _read_manifest(root, set_path):
    path = Path(root) / set_path / 'manifest.json'
    try:
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _new_index_steps():
    return [
        {
            'id': 'discover_sets',
            'label': 'Scan Sources',
            'status': 'pending',
            'current': 0,
            'total': 0,
            'unit': 'Source',
            'detail': 'Ready',
        },
        {
            'id': 'plan_assets',
            'label': 'Plan Photos',
            'status': 'pending',
            'current': 0,
            'total': 0,
            'unit': 'Set',
            'detail': 'Waiting for Source scan',
        },
        {
            'id': 'index_assets',
            'label': 'Read Metadata',
            'status': 'pending',
            'current': 0,
            'total': 0,
            'unit': 'Photos',
            'detail': 'Waiting for photo plan',
        },
        {
            'id': 'verify',
            'label': 'Verify',
            'status': 'pending',
            'current': 0,
            'total': 1,
            'unit': 'Items',
            'detail': 'Waiting for index write',
        },
    ]


def _refresh_index(smart_db_path, main_db_path, *, source_ids, progress_callback=None, cancel_callback=None):
    if SMART_ALBUM_CAPTURE_METADATA_SOURCE not in _CAPTURE_METADATA_MODES:
        raise RuntimeError(
            'SMART_ALBUM_CAPTURE_METADATA_SOURCE must be one of: ' +
            ', '.join(sorted(_CAPTURE_METADATA_MODES))
        )

    steps = _new_index_steps()
    step_map = {step['id']: step for step in steps}
    summary = {
        'overall_dimension': 'image',
        'source_total': 0,
        'source_available': 0,
        'set_total': 0,
        'asset_total': 0,
        'stage_counts': {stage: 0 for stage, _ in _STAGE_DEFS},
        'manifest_mode': 'live',
        'state_mode': 'live',
        'originals_indexed': any(stage == 'original_jpg' for stage, _ in _STAGE_DEFS),
        'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
    }
    overall_current = 0
    overall_total = 0
    overall_ready = False

    def check_cancelled():
        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index rebuild cancelled')

    def emit(phase, message, *, current=None, total=None, percent=None):
        nonlocal overall_current, overall_total, overall_ready
        if current is not None:
            overall_current = int(current)
        if total is not None:
            overall_total = int(total)
        if percent is None:
            if overall_total > 0:
                percent = 100.0 * overall_current / overall_total
            else:
                percent = 0.0
            # Reserve 100% for the completed state so the bar never reaches
            # 100 and then moves backward while final verification is running.
            if overall_ready and phase != 'done':
                percent = min(percent, 99.0)
        payload = {
            'phase': phase,
            'message': message,
            'overall': {
                'dimension': 'image',
                'label': 'Overall',
                'current': overall_current,
                'total': overall_total,
                'percent': max(0, min(100, int(round(percent)))),
                'ready': overall_ready,
            },
            'steps': copy.deepcopy(steps),
            'summary': copy.deepcopy(summary),
        }
        if progress_callback:
            progress_callback(payload)

    check_cancelled()
    sources = _selected_enabled_sources(main_db_path, source_ids)
    summary['source_total'] = len(sources)
    summary['selected_source_ids'] = [int(source['id']) for source in sources]
    summary['selected_source_names'] = [source['name'] for source in sources]
    exiftool = resolve_exiftool()
    exiftool_version = probe_exiftool_version(exiftool) if exiftool else ''
    warnings = []
    if not exiftool:
        warnings.append('ExifTool unavailable: EXIF-backed fields will be empty.')

    indexed_at = _now_iso()
    set_count = 0
    asset_count = 0
    unavailable_sources = []
    source_jobs = []

    # First discover projects (Sets), then fix the image denominator. The overall
    # bar never switches to Source/Set counts.
    discover_step = step_map['discover_sets']
    discover_step.update(status='active', current=0, total=len(sources), detail='Scanning enabled Sources')
    emit('discover_sets', 'Scan Sources')

    for source_index, source in enumerate(sources, start=1):
        check_cancelled()
        root = Path(source['root_path']).expanduser().resolve()
        if not root.is_dir():
            unavailable_sources.append(source['name'])
            discover_step['current'] = source_index
            discover_step['detail'] = f"Skipped unavailable Source: {source['name']}"
            emit('discover_sets', discover_step['detail'])
            continue

        set_dirs = _discover_sets(root)
        summary['source_available'] += 1
        set_count += len(set_dirs)
        source_jobs.append({
            'source': source,
            'root': root,
            'set_dirs': set_dirs,
            'plans': [],
        })
        discover_step['current'] = source_index
        discover_step['detail'] = f"{source['name']}: {len(set_dirs)} Sets · {set_count} total"
        emit('discover_sets', discover_step['detail'])

    summary['set_total'] = set_count
    discover_step.update(
        status='done',
        current=len(sources),
        total=len(sources),
        detail=f'Scan complete: {summary["source_available"]} Sources · {set_count} Sets',
    )

    plan_step = step_map['plan_assets']
    plan_step.update(status='active', current=0, total=set_count, detail='Planning Base / Model / Revision / Final photos')
    emit('plan_assets', 'Plan Photos')
    planned_sets = 0

    for job in source_jobs:
        root = job['root']
        plans = []
        for set_dir in job['set_dirs']:
            check_cancelled()
            set_rel = set_dir.relative_to(root).as_posix()
            candidates = _set_candidates(set_dir)

            jpg_index = raw_index = {}
            if candidates and SMART_ALBUM_CAPTURE_METADATA_SOURCE != 'asset':
                include_raw = SMART_ALBUM_CAPTURE_METADATA_SOURCE == 'original_jpg_raw'
                _jpg_files, _raw_files, jpg_index, raw_index = _original_indexes(
                    set_dir,
                    include_raw=include_raw,
                )

            for stage, path in candidates:
                logical_id = _logical_id(path)
                donor = path
                if SMART_ALBUM_CAPTURE_METADATA_SOURCE != 'asset':
                    key = logical_id.casefold()
                    donor = _choose_donor(jpg_index.get(key, []), logical_id)
                    if donor is None and SMART_ALBUM_CAPTURE_METADATA_SOURCE == 'original_jpg_raw':
                        donor = _choose_donor(raw_index.get(key, []), logical_id)
                plans.append((set_dir, set_rel, stage, path, logical_id, donor))
                summary['stage_counts'][stage] = summary['stage_counts'].get(stage, 0) + 1

            planned_sets += 1
            plan_step['current'] = planned_sets
            plan_step['detail'] = f'Planned {planned_sets} / {set_count} Sets · Candidates {sum(summary["stage_counts"].values())} Photos'
            emit('plan_assets', plan_step['detail'])

        job['plans'] = plans

    total_assets = sum(len(job['plans']) for job in source_jobs)
    summary['asset_total'] = total_assets
    overall_total = total_assets
    overall_ready = True
    plan_step.update(status='done', current=set_count, total=set_count, detail=f'Candidates: {total_assets} Photos')
    emit('plan_assets', f'Plan complete: {total_assets} Photos', current=0, total=total_assets, percent=0)

    index_step = step_map['index_assets']
    index_step.update(status='active', current=0, total=total_assets, detail='Preparing metadata')

    check_cancelled()
    conn = _connect(smart_db_path)
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('DELETE FROM smart_album_assets')
        indexed_done = 0
        for job in source_jobs:
            source = job['source']
            root = job['root']
            plans = job['plans']

            for offset in range(0, len(plans), SMART_ALBUM_EXIF_BATCH_SIZE):
                check_cancelled()
                batch_plans = plans[offset:offset + SMART_ALBUM_EXIF_BATCH_SIZE]
                metadata_files = []
                for _set_dir, _set_rel, _stage, path, _logical_id_value, donor in batch_plans:
                    metadata_files.append(path)
                    if donor is not None and donor != path:
                        metadata_files.append(donor)

                batch_start = indexed_done + 1 if batch_plans else indexed_done
                batch_end = indexed_done + len(batch_plans)
                index_step['detail'] = (
                    f"{source['name']}: {batch_start}–{batch_end} / {total_assets} Photos"
                    if total_assets else f"{source['name']}: no candidate photos"
                )
                emit('index_assets', index_step['detail'], current=indexed_done, total=total_assets)

                exif_by_path = _run_exiftool_records(
                    exiftool,
                    metadata_files,
                    cancel_callback=cancel_callback,
                ) if exiftool else {}
                check_cancelled()

                for set_dir, set_rel, stage, path, logical_id, donor in batch_plans:
                    check_cancelled()
                    relative_path = path.relative_to(root).as_posix()
                    path_key = str(path.resolve())
                    donor_key = str(donor.resolve()) if donor else None
                    asset_record = exif_by_path.get(path_key, {})
                    capture_record = exif_by_path.get(donor_key, {}) if donor_key else {}
                    capture = _capture_fields(capture_record)
                    width, height, ratio, orientation, mode = _image_facts(path, asset_record)
                    color_space, color_status = _color_space_analysis(asset_record)
                    bit_depth = _bit_depth(asset_record)
                    stat = path.stat()
                    conn.execute(
                        '''INSERT INTO smart_album_assets (
                            photo_id, source_id, relative_path, set_path, set_name,
                            stage, logical_id, file_name, extension, file_size,
                            file_mtime_ns, file_mtime, width, height, aspect_ratio,
                            orientation, image_mode, color_space, color_space_status,
                            bit_depth_json, exif_json, capture_exif_json,
                            capture_donor_relative_path, capture_time, capture_camera,
                            capture_lens, capture_focal_length_mm, capture_iso,
                            capture_gps_lat, capture_gps_lng, indexed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (
                            f"library:{source['id']}:{relative_path}",
                            source['id'], relative_path, set_rel, set_dir.name,
                            stage, logical_id, path.name, path.suffix.lower(), stat.st_size,
                            stat.st_mtime_ns,
                            datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                            width, height, ratio, orientation, mode,
                            color_space, color_status,
                            json.dumps(bit_depth, ensure_ascii=False),
                            json.dumps(asset_record, ensure_ascii=False),
                            json.dumps(capture_record, ensure_ascii=False),
                            donor.relative_to(root).as_posix() if donor else None,
                            capture['time'], capture['camera'], capture['lens'],
                            capture['focal_length_mm'], capture['iso'],
                            capture['gps_lat'], capture['gps_lng'], indexed_at,
                        ),
                    )
                    asset_count += 1
                    indexed_done += 1

                index_step['current'] = indexed_done
                index_step['detail'] = f'Indexed {indexed_done} / {total_assets} Photos'
                emit('index_assets', index_step['detail'], current=indexed_done, total=total_assets)

        index_step.update(status='done', current=total_assets, total=total_assets, detail=f'Indexed: {total_assets} Photos')

        verify_step = step_map['verify']
        verify_step.update(status='active', current=0, total=1, detail='Verifying index')
        emit('verify', 'Verify', current=overall_current, total=overall_total, percent=99 if overall_ready else 0)

        check_cancelled()
        row_count = conn.execute('SELECT COUNT(*) AS count FROM smart_album_assets').fetchone()['count']
        if int(row_count) != int(asset_count):
            raise RuntimeError(f'Smart View index verification failed: wrote {asset_count} Photos, database has {row_count} Photos')

        if unavailable_sources:
            warnings.append('Unavailable Sources: ' + ', '.join(unavailable_sources))
        _meta_set(conn, 'last_refresh_at', indexed_at)
        _meta_set(conn, 'asset_count', asset_count)
        _meta_set(conn, 'set_count', set_count)
        _meta_set(conn, 'exiftool_version', exiftool_version)
        indexed_sources = [job['source'] for job in source_jobs]
        indexed_source_ids = [int(source['id']) for source in indexed_sources]
        indexed_source_names = [source['name'] for source in indexed_sources]
        _meta_set(conn, 'warnings_json', json.dumps(warnings, ensure_ascii=False))
        _meta_set(conn, 'index_policy_signature', _index_policy_signature())
        _meta_set(conn, 'indexed_source_ids_json', json.dumps(indexed_source_ids))
        _meta_set(conn, 'indexed_source_names_json', json.dumps(indexed_source_names, ensure_ascii=False))
        check_cancelled()
        conn.commit()

        verify_step.update(status='done', current=1, total=1, detail=f'Verified: {asset_count} Photos')
    except Exception:
        conn.rollback()
        for step in steps:
            if step['status'] == 'active':
                step['status'] = 'error'
        raise
    finally:
        conn.close()

    emit('done', f'Indexed: {asset_count} Photos', current=asset_count, total=asset_count, percent=100)
    return {
        'last_refresh_at': indexed_at,
        'asset_count': asset_count,
        'set_count': set_count,
        'exiftool_version': exiftool_version,
        'warnings': warnings,
        'index_stages': [stage for stage, _ in _STAGE_DEFS],
        'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
        'indexed_source_ids': indexed_source_ids,
        'indexed_source_names': indexed_source_names,
    }


def _sync_signature_rows(rows):
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _sync_inventory_signature(records):
    rows = []
    for key in sorted(records):
        item = records[key]
        rows.append({
            'source_id': int(item['source_id']),
            'relative_path': item['relative_path'],
            'set_path': item['set_path'],
            'stage': item['stage'],
            'logical_id': item['logical_id'],
            'file_name': item['file_name'],
            'extension': item['extension'],
            'file_size': item['file_size'],
            'file_mtime_ns': item['file_mtime_ns'],
            'capture_donor_relative_path': item.get('capture_donor_relative_path'),
        })
    return _sync_signature_rows(rows)


def _sync_index_signature(rows):
    normalized = []
    for row in rows:
        normalized.append({
            'source_id': int(row['source_id']),
            'relative_path': row['relative_path'],
            'set_path': row['set_path'],
            'stage': row['stage'],
            'logical_id': row['logical_id'],
            'file_name': row['file_name'],
            'extension': row['extension'],
            'file_size': row['file_size'],
            'file_mtime_ns': row['file_mtime_ns'],
            'capture_donor_relative_path': row['capture_donor_relative_path'],
        })
    normalized.sort(key=lambda item: (item['source_id'], item['relative_path'].casefold()))
    return _sync_signature_rows(normalized)


def _indexed_rows_for_sources(smart_db_path, source_ids):
    source_ids = [int(source_id) for source_id in source_ids]
    if not source_ids:
        return []
    placeholders = ','.join('?' for _ in source_ids)
    conn = _connect(smart_db_path)
    rows = conn.execute(
        f"""SELECT * FROM smart_album_assets
            WHERE source_id IN ({placeholders})
            ORDER BY source_id, relative_path""",
        tuple(source_ids),
    ).fetchall()
    conn.close()
    return rows


def _scan_sync_inventory(main_db_path, source_ids):
    # Cheap filesystem snapshot: directory/stat + stage/logical structure only.
    # No ExifTool and no image-pixel decode happens during change discovery.
    if SMART_ALBUM_CAPTURE_METADATA_SOURCE not in _CAPTURE_METADATA_MODES:
        raise RuntimeError(
            'SMART_ALBUM_CAPTURE_METADATA_SOURCE must be one of: ' +
            ', '.join(sorted(_CAPTURE_METADATA_MODES))
        )

    sources = _selected_enabled_sources(main_db_path, source_ids)
    unavailable = [source['name'] for source in sources if not Path(source['root_path']).expanduser().is_dir()]
    if unavailable:
        raise ValueError(
            'Sync cannot scan unavailable Sources: ' +
            ', '.join(unavailable)
        )

    records = {}
    set_total = 0
    stage_counts = {stage: 0 for stage, _ in _STAGE_DEFS}
    source_summaries = []

    for source in sources:
        source_id = int(source['id'])
        root = Path(source['root_path']).expanduser().resolve()
        set_dirs = _discover_sets(root)
        set_total += len(set_dirs)
        source_asset_count = 0

        for set_dir in set_dirs:
            set_rel = set_dir.relative_to(root).as_posix()
            candidates = _set_candidates(set_dir)

            jpg_index = raw_index = {}
            if candidates and SMART_ALBUM_CAPTURE_METADATA_SOURCE != 'asset':
                include_raw = SMART_ALBUM_CAPTURE_METADATA_SOURCE == 'original_jpg_raw'
                _jpg_files, _raw_files, jpg_index, raw_index = _original_indexes(
                    set_dir,
                    include_raw=include_raw,
                )

            for stage, path in candidates:
                logical_id = _logical_id(path)
                donor = path
                if SMART_ALBUM_CAPTURE_METADATA_SOURCE != 'asset':
                    key = logical_id.casefold()
                    donor = _choose_donor(jpg_index.get(key, []), logical_id)
                    if donor is None and SMART_ALBUM_CAPTURE_METADATA_SOURCE == 'original_jpg_raw':
                        donor = _choose_donor(raw_index.get(key, []), logical_id)

                stat = path.stat()
                relative_path = path.relative_to(root).as_posix()
                donor_relative_path = donor.relative_to(root).as_posix() if donor else None
                key = (source_id, relative_path)
                records[key] = {
                    'source_id': source_id,
                    'source_name': source['name'],
                    'root': root,
                    'path': path,
                    'relative_path': relative_path,
                    'set_path': set_rel,
                    'set_name': set_dir.name,
                    'set_dir': set_dir,
                    'stage': stage,
                    'logical_id': logical_id,
                    'file_name': path.name,
                    'extension': path.suffix.lower(),
                    'file_size': stat.st_size,
                    'file_mtime_ns': stat.st_mtime_ns,
                    'file_mtime': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                    'donor': donor,
                    'capture_donor_relative_path': donor_relative_path,
                }
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
                source_asset_count += 1

        source_summaries.append({
            'id': source_id,
            'name': source['name'],
            'set_count': len(set_dirs),
            'asset_count': source_asset_count,
        })

    return {
        'sources': sources,
        'source_summaries': source_summaries,
        'records': records,
        'set_total': set_total,
        'asset_total': len(records),
        'stage_counts': stage_counts,
        'signature': _sync_inventory_signature(records),
    }


def _sync_change_reasons(current, old):
    reasons = []
    if int(current['file_size'] or 0) != int(old['file_size'] or 0):
        reasons.append('Size')
    if int(current['file_mtime_ns'] or 0) != int(old['file_mtime_ns'] or 0):
        reasons.append('Modified')
    if current['stage'] != old['stage']:
        reasons.append('Stage')
    if current['logical_id'] != old['logical_id']:
        reasons.append('logical stem')
    if current['set_path'] != old['set_path']:
        reasons.append('Set')
    if current['file_name'] != old['file_name']:
        reasons.append('Filename')
    if current['extension'] != old['extension']:
        reasons.append('Extension')
    if (current.get('capture_donor_relative_path') or '') != (old['capture_donor_relative_path'] or ''):
        reasons.append('Capture donor')
    # Donor modes currently do not persist donor size/mtime. Be conservative so
    # a changed Original donor can never silently keep stale capture metadata.
    if SMART_ALBUM_CAPTURE_METADATA_SOURCE != 'asset' and 'Original donor metadata' not in reasons:
        reasons.append('Original donor metadata')
    return reasons


def _sync_current_detail(item, reasons=None):
    return {
        'source_id': int(item['source_id']),
        'source_name': item['source_name'],
        'relative_path': item['relative_path'],
        'set_name': item['set_name'],
        'stage': item['stage'],
        'logical_id': item['logical_id'],
        'file_size': item['file_size'],
        'file_mtime': item['file_mtime'],
        'reasons': reasons or [],
    }


def _sync_old_detail(row, source_name=''):
    return {
        'source_id': int(row['source_id']),
        'source_name': source_name,
        'relative_path': row['relative_path'],
        'set_name': row['set_name'],
        'stage': row['stage'],
        'logical_id': row['logical_id'],
        'file_size': row['file_size'],
        'file_mtime': row['file_mtime'],
        'reasons': [],
    }


def _build_sync_plan(smart_db_path, main_db_path, source_ids):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('Rebuild the index before Sync.')

    inventory = _scan_sync_inventory(main_db_path, source_ids)
    source_name_map = {int(source['id']): source['name'] for source in inventory['sources']}
    old_rows = _indexed_rows_for_sources(smart_db_path, source_ids)
    old_map = {(int(row['source_id']), row['relative_path']): row for row in old_rows}
    current_map = inventory['records']

    current_keys = set(current_map)
    old_keys = set(old_map)
    added_keys = sorted(current_keys - old_keys)
    deleted_keys = sorted(old_keys - current_keys)
    changed_keys = []
    unchanged_keys = []
    changed_reason_map = {}

    for key in sorted(current_keys & old_keys):
        reasons = _sync_change_reasons(current_map[key], old_map[key])
        if reasons:
            changed_keys.append(key)
            changed_reason_map[key] = reasons
        else:
            unchanged_keys.append(key)

    added = [_sync_current_detail(current_map[key]) for key in added_keys]
    changed = []
    for key in changed_keys:
        detail = _sync_current_detail(current_map[key], changed_reason_map[key])
        old = old_map[key]
        detail.update({
            'old_file_size': old['file_size'],
            'old_file_mtime': old['file_mtime'],
            'old_stage': old['stage'],
        })
        changed.append(detail)
    deleted = [
        _sync_old_detail(old_map[key], source_name_map.get(int(old_map[key]['source_id']), str(old_map[key]['source_id'])))
        for key in deleted_keys
    ]

    return {
        'plan_id': uuid.uuid4().hex,
        'created_at': _now_iso(),
        'policy_signature': _index_policy_signature(),
        'source_ids': [int(source['id']) for source in inventory['sources']],
        'source_names': [source['name'] for source in inventory['sources']],
        'source_summaries': inventory['source_summaries'],
        'inventory_signature': inventory['signature'],
        'index_signature': _sync_index_signature(old_rows),
        'records': current_map,
        'old_rows': old_map,
        'added_keys': added_keys,
        'changed_keys': changed_keys,
        'deleted_keys': deleted_keys,
        'summary': {
            'source_total': len(inventory['sources']),
            'set_total': inventory['set_total'],
            'asset_total': inventory['asset_total'],
            'added_count': len(added_keys),
            'changed_count': len(changed_keys),
            'deleted_count': len(deleted_keys),
            'unchanged_count': len(unchanged_keys),
            'process_total': len(added_keys) + len(changed_keys),
            'stage_counts': inventory['stage_counts'],
        },
        'changes': {'added': added, 'changed': changed, 'deleted': deleted},
    }


def _public_sync_plan(plan):
    return {
        'plan_id': plan['plan_id'],
        'created_at': plan['created_at'],
        'source_ids': list(plan['source_ids']),
        'source_names': list(plan['source_names']),
        'source_summaries': copy.deepcopy(plan['source_summaries']),
        'summary': copy.deepcopy(plan['summary']),
        'changes': copy.deepcopy(plan['changes']),
    }


def _verify_sync_plan(plan, smart_db_path, main_db_path):
    if plan.get('policy_signature') != _index_policy_signature():
        raise RuntimeError('Smart View index configuration changed. Rebuild the index.')
    fresh = _build_sync_plan(smart_db_path, main_db_path, plan['source_ids'])
    if fresh['inventory_signature'] != plan['inventory_signature']:
        raise RuntimeError('File state changed after the scan. Scan again.')
    if fresh['index_signature'] != plan['index_signature']:
        raise RuntimeError('Smart View index changed after the scan. Scan again.')
    return fresh


def _new_sync_steps():
    return [
        {'id': 'verify_plan', 'label': 'Verify Plan', 'status': 'pending', 'current': 0, 'total': 1, 'unit': 'Items', 'detail': 'Ready'},
        {'id': 'process_assets', 'label': 'Read Metadata', 'status': 'pending', 'current': 0, 'total': 0, 'unit': 'Photos', 'detail': 'Ready'},
        {'id': 'apply_changes', 'label': 'Update Index', 'status': 'pending', 'current': 0, 'total': 1, 'unit': 'Items', 'detail': 'Apply added, changed, and deleted records'},
        {'id': 'verify_commit', 'label': 'Verify & Commit', 'status': 'pending', 'current': 0, 'total': 1, 'unit': 'Items', 'detail': 'Waiting to commit'},
    ]


def _prepare_sync_rows(plan, exiftool, *, progress_callback=None, cancel_callback=None):
    keys = [*plan['added_keys'], *plan['changed_keys']]
    rows = []
    indexed_at = _now_iso()
    total = len(keys)
    done = 0

    for offset in range(0, total, SMART_ALBUM_EXIF_BATCH_SIZE):
        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index sync cancelled')
        batch_keys = keys[offset:offset + SMART_ALBUM_EXIF_BATCH_SIZE]
        batch_items = [plan['records'][key] for key in batch_keys]
        metadata_files = []
        for item in batch_items:
            metadata_files.append(item['path'])
            donor = item.get('donor')
            if donor is not None and donor != item['path']:
                metadata_files.append(donor)

        exif_by_path = _run_exiftool_records(
            exiftool,
            metadata_files,
            cancel_callback=cancel_callback,
        ) if exiftool else {}
        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index sync cancelled')

        for item in batch_items:
            path = item['path']
            donor = item.get('donor')
            path_key = str(path.resolve())
            donor_key = str(donor.resolve()) if donor else None
            asset_record = exif_by_path.get(path_key, {})
            capture_record = exif_by_path.get(donor_key, {}) if donor_key else {}
            capture = _capture_fields(capture_record)
            width, height, ratio, orientation, mode = _image_facts(path, asset_record)
            color_space, color_status = _color_space_analysis(asset_record)
            bit_depth = _bit_depth(asset_record)
            stat = path.stat()
            rows.append({
                'photo_id': f"library:{item['source_id']}:{item['relative_path']}",
                'source_id': item['source_id'], 'relative_path': item['relative_path'],
                'set_path': item['set_path'], 'set_name': item['set_name'],
                'stage': item['stage'], 'logical_id': item['logical_id'],
                'file_name': item['file_name'], 'extension': item['extension'],
                'file_size': stat.st_size, 'file_mtime_ns': stat.st_mtime_ns,
                'file_mtime': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                'width': width, 'height': height, 'aspect_ratio': ratio,
                'orientation': orientation, 'image_mode': mode,
                'color_space': color_space, 'color_space_status': color_status,
                'bit_depth_json': json.dumps(bit_depth, ensure_ascii=False),
                'exif_json': json.dumps(asset_record, ensure_ascii=False),
                'capture_exif_json': json.dumps(capture_record, ensure_ascii=False),
                'capture_donor_relative_path': item.get('capture_donor_relative_path'),
                'capture_time': capture['time'], 'capture_camera': capture['camera'],
                'capture_lens': capture['lens'], 'capture_focal_length_mm': capture['focal_length_mm'],
                'capture_iso': capture['iso'], 'capture_gps_lat': capture['gps_lat'],
                'capture_gps_lng': capture['gps_lng'], 'indexed_at': indexed_at,
            })
            done += 1
        if progress_callback:
            progress_callback(done, total)

    return rows


def _apply_sync_changes(plan, prepared_rows, smart_db_path, main_db_path, exiftool_version, *, cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise SmartAlbumIndexCancelled('Smart View index sync cancelled')

    status_before = _index_status(smart_db_path)
    existing_ids = [int(value) for value in status_before.get('indexed_source_ids') or []]
    existing_names = list(status_before.get('indexed_source_names') or [])
    name_by_id = {
        source_id: existing_names[index]
        for index, source_id in enumerate(existing_ids)
        if index < len(existing_names)
    }
    enabled_name_by_id = {int(source['id']): source['name'] for source in _enabled_sources(main_db_path)}
    for source_id, source_name in zip(plan['source_ids'], plan['source_names']):
        name_by_id[int(source_id)] = source_name
    indexed_ids = list(existing_ids)
    for source_id in plan['source_ids']:
        source_id = int(source_id)
        if source_id not in indexed_ids:
            indexed_ids.append(source_id)
    indexed_names = [name_by_id.get(source_id) or enabled_name_by_id.get(source_id) or str(source_id) for source_id in indexed_ids]

    conn = _connect(smart_db_path)
    conn.execute('BEGIN IMMEDIATE')
    try:
        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index sync cancelled')

        for source_id, relative_path in [*plan['deleted_keys'], *plan['changed_keys']]:
            conn.execute('DELETE FROM smart_album_assets WHERE source_id=? AND relative_path=?', (source_id, relative_path))

        insert_sql = """INSERT INTO smart_album_assets (
            photo_id, source_id, relative_path, set_path, set_name,
            stage, logical_id, file_name, extension, file_size,
            file_mtime_ns, file_mtime, width, height, aspect_ratio,
            orientation, image_mode, color_space, color_space_status,
            bit_depth_json, exif_json, capture_exif_json,
            capture_donor_relative_path, capture_time, capture_camera,
            capture_lens, capture_focal_length_mm, capture_iso,
            capture_gps_lat, capture_gps_lng, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        columns = [
            'photo_id', 'source_id', 'relative_path', 'set_path', 'set_name',
            'stage', 'logical_id', 'file_name', 'extension', 'file_size',
            'file_mtime_ns', 'file_mtime', 'width', 'height', 'aspect_ratio',
            'orientation', 'image_mode', 'color_space', 'color_space_status',
            'bit_depth_json', 'exif_json', 'capture_exif_json',
            'capture_donor_relative_path', 'capture_time', 'capture_camera',
            'capture_lens', 'capture_focal_length_mm', 'capture_iso',
            'capture_gps_lat', 'capture_gps_lng', 'indexed_at',
        ]
        for row in prepared_rows:
            conn.execute(insert_sql, tuple(row[column] for column in columns))

        placeholders = ','.join('?' for _ in plan['source_ids'])
        selected_rows = conn.execute(
            f"""SELECT source_id, relative_path FROM smart_album_assets
                WHERE source_id IN ({placeholders})""",
            tuple(plan['source_ids']),
        ).fetchall()
        selected_keys = {(int(row['source_id']), row['relative_path']) for row in selected_rows}
        if selected_keys != set(plan['records']):
            raise RuntimeError('Sync verification failed: indexed files do not match the scan plan')

        asset_count = int(conn.execute('SELECT COUNT(*) AS count FROM smart_album_assets').fetchone()['count'])
        set_count = int(conn.execute(
            """SELECT COUNT(*) AS count FROM (
                   SELECT DISTINCT source_id, set_path FROM smart_album_assets
               )"""
        ).fetchone()['count'])
        warnings = [warning for warning in (status_before.get('warnings') or []) if 'index configuration changed' not in warning]
        if not exiftool_version:
            warning = 'ExifTool unavailable: EXIF-backed fields will be empty.'
            if warning not in warnings:
                warnings.append(warning)

        now = _now_iso()
        _meta_set(conn, 'last_refresh_at', now)
        _meta_set(conn, 'last_sync_at', now)
        _meta_set(conn, 'asset_count', asset_count)
        _meta_set(conn, 'set_count', set_count)
        _meta_set(conn, 'exiftool_version', exiftool_version)
        _meta_set(conn, 'warnings_json', json.dumps(warnings, ensure_ascii=False))
        _meta_set(conn, 'index_policy_signature', _index_policy_signature())
        _meta_set(conn, 'indexed_source_ids_json', json.dumps(indexed_ids))
        _meta_set(conn, 'indexed_source_names_json', json.dumps(indexed_names, ensure_ascii=False))

        if cancel_callback and cancel_callback():
            raise SmartAlbumIndexCancelled('Smart View index sync cancelled')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _index_status(smart_db_path)


def _index_status(smart_db_path):
    conn = _connect(smart_db_path)
    last_refresh_at = _meta_get(conn, 'last_refresh_at')
    asset_count = int(_meta_get(conn, 'asset_count', '0') or 0)
    set_count = int(_meta_get(conn, 'set_count', '0') or 0)
    exiftool_version = _meta_get(conn, 'exiftool_version')
    stored_policy = _meta_get(conn, 'index_policy_signature')
    current_policy = _index_policy_signature()
    try:
        warnings = json.loads(_meta_get(conn, 'warnings_json', '[]') or '[]')
    except json.JSONDecodeError:
        warnings = []
    try:
        indexed_source_ids = [int(value) for value in json.loads(_meta_get(conn, 'indexed_source_ids_json', '[]') or '[]')]
    except (json.JSONDecodeError, TypeError, ValueError):
        indexed_source_ids = []
    try:
        indexed_source_names = [str(value) for value in json.loads(_meta_get(conn, 'indexed_source_names_json', '[]') or '[]')]
    except (json.JSONDecodeError, TypeError, ValueError):
        indexed_source_names = []
    conn.close()
    policy_matches = stored_policy == current_policy
    if last_refresh_at and not policy_matches:
        warnings = [*warnings, 'Smart View index configuration changed. Rebuild the index.']
    return {
        'ready': bool(last_refresh_at) and policy_matches,
        'last_refresh_at': last_refresh_at or None,
        'asset_count': asset_count,
        'set_count': set_count,
        'exiftool_version': exiftool_version,
        'warnings': warnings,
        'index_stages': [stage for stage, _ in _STAGE_DEFS],
        'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
        'indexed_source_ids': indexed_source_ids,
        'indexed_source_names': indexed_source_names,
    }


def _asset_payloads(smart_db_path, main_db_path):
    # Exclude enabled Sources that are currently unmounted so queries never
    # surface broken paths from disconnected drives.
    sources = [
        source for source in _enabled_sources(main_db_path)
        if Path(source['root_path']).expanduser().is_dir()
    ]
    source_map = {source['id']: source for source in sources}
    source_ids = list(source_map)
    if not source_ids:
        return [], {}
    states = _library_states(main_db_path, source_ids)

    conn = _connect(smart_db_path)
    placeholders = ','.join('?' for _ in source_ids)
    rows = conn.execute(
        f'''SELECT * FROM smart_album_assets
            WHERE source_id IN ({placeholders})
            ORDER BY photo_id''',
        tuple(source_ids),
    ).fetchall()
    conn.close()

    manifest_cache = {}
    payloads = []
    result_rows = {}
    for row in rows:
        source = source_map.get(row['source_id'])
        if not source:
            continue
        manifest_key = (row['source_id'], row['set_path'])
        if manifest_key not in manifest_cache:
            manifest_cache[manifest_key] = _read_manifest(source['root_path'], row['set_path'])
        state = states.get((row['source_id'], row['relative_path']), {'favorite': False, 'description': ''})
        try:
            exif = json.loads(row['exif_json'] or '{}')
        except json.JSONDecodeError:
            exif = {}
        try:
            capture_exif = json.loads(row['capture_exif_json'] or '{}')
        except json.JSONDecodeError:
            capture_exif = {}
        try:
            bit_depth = json.loads(row['bit_depth_json']) if row['bit_depth_json'] is not None else None
        except json.JSONDecodeError:
            bit_depth = None

        # Query execution stays index-backed. Refresh Index is the explicit
        # boundary for filesystem-derived fields; do not re-stat every asset on
        # the external volume for each Python query.
        actual_path = Path(source['root_path']).expanduser() / row['relative_path']
        file_size = row['file_size']
        file_mtime = row['file_mtime']
        absolute_path = str(actual_path)
        gps = None
        if row['capture_gps_lat'] is not None and row['capture_gps_lng'] is not None:
            gps = {'lat': row['capture_gps_lat'], 'lng': row['capture_gps_lng']}

        payload = {
            'id': row['photo_id'],
            'origin': {'kind': 'library'},
            'source': {'id': row['source_id'], 'name': source['name']},
            'file': {
                'name': row['file_name'],
                'path': absolute_path,
                'extension': row['extension'],
                'size': file_size,
                'mtime': {'__smart_datetime__': file_mtime},
            },
            'image': {
                'width': row['width'],
                'height': row['height'],
                'aspect_ratio': row['aspect_ratio'],
                'orientation': row['orientation'],
                'mode': row['image_mode'],
                'color_space': row['color_space'],
                'color_space_status': row['color_space_status'],
                'bit_depth': bit_depth,
            },
            'stage': row['stage'],
            'logical_id': row['logical_id'],
            'state': {
                'favorite': state['favorite'],
                'description': state['description'],
            },
            'exif': {'__smart_exif__': exif},
            'capture': {
                'exif': {'__smart_exif__': capture_exif},
                'time': {'__smart_datetime__': row['capture_time']} if row['capture_time'] else None,
                'camera': row['capture_camera'],
                'lens': row['capture_lens'],
                'focal_length_mm': row['capture_focal_length_mm'],
                'iso': row['capture_iso'],
                'gps': gps,
            },
            'set': {
                'name': row['set_name'],
                'path': row['set_path'],
                'manifest': manifest_cache[manifest_key],
            },
        }
        payloads.append(payload)
        capture_sort_time = _smart_album_capture_sort_time(
            manifest_cache[manifest_key], row['capture_time'], row['set_name']
        )
        result_rows[row['photo_id']] = {
            'source_type': 'library',
            'source_id': row['source_id'],
            'id': row['photo_id'],
            'relative_path': row['relative_path'],
            'original_filename': row['file_name'],
            'file_size': file_size,
            'width': row['width'],
            'height': row['height'],
            'uploaded_at': file_mtime,
            'modified_at': file_mtime,
            'capture_sort_time': capture_sort_time,
            'is_favorited': state['favorite'],
            'description': state['description'],
            'stage': row['stage'],
            'logical_id': row['logical_id'],
            'source_name': source['name'],
            'set_name': row['set_name'],
            'set_path': row['set_path'],
            'color_space': row['color_space'],
            'color_space_status': row['color_space_status'],
            'bit_depth': bit_depth,
        }
    return payloads, result_rows


def _source_scope(main_db_path, smart_db_path):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')
    indexed_ids = {int(value) for value in status.get('indexed_source_ids') or []}
    sources = []
    for source in _enabled_sources(main_db_path):
        if int(source['id']) not in indexed_ids:
            continue
        if not Path(source['root_path']).expanduser().is_dir():
            continue
        sources.append(source)
    return sources, status


def _manifest_info(root, set_path):
    manifest_path = Path(root) / set_path / 'manifest.json'
    if not manifest_path.is_file():
        return {}, False, True
    try:
        raw = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return {}, True, False
        return raw, True, True
    except Exception:
        return {}, True, False


def _shoot_date(manifest, set_name):
    shoot = manifest.get('shoot') if isinstance(manifest.get('shoot'), dict) else {}
    date_text = str(shoot.get('date') or '').strip()
    if date_text:
        return date_text
    prefix = str(set_name or '')[:8]
    if len(prefix) == 8 and prefix.isdigit():
        return f'{prefix[:4]}-{prefix[4:6]}-{prefix[6:8]}'
    return ''


def _favorite_paths(main_db_path, source_ids):
    if not source_ids:
        return []
    placeholders = ','.join('?' for _ in source_ids)
    conn = _connect(main_db_path)
    rows = conn.execute(
        f'''SELECT source_id, relative_path
            FROM library_image_states
            WHERE is_favorited=1 AND source_id IN ({placeholders})''',
        tuple(source_ids),
    ).fetchall()
    conn.close()
    return [(int(row['source_id']), str(row['relative_path'])) for row in rows]


def _stage_counts(set_dir):
    counts = {
        'original_jpg': len(_walk_files(set_dir / '01_Original' / 'JPG', _DISPLAY_IMAGE_EXTENSIONS)),
        'original_raw': len(_walk_files(set_dir / '01_Original' / 'RAW', _RAW_EXTENSIONS)),
    }
    for stage, relative_dir in SMART_ALBUM_INDEX_STAGE_DEFS:
        counts[stage] = len(_walk_files(set_dir / relative_dir, _DISPLAY_IMAGE_EXTENSIONS))
    return counts


def set_candidates(smart_db_path, main_db_path, photo_payloads=None):
    sources, status = _source_scope(main_db_path, smart_db_path)
    source_map = {int(source['id']): source for source in sources}

    if photo_payloads is None:
        photo_payloads, _ = _asset_payloads(smart_db_path, main_db_path)
    photos_by_set = {}
    for photo in photo_payloads:
        source = photo.get('source') or {}
        set_info = photo.get('set') or {}
        key = (int(source.get('id')), str(set_info.get('path') or ''))
        if key[0] in source_map:
            photos_by_set.setdefault(key, []).append(photo)

    discovered = []
    set_paths_by_source = {}
    for source in sources:
        source_id = int(source['id'])
        root = Path(source['root_path']).expanduser().resolve()
        for set_dir in _discover_sets(root):
            set_path = set_dir.relative_to(root).as_posix()
            discovered.append((source, root, set_dir, set_path))
            set_paths_by_source.setdefault(source_id, []).append(set_path)

    favorite_counts = {(int(source['id']), path): 0 for source in sources for path in set_paths_by_source.get(int(source['id']), [])}
    favorite_rows = _favorite_paths(main_db_path, list(source_map))
    sorted_paths = {
        source_id: sorted(paths, key=lambda value: len(value), reverse=True)
        for source_id, paths in set_paths_by_source.items()
    }
    for source_id, relative_path in favorite_rows:
        source = source_map.get(source_id)
        if not source or not (Path(source['root_path']).expanduser() / relative_path).is_file():
            continue
        for set_path in sorted_paths.get(source_id, []):
            if relative_path == set_path or relative_path.startswith(set_path + '/'):
                favorite_counts[(source_id, set_path)] = favorite_counts.get((source_id, set_path), 0) + 1
                break

    payloads = []
    rows = {}
    for source, root, set_dir, set_path in discovered:
        source_id = int(source['id'])
        manifest, has_manifest, manifest_valid = _manifest_info(root, set_path)
        counts = _stage_counts(set_dir)
        key = (source_id, set_path)
        set_id = f'library-set:{source_id}:{set_path}'
        photo_items = photos_by_set.get(key, [])
        shoot_date = _shoot_date(manifest, set_dir.name)
        payload = {
            'id': set_id,
            'source': {'id': source_id, 'name': source['name']},
            'name': set_dir.name,
            'path': set_path,
            'manifest': manifest,
            'counts': counts,
            'favorite_count': favorite_counts.get(key, 0),
            'indexed_photo_count': len(photo_items),
            'photos': photo_items,
            'shoot_date': {'__smart_date__': shoot_date} if shoot_date else None,
        }
        payloads.append(payload)
        rows[set_id] = {
            'id': set_id,
            'source_id': source_id,
            'source_name': source['name'],
            'name': set_dir.name,
            'set_path': set_path,
            'shoot_date': shoot_date,
            'model': str(manifest.get('model') or '').strip(),
            'has_manifest': has_manifest,
            'manifest_valid': manifest_valid,
            'counts': counts,
            'favorite_count': favorite_counts.get(key, 0),
            'indexed_photo_count': len(photo_items),
        }
    return payloads, rows, status


# Public shared Smart data/index boundary.
connect = _connect
init_smart_db = _init_smart_db
index_status = _index_status
asset_payloads = _asset_payloads
enabled_sources = _enabled_sources
discover_sets = _discover_sets
walk_files = _walk_files
now_iso = _now_iso
source_scope = _source_scope
manifest_info = _manifest_info
shoot_date = _shoot_date
favorite_paths = _favorite_paths
stage_counts = _stage_counts

