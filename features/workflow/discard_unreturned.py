import shutil
import threading
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.filesystem import list_top_level_images
from core.operations import file_signature, get_plan, new_task, remember_plan, update_task, verify_signatures

_SET_RE = re.compile(r'^\d{8}-.+-.+$')


def _duplicate_stems(paths):
    grouped = {}
    for path in paths:
        grouped.setdefault(path.stem.casefold(), []).append(path)
    return {key: value for key, value in grouped.items() if len(value) > 1}


def _format_duplicate_stem_error(label: str, duplicates):
    examples = []
    for paths in list(duplicates.values())[:4]:
        examples.append(' / '.join(path.name for path in paths))
    detail = '；'.join(examples)
    if len(duplicates) > 4:
        detail += f'; plus {len(duplicates) - 4} groups'
    return f'{label} has duplicate stems; one-to-one matching is ambiguous: {detail}'


def _build_discard_unreturned_base_plan(source_id, set_dir: Path, set_rel: str):
    """Plan moving active Base Edit images that have no same-stem Model Edit.

    This intentionally compares *exact stage stems* rather than logical photo IDs.
    Visual Rename exists specifically to align Model Edit filenames with Base Edit,
    and a stage suffix can represent a distinct version that should not be silently
    treated as equivalent to another version of the same logical photo.

    Only top-level stage images participate. Existing Ready/discards/support folders
    are stateful side paths and must never be swept into this operation recursively.
    """
    base_dir = set_dir / '02_Base_Edit'
    model_dir = set_dir / '03_Model_Edit'
    discards_dir = base_dir / 'discards'
    if not base_dir.is_dir():
        raise ValueError('Base Edit missing')
    if not model_dir.is_dir():
        raise ValueError('Model Edit missing')

    base_files = list_top_level_images(base_dir)
    model_files = list_top_level_images(model_dir)
    if not base_files:
        raise ValueError('No images in Base Edit')
    if not model_files:
        raise ValueError('No images in Model Edit')

    base_duplicates = _duplicate_stems(base_files)
    if base_duplicates:
        raise ValueError(_format_duplicate_stem_error('Base Edit', base_duplicates))
    model_duplicates = _duplicate_stems(model_files)
    if model_duplicates:
        raise ValueError(_format_duplicate_stem_error('Model Edit', model_duplicates))

    model_stems = {path.stem.casefold() for path in model_files}
    files_to_move = [path for path in base_files if path.stem.casefold() not in model_stems]

    items = []
    operations = []
    signatures = {}
    conflict_count = 0
    for path in [*base_files, *model_files]:
        signatures[str(path)] = file_signature(path)

    for path in files_to_move:
        destination = discards_dir / path.name
        status = 'move_to_discards'
        if destination.exists():
            status = 'destination_exists'
            conflict_count += 1
            signatures[str(destination)] = file_signature(destination)
        else:
            operations.append({'src': str(path), 'dst': str(destination)})
        items.append({
            'name': path.name,
            'stem': path.stem,
            'relative_path': f'02_Base_Edit/{path.name}',
            'destination': f'02_Base_Edit/discards/{path.name}',
            'size': path.stat().st_size,
            'status': status,
        })

    return {
        'kind': 'discard_unreturned_base',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'base_dir': str(base_dir),
        'model_dir': str(model_dir),
        'discards_dir': str(discards_dir),
        'operations': operations,
        'items': items,
        'signatures': signatures,
        # Signatures catch edits/removals, while these snapshots also catch files
        # newly added after preview (especially a late Model Edit return).
        'base_names_snapshot': [path.name for path in base_files],
        'model_names_snapshot': [path.name for path in model_files],
        'summary': {
            'base_count': len(base_files),
            'model_count': len(model_files),
            'move_count': len(operations),
            'conflict_count': conflict_count,
            'matched_count': len(base_files) - len(files_to_move),
        },
    }


def create_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('workflow_discard_unreturned', __name__)

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

    @bp.route('/api/library/workflow/sources/<int:source_id>/discard-unreturned/preview', methods=['POST'])
    def discard_unreturned_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_discard_unreturned_base_plan(source_id, set_dir, set_rel)
            plan_id = remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'items': plan['items'],
                'summary': plan['summary'],
                'destination': '02_Base_Edit/discards',
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/discard-unreturned/start', methods=['POST'])
    def discard_unreturned_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = get_plan(str(data.get('plan_id') or ''), 'discard_unreturned_base', source_id, set_rel)
            verify_signatures(plan['signatures'])

            base_dir = Path(plan['base_dir'])
            model_dir = Path(plan['model_dir'])
            current_base_names = [path.name for path in list_top_level_images(base_dir)]
            current_model_names = [path.name for path in list_top_level_images(model_dir)]
            if current_base_names != plan['base_names_snapshot'] or current_model_names != plan['model_names_snapshot']:
                raise RuntimeError('Preview is stale. Run Preview again.')
            if plan['summary'].get('conflict_count', 0):
                raise RuntimeError('Name conflicts in discards. Resolve them and preview again.')

            operations = list(plan['operations'])
            task_id = new_task('discard_unreturned_base', len(operations))

            def worker():
                moved = 0
                try:
                    update_task(task_id, status='running', message='Moving unmatched Base files…')
                    discards_dir = Path(plan['discards_dir'])
                    for item in operations:
                        src = Path(item['src'])
                        dst = Path(item['dst'])
                        if not src.exists():
                            raise FileNotFoundError(f'Base file not found: {src.name}')
                        if dst.exists():
                            raise FileExistsError(f'File already exists in discards: {dst.name}')
                    if operations:
                        discards_dir.mkdir(parents=True, exist_ok=True)

                    for index, item in enumerate(operations, start=1):
                        src = Path(item['src'])
                        dst = Path(item['dst'])

                        old_rel = src.relative_to(root).as_posix()
                        shutil.move(str(src), str(dst))
                        new_rel = dst.relative_to(root).as_posix()

                        conn = get_db_connection()
                        try:
                            conn.execute(
                                'DELETE FROM library_image_states WHERE source_id=? AND relative_path=? AND relative_path<>?',
                                (source_id, new_rel, old_rel),
                            )
                            conn.execute(
                                'UPDATE library_image_states SET relative_path=?, updated_at=CURRENT_TIMESTAMP WHERE source_id=? AND relative_path=?',
                                (new_rel, source_id, old_rel),
                            )
                            conn.commit()
                        finally:
                            conn.close()

                        moved += 1
                        message = f'{src.name} → discards'
                        update_task(task_id, completed=index, current=index, message=message, log=message)

                    update_task(
                        task_id,
                        status='done',
                        message=f'Moved {moved} Base files to discards',
                        result={'moved_count': moved, 'destination': '02_Base_Edit/discards'},
                    )
                except Exception as exc:
                    update_task(task_id, status='error', message='Move failed', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    return bp
