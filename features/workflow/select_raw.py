import os
import shutil
import threading
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.operations import file_signature, get_plan, new_task, remember_plan, update_task, verify_signatures

_SET_RE = re.compile(r'^\d{8}-.+-.+$')


def _logical_id_from_jpg_name(filename: str):
    stem = Path(filename).stem
    # Supports the archive's stage suffixes and the special Original "-dpp" marker.
    return stem.split('-', 1)[0]


def _build_select_raw_plan(source_id, set_dir: Path, set_rel: str, get_db_connection):
    raw_dir = set_dir / '01_Original' / 'RAW'
    if not raw_dir.is_dir():
        raise ValueError('01_Original/RAW 不存在')
    selects_dir = set_dir / '01_Original' / 'Selects'

    conn = get_db_connection()
    rows = conn.execute(
        'SELECT relative_path FROM library_image_states WHERE source_id=? AND is_favorited=1',
        (source_id,),
    ).fetchall()
    conn.close()

    prefix = (set_rel.rstrip('/') + '/') if set_rel else ''
    favorite_paths = []
    ignored_non_jpg = []
    for row in rows:
        rel = str(row['relative_path'])
        if prefix and not rel.startswith(prefix):
            continue
        if not prefix and '/' not in rel:
            # A source root should not normally be a Set, but keep containment explicit.
            pass
        suffix = Path(rel).suffix.lower()
        if suffix in ('.jpg', '.jpeg'):
            favorite_paths.append(rel)
        elif suffix:
            ignored_non_jpg.append(rel)

    raw_candidates = []
    for root, dirs, files in os.walk(raw_dir):
        dirs[:] = [d for d in dirs if d.casefold() not in {'deleted', 'selects'}]
        for filename in files:
            if filename.lower().endswith('.cr3'):
                raw_candidates.append(Path(root) / filename)

    raw_by_id = {}
    for path in raw_candidates:
        key = path.stem.casefold()
        raw_by_id.setdefault(key, []).append(path)

    grouped_favorites = {}
    for rel in sorted(set(favorite_paths), key=str.casefold):
        filename = Path(rel).name
        logical_id = _logical_id_from_jpg_name(filename)
        group = grouped_favorites.setdefault(logical_id.casefold(), {
            'logical_id': logical_id,
            'favorites': [],
        })
        group['favorites'].append(rel)

    items = []
    copy_operations = []
    signatures = {}
    for paths in raw_by_id.values():
        for path in paths:
            signatures[str(path)] = file_signature(path)

    for key, group in grouped_favorites.items():
        matches = raw_by_id.get(key, [])
        item = {
            'logical_id': group['logical_id'],
            'favorite_files': [Path(rel).name for rel in group['favorites']],
            'favorite_paths': group['favorites'],
            'raw_name': '',
            'destination': '',
            'status': '',
        }
        if not matches:
            item['status'] = 'missing_raw'
        elif len(matches) > 1:
            item['status'] = 'ambiguous_raw'
            item['raw_name'] = ' / '.join(path.name for path in matches)
        else:
            raw_path = matches[0]
            dest = selects_dir / raw_path.name
            item['raw_name'] = raw_path.name
            item['destination'] = f'01_Original/Selects/{raw_path.name}'
            if dest.exists():
                item['status'] = 'already_exists'
            else:
                item['status'] = 'copy'
                copy_operations.append({'src': str(raw_path), 'dst': str(dest), 'logical_id': group['logical_id']})
        items.append(item)

    return {
        'kind': 'select_raw',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'selects_dir': str(selects_dir),
        'operations': copy_operations,
        'signatures': signatures,
        'items': items,
        'summary': {
            'favorite_jpg_count': len(favorite_paths),
            'logical_id_count': len(grouped_favorites),
            'copy_count': len(copy_operations),
            'missing_count': sum(1 for item in items if item['status'] == 'missing_raw'),
            'ambiguous_count': sum(1 for item in items if item['status'] == 'ambiguous_raw'),
            'already_exists_count': sum(1 for item in items if item['status'] == 'already_exists'),
            'ignored_non_jpg_count': len(ignored_non_jpg),
        },
        'ignored_non_jpg': [Path(rel).name for rel in ignored_non_jpg],
    }


def create_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('workflow_select_raw', __name__)

    def resolve_set(source_id, path_value):
        source = get_source(source_id)
        if not source:
            raise FileNotFoundError('Source 不存在或已禁用')
        root, target, rel = resolve_path(source, path_value)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            raise ValueError('当前目录不是 Set')
        return source, root, target, rel

    def require_set(source_id):
        return resolve_set(source_id, (request.get_json(silent=True) or {}).get('path', ''))

    @bp.route('/api/library/workflow/sources/<int:source_id>/select-raw/preview', methods=['POST'])
    def select_raw_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_select_raw_plan(source_id, set_dir, set_rel, get_db_connection)
            plan_id = remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'items': plan['items'],
                'summary': plan['summary'],
                'ignored_non_jpg': plan['ignored_non_jpg'],
                'destination': '01_Original/Selects',
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/select-raw/start', methods=['POST'])
    def select_raw_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = get_plan(str(data.get('plan_id') or ''), 'select_raw', source_id, set_rel)
            verify_signatures(plan['signatures'])
            operations = list(plan['operations'])
            task_id = new_task('select_raw', len(operations))

            def worker():
                copied = 0
                try:
                    update_task(task_id, status='running', message='开始复制 RAW…')
                    selects_dir = Path(plan['selects_dir'])
                    if operations:
                        selects_dir.mkdir(parents=True, exist_ok=True)
                    for index, item in enumerate(operations, start=1):
                        src = Path(item['src'])
                        dst = Path(item['dst'])
                        if not src.exists():
                            raise FileNotFoundError(f'RAW 不存在: {src.name}')
                        if dst.exists():
                            message = f'{dst.name} 已存在，跳过'
                        else:
                            shutil.copy2(src, dst)
                            copied += 1
                            message = f'{src.name} → 01_Original/Selects/'
                        update_task(task_id, completed=index, current=index, message=message, log=message)
                    update_task(
                        task_id,
                        status='done',
                        message=f'完成：复制 {copied} 个 RAW',
                        result={'copied_count': copied, 'destination': '01_Original/Selects'},
                    )
                except Exception as exc:
                    update_task(task_id, status='error', message='复制 RAW 失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    return bp
