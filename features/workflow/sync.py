import os
import platform
import shutil
import subprocess
import threading
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.filesystem import original_stem_key
from core.operations import file_signature, get_plan, new_task, next_available_path, remember_plan, update_task, verify_signatures

_SET_RE = re.compile(r'^\d{8}-.+-.+$')


def _scan_by_stem(directory: Path, extensions, excluded_dir_names=None, *, is_jpg=False):
    excluded = {name.casefold() for name in (excluded_dir_names or [])}
    grouped = {}
    if not directory.is_dir():
        return grouped, {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d.casefold() not in excluded]
        for filename in files:
            if not filename.lower().endswith(extensions):
                continue
            path = Path(root) / filename
            key = original_stem_key(path, is_jpg=is_jpg)
            grouped.setdefault(key, []).append(path)
    duplicates = {key: paths for key, paths in grouped.items() if len(paths) > 1}
    return grouped, duplicates


def _build_sync_plan(source_id, set_dir: Path, set_rel: str, direction: str):
    jpg_dir = set_dir / '01_Original' / 'JPG'
    raw_dir = set_dir / '01_Original' / 'RAW'
    if not jpg_dir.is_dir() or not raw_dir.is_dir():
        raise ValueError('Original JPG or RAW folder missing')

    if direction == 'raw_by_jpg':
        target_type = 'RAW'
        reference_type = 'JPG'
        target_dir = raw_dir
        reference_dir = jpg_dir
        target_ext = ('.cr3',)
        ref_ext = ('.jpg', '.jpeg')
        target_is_jpg = False
        reference_is_jpg = True
    elif direction == 'jpg_by_raw':
        target_type = 'JPG'
        reference_type = 'RAW'
        target_dir = jpg_dir
        reference_dir = raw_dir
        target_ext = ('.jpg', '.jpeg')
        ref_ext = ('.cr3',)
        target_is_jpg = True
        reference_is_jpg = False
    else:
        raise ValueError('Invalid sync direction')

    target_groups, target_duplicates = _scan_by_stem(
        target_dir,
        target_ext,
        {'Deleted', 'Selects'},
        is_jpg=target_is_jpg,
    )
    reference_groups, reference_duplicates = _scan_by_stem(
        reference_dir,
        ref_ext,
        {'Deleted', 'Selects'},
        is_jpg=reference_is_jpg,
    )
    target_files = [path for paths in target_groups.values() for path in paths]
    reference_files = [path for paths in reference_groups.values() for path in paths]
    files_to_move = [
        path
        for key, paths in target_groups.items()
        if key not in reference_groups
        for path in paths
    ]
    files_to_move.sort(key=lambda p: str(p).casefold())

    output_dir = target_dir / 'Deleted'
    preexisting_deleted = []
    if output_dir.is_dir():
        preexisting_deleted = sorted(
            [p for p in output_dir.rglob('*') if p.is_file()],
            key=lambda p: str(p).casefold(),
        )

    signatures = {}
    for path in [*target_files, *reference_files, *preexisting_deleted]:
        signatures[str(path)] = file_signature(path)

    return {
        'kind': 'sync_originals',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'direction': direction,
        'target_type': target_type,
        'reference_type': reference_type,
        'target_dir': str(target_dir),
        'reference_dir': str(reference_dir),
        'files_to_move': [str(path) for path in files_to_move],
        'signatures': signatures,
        'items': [
            *[
                {
                    'name': path.name,
                    'relative_path': path.relative_to(target_dir).as_posix(),
                    'size': path.stat().st_size,
                    'status': 'move_to_deleted',
                }
                for path in files_to_move
            ],
            *[
                {
                    'name': path.name,
                    'relative_path': path.relative_to(target_dir).as_posix(),
                    'size': path.stat().st_size,
                    'status': 'already_in_deleted',
                }
                for path in preexisting_deleted
            ],
        ],
        'summary': {
            'target_count': len(target_files),
            'reference_count': len(reference_files),
            'move_count': len(files_to_move),
            'already_deleted_count': len(preexisting_deleted),
            'trash_count': len(files_to_move) + len(preexisting_deleted),
            'target_duplicate_stems': len(target_duplicates),
            'reference_duplicate_stems': len(reference_duplicates),
        },
        'warnings': [
            *(
                [f'{target_type} has {len(target_duplicates)} duplicate Original stems; matched by stem without tie-breaking.']
                if target_duplicates else []
            ),
            *(
                [f'{reference_type} has {len(reference_duplicates)} duplicate Original stems; matched by stem.']
                if reference_duplicates else []
            ),
        ],
    }


