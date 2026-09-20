import json
import math
import os
import platform
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request
from PIL import Image


_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
_SET_RE = __import__('re').compile(r'^\d{8}-.+-.+$')
_PLAN_TTL_SECONDS = 30 * 60
_TASK_TTL_SECONDS = 60 * 60

_PLAN_LOCK = threading.Lock()
_PLANS = {}
_TASK_LOCK = threading.Lock()
_TASKS = {}


def _cleanup_state():
    now = time.time()
    with _PLAN_LOCK:
        stale = [key for key, value in _PLANS.items() if now - value.get('created_at', now) > _PLAN_TTL_SECONDS]
        for key in stale:
            _PLANS.pop(key, None)
    with _TASK_LOCK:
        stale = [key for key, value in _TASKS.items() if now - value.get('updated_at', now) > _TASK_TTL_SECONDS]
        for key in stale:
            _TASKS.pop(key, None)


def _remember_plan(plan):
    _cleanup_state()
    plan_id = uuid.uuid4().hex
    plan['id'] = plan_id
    plan['created_at'] = time.time()
    with _PLAN_LOCK:
        _PLANS[plan_id] = plan
    return plan_id


def _get_plan(plan_id, kind, source_id, set_rel):
    _cleanup_state()
    with _PLAN_LOCK:
        plan = _PLANS.get(plan_id)
    if not plan:
        raise ValueError('预览结果已过期，请重新预览')
    if plan.get('kind') != kind or int(plan.get('source_id')) != int(source_id) or plan.get('set_rel') != set_rel:
        raise ValueError('预览结果与当前操作不匹配，请重新预览')
    return plan


def _new_task(kind, total):
    _cleanup_state()
    task_id = uuid.uuid4().hex
    now = time.time()
    task = {
        'id': task_id,
        'kind': kind,
        'status': 'queued',
        'total': int(total),
        'completed': 0,
        'current': 0,
        'message': '等待开始…',
        'logs': [],
        'result': None,
        'error': None,
        'created_at': now,
        'updated_at': now,
    }
    with _TASK_LOCK:
        _TASKS[task_id] = task
    return task_id


def _update_task(task_id, **changes):
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        log = changes.pop('log', None)
        task.update(changes)
        if log:
            task['logs'].append(str(log))
            task['logs'] = task['logs'][-100:]
        task['updated_at'] = time.time()


def _task_snapshot(task_id):
    _cleanup_state()
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def _safe_case_rename(src: Path, dst: Path):
    src = Path(src)
    dst = Path(dst)
    if str(src) == str(dst):
        return False
    if str(src).lower() == str(dst).lower():
        tmp = src.with_name(f'.__case_tmp__{uuid.uuid4().hex}{src.suffix}')
        src.rename(tmp)
        tmp.rename(dst)
        return True
    if dst.exists():
        raise FileExistsError(f'目标文件已存在: {dst.name}')
    src.rename(dst)
    return True


def _next_available_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f'{path.stem}_{counter}{path.suffix}')
        if not candidate.exists():
            return candidate
        counter += 1


def _file_signature(path: Path):
    st = path.stat()
    return [int(st.st_size), int(st.st_mtime_ns)]


def _verify_signatures(signatures):
    changed = []
    for path_str, signature in signatures.items():
        path = Path(path_str)
        try:
            current = _file_signature(path)
        except OSError:
            changed.append(path.name)
            continue
        if current != signature:
            changed.append(path.name)
    if changed:
        preview = ', '.join(changed[:8])
        if len(changed) > 8:
            preview += f' 等 {len(changed)} 个文件'
        raise RuntimeError(f'文件在预览后发生变化，请重新预览：{preview}')


def _list_top_level_images(directory: Path):
    if not directory.is_dir():
        return []
    # Target order in the original Qt worker was explicitly sorted.
    return sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )


def _list_source_images_original_order(directory: Path):
    if not directory.is_dir():
        return []
    # Preserve the old os.listdir() tie behaviour for source candidates.
    names = os.listdir(directory)
    return [directory / name for name in names if (directory / name).is_file() and Path(name).suffix.lower() in _IMAGE_EXTENSIONS]


