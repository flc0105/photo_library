import os
import stat
import threading
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.filesystem import list_top_level_images, original_stem_key
from core.operations import file_signature, get_plan, new_task, remember_plan, update_task, verify_signatures

_SET_RE = re.compile(r'^\d{8}-.+-.+$')


def _user_immutable_mask():
    mask = getattr(stat, 'UF_IMMUTABLE', None)
    if mask is None or not hasattr(os, 'chflags'):
        raise RuntimeError('Protect Originals requires macOS/BSD user immutable flags')
    return int(mask)


def _file_is_user_immutable(path: Path) -> bool:
    """Read the real filesystem protection state; no database mirror is kept."""
    flags = getattr(path.stat(), 'st_flags', None)
    if flags is None:
        raise RuntimeError('Filesystem cannot read user immutable flags')
    return bool(int(flags) & _user_immutable_mask())


def _ensure_user_immutable(path: Path):
    """Add UF_IMMUTABLE while preserving every other existing file flag."""
    st = path.stat()
    flags = getattr(st, 'st_flags', None)
    if flags is None:
        raise RuntimeError('Filesystem cannot read user immutable flags')
    os.chflags(path, int(flags) | _user_immutable_mask())


def _clear_user_immutable(path: Path):
    """Remove only UF_IMMUTABLE while preserving every other existing file flag."""
    st = path.stat()
    flags = getattr(st, 'st_flags', None)
    if flags is None:
        raise RuntimeError('Filesystem cannot read user immutable flags')
    os.chflags(path, int(flags) & ~_user_immutable_mask())


def _direct_files_with_extensions(directory: Path, extensions):
    if not directory.is_dir():
        return []
    allowed = {str(ext).casefold() for ext in extensions}
    return sorted(
        [
            path
            for path in directory.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() in allowed
        ],
        key=lambda path: path.name.casefold(),
    )


def _protect_originals_snapshot(set_dir: Path):
    paths = {
        'base': list_top_level_images(set_dir / '02_Base_Edit'),
        'model': list_top_level_images(set_dir / '03_Model_Edit'),
        'jpg': _direct_files_with_extensions(set_dir / '01_Original' / 'JPG', ('.jpg', '.jpeg')),
        'raw': _direct_files_with_extensions(set_dir / '01_Original' / 'RAW', ('.cr3',)),
    }
    return {
        key: [path.name for path in values]
        for key, values in paths.items()
    }, paths


def _build_protect_originals_plan(source_id, set_dir: Path, set_rel: str):
    """Protect matching Original JPG/RAW and expose every protected Original for reset."""
    _user_immutable_mask()
    snapshot, paths = _protect_originals_snapshot(set_dir)
    downstream_stages = {}
    for stage_label, stage_files in (('Base', paths['base']), ('Model', paths['model'])):
        for path in stage_files:
            downstream_stages.setdefault(path.stem.casefold(), set()).add(stage_label)

    original_files = paths['jpg'] + paths['raw']
    protection_snapshot = {
        path.relative_to(set_dir).as_posix(): _file_is_user_immutable(path)
        for path in original_files
    }
    signatures = {str(path): file_signature(path) for path in original_files}

    candidates = []
    protected_originals = []
    for kind, original_group, is_jpg in (
        ('JPG', paths['jpg'], True),
        ('RAW', paths['raw'], False),
    ):
        for path in original_group:
            relative_path = path.relative_to(set_dir).as_posix()
            protected = protection_snapshot[relative_path]
            if protected:
                protected_originals.append(str(path))

            stem_key = original_stem_key(path, is_jpg=is_jpg)
            matched = downstream_stages.get(stem_key)
            if not matched:
                continue
            candidates.append({
                'path': str(path),
                'name': path.name,
                'relative_path': relative_path,
                'kind': kind,
                'stem': path.stem[:-4] if is_jpg and path.stem.casefold().endswith('-dpp') else path.stem,
                'matched_stages': sorted(matched),
                'size': int(path.stat().st_size),
                'status': 'protected' if protected else 'protect',
            })

    candidates.sort(key=lambda item: (item['stem'].casefold(), item['kind'], item['name'].casefold()))
    protected_count = sum(1 for item in candidates if item['status'] == 'protected')
    jpg_count = sum(1 for item in candidates if item['kind'] == 'JPG')
    raw_count = sum(1 for item in candidates if item['kind'] == 'RAW')
    candidate_count = len(candidates)
    protected_original_count = len(protected_originals)
    return {
        'kind': 'protect_originals',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'items': candidates,
        'paths': [item['path'] for item in candidates],
        'unprotect_paths': protected_originals,
        'signatures': signatures,
        'snapshot': snapshot,
        'protection_snapshot': protection_snapshot,
        'summary': {
            'candidate_count': candidate_count,
            'jpg_count': jpg_count,
            'raw_count': raw_count,
            'protected_count': protected_count,
            'unprotected_count': candidate_count - protected_count,
            'protected_original_count': protected_original_count,
            'all_protected': candidate_count > 0 and protected_count == candidate_count,
        },
    }


