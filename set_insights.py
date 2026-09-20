import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request
from PIL import ExifTags, Image


_SET_RE = re.compile(r'^\d{8}-.+-.+$')
_BASE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}
_STAGE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}
_ORIGINAL_JPG_EXTENSIONS = {'.jpg', '.jpeg'}
_RAW_EXTENSIONS = {'.cr3'}
_IGNORED_CONTROL_NAMES = {'manifest.json', 'manifest.json.bak', 'manifest.json.tmp'}
_REQUIRED_SET_DIRS = {
    '01_Original',
    '02_Base_Edit',
    '03_Model_Edit',
    '04_Revision',
    '05_Final',
}
_ALLOWED_TOP_LEVEL_DIRS = _REQUIRED_SET_DIRS | {'Discards'}
_ALLOWED_ORIGINAL_DIRS = {'JPG', 'RAW', 'Selects'}
_ALLOWED_BASE_SUBDIRS = {'Ready'}


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
    exiftool = shutil.which('exiftool')
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
    base_edit = _iter_files(set_dir / '02_Base_Edit', _STAGE_IMAGE_EXTENSIONS, {'Deleted'})
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


def _top_level_subdirs(path: Path):
    if not path.is_dir():
        return set()
    result = set()
    try:
        for item in path.iterdir():
            if item.name.startswith('.'):
                continue
            if item.is_dir() and not item.is_symlink():
                result.add(item.name)
    except OSError:
        return result
    return result


def _direct_invalid_files(path: Path, allowed_extensions):
    if not path.is_dir():
        return []
    invalid = []
    for file_path in _iter_files(path, None, {'Deleted'}):
        if file_path.suffix.lower() not in allowed_extensions:
            invalid.append(file_path)
    return invalid


def _extract_version_tags(paths):
    counter = Counter()
    for path in paths:
        stem = path.stem
        if '-' not in stem:
            continue
        _, suffix = stem.split('-', 1)
        for tag in (part.strip() for part in suffix.split('-')):
            if tag:
                counter[tag] += 1
    return [
        {'name': name, 'count': count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]


def _validate_one_set(set_dir: Path):
    issues = []
    notes = []

    top_dirs = _top_level_subdirs(set_dir)
    missing = sorted(_REQUIRED_SET_DIRS - top_dirs)
    unexpected_top = sorted(top_dirs - _ALLOWED_TOP_LEVEL_DIRS)
    if missing:
        issues.append('Missing: ' + ', '.join(missing))
    if unexpected_top:
        issues.append('Unexpected top-level: ' + ', '.join(unexpected_top))

    original_dir = set_dir / '01_Original'
    original_subdirs = _top_level_subdirs(original_dir)
    missing_original = sorted({'JPG', 'RAW'} - original_subdirs)
    unexpected_original = sorted(original_subdirs - _ALLOWED_ORIGINAL_DIRS)
    if missing_original:
        issues.append('01_Original missing: ' + ', '.join(missing_original))
    if unexpected_original:
        issues.append('01_Original unexpected: ' + ', '.join(unexpected_original))

    base_subdirs = _top_level_subdirs(set_dir / '02_Base_Edit')
    unexpected_base = sorted(base_subdirs - _ALLOWED_BASE_SUBDIRS)
    if unexpected_base:
        issues.append('02_Base_Edit unexpected: ' + ', '.join(unexpected_base))

    jpg_dir = set_dir / '01_Original' / 'JPG'
    raw_dir = set_dir / '01_Original' / 'RAW'
    jpg_files = _iter_files(jpg_dir, _ORIGINAL_JPG_EXTENSIONS, {'Deleted'})
    raw_files = _iter_files(raw_dir, _RAW_EXTENSIONS, {'Deleted'})
    invalid_jpg = _direct_invalid_files(jpg_dir, _ORIGINAL_JPG_EXTENSIONS)
    invalid_raw = _direct_invalid_files(raw_dir, _RAW_EXTENSIONS)
    if invalid_jpg:
        issues.append(f'Original/JPG has {len(invalid_jpg)} non-JPG file(s)')
    if invalid_raw:
        issues.append(f'Original/RAW has {len(invalid_raw)} non-CR3 file(s)')
    if len(jpg_files) != len(raw_files):
        issues.append(f'Original count mismatch: {len(jpg_files)} JPG / {len(raw_files)} RAW')

    dpp_files = [path.name for path in jpg_files if path.stem.lower().endswith('-dpp')]
    if dpp_files:
        notes.append(f'{len(dpp_files)} DPP regenerated JPG')

    base_all = _iter_files(set_dir / '02_Base_Edit', _STAGE_IMAGE_EXTENSIONS, {'Deleted'})
    ready_files = _iter_files(set_dir / '02_Base_Edit' / 'Ready', _STAGE_IMAGE_EXTENSIONS, {'Deleted'})
    ready_set = {str(path.resolve()) for path in ready_files}
    base_comparable = [path for path in base_all if str(path.resolve()) not in ready_set]
    model_files = _iter_files(set_dir / '03_Model_Edit', _STAGE_IMAGE_EXTENSIONS, {'Deleted'})
    if len(base_comparable) != len(model_files):
        suffix = f' (+{len(ready_files)} Ready excluded)' if ready_files else ''
        issues.append(f'Base/Model mismatch: {len(base_comparable)} / {len(model_files)}{suffix}')
    elif ready_files:
        notes.append(f'{len(ready_files)} Ready file(s) excluded from Base/Model comparison')

    version_files = []
    for stage in ('02_Base_Edit', '03_Model_Edit', '04_Revision'):
        version_files.extend(_iter_files(set_dir / stage, _STAGE_IMAGE_EXTENSIONS, {'Deleted'}))
    versions = _extract_version_tags(version_files)

    return {
        'name': set_dir.name,
        'status': 'ok' if not issues else 'issue',
        'structure_ok': not (missing or unexpected_top or missing_original or unexpected_original or unexpected_base),
        'missing_directories': missing + [f'01_Original/{name}' for name in missing_original],
        'unexpected_directories': unexpected_top + [f'01_Original/{name}' for name in unexpected_original] + [f'02_Base_Edit/{name}' for name in unexpected_base],
        'jpg_count': len(jpg_files),
        'raw_count': len(raw_files),
        'original_count_match': len(jpg_files) == len(raw_files),
        'base_count': len(base_comparable),
        'base_total_count': len(base_all),
        'ready_count': len(ready_files),
        'model_count': len(model_files),
        'base_model_match': len(base_comparable) == len(model_files),
        'dpp_files': dpp_files,
        'versions': versions,
        'issues': issues,
        'notes': notes,
    }


def _validate_root(root: Path):
    sets = []
    try:
        children = sorted(
            [path for path in root.iterdir() if path.is_dir() and _SET_RE.fullmatch(path.name)],
            key=lambda path: path.name.casefold(),
        )
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc

    for set_dir in children:
        sets.append(_validate_one_set(set_dir))

    ok_count = sum(1 for item in sets if item['status'] == 'ok')
    dpp_count = sum(1 for item in sets if item['dpp_files'])
    version_counter = Counter()
    for item in sets:
        for version in item['versions']:
            version_counter[version['name']] += int(version['count'])

    return {
        'root': str(root),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'set_count': len(sets),
            'ok_count': ok_count,
            'issue_count': len(sets) - ok_count,
            'dpp_set_count': dpp_count,
        },
        'versions': [
            {'name': name, 'count': count}
            for name, count in sorted(version_counter.items(), key=lambda item: (-item[1], item[0].casefold()))
        ],
        'sets': sets,
    }


