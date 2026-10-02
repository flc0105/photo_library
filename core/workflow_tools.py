import io
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file
from PIL import Image, ImageOps

from core.external_tools import probe_exiftool_version, resolve_exiftool
from core.final_delivery_contract import (
    FINAL_CROP_WARNING_PERCENT,
    FINAL_JPEG_CHROMA_SAMPLING,
    FINAL_JPEG_QUALITY,
    FINAL_SRGB_ICC_BYTES,
    FINAL_SRGB_ICC_SHA256,
    FINAL_SRGB_PROFILE_DESCRIPTION,
    public_final_delivery_contract,
)
from core.manifest_autofill import get_datetime_original
from core.original_naming import original_stem_key
from core.final_metadata_fields import (
    FINAL_STRUCTURAL_FIELDS,
    field_output_key,
    field_read_keys,
    load_metadata_fields,
    validate_metadata_fields_with_exiftool,
)
from core.final_resolution import allowed_final_dimensions, choose_target_for_crop


_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
_IMPORT_JPEG_EXTENSIONS = {'.jpg'}
_IMPORT_RAW_EXTENSIONS = {'.cr3'}
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


def _photo_import_set_date(set_dir: Path):
    """Resolve the import date from manifest first, then the canonical Set name."""
    manifest_path = Path(set_dir) / 'manifest.json'
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding='utf-8'))
            shoot = data.get('shoot') if isinstance(data, dict) else None
            date_text = str((shoot or {}).get('date') or '').strip() if isinstance(shoot, dict) else ''
            if date_text:
                datetime.strptime(date_text, '%Y-%m-%d')
                return date_text
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    match = re.match(r'^(\d{4})(\d{2})(\d{2})-', Path(set_dir).name)
    if not match:
        raise ValueError('无法从 manifest 或 Set 名称确定拍摄日期')
    date_text = f'{match.group(1)}-{match.group(2)}-{match.group(3)}'
    datetime.strptime(date_text, '%Y-%m-%d')
    return date_text


def _photo_import_target_dirs(set_dir: Path):
    base = Path(set_dir) / '01_Original'
    return base / 'JPG', base / 'RAW'


def _directory_has_files(directory: Path):
    if not directory.exists():
        return False
    if not directory.is_dir():
        raise ValueError(f'目标路径不是目录: {directory}')
    try:
        return any(path.is_file() and not path.name.startswith('.') for path in directory.rglob('*'))
    except OSError as exc:
        raise ValueError(f'无法读取目标目录: {directory}') from exc


def _assert_photo_import_target_empty(set_dir: Path):
    jpg_target, raw_target = _photo_import_target_dirs(set_dir)
    occupied = []
    if _directory_has_files(jpg_target):
        occupied.append('01_Original/JPG')
    if _directory_has_files(raw_target):
        occupied.append('01_Original/RAW')
    if occupied:
        raise ValueError(f'导入只允许用于空的 Original 目录：{", ".join(occupied)} 已有文件')
    return jpg_target, raw_target


def _photo_import_source_dirs(source_root, shoot_date):
    root = Path(str(source_root or '')).expanduser()
    if not root.is_absolute():
        raise ValueError('Source Root 必须是绝对路径')
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f'Source Root 不存在: {root}')
    date_dir = root / shoot_date
    if not date_dir.is_dir():
        raise FileNotFoundError(f'找不到当天导出目录: {date_dir}')
    return root, date_dir, date_dir


def _photo_import_sample(file_ids, records):
    if len(file_ids) <= 10:
        head = file_ids
        tail = []
        omitted = 0
    else:
        head = file_ids[:5]
        tail = file_ids[-5:]
        omitted = len(file_ids) - 10

    def public_record(file_id):
        item = records[file_id]
        value = item.get('taken_at')
        return {
            'id': file_id,
            'name': item['name'],
            'taken_at': value.strftime('%H:%M:%S') if value else '',
        }

    return {
        'head': [public_record(file_id) for file_id in head],
        'tail': [public_record(file_id) for file_id in tail],
        'omitted_count': omitted,
    }