def _phash64(path: Path) -> int:
    """Compatible 8x8 perceptual hash used by the old imagehash.phash flow.

    It keeps the same 32x32 grayscale/Lanczos + low-frequency DCT + median rule,
    but is implemented locally so Gallery does not need the imagehash package.
    """
    size = 32
    low = 8
    with Image.open(path) as image:
        image = image.convert('L').resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(image.getdata())

    cos_table = [
        [math.cos(math.pi * (n + 0.5) * k / size) for n in range(size)]
        for k in range(low)
    ]
    coeffs = []
    for ky in range(low):
        cy = cos_table[ky]
        for kx in range(low):
            cx = cos_table[kx]
            total = 0.0
            for y in range(size):
                row_offset = y * size
                y_weight = cy[y]
                subtotal = 0.0
                for x in range(size):
                    subtotal += pixels[row_offset + x] * cx[x]
                total += subtotal * y_weight
            coeffs.append(total)

    ordered = sorted(coeffs)
    median = (ordered[31] + ordered[32]) / 2.0
    bits = 0
    for value in coeffs:
        bits = (bits << 1) | (1 if value > median else 0)
    return bits


def _hash_similarity(a: int, b: int) -> float:
    return 1.0 - ((a ^ b).bit_count() / 64.0)


def _build_visual_rename_plan(source_id, set_dir: Path, set_rel: str, threshold: float):
    base_dir = set_dir / '02_Base_Edit'
    model_dir = set_dir / '03_Model_Edit'
    if not base_dir.is_dir():
        raise ValueError('02_Base_Edit 不存在')
    if not model_dir.is_dir():
        raise ValueError('03_Model_Edit 不存在')

    src_files = _list_source_images_original_order(base_dir)
    target_files = _list_top_level_images(model_dir)
    if not src_files:
        raise ValueError('02_Base_Edit 中没有 JPG/JPEG/PNG')
    if not target_files:
        raise ValueError('03_Model_Edit 中没有 JPG/JPEG/PNG')

    # Optimization only: the Qt version recalculated both hashes for every pair.
    # Computing each image hash once produces the same comparisons and threshold logic.
    src_hashes = {}
    for path in src_files:
        src_hashes[path.name] = _phash64(path)
    target_hashes = {}
    for path in target_files:
        target_hashes[path.name] = _phash64(path)

    candidates = []
    skipped = []
    for target_path in target_files:
        best_match = None
        best_similarity = 0.0
        target_hash = target_hashes[target_path.name]
        for src_path in src_files:
            similarity = _hash_similarity(src_hashes[src_path.name], target_hash)
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = src_path
        if best_match is not None and best_similarity >= threshold:
            new_name = best_match.stem + target_path.suffix
            candidates.append({
                'current_name': target_path.name,
                'requested_name': new_name,
                'source_name': best_match.name,
                'similarity': best_similarity,
            })
        else:
            skipped.append({
                'current_name': target_path.name,
                'new_name': '',
                'source_name': best_match.name if best_match else '',
                'similarity': best_similarity,
                'status': 'below_threshold',
            })

    # Preserve the Qt execution rule: highest similarity is renamed first.
    candidates.sort(key=lambda item: item['similarity'], reverse=True)
    existing_names = {path.name for path in target_files}
    used_names = set()
    operations = []
    for order, item in enumerate(candidates, start=1):
        current_name = item['current_name']
        requested_name = item['requested_name']
        if current_name == requested_name:
            used_names.add(requested_name)
            operations.append({
                **item,
                'new_name': requested_name,
                'status': 'already_named',
                'order': order,
            })
            continue

        final_name = requested_name
        counter = 1
        while final_name in used_names or final_name in existing_names:
            base_name = Path(requested_name).stem
            ext = Path(requested_name).suffix
            final_name = f'{base_name}_{counter}{ext}'
            counter += 1

        used_names.add(final_name)
        existing_names.discard(current_name)
        existing_names.add(final_name)
        operations.append({
            **item,
            'new_name': final_name,
            'status': 'rename',
            'order': order,
        })

    display_by_name = {item['current_name']: item for item in operations}
    display_by_name.update({item['current_name']: item for item in skipped})
    display_items = [display_by_name[path.name] for path in target_files]

    signatures = {}
    for path in [*src_files, *target_files]:
        signatures[str(path)] = _file_signature(path)

    return {
        'kind': 'visual_rename',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'threshold': float(threshold),
        'base_dir': str(base_dir),
        'model_dir': str(model_dir),
        'operations': operations,
        'display_items': display_items,
        'signatures': signatures,
        'summary': {
            'base_count': len(src_files),
            'model_count': len(target_files),
            'rename_count': sum(1 for item in operations if item['status'] == 'rename'),
            'already_named_count': sum(1 for item in operations if item['status'] == 'already_named'),
            'skipped_count': len(skipped),
        },
    }