def create_set_insights_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('library_set_insights', __name__)

    def resolve_set(source_id):
        denied = admin_guard()
        if denied:
            return None, None, None, denied
        source = get_source(source_id)
        if not source:
            return None, None, None, (jsonify({'error': 'Source 不存在或已禁用'}), 404)
        try:
            _, target, rel = resolve_path(source, request.args.get('path', ''))
        except Exception as exc:
            return None, None, None, (jsonify({'error': str(exc)}), 400)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            return None, None, None, (jsonify({'error': '目标不是 Set 目录'}), 400)
        return source, target, rel, None

    @bp.route('/api/library/insights/sources/<int:source_id>/set-stats', methods=['GET'])
    def set_stats(source_id):
        _source, target, _rel, error = resolve_set(source_id)
        if error:
            return error
        try:
            return jsonify(_count_set_stages(target))
        except OSError as exc:
            return jsonify({'error': f'统计 Set 文件失败: {exc}'}), 500

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
            return jsonify({'error': '设备信息扫描超时'}), 504
        except Exception as exc:
            return jsonify({'error': f'设备信息扫描失败: {exc}'}), 500

    @bp.route('/api/library/insights/sources/<int:source_id>/validate-root', methods=['POST'])
    def validate_root(source_id):
        denied = admin_guard()
        if denied:
            return denied
        source = get_source(source_id)
        if not source:
            return jsonify({'error': 'Source 不存在或已禁用'}), 404
        try:
            root, target, rel = resolve_path(source, '')
            if rel or target != root:
                return jsonify({'error': 'Validation 只能在 Source 根目录执行'}), 400
            return jsonify(_validate_root(root))
        except Exception as exc:
            return jsonify({'error': f'Validation 失败: {exc}'}), 500

    return bp
