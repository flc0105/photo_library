from pathlib import Path

from flask import Blueprint, jsonify, request

from features.smart.runtime import _validate_script, custom_helper_docs, run_set_query
from features.smart.helpers import read_custom_helpers_source
from features.smart.index import (
    SMART_ALBUM_DB_FILENAME, SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    _connect, _index_status, _now_iso, set_candidates,
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


def _run_smart_set(smart_db_path, main_db_path, row):
    payloads, result_rows, status = set_candidates(smart_db_path, main_db_path)
    ordered_ids = run_set_query(
        row['python_code'],
        payloads,
        read_custom_helpers_source(),
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
            return jsonify({'error': 'Name required.'}), 400
        try:
            _validate_script(python_code)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400
        now = _now_iso()
        conn = _connect(smart_db_path)
        cursor = conn.execute(
            '''INSERT INTO smart_sets
               (name, description, python_code, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)''',
            (name, description, python_code, now, now),
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
            return jsonify({'error': 'Smart Set not found.'}), 404
        name = str(data.get('name', existing['name']) or '').strip()
        description = str(data.get('description', existing['description']) or '')
        python_code = str(data.get('python_code', existing['python_code']) or '')
        if not name:
            conn.close()
            return jsonify({'error': 'Name required.'}), 400
        try:
            _validate_script(python_code)
        except Exception as exc:
            conn.close()
            return jsonify({'error': str(exc)}), 400
        conn.execute(
            '''UPDATE smart_sets
               SET name=?, description=?, python_code=?, updated_at=?
               WHERE id=?''',
            (name, description, python_code, _now_iso(), smart_set_id),
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
            return jsonify({'error': 'Smart Set not found.'}), 404
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
            return jsonify({'error': 'Smart Set not found.'}), 404
        try:
            results, status = _run_smart_set(smart_db_path, main_db_path, row)
            conn = _connect(smart_db_path)
            fresh_row = _set_row(conn, smart_set_id)
            conn.close()
            return jsonify({'smart_set': _set_dict(fresh_row), 'sets': results, 'index': status})
        except RuntimeError as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Index required.',
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
        helper_docs = custom_helper_docs(read_custom_helpers_source())
        return jsonify({
            'default_code': DEFAULT_SMART_SET_CODE,
            'provider': 'library-set',
            'notes': [
                'sets contains real Set directories from currently indexed, enabled and mounted Library Sources.',
                'Manifest and stage counts are read live when the Smart Set runs.',
                'set.photos reuses the existing Smart Album photo index; no second photo index is created.',
                'result must be a Set object or an iterable of Set objects.',
            ],
            'helpers': [f"helpers.{item['signature']}" for item in helper_docs],
            'helper_docs': [
                {**item, 'signature': f"helpers.{item['signature']}"}
                for item in helper_docs
            ],
            'contract_groups': SMART_SET_CONTRACT_GROUPS,
        })

    return bp
