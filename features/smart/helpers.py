import ast
import os
import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from features.smart.index import SMART_ALBUM_DB_FILENAME, _connect, _now_iso
from features.smart.runtime import _validate_custom_helpers_source, _validate_script, custom_helper_docs


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CUSTOM_HELPERS_PATH = PROJECT_ROOT / 'data' / 'custom_helpers.py'
_CUSTOM_HELPER_MIGRATION_KEY = 'custom_helpers_namespace_v1'
_LEGACY_HELPER_NAMES = {
    'finals',
    'shoot_time',
    'set_key',
    'sample_per_set',
    'preferred_versions',
    'logical_photo_key',
}

_cache_lock = threading.Lock()
_cache_signature = None
_cache_source = None


def _read_source_bytes():
    try:
        return CUSTOM_HELPERS_PATH.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f'Custom Helpers 文件不存在：{CUSTOM_HELPERS_PATH}'
        ) from exc


def read_custom_helpers_source(*, validate=True):
    """Read the editable helper source, caching only unchanged file bytes."""
    global _cache_signature, _cache_source
    try:
        stat = CUSTOM_HELPERS_PATH.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f'Custom Helpers 文件不存在：{CUSTOM_HELPERS_PATH}'
        ) from exc

    signature = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if _cache_signature == signature and _cache_source is not None:
            source = _cache_source
        else:
            source = _read_source_bytes().decode('utf-8')
            _cache_signature = signature
            _cache_source = source

    if validate:
        _validate_custom_helpers_source(source)
    return source


def _invalidate_cache():
    global _cache_signature, _cache_source
    with _cache_lock:
        _cache_signature = None
        _cache_source = None


def write_custom_helpers_source(source):
    if not isinstance(source, str):
        raise ValueError('Custom Helpers source 必须是字符串。')
    docs = custom_helper_docs(source)
    CUSTOM_HELPERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CUSTOM_HELPERS_PATH.with_name(
        f'.{CUSTOM_HELPERS_PATH.name}.{uuid.uuid4().hex}.tmp'
    )
    try:
        with temp_path.open('w', encoding='utf-8', newline='') as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, CUSTOM_HELPERS_PATH)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    _invalidate_cache()
    return docs


def _bound_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def migrate_legacy_helper_calls(source):
    """Rewrite unambiguous legacy bare helper references to ``helpers.<name>``.

    Replacements are inserted at AST-confirmed Name/Load byte offsets so user
    formatting and comments stay byte-for-byte unchanged outside the inserted
    ``helpers.`` prefix. If a legacy helper name is also bound by the script,
    that name is treated as user-owned and is not rewritten.
    """
    tree = ast.parse(source, mode='exec')
    bound = _bound_names(tree)
    load_nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in _LEGACY_HELPER_NAMES
        and node.id not in bound
    ]
    if not load_nodes:
        return source, []
    if 'helpers' in bound:
        raise ValueError('脚本同时绑定了 helpers 名称，无法安全迁移旧 Helper 调用。')

    encoded_lines = source.encode('utf-8').splitlines(keepends=True)
    line_starts = []
    offset = 0
    for line in encoded_lines:
        line_starts.append(offset)
        offset += len(line)

    replacements = []
    migrated_names = set()
    source_bytes = source.encode('utf-8')
    for node in load_nodes:
        if node.lineno < 1 or node.lineno > len(line_starts):
            raise ValueError('旧 Helper 迁移定位失败。')
        absolute = line_starts[node.lineno - 1] + node.col_offset
        name_bytes = node.id.encode('ascii')
        if source_bytes[absolute:absolute + len(name_bytes)] != name_bytes:
            raise ValueError(f'旧 Helper 迁移定位失败：{node.id}')
        replacements.append(absolute)
        migrated_names.add(node.id)

    output = source_bytes
    for absolute in sorted(set(replacements), reverse=True):
        output = output[:absolute] + b'helpers.' + output[absolute:]
    migrated = output.decode('utf-8')
    return migrated, sorted(migrated_names)


