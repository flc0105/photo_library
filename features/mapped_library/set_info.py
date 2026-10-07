import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image
from flask import Blueprint, jsonify, request

from core.external_tools import resolve_exiftool

_SET_RE = re.compile(r'^\d{8}-.+-.+$')
_BASE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}
_STAGE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}
_ORIGINAL_JPG_EXTENSIONS = {'.jpg', '.jpeg'}
_RAW_EXTENSIONS = {'.cr3'}
_IGNORED_CONTROL_NAMES = {'manifest.json', 'manifest.json.bak', 'manifest.json.tmp'}

def _is_hidden_or_control(name):
    return name.startswith('.') or name in _IGNORED_CONTROL_NAMES


def _iter_files(root: Path, extensions=None, skip_dir_names=None):
    if not root.is_dir():
        return []
    extensions = {ext.lower() for ext in extensions} if extensions else None
    skip_dir_names = {name.casefold() for name in (skip_dir_names or set())}
    result = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [
            name for name in dir_names
            if not name.startswith('.') and name.casefold() not in skip_dir_names
        ]
        current = Path(current_root)
        for name in file_names:
            if _is_hidden_or_control(name):
                continue
            path = current / name
            if extensions is not None and path.suffix.lower() not in extensions:
                continue
            result.append(path)
    return sorted(result, key=lambda path: str(path).casefold())


def _equipment_files(base_dir: Path):
    return _iter_files(base_dir, _BASE_IMAGE_EXTENSIONS)


def _equipment_signature(base_dir: Path, files):
    digest = hashlib.sha256()
    digest.update(str(base_dir).encode('utf-8', errors='surrogatepass'))
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(base_dir).as_posix()
        digest.update(rel.encode('utf-8', errors='surrogatepass'))
        digest.update(b'\0')
        digest.update(str(int(stat.st_size)).encode())
        digest.update(b'\0')
        digest.update(str(int(stat.st_mtime_ns)).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def _normalize_text(value):
    if value is None:
        return ''
    return str(value).strip()


def _normalize_focal(value):
    if value is None or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value).strip().lower().replace('mm', '').strip()
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
    if not (number > 0):
        return None
    # Camera EXIF often carries rational noise. 0.1 mm keeps genuine values like
    # 35.5 while avoiding 34.999999-style duplicates.
    return round(number, 1)


def _summarize_equipment_records(records, file_count, method):
    cameras = set()
    lenses = set()
    focals = set()
    metadata_files = 0

    for record in records:
        camera = _normalize_text(record.get('CameraModelName') or record.get('Model'))
        lens = _normalize_text(record.get('LensModel'))
        focal = _normalize_focal(record.get('FocalLength'))
        if camera or lens or focal is not None:
            metadata_files += 1
        if camera:
            cameras.add(camera)
        if lens:
            lenses.add(lens)
        if focal is not None:
            focals.add(focal)

    def focal_sort_key(value):
        return float(value)

    focal_lengths = sorted(focals, key=focal_sort_key)
    return {
        'cameras': sorted(cameras, key=str.casefold),
        'lenses': sorted(lenses, key=str.casefold),
        'focal_lengths': focal_lengths,
        'file_count': int(file_count),
        'metadata_file_count': int(metadata_files),
        'method': method,
        'source': '02_Base_Edit',
    }


def _scan_equipment_exiftool(base_dir: Path, file_count: int):
    exiftool = resolve_exiftool()
    if not exiftool:
        raise FileNotFoundError('exiftool not found')

    command = [
        exiftool,
        '-j',
        '-n',
        '-r',
        '-ext', 'jpg',
        '-ext', 'jpeg',
        '-ext', 'png',
        '-ext', 'tif',
        '-ext', 'tiff',
        '-ext', 'webp',
        '-CameraModelName',
        '-Model',
        '-LensModel',
        '-FocalLength',
        str(base_dir),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    stdout = completed.stdout.strip()
    if not stdout:
        detail = completed.stderr.strip() or f'exiftool exited with {completed.returncode}'
        raise RuntimeError(detail)
    try:
        records = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'exiftool JSON parse failed: {exc}') from exc
    if not isinstance(records, list):
        raise RuntimeError('exiftool returned an unexpected result')
    return _summarize_equipment_records(records, file_count, 'exiftool')


def _scan_equipment_pillow(files):
    tag_names = ExifTags.TAGS
    records = []
    for path in files:
        try:
            with Image.open(path) as image:
                exif = image.getexif()
                if not exif:
                    records.append({})
                    continue
                data = {tag_names.get(tag_id, tag_id): value for tag_id, value in exif.items()}
                records.append({
                    'Model': data.get('Model'),
                    'LensModel': data.get('LensModel'),
                    'FocalLength': data.get('FocalLength'),
                })
        except Exception:
            records.append({})
    return _summarize_equipment_records(records, len(files), 'pillow')


def _scan_equipment(base_dir: Path, files):
    if not files:
        return {
            'cameras': [],
            'lenses': [],
            'focal_lengths': [],
            'file_count': 0,
            'metadata_file_count': 0,
            'method': 'none',
            'source': '02_Base_Edit',
        }
    try:
        return _scan_equipment_exiftool(base_dir, len(files))
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError, OSError):
        return _scan_equipment_pillow(files)


