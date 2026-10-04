import json
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.smart_album_runtime import _validate_script, run_set_query
from core.smart_albums import (
    SMART_ALBUM_DB_FILENAME,
    SMART_ALBUM_ENGINE_VERSION,
    SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    SMART_ALBUM_INDEX_STAGE_DEFS,
    _DISPLAY_IMAGE_EXTENSIONS,
    _RAW_EXTENSIONS,
    _asset_payloads,
    _connect,
    _discover_sets,
    _enabled_sources,
    _index_status,
    _now_iso,
    _walk_files,
)


DEFAULT_SMART_SET_CODE = """result = list(sets)\n"""

SMART_SET_CONTRACT_GROUPS = [
    {'label': 'Identity', 'fields': ['set.id', 'set.name', 'set.path', 'set.shoot_date']},
    {'label': 'Source', 'fields': ['set.source.id', 'set.source.name']},
    {'label': 'Manifest', 'fields': ['set.manifest']},
    {'label': 'Counts', 'fields': [
        'set.counts.original_jpg', 'set.counts.original_raw',
        'set.counts.base_edit', 'set.counts.model_edit',
        'set.counts.revision', 'set.counts.final',
        'set.favorite_count', 'set.indexed_photo_count',
    ]},
    {'label': 'Photos', 'fields': ['set.photos']},
]


def _init_smart_set_db(db_path):
    conn = _connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS smart_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            python_code TEXT NOT NULL,
            engine_version INTEGER NOT NULL DEFAULT 1,
            last_result_count INTEGER,
            last_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def _set_row(conn, smart_set_id):
    return conn.execute('SELECT * FROM smart_sets WHERE id=?', (smart_set_id,)).fetchone()


def _set_dict(row):
    if not row:
        return None
    data = dict(row)
    data['type'] = 'smart_set'
    return data


def _source_scope(main_db_path, smart_db_path):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')
    indexed_ids = {int(value) for value in status.get('indexed_source_ids') or []}
    sources = []
    for source in _enabled_sources(main_db_path):
        if int(source['id']) not in indexed_ids:
            continue
        if not Path(source['root_path']).expanduser().is_dir():
            continue
        sources.append(source)
    return sources, status


def _manifest_info(root, set_path):
    manifest_path = Path(root) / set_path / 'manifest.json'
    if not manifest_path.is_file():
        return {}, False, True
    try:
        raw = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return {}, True, False
        return raw, True, True
    except Exception:
        return {}, True, False


def _shoot_date(manifest, set_name):
    shoot = manifest.get('shoot') if isinstance(manifest.get('shoot'), dict) else {}
    date_text = str(shoot.get('date') or '').strip()
    if date_text:
        return date_text
    prefix = str(set_name or '')[:8]
    if len(prefix) == 8 and prefix.isdigit():
        return f'{prefix[:4]}-{prefix[4:6]}-{prefix[6:8]}'
    return ''


def _favorite_paths(main_db_path, source_ids):
    if not source_ids:
        return []
    placeholders = ','.join('?' for _ in source_ids)
    conn = _connect(main_db_path)
    rows = conn.execute(
        f'''SELECT source_id, relative_path
            FROM library_image_states
            WHERE is_favorited=1 AND source_id IN ({placeholders})''',
        tuple(source_ids),
    ).fetchall()
    conn.close()
    return [(int(row['source_id']), str(row['relative_path'])) for row in rows]


def _stage_counts(set_dir):
    counts = {
        'original_jpg': len(_walk_files(set_dir / '01_Original' / 'JPG', _DISPLAY_IMAGE_EXTENSIONS)),
        'original_raw': len(_walk_files(set_dir / '01_Original' / 'RAW', _RAW_EXTENSIONS)),
    }
    for stage, relative_dir in SMART_ALBUM_INDEX_STAGE_DEFS:
        counts[stage] = len(_walk_files(set_dir / relative_dir, _DISPLAY_IMAGE_EXTENSIONS))
    return counts


