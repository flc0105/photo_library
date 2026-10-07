import io
import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from flask import Blueprint, jsonify, request, send_file

from core.operations import file_signature, get_plan, new_task, remember_plan, update_task, verify_signatures
from features.mapped_library.autofill import get_datetime_original

_IMPORT_JPEG_EXTENSIONS = {'.jpg'}
_IMPORT_RAW_EXTENSIONS = {'.cr3'}
_SET_RE = re.compile(r'^\d{8}-.+-.+$')


def _photo_import_set_date(set_dir: Path):
    """Resolve the import date from manifest first, then the canonical Set name."""
    manifest_path = Path(set_dir) / 'manifest.json'
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding='utf-8'))
            shoot = data.get('shoot') if isinstance(data, dict) else None
            date_text = str((shoot or {}).get('date') or '').strip() if isinstance(shoot, dict) else ''
            if date_text:
                datetime.strptime(date_text, '%Y-%m-%d')
                return date_text
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    match = re.match(r'^(\d{4})(\d{2})(\d{2})-', Path(set_dir).name)
    if not match:
        raise ValueError('Cannot determine shoot date from manifest or Set name')
    date_text = f'{match.group(1)}-{match.group(2)}-{match.group(3)}'
    datetime.strptime(date_text, '%Y-%m-%d')
    return date_text


def _photo_import_target_dirs(set_dir: Path):
    base = Path(set_dir) / '01_Original'
    return base / 'JPG', base / 'RAW'


def _directory_has_files(directory: Path):
    if not directory.exists():
        return False
    if not directory.is_dir():
        raise ValueError(f'Not a directory: {directory}')
    try:
        return any(path.is_file() and not path.name.startswith('.') for path in directory.rglob('*'))
    except OSError as exc:
        raise ValueError(f'Cannot read directory: {directory}') from exc


def _assert_photo_import_target_empty(set_dir: Path):
    jpg_target, raw_target = _photo_import_target_dirs(set_dir)
    if _directory_has_files(jpg_target) or _directory_has_files(raw_target):
        raise ValueError('Original must be empty.')
    return jpg_target, raw_target


def _photo_import_source_dirs(source_root, shoot_date):
    root = Path(str(source_root or '')).expanduser()
    if not root.is_absolute():
        raise ValueError('Source Root must be absolute')
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f'Source Root not found: {root}')
    date_dir = root / shoot_date
    if not date_dir.is_dir():
        raise FileNotFoundError(f'Export folder not found: {date_dir}')
    return root, date_dir, date_dir


def _photo_import_sample(file_ids, records):
    if len(file_ids) <= 10:
        head = file_ids
        tail = []
        omitted = 0
    else:
        head = file_ids[:5]
        tail = file_ids[-5:]
        omitted = len(file_ids) - 10

    def public_record(file_id):
        item = records[file_id]
        value = item.get('taken_at')
        return {
            'id': file_id,
            'name': item['name'],
            'taken_at': value.strftime('%H:%M:%S') if value else '',
        }

    return {
        'head': [public_record(file_id) for file_id in head],
        'tail': [public_record(file_id) for file_id in tail],
        'omitted_count': omitted,
    }


