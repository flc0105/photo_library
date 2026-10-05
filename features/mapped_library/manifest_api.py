import json
import os
import shutil
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from core.database import get_db_connection
from core.image import extract_gps_from_image
from core.manifest import MANIFEST_FILENAME, _order_manifest_keys as order_manifest_keys
from features.mapped_library.autofill import build_manifest_reference_index, get_original_jpg_time_range
from features.mapped_library.browser import (
    get_library_source, is_set_folder_name, library_admin_guard,
    resolve_library_path, suggest_manifest,
)

bp = Blueprint('mapped_library_manifest', __name__)

@bp.route('/api/library/sources/<int:source_id>/manifest-suggestion', methods=['GET'])
def get_library_manifest_suggestion(source_id):
    denied = library_admin_guard()
    if denied:
        return denied
    source = get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        _, target, _ = resolve_library_path(source, request.args.get('path', ''))
        if not target.is_dir():
            return jsonify({'error': '目标不是目录'}), 400
        if not is_set_folder_name(target.name):
            return jsonify({'error': '目标不是 Set 目录'}), 400
        return jsonify(suggest_manifest(target, include_times=True))
    except (ValueError, FileNotFoundError, NotADirectoryError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/manifest-reference', methods=['GET'])
def get_library_manifest_reference(source_id):
    denied = library_admin_guard()
    if denied:
        return denied
    source = get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        conn = get_db_connection()
        rows = conn.execute('SELECT root_path FROM library_sources WHERE enabled = 1').fetchall()
        conn.close()
        roots = []
        for row in rows:
            root = Path(row['root_path']).expanduser().resolve()
            if root.is_dir():
                roots.append(root)
        return jsonify(build_manifest_reference_index(roots))
    except (ValueError, FileNotFoundError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/tools/extract-gps', methods=['POST'])
def extract_gps_from_uploaded_photo():
    denied = library_admin_guard()
    if denied:
        return denied

    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify({'error': '请选择一张照片'}), 400

    # iPhone originals are commonly JPEG or HEIC. ExifTool, when installed,
    # handles HEIC/HEIF; Pillow remains the fallback for supported formats.
    suffix = Path(photo.filename).suffix.lower() or '.img'
    allowed = {'.jpg', '.jpeg', '.heic', '.heif', '.tif', '.tiff', '.png'}
    if suffix not in allowed:
        return jsonify({'error': '仅支持 JPG/JPEG/HEIC/HEIF/TIFF/PNG 照片'}), 400

    if request.content_length and request.content_length > 100 * 1024 * 1024:
        return jsonify({'error': '照片过大，最大支持 100 MB'}), 413

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix='photo-library-gps-', suffix=suffix, delete=False) as temp_file:
            temp_path = temp_file.name
            photo.save(temp_file)

        gps = extract_gps_from_image(temp_path, precision=5)
        return jsonify(gps)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'读取 GPS 失败: {str(exc)}'}), 500
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


@bp.route('/api/library/sources/<int:source_id>/manifest', methods=['PUT'])
def save_library_manifest(source_id):
    denied = library_admin_guard()
    if denied:
        return denied
    source = get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'manifest 必须是 JSON object'}), 400
    # Enforce only the agreed ordering. Custom fields stay exactly where the
    # user inserted them relative to the available slots, and no missing
    # optional field is synthesized.
    payload = order_manifest_keys(payload)
    try:
        root, target, rel = resolve_library_path(source, request.args.get('path', ''))
        if not target.is_dir():
            return jsonify({'error': 'manifest 只能保存到目录'}), 400
        manifest_path = target / MANIFEST_FILENAME
        backup_path = target / (MANIFEST_FILENAME + '.bak')
        temp_path = target / (MANIFEST_FILENAME + '.tmp')
        if manifest_path.exists():
            shutil.copy2(manifest_path, backup_path)
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, manifest_path)
        # The backup is only a transactional safety copy. Once the atomic
        # replacement succeeds, remove it so manifest.json.bak never lingers
        # in a healthy Set directory.
        try:
            if backup_path.exists():
                backup_path.unlink()
        except OSError as exc:
            current_app.logger.warning('manifest saved but backup cleanup failed: %s', exc)
        return jsonify({'success': True, 'path': rel, 'manifest': payload})
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({'error': str(exc)}), 400


