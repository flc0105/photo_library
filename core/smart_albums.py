import copy
import json
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image
from flask import Blueprint, jsonify, request

from core.external_tools import probe_exiftool_version, resolve_exiftool
from core.original_naming import original_stem_key
from core.smart_album_runtime import run_query


SMART_ALBUM_DB_FILENAME = 'smart_albums.db'
SMART_ALBUM_ENGINE_VERSION = 1
SMART_ALBUM_QUERY_TIMEOUT_SECONDS = 10
SMART_ALBUM_EXIF_BATCH_SIZE = 25  # Smaller batches keep index progress visibly granular without changing query semantics.

# --- Smart Album indexing policy -------------------------------------------------
# v1 intentionally keeps 01_Original out of the index. The archive can contain
# very large Original JPG/RAW collections and Smart Album is currently focused on
# edited/final assets. To make Original JPG queryable later, add this tuple to
# SMART_ALBUM_INDEX_STAGE_DEFS:
#     ('original_jpg', '01_Original/JPG'),
SMART_ALBUM_INDEX_STAGE_DEFS = (
    ('base_edit', '02_Base_Edit'),
    ('model_edit', '03_Model_Edit'),
    ('revision', '04_Revision'),
    ('final', '05_Final'),
)

# Capture metadata policy. Keep this at 'asset' to avoid touching 01_Original at
# all: photo.capture.* is then derived from the indexed asset's own EXIF.
# Later, if trusted Original donor metadata is worth the extra I/O, change to:
#     'original_jpg'      -> matching 01_Original/JPG donor only
#     'original_jpg_raw'  -> JPG donor first, RAW fallback
SMART_ALBUM_CAPTURE_METADATA_SOURCE = 'asset'

_SET_FOLDER_RE = re.compile(r'^\d{8}-.+-.+$')
_DISPLAY_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
_RAW_EXTENSIONS = {'.cr3', '.cr2', '.dng', '.nef', '.arw', '.raf', '.rw2', '.orf'}
_STAGE_DEFS = SMART_ALBUM_INDEX_STAGE_DEFS
_SKIP_DIR_NAMES = {'deleted', 'discards', 'intermediates'}
_CAPTURE_METADATA_MODES = {'asset', 'original_jpg', 'original_jpg_raw'}

DEFAULT_QUERY_CODE = """# `photos` contains all indexed photos from enabled Library Sources.\n# Return Photo objects through `result`.\nresult = list(photos)\n"""


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
            engine_version INTEGER NOT NULL DEFAULT 1,
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


def _run_exiftool_records(exiftool_path, files, *, progress_callback=None):
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
            raise RuntimeError(f'Smart Album ExifTool 扫描失败：{detail}')
        batch = json.loads(result.stdout)
        if not isinstance(batch, list):
            raise RuntimeError('Smart Album ExifTool 返回格式异常')
        for record in batch:
            source = record.get('SourceFile')
            if source:
                records_by_path[str(Path(source).resolve())] = record
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
            'label': '扫描 Source / Set',
            'status': 'pending',
            'current': 0,
            'total': 0,
            'unit': 'Source',
            'detail': '等待开始',
        },
        {
            'id': 'plan_assets',
            'label': '统计候选图片',
            'status': 'pending',
            'current': 0,
            'total': 0,
            'unit': 'Set',
            'detail': '等待 Set 扫描完成',
        },
        {
            'id': 'index_assets',
            'label': '读取图片 Metadata / EXIF 并写入索引',
            'status': 'pending',
            'current': 0,
            'total': 0,
            'unit': '图片',
            'detail': '包含尺寸、方向、Color Space、Bit Depth 与 capture metadata',
        },
        {
            'id': 'verify',
            'label': '完成校验',
            'status': 'pending',
            'current': 0,
            'total': 1,
            'unit': '项',
            'detail': '等待索引写入完成',
        },
    ]