def _set_candidates(smart_db_path, main_db_path, photo_payloads=None):
    sources, status = _source_scope(main_db_path, smart_db_path)
    source_map = {int(source['id']): source for source in sources}

    if photo_payloads is None:
        photo_payloads, _ = _asset_payloads(smart_db_path, main_db_path)
    photos_by_set = {}
    for photo in photo_payloads:
        source = photo.get('source') or {}
        set_info = photo.get('set') or {}
        key = (int(source.get('id')), str(set_info.get('path') or ''))
        if key[0] in source_map:
            photos_by_set.setdefault(key, []).append(photo)

    discovered = []
    set_paths_by_source = {}
    for source in sources:
        source_id = int(source['id'])
        root = Path(source['root_path']).expanduser().resolve()
        for set_dir in _discover_sets(root):
            set_path = set_dir.relative_to(root).as_posix()
            discovered.append((source, root, set_dir, set_path))
            set_paths_by_source.setdefault(source_id, []).append(set_path)

    favorite_counts = {(int(source['id']), path): 0 for source in sources for path in set_paths_by_source.get(int(source['id']), [])}
    favorite_rows = _favorite_paths(main_db_path, list(source_map))
    sorted_paths = {
        source_id: sorted(paths, key=lambda value: len(value), reverse=True)
        for source_id, paths in set_paths_by_source.items()
    }
    for source_id, relative_path in favorite_rows:
        source = source_map.get(source_id)
        if not source or not (Path(source['root_path']).expanduser() / relative_path).is_file():
            continue
        for set_path in sorted_paths.get(source_id, []):
            if relative_path == set_path or relative_path.startswith(set_path + '/'):
                favorite_counts[(source_id, set_path)] = favorite_counts.get((source_id, set_path), 0) + 1
                break

    payloads = []
    rows = {}
    for source, root, set_dir, set_path in discovered:
        source_id = int(source['id'])
        manifest, has_manifest, manifest_valid = _manifest_info(root, set_path)
        counts = _stage_counts(set_dir)
        key = (source_id, set_path)
        set_id = f'library-set:{source_id}:{set_path}'
        photo_items = photos_by_set.get(key, [])
        shoot_date = _shoot_date(manifest, set_dir.name)
        payload = {
            'id': set_id,
            'source': {'id': source_id, 'name': source['name']},
            'name': set_dir.name,
            'path': set_path,
            'manifest': manifest,
            'counts': counts,
            'favorite_count': favorite_counts.get(key, 0),
            'indexed_photo_count': len(photo_items),
            'photos': photo_items,
            'shoot_date': {'__smart_date__': shoot_date} if shoot_date else None,
        }
        payloads.append(payload)
        rows[set_id] = {
            'id': set_id,
            'source_id': source_id,
            'source_name': source['name'],
            'name': set_dir.name,
            'set_path': set_path,
            'shoot_date': shoot_date,
            'model': str(manifest.get('model') or '').strip(),
            'has_manifest': has_manifest,
            'manifest_valid': manifest_valid,
            'counts': counts,
            'favorite_count': favorite_counts.get(key, 0),
            'indexed_photo_count': len(photo_items),
        }
    return payloads, rows, status


def _run_smart_set(smart_db_path, main_db_path, row):
    payloads, result_rows, status = _set_candidates(smart_db_path, main_db_path)
    ordered_ids = run_set_query(
        row['python_code'],
        payloads,
        timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    )
    results = [dict(result_rows[item_id]) for item_id in ordered_ids if item_id in result_rows]
    conn = _connect(smart_db_path)
    conn.execute(
        'UPDATE smart_sets SET last_result_count=?, last_run_at=? WHERE id=?',
        (len(results), _now_iso(), row['id']),
    )
    conn.commit()
    conn.close()
    return results, status


