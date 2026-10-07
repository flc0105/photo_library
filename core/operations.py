import threading
import time
import uuid
from pathlib import Path

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
        raise ValueError('Preview expired. Preview again.')
    if plan.get('kind') != kind or int(plan.get('source_id')) != int(source_id) or plan.get('set_rel') != set_rel:
        raise ValueError('Preview mismatch. Preview again.')
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
        'message': 'Ready.',
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
        raise FileExistsError(f'Destination exists: {dst.name}')
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
            preview += f' and {len(changed)} files total'
        raise RuntimeError(f'Files changed after preview. Preview again: {preview}')

# Public aliases keep feature modules readable without changing behavior.
remember_plan = _remember_plan
get_plan = _get_plan
new_task = _new_task
update_task = _update_task
task_snapshot = _task_snapshot
file_signature = _file_signature
verify_signatures = _verify_signatures
safe_case_rename = _safe_case_rename
next_available_path = _next_available_path


def create_operations_blueprint(admin_guard):
    from flask import Blueprint, jsonify

    bp = Blueprint('workflow_operations', __name__)

    @bp.route('/api/library/workflow/tasks/<task_id>', methods=['GET'])
    def workflow_task_status(task_id):
        denied = admin_guard()
        if denied:
            return denied
        task = task_snapshot(task_id)
        if not task:
            return jsonify({'error': 'Task not found or expired.'}), 404
        task.pop('created_at', None)
        task.pop('updated_at', None)
        total = max(0, int(task.get('total') or 0))
        completed = max(0, int(task.get('completed') or 0))
        task['percent'] = 100 if task.get('status') == 'done' and total == 0 else (round(completed * 100 / total) if total else 0)
        return jsonify(task)

    return bp