def _refresh_index(smart_db_path, main_db_path, *, progress_callback=None):
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
            # Keep legacy top-level fields for the existing API shape, but from
            # planning onward current/total always mean indexed images.
            'percent': max(0, min(100, int(round(percent)))),
            'phase': phase,
            'message': message,
            'current': overall_current,
            'total': overall_total,
            'overall': {
                'dimension': 'image',
                'label': '图片总进度',
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

    sources = _enabled_sources(main_db_path)
    summary['source_total'] = len(sources)
    exiftool = resolve_exiftool()
    exiftool_version = probe_exiftool_version(exiftool) if exiftool else ''
    warnings = []
    if not exiftool:
        warnings.append('ExifTool 不可用：photo.exif / photo.capture.exif 与依赖 EXIF 的字段会为空。')

    conn = _connect(smart_db_path)
    indexed_at = _now_iso()
    set_count = 0
    asset_count = 0
    unavailable_sources = []
    source_jobs = []

    # First discover projects (Sets), then fix the image denominator. The overall
    # bar never switches to Source/Set counts.
    discover_step = step_map['discover_sets']
    discover_step.update(status='active', current=0, total=len(sources), detail='开始扫描 enabled Library Sources')
    emit('discover_sets', '扫描 Source / Set')

    for source_index, source in enumerate(sources, start=1):
        root = Path(source['root_path']).expanduser().resolve()
        if not root.is_dir():
            unavailable_sources.append(source['name'])
            discover_step['current'] = source_index
            discover_step['detail'] = f"跳过不可用 Source：{source['name']}"
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
        discover_step['detail'] = f"{source['name']}：发现 {len(set_dirs)} 个 Set；累计 {set_count} 个 Set"
        emit('discover_sets', discover_step['detail'])

    summary['set_total'] = set_count
    discover_step.update(
        status='done',
        current=len(sources),
        total=len(sources),
        detail=f'扫描完成：{summary["source_available"]} 个可用 Source，{set_count} 个 Set',
    )

    plan_step = step_map['plan_assets']
    plan_step.update(status='active', current=0, total=set_count, detail='统计 Base / Model / Revision / Final 候选图片')
    emit('plan_assets', '统计候选图片')
    planned_sets = 0

    for job in source_jobs:
        root = job['root']
        plans = []
        for set_dir in job['set_dirs']:
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
            plan_step['detail'] = f'已统计 {planned_sets} / {set_count} 个 Set；候选图片 {sum(summary["stage_counts"].values())} 张'
            emit('plan_assets', plan_step['detail'])

        job['plans'] = plans

    total_assets = sum(len(job['plans']) for job in source_jobs)
    summary['asset_total'] = total_assets
    overall_total = total_assets
    overall_ready = True
    plan_step.update(status='done', current=set_count, total=set_count, detail=f'候选图片共 {total_assets} 张')
    emit('plan_assets', f'候选图片统计完成：{total_assets} 张', current=0, total=total_assets, percent=0)

    index_step = step_map['index_assets']
    index_step.update(status='active', current=0, total=total_assets, detail='准备读取图片 Metadata / EXIF')

    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('DELETE FROM smart_album_assets')
        indexed_done = 0
        for job in source_jobs:
            source = job['source']
            root = job['root']
            plans = job['plans']

            for offset in range(0, len(plans), SMART_ALBUM_EXIF_BATCH_SIZE):
                batch_plans = plans[offset:offset + SMART_ALBUM_EXIF_BATCH_SIZE]
                metadata_files = []
                for _set_dir, _set_rel, _stage, path, _logical_id_value, donor in batch_plans:
                    metadata_files.append(path)
                    if donor is not None and donor != path:
                        metadata_files.append(donor)

                batch_start = indexed_done + 1 if batch_plans else indexed_done
                batch_end = indexed_done + len(batch_plans)
                index_step['detail'] = (
                    f"{source['name']}：读取第 {batch_start}–{batch_end} / {total_assets} 张图片的 Metadata / EXIF"
                    if total_assets else f"{source['name']}：没有候选图片"
                )
                emit('index_assets', index_step['detail'], current=indexed_done, total=total_assets)

                exif_by_path = _run_exiftool_records(exiftool, metadata_files) if exiftool else {}

                for set_dir, set_rel, stage, path, logical_id, donor in batch_plans:
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
                index_step['detail'] = f'已完成 {indexed_done} / {total_assets} 张图片'
                emit('index_assets', index_step['detail'], current=indexed_done, total=total_assets)

        index_step.update(status='done', current=total_assets, total=total_assets, detail=f'图片索引完成：{total_assets} 张')

        verify_step = step_map['verify']
        verify_step.update(status='active', current=0, total=1, detail='校验写入数量与索引配置')
        emit('verify', '完成校验', current=overall_current, total=overall_total, percent=99 if overall_ready else 0)

        row_count = conn.execute('SELECT COUNT(*) AS count FROM smart_album_assets').fetchone()['count']
        if int(row_count) != int(asset_count):
            raise RuntimeError(f'Smart Album 索引校验失败：写入 {asset_count} 张，但数据库中为 {row_count} 张')

        if unavailable_sources:
            warnings.append('不可用 Source：' + ', '.join(unavailable_sources))
        _meta_set(conn, 'last_refresh_at', indexed_at)
        _meta_set(conn, 'asset_count', asset_count)
        _meta_set(conn, 'set_count', set_count)
        _meta_set(conn, 'exiftool_version', exiftool_version)
        _meta_set(conn, 'warnings_json', json.dumps(warnings, ensure_ascii=False))
        _meta_set(conn, 'index_policy_signature', _index_policy_signature())
        conn.commit()

        verify_step.update(status='done', current=1, total=1, detail=f'校验通过：{asset_count} 张图片')
    except Exception:
        conn.rollback()
        for step in steps:
            if step['status'] == 'active':
                step['status'] = 'error'
        raise
    finally:
        conn.close()

    emit('done', f'Smart Album 索引完成：{asset_count} 张图片', current=asset_count, total=asset_count, percent=100)
    return {
        'last_refresh_at': indexed_at,
        'asset_count': asset_count,
        'set_count': set_count,
        'exiftool_version': exiftool_version,
        'warnings': warnings,
        'index_stages': [stage for stage, _ in _STAGE_DEFS],
        'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
    }

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
    conn.close()
    policy_matches = stored_policy == current_policy
    if last_refresh_at and not policy_matches:
        warnings = [*warnings, 'Smart Album 索引配置已变化，请重新刷新索引。']
    return {
        'ready': bool(last_refresh_at) and policy_matches,
        'last_refresh_at': last_refresh_at or None,
        'asset_count': asset_count,
        'set_count': set_count,
        'exiftool_version': exiftool_version,
        'warnings': warnings,
        'index_stages': [stage for stage, _ in _STAGE_DEFS],
        'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
    }


def _asset_payloads(smart_db_path, main_db_path):
    # Enabled-but-unmounted Sources are excluded at query time so an old index
    # never surfaces broken files after a removable drive is disconnected.
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


def _album_row(conn, album_id):
    return conn.execute('SELECT * FROM smart_albums WHERE id=?', (album_id,)).fetchone()


def _album_dict(row):
    if not row:
        return None
    data = dict(row)
    data['type'] = 'smart'
    return data


def _run_album_query(smart_db_path, main_db_path, album_row):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')

    payloads, result_rows = _asset_payloads(smart_db_path, main_db_path)
    ordered_ids = run_query(
        album_row['python_code'],
        payloads,
        timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    )
    results = []
    for photo_id in ordered_ids:
        row = result_rows.get(photo_id)
        if row:
            row = dict(row)
            row['smart_album_id'] = album_row['id']
            results.append(row)

    conn = _connect(smart_db_path)
    conn.execute(
        'UPDATE smart_albums SET last_result_count=?, last_run_at=? WHERE id=?',
        (len(results), _now_iso(), album_row['id']),
    )
    conn.commit()
    conn.close()
    return results, _index_status(smart_db_path)


def create_smart_album_blueprint(admin_guard, main_db_path):
    """Create the removable Smart Album module.

    Smart Album definitions and the derived index live in a dedicated SQLite
    database beside the main Gallery DB. The main DB remains the source for
    Library Source configuration and live favorite/description state.
    """
    main_db_path = Path(main_db_path).resolve()
    smart_db_path = main_db_path.parent / SMART_ALBUM_DB_FILENAME
    _init_smart_db(smart_db_path)
    bp = Blueprint('smart_albums', __name__)

    # Index refresh is intentionally isolated inside the Smart Album module.
    # Progress is process-local because this Gallery is a personal/local app;
    # restarting the app simply clears the transient progress state, not the index.
    index_job_lock = threading.Lock()
    index_job = {
        'active': False,
        'percent': 0,
        'phase': 'idle',
        'message': '',
        'current': 0,
        'total': 0,
        'overall': {'dimension': 'image', 'label': '图片总进度', 'current': 0, 'total': 0, 'percent': 0, 'ready': False},
        'steps': _new_index_steps(),
        'summary': {
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
        },
        'error': '',
        'index': None,
    }

    def index_job_snapshot():
        with index_job_lock:
            return copy.deepcopy(index_job)

    def update_index_job(**changes):
        with index_job_lock:
            index_job.update(changes)

    def run_index_refresh_job():
        try:
            status = _refresh_index(
                smart_db_path,
                main_db_path,
                progress_callback=lambda progress: update_index_job(**progress),
            )
            snapshot = index_job_snapshot()
            overall = dict(snapshot.get('overall') or {})
            overall.update({'current': status['asset_count'], 'total': status['asset_count'], 'percent': 100, 'ready': True})
            update_index_job(
                active=False,
                percent=100,
                phase='done',
                message=f"Smart Album 索引完成：{status['asset_count']} 张图片",
                current=status['asset_count'],
                total=status['asset_count'],
                overall=overall,
                error='',
                index={**status, 'ready': True},
            )
        except Exception as exc:
            snapshot = index_job_snapshot()
            failed_steps = copy.deepcopy(snapshot.get('steps') or [])
            for step in failed_steps:
                if step.get('status') == 'active':
                    step['status'] = 'error'
            update_index_job(
                active=False,
                phase='error',
                message='Smart Album 索引刷新失败',
                steps=failed_steps,
                error=str(exc),
                index=_index_status(smart_db_path),
            )

    def guard():
        return admin_guard()

    @bp.route('/api/smart-albums', methods=['GET'])
    def list_smart_albums():
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        rows = conn.execute('SELECT * FROM smart_albums ORDER BY created_at DESC, id DESC').fetchall()
        conn.close()
        return jsonify({'albums': [_album_dict(row) for row in rows], 'index': _index_status(smart_db_path)})

    @bp.route('/api/smart-albums', methods=['POST'])
    def create_smart_album():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '')
        python_code = str(data.get('python_code') or DEFAULT_QUERY_CODE)
        if not name:
            return jsonify({'error': '相册名称不能为空'}), 400
        # Parse/validate before saving. Execution happens when the album runs.
        try:
            from core.smart_album_runtime import _validate_script
            _validate_script(python_code)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400
        now = _now_iso()
        conn = _connect(smart_db_path)
        cursor = conn.execute(
            '''INSERT INTO smart_albums
               (name, description, python_code, engine_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (name, description, python_code, SMART_ALBUM_ENGINE_VERSION, now, now),
        )
        album_id = cursor.lastrowid
        conn.commit()
        row = _album_row(conn, album_id)
        conn.close()
        return jsonify({'album': _album_dict(row)}), 201

    @bp.route('/api/smart-albums/<int:album_id>', methods=['GET'])
    def get_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _album_row(conn, album_id)
        conn.close()
        if not row:
            return jsonify({'error': 'Smart Album 不存在'}), 404
        return jsonify({'album': _album_dict(row), 'index': _index_status(smart_db_path)})

    @bp.route('/api/smart-albums/<int:album_id>', methods=['PUT'])
    def update_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        conn = _connect(smart_db_path)
        existing = _album_row(conn, album_id)
        if not existing:
            conn.close()
            return jsonify({'error': 'Smart Album 不存在'}), 404
        name = str(data.get('name', existing['name']) or '').strip()
        description = str(data.get('description', existing['description']) or '')
        python_code = str(data.get('python_code', existing['python_code']) or '')
        if not name:
            conn.close()
            return jsonify({'error': '相册名称不能为空'}), 400
        try:
            from core.smart_album_runtime import _validate_script
            _validate_script(python_code)
        except Exception as exc:
            conn.close()
            return jsonify({'error': str(exc)}), 400
        conn.execute(
            '''UPDATE smart_albums
               SET name=?, description=?, python_code=?, engine_version=?, updated_at=?
               WHERE id=?''',
            (name, description, python_code, SMART_ALBUM_ENGINE_VERSION, _now_iso(), album_id),
        )
        conn.commit()
        row = _album_row(conn, album_id)
        conn.close()
        return jsonify({'album': _album_dict(row)})

    @bp.route('/api/smart-albums/<int:album_id>', methods=['DELETE'])
    def delete_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _album_row(conn, album_id)
        if not row:
            conn.close()
            return jsonify({'error': 'Smart Album 不存在'}), 404
        conn.execute('DELETE FROM smart_albums WHERE id=?', (album_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @bp.route('/api/smart-albums/<int:album_id>/query', methods=['POST'])
    def query_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _album_row(conn, album_id)
        conn.close()
        if not row:
            return jsonify({'error': 'Smart Album 不存在'}), 404
        try:
            results, status = _run_album_query(smart_db_path, main_db_path, row)
            conn = _connect(smart_db_path)
            fresh_row = _album_row(conn, album_id)
            conn.close()
            return jsonify({
                'album': _album_dict(fresh_row),
                'images': results,
                'count': len(results),
                'index': status,
            })
        except Exception as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Smart Album 索引尚未建立，请先点击“刷新索引”。',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                    'traceback': '',
                }), 409
            return jsonify({
                'error': str(exc),
                'traceback': getattr(exc, 'smart_traceback', ''),
            }), 400

    @bp.route('/api/smart-albums/index/status', methods=['GET'])
    def smart_album_index_status():
        denied = guard()
        if denied:
            return denied
        return jsonify(_index_status(smart_db_path))

    @bp.route('/api/smart-albums/index/refresh', methods=['POST'])
    def refresh_smart_album_index():
        denied = guard()
        if denied:
            return denied
        with index_job_lock:
            if index_job['active']:
                return jsonify(dict(index_job)), 202
            index_job.update({
                'active': True,
                'percent': 0,
                'phase': 'starting',
                'message': '准备刷新 Smart Album 索引',
                'current': 0,
                'total': 0,
                'overall': {'dimension': 'image', 'label': '图片总进度', 'current': 0, 'total': 0, 'percent': 0, 'ready': False},
                'steps': _new_index_steps(),
                'summary': {
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
                },
                'error': '',
                'index': _index_status(smart_db_path),
            })
        threading.Thread(target=run_index_refresh_job, daemon=True).start()
        return jsonify(index_job_snapshot()), 202

    @bp.route('/api/smart-albums/index/progress', methods=['GET'])
    def smart_album_index_progress():
        denied = guard()
        if denied:
            return denied
        return jsonify(index_job_snapshot())

    @bp.route('/api/smart-albums/runtime', methods=['GET'])
    def smart_album_runtime_contract():
        denied = guard()
        if denied:
            return denied
        return jsonify({
            'engine_version': SMART_ALBUM_ENGINE_VERSION,
            'default_code': DEFAULT_QUERY_CODE,
            'provider': 'library',
            'notes': [
                'photos contains indexed photos from currently enabled Library Sources.',
                'result must be a Photo object or an iterable of Photo objects.',
                'import/file/process/network/database mutation capabilities are not exposed.',
                f"Indexed stages: {', '.join(stage for stage, _ in _STAGE_DEFS)}.",
                f"photo.capture.* metadata source: {SMART_ALBUM_CAPTURE_METADATA_SOURCE}.",
                'Edit the Smart Album indexing-policy constants near the top of core/smart_albums.py to enable Original indexing/donors later.',
            ],
            'helpers': [
                "preferred_versions(items, stage_order=('revision', 'model_edit', 'base_edit'))",
                'logical_photo_key(photo)',
            ],
            'photo_contract': [
                'photo.id',
                'photo.origin.kind',
                'photo.source.id', 'photo.source.name',
                'photo.file.name', 'photo.file.path', 'photo.file.extension', 'photo.file.size', 'photo.file.mtime',
                'photo.image.width', 'photo.image.height', 'photo.image.aspect_ratio', 'photo.image.orientation',
                'photo.image.mode', 'photo.image.color_space', 'photo.image.color_space_status', 'photo.image.bit_depth',
                'photo.stage', 'photo.logical_id',
                'photo.state.favorite', 'photo.state.description',
                'photo.exif', 'photo.capture.exif',
                'photo.capture.time', 'photo.capture.camera', 'photo.capture.lens',
                'photo.capture.focal_length_mm', 'photo.capture.iso', 'photo.capture.gps',
                'photo.set.name', 'photo.set.path', 'photo.set.manifest',
            ],
        })

    return bp
