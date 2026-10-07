import math
import re
import threading
from pathlib import Path

from PIL import Image
from flask import Blueprint, jsonify, request

from core.filesystem import list_source_images_original_order, list_top_level_images
from core.operations import file_signature, get_plan, new_task, remember_plan, safe_case_rename, update_task, verify_signatures

_SET_RE = re.compile(r'^\d{8}-.+-.+$')


def _phash64(path: Path) -> int:
    """Return the 8x8 perceptual hash used for visual similarity matching.

    Uses 32x32 grayscale/Lanczos input, low-frequency DCT, and a median threshold.
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

    src_files = list_source_images_original_order(base_dir)
    target_files = list_top_level_images(model_dir)
    if not src_files:
        raise ValueError('02_Base_Edit 中没有 JPG/JPEG/PNG')
    if not target_files:
        raise ValueError('03_Model_Edit 中没有 JPG/JPEG/PNG')

    # Cache each image hash once before pairwise similarity comparisons.
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
        signatures[str(path)] = file_signature(path)

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


def create_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('workflow_visual_rename', __name__)

    def resolve_set(source_id, path_value):
        source = get_source(source_id)
        if not source:
            raise FileNotFoundError('Source 不存在或已禁用')
        root, target, rel = resolve_path(source, path_value)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            raise ValueError('当前目录不是 Set')
        return source, root, target, rel

    def require_set(source_id):
        return resolve_set(source_id, (request.get_json(silent=True) or {}).get('path', ''))

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
            plan_id = remember_plan(plan)
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
            plan = get_plan(str(data.get('plan_id') or ''), 'visual_rename', source_id, set_rel)
            verify_signatures(plan['signatures'])
            operations = list(plan['operations'])
            task_id = new_task('visual_rename', len(operations))

            def worker():
                renamed = 0
                unchanged = 0
                try:
                    update_task(task_id, status='running', message='开始重命名…')
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
                            safe_case_rename(current, destination)
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
                        update_task(
                            task_id,
                            completed=index,
                            current=index,
                            message=message,
                            log=message,
                        )
                    result = {'renamed_count': renamed, 'unchanged_count': unchanged}
                    update_task(task_id, status='done', message=f'完成：重命名 {renamed} 个', result=result)
                except Exception as exc:
                    update_task(task_id, status='error', message='重命名失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    return bp
