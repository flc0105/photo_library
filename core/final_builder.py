import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file
from PIL import Image, ImageCms, ImageOps, JpegImagePlugin

from core.build_manifest import prepare_build_manifest, record_build_failure, record_build_success
from core.final_delivery_contract import (
    FINAL_CROP_WARNING_PERCENT,
    FINAL_JPEG_CHROMA_SAMPLING,
    FINAL_JPEG_CJPEG_SAMPLE,
    FINAL_JPEG_HUFFMAN_OPTIMIZE,
    FINAL_JPEG_PIL_SAMPLING,
    FINAL_JPEG_PROGRESSIVE,
    FINAL_JPEG_QUALITY,
    FINAL_SRGB_ICC_FILENAME,
    FINAL_SRGB_ICC_SHA256,
    FINAL_SRGB_PROFILE_DESCRIPTION,
)
from core.external_tools import (
    IMAGEMAGICK_REQUIRED_QUANTUM,
    IMAGEMAGICK_REQUIRED_VERSION,
    probe_imagemagick,
    resolve_imagemagick,
)
from core.final_metadata_fields import load_metadata_field_keys
from core.final_resolution import choose_target_for_crop, current_policy


_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
_SET_RE = re.compile(r'^\d{8}-.+-.+$')
_PLAN_TTL_SECONDS = 30 * 60
_TASK_TTL_SECONDS = 60 * 60
_FINAL_PROFILE_BASE = {
    'resize_filter': 'Lanczos',
    'resize_lobes': 3,
    'quality': FINAL_JPEG_QUALITY,
    'chroma': FINAL_JPEG_CHROMA_SAMPLING,
    'huffman_optimize': FINAL_JPEG_HUFFMAN_OPTIMIZE,
    'progressive': FINAL_JPEG_PROGRESSIVE,
    'dct': 'int',
    'icc': 'fixed sRGB',
    'source_color_contract': 'verify sRGB from ICC / PNG signals / EXIF; otherwise use project sRGB assumption',
    'icc_profile': FINAL_SRGB_PROFILE_DESCRIPTION,
    'output_icc': f'{FINAL_SRGB_PROFILE_DESCRIPTION} · ICC v2.1 · HP/IEC 1998 fixed profile',
    'icc_sha256': FINAL_SRGB_ICC_SHA256,
    'crop_warning_percent': FINAL_CROP_WARNING_PERCENT,
}

_FINAL_RUNTIME = {
    'imagemagick': IMAGEMAGICK_REQUIRED_VERSION,
    'imagemagick_quantum': IMAGEMAGICK_REQUIRED_QUANTUM,
    'libjpeg_turbo': '3.2.0',
}


def _current_final_profile():
    policy = current_policy()
    profile = dict(_FINAL_PROFILE_BASE)
    profile.update({
        'resolution_policy': policy['key'],
        'resolution_mode': policy['mode'],
        'resolution_label': policy['label'],
    })
    return profile

_PLAN_LOCK = threading.Lock()
_PLANS = {}
_TASK_LOCK = threading.Lock()
_TASKS = {}


def _image_time_text():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


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
        raise ValueError('预览结果已过期，请重新开始 Build Final')
    if plan.get('kind') != kind or int(plan.get('source_id')) != int(source_id) or plan.get('set_rel') != set_rel:
        raise ValueError('预览结果与当前 Set 不匹配，请重新开始 Build Final')
    return plan


