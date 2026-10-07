import os
import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from features.smart.runtime import _validate_custom_helpers_source, custom_helper_docs


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CUSTOM_HELPERS_PATH = PROJECT_ROOT / 'data' / 'custom_helpers.py'
_cache_lock = threading.Lock()
_cache_signature = None
_cache_source = None


def _read_source_bytes():
    try:
        return CUSTOM_HELPERS_PATH.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f'Custom Helpers file not found: {CUSTOM_HELPERS_PATH}'
        ) from exc


def read_custom_helpers_source(*, validate=True):
    """Read the editable helper source, caching only unchanged file bytes."""
    global _cache_signature, _cache_source
    try:
        stat = CUSTOM_HELPERS_PATH.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f'Custom Helpers file not found: {CUSTOM_HELPERS_PATH}'
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
        raise ValueError('Custom Helpers source must be a string.')
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


def create_smart_helpers_blueprint(admin_guard):
    read_custom_helpers_source(validate=True)

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
            return jsonify({'error': 'source must be a string.'}), 400
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
            return jsonify({'error': 'source must be a string.'}), 400
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