def _build_photo_import_plan(source_id, set_dir: Path, set_rel: str, source_root, gap_minutes):
    try:
        gap = int(gap_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError('Gap 必须是分钟整数') from exc
    if gap < 1 or gap > 24 * 60:
        raise ValueError('Gap 必须在 1–1440 分钟之间')

    _assert_photo_import_target_empty(set_dir)
    shoot_date = _photo_import_set_date(set_dir)
    root, jpg_dir, raw_dir = _photo_import_source_dirs(source_root, shoot_date)
    jpg_files = sorted(
        [path for path in jpg_dir.iterdir() if path.is_file() and path.suffix.lower() in _IMPORT_JPEG_EXTENSIONS],
        key=lambda path: path.name.casefold(),
    )
    if not jpg_files:
        raise ValueError(f'当天导出目录没有 JPG: {jpg_dir}')

    records = {}
    known = []
    unknown = []
    signatures = {}
    for index, path in enumerate(jpg_files, start=1):
        file_id = str(index)
        taken_at = get_datetime_original(path)
        records[file_id] = {
            'id': file_id,
            'name': path.name,
            'path': str(path),
            'stem': path.stem,
            'taken_at': taken_at,
        }
        signatures[str(path)] = _file_signature(path)
        if taken_at is None:
            unknown.append(file_id)
        else:
            known.append(file_id)

    known.sort(key=lambda file_id: (records[file_id]['taken_at'], records[file_id]['name'].casefold()))
    grouped_ids = []
    current = []
    previous = None
    for file_id in known:
        current_time = records[file_id]['taken_at']
        if previous is not None and (current_time - previous).total_seconds() > gap * 60:
            grouped_ids.append((current, False))
            current = []
        current.append(file_id)
        previous = current_time
    if current:
        grouped_ids.append((current, False))
    if unknown:
        grouped_ids.append((unknown, True))

    groups = []
    for index, (file_ids, is_unknown) in enumerate(grouped_ids, start=1):
        times = [records[file_id]['taken_at'] for file_id in file_ids if records[file_id]['taken_at'] is not None]
        groups.append({
            'id': f'g{index}',
            'index': index,
            'file_ids': file_ids,
            'count': len(file_ids),
            'start_time': min(times).strftime('%H:%M:%S') if times else '',
            'end_time': max(times).strftime('%H:%M:%S') if times else '',
            'unknown_time': bool(is_unknown),
            'sample': _photo_import_sample(file_ids, records),
        })

    return {
        'kind': 'photo_import_preview',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'shoot_date': shoot_date,
        'source_root': str(root),
        'jpg_dir': str(jpg_dir),
        'raw_dir': str(raw_dir),
        'gap_minutes': gap,
        'records': records,
        'groups': groups,
        'signatures': signatures,
    }


def _photo_import_public_plan(plan, plan_id):
    return {
        'plan_id': plan_id,
        'shoot_date': plan['shoot_date'],
        'source_root': plan['source_root'],
        'jpg_dir': plan['jpg_dir'],
        'raw_dir': plan['raw_dir'],
        'gap_minutes': plan['gap_minutes'],
        'jpg_count': len(plan['records']),
        'groups': [
            {
                'id': group['id'],
                'index': group['index'],
                'count': group['count'],
                'start_time': group['start_time'],
                'end_time': group['end_time'],
                'unknown_time': group['unknown_time'],
                'sample': group['sample'],
            }
            for group in plan['groups']
        ],
    }


def _photo_import_selected_records(plan, group_ids):
    selected = {str(value) for value in (group_ids or [])}
    valid = {group['id'] for group in plan['groups']}
    if not selected:
        raise ValueError('至少选择一组照片')
    invalid = selected - valid
    if invalid:
        raise ValueError('选择的分组已经失效，请重新预览')

    file_ids = []
    for group in plan['groups']:
        if group['id'] in selected:
            file_ids.extend(group['file_ids'])
    return [plan['records'][file_id] for file_id in file_ids]


def _prepare_photo_import_execution(source_id, set_rel, plan, group_ids):
    set_dir = Path(plan['set_dir'])
    jpg_target, raw_target = _assert_photo_import_target_empty(set_dir)
    selected = _photo_import_selected_records(plan, group_ids)
    selected_signatures = {item['path']: plan['signatures'][item['path']] for item in selected}
    _verify_signatures(selected_signatures)

    # Multiple JPGs with the same stem cannot be paired safely with one RAW.
    jpg_stems = {}
    for item in selected:
        key = item['stem'].casefold()
        jpg_stems.setdefault(key, []).append(item)
    duplicates = [items for items in jpg_stems.values() if len(items) > 1]
    if duplicates:
        names = ', '.join('/'.join(item['name'] for item in items) for items in duplicates[:5])
        raise ValueError(f'选中的 JPG 存在重复 stem，无法一一配对 RAW: {names}')

    raw_dir = Path(plan['raw_dir'])
    raw_index = {}
    for path in raw_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in _IMPORT_RAW_EXTENSIONS:
            continue
        raw_index.setdefault(path.stem.casefold(), []).append(path)

    missing = []
    ambiguous = []
    operations = []
    signatures = dict(selected_signatures)
    for jpg in selected:
        candidates = raw_index.get(jpg['stem'].casefold(), [])
        if not candidates:
            missing.append(jpg['name'])
            continue
        if len(candidates) != 1:
            ambiguous.append(f"{jpg['name']} → {', '.join(path.name for path in candidates)}")
            continue
        raw = candidates[0]
        signatures[str(raw)] = _file_signature(raw)
        jpg_dst = jpg_target / jpg['name']
        raw_dst = raw_target / raw.name
        if jpg_dst.exists() or raw_dst.exists():
            raise FileExistsError(f'目标已有同名文件: {jpg_dst.name if jpg_dst.exists() else raw_dst.name}')
        operations.append({
            'jpg_src': jpg['path'],
            'jpg_dst': str(jpg_dst),
            'raw_src': str(raw),
            'raw_dst': str(raw_dst),
            'stem': jpg['stem'],
        })

    if missing or ambiguous:
        messages = []
        if missing:
            preview = ', '.join(missing[:8])
            if len(missing) > 8:
                preview += f' 等 {len(missing)} 个'
            messages.append(f'缺少同 stem RAW: {preview}')
        if ambiguous:
            preview = '; '.join(ambiguous[:5])
            if len(ambiguous) > 5:
                preview += f' 等 {len(ambiguous)} 组'
            messages.append(f'同 stem RAW 不唯一: {preview}')
        raise ValueError('；'.join(messages))

    return {
        'kind': 'photo_import_execute',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'source_root': plan['source_root'],
        'jpg_dir': plan['jpg_dir'],
        'raw_dir': plan['raw_dir'],
        'shoot_date': plan['shoot_date'],
        'operations': operations,
        'signatures': signatures,
        'jpg_target': str(jpg_target),
        'raw_target': str(raw_target),
    }


def _rollback_photo_import(moved):
    errors = []
    for src, dst in reversed(moved):
        src = Path(src)
        dst = Path(dst)
        try:
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
        except Exception as exc:
            errors.append(f'{dst.name}: {exc}')
    return errors


def _phash64(path: Path) -> int:
    """Compatible 8x8 perceptual hash used by the old imagehash.phash flow.

    It keeps the same 32x32 grayscale/Lanczos + low-frequency DCT + median rule,
    but is implemented locally so Photo Library does not need the imagehash package.
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


def _duplicate_stems(paths):
    grouped = {}
    for path in paths:
        grouped.setdefault(path.stem.casefold(), []).append(path)
    return {key: value for key, value in grouped.items() if len(value) > 1}


def _format_duplicate_stem_error(label: str, duplicates):
    examples = []
    for paths in list(duplicates.values())[:4]:
        examples.append(' / '.join(path.name for path in paths))
    detail = '；'.join(examples)
    if len(duplicates) > 4:
        detail += f'；另有 {len(duplicates) - 4} 组'
    return f'{label} 中存在重复 stem，无法安全判断一一对应关系：{detail}'


def _build_discard_unreturned_base_plan(source_id, set_dir: Path, set_rel: str):
    """Plan moving active Base Edit images that have no same-stem Model Edit.

    This intentionally compares *exact stage stems* rather than logical photo IDs.
    Visual Rename exists specifically to align Model Edit filenames with Base Edit,
    and a stage suffix can represent a distinct version that should not be silently
    treated as equivalent to another version of the same logical photo.

    Only top-level stage images participate. Existing Ready/discards/support folders
    are stateful side paths and must never be swept into this operation recursively.
    """
    base_dir = set_dir / '02_Base_Edit'
    model_dir = set_dir / '03_Model_Edit'
    discards_dir = base_dir / 'discards'
    if not base_dir.is_dir():
        raise ValueError('02_Base_Edit 不存在')
    if not model_dir.is_dir():
        raise ValueError('03_Model_Edit 不存在')

    base_files = _list_top_level_images(base_dir)
    model_files = _list_top_level_images(model_dir)
    if not base_files:
        raise ValueError('02_Base_Edit 顶层没有 JPG/JPEG/PNG')
    if not model_files:
        raise ValueError('03_Model_Edit 顶层没有 JPG/JPEG/PNG；为避免把整组 Base_Edit 误判成未返图，不执行')

    base_duplicates = _duplicate_stems(base_files)
    if base_duplicates:
        raise ValueError(_format_duplicate_stem_error('02_Base_Edit', base_duplicates))
    model_duplicates = _duplicate_stems(model_files)
    if model_duplicates:
        raise ValueError(_format_duplicate_stem_error('03_Model_Edit', model_duplicates))

    model_stems = {path.stem.casefold() for path in model_files}
    files_to_move = [path for path in base_files if path.stem.casefold() not in model_stems]

    items = []
    operations = []
    signatures = {}
    conflict_count = 0
    for path in [*base_files, *model_files]:
        signatures[str(path)] = _file_signature(path)

    for path in files_to_move:
        destination = discards_dir / path.name
        status = 'move_to_discards'
        if destination.exists():
            status = 'destination_exists'
            conflict_count += 1
            signatures[str(destination)] = _file_signature(destination)
        else:
            operations.append({'src': str(path), 'dst': str(destination)})
        items.append({
            'name': path.name,
            'stem': path.stem,
            'relative_path': f'02_Base_Edit/{path.name}',
            'destination': f'02_Base_Edit/discards/{path.name}',
            'size': path.stat().st_size,
            'status': status,
        })

    return {
        'kind': 'discard_unreturned_base',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'base_dir': str(base_dir),
        'model_dir': str(model_dir),
        'discards_dir': str(discards_dir),
        'operations': operations,
        'items': items,
        'signatures': signatures,
        # Signatures catch edits/removals, while these snapshots also catch files
        # newly added after preview (especially a late Model Edit return).
        'base_names_snapshot': [path.name for path in base_files],
        'model_names_snapshot': [path.name for path in model_files],
        'summary': {
            'base_count': len(base_files),
            'model_count': len(model_files),
            'move_count': len(operations),
            'conflict_count': conflict_count,
            'matched_count': len(base_files) - len(files_to_move),
        },
    }


def _user_immutable_mask():
    mask = getattr(stat, 'UF_IMMUTABLE', None)
    if mask is None or not hasattr(os, 'chflags'):
        raise RuntimeError('Protect Originals 需要 macOS/BSD user immutable file flag 支持')
    return int(mask)


def _file_is_user_immutable(path: Path) -> bool:
    """Read the real filesystem protection state; no database mirror is kept."""
    flags = getattr(path.stat(), 'st_flags', None)
    if flags is None:
        raise RuntimeError('当前文件系统无法读取 user immutable flag')
    return bool(int(flags) & _user_immutable_mask())


def _ensure_user_immutable(path: Path):
    """Add UF_IMMUTABLE while preserving every other existing file flag."""
    st = path.stat()
    flags = getattr(st, 'st_flags', None)
    if flags is None:
        raise RuntimeError('当前文件系统无法读取 user immutable flag')
    os.chflags(path, int(flags) | _user_immutable_mask())


def _direct_files_with_extensions(directory: Path, extensions):
    if not directory.is_dir():
        return []
    allowed = {str(ext).casefold() for ext in extensions}
    return sorted(
        [
            path
            for path in directory.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() in allowed
        ],
        key=lambda path: path.name.casefold(),
    )


def _protect_originals_snapshot(set_dir: Path):
    paths = {
        'base': _list_top_level_images(set_dir / '02_Base_Edit'),
        'model': _list_top_level_images(set_dir / '03_Model_Edit'),
        'jpg': _direct_files_with_extensions(set_dir / '01_Original' / 'JPG', ('.jpg', '.jpeg')),
        'raw': _direct_files_with_extensions(set_dir / '01_Original' / 'RAW', ('.cr3',)),
    }
    return {
        key: [path.name for path in values]
        for key, values in paths.items()
    }, paths


def _build_protect_originals_plan(source_id, set_dir: Path, set_rel: str):
    """Protect Original JPG/RAW when the same exact stem exists in Base or Model Edit."""
    _user_immutable_mask()
    snapshot, paths = _protect_originals_snapshot(set_dir)
    downstream_stages = {}
    for stage_label, stage_files in (('Base', paths['base']), ('Model', paths['model'])):
        for path in stage_files:
            downstream_stages.setdefault(path.stem.casefold(), set()).add(stage_label)

    candidates = []
    signatures = {}
    for kind, original_files, is_jpg in (
        ('JPG', paths['jpg'], True),
        ('RAW', paths['raw'], False),
    ):
        for path in original_files:
            stem_key = original_stem_key(path, is_jpg=is_jpg)
            matched = downstream_stages.get(stem_key)
            if not matched:
                continue
            protected = _file_is_user_immutable(path)
            signatures[str(path)] = _file_signature(path)
            candidates.append({
                'path': str(path),
                'name': path.name,
                'relative_path': path.relative_to(set_dir).as_posix(),
                'kind': kind,
                'stem': path.stem[:-4] if is_jpg and path.stem.casefold().endswith('-dpp') else path.stem,
                'matched_stages': sorted(matched),
                'size': int(path.stat().st_size),
                'status': 'protected' if protected else 'protect',
            })

    candidates.sort(key=lambda item: (item['stem'].casefold(), item['kind'], item['name'].casefold()))
    protected_count = sum(1 for item in candidates if item['status'] == 'protected')
    jpg_count = sum(1 for item in candidates if item['kind'] == 'JPG')
    raw_count = sum(1 for item in candidates if item['kind'] == 'RAW')
    candidate_count = len(candidates)
    return {
        'kind': 'protect_originals',
        'source_id': int(source_id),
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'items': candidates,
        'paths': [item['path'] for item in candidates],
        'signatures': signatures,
        'snapshot': snapshot,
        'summary': {
            'candidate_count': candidate_count,
            'jpg_count': jpg_count,
            'raw_count': raw_count,
            'protected_count': protected_count,
            'unprotected_count': candidate_count - protected_count,
            'all_protected': candidate_count > 0 and protected_count == candidate_count,
        },
    }


def _verify_protect_originals_snapshot(plan, set_dir: Path):
    current_snapshot, _ = _protect_originals_snapshot(set_dir)
    if current_snapshot != plan.get('snapshot'):
        raise RuntimeError('Base/Model 或 Original 文件列表在预览后发生变化，请重新预览')


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
        raise ValueError('01_Original/JPG 或 01_Original/RAW 不存在')

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
        raise ValueError('同步方向不合法')

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
            *(
                [f'{target_type} 中有 {len(target_duplicates)} 个重复 Original stem；同步按 stem 分组判断是否存在对应项，不在同 stem 文件之间自动取舍。']
                if target_duplicates else []
            ),
            *(
                [f'{reference_type} 中有 {len(reference_duplicates)} 个重复 Original stem；同步按 stem 分组匹配。']
                if reference_duplicates else []
            ),
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



_IMAGE_INSPECTION_STAGES = [
    ('base_edit', 'Base Edit', '02_Base_Edit'),
    ('model_edit', 'Model Edit', '03_Model_Edit'),
    ('revision', 'Revision', '04_Revision'),
    ('final', 'Final', '05_Final'),
]
_IMAGE_INSPECTION_EXCLUDED_META_GROUPS = {'ExifTool', 'File', 'System', 'Composite'}
_IMAGE_INSPECTION_BATCH_SIZE = 120
_IMAGE_INSPECTION_JPEG_ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]
_IMAGE_INSPECTION_JPEG_LUMA_BASE = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]
_IMAGE_INSPECTION_JPEG_CHROMA_BASE = [
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
]


def _image_inspection_files(set_dir: Path):
    """Recursively scan Base / Model / Revision / Final for read-only inspection.

    01_Original is intentionally not inspected here. Nested directories remain
    supported, while discard/deleted side paths and AppleDouble files are not
    part of the active image stages.
    """
    entries = []
    for stage_key, stage_label, stage_dir_name in _IMAGE_INSPECTION_STAGES:
        stage_dir = set_dir / stage_dir_name
        if not stage_dir.is_dir():
            continue
        for current_root, dir_names, file_names in os.walk(stage_dir):
            dir_names[:] = [name for name in dir_names if name.casefold() not in {'discards', 'deleted'}]
            root_path = Path(current_root)
            for name in file_names:
                if name.startswith('._'):
                    continue
                path = root_path / name
                if path.suffix.lower() not in _IMAGE_EXTENSIONS:
                    continue
                entries.append({
                    'stage': stage_key,
                    'stage_label': stage_label,
                    'stage_dir_name': stage_dir_name,
                    'stage_dir': stage_dir,
                    'path': path,
                })
    return sorted(
        entries,
        key=lambda item: (
            next(i for i, stage in enumerate(_IMAGE_INSPECTION_STAGES) if stage[0] == item['stage']),
            item['path'].relative_to(item['stage_dir']).as_posix().lower(),
        ),
    )


def _exiftool_group(tag_name: str):
    return tag_name.split(':', 1)[0] if ':' in tag_name else ''


def _embedded_metadata_fields(record):
    fields = []
    for key in record:
        if key == 'SourceFile':
            continue
        group = _exiftool_group(key)
        if group in _IMAGE_INSPECTION_EXCLUDED_META_GROUPS:
            continue
        fields.append(key)
    return sorted(fields, key=str.lower)


def _metadata_value_text(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(_metadata_value_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _configured_metadata_value(record, field):
    # Use the exact same source-tag precedence as Write Metadata.
    for key in field_read_keys(field):
        if key not in record:
            continue
        text = _metadata_value_text(record.get(key))
        if text != '':
            return {'present': True, 'value': text, 'source_tag': key}
    return {'present': False, 'value': '', 'source_tag': ''}


def _record_group_value(record, group: str, tag: str):
    return record.get(f'{group}:{tag}')


def _record_first_text(record, keys):
    for key in keys:
        text = _metadata_value_text(record.get(key)).strip()
        if text:
            return text
    return ''


def _is_srgb_label(value: str):
    normalized = re.sub(r'[^a-z0-9]+', '', value.casefold())
    return 'nonsrgb' not in normalized and ('srgb' in normalized or 'iec6196621' in normalized)


def _image_color_space_analysis(record):
    """Return conservative color-space information for Image Inspection.

    An embedded ICC profile is the strongest named-space evidence. With no ICC,
    only explicit sRGB / Adobe RGB declarations are treated as conclusive;
    missing, uncalibrated, or otherwise ambiguous EXIF stays unknown so the UI
    does not raise a false non-sRGB warning.
    """
    profile_description = _record_first_text(record, [
        'ICC_Profile:ProfileDescription',
        'ICC_Profile:ProfileName',
    ])
    if profile_description:
        if _is_srgb_label(profile_description):
            return {
                'status': 'srgb',
                'display': 'sRGB',
                'tag': '',
                'evidence': 'ICC profile',
            }
        return {
            'status': 'non_srgb',
            'display': profile_description,
            'tag': profile_description,
            'evidence': 'ICC profile',
        }

    # A PNG sRGB chunk is an explicit standard declaration.
    if _record_first_text(record, ['PNG:SRGBRendering', 'PNG:sRGBRendering']):
        return {
            'status': 'srgb',
            'display': 'sRGB',
            'tag': '',
            'evidence': 'PNG sRGB',
        }

    interop_index = _record_first_text(record, [
        'InteropIFD:InteropIndex',
        'EXIF:InteropIndex',
    ])
    interop_upper = interop_index.upper()
    if interop_upper.startswith('R98'):
        return {
            'status': 'srgb',
            'display': 'sRGB',
            'tag': '',
            'evidence': 'EXIF InteropIndex',
        }
    if interop_upper.startswith('R03'):
        return {
            'status': 'non_srgb',
            'display': 'Adobe RGB',
            'tag': 'Adobe RGB',
            'evidence': 'EXIF InteropIndex',
        }

    explicit_srgb = False
    explicit_non_srgb = []
    for key, value in record.items():
        if key.rsplit(':', 1)[-1].casefold() != 'colorspace':
            continue
        color_space = _metadata_value_text(value).strip()
        if not color_space:
            continue
        color_cf = color_space.casefold()
        if _is_srgb_label(color_space):
            explicit_srgb = True
            continue
        if any(token in color_cf for token in (
            'adobe rgb',
            'wide gamut rgb',
            'display p3',
            'dci-p3',
            'prophoto',
            'romm',
            'rec.2020',
            'bt.2020',
        )):
            explicit_non_srgb.append(color_space)

    if explicit_non_srgb and not explicit_srgb:
        color_space = explicit_non_srgb[0]
        return {
            'status': 'non_srgb',
            'display': color_space,
            'tag': color_space,
            'evidence': 'ColorSpace tag',
        }
    if explicit_srgb and not explicit_non_srgb:
        return {
            'status': 'srgb',
            'display': 'sRGB',
            'tag': '',
            'evidence': 'ColorSpace tag',
        }

    return {
        'status': 'unknown',
        'display': 'Unknown',
        'tag': '',
        'evidence': '',
    }


def _image_bit_depth_analysis(record):
    raw_value = None
    for key in (
        'File:BitsPerSample',
        'PNG:BitDepth',
        'JPEG:BitsPerSample',
        'IFD0:BitsPerSample',
        'ExifIFD:BitsPerSample',
    ):
        if key in record and _metadata_value_text(record.get(key)).strip():
            raw_value = record.get(key)
            break

    if raw_value is None:
        return {'status': 'unknown', 'display': 'Unknown', 'tag': ''}

    text = _metadata_value_text(raw_value).strip()
    values = [int(value) for value in re.findall(r'(?<![.\d])\d+(?![.\d])', text)]
    if not values:
        return {'status': 'unknown', 'display': text or 'Unknown', 'tag': ''}

    unique = sorted(set(values))
    if len(unique) == 1:
        display = f'{unique[0]}-bit'
    else:
        display = '/'.join(str(value) for value in unique) + '-bit'

    if all(value == 8 for value in values):
        return {'status': 'standard', 'display': '8-bit', 'tag': ''}
    return {'status': 'warning', 'display': display, 'tag': display}


def _canonical_final_exif_analysis(record, metadata_fields, field_defs):
    fields = []
    missing = []
    expected_keys = set()
    for field in field_defs:
        output_key = field_output_key(field)
        expected_keys.add(output_key)
        text = _metadata_value_text(record.get(output_key))
        present = text != ''
        fields.append({
            'name': field['key'],
            'label': field['label'],
            'present': present,
            'value': text,
            'source_tag': output_key if present else '',
        })
        if not present:
            missing.append(field['key'])

    extras = []
    for tag_name in metadata_fields:
        group = _exiftool_group(tag_name)
        if tag_name in expected_keys or tag_name in FINAL_STRUCTURAL_FIELDS:
            continue
        if group == 'JFIF' or group.startswith('ICC'):
            continue
        extras.append(tag_name)

    return {
        'fields': fields,
        'present': len(fields) - len(missing),
        'total': len(fields),
        'missing': missing,
        'extras': sorted(extras, key=str.lower),
        'exact': not missing and not extras,
    }


def _parse_exiftool_full_output(text: str):
    groups = {}
    order = []
    previous = None
    for line in text.splitlines():
        match = re.match(r'^\[([^\]]+)\]\s+(\S+)\s*:\s?(.*)$', line)
        if match:
            group, tag, value = match.groups()
            if group not in groups:
                groups[group] = []
                order.append(group)
            field = {
                'tag': tag,
                'full_tag': f'{group}:{tag}',
                'value': value,
            }
            groups[group].append(field)
            previous = field
            continue
        if previous is not None and line.strip():
            previous['value'] += '\n' + line.rstrip()
    return [{'name': group, 'fields': groups[group]} for group in order]


def _jpeg_quality_table(base, quality: int):
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    natural = [max(1, min(255, (value * scale + 50) // 100)) for value in base]
    return [natural[index] for index in _IMAGE_INSPECTION_JPEG_ZIGZAG]


def _inspect_final_jpeg_structure(path: Path):
    result = {
        'jfif': None,
        'icc_sha256': '',
        'icc_bytes': 0,
        'has_adobe_app14': False,
        'quant_tables': {},
        'huffman_tables': {},
        'error': '',
    }
    try:
        data = path.read_bytes()
        if len(data) < 4 or data[:2] != b'\xff\xd8':
            result['error'] = 'Missing JPEG SOI'
            return result

        icc_chunks = {}
        pos = 2
        while pos < len(data):
            if data[pos] != 0xFF:
                next_marker = data.find(b'\xff', pos)
                if next_marker < 0:
                    break
                pos = next_marker
            while pos < len(data) and data[pos] == 0xFF:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker in {0xD9, 0xDA}:
                break
            if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            if pos + 2 > len(data):
                result['error'] = 'Truncated JPEG marker length'
                break
            length = int.from_bytes(data[pos:pos + 2], 'big')
            if length < 2 or pos + length > len(data):
                result['error'] = 'Invalid JPEG marker length'
                break
            payload = data[pos + 2:pos + length]
            pos += length

            if marker == 0xE0 and payload.startswith(b'JFIF\x00') and len(payload) >= 14:
                result['jfif'] = {
                    'version': f'{payload[5]}.{payload[6]:02d}',
                    'unit': payload[7],
                    'x_density': int.from_bytes(payload[8:10], 'big'),
                    'y_density': int.from_bytes(payload[10:12], 'big'),
                    'thumb_width': payload[12],
                    'thumb_height': payload[13],
                }
            elif marker == 0xE2 and payload.startswith(b'ICC_PROFILE\x00') and len(payload) >= 14:
                sequence = payload[12]
                count = payload[13]
                icc_chunks[sequence] = (count, payload[14:])
            elif marker == 0xEE and payload.startswith(b'Adobe'):
                result['has_adobe_app14'] = True
            elif marker == 0xDB:
                offset = 0
                while offset < len(payload):
                    info = payload[offset]
                    offset += 1
                    precision = info >> 4
                    table_id = info & 0x0F
                    value_bytes = 128 if precision else 64
                    if offset + value_bytes > len(payload):
                        result['error'] = 'Truncated JPEG quantization table'
                        break
                    if precision:
                        values = [
                            int.from_bytes(payload[offset + index * 2:offset + index * 2 + 2], 'big')
                            for index in range(64)
                        ]
                    else:
                        values = list(payload[offset:offset + 64])
                    result['quant_tables'][table_id] = values
                    offset += value_bytes
            elif marker == 0xC4:
                offset = 0
                while offset < len(payload):
                    if offset + 17 > len(payload):
                        result['error'] = 'Truncated JPEG Huffman table'
                        break
                    info = payload[offset]
                    offset += 1
                    table_class = info >> 4
                    table_id = info & 0x0F
                    code_counts = list(payload[offset:offset + 16])
                    offset += 16
                    symbol_count = sum(code_counts)
                    if offset + symbol_count > len(payload):
                        result['error'] = 'Truncated JPEG Huffman symbols'
                        break
                    symbols = list(payload[offset:offset + symbol_count])
                    offset += symbol_count
                    result['huffman_tables'][f'{table_class}:{table_id}'] = {
                        'code_counts': code_counts,
                        'symbols': symbols,
                    }

        if icc_chunks:
            expected_count = next(iter(icc_chunks.values()))[0]
            if all(index in icc_chunks for index in range(1, expected_count + 1)):
                icc = b''.join(icc_chunks[index][1] for index in range(1, expected_count + 1))
                result['icc_sha256'] = hashlib.sha256(icc).hexdigest()
                result['icc_bytes'] = len(icc)
            else:
                result['error'] = result['error'] or 'Incomplete ICC APP2 sequence'
    except OSError as exc:
        result['error'] = str(exc)
    return result


def _final_delivery_analysis(path: Path, record, width: int, height: int, canonical_exif):
    """Validate distribution-only invariants for a Final image.

    Normal results stay compact in the UI. Detailed packaging rules are retained
    here and only failed rules are surfaced to the user.
    """
    file_type = _metadata_value_text(_record_group_value(record, 'File', 'FileType'))
    encoding = _metadata_value_text(_record_group_value(record, 'File', 'EncodingProcess'))
    bits = _metadata_value_text(_record_group_value(record, 'File', 'BitsPerSample'))
    subsampling = _metadata_value_text(_record_group_value(record, 'File', 'YCbCrSubSampling'))
    profile_description = _metadata_value_text(_record_group_value(record, 'ICC_Profile', 'ProfileDescription'))
    jpeg_structure = _inspect_final_jpeg_structure(path)
    jfif = jpeg_structure.get('jfif') or {}
    jfif_ok = (
        jfif.get('version') == '1.01'
        and jfif.get('unit') == 0
        and jfif.get('x_density') == 1
        and jfif.get('y_density') == 1
        and jfif.get('thumb_width') == 0
        and jfif.get('thumb_height') == 0
    )
    icc_ok = (
        profile_description == FINAL_SRGB_PROFILE_DESCRIPTION
        and jpeg_structure.get('icc_sha256') == FINAL_SRGB_ICC_SHA256
        and jpeg_structure.get('icc_bytes') == FINAL_SRGB_ICC_BYTES
    )
    quant_tables = jpeg_structure.get('quant_tables') or {}
    quality_ok = (
        quant_tables.get(0) == _jpeg_quality_table(_IMAGE_INSPECTION_JPEG_LUMA_BASE, FINAL_JPEG_QUALITY)
        and quant_tables.get(1) == _jpeg_quality_table(_IMAGE_INSPECTION_JPEG_CHROMA_BASE, FINAL_JPEG_QUALITY)
    )

    exact_dimensions = (width, height) in allowed_final_dimensions()
    exact_ratio = bool(
        width and height and (
            (height >= width and width * 3 == height * 2)
            or (width > height and width * 2 == height * 3)
        )
    )
    huffman_keys = set((jpeg_structure.get('huffman_tables') or {}).keys())
    huffman_ok = huffman_keys == {'0:0', '1:0', '0:1', '1:1'}
    exif_details = []
    if canonical_exif['missing']:
        exif_details.append('Missing: ' + ', '.join(canonical_exif['missing']))
    if canonical_exif['extras']:
        exif_details.append('Extra: ' + ', '.join(canonical_exif['extras']))
    exif_value = (
        f"{canonical_exif['present']}/{canonical_exif['total']}"
        if canonical_exif['exact']
        else ' · '.join(exif_details) or 'Not exact'
    )

    subsampling_ok = FINAL_JPEG_CHROMA_SAMPLING in subsampling
    if jfif:
        jfif_unit = {0: 'unitless', 1: 'dpi', 2: 'dpcm'}.get(
            jfif.get('unit'),
            f"unit {jfif.get('unit')}" if jfif.get('unit') is not None else 'unit missing',
        )
        jfif_value = (
            f"v{jfif.get('version') or '?'} · {jfif_unit} · "
            f"{jfif.get('x_density', '?')}×{jfif.get('y_density', '?')}"
        )
        thumb_width = int(jfif.get('thumb_width') or 0)
        thumb_height = int(jfif.get('thumb_height') or 0)
        jfif_value += (
            ' · no thumbnail'
            if not thumb_width and not thumb_height
            else f' · thumbnail {thumb_width}×{thumb_height}'
        )
    else:
        jfif_value = 'Missing'

    exact_ratio_value = ('2:3' if height >= width else '3:2') if exact_ratio else 'Not exact'
    if icc_ok:
        icc_value = FINAL_SRGB_PROFILE_DESCRIPTION
    elif not jpeg_structure.get('icc_sha256'):
        icc_value = 'Missing'
    elif profile_description == FINAL_SRGB_PROFILE_DESCRIPTION:
        icc_value = f'{profile_description} · profile mismatch'
    else:
        icc_value = profile_description or 'Profile mismatch'

    checks = [
        ('jpeg', 'File Format', file_type.upper() == 'JPEG', 'JPEG' if file_type.upper() == 'JPEG' else (file_type or 'Missing')),
        ('dimensions', 'Pixel Dimensions', exact_dimensions, f'{width}×{height}' if width and height else 'Missing'),
        ('ratio', 'Aspect Ratio', exact_ratio, exact_ratio_value),
        ('baseline', 'JPEG Encoding', encoding.startswith('Baseline DCT'), 'Baseline DCT' if encoding.startswith('Baseline DCT') else (encoding or 'Missing')),
        ('bits', 'Bit Depth', bits == '8', '8-bit' if bits == '8' else (f'{bits}-bit' if bits else 'Missing')),
        ('subsampling', 'Chroma Sampling', subsampling_ok, FINAL_JPEG_CHROMA_SAMPLING if subsampling_ok else (subsampling or 'Missing')),
        ('icc', 'ICC Profile', icc_ok, icc_value),
        ('quantization', 'JPEG Quality', quality_ok, f'Q{FINAL_JPEG_QUALITY}' if quality_ok else f'Not Q{FINAL_JPEG_QUALITY}'),
        ('huffman', 'Huffman Tables', huffman_ok, f'{len(huffman_keys)} DHT tables' if huffman_keys else 'Missing'),
        ('jfif', 'JFIF Header', jfif_ok, jfif_value),
        ('adobe_app14', 'Adobe APP14', not jpeg_structure.get('has_adobe_app14'), 'Absent' if not jpeg_structure.get('has_adobe_app14') else 'Present'),
        ('canonical_exif', 'Metadata Contract', canonical_exif['exact'], exif_value),
    ]
    if jpeg_structure.get('error'):
        checks.append(('jpeg_structure', 'JPEG structure', False, jpeg_structure['error']))

    failures = [
        {'name': name, 'label': label, 'value': value}
        for name, label, ok, value in checks
        if not ok
    ]
    passed = sum(1 for _name, _label, ok, _value in checks if ok)
    total = len(checks)
    return {
        'final_qc_checks': [
            {'name': name, 'label': label, 'ok': ok, 'value': value}
            for name, label, ok, value in checks
        ],
        'final_qc_failures': failures,
        'final_qc_issue_count': len(failures),
        'final_qc_present': passed,
        'final_qc_total': total,
        'final_qc_missing': len(failures),
        'final_qc_status_level': 'success' if not failures else 'error',
        'final_qc_status_text': 'Final 合格' if not failures else 'Final 异常',
        'final_bit_depth': f'{bits}-bit' if bits else 'Missing',
        'final_canonical_exif_present': canonical_exif['present'],
        'final_canonical_exif_total': canonical_exif['total'],
        'final_canonical_exif_missing': canonical_exif['missing'],
        'final_canonical_exif_extras': canonical_exif['extras'],
        'final_canonical_exif_exact': canonical_exif['exact'],
    }


def _extract_dimension(record, tag):
    preferred = [
        f'File:{tag}',
        f'EXIF:{tag}',
        f'PNG:{tag}',
        f'JPEG:{tag}',
    ]
    for key in preferred:
        if key in record:
            try:
                return int(record[key])
            except (TypeError, ValueError):
                pass
    for key, value in record.items():
        if key.split(':', 1)[-1] == tag:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def _format_ratio_part(numerator: int, denominator: int, exact_value: int):
    """Format a normalized ratio component without rounding it into a false exact ratio.

    Start with four decimal places, truncating rather than rounding. If a non-exact
    ratio is so close to the target integer that four places would collapse to the
    exact-looking value, increase precision up to six places.
    """
    if denominator <= 0:
        return '—'

    for decimals in range(4, 7):
        scale = 10 ** decimals
        scaled = numerator * scale // denominator
        integer = scaled // scale
        fraction = scaled % scale
        text = f'{integer}.{fraction:0{decimals}d}'.rstrip('0').rstrip('.')
        if text != str(exact_value):
            return text

    # With ordinary image dimensions six decimal places is already more than enough.
    # Keep a bounded display even for pathological dimensions while still making it
    # explicit that the ratio is not exact.
    return f'>{exact_value}' if numerator > exact_value * denominator else f'<{exact_value}'


def _ratio_analysis(width: int, height: int):
    if width <= 0 or height <= 0:
        return {
            'orientation': 'unknown',
            'ratio_value': None,
            'ratio_display': '—',
            'target_ratio': '',
            'exact_ratio': False,
            'crop_width': 0,
            'crop_height': 0,
            'crop_width_px': 0,
            'crop_height_px': 0,
            'crop_left_px': 0,
            'crop_right_px': 0,
            'crop_top_px': 0,
            'crop_bottom_px': 0,
            'crop_area_percent': None,
            'crop_adjustment': '—',
            'crop_detail': '—',
            'final_target': '',
            'perfect_dimensions': False,
            'resize_scale': None,
            'resize_scale_display': '—',
            'resize_pixel_loss': None,
            'resize_pixel_loss_percent': None,
            'resize_pixel_loss_display': '—',
            'pixel_insufficient': True,
            'crop_loss_excessive': False,
            'display_status_level': 'error',
            'display_status_text': 'Read Error',
            'status_ok': False,
        }

    portrait = height >= width
    if portrait:
        orientation = 'portrait' if height > width else 'square'
        target_ratio = '2:3'
        unit = min(width // 2, height // 3)
        crop_width = unit * 2
        crop_height = unit * 3
        exact = width * 3 == height * 2
        ratio_display = '2:3' if exact else f'{_format_ratio_part(width * 3, height, 2)}:3'
    else:
        orientation = 'landscape'
        target_ratio = '3:2'
        unit = min(width // 3, height // 2)
        crop_width = unit * 3
        crop_height = unit * 2
        exact = width * 2 == height * 3
        ratio_display = '3:2' if exact else f'3:{_format_ratio_part(height * 3, width, 2)}'

    target = choose_target_for_crop(crop_width, crop_height, portrait)
    final_width = int(target['target_width'])
    final_height = int(target['target_height'])

    crop_width_px = max(0, width - crop_width)
    crop_height_px = max(0, height - crop_height)
    crop_left_px = crop_width_px // 2
    crop_right_px = crop_width_px - crop_left_px
    crop_top_px = crop_height_px // 2
    crop_bottom_px = crop_height_px - crop_top_px
    source_area = width * height
    crop_area = max(0, source_area - crop_width * crop_height)
    crop_area_percent = (crop_area / source_area * 100.0) if source_area else 0.0

    crop_parts = []
    if crop_width_px:
        crop_parts.append(f'W -{crop_width_px}px')
    if crop_height_px:
        crop_parts.append(f'H -{crop_height_px}px')
    crop_adjustment = 'Exact' if exact else (' · '.join(crop_parts) if crop_parts else 'Crop required')
    crop_detail = (
        'No crop' if exact else
        f'L {crop_left_px}px · R {crop_right_px}px · T {crop_top_px}px · B {crop_bottom_px}px'
    )

    # Pixel sufficiency uses the exact center-crop dimensions because the active
    # Final policy is defined on the pixels that can actually reach Final.
    pixel_insufficient = bool(target['pixel_insufficient'])
    crop_loss_excessive = (not exact) and crop_area_percent > FINAL_CROP_WARNING_PERCENT
    perfect_dimensions = exact and (width, height) in allowed_final_dimensions()

    crop_pixel_count = crop_width * crop_height
    target_pixel_count = final_width * final_height
    resize_scale = (final_width / crop_width) if crop_width else None
    if pixel_insufficient:
        resize_pixel_loss = None
        resize_pixel_loss_percent = None
        resize_pixel_loss_display = '— · upscale blocked'
    else:
        resize_pixel_loss = max(0, crop_pixel_count - target_pixel_count)
        resize_pixel_loss_percent = (
            resize_pixel_loss / crop_pixel_count * 100.0
            if crop_pixel_count else 0.0
        )
        resize_pixel_loss_display = f'{resize_pixel_loss:,} px · {resize_pixel_loss_percent:.2f}%'
    resize_scale_display = f'{resize_scale:.4f}×' if resize_scale is not None else '—'

    # Perfect is reserved for files already matching a canonical Final tier.
    # Exact-ratio source files at any other sufficient size still need resize.
    if pixel_insufficient:
        display_status_level = 'error'
        display_status_text = 'Low Res'
    elif perfect_dimensions:
        display_status_level = 'success'
        display_status_text = 'Perfect'
    elif exact:
        display_status_level = 'success'
        display_status_text = 'Exact Ratio'
    elif crop_loss_excessive:
        display_status_level = 'error'
        display_status_text = 'Ratio Error'
    else:
        display_status_level = 'warning'
        display_status_text = 'Slight Off'

    return {
        'orientation': orientation,
        'ratio_value': round(width / height, 6),
        'ratio_display': ratio_display,
        'target_ratio': target_ratio,
        'exact_ratio': exact,
        'crop_width': crop_width,
        'crop_height': crop_height,
        'crop_width_px': crop_width_px,
        'crop_height_px': crop_height_px,
        'crop_left_px': crop_left_px,
        'crop_right_px': crop_right_px,
        'crop_top_px': crop_top_px,
        'crop_bottom_px': crop_bottom_px,
        'crop_area_percent': round(crop_area_percent, 4),
        'crop_adjustment': crop_adjustment,
        'crop_detail': crop_detail,
        'final_target': f'{final_width}×{final_height}',
        'perfect_dimensions': perfect_dimensions,
        'resize_scale': round(resize_scale, 6) if resize_scale is not None else None,
        'resize_scale_display': resize_scale_display,
        'resize_pixel_loss': resize_pixel_loss,
        'resize_pixel_loss_percent': round(resize_pixel_loss_percent, 4) if resize_pixel_loss_percent is not None else None,
        'resize_pixel_loss_display': resize_pixel_loss_display,
        'pixel_insufficient': pixel_insufficient,
        'crop_loss_excessive': crop_loss_excessive,
        'display_status_level': display_status_level,
        'display_status_text': display_status_text,
        'status_ok': display_status_level == 'success',
    }

def _run_exiftool_records(exiftool_path: str, files):
    records = []
    for offset in range(0, len(files), _IMAGE_INSPECTION_BATCH_SIZE):
        chunk = files[offset:offset + _IMAGE_INSPECTION_BATCH_SIZE]
        command = [
            exiftool_path,
            '-j',
            '-a',
            '-G1',
            '-s',
            '-charset',
            'filename=UTF8',
            *[str(path) for path in chunk],
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
            check=False,
        )
        if not result.stdout.strip():
            detail = result.stderr.strip() or f'ExifTool exited with code {result.returncode}'
            raise RuntimeError(f'ExifTool 扫描失败：{detail}')
        try:
            batch_records = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'ExifTool 返回结果无法解析：{exc}') from exc
        if not isinstance(batch_records, list):
            raise RuntimeError('ExifTool 返回结果格式异常')
        records.extend(batch_records)
    return records


def _run_exiftool_full_metadata(exiftool_path: str, path: Path):
    command = [
        exiftool_path,
        '-G1',
        '-a',
        '-u',
        '-s',
        '-charset',
        'filename=UTF8',
        str(path),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
        check=False,
    )
    if not result.stdout.strip():
        detail = result.stderr.strip() or f'ExifTool exited with code {result.returncode}'
        raise RuntimeError(f'ExifTool 元数据读取失败：{detail}')
    groups = _parse_exiftool_full_output(result.stdout)
    if not groups:
        raise RuntimeError('ExifTool 元数据结果为空')
    return groups


def _natural_text_key(value: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', value)]


def _build_image_inspection(set_dir: Path):
    field_defs = load_metadata_fields()
    key_exif_fields = [
        {'key': field['key'], 'label': field['label']}
        for field in field_defs
    ]

    exiftool_path = resolve_exiftool()
    if not exiftool_path:
        raise FileNotFoundError('ExifTool 未安装或不可用；图像检测不会 fallback 到 Pillow。')
    validate_metadata_fields_with_exiftool(exiftool_path, field_defs)

    version = probe_exiftool_version(exiftool_path)

    entries = _image_inspection_files(set_dir)
    if not entries:
        return {
            'exiftool_version': version,
            'contract': public_final_delivery_contract(),
            'summary': {
                'file_count': 0,
                'perfect_count': 0,
                'exact_ratio_count': 0,
                'ratio_error_count': 0,
                'pixel_insufficient_count': 0,
            },
            'key_exif_fields': key_exif_fields,
            'rows': [],
        }

    files = [entry['path'] for entry in entries]
    records = _run_exiftool_records(exiftool_path, files)
    by_path = {}
    for record in records:
        source = record.get('SourceFile')
        if not source:
            continue
        try:
            key = str(Path(source).resolve())
        except OSError:
            key = str(Path(source))
        by_path[key] = record

    items = []
    perfect_count = 0
    exact_ratio_count = 0
    ratio_error_count = 0
    pixel_insufficient_count = 0

    for entry in entries:
        path = entry['path']
        stage_dir = entry['stage_dir']
        record = by_path.get(str(path.resolve()), {})
        width = _extract_dimension(record, 'ImageWidth')
        height = _extract_dimension(record, 'ImageHeight')
        ratio = _ratio_analysis(width, height)

        if ratio['pixel_insufficient']:
            pixel_insufficient_count += 1
        elif ratio['perfect_dimensions']:
            perfect_count += 1
        elif ratio['exact_ratio']:
            exact_ratio_count += 1
        else:
            ratio_error_count += 1

        focus_fields = []
        focus_present = 0
        for field in field_defs:
            value = _configured_metadata_value(record, field)
            focus_fields.append({'name': field['key'], 'label': field['label'], **value})
            if value['present']:
                focus_present += 1
        metadata_fields = _embedded_metadata_fields(record)
        color_space = _image_color_space_analysis(record)
        bit_depth = _image_bit_depth_analysis(record)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = None
        final_qc = {}
        if entry['stage'] == 'final':
            canonical_exif = _canonical_final_exif_analysis(record, metadata_fields, field_defs)
            focus_fields = canonical_exif['fields']
            focus_present = canonical_exif['present']
            final_qc = _final_delivery_analysis(path, record, width, height, canonical_exif)

        stage_relative_path = path.relative_to(stage_dir).as_posix()
        items.append({
            'stage': entry['stage'],
            'stage_label': entry['stage_label'],
            'stage_dir_name': entry['stage_dir_name'],
            'file': path.name,
            'stem': path.stem,
            'match_key': path.stem.casefold(),
            'stage_relative_path': stage_relative_path,
            'relative_path': path.relative_to(set_dir).as_posix(),
            'width': width,
            'height': height,
            'resolution': f'{width}×{height}' if width and height else '—',
            'size_bytes': size_bytes,
            'color_space_status': color_space['status'],
            'color_space_display': color_space['display'],
            'color_space_tag': color_space['tag'],
            'color_space_evidence': color_space['evidence'],
            'bit_depth_status': bit_depth['status'],
            'bit_depth_display': bit_depth['display'],
            'bit_depth_tag': bit_depth['tag'],
            **ratio,
            'metadata_field_count': len(metadata_fields),
            'metadata_focus_present': focus_present,
            'metadata_focus_total': len(field_defs),
            'metadata_focus_missing': len(field_defs) - focus_present,
            'metadata_status_level': 'success' if focus_present == len(field_defs) else 'error',
            'metadata_status_text': (
                'EXIF完整' if focus_present == len(field_defs)
                else 'EXIF缺失'
            ),
            'focus_fields': focus_fields,
            'focus_field_map': {field['name']: field for field in focus_fields},
            **final_qc,
            'metadata_error': _metadata_value_text(record.get('ExifTool:Error') or record.get('File:Error') or ''),
        })

    groups = {}
    for item in items:
        group = groups.setdefault(item['match_key'], {
            'display_stem': item['stem'],
            **{stage_key: [] for stage_key, _stage_label, _stage_dir_name in _IMAGE_INSPECTION_STAGES},
        })
        group[item['stage']].append(item)

    rows = []
    for match_key, group in groups.items():
        for stage_key, _stage_label, _stage_dir_name in _IMAGE_INSPECTION_STAGES:
            group[stage_key].sort(key=lambda item: _natural_text_key(item['stage_relative_path']))
        max_count = max(len(group[stage_key]) for stage_key, _, _ in _IMAGE_INSPECTION_STAGES)
        for index in range(max_count):
            row = {
                'row_key': f'{match_key}:{index}',
                'stem': group['display_stem'],
            }
            for stage_key, _stage_label, _stage_dir_name in _IMAGE_INSPECTION_STAGES:
                row[stage_key] = group[stage_key][index] if index < len(group[stage_key]) else None
            rows.append(row)

    rows.sort(key=lambda row: _natural_text_key(row['row_key']))

    return {
        'exiftool_version': version,
        'contract': public_final_delivery_contract(),
        'key_exif_fields': key_exif_fields,
        'summary': {
            'file_count': len(items),
            'perfect_count': perfect_count,
            'exact_ratio_count': exact_ratio_count,
            'ratio_error_count': ratio_error_count,
            'pixel_insufficient_count': pixel_insufficient_count,
        },
        'rows': rows,
    }

def create_workflow_blueprint(admin_guard, get_source, resolve_path, get_db_connection):
    bp = Blueprint('workflow_tools', __name__)

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


    @bp.route('/api/library/workflow/sources/<int:source_id>/image-inspection', methods=['POST'])
    def image_inspection(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            return jsonify(_build_image_inspection(set_dir))
        except FileNotFoundError as exc:
            return jsonify({'error': str(exc)}), 503
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/image-inspection/metadata', methods=['POST'])
    def image_inspection_metadata(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            relative_path = str(data.get('relative_path') or '').strip().replace('\\', '/')
            if not relative_path:
                raise ValueError('缺少图片路径')

            candidate = (set_dir / relative_path).resolve()
            set_resolved = set_dir.resolve()
            try:
                candidate.relative_to(set_resolved)
            except ValueError as exc:
                raise ValueError('图片路径超出当前 Set') from exc

            relative = candidate.relative_to(set_resolved)
            if not relative.parts or relative.parts[0] not in {stage[2] for stage in _IMAGE_INSPECTION_STAGES}:
                raise ValueError('图片不属于图像检测 stage')
            if any(part.casefold() in {'deleted', 'discards'} for part in relative.parts):
                raise ValueError('Deleted / discards 不属于图像检测范围')
            if candidate.name.startswith('._') or candidate.suffix.lower() not in _IMAGE_EXTENSIONS or not candidate.is_file():
                raise FileNotFoundError('图片不存在或格式不支持')

            exiftool_path = resolve_exiftool()
            if not exiftool_path:
                raise FileNotFoundError('ExifTool 未安装或不可用')
            groups = _run_exiftool_full_metadata(exiftool_path, candidate)
            return jsonify({
                'file': candidate.name,
                'relative_path': relative.as_posix(),
                'field_count': sum(len(group['fields']) for group in groups),
                'groups': groups,
                'command': 'exiftool -G1 -a -u -s',
            })
        except FileNotFoundError as exc:
            return jsonify({'error': str(exc)}), 404
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400


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

    @bp.route('/api/library/workflow/sources/<int:source_id>/discard-unreturned/preview', methods=['POST'])
    def discard_unreturned_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_discard_unreturned_base_plan(source_id, set_dir, set_rel)
            plan_id = _remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'items': plan['items'],
                'summary': plan['summary'],
                'destination': '02_Base_Edit/discards',
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/discard-unreturned/start', methods=['POST'])
    def discard_unreturned_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), 'discard_unreturned_base', source_id, set_rel)
            _verify_signatures(plan['signatures'])

            base_dir = Path(plan['base_dir'])
            model_dir = Path(plan['model_dir'])
            current_base_names = [path.name for path in _list_top_level_images(base_dir)]
            current_model_names = [path.name for path in _list_top_level_images(model_dir)]
            if current_base_names != plan['base_names_snapshot'] or current_model_names != plan['model_names_snapshot']:
                raise RuntimeError('Base_Edit 或 Model_Edit 在预览后发生变化，请重新预览')
            if plan['summary'].get('conflict_count', 0):
                raise RuntimeError('discards 中存在同名文件冲突，请先处理冲突后重新预览')

            operations = list(plan['operations'])
            task_id = _new_task('discard_unreturned_base', len(operations))

            def worker():
                moved = 0
                try:
                    _update_task(task_id, status='running', message='开始移动未返图 Base…')
                    discards_dir = Path(plan['discards_dir'])
                    for item in operations:
                        src = Path(item['src'])
                        dst = Path(item['dst'])
                        if not src.exists():
                            raise FileNotFoundError(f'Base 文件不存在: {src.name}')
                        if dst.exists():
                            raise FileExistsError(f'discards 中已存在同名文件: {dst.name}')
                    if operations:
                        discards_dir.mkdir(parents=True, exist_ok=True)

                    for index, item in enumerate(operations, start=1):
                        src = Path(item['src'])
                        dst = Path(item['dst'])

                        old_rel = src.relative_to(root).as_posix()
                        shutil.move(str(src), str(dst))
                        new_rel = dst.relative_to(root).as_posix()

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

                        moved += 1
                        message = f'{src.name} → 02_Base_Edit/discards/'
                        _update_task(task_id, completed=index, current=index, message=message, log=message)

                    _update_task(
                        task_id,
                        status='done',
                        message=f'完成：移动 {moved} 个 Base 文件到 discards',
                        result={'moved_count': moved, 'destination': '02_Base_Edit/discards'},
                    )
                except Exception as exc:
                    _update_task(task_id, status='error', message='移动未返图 Base 失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/workflow/sources/<int:source_id>/protect-originals/status', methods=['POST'])
    def protect_originals_status(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_protect_originals_plan(source_id, set_dir, set_rel)
            return jsonify({'summary': plan['summary']})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/protect-originals/preview', methods=['POST'])
    def protect_originals_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _build_protect_originals_plan(source_id, set_dir, set_rel)
            plan_id = _remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'items': plan['items'],
                'summary': plan['summary'],
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/protect-originals/start', methods=['POST'])
    def protect_originals_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), 'protect_originals', source_id, set_rel)
            _verify_signatures(plan['signatures'])
            _verify_protect_originals_snapshot(plan, set_dir)
            paths = [Path(path) for path in plan['paths']]
            if not paths:
                raise ValueError('当前没有需要保护的 Original JPG/RAW')
            task_id = _new_task('protect_originals', len(paths))

            def worker():
                newly_protected = 0
                already_protected = 0
                try:
                    _update_task(task_id, status='running', message='开始设置 Original 保护…')
                    for index, path in enumerate(paths, start=1):
                        if not path.is_file():
                            raise FileNotFoundError(f'Original 文件不存在: {path.name}')
                        was_protected = _file_is_user_immutable(path)
                        _ensure_user_immutable(path)
                        if not _file_is_user_immutable(path):
                            raise RuntimeError(f'保护设置未生效: {path.name}')
                        if was_protected:
                            already_protected += 1
                            message = f'{path.name} · 已设置保护'
                        else:
                            newly_protected += 1
                            message = f'{path.name} · 已设置保护'
                        _update_task(task_id, completed=index, current=index, message=message, log=message)
                    result = {
                        'protected_count': len(paths),
                        'newly_protected_count': newly_protected,
                        'already_protected_count': already_protected,
                    }
                    _update_task(
                        task_id,
                        status='done',
                        message=f'完成：已确认保护 {len(paths)} 个 Original 文件',
                        result=result,
                    )
                except Exception as exc:
                    _update_task(task_id, status='error', message='设置 Original 保护失败', error=str(exc), log=f'ERROR: {exc}')

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

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/preview', methods=['POST'])
    def photo_import_preview(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _build_photo_import_plan(
                source_id,
                set_dir,
                set_rel,
                data.get('source_root') or '/Users/flc/Pictures/Camera Exports/',
                data.get('gap_minutes', 30),
            )
            plan_id = _remember_plan(plan)
            return jsonify(_photo_import_public_plan(plan, plan_id))
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/thumbnail/<plan_id>/<file_id>', methods=['GET'])
    def photo_import_thumbnail(source_id, plan_id, file_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = resolve_set(source_id, request.args.get('path', ''))
            plan = _get_plan(str(plan_id), 'photo_import_preview', source_id, set_rel)
            record = plan['records'].get(str(file_id))
            if not record:
                raise FileNotFoundError('预览图片不存在')
            path = Path(record['path'])
            expected = plan['signatures'].get(str(path))
            if expected is None or _file_signature(path) != expected:
                raise RuntimeError('源 JPG 已发生变化，请重新 Preview')

            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode != 'RGB':
                    image = image.convert('RGB')
                image.thumbnail((220, 160), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, 'JPEG', quality=72, optimize=True)
            buffer.seek(0)
            return send_file(buffer, mimetype='image/jpeg', max_age=300)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 404

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/prepare', methods=['POST'])
    def photo_import_prepare(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), 'photo_import_preview', source_id, set_rel)
            execution = _prepare_photo_import_execution(source_id, set_rel, plan, data.get('group_ids'))
            execution_id = _remember_plan(execution)
            count = len(execution['operations'])
            return jsonify({
                'plan_id': execution_id,
                'shoot_date': execution['shoot_date'],
                'jpg_count': count,
                'raw_count': count,
                'total_files': count * 2,
                'jpg_destination': '01_Original/JPG',
                'raw_destination': '01_Original/RAW',
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/workflow/sources/<int:source_id>/photo-import/start', methods=['POST'])
    def photo_import_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), 'photo_import_execute', source_id, set_rel)
            _verify_signatures(plan['signatures'])
            _assert_photo_import_target_empty(set_dir)
            operations = list(plan['operations'])
            for item in operations:
                if Path(item['jpg_dst']).exists() or Path(item['raw_dst']).exists():
                    raise FileExistsError(f'目标已有同名文件: {item["stem"]}')

            task_id = _new_task('photo_import', len(operations) * 2)

            def worker():
                moved = []
                completed = 0
                try:
                    _update_task(task_id, status='running', message='开始导入 JPG / RAW…')
                    Path(plan['jpg_target']).mkdir(parents=True, exist_ok=True)
                    Path(plan['raw_target']).mkdir(parents=True, exist_ok=True)
                    for item in operations:
                        jpg_src = Path(item['jpg_src'])
                        jpg_dst = Path(item['jpg_dst'])
                        raw_src = Path(item['raw_src'])
                        raw_dst = Path(item['raw_dst'])

                        if not jpg_src.exists() or not raw_src.exists():
                            raise FileNotFoundError(f'源文件不存在: {item["stem"]}')
                        if jpg_dst.exists() or raw_dst.exists():
                            raise FileExistsError(f'目标已有同名文件: {item["stem"]}')

                        shutil.move(str(jpg_src), str(jpg_dst))
                        moved.append((str(jpg_src), str(jpg_dst)))
                        completed += 1
                        jpg_message = f'{jpg_src.name} → 01_Original/JPG/'
                        _update_task(task_id, completed=completed, current=completed, message=jpg_message, log=jpg_message)

                        shutil.move(str(raw_src), str(raw_dst))
                        moved.append((str(raw_src), str(raw_dst)))
                        completed += 1
                        raw_message = f'{raw_src.name} → 01_Original/RAW/'
                        _update_task(task_id, completed=completed, current=completed, message=raw_message, log=raw_message)

                    _update_task(
                        task_id,
                        status='done',
                        message=f'完成：导入 {len(operations)} 组 JPG + RAW',
                        result={
                            'jpg_count': len(operations),
                            'raw_count': len(operations),
                            'total_files': len(operations) * 2,
                        },
                    )
                except Exception as exc:
                    rollback_errors = _rollback_photo_import(moved)
                    message = str(exc)
                    if rollback_errors:
                        message += '；回滚失败: ' + '; '.join(rollback_errors[:5])
                    _update_task(task_id, status='error', message='导入失败，已尝试回滚', error=message, log=f'ERROR: {message}')

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
