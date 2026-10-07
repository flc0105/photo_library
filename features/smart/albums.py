import copy
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.external_tools import probe_exiftool_version, resolve_exiftool

from features.smart.runtime import custom_helper_docs, run_query
from features.smart.helpers import read_custom_helpers_source
from features.smart.index import (
    SMART_ALBUM_CAPTURE_METADATA_SOURCE, SMART_ALBUM_DB_FILENAME,
    SMART_ALBUM_QUERY_TIMEOUT_SECONDS, _STAGE_DEFS,
    SmartAlbumIndexCancelled, _apply_sync_changes, _asset_payloads, _build_sync_plan,
    _connect, _index_status, _init_smart_db, _new_index_steps, _new_sync_steps,
    _now_iso, _prepare_sync_rows, _public_sync_plan, _refresh_index,
    _selected_enabled_sources, _verify_sync_plan,
)


DEFAULT_QUERY_CODE = """result = [\n    photo\n    for photo in photos\n    if photo.state.favorite\n]\n"""

PHOTO_CONTRACT_GROUPS = [
    {'label': 'Identity', 'fields': ['photo.id', 'photo.stage', 'photo.logical_id']},
    {'label': 'Origin / Source', 'fields': ['photo.origin.kind', 'photo.source.id', 'photo.source.name']},
    {'label': 'File', 'fields': [
        'photo.file.name', 'photo.file.path', 'photo.file.extension', 'photo.file.size', 'photo.file.mtime',
    ]},
    {'label': 'Image', 'fields': [
        'photo.image.width', 'photo.image.height', 'photo.image.aspect_ratio', 'photo.image.orientation',
        'photo.image.mode', 'photo.image.color_space', 'photo.image.color_space_status', 'photo.image.bit_depth',
    ]},
    {'label': 'State', 'fields': ['photo.state.favorite', 'photo.state.description']},
    {'label': 'EXIF / Capture', 'fields': [
        'photo.exif', 'photo.capture.exif', 'photo.capture.time', 'photo.capture.camera',
        'photo.capture.lens', 'photo.capture.focal_length_mm', 'photo.capture.iso', 'photo.capture.gps',
        'photo.capture.gps.lat', 'photo.capture.gps.lng',
    ]},
    {'label': 'Set / Manifest', 'fields': ['photo.set.name', 'photo.set.path', 'photo.set.manifest']},
]

SMART_ALBUM_HELP_EXAMPLE = '''result = [
    photo
    for photo in photos
    if photo.state.favorite
]
'''


def _album_row(conn, album_id):
    return conn.execute('SELECT * FROM smart_albums WHERE id=?', (album_id,)).fetchone()


def _album_dict(row):
    if not row:
        return None
    data = dict(row)
    data['type'] = 'smart'
    return data


def _run_album_query(smart_db_path, main_db_path, album_row):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')

    payloads, result_rows = _asset_payloads(smart_db_path, main_db_path)
    ordered_ids = run_query(
        album_row['python_code'],
        payloads,
        read_custom_helpers_source(),
        timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    )
    results = []
    for photo_id in ordered_ids:
        row = result_rows.get(photo_id)
        if row:
            row = dict(row)
            row['smart_album_id'] = album_row['id']
            results.append(row)

    conn = _connect(smart_db_path)
    conn.execute(
        'UPDATE smart_albums SET last_result_count=?, last_run_at=? WHERE id=?',
        (len(results), _now_iso(), album_row['id']),
    )
    conn.commit()
    conn.close()
    return results, _index_status(smart_db_path)


