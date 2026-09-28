import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request
from PIL import ExifTags, Image

from core.external_tools import resolve_exiftool


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
_ALLOWED_ORIGINAL_DIRS = {'JPG', 'RAW'}


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


def _direct_files(path: Path, extensions=None):
    if not path.is_dir():
        return []
    extensions = {ext.lower() for ext in extensions} if extensions else None
    result = []
    try:
        for item in path.iterdir():
            if _is_hidden_or_control(item.name) or not item.is_file():
                continue
            if extensions is not None and item.suffix.lower() not in extensions:
                continue
            result.append(item)
    except OSError:
        return result
    return sorted(result, key=lambda item: item.name.casefold())


def _direct_invalid_files(path: Path, allowed_extensions):
    return [
        file_path for file_path in _direct_files(path)
        if file_path.suffix.lower() not in allowed_extensions
    ]


def _set_structure_findings(set_dir: Path):
    missing = []
    unexpected = []

    top_dirs = _top_level_subdirs(set_dir)
    missing.extend(sorted(_REQUIRED_SET_DIRS - top_dirs))
    unexpected.extend(sorted(top_dirs - _REQUIRED_SET_DIRS))

    original_dir = set_dir / '01_Original'
    if original_dir.is_dir():
        original_subdirs = _top_level_subdirs(original_dir)
        missing.extend(
            f'01_Original/{name}'
            for name in sorted(_ALLOWED_ORIGINAL_DIRS - original_subdirs)
        )
        unexpected.extend(
            f'01_Original/{name}'
            for name in sorted(original_subdirs - _ALLOWED_ORIGINAL_DIRS)
        )

    # Every standard leaf directory is file-only. Reporting the first unexpected
    # child is enough; descendants are already outside the standard structure.
    leaf_dirs = (
        '01_Original/JPG',
        '01_Original/RAW',
        '02_Base_Edit',
        '03_Model_Edit',
        '04_Revision',
        '05_Final',
    )
    for relative in leaf_dirs:
        for name in sorted(_top_level_subdirs(set_dir / relative)):
            unexpected.append(f'{relative}/{name}')

    return missing, unexpected


def _format_file_preview(names):
    names = list(names)
    if len(names) <= 3:
        return ', '.join(names)
    return f'{", ".join(names[:2])} 等 {len(names)} 个文件'


def _format_stem_counter(counter, labels):
    items = []
    for stem, count in sorted(counter.items()):
        label = labels.get(stem, stem)
        items.append(f'{label} ×{count}' if count > 1 else label)
    return ', '.join(items)


def _structure_affects(paths, roots):
    roots = set(roots)
    return any(path.split('/', 1)[0] in roots for path in paths)


def _original_jpg_stem(path: Path):
    stem = path.stem
    if stem.casefold().endswith('-dpp'):
        stem = stem[:-4]
    return stem.casefold()


def _stage_filename_violations(paths, original_stems):
    """Return stage images whose stem is not an exact Original file stem."""
    if not original_stems:
        return []
    return [path for path in paths if path.stem.casefold() not in original_stems]


def _format_stage_filename_warning(stage_label, paths):
    names = [path.name for path in paths]
    return f'{stage_label} filename does not match Original stem: {_format_file_preview(names)}'