def _build_photo_import_plan(source_id, set_dir: Path, set_rel: str, source_root, gap_minutes):
    try:
        gap = int(gap_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError('Gap must be an integer number of minutes') from exc
    if gap < 1 or gap > 24 * 60:
        raise ValueError('Gap must be 1–1440 minutes')

    _assert_photo_import_target_empty(set_dir)
    shoot_date = _photo_import_set_date(set_dir)
    root, jpg_dir, raw_dir = _photo_import_source_dirs(source_root, shoot_date)
    jpg_files = sorted(
        [path for path in jpg_dir.iterdir() if path.is_file() and path.suffix.lower() in _IMPORT_JPEG_EXTENSIONS],
        key=lambda path: path.name.casefold(),
    )
    if not jpg_files:
        raise ValueError(f'No JPGs in export folder: {jpg_dir}')

    records = {}
    known = []
    unknown = []
    signatures = {}
    for index, path in enumerate(jpg_files, start=1):
        file_id = str(index)
        taken_at = get_datetime_original(path)
        records[file_id] = {
            'id': file_id,
            'name': path.name,
            'path': str(path),
            'stem': path.stem,
            'taken_at': taken_at,
        }
        signatures[str(path)] = file_signature(path)
        if taken_at is None:
            unknown.append(file_id)
        else:
            known.append(file_id)

    known.sort(key=lambda file_id: (records[file_id]['taken_at'], records[file_id]['name'].casefold()))
    grouped_ids = []
    current = []
    previous = None
    for file_id in known:
        current_time = records[file_id]['taken_at']
        if previous is not None and (current_time - previous).total_seconds() > gap * 60:
            grouped_ids.append((current, False))
            current = []
        current.append(file_id)
        previous = current_time
    if current:
        grouped_ids.append((current, False))
    if unknown:
        grouped_ids.append((unknown, True))

    groups = []
    for index, (file_ids, is_unknown) in enumerate(grouped_ids, start=1):
        times = [records[file_id]['taken_at'] for file_id in file_ids if records[file_id]['taken_at'] is not None]
        groups.append({
            'id': f'g{index}',
            'index': index,
            'file_ids': file_ids,
            'count': len(file_ids),
            'start_time': min(times).strftime('%H:%M:%S') if times else '',
            'end_time': max(times).strftime('%H:%M:%S') if times else '',
            'unknown_time': bool(is_unknown),
            'sample': _photo_import_sample(file_ids, records),
        })

    return {
        'kind': 'photo_import_preview',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'shoot_date': shoot_date,
        'source_root': str(root),
        'jpg_dir': str(jpg_dir),
        'raw_dir': str(raw_dir),
        'gap_minutes': gap,
        'records': records,
        'groups': groups,
        'signatures': signatures,
    }


def _photo_import_public_plan(plan, plan_id):
    return {
        'plan_id': plan_id,
        'shoot_date': plan['shoot_date'],
        'source_root': plan['source_root'],
        'jpg_dir': plan['jpg_dir'],
        'raw_dir': plan['raw_dir'],
        'gap_minutes': plan['gap_minutes'],
        'jpg_count': len(plan['records']),
        'groups': [
            {
                'id': group['id'],
                'index': group['index'],
                'count': group['count'],
                'start_time': group['start_time'],
                'end_time': group['end_time'],
                'unknown_time': group['unknown_time'],
                'sample': group['sample'],
            }
            for group in plan['groups']
        ],
    }


def _photo_import_selected_records(plan, group_ids):
    selected = {str(value) for value in (group_ids or [])}
    valid = {group['id'] for group in plan['groups']}
    if not selected:
        raise ValueError('Select at least one group')
    invalid = selected - valid
    if invalid:
        raise ValueError('Selected groups are stale. Preview again.')

    file_ids = []
    for group in plan['groups']:
        if group['id'] in selected:
            file_ids.extend(group['file_ids'])
    return [plan['records'][file_id] for file_id in file_ids]


def _prepare_photo_import_execution(source_id, set_rel, plan, group_ids):
    set_dir = Path(plan['set_dir'])
    jpg_target, raw_target = _assert_photo_import_target_empty(set_dir)
    selected = _photo_import_selected_records(plan, group_ids)
    selected_signatures = {item['path']: plan['signatures'][item['path']] for item in selected}
    verify_signatures(selected_signatures)

    # Multiple JPGs with the same stem cannot be paired safely with one RAW.
    jpg_stems = {}
    for item in selected:
        key = item['stem'].casefold()
        jpg_stems.setdefault(key, []).append(item)
    duplicates = [items for items in jpg_stems.values() if len(items) > 1]
    if duplicates:
        names = ', '.join('/'.join(item['name'] for item in items) for items in duplicates[:5])
        raise ValueError(f'Duplicate JPG stems; RAW pairing is ambiguous: {names}')

    raw_dir = Path(plan['raw_dir'])
    raw_index = {}
    for path in raw_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in _IMPORT_RAW_EXTENSIONS:
            continue
        raw_index.setdefault(path.stem.casefold(), []).append(path)

    missing = []
    ambiguous = []
    operations = []
    signatures = dict(selected_signatures)
    for jpg in selected:
        candidates = raw_index.get(jpg['stem'].casefold(), [])
        if not candidates:
            missing.append(jpg['name'])
            continue
        if len(candidates) != 1:
            ambiguous.append(f"{jpg['name']} → {', '.join(path.name for path in candidates)}")
            continue
        raw = candidates[0]
        signatures[str(raw)] = file_signature(raw)
        jpg_dst = jpg_target / jpg['name']
        raw_dst = raw_target / raw.name
        if jpg_dst.exists() or raw_dst.exists():
            raise FileExistsError(f'Destination file exists: {jpg_dst.name if jpg_dst.exists() else raw_dst.name}')
        operations.append({
            'jpg_src': jpg['path'],
            'jpg_dst': str(jpg_dst),
            'raw_src': str(raw),
            'raw_dst': str(raw_dst),
            'stem': jpg['stem'],
        })

    if missing or ambiguous:
        messages = []
        if missing:
            preview = ', '.join(missing[:8])
            if len(missing) > 8:
                preview += f' · {len(missing)} total'
            messages.append(f'Missing matching RAW: {preview}')
        if ambiguous:
            preview = '; '.join(ambiguous[:5])
            if len(ambiguous) > 5:
                preview += f' · {len(ambiguous)} groups total'
            messages.append(f'Ambiguous matching RAW: {preview}')
        raise ValueError('；'.join(messages))

    return {
        'kind': 'photo_import_execute',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'source_root': plan['source_root'],
        'jpg_dir': plan['jpg_dir'],
        'raw_dir': plan['raw_dir'],
        'shoot_date': plan['shoot_date'],
        'operations': operations,
        'signatures': signatures,
        'jpg_target': str(jpg_target),
        'raw_target': str(raw_target),
    }


def _rollback_photo_import(moved):
    errors = []
    for src, dst in reversed(moved):
        src = Path(src)
        dst = Path(dst)
        try:
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
        except Exception as exc:
            errors.append(f'{dst.name}: {exc}')
    return errors


def create_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('workflow_photo_import', __name__)

    def resolve_set(source_id, path_value):
        source = get_source(source_id)
        if not source:
            raise FileNotFoundError('Source unavailable')
        root, target, rel = resolve_path(source, path_value)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            raise ValueError('Current folder is not a Set')
        return source, root, target, rel

    def require_set(source_id):
        return resolve_set(source_id, (request.get_json(silent=True) or {}).get('path', ''))

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/preview', methods=['POST'])
    def photo_import_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _build_photo_import_plan(
                source_id,
                set_dir,
                set_rel,
                data.get('source_root') or '/Users/flc/Pictures/Camera Exports/',
                data.get('gap_minutes', 30),
            )
            plan_id = remember_plan(plan)
            return jsonify(_photo_import_public_plan(plan, plan_id))
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/thumbnail/<plan_id>/<file_id>', methods=['GET'])
    def photo_import_thumbnail(source_id, plan_id, file_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = resolve_set(source_id, request.args.get('path', ''))
            plan = get_plan(str(plan_id), 'photo_import_preview', source_id, set_rel)
            record = plan['records'].get(str(file_id))
            if not record:
                raise FileNotFoundError('Preview image not found')
            path = Path(record['path'])
            expected = plan['signatures'].get(str(path))
            if expected is None or file_signature(path) != expected:
                raise RuntimeError('Source JPG changed. Preview again.')

            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode != 'RGB':
                    image = image.convert('RGB')
                image.thumbnail((220, 160), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, 'JPEG', quality=72, optimize=True)
            buffer.seek(0)
            return send_file(buffer, mimetype='image/jpeg', max_age=300)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 404

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/prepare', methods=['POST'])
    def photo_import_prepare(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = get_plan(str(data.get('plan_id') or ''), 'photo_import_preview', source_id, set_rel)
            execution = _prepare_photo_import_execution(source_id, set_rel, plan, data.get('group_ids'))
            execution_id = remember_plan(execution)
            count = len(execution['operations'])
            return jsonify({
                'plan_id': execution_id,
                'shoot_date': execution['shoot_date'],
                'jpg_count': count,
                'raw_count': count,
                'total_files': count * 2,
                'jpg_destination': '01_Original/JPG',
                'raw_destination': '01_Original/RAW',
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/start', methods=['POST'])
    def photo_import_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = get_plan(str(data.get('plan_id') or ''), 'photo_import_execute', source_id, set_rel)
            verify_signatures(plan['signatures'])
            _assert_photo_import_target_empty(set_dir)
            operations = list(plan['operations'])
            for item in operations:
                if Path(item['jpg_dst']).exists() or Path(item['raw_dst']).exists():
                    raise FileExistsError(f'Destination file exists: {item["stem"]}')

            task_id = new_task('photo_import', len(operations) * 2)

            def worker():
                moved = []
                completed = 0
                try:
                    update_task(task_id, status='running', message='Importing JPG / RAW…')
                    Path(plan['jpg_target']).mkdir(parents=True, exist_ok=True)
                    Path(plan['raw_target']).mkdir(parents=True, exist_ok=True)
                    for item in operations:
                        jpg_src = Path(item['jpg_src'])
                        jpg_dst = Path(item['jpg_dst'])
                        raw_src = Path(item['raw_src'])
                        raw_dst = Path(item['raw_dst'])

                        if not jpg_src.exists() or not raw_src.exists():
                            raise FileNotFoundError(f'Source file not found: {item["stem"]}')
                        if jpg_dst.exists() or raw_dst.exists():
                            raise FileExistsError(f'Destination file exists: {item["stem"]}')

                        shutil.move(str(jpg_src), str(jpg_dst))
                        moved.append((str(jpg_src), str(jpg_dst)))
                        completed += 1
                        jpg_message = f'{jpg_src.name} → Original JPG'
                        update_task(task_id, completed=completed, current=completed, message=jpg_message, log=jpg_message)

                        shutil.move(str(raw_src), str(raw_dst))
                        moved.append((str(raw_src), str(raw_dst)))
                        completed += 1
                        raw_message = f'{raw_src.name} → Original RAW'
                        update_task(task_id, completed=completed, current=completed, message=raw_message, log=raw_message)

                    update_task(
                        task_id,
                        status='done',
                        message=f'Imported {len(operations)} JPG + RAW pairs',
                        result={
                            'jpg_count': len(operations),
                            'raw_count': len(operations),
                            'total_files': len(operations) * 2,
                        },
                    )
                except Exception as exc:
                    rollback_errors = _rollback_photo_import(moved)
                    message = str(exc)
                    if rollback_errors:
                        message += '; rollback failed: ' + '; '.join(rollback_errors[:5])
                    update_task(task_id, status='error', message='Import failed; rollback attempted', error=message, log=f'ERROR: {message}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    return bp