def create_smart_set_blueprint(admin_guard, main_db_path):
    main_db_path = Path(main_db_path).resolve()
    smart_db_path = main_db_path.parent / SMART_ALBUM_DB_FILENAME
    _init_smart_set_db(smart_db_path)
    bp = Blueprint('smart_sets', __name__)

    def guard():
        return admin_guard()

    @bp.route('/api/smart-sets', methods=['GET'])
    def list_smart_sets():
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        rows = conn.execute('SELECT * FROM smart_sets ORDER BY created_at DESC, id DESC').fetchall()
        conn.close()
        return jsonify({'sets': [_set_dict(row) for row in rows], 'index': _index_status(smart_db_path)})

    @bp.route('/api/smart-sets', methods=['POST'])
    def create_smart_set():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '')
        python_code = str(data.get('python_code') or DEFAULT_SMART_SET_CODE)
        if not name:
            return jsonify({'error': 'Smart Set 名称不能为空'}), 400
        try:
            _validate_script(python_code)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400
        now = _now_iso()
        conn = _connect(smart_db_path)
        cursor = conn.execute(
            '''INSERT INTO smart_sets
               (name, description, python_code, engine_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (name, description, python_code, SMART_ALBUM_ENGINE_VERSION, now, now),
        )
        smart_set_id = cursor.lastrowid
        conn.commit()
        row = _set_row(conn, smart_set_id)
        conn.close()
        return jsonify({'smart_set': _set_dict(row)}), 201

    @bp.route('/api/smart-sets/<int:smart_set_id>', methods=['PUT'])
    def update_smart_set(smart_set_id):
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        conn = _connect(smart_db_path)
        existing = _set_row(conn, smart_set_id)
        if not existing:
            conn.close()
            return jsonify({'error': 'Smart Set 不存在'}), 404
        name = str(data.get('name', existing['name']) or '').strip()
        description = str(data.get('description', existing['description']) or '')
        python_code = str(data.get('python_code', existing['python_code']) or '')
        if not name:
            conn.close()
            return jsonify({'error': 'Smart Set 名称不能为空'}), 400
        try:
            _validate_script(python_code)
        except Exception as exc:
            conn.close()
            return jsonify({'error': str(exc)}), 400
        conn.execute(
            '''UPDATE smart_sets
               SET name=?, description=?, python_code=?, engine_version=?, updated_at=?
               WHERE id=?''',
            (name, description, python_code, SMART_ALBUM_ENGINE_VERSION, _now_iso(), smart_set_id),
        )
        conn.commit()
        row = _set_row(conn, smart_set_id)
        conn.close()
        return jsonify({'smart_set': _set_dict(row)})

    @bp.route('/api/smart-sets/<int:smart_set_id>', methods=['DELETE'])
    def delete_smart_set(smart_set_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _set_row(conn, smart_set_id)
        if not row:
            conn.close()
            return jsonify({'error': 'Smart Set 不存在'}), 404
        conn.execute('DELETE FROM smart_sets WHERE id=?', (smart_set_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @bp.route('/api/smart-sets/<int:smart_set_id>/query', methods=['POST'])
    def query_smart_set(smart_set_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _set_row(conn, smart_set_id)
        conn.close()
        if not row:
            return jsonify({'error': 'Smart Set 不存在'}), 404
        try:
            results, status = _run_smart_set(smart_db_path, main_db_path, row)
            conn = _connect(smart_db_path)
            fresh_row = _set_row(conn, smart_set_id)
            conn.close()
            return jsonify({'smart_set': _set_dict(fresh_row), 'sets': results, 'index': status})
        except RuntimeError as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Smart Album 索引尚未建立或配置已变化，请先完成索引刷新。',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                }), 409
            return jsonify({'error': str(exc), 'traceback': getattr(exc, 'smart_traceback', '')}), 400
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/smart-sets/runtime', methods=['GET'])
    def smart_set_runtime_contract():
        denied = guard()
        if denied:
            return denied
        return jsonify({
            'engine_version': SMART_ALBUM_ENGINE_VERSION,
            'default_code': DEFAULT_SMART_SET_CODE,
            'provider': 'library-set',
            'notes': [
                'sets contains real Set directories from currently indexed, enabled and mounted Library Sources.',
                'Manifest and stage counts are read live when the Smart Set runs.',
                'set.photos reuses the existing Smart Album photo index; no second photo index is created.',
                'result must be a Set object or an iterable of Set objects.',
            ],
            'contract_groups': SMART_SET_CONTRACT_GROUPS,
        })

    return bp