def migrate_saved_smart_python(smart_db_path):
    """Run the one-time bare-helper -> helpers.* migration transaction."""
    helper_source = read_custom_helpers_source(validate=True)
    _validate_custom_helpers_source(helper_source)
    conn = _connect(smart_db_path)
    try:
        marker = conn.execute(
            'SELECT value FROM smart_album_meta WHERE key=?',
            (_CUSTOM_HELPER_MIGRATION_KEY,),
        ).fetchone()
        if marker and str(marker['value'] or '') == '1':
            return {'migrated': False, 'rows': 0, 'helpers': []}

        tables = [
            ('smart_albums', 'Smart Album'),
            ('smart_sets', 'Smart Set'),
            ('explore_blocks', 'Explore Block'),
        ]
        changed_rows = 0
        names = set()
        now = _now_iso()
        for table, label in tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            rows = conn.execute(f'SELECT id, name, python_code FROM {table}').fetchall()
            for row in rows:
                original = str(row['python_code'] or '')
                try:
                    migrated, migrated_names = migrate_legacy_helper_calls(original)
                    _validate_script(migrated)
                except Exception as exc:
                    raise ValueError(
                        f'{label} “{row["name"]}” 的旧 Helper 代码迁移失败：{exc}'
                    ) from exc
                if migrated == original:
                    continue
                conn.execute(
                    f'UPDATE {table} SET python_code=?, updated_at=? WHERE id=?',
                    (migrated, now, row['id']),
                )
                changed_rows += 1
                names.update(migrated_names)

        conn.execute(
            '''INSERT INTO smart_album_meta (key, value) VALUES (?, '1')
               ON CONFLICT(key) DO UPDATE SET value='1' ''',
            (_CUSTOM_HELPER_MIGRATION_KEY,),
        )
        conn.commit()
        return {'migrated': True, 'rows': changed_rows, 'helpers': sorted(names)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _helper_payload(source):
    try:
        docs = custom_helper_docs(source)
        return {
            'source': source,
            'helpers': docs,
            'valid': True,
            'error': '',
            'path': 'data/custom_helpers.py',
        }
    except Exception as exc:
        return {
            'source': source,
            'helpers': [],
            'valid': False,
            'error': str(exc),
            'path': 'data/custom_helpers.py',
        }


def create_smart_helpers_blueprint(admin_guard, main_db_path):
    main_db_path = Path(main_db_path).resolve()
    smart_db_path = main_db_path.parent / SMART_ALBUM_DB_FILENAME

    # All Smart tables are registered before this blueprint in main.py. The
    # migration runs exactly once per Smart database and the runtime itself has
    # no legacy helper aliases.
    read_custom_helpers_source(validate=True)
    migrate_saved_smart_python(smart_db_path)

    bp = Blueprint('smart_helpers', __name__)

    def guard():
        return admin_guard()

    @bp.route('/api/smart-helpers', methods=['GET'])
    def get_smart_helpers():
        denied = guard()
        if denied:
            return denied
        try:
            source = read_custom_helpers_source(validate=False)
        except FileNotFoundError:
            source = ''
        return jsonify(_helper_payload(source))

    @bp.route('/api/smart-helpers/validate', methods=['POST'])
    def validate_smart_helpers():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        source = data.get('source')
        if not isinstance(source, str):
            return jsonify({'error': 'source 必须是字符串。'}), 400
        payload = _helper_payload(source)
        if not payload['valid']:
            return jsonify(payload), 400
        return jsonify(payload)

    @bp.route('/api/smart-helpers', methods=['PUT'])
    def save_smart_helpers():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        source = data.get('source')
        if not isinstance(source, str):
            return jsonify({'error': 'source 必须是字符串。'}), 400
        try:
            docs = write_custom_helpers_source(source)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400
        return jsonify({
            'source': source,
            'helpers': docs,
            'valid': True,
            'error': '',
            'path': 'data/custom_helpers.py',
        })

    return bp