def _validate_one_set(set_dir: Path):
    issues = []
    warnings = []
    info = []

    missing, unexpected = _set_structure_findings(set_dir)
    if missing:
        issues.append('Missing standard directories: ' + ', '.join(missing))
    if unexpected:
        issues.append('Unexpected directories: ' + ', '.join(unexpected))

    jpg_dir = set_dir / '01_Original' / 'JPG'
    raw_dir = set_dir / '01_Original' / 'RAW'
    jpg_files = _direct_files(jpg_dir, _ORIGINAL_JPG_EXTENSIONS)
    raw_files = _direct_files(raw_dir, _RAW_EXTENSIONS)
    invalid_jpg = _direct_invalid_files(jpg_dir, _ORIGINAL_JPG_EXTENSIONS)
    invalid_raw = _direct_invalid_files(raw_dir, _RAW_EXTENSIONS)
    if invalid_jpg:
        issues.append(
            'Original JPG contains non-JPG files: '
            + _format_file_preview(path.name for path in invalid_jpg)
        )
    if invalid_raw:
        issues.append(
            'Original RAW contains non-CR3 files: '
            + _format_file_preview(path.name for path in invalid_raw)
        )

    jpg_count = len(jpg_files)
    raw_count = len(raw_files)
    jpg_stems = Counter(_original_jpg_stem(path) for path in jpg_files)
    raw_stems = Counter(path.stem.casefold() for path in raw_files)
    jpg_stem_labels = {}
    raw_stem_labels = {}
    for path in jpg_files:
        stem = _original_jpg_stem(path)
        label = path.stem[:-4] if path.stem.casefold().endswith('-dpp') else path.stem
        jpg_stem_labels.setdefault(stem, label)
    for path in raw_files:
        raw_stem_labels.setdefault(path.stem.casefold(), path.stem)

    if raw_dir.is_dir() and raw_count == 0:
        issues.append('Original RAW is empty')
    if jpg_dir.is_dir() and raw_count > 0 and jpg_count == 0:
        info.append(f'Original JPG is empty (RAW ×{raw_count})')
    if jpg_count > 0 and raw_count > 0 and jpg_stems != raw_stems:
        jpg_extra = jpg_stems - raw_stems
        raw_extra = raw_stems - jpg_stems
        if jpg_extra:
            count = sum(jpg_extra.values())
            issues.append(
                f'JPG/RAW mismatch, RAW missing ({count}): '
                f'{_format_stem_counter(jpg_extra, jpg_stem_labels)}'
            )
        if raw_extra:
            count = sum(raw_extra.values())
            issues.append(
                f'JPG/RAW mismatch, JPG missing ({count}): '
                f'{_format_stem_counter(raw_extra, raw_stem_labels)}'
            )

    dpp_files = [path.name for path in jpg_files if path.stem.casefold().endswith('-dpp')]

    # Validation counts only files directly inside each standard stage. Any
    # nested directory is already reported as a structure Issue.
    base_files = _direct_files(set_dir / '02_Base_Edit', _STAGE_IMAGE_EXTENSIONS)
    model_files = _direct_files(set_dir / '03_Model_Edit', _STAGE_IMAGE_EXTENSIONS)
    revision_files = _direct_files(set_dir / '04_Revision', _STAGE_IMAGE_EXTENSIONS)
    final_files = _direct_files(set_dir / '05_Final', _ORIGINAL_JPG_EXTENSIONS)

    base_count = len(base_files)
    model_count = len(model_files)

    original_stems = set(jpg_stems) | set(raw_stems)
    filename_warning_by_stage = {}
    for stage_label, files in (
        ('Base Edit', base_files),
        ('Model Edit', model_files),
        ('Revision', revision_files),
    ):
        violations = _stage_filename_violations(files, original_stems)
        filename_warning_by_stage[stage_label] = bool(violations)
        if violations:
            warnings.append(_format_stage_filename_warning(stage_label, violations))

    base_stems = Counter(path.stem.casefold() for path in base_files)
    model_stems = Counter(path.stem.casefold() for path in model_files)

    non_png_revision = [path for path in revision_files if path.suffix.lower() != '.png']
    if non_png_revision:
        issues.append(
            'Revision must be PNG: '
            + _format_file_preview(path.name for path in non_png_revision)
        )

    base_stem_labels = {}
    model_stem_labels = {}
    for path in base_files:
        base_stem_labels.setdefault(path.stem.casefold(), path.stem)
    for path in model_files:
        model_stem_labels.setdefault(path.stem.casefold(), path.stem)

    if base_count > 0 and model_count > 0 and base_stems != model_stems:
        base_extra = base_stems - model_stems
        model_extra = model_stems - base_stems
        if base_extra:
            count = sum(base_extra.values())
            warnings.append(
                f'Base/Model mismatch, Model missing ({count}): '
                f'{_format_stem_counter(base_extra, base_stem_labels)}'
            )
        if model_extra:
            count = sum(model_extra.values())
            warnings.append(
                f'Base/Model mismatch, Base missing ({count}): '
                f'{_format_stem_counter(model_extra, model_stem_labels)}'
            )
    elif base_count > 0 and model_count == 0:
        info.append(f'Model Edit is empty (Base Edit ×{base_count})')
    elif base_count == 0 and model_count > 0:
        info.append(f'Base Edit is empty (Model Edit ×{model_count})')
    elif base_count == 0 and model_count == 0:
        info.append('Base Edit and Model Edit are both empty')

    structure_paths = missing + unexpected
    original_issue = (
        _structure_affects(structure_paths, {'01_Original'})
        or bool(invalid_jpg)
        or bool(invalid_raw)
        or (raw_dir.is_dir() and raw_count == 0)
        or (jpg_count > 0 and raw_count > 0 and jpg_stems != raw_stems)
    )
    base_model_issue = _structure_affects(structure_paths, {'02_Base_Edit', '03_Model_Edit'})
    revision_issue = (
        _structure_affects(structure_paths, {'04_Revision'})
        or bool(non_png_revision)
    )
    final_issue = _structure_affects(structure_paths, {'05_Final'})

    if original_issue:
        original_status = 'issue'
    elif jpg_dir.is_dir() and raw_count > 0 and jpg_count == 0:
        original_status = 'info'
    else:
        original_status = 'ok'

    if base_model_issue:
        base_model_status = 'issue'
    elif (
        (base_count > 0 and model_count > 0 and base_stems != model_stems)
        or filename_warning_by_stage.get('Base Edit')
        or filename_warning_by_stage.get('Model Edit')
    ):
        base_model_status = 'warning'
    elif base_count == 0 or model_count == 0:
        base_model_status = 'info'
    else:
        base_model_status = 'ok'

    if revision_issue:
        revision_status = 'issue'
    elif filename_warning_by_stage.get('Revision'):
        revision_status = 'warning'
    else:
        revision_status = 'ok'

    final_status = 'issue' if final_issue else 'ok'

    if issues:
        status = 'issue'
    elif warnings:
        status = 'warning'
    elif info:
        status = 'info'
    else:
        status = 'ok'

    return {
        'name': set_dir.name,
        'date_key': set_dir.name[:8],
        'status': status,
        'jpg_count': jpg_count,
        'raw_count': raw_count,
        'original_status': original_status,
        'original_count_match': jpg_count > 0 and raw_count > 0 and jpg_stems == raw_stems,
        'base_count': base_count,
        'model_count': model_count,
        'base_model_status': base_model_status,
        'revision_count': len(revision_files),
        'revision_status': revision_status,
        'final_count': len(final_files),
        'final_status': final_status,
        'base_model_match': base_count > 0 and model_count > 0 and base_stems == model_stems,
        'dpp_files': dpp_files,
        'issues': issues,
        'warnings': warnings,
        'info': info,
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

    status_counts = Counter(item['status'] for item in sets)

    return {
        'root': str(root),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'set_count': len(sets),
            'issue_count': status_counts.get('issue', 0),
            'warning_count': status_counts.get('warning', 0),
            'info_count': status_counts.get('info', 0),
            'ok_count': status_counts.get('ok', 0),
        },
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
        source, target, _rel, error = resolve_set(source_id)
        if error:
            return error
        try:
            payload = _count_set_stages(target)
            root = Path(source['root_path']).expanduser().resolve()
            payload['same_day_sessions'] = _same_day_sessions(root, target)
            return jsonify(payload)
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