def _scan_by_stem(directory: Path, extensions, excluded_dir_names=None):
    excluded = {name.casefold() for name in (excluded_dir_names or [])}
    result = {}
    duplicates = {}
    if not directory.is_dir():
        return result, duplicates
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d.casefold() not in excluded]
        for filename in files:
            if not filename.lower().endswith(extensions):
                continue
            path = Path(root) / filename
            key = path.stem  # Keep the original Qt case-sensitive stem logic.
            if key in result:
                duplicates.setdefault(key, [result[key]]).append(path)
            result[key] = path
    return result, duplicates


def _build_sync_plan(source_id, set_dir: Path, set_rel: str, direction: str):
    jpg_dir = set_dir / '01_Original' / 'JPG'
    raw_dir = set_dir / '01_Original' / 'RAW'
    if not jpg_dir.is_dir() or not raw_dir.is_dir():
        raise ValueError('01_Original/JPG 或 01_Original/RAW 不存在')

    if direction == 'raw_by_jpg':
        target_type = 'RAW'
        reference_type = 'JPG'
        target_dir = raw_dir
        reference_dir = jpg_dir
        target_ext = ('.cr3',)
        ref_ext = ('.jpg', '.jpeg')
    elif direction == 'jpg_by_raw':
        target_type = 'JPG'
        reference_type = 'RAW'
        target_dir = jpg_dir
        reference_dir = raw_dir
        target_ext = ('.jpg', '.jpeg')
        ref_ext = ('.cr3',)
    else:
        raise ValueError('同步方向不合法')

    target_files, target_duplicates = _scan_by_stem(target_dir, target_ext, {'Deleted', 'Selects'})
    reference_files, reference_duplicates = _scan_by_stem(reference_dir, ref_ext, {'Deleted', 'Selects'})
    files_to_move = [path for key, path in target_files.items() if key not in reference_files]
    files_to_move.sort(key=lambda p: str(p).casefold())

    output_dir = target_dir / 'Deleted'
    preexisting_deleted = []
    if output_dir.is_dir():
        preexisting_deleted = sorted(
            [p for p in output_dir.rglob('*') if p.is_file()],
            key=lambda p: str(p).casefold(),
        )

    signatures = {}
    for path in [*target_files.values(), *reference_files.values(), *preexisting_deleted]:
        signatures[str(path)] = _file_signature(path)

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
            *( [f'{target_type} 中有 {len(target_duplicates)} 个重复 stem；保持旧逻辑，按扫描到的最后一个文件参与同步。'] if target_duplicates else [] ),
            *( [f'{reference_type} 中有 {len(reference_duplicates)} 个重复 stem；保持旧逻辑，按扫描到的最后一个文件参与匹配。'] if reference_duplicates else [] ),
        ],
    }


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
            signatures[str(path)] = _file_signature(path)

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
    raise RuntimeError('无法调用系统回收站，文件已保留在 Deleted 目录，未执行永久删除' + (f'：{detail}' if detail else ''))