def _ensure_equipment_cache_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS library_set_equipment_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            signature TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, relative_path)
        )
    ''')
    conn.commit()


def _read_equipment_cache(get_db_connection, source_id, set_rel, signature):
    conn = get_db_connection()
    try:
        _ensure_equipment_cache_table(conn)
        row = conn.execute(
            '''SELECT payload_json, updated_at
               FROM library_set_equipment_cache
               WHERE source_id=? AND relative_path=? AND signature=?''',
            (source_id, set_rel, signature),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row['payload_json'])
        payload['cached'] = True
        payload['cached_at'] = row['updated_at']
        return payload
    finally:
        conn.close()


def _write_equipment_cache(get_db_connection, source_id, set_rel, signature, payload):
    conn = get_db_connection()
    try:
        _ensure_equipment_cache_table(conn)
        conn.execute(
            '''INSERT INTO library_set_equipment_cache
                   (source_id, relative_path, signature, payload_json, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(source_id, relative_path) DO UPDATE SET
                   signature=excluded.signature,
                   payload_json=excluded.payload_json,
                   updated_at=CURRENT_TIMESTAMP''',
            (source_id, set_rel, signature, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def _count_set_stages(set_dir: Path):
    original_jpg = _iter_files(set_dir / '01_Original' / 'JPG', _ORIGINAL_JPG_EXTENSIONS, {'Deleted'})
    original_raw = _iter_files(set_dir / '01_Original' / 'RAW', _RAW_EXTENSIONS, {'Deleted'})
    base_edit = _iter_files(set_dir / '02_Base_Edit', _STAGE_IMAGE_EXTENSIONS, {'Deleted', 'discards', 'Ready'})
    model_edit = _iter_files(set_dir / '03_Model_Edit', _STAGE_IMAGE_EXTENSIONS, {'Deleted'})
    revision = _iter_files(set_dir / '04_Revision', _STAGE_IMAGE_EXTENSIONS, {'Deleted'})
    final = _iter_files(set_dir / '05_Final', _ORIGINAL_JPG_EXTENSIONS, {'Deleted'})
    return {
        'original_jpg': len(original_jpg),
        'original_raw': len(original_raw),
        'base_edit': len(base_edit),
        'model_edit': len(model_edit),
        'revision': len(revision),
        'final': len(final),
    }


def _set_theme_name(set_dir: Path):
    manifest_path = set_dir / 'manifest.json'
    try:
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            theme = data.get('theme')
            if isinstance(theme, dict):
                value = str(theme.get('name') or '').strip()
                if value:
                    return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    parts = set_dir.name.split('-', 2)
    return parts[2] if len(parts) >= 3 else set_dir.name


def _same_day_sessions(root: Path, current_set: Path):
    date_key = current_set.name[:8]
    if len(date_key) != 8 or not date_key.isdigit():
        return {'count': 0, 'items': []}

    items = []
    try:
        candidates = sorted(
            [
                path for path in root.iterdir()
                if path.is_dir()
                and path != current_set
                and _SET_RE.fullmatch(path.name)
                and path.name.startswith(date_key)
            ],
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        candidates = []

    for path in candidates:
        parts = path.name.split('-', 2)
        items.append({
            'path': path.relative_to(root).as_posix(),
            'name': path.name,
            'model': parts[1] if len(parts) >= 2 else '',
            'theme': _set_theme_name(path),
        })

    return {'count': len(items), 'items': items}


def create_set_info_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('library_set_info', __name__)

    def resolve_set(source_id):
        denied = admin_guard()
        if denied:
            return None, None, None, denied
        source = get_source(source_id)
        if not source:
            return None, None, None, (jsonify({'error': 'Source not found or disabled.'}), 404)
        try:
            _, target, rel = resolve_path(source, request.args.get('path', ''))
        except Exception as exc:
            return None, None, None, (jsonify({'error': str(exc)}), 400)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            return None, None, None, (jsonify({'error': 'Target is not a Set directory.'}), 400)
        return source, target, rel, None

    @bp.route('/api/library/insights/sources/<int:source_id>/set-stats', methods=['GET'])
    def set_stats(source_id):
        source, target, _rel, error = resolve_set(source_id)
        if error:
            return error
        try:
            payload = _count_set_stages(target)
            root = Path(source['root_path']).expanduser().resolve()
            payload['same_day_sessions'] = _same_day_sessions(root, target)
            return jsonify(payload)
        except OSError as exc:
            return jsonify({'error': f'Set stats failed: {exc}'}), 500

    @bp.route('/api/library/insights/sources/<int:source_id>/equipment', methods=['GET'])
    def equipment(source_id):
        _source, target, rel, error = resolve_set(source_id)
        if error:
            return error
        base_dir = target / '02_Base_Edit'
        files = _equipment_files(base_dir)
        signature = _equipment_signature(base_dir, files)
        force = request.args.get('force', '').strip().lower() in {'1', 'true', 'yes'}
        if not force:
            try:
                cached = _read_equipment_cache(get_db_connection, source_id, rel, signature)
            except Exception:
                cached = None
            if cached is not None:
                return jsonify(cached)

        try:
            payload = _scan_equipment(base_dir, files)
            payload['cached'] = False
            payload['cached_at'] = None
            _write_equipment_cache(get_db_connection, source_id, rel, signature, payload)
            return jsonify(payload)
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Equipment scan timed out.'}), 504
        except Exception as exc:
            return jsonify({'error': f'Equipment scan failed: {exc}'}), 500

    return bp