def _verify_protect_originals_snapshot(plan, set_dir: Path):
    current_snapshot, current_paths = _protect_originals_snapshot(set_dir)
    if current_snapshot != plan.get('snapshot'):
        raise RuntimeError('Preview is stale. Run Preview again.')
    current_protection_snapshot = {
        path.relative_to(set_dir).as_posix(): _file_is_user_immutable(path)
        for path in (current_paths['jpg'] + current_paths['raw'])
    }
    if current_protection_snapshot != plan.get('protection_snapshot'):
        raise RuntimeError('Protection changed after Preview. Run Preview again.')


def create_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('workflow_protect_originals', __name__)

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

    @bp.route('/api/library/workflow/sources/<int:source_id>/protect-originals/status', methods=['POST'])
    def protect_originals_status(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_protect_originals_plan(source_id, set_dir, set_rel)
            return jsonify({'summary': plan['summary']})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/protect-originals/preview', methods=['POST'])
    def protect_originals_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_protect_originals_plan(source_id, set_dir, set_rel)
            plan_id = remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'items': plan['items'],
                'summary': plan['summary'],
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/protect-originals/start', methods=['POST'])
    def protect_originals_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            action = str(data.get('action') or 'protect').strip().casefold()
            if action not in {'protect', 'unprotect'}:
                raise ValueError('Invalid protection action')

            plan = get_plan(str(data.get('plan_id') or ''), 'protect_originals', source_id, set_rel)
            verify_signatures(plan['signatures'])
            _verify_protect_originals_snapshot(plan, set_dir)
            paths = [Path(path) for path in (plan['paths'] if action == 'protect' else plan['unprotect_paths'])]
            if not paths:
                if action == 'protect':
                    raise ValueError('No Original files to protect')
                raise ValueError('No protected Original files')
            task_id = new_task('protect_originals', len(paths))

            def worker():
                try:
                    if action == 'protect':
                        newly_protected = 0
                        already_protected = 0
                        update_task(task_id, status='running', message='Protecting Originals…')
                        for index, path in enumerate(paths, start=1):
                            if not path.is_file():
                                raise FileNotFoundError(f'Original file not found: {path.name}')
                            was_protected = _file_is_user_immutable(path)
                            _ensure_user_immutable(path)
                            if not _file_is_user_immutable(path):
                                raise RuntimeError(f'Protection failed to apply: {path.name}')
                            if was_protected:
                                already_protected += 1
                            else:
                                newly_protected += 1
                            message = f'{path.name} · Protected'
                            update_task(task_id, completed=index, current=index, message=message, log=message)
                        result = {
                            'action': action,
                            'protected_count': len(paths),
                            'newly_protected_count': newly_protected,
                            'already_protected_count': already_protected,
                        }
                        update_task(
                            task_id,
                            status='done',
                            message=f'Protected {len(paths)} Original files',
                            result=result,
                        )
                    else:
                        update_task(task_id, status='running', message='Removing protection…')
                        for index, path in enumerate(paths, start=1):
                            if not path.is_file():
                                raise FileNotFoundError(f'Original file not found: {path.name}')
                            _clear_user_immutable(path)
                            if _file_is_user_immutable(path):
                                raise RuntimeError(f'Unprotect failed: {path.name}')
                            message = f'{path.name} · Unprotected'
                            update_task(task_id, completed=index, current=index, message=message, log=message)
                        result = {
                            'action': action,
                            'unprotected_count': len(paths),
                        }
                        update_task(
                            task_id,
                            status='done',
                            message=f'Unprotected {len(paths)} Original files',
                            result=result,
                        )
                except Exception as exc:
                    message = 'Protect failed' if action == 'protect' else 'Unprotect failed'
                    update_task(task_id, status='error', message=message, error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    return bp