def _new_task(total):
    _cleanup_state()
    task_id = uuid.uuid4().hex
    now = time.time()
    task = {
        'id': task_id,
        'kind': 'final_build',
        'status': 'queued',
        'total': int(total),
        'completed': 0,
        'current': 0,
        'percent': 0,
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
        total = max(0, int(task.get('total') or 0))
        completed = max(0, int(task.get('completed') or 0))
        task['percent'] = round(completed * 100 / total) if total else 0
        if task.get('status') == 'done':
            task['percent'] = 100
        if log:
            task['logs'].append(str(log))
            task['logs'] = task['logs'][-100:]
        task['updated_at'] = time.time()


def _task_snapshot(task_id):
    _cleanup_state()
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def _file_signature(path: Path):
    stat = path.stat()
    return [int(stat.st_size), int(stat.st_mtime_ns)]


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
        names = ', '.join(changed[:8])
        if len(changed) > 8:
            names += f' 等 {len(changed)} 个文件'
        raise RuntimeError(f'源文件在预览后发生变化，请重新 Preview：{names}')


def _logical_id(filename: str):
    return Path(filename).stem.split('-', 1)[0]


def _top_level_images(directory: Path):
    if not directory.is_dir():
        return []
    return sorted(
        [
            path for path in directory.iterdir()
            if path.is_file()
            and not path.name.startswith('._')
            and path.suffix.lower() in _IMAGE_EXTENSIONS
        ],
        key=lambda item: item.name.casefold(),
    )


def _selection_plan(source_id, set_dir: Path, set_rel: str):
    stage_defs = [
        ('revision', 'Revision', set_dir / '04_Revision'),
        ('model_edit', 'Model Edit', set_dir / '03_Model_Edit'),
        ('base_edit', 'Base Edit', set_dir / '02_Base_Edit'),
    ]
    stage_files = {key: _top_level_images(directory) for key, _, directory in stage_defs}

    revision_ids = {_logical_id(path.name).casefold() for path in stage_files['revision']}
    later_stage_has_any = bool(stage_files['revision'] or stage_files['model_edit'])
    records = {}
    groups = []
    signatures = {}

    for stage_key, stage_label, stage_dir in stage_defs:
        items = []
        for path in stage_files[stage_key]:
            logical_id = _logical_id(path.name)
            if stage_key == 'revision':
                default_selected = True
                default_reason = '默认选中'
            elif stage_key == 'model_edit':
                default_selected = logical_id.casefold() not in revision_ids
                default_reason = '默认选中' if default_selected else '已有 Revision'
            else:
                default_selected = not later_stage_has_any
                default_reason = '默认选中' if default_selected else '手动选择'

            item_id = uuid.uuid4().hex
            relative_path = path.relative_to(set_dir).as_posix()
            record = {
                'id': item_id,
                'stage': stage_key,
                'stage_label': stage_label,
                'name': path.name,
                'logical_id': logical_id,
                'relative_path': relative_path,
                'path': str(path),
                'default_selected': default_selected,
                'default_reason': default_reason,
                'size': path.stat().st_size,
            }
            records[item_id] = record
            signatures[str(path)] = _file_signature(path)
            items.append({key: value for key, value in record.items() if key != 'path'})
        groups.append({'stage': stage_key, 'label': stage_label, 'items': items})

    return {
        'kind': 'final_selection',
        'source_id': source_id,
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'records': records,
        'groups': groups,
        'signatures': signatures,
        'summary': {
            'revision_count': len(stage_files['revision']),
            'model_count': len(stage_files['model_edit']),
            'base_count': len(stage_files['base_edit']),
            'default_count': sum(1 for record in records.values() if record['default_selected']),
        },
    }


def _source_icc_info(image):
    icc_blob = image.info.get('icc_profile')
    if not icc_blob:
        return {
            'status': 'untagged',
            'description': 'No embedded ICC',
            'sha256': '',
        }

    try:
        raw = bytes(icc_blob)
        profile = ImageCms.ImageCmsProfile(io.BytesIO(raw))
        labels = []
        for getter in (ImageCms.getProfileName, ImageCms.getProfileDescription, ImageCms.getProfileInfo):
            try:
                value = str(getter(profile) or '').strip()
            except Exception:
                value = ''
            if value and value not in labels:
                labels.append(value)
        description = ' | '.join(labels) or 'Embedded ICC'
        signature = description.casefold().replace(' ', '')
        is_srgb = (
            'srgb' in signature
            or 'iec61966-2.1' in signature
            or 'iec61966-2-1' in signature
        )
        return {
            'status': 'srgb' if is_srgb else 'non_srgb',
            'description': description,
            'sha256': hashlib.sha256(raw).hexdigest(),
        }
    except Exception as exc:
        return {
            'status': 'invalid',
            'description': f'Embedded ICC cannot be parsed: {exc}',
            'sha256': '',
        }


def _exif_color_space(image):
    try:
        exif = image.getexif()
        value = exif.get(0xA001)
        if value is None:
            try:
                value = exif.get_ifd(0x8769).get(0xA001)
            except Exception:
                value = None
        return int(value) if value is not None else None
    except Exception:
        return None


def _png_srgb_chromaticity_matches(image):
    gamma = image.info.get('gamma')
    chromaticity = image.info.get('chromaticity')
    if gamma is None or not isinstance(chromaticity, (tuple, list)) or len(chromaticity) != 8:
        return None

    expected_gamma = 0.45455
    expected_chromaticity = (
        0.31270, 0.32900,
        0.64000, 0.33000,
        0.30000, 0.60000,
        0.15000, 0.06000,
    )
    tolerance = 0.00001
    gamma_matches = abs(float(gamma) - expected_gamma) <= tolerance
    chromaticity_matches = all(
        abs(float(actual) - expected) <= tolerance
        for actual, expected in zip(chromaticity, expected_chromaticity)
    )
    return gamma_matches and chromaticity_matches


def _source_color_info(image):
    icc = _source_icc_info(image)
    if icc['status'] == 'srgb':
        return {
            'status': 'confirmed_srgb',
            'evidence': 'icc',
            'label': 'sRGB · ICC',
            'description': icc['description'],
            'icc_status': icc['status'],
            'icc_description': icc['description'],
            'icc_sha256': icc['sha256'],
        }
    if icc['status'] == 'non_srgb':
        return {
            'status': 'non_srgb',
            'evidence': 'icc',
            'label': 'Non-sRGB ICC',
            'description': icc['description'],
            'icc_status': icc['status'],
            'icc_description': icc['description'],
            'icc_sha256': icc['sha256'],
        }
    if icc['status'] == 'invalid':
        return {
            'status': 'invalid',
            'evidence': 'icc',
            'label': 'ICC invalid',
            'description': icc['description'],
            'icc_status': icc['status'],
            'icc_description': icc['description'],
            'icc_sha256': icc['sha256'],
        }

    # With no embedded ICC, prefer explicit standard declarations before the
    # project-level fallback that all library sources are known to be sRGB.
    if image.format == 'PNG':
        if 'srgb' in image.info:
            return {
                'status': 'confirmed_srgb',
                'evidence': 'png_srgb',
                'label': 'sRGB · PNG sRGB',
                'description': 'PNG sRGB chunk declares sRGB',
                'icc_status': icc['status'],
                'icc_description': icc['description'],
                'icc_sha256': icc['sha256'],
            }

        chromaticity_match = _png_srgb_chromaticity_matches(image)
        if chromaticity_match is True:
            return {
                'status': 'confirmed_srgb',
                'evidence': 'png_chrm_gama',
                'label': 'sRGB · cHRM+gAMA',
                'description': 'PNG cHRM + gAMA match the standard sRGB values',
                'icc_status': icc['status'],
                'icc_description': icc['description'],
                'icc_sha256': icc['sha256'],
            }
        if chromaticity_match is False:
            return {
                'status': 'non_srgb',
                'evidence': 'png_chrm_gama',
                'label': 'Non-sRGB PNG signal',
                'description': 'PNG cHRM + gAMA are present but do not match the standard sRGB values',
                'icc_status': icc['status'],
                'icc_description': icc['description'],
                'icc_sha256': icc['sha256'],
            }

    if _exif_color_space(image) == 1:
        return {
            'status': 'confirmed_srgb',
            'evidence': 'exif',
            'label': 'sRGB · EXIF',
            'description': 'EXIF ColorSpace=1 declares sRGB',
            'icc_status': icc['status'],
            'icc_description': icc['description'],
            'icc_sha256': icc['sha256'],
        }

    return {
        'status': 'assumed_srgb',
        'evidence': 'project_assumption',
        'label': 'sRGB · assumed',
        'description': 'No independent color-space declaration found; accepted by the project sRGB input rule',
        'icc_status': icc['status'],
        'icc_description': icc['description'],
        'icc_sha256': icc['sha256'],
    }


def _fixed_srgb_icc_path():
    path = Path(__file__).resolve().parents[1] / 'assets' / FINAL_SRGB_ICC_FILENAME
    if not path.is_file():
        raise RuntimeError(f'缺少固定 sRGB ICC：{FINAL_SRGB_ICC_FILENAME}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != FINAL_SRGB_ICC_SHA256:
        raise RuntimeError('固定 sRGB ICC hash 不匹配；拒绝生成 Final')
    return path


def _read_image_info(path: Path):
    with Image.open(path) as image:
        width, height = image.size
        orientation = image.getexif().get(274, 1)
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        has_alpha = image.mode in {'RGBA', 'LA', 'PA'} or ('transparency' in image.info)
        alpha_has_transparency = False
        if has_alpha:
            alpha_min, _ = image.convert('RGBA').getchannel('A').getextrema()
            alpha_has_transparency = int(alpha_min) < 255

        color = _source_color_info(image)
        return {
            'width': int(width),
            'height': int(height),
            'mode': image.mode,
            'has_alpha': has_alpha,
            'alpha_has_transparency': alpha_has_transparency,
            'color_status': color['status'],
            'color_evidence': color['evidence'],
            'color_label': color['label'],
            'color_description': color['description'],
            'icc_status': color['icc_status'],
            'icc_description': color['icc_description'],
            'icc_sha256': color['icc_sha256'],
        }


def _geometry_plan(width: int, height: int):
    if width <= 0 or height <= 0:
        raise ValueError('无法读取有效像素尺寸')

    portrait = height >= width
    if portrait:
        unit = min(width // 2, height // 3)
        crop_width = unit * 2
        crop_height = unit * 3
        target_ratio = '2:3'
    else:
        unit = min(width // 3, height // 2)
        crop_width = unit * 3
        crop_height = unit * 2
        target_ratio = '3:2'

    if crop_width <= 0 or crop_height <= 0:
        raise ValueError('图片尺寸过小，无法计算 2:3 / 3:2 裁切')

    target = choose_target_for_crop(crop_width, crop_height, portrait)
    target_width = int(target['target_width'])
    target_height = int(target['target_height'])

    removed_width = width - crop_width
    removed_height = height - crop_height
    left = removed_width // 2
    right = removed_width - left
    top = removed_height // 2
    bottom = removed_height - top
    source_area = width * height
    crop_area = crop_width * crop_height
    crop_percent = ((source_area - crop_area) / source_area * 100.0) if source_area else 0.0
    exact_ratio = width == crop_width and height == crop_height
    crop_required = not exact_ratio
    resize_required = crop_width != target_width or crop_height != target_height
    pixel_insufficient = bool(target['pixel_insufficient'])

    return {
        'orientation': 'portrait' if portrait else 'landscape',
        'target_ratio': target_ratio,
        'target_width': target_width,
        'target_height': target_height,
        'crop_width': crop_width,
        'crop_height': crop_height,
        'crop_left': left,
        'crop_right': right,
        'crop_top': top,
        'crop_bottom': bottom,
        'crop_percent': round(crop_percent, 4),
        'exact_ratio': exact_ratio,
        'crop_required': crop_required,
        'resize_required': resize_required,
        'pixel_insufficient': pixel_insufficient,
        'crop_warning': crop_percent > FINAL_CROP_WARNING_PERCENT,
    }


def _cjpeg_candidates():
    candidates = [
        shutil.which('cjpeg'),
        '/opt/homebrew/opt/jpeg-turbo/bin/cjpeg',
        '/usr/local/opt/jpeg-turbo/bin/cjpeg',
        '/opt/libjpeg-turbo/bin/cjpeg',
    ]
    found = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = str(Path(candidate).expanduser())
        if path in seen:
            continue
        seen.add(path)
        if Path(path).is_file() and os.access(path, os.X_OK):
            found.append(path)
    return found


def _cjpeg_version(cjpeg_path):
    if not cjpeg_path:
        return ''
    try:
        result = subprocess.run(
            [cjpeg_path, '-version'],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        text = f'{result.stdout}\n{result.stderr}'
        match = re.search(r'libjpeg-turbo[^\d]*(\d+\.\d+\.\d+)', text, re.IGNORECASE)
        return match.group(1) if match else ''
    except Exception:
        return ''


def _dependency_status():
    status = {
        'ready': False,
        'imagemagick_required': _FINAL_RUNTIME['imagemagick'],
        'imagemagick_quantum_required': _FINAL_RUNTIME['imagemagick_quantum'],
        'imagemagick_version': '',
        'imagemagick_quantum': '',
        'imagemagick_banner': '',
        'magick_path': '',
        'libjpeg_turbo_required': _FINAL_RUNTIME['libjpeg_turbo'],
        'libjpeg_turbo_version': '',
        'cjpeg_path': '',
        'icc_profile_path': '',
        'icc_profile_sha256': '',
        'messages': [],
    }

    magick_path = resolve_imagemagick()
    magick_info = probe_imagemagick(magick_path)
    status['magick_path'] = magick_path
    status['imagemagick_version'] = magick_info['version']
    status['imagemagick_quantum'] = magick_info['quantum']
    status['imagemagick_banner'] = magick_info['banner']
    if not magick_path:
        status['messages'].append(
            f'找不到 ImageMagick {IMAGEMAGICK_REQUIRED_VERSION} {IMAGEMAGICK_REQUIRED_QUANTUM}'
        )
    else:
        if magick_info['version'] != IMAGEMAGICK_REQUIRED_VERSION:
            status['messages'].append(
                f'ImageMagick {magick_info["version"] or "unknown"}（需要 {IMAGEMAGICK_REQUIRED_VERSION}）'
            )
        if magick_info['quantum'] != IMAGEMAGICK_REQUIRED_QUANTUM:
            status['messages'].append(
                f'ImageMagick {magick_info["quantum"] or "unknown build"}（需要 {IMAGEMAGICK_REQUIRED_QUANTUM}）'
            )

    try:
        icc_path = _fixed_srgb_icc_path()
        status['icc_profile_path'] = str(icc_path)
        status['icc_profile_sha256'] = FINAL_SRGB_ICC_SHA256
    except Exception as exc:
        status['messages'].append(str(exc))

    cjpeg_candidates = _cjpeg_candidates()
    versions = [(path, _cjpeg_version(path)) for path in cjpeg_candidates]
    exact = next((item for item in versions if item[1] == _FINAL_RUNTIME['libjpeg_turbo']), None)
    chosen = exact or (versions[0] if versions else ('', ''))
    status['cjpeg_path'], status['libjpeg_turbo_version'] = chosen
    if not status['cjpeg_path']:
        status['messages'].append(
            '找不到 cjpeg（libjpeg-turbo）；macOS 可安装官方 3.2.0 DMG，默认路径 /opt/libjpeg-turbo/bin/cjpeg'
        )
    elif status['libjpeg_turbo_version'] != _FINAL_RUNTIME['libjpeg_turbo']:
        status['messages'].append(
            f'libjpeg-turbo {status["libjpeg_turbo_version"] or "unknown"}（需要 {_FINAL_RUNTIME["libjpeg_turbo"]}）'
        )

    status['ready'] = (
        status['imagemagick_version'] == IMAGEMAGICK_REQUIRED_VERSION
        and status['imagemagick_quantum'] == IMAGEMAGICK_REQUIRED_QUANTUM
        and status['libjpeg_turbo_version'] == _FINAL_RUNTIME['libjpeg_turbo']
        and bool(status['icc_profile_path'])
    )
    return status


def _build_execution_plan(source_id, set_dir: Path, set_rel: str, selection, selected_ids):
    selected_ids = [str(item_id) for item_id in selected_ids]
    if not selected_ids:
        raise ValueError('至少选择一张图片')
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError('选择列表包含重复项')

    selected_records = []
    for item_id in selected_ids:
        record = selection['records'].get(item_id)
        if not record:
            raise ValueError('选择列表包含已失效的文件，请重新开始')
        selected_records.append(record)

    _verify_signatures({record['path']: selection['signatures'][record['path']] for record in selected_records})

    final_dir = set_dir / '05_Final'
    items = []
    signatures = {}
    output_groups = {}

    for record in selected_records:
        source_path = Path(record['path'])
        logical_id = record['logical_id'].strip()
        output_name = f'{logical_id}.jpg'
        output_path = final_dir / output_name
        errors = []
        warnings = []
        info = None
        geometry = None

        if not logical_id:
            errors.append('无法解析文件标识')
        try:
            info = _read_image_info(source_path)
            geometry = _geometry_plan(info['width'], info['height'])
        except Exception as exc:
            errors.append(f'图像分析失败：{exc}')

        if info and info['alpha_has_transparency']:
            errors.append('存在实际透明像素；JPEG 不支持透明，Final Builder 不擅自决定背景合成颜色')
        elif info and info['has_alpha']:
            warnings.append('Alpha 通道实际全不透明；Build 时只移除 Alpha，不改变可见像素')

        if info and info['mode'] == 'CMYK':
            errors.append('Final v1 的输入契约是 sRGB；CMYK 不允许按 sRGB 直接解释')
        if info and info['color_status'] == 'non_srgb':
            errors.append(f'sRGB 预检未通过：{info["color_description"]}')
        elif info and info['color_status'] == 'invalid':
            errors.append(f'sRGB 预检无法完成：{info["color_description"]}')
        if geometry and geometry['pixel_insufficient']:
            errors.append('Center Crop 后像素不足目标尺寸')
        if geometry and geometry['crop_warning']:
            warnings.append(f'Center Crop 将移除 {geometry["crop_percent"]:.2f}% 画面，超过 {FINAL_CROP_WARNING_PERCENT:.0f}%')
        if output_path.exists():
            errors.append('05_Final 已有同名文件；禁止静默覆盖')

        item = {
            'source_item_id': record['id'],
            'stage': record['stage'],
            'stage_label': record['stage_label'],
            'source_name': record['name'],
            'source_relative_path': record['relative_path'],
            'source_path': str(source_path),
            'logical_id': logical_id,
            'output_name': output_name,
            'output_relative_path': f'05_Final/{output_name}',
            'output_path': str(output_path),
            'source_width': info['width'] if info else 0,
            'source_height': info['height'] if info else 0,
            'source_mode': info['mode'] if info else '',
            'has_alpha': info['has_alpha'] if info else False,
            'alpha_has_transparency': info['alpha_has_transparency'] if info else False,
            'source_color_status': info['color_status'] if info else '',
            'source_color_evidence': info['color_evidence'] if info else '',
            'source_color_label': info['color_label'] if info else '',
            'source_color_description': info['color_description'] if info else '',
            'source_icc_status': info['icc_status'] if info else '',
            'source_icc_description': info['icc_description'] if info else '',
            'source_icc_sha256': info['icc_sha256'] if info else '',
            'color_policy': 'verify_srgb_then_project_assumption',
            'geometry': geometry,
            'errors': errors,
            'warnings': warnings,
            'status': 'blocked' if errors else ('warning' if warnings else 'ready'),
        }
        items.append(item)
        signatures[str(source_path)] = _file_signature(source_path)
        output_groups.setdefault(output_name.casefold(), []).append(item)

    conflict_count = 0
    for grouped in output_groups.values():
        if len(grouped) <= 1:
            continue
        conflict_count += 1
        names = ' / '.join(item['source_name'] for item in grouped)
        for item in grouped:
            item['errors'].append(f'多个已选源会生成同一 Final：{names}')
            item['status'] = 'blocked'

    dependency = _dependency_status()
    blocked_count = sum(1 for item in items if item['status'] == 'blocked')
    warning_count = sum(1 for item in items if item['status'] == 'warning')
    crop_warning_count = sum(1 for item in items if item['geometry'] and item['geometry']['crop_warning'])
    insufficient_count = sum(1 for item in items if item['geometry'] and item['geometry']['pixel_insufficient'])
    existing_count = sum(1 for item in items if Path(item['output_path']).exists())
    color_confirmed_count = sum(1 for item in items if item.get('source_color_status') == 'confirmed_srgb')
    color_assumed_count = sum(1 for item in items if item.get('source_color_status') == 'assumed_srgb')
    color_rejected_count = sum(1 for item in items if item.get('source_color_status') in {'non_srgb', 'invalid'})

    return {
        'kind': 'final_build',
        'source_id': source_id,
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'final_dir': str(final_dir),
        'selection_plan_id': selection['id'],
        'items': items,
        'signatures': signatures,
        'dependency': dependency,
        'profile': _current_final_profile(),
        'summary': {
            'selected_count': len(items),
            'blocked_count': blocked_count,
            'warning_count': warning_count,
            'crop_warning_count': crop_warning_count,
            'pixel_insufficient_count': insufficient_count,
            'output_exists_count': existing_count,
            'conflict_count': conflict_count,
            'color_confirmed_count': color_confirmed_count,
            'color_assumed_count': color_assumed_count,
            'color_rejected_count': color_rejected_count,
        },
        'can_execute': len(items) > 0 and blocked_count == 0 and dependency['ready'],
    }


def _verify_srgb_inputs(items):
    failures = []
    for item in items:
        path = Path(item['source_path'])
        try:
            with Image.open(path) as image:
                if image.mode == 'CMYK':
                    failures.append(f'{item["source_name"]}: CMYK')
                    continue
                color = _source_color_info(image)
        except Exception as exc:
            failures.append(f'{item["source_name"]}: sRGB 预检失败：{exc}')
            continue

        if color['status'] == 'non_srgb':
            failures.append(f'{item["source_name"]}: 非 sRGB · {color["description"]}')
        elif color['status'] == 'invalid':
            failures.append(f'{item["source_name"]}: ICC 无法解析')

    if failures:
        detail = '；'.join(failures[:6])
        if len(failures) > 6:
            detail += f'；另有 {len(failures) - 6} 张'
        raise RuntimeError(f'执行前 sRGB 检查失败：{detail}')



def _magick_argv(item, magick_path):
    geometry = item['geometry']
    source_path = Path(item['source_path'])
    source_arg = str(item.get('source_relative_path') or source_path)
    command = [
        magick_path,
        source_arg,
        '-auto-orient',
        '-alpha', 'off',
        '-set', 'colorspace', 'sRGB',
    ]

    if geometry['crop_required']:
        command.extend([
            '-crop',
            f'{geometry["crop_width"]}x{geometry["crop_height"]}+{geometry["crop_left"]}+{geometry["crop_top"]}',
            '+repage',
        ])

    if geometry['resize_required']:
        command.extend([
            '-filter', 'Lanczos',
            '-define', 'filter:lobes=3',
            '-resize', f'{geometry["target_width"]}x{geometry["target_height"]}!',
        ])

    command.extend([
        '-depth', '8',
        '-type', 'TrueColor',
        'ppm:-',
    ])
    return command


def _command_step(name, tool, argv, *, cwd, stdin=None, stdout=None):
    step = {
        'name': name,
        'tool': tool,
        'argv': list(argv),
        'cwd': str(cwd),
    }
    if stdin:
        step['stdin'] = stdin
    if stdout:
        step['stdout'] = stdout
    return step


def _render_final(item, temp_dir: Path, magick_path: str, cjpeg_path: str, set_dir: Path):
    icc_path = _fixed_srgb_icc_path()
    jpeg_path = temp_dir / f'{uuid.uuid4().hex}.jpg'
    magick_command = _magick_argv(item, magick_path)
    cjpeg_command = [
        cjpeg_path,
        '-quality', str(_FINAL_PROFILE_BASE['quality']),
        '-sample', FINAL_JPEG_CJPEG_SAMPLE,
        '-optimize',
        '-dct', 'int',
        '-icc', str(icc_path),
        '-outfile', str(jpeg_path),
    ]
    replay_cjpeg_command = [
        cjpeg_path,
        '-quality', str(_FINAL_PROFILE_BASE['quality']),
        '-sample', FINAL_JPEG_CJPEG_SAMPLE,
        '-optimize',
        '-dct', 'int',
        '-icc', str(icc_path),
        '-outfile', str(item.get('output_relative_path') or item.get('output_name') or '05_Final/output.jpg'),
    ]
    # steps[].argv is runtime provenance: it records the exact argv passed to
    # Popen, including the temporary staged output path. replay_shell is the
    # separate stable Set-relative command intended for later reproduction.
    command_trace = {
        'steps': [
            _command_step(
                'transform',
                'magick',
                magick_command,
                cwd=set_dir,
                stdout='P6 PPM · 8-bit RGB',
            ),
            _command_step(
                'encode',
                'cjpeg',
                cjpeg_command,
                cwd=set_dir,
                stdin='magick stdout · P6 PPM',
            ),
        ],
        'pipeline': {
            'replay_shell': f'{shlex.join(magick_command)} | {shlex.join(replay_cjpeg_command)}',
            'cwd': '.',
        },
    }

    magick_stderr_file = tempfile.TemporaryFile()
    renderer = None
    cjpeg = None
    cjpeg_stderr = b''
    try:
        renderer = subprocess.Popen(
            magick_command,
            cwd=str(set_dir),
            stdout=subprocess.PIPE,
            stderr=magick_stderr_file,
        )
        if renderer.stdout is None:
            raise RuntimeError('无法打开 ImageMagick stdout')
        cjpeg = subprocess.Popen(
            cjpeg_command,
            cwd=str(set_dir),
            stdin=renderer.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # cjpeg owns the pipe now; closing the parent's copy lets EOF propagate.
        renderer.stdout.close()
        renderer.stdout = None
        try:
            _, cjpeg_stderr = cjpeg.communicate(timeout=300)
            renderer.wait(timeout=30)
        except subprocess.TimeoutExpired:
            if cjpeg.poll() is None:
                cjpeg.kill()
                _, cjpeg_stderr = cjpeg.communicate()
            if renderer.poll() is None:
                renderer.kill()
                renderer.wait()
            raise RuntimeError('Final 图像变换 / 编码超时（300 秒）')

        magick_stderr_file.seek(0)
        renderer_stderr = magick_stderr_file.read().decode('utf-8', errors='replace').strip()
        if renderer.returncode != 0 and renderer_stderr:
            raise RuntimeError(f'ImageMagick 图像变换失败：{renderer_stderr}')
        if cjpeg.returncode != 0 or not jpeg_path.is_file():
            detail = (cjpeg_stderr or b'').decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'libjpeg-turbo 编码失败：{detail or "cjpeg returned non-zero"}')
        if renderer.returncode != 0:
            raise RuntimeError('ImageMagick 图像变换失败：magick returned non-zero')
    finally:
        if cjpeg is not None and cjpeg.poll() is None:
            cjpeg.kill()
            cjpeg.communicate()
        if renderer is not None and renderer.poll() is None:
            renderer.kill()
            renderer.wait()
        magick_stderr_file.close()

    return jpeg_path, command_trace


def _validate_output(path: Path, item):
    geometry = item['geometry']
    with Image.open(path) as image:
        image.load()
        if image.format != 'JPEG':
            raise RuntimeError('输出不是 JPEG')
        if image.size != (geometry['target_width'], geometry['target_height']):
            raise RuntimeError(f'输出尺寸验证失败：{image.width}×{image.height}')
        icc_blob = image.info.get('icc_profile')
        if not icc_blob:
            raise RuntimeError('输出缺少 sRGB ICC profile')
        if hashlib.sha256(bytes(icc_blob)).hexdigest() != FINAL_SRGB_ICC_SHA256:
            raise RuntimeError('输出 sRGB ICC 与 Final 固定 profile 不一致')
        if image.getexif():
            raise RuntimeError('图像生成阶段不应携带 EXIF；请检查编码流程')
        sampling = JpegImagePlugin.get_sampling(image)
        if sampling != FINAL_JPEG_PIL_SAMPLING:
            raise RuntimeError(f'输出不是 {FINAL_JPEG_CHROMA_SAMPLING} chroma sampling（Pillow sampling={sampling}）')
        if not FINAL_JPEG_PROGRESSIVE and (image.info.get('progressive') or image.info.get('progression')):
            raise RuntimeError('输出意外成为 Progressive JPEG')


def _publish_staged(staged, final_dir: Path):
    """Publish a validated batch without overwriting existing Final files.

    External/removable filesystems such as exFAT may not support hard links.
    Reserve every destination first with O_EXCL, then atomically replace only
    those reservations with the already-validated staged JPEGs. If anything
    fails, remove both published files and remaining reservations from this run.
    Source images are never touched.
    """
    reservations = []
    published = []
    final_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Reserve the whole batch before moving any JPEG. O_EXCL guarantees that
        # an existing Final is never silently overwritten, including files that
        # appeared after Preview but before publication.
        for _staged_path, item in staged:
            destination = final_dir / item['output_name']
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(destination, flags, 0o644)
            os.close(fd)
            reservations.append(destination)

        # staged_path and final_dir live inside the same Set, so os.replace() is
        # a same-filesystem atomic rename. It replaces only our own reservation.
        for staged_path, item in staged:
            destination = final_dir / item['output_name']
            os.replace(staged_path, destination)
            published.append(destination)

        return published
    except FileExistsError as exc:
        raise FileExistsError(f'发布前发现同名 Final；禁止覆盖：{Path(exc.filename).name if exc.filename else exc}') from exc
    finally:
        if len(published) != len(staged):
            # Roll back any files/reservations created by this publication attempt.
            for destination in reversed(reservations):
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def create_final_builder_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('final_builder', __name__)

    def resolve_set(source_id, path_value):
        source = get_source(source_id)
        if not source:
            raise FileNotFoundError('Source 不存在或已禁用')
        root, target, rel = resolve_path(source, path_value)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            raise ValueError('当前目录不是 Set')
        return source, root, target, rel

    def require_set(source_id):
        data = request.get_json(silent=True) or {}
        return resolve_set(source_id, data.get('path', ''))

    @bp.route('/api/library/final-builder/sources/<int:source_id>/selection', methods=['POST'])
    def final_selection(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            metadata_field_keys = load_metadata_field_keys()
            plan = _selection_plan(source_id, set_dir, set_rel)
            plan['metadata_field_keys'] = metadata_field_keys
            plan_id = _remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'groups': plan['groups'],
                'summary': plan['summary'],
                'profile': _current_final_profile(),
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/final-builder/sources/<int:source_id>/thumbnail/<plan_id>/<item_id>', methods=['GET'])
    def final_thumbnail(source_id, plan_id, item_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            with _PLAN_LOCK:
                plan = _PLANS.get(str(plan_id))
            if not plan or plan.get('kind') != 'final_selection' or int(plan.get('source_id')) != int(source_id):
                raise FileNotFoundError('选择预览已过期')
            record = plan['records'].get(str(item_id))
            if not record:
                raise FileNotFoundError('预览图片不存在')
            path = Path(record['path'])
            if _file_signature(path) != plan['signatures'][str(path)]:
                raise RuntimeError('源图已发生变化，请重新打开 Build Final')

            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode != 'RGB':
                    if image.mode in ('RGBA', 'LA'):
                        background = Image.new('RGB', image.size, 'white')
                        alpha = image.getchannel('A')
                        background.paste(image.convert('RGB'), mask=alpha)
                        image = background
                    else:
                        image = image.convert('RGB')
                # UI-only preview: generate a Retina-friendly square thumbnail so the
                # source-card grid does not upscale a small 4:3 preview. This never
                # writes back to the source image and is unrelated to Final JPEG output.
                image = ImageOps.fit(
                    image,
                    (512, 512),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                buffer = io.BytesIO()
                image.save(buffer, 'JPEG', quality=85, optimize=True)
                buffer.seek(0)
                response = send_file(buffer, mimetype='image/jpeg', max_age=0)
                response.cache_control.no_store = True
                response.cache_control.max_age = 0
                return response
        except Exception as exc:
            return jsonify({'error': str(exc)}), 404

    @bp.route('/api/library/final-builder/sources/<int:source_id>/plan', methods=['POST'])
    def final_plan(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            selection = _get_plan(str(data.get('selection_plan_id') or ''), 'final_selection', source_id, set_rel)
            if load_metadata_field_keys() != selection.get('metadata_field_keys'):
                raise RuntimeError('Final Metadata 字段配置在选择后发生变化，请重新打开 Build Final')
            plan = _build_execution_plan(source_id, set_dir, set_rel, selection, data.get('selected_ids') or [])
            plan['metadata_field_keys'] = list(selection['metadata_field_keys'])
            plan_id = _remember_plan(plan)
            public_items = []
            for item in plan['items']:
                public_items.append({key: value for key, value in item.items() if key not in {'source_path', 'output_path'}})
            return jsonify({
                'plan_id': plan_id,
                'items': public_items,
                'summary': plan['summary'],
                'dependency': plan['dependency'],
                'profile': plan['profile'],
                'can_execute': plan['can_execute'],
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/final-builder/sources/<int:source_id>/start', methods=['POST'])
    def final_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), 'final_build', source_id, set_rel)
            if load_metadata_field_keys() != plan.get('metadata_field_keys'):
                raise RuntimeError('Final Metadata 字段配置在 Preview 后发生变化，请重新打开 Build Final')
            dependency = _dependency_status()
            if not dependency['ready']:
                raise RuntimeError('Final Runtime 不符合要求，请处理后重新 Preview')
            if any(item['status'] == 'blocked' for item in plan['items']):
                raise RuntimeError('Build Plan 中仍有阻断项，请先处理')
            _verify_signatures(plan['signatures'])
            _verify_srgb_inputs(plan['items'])
            for item in plan['items']:
                if Path(item['output_path']).exists():
                    raise FileExistsError(f'05_Final 已有同名文件：{item["output_name"]}')

            task_id = _new_task(len(plan['items']))

            def worker():
                staged = []
                commands_by_stem = {}
                built_at_by_stem = {}
                try:
                    _update_task(task_id, status='running', message='开始生成 Final…')
                    manifest_ok, manifest_error = prepare_build_manifest(
                        Path(plan['set_dir']),
                        metadata_fields=plan['metadata_field_keys'],
                    )
                    if not manifest_ok:
                        _update_task(task_id, log=f'build.json cleanup skipped: {manifest_error}')
                    temp_parent = Path(plan['set_dir'])
                    with tempfile.TemporaryDirectory(prefix='.final-builder-', dir=str(temp_parent)) as temp_name:
                        temp_dir = Path(temp_name)
                        for index, item in enumerate(plan['items'], start=1):
                            message = f'编码 {index}/{len(plan["items"])} · {item["source_name"]}'
                            _update_task(task_id, current=index, message=message, log=message)
                            jpeg_path, command_trace = _render_final(
                                item,
                                temp_dir,
                                dependency['magick_path'],
                                dependency['cjpeg_path'],
                                Path(plan['set_dir']),
                            )
                            _validate_output(jpeg_path, item)
                            stem = str(item.get('logical_id') or '')
                            commands_by_stem[stem] = command_trace
                            built_at_by_stem[stem] = _image_time_text()
                            staged.append((jpeg_path, item))
                            _update_task(task_id, completed=index, current=index, message=message)

                        # Re-check sources immediately before publication. External changes
                        # never cause a mixed Final batch to be published silently.
                        _verify_signatures(plan['signatures'])
                        if load_metadata_field_keys() != plan.get('metadata_field_keys'):
                            raise RuntimeError('Final Metadata 字段配置在 Build 期间发生变化，请重新执行')
                        published = _publish_staged(staged, Path(plan['final_dir']))

                    manifest_ok, manifest_error = record_build_success(
                        Path(plan['set_dir']),
                        plan['items'],
                        built_at_by_stem,
                        commands_by_stem=commands_by_stem,
                        metadata_fields=plan['metadata_field_keys'],
                    )
                    result = {
                        'count': len(published),
                        'destination': '05_Final',
                        'profile': _current_final_profile(),
                    }
                    if not manifest_ok:
                        _update_task(task_id, log=f'build.json skipped: {manifest_error}')
                    _update_task(
                        task_id,
                        status='done',
                        completed=len(plan['items']),
                        message=f'完成：生成 {len(published)} 张 Final',
                        result=result,
                        log=f'Published {len(published)} file(s) to 05_Final',
                    )
                except Exception as exc:
                    manifest_ok, manifest_error = record_build_failure(
                        Path(plan['set_dir']),
                        plan['items'],
                        exc,
                        commands_by_stem=commands_by_stem,
                        metadata_fields=plan['metadata_field_keys'],
                    )
                    if not manifest_ok:
                        _update_task(task_id, log=f'build.json skipped: {manifest_error}')
                    _update_task(task_id, status='error', message='Build Final 失败', error=str(exc), log=f'ERROR: {exc}')

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/final-builder/tasks/<task_id>', methods=['GET'])
    def final_task_status(task_id):
        denied = admin_guard()
        if denied:
            return denied
        task = _task_snapshot(task_id)
        if not task:
            return jsonify({'error': '任务不存在或已过期'}), 404
        task.pop('created_at', None)
        task.pop('updated_at', None)
        return jsonify(task)

    return bp