def create_workflow_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('workflow_tools', __name__)

    def require_set(source_id):
        source = get_source(source_id)
        if not source:
            raise FileNotFoundError('Source 不存在或已禁用')
        root, target, rel = resolve_path(source, (request.get_json(silent=True) or {}).get('path', ''))
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            raise ValueError('当前目录不是 Set')
        return source, root, target, rel

    @bp.route('/api/library/workflow/sources/<int:source_id>/visual-rename/preview', methods=['POST'])
    def visual_rename_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            threshold = float(data.get('threshold', 0.8))
            if not 0.0 <= threshold <= 1.0:
                raise ValueError('相似度阈值必须在 0.0–1.0 之间')
            plan = _build_visual_rename_plan(source_id, set_dir, set_rel, threshold)
            plan_id = _remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'threshold': threshold,
                'items': plan['display_items'],
                'summary': plan['summary'],
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/visual-rename/start', methods=['POST'])
    def visual_rename_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), 'visual_rename', source_id, set_rel)
            _verify_signatures(plan['signatures'])
            operations = list(plan['operations'])
            task_id = _new_task('visual_rename', len(operations))

            def worker():
                renamed = 0
                unchanged = 0
                try:
                    _update_task(task_id, status='running', message='开始重命名…')
                    model_dir = Path(plan['model_dir'])
                    for index, item in enumerate(operations, start=1):
                        current = model_dir / item['current_name']
                        destination = model_dir / item['new_name']
                        if item['status'] == 'already_named':
                            unchanged += 1
                            message = f"{item['current_name']} 已是目标文件名"
                        else:
                            if not current.exists():
                                raise FileNotFoundError(f'文件不存在: {item["current_name"]}')
                            old_rel = current.relative_to(root).as_posix()
                            _safe_case_rename(current, destination)
                            new_rel = destination.relative_to(root).as_posix()
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
                            renamed += 1
                            message = f"{item['current_name']} → {item['new_name']} · {item['similarity']:.3f}"
                        _update_task(
                            task_id,
                            completed=index,
                            current=index,
                            message=message,
                            log=message,
                        )
                    result = {'renamed_count': renamed, 'unchanged_count': unchanged}
                    _update_task(task_id, status='done', message=f'完成：重命名 {renamed} 个', result=result)
                except Exception as exc:
                    _update_task(task_id, status='error', message='重命名失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

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
            plan_id = _remember_plan(plan)
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
            plan = _get_plan(str(data.get('plan_id') or ''), 'sync_originals', source_id, set_rel)
            _verify_signatures(plan['signatures'])
            paths = [Path(path) for path in plan['files_to_move']]
            task_id = _new_task('sync_originals', len(paths))

            def worker():
                moved = 0
                output_dir = Path(plan['target_dir']) / 'Deleted'
                try:
                    _update_task(task_id, status='running', message='开始移动到 Deleted…')
                    if paths:
                        output_dir.mkdir(parents=True, exist_ok=True)
                    for index, file_path in enumerate(paths, start=1):
                        if not file_path.exists():
                            raise FileNotFoundError(f'文件不存在: {file_path.name}')
                        dest = _next_available_path(output_dir / file_path.name)
                        shutil.move(str(file_path), str(dest))
                        moved += 1
                        message = f'{file_path.name} → Deleted/{dest.name}'
                        _update_task(task_id, completed=index, current=index, message=message, log=message)

                    trashed = False
                    trash_error = None
                    if output_dir.exists() and moved > 0:
                        _update_task(task_id, message='正在将 Deleted 移入系统回收站…', log='文件移动完成，准备移入系统回收站')
                        try:
                            _move_to_trash(output_dir)
                            trashed = True
                        except Exception as exc:
                            trash_error = str(exc)
                            _update_task(task_id, log=trash_error)

                    result = {
                        'moved_count': moved,
                        'trashed': trashed,
                        'trash_error': trash_error,
                        'deleted_dir': str(output_dir),
                    }
                    if trash_error:
                        _update_task(
                            task_id,
                            status='done',
                            message=f'已移动 {moved} 个文件；回收站操作失败，文件仍保留在 Deleted',
                            result=result,
                        )
                    else:
                        _update_task(task_id, status='done', message=f'完成：处理 {moved} 个文件', result=result)
                except Exception as exc:
                    _update_task(task_id, status='error', message='同步失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/workflow/sources/<int:source_id>/select-raw/preview', methods=['POST'])
    def select_raw_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_select_raw_plan(source_id, set_dir, set_rel, get_db_connection)
            plan_id = _remember_plan(plan)
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
            plan = _get_plan(str(data.get('plan_id') or ''), 'select_raw', source_id, set_rel)
            _verify_signatures(plan['signatures'])
            operations = list(plan['operations'])
            task_id = _new_task('select_raw', len(operations))

            def worker():
                copied = 0
                try:
                    _update_task(task_id, status='running', message='开始复制 RAW…')
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
                        _update_task(task_id, completed=index, current=index, message=message, log=message)
                    _update_task(
                        task_id,
                        status='done',
                        message=f'完成：复制 {copied} 个 RAW',
                        result={'copied_count': copied, 'destination': '01_Original/Selects'},
                    )
                except Exception as exc:
                    _update_task(task_id, status='error', message='复制 RAW 失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/workflow/tasks/<task_id>', methods=['GET'])
    def workflow_task_status(task_id):
        denied = admin_guard()
        if denied:
            return denied
        task = _task_snapshot(task_id)
        if not task:
            return jsonify({'error': '任务不存在或已过期'}), 404
        task.pop('created_at', None)
        task.pop('updated_at', None)
        total = max(0, int(task.get('total') or 0))
        completed = max(0, int(task.get('completed') or 0))
        task['percent'] = 100 if task.get('status') == 'done' and total == 0 else (round(completed * 100 / total) if total else 0)
        return jsonify(task)

    return bp