def create_smart_album_blueprint(admin_guard, main_db_path):
    """Create the removable Smart Album module.

    Smart Album definitions and the derived index live in a dedicated SQLite
    database beside the main Gallery DB. The main DB remains the source for
    Library Source configuration and live favorite/description state.
    """
    main_db_path = Path(main_db_path).resolve()
    smart_db_path = main_db_path.parent / SMART_ALBUM_DB_FILENAME
    _init_smart_db(smart_db_path)
    bp = Blueprint('smart_albums', __name__)

    # Index refresh is intentionally isolated inside the Smart Album module.
    # Progress is process-local because this Gallery is a personal/local app;
    # restarting the app simply clears the transient progress state, not the index.
    index_job_lock = threading.Lock()
    index_job = {
        'active': False,
        'phase': 'idle',
        'message': '',
        'overall': {'dimension': 'image', 'label': 'Overall', 'current': 0, 'total': 0, 'percent': 0, 'ready': False},
        'steps': _new_index_steps(),
        'summary': {
            'overall_dimension': 'image',
            'source_total': 0,
            'source_available': 0,
            'selected_source_ids': [],
            'selected_source_names': [],
            'set_total': 0,
            'asset_total': 0,
            'stage_counts': {stage: 0 for stage, _ in _STAGE_DEFS},
            'manifest_mode': 'live',
            'state_mode': 'live',
            'originals_indexed': any(stage == 'original_jpg' for stage, _ in _STAGE_DEFS),
            'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
        },
        'error': '',
        'cancel_requested': False,
        'index': None,
    }

    def index_job_snapshot():
        with index_job_lock:
            return copy.deepcopy(index_job)

    def update_index_job(**changes):
        with index_job_lock:
            index_job.update(changes)

    def index_cancel_requested():
        with index_job_lock:
            return bool(index_job.get('cancel_requested'))

    def publish_index_progress(progress):
        with index_job_lock:
            if index_job.get('cancel_requested'):
                progress = dict(progress)
                progress['phase'] = 'cancelling'
                progress['message'] = 'Cancelling rebuild…'
            index_job.update(progress)

    def run_index_refresh_job(selected_source_ids):
        try:
            status = _refresh_index(
                smart_db_path,
                main_db_path,
                source_ids=selected_source_ids,
                progress_callback=publish_index_progress,
                cancel_callback=index_cancel_requested,
            )
            snapshot = index_job_snapshot()
            overall = dict(snapshot.get('overall') or {})
            overall.update({'current': status['asset_count'], 'total': status['asset_count'], 'percent': 100, 'ready': True})
            update_index_job(
                active=False,
                phase='done',
                message=f"Indexed {status['asset_count']} Photos.",
                overall=overall,
                error='',
                cancel_requested=False,
                index={**status, 'ready': True},
            )
        except SmartAlbumIndexCancelled:
            snapshot = index_job_snapshot()
            cancelled_steps = copy.deepcopy(snapshot.get('steps') or [])
            for step in cancelled_steps:
                if step.get('status') == 'active':
                    step['status'] = 'cancelled'
                    step['detail'] = (step.get('detail') or '') + ' (Cancelled)'
            update_index_job(
                active=False,
                phase='cancelled',
                message='Rebuild cancelled.',
                steps=cancelled_steps,
                error='',
                cancel_requested=False,
                index=_index_status(smart_db_path),
            )
        except Exception as exc:
            snapshot = index_job_snapshot()
            failed_steps = copy.deepcopy(snapshot.get('steps') or [])
            for step in failed_steps:
                if step.get('status') == 'active':
                    step['status'] = 'error'
            update_index_job(
                active=False,
                phase='error',
                message='Smart View index rebuild failed.',
                steps=failed_steps,
                error=str(exc),
                cancel_requested=False,
                index=_index_status(smart_db_path),
            )

    # Incremental sync is a separate workflow from the proven full rebuild above.
    # It never changes the full-rebuild task state or its progress contract.
    sync_job_lock = threading.Lock()
    sync_plans = {}
    sync_job = {
        'active': False,
        'phase': 'idle',
        'message': '',
        'percent': 0,
        'current': 0,
        'total': 0,
        'steps': _new_sync_steps(),
        'summary': {},
        'error': '',
        'cancel_requested': False,
        'plan': None,
        'index': None,
    }

    def sync_job_snapshot():
        with sync_job_lock:
            return copy.deepcopy(sync_job)

    def update_sync_job(**changes):
        with sync_job_lock:
            sync_job.update(changes)

    def sync_cancel_requested():
        with sync_job_lock:
            return bool(sync_job.get('cancel_requested'))

    def run_sync_job(plan_id):
        with sync_job_lock:
            plan = sync_plans.get(plan_id)
        if not plan:
            update_sync_job(
                active=False,
                phase='error',
                message='Sync plan unavailable. Scan again.',
                error='Sync plan unavailable. Scan again.',
                cancel_requested=False,
            )
            return

        steps = _new_sync_steps()
        step_map = {step['id']: step for step in steps}
        public_plan = _public_sync_plan(plan)
        change_total = (
            plan['summary']['added_count'] +
            plan['summary']['changed_count'] +
            plan['summary']['deleted_count']
        )

        def publish(phase, message, *, current=None, total=None, percent=None):
            if current is None:
                current = sync_job_snapshot().get('current', 0)
            if total is None:
                total = sync_job_snapshot().get('total', change_total)
            if percent is None:
                percent = int(round((100.0 * current / total))) if total else 0
            update_sync_job(
                active=True,
                phase=phase,
                message=message,
                current=int(current),
                total=int(total),
                percent=max(0, min(99, int(percent))),
                steps=copy.deepcopy(steps),
                summary=copy.deepcopy(plan['summary']),
                plan=public_plan,
            )

        try:
            verify_step = step_map['verify_plan']
            verify_step.update(status='active', detail='Rescanning selected Sources')
            publish('verify_plan', 'Verify Plan', current=0, total=change_total)
            if index_job_snapshot().get('active'):
                raise RuntimeError('Index rebuild is running.')
            verified = _verify_sync_plan(plan, smart_db_path, main_db_path)
            verify_step.update(status='done', current=1, detail='Plan verified')

            exiftool = resolve_exiftool()
            exiftool_version = probe_exiftool_version(exiftool) if exiftool else ''
            process_step = step_map['process_assets']
            process_total = verified['summary']['process_total']
            process_step.update(status='active', current=0, total=process_total)
            if process_total:
                process_step['detail'] = f'{process_total} Photos to process'
            else:
                process_step['detail'] = 'No added or changed Photos'
            publish('process_assets', process_step['detail'], current=0, total=change_total)

            def on_processed(done, total):
                process_step['current'] = done
                process_step['total'] = total
                process_step['detail'] = f'Processed {done} / {total} Photos'
                publish('process_assets', process_step['detail'], current=done, total=change_total)

            prepared_rows = _prepare_sync_rows(
                verified,
                exiftool,
                progress_callback=on_processed,
                cancel_callback=sync_cancel_requested,
            )
            process_step.update(status='done', current=process_total, total=process_total, detail=f'Processed {process_total} Photos')

            # Metadata extraction can take time. Re-scan before touching SQLite so
            # execution always commits the exact previewed plan.
            verified_again = _verify_sync_plan(plan, smart_db_path, main_db_path)
            if index_job_snapshot().get('active'):
                raise RuntimeError('Index rebuild started. Sync stopped.')

            apply_step = step_map['apply_changes']
            apply_step.update(
                status='active',
                current=0,
                total=1,
                detail=(
                    f"Added {verified_again['summary']['added_count']} · "
                    f"Changed {verified_again['summary']['changed_count']} · "
                    f"Deleted {verified_again['summary']['deleted_count']}"
                ),
            )
            publish('apply_changes', 'Update Index', current=process_total, total=change_total)
            status = _apply_sync_changes(
                verified_again,
                prepared_rows,
                smart_db_path,
                main_db_path,
                exiftool_version,
                cancel_callback=sync_cancel_requested,
            )
            apply_step.update(status='done', current=1, total=1, detail='Changes staged')

            commit_step = step_map['verify_commit']
            commit_step.update(status='done', current=1, total=1, detail=f"Committed · {status['asset_count']} Photos")
            update_sync_job(
                active=False,
                phase='done',
                message=(
                    f"Sync complete · Added {plan['summary']['added_count']} · "
                    f"Changed {plan['summary']['changed_count']} · "
                    f"Deleted {plan['summary']['deleted_count']}"
                ),
                percent=100,
                current=change_total,
                total=change_total,
                steps=copy.deepcopy(steps),
                summary=copy.deepcopy(plan['summary']),
                error='',
                cancel_requested=False,
                plan=public_plan,
                index=status,
            )
        except SmartAlbumIndexCancelled:
            for step in steps:
                if step.get('status') == 'active':
                    step['status'] = 'cancelled'
                    step['detail'] = (step.get('detail') or '') + ' (Cancelled)'
            update_sync_job(
                active=False,
                phase='cancelled',
                message='Sync cancelled.',
                steps=copy.deepcopy(steps),
                error='',
                cancel_requested=False,
                plan=public_plan,
                index=_index_status(smart_db_path),
            )
        except Exception as exc:
            for step in steps:
                if step.get('status') == 'active':
                    step['status'] = 'error'
            update_sync_job(
                active=False,
                phase='error',
                message='Smart View index sync failed.',
                steps=copy.deepcopy(steps),
                error=str(exc),
                cancel_requested=False,
                plan=public_plan,
                index=_index_status(smart_db_path),
            )

    def guard():
        return admin_guard()

    @bp.route('/api/smart-albums', methods=['GET'])
    def list_smart_albums():
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        rows = conn.execute('SELECT * FROM smart_albums ORDER BY created_at DESC, id DESC').fetchall()
        conn.close()
        return jsonify({'albums': [_album_dict(row) for row in rows], 'index': _index_status(smart_db_path)})

    @bp.route('/api/smart-albums', methods=['POST'])
    def create_smart_album():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '')
        python_code = str(data.get('python_code') or DEFAULT_QUERY_CODE)
        if not name:
            return jsonify({'error': 'Name required.'}), 400
        # Parse/validate before saving. Execution happens when the album runs.
        try:
            from features.smart.runtime import _validate_script
            _validate_script(python_code)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400
        now = _now_iso()
        conn = _connect(smart_db_path)
        cursor = conn.execute(
            '''INSERT INTO smart_albums
               (name, description, python_code, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)''',
            (name, description, python_code, now, now),
        )
        album_id = cursor.lastrowid
        conn.commit()
        row = _album_row(conn, album_id)
        conn.close()
        return jsonify({'album': _album_dict(row)}), 201

    @bp.route('/api/smart-albums/<int:album_id>', methods=['GET'])
    def get_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _album_row(conn, album_id)
        conn.close()
        if not row:
            return jsonify({'error': 'Smart Album not found.'}), 404
        return jsonify({'album': _album_dict(row), 'index': _index_status(smart_db_path)})

    @bp.route('/api/smart-albums/<int:album_id>', methods=['PUT'])
    def update_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        conn = _connect(smart_db_path)
        existing = _album_row(conn, album_id)
        if not existing:
            conn.close()
            return jsonify({'error': 'Smart Album not found.'}), 404
        name = str(data.get('name', existing['name']) or '').strip()
        description = str(data.get('description', existing['description']) or '')
        python_code = str(data.get('python_code', existing['python_code']) or '')
        if not name:
            conn.close()
            return jsonify({'error': 'Name required.'}), 400
        try:
            from features.smart.runtime import _validate_script
            _validate_script(python_code)
        except Exception as exc:
            conn.close()
            return jsonify({'error': str(exc)}), 400
        conn.execute(
            '''UPDATE smart_albums
               SET name=?, description=?, python_code=?, updated_at=?
               WHERE id=?''',
            (name, description, python_code, _now_iso(), album_id),
        )
        conn.commit()
        row = _album_row(conn, album_id)
        conn.close()
        return jsonify({'album': _album_dict(row)})

    @bp.route('/api/smart-albums/<int:album_id>', methods=['DELETE'])
    def delete_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _album_row(conn, album_id)
        if not row:
            conn.close()
            return jsonify({'error': 'Smart Album not found.'}), 404
        conn.execute('DELETE FROM smart_albums WHERE id=?', (album_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @bp.route('/api/smart-albums/<int:album_id>/query', methods=['POST'])
    def query_smart_album(album_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _album_row(conn, album_id)
        conn.close()
        if not row:
            return jsonify({'error': 'Smart Album not found.'}), 404
        try:
            results, status = _run_album_query(smart_db_path, main_db_path, row)
            conn = _connect(smart_db_path)
            fresh_row = _album_row(conn, album_id)
            conn.close()
            return jsonify({
                'album': _album_dict(fresh_row),
                'images': results,
                'count': len(results),
                'index': status,
            })
        except Exception as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Smart View index required. Rebuild the index.',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                    'traceback': '',
                }), 409
            return jsonify({
                'error': str(exc),
                'traceback': getattr(exc, 'smart_traceback', ''),
            }), 400

    @bp.route('/api/smart-albums/index/status', methods=['GET'])
    def smart_album_index_status():
        denied = guard()
        if denied:
            return denied
        return jsonify(_index_status(smart_db_path))

    @bp.route('/api/smart-albums/index/refresh', methods=['POST'])
    def refresh_smart_album_index():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        requested_source_ids = data.get('source_ids')
        try:
            selected_sources = _selected_enabled_sources(main_db_path, requested_source_ids)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        if not selected_sources:
            return jsonify({'error': 'No enabled Sources available for Smart View indexing'}), 400
        if not any(Path(source['root_path']).expanduser().is_dir() for source in selected_sources):
            return jsonify({'error': 'Selected Sources are unavailable'}), 400
        selected_source_ids = [int(source['id']) for source in selected_sources]
        selected_source_names = [source['name'] for source in selected_sources]

        with index_job_lock:
            if index_job['active']:
                return jsonify(dict(index_job)), 202
            index_job.update({
                'active': True,
                'phase': 'starting',
                'message': 'Preparing index rebuild',
                'overall': {'dimension': 'image', 'label': 'Overall', 'current': 0, 'total': 0, 'percent': 0, 'ready': False},
                'steps': _new_index_steps(),
                'summary': {
                    'overall_dimension': 'image',
                    'source_total': len(selected_sources),
                    'source_available': 0,
                    'selected_source_ids': selected_source_ids,
                    'selected_source_names': selected_source_names,
                    'set_total': 0,
                    'asset_total': 0,
                    'stage_counts': {stage: 0 for stage, _ in _STAGE_DEFS},
                    'manifest_mode': 'live',
                    'state_mode': 'live',
                    'originals_indexed': any(stage == 'original_jpg' for stage, _ in _STAGE_DEFS),
                    'capture_metadata_source': SMART_ALBUM_CAPTURE_METADATA_SOURCE,
                },
                'error': '',
                'cancel_requested': False,
                'index': _index_status(smart_db_path),
            })
        threading.Thread(target=run_index_refresh_job, args=(selected_source_ids,), daemon=True).start()
        return jsonify(index_job_snapshot()), 202

    @bp.route('/api/smart-albums/index/cancel', methods=['POST'])
    def cancel_smart_album_index():
        denied = guard()
        if denied:
            return denied
        with index_job_lock:
            if not index_job['active']:
                return jsonify(copy.deepcopy(index_job))
            index_job['cancel_requested'] = True
            index_job['phase'] = 'cancelling'
            index_job['message'] = 'Cancelling rebuild…'
            snapshot = copy.deepcopy(index_job)
        return jsonify(snapshot), 202

    @bp.route('/api/smart-albums/index/progress', methods=['GET'])
    def smart_album_index_progress():
        denied = guard()
        if denied:
            return denied
        return jsonify(index_job_snapshot())

    @bp.route('/api/smart-albums/index/sync/preview', methods=['POST'])
    def preview_smart_album_index_sync():
        denied = guard()
        if denied:
            return denied
        if index_job_snapshot().get('active'):
            return jsonify({'error': 'Index rebuild is running.'}), 409
        if sync_job_snapshot().get('active'):
            return jsonify({'error': 'Index sync is running.'}), 409
        data = request.get_json(silent=True) or {}
        requested_source_ids = data.get('source_ids')
        try:
            plan = _build_sync_plan(smart_db_path, main_db_path, requested_source_ids)
        except (ValueError, RuntimeError) as exc:
            return jsonify({'error': str(exc)}), 400
        with sync_job_lock:
            sync_plans.clear()
            sync_plans[plan['plan_id']] = plan
        return jsonify(_public_sync_plan(plan))

    @bp.route('/api/smart-albums/index/sync/start', methods=['POST'])
    def start_smart_album_index_sync():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        plan_id = str(data.get('plan_id') or '').strip()
        if not plan_id:
            return jsonify({'error': 'Missing sync plan. Scan again.'}), 400
        if index_job_snapshot().get('active'):
            return jsonify({'error': 'Index rebuild is running.'}), 409
        with sync_job_lock:
            plan = sync_plans.get(plan_id)
            if not plan:
                return jsonify({'error': 'Sync plan expired. Scan again.'}), 409
            if sync_job['active']:
                return jsonify(copy.deepcopy(sync_job)), 202
            change_total = (
                plan['summary']['added_count'] +
                plan['summary']['changed_count'] +
                plan['summary']['deleted_count']
            )
            if change_total <= 0:
                return jsonify({'error': 'No changes for the selected Sources.'}), 400
            sync_job.update({
                'active': True,
                'phase': 'starting',
                'message': 'Preparing index sync',
                'percent': 0,
                'current': 0,
                'total': change_total,
                'steps': _new_sync_steps(),
                'summary': copy.deepcopy(plan['summary']),
                'error': '',
                'cancel_requested': False,
                'plan': _public_sync_plan(plan),
                'index': _index_status(smart_db_path),
            })
        threading.Thread(target=run_sync_job, args=(plan_id,), daemon=True).start()
        return jsonify(sync_job_snapshot()), 202

    @bp.route('/api/smart-albums/index/sync/cancel', methods=['POST'])
    def cancel_smart_album_index_sync():
        denied = guard()
        if denied:
            return denied
        with sync_job_lock:
            if not sync_job['active']:
                return jsonify(copy.deepcopy(sync_job))
            sync_job['cancel_requested'] = True
            sync_job['phase'] = 'cancelling'
            sync_job['message'] = 'Cancelling sync…'
            snapshot = copy.deepcopy(sync_job)
        return jsonify(snapshot), 202

    @bp.route('/api/smart-albums/index/sync/progress', methods=['GET'])
    def smart_album_index_sync_progress():
        denied = guard()
        if denied:
            return denied
        return jsonify(sync_job_snapshot())

    @bp.route('/api/smart-albums/runtime', methods=['GET'])
    def smart_album_runtime_contract():
        denied = guard()
        if denied:
            return denied
        helper_docs = custom_helper_docs(read_custom_helpers_source())
        return jsonify({
            'default_code': DEFAULT_QUERY_CODE,
            'provider': 'library',
            'notes': [
                'photos contains indexed photos from currently enabled Library Sources.',
                'result must be a Photo object or an iterable of Photo objects.',
                'import/file/process/network/database mutation capabilities are not exposed.',
                f"Indexed stages: {', '.join(stage for stage, _ in _STAGE_DEFS)}.",
                f"photo.capture.* metadata source: {SMART_ALBUM_CAPTURE_METADATA_SOURCE}.",
                'Edit the Smart indexing-policy constants near the top of features/smart/index.py to enable Original indexing/donors later.',
            ],
            'helpers': [f"helpers.{item['signature']}" for item in helper_docs],
            'helper_docs': [
                {**item, 'signature': f"helpers.{item['signature']}"}
                for item in helper_docs
            ],
            'photo_contract_groups': PHOTO_CONTRACT_GROUPS,
            'photo_contract': [field for group in PHOTO_CONTRACT_GROUPS for field in group['fields']],
            'example_code': SMART_ALBUM_HELP_EXAMPLE,
        })

    return bp