def _move_to_trash(path: Path):
    """Move path to the OS trash/recycle bin. Never falls back to permanent deletion."""
    path = Path(path)
    if not path.exists():
        return

    system = platform.system().lower()
    errors = []

    if system == 'darwin' and shutil.which('osascript'):
        script = (
            'on run argv\n'
            'tell application "Finder" to delete POSIX file (item 1 of argv)\n'
            'end run'
        )
        result = subprocess.run(['osascript', '-e', script, str(path)], capture_output=True, text=True)
        if result.returncode == 0:
            return
        errors.append(result.stderr.strip() or result.stdout.strip() or 'osascript failed')

    if system == 'linux' and shutil.which('gio'):
        result = subprocess.run(['gio', 'trash', str(path)], capture_output=True, text=True)
        if result.returncode == 0:
            return
        errors.append(result.stderr.strip() or result.stdout.strip() or 'gio trash failed')

    if system == 'windows' and shutil.which('powershell'):
        ps = (
            'Add-Type -AssemblyName Microsoft.VisualBasic; '
            '[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory('
            '$args[0], '
            '[Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, '
            '[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)'
        )
        result = subprocess.run(['powershell', '-NoProfile', '-Command', ps, str(path)], capture_output=True, text=True)
        if result.returncode == 0:
            return
        errors.append(result.stderr.strip() or result.stdout.strip() or 'PowerShell recycle failed')

    # Optional compatibility with an already-installed package; never require it.
    try:
        import send2trash  # type: ignore
        send2trash.send2trash(str(path))
        return
    except Exception as exc:
        errors.append(str(exc))

    detail = '; '.join(error for error in errors if error)
    raise RuntimeError('Trash unavailable; files remain in Deleted. No permanent delete.' + (f': {detail}' if detail else ''))


def create_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('workflow_sync', __name__)

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

    @bp.route('/api/library/workflow/sources/<int:source_id>/sync/preview', methods=['POST'])
    def sync_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            direction = str(data.get('direction') or '')
            plan = _build_sync_plan(source_id, set_dir, set_rel, direction)
            plan_id = remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'direction': direction,
                'items': plan['items'],
                'summary': plan['summary'],
                'warnings': plan['warnings'],
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/sync/start', methods=['POST'])
    def sync_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = get_plan(str(data.get('plan_id') or ''), 'sync_originals', source_id, set_rel)
            verify_signatures(plan['signatures'])
            paths = [Path(path) for path in plan['files_to_move']]
            task_id = new_task('sync_originals', len(paths))

            def worker():
                moved = 0
                output_dir = Path(plan['target_dir']) / 'Deleted'
                try:
                    update_task(task_id, status='running', message='Moving to Deleted…')
                    if paths:
                        output_dir.mkdir(parents=True, exist_ok=True)
                    for index, file_path in enumerate(paths, start=1):
                        if not file_path.exists():
                            raise FileNotFoundError(f'File not found: {file_path.name}')
                        dest = next_available_path(output_dir / file_path.name)
                        shutil.move(str(file_path), str(dest))
                        moved += 1
                        message = f'{file_path.name} → Deleted/{dest.name}'
                        update_task(task_id, completed=index, current=index, message=message, log=message)

                    trashed = False
                    trash_error = None
                    if output_dir.exists() and moved > 0:
                        update_task(task_id, message='Moving Deleted to Trash…', log='Files moved; sending Deleted to Trash')
                        try:
                            _move_to_trash(output_dir)
                            trashed = True
                        except Exception as exc:
                            trash_error = str(exc)
                            update_task(task_id, log=trash_error)

                    result = {
                        'moved_count': moved,
                        'trashed': trashed,
                        'trash_error': trash_error,
                        'deleted_dir': str(output_dir),
                    }
                    if trash_error:
                        update_task(
                            task_id,
                            status='done',
                            message=f'Moved {moved} files; Trash failed. Files remain in Deleted.',
                            result=result,
                        )
                    else:
                        update_task(task_id, status='done', message=f'Processed {moved} files', result=result)
                except Exception as exc:
                    update_task(task_id, status='error', message='Sync failed', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    return bp
