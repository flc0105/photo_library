import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file
from PIL import Image, ImageOps


_SET_RE = re.compile(r'^\d{8}-.+-.+$')
_FINAL_EXTENSIONS = {'.jpg', '.jpeg'}
_DONOR_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
_PLAN_TTL_SECONDS = 30 * 60
_TASK_TTL_SECONDS = 60 * 60
_EXIFTOOL_VERSION = '13.55'
_SETTINGS_FILENAME = 'final_metadata_settings.json'
_FINAL_SRGB_ICC_SHA256 = '2b3aa1645779a9e634744faf9b01e9102b0c9b88fd6deced7934df86b949af7e'

# Curated, intentionally small metadata catalog.  Final metadata should keep
# photographic capture facts, not editor history or arbitrary print-density
# preferences.  Resolution/DPI tags and rarely populated derivative fields are
# intentionally omitted; users can still add an extra tag explicitly in the
# global settings when they truly need one.
_FIELD_DEFS = [
    {
        'key': 'Make', 'label': 'Make', 'group': 'IFD0', 'default': True,
        'read_keys': ['IFD0:Make'],
        'copy_arg': '-IFD0:Make<IFD0:Make',
    },
    {
        'key': 'Model', 'label': 'Model', 'group': 'IFD0', 'default': True,
        'read_keys': ['IFD0:Model'],
        'copy_arg': '-IFD0:Model<IFD0:Model',
    },
    {
        'key': 'LensModel', 'label': 'LensModel', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:LensModel'],
        'copy_arg': '-ExifIFD:LensModel<ExifIFD:LensModel',
    },
    {
        'key': 'SerialNumber', 'label': 'SerialNumber', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:SerialNumber', 'Canon:SerialNumber'],
        # Prefer the standard EXIF serial when present.  Canon MakerNotes are an
        # explicit fallback only; the MakerNotes container itself is never copied.
        'copy_args': [
            '-ExifIFD:SerialNumber<Canon:SerialNumber',
            '-ExifIFD:SerialNumber<ExifIFD:SerialNumber',
        ],
    },
    {
        'key': 'DateTimeOriginal', 'label': 'DateTimeOriginal', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:DateTimeOriginal'],
        'copy_arg': '-ExifIFD:DateTimeOriginal<ExifIFD:DateTimeOriginal',
    },
    {
        'key': 'FocalLength', 'label': 'FocalLength', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:FocalLength'],
        'copy_arg': '-ExifIFD:FocalLength<ExifIFD:FocalLength',
    },
    {
        'key': 'FNumber', 'label': 'FNumber', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:FNumber'],
        'copy_arg': '-ExifIFD:FNumber<ExifIFD:FNumber',
    },
    {
        'key': 'ExposureTime', 'label': 'ExposureTime', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:ExposureTime'],
        'copy_arg': '-ExifIFD:ExposureTime<ExifIFD:ExposureTime',
    },
    {
        'key': 'ISO', 'label': 'ISO', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:ISO'],
        'copy_arg': '-ExifIFD:ISO<ExifIFD:ISO',
    },
    {
        'key': 'ExposureCompensation', 'label': 'ExposureCompensation', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:ExposureCompensation'],
        'copy_arg': '-ExifIFD:ExposureCompensation<ExifIFD:ExposureCompensation',
    },
    {
        'key': 'ExposureProgram', 'label': 'ExposureProgram', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:ExposureProgram'],
        'copy_arg': '-ExifIFD:ExposureProgram<ExifIFD:ExposureProgram',
    },
    {
        'key': 'Flash', 'label': 'Flash', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:Flash'],
        'copy_arg': '-ExifIFD:Flash<ExifIFD:Flash',
    },
    {
        'key': 'MeteringMode', 'label': 'MeteringMode', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:MeteringMode'],
        'copy_arg': '-ExifIFD:MeteringMode<ExifIFD:MeteringMode',
    },
    {
        'key': 'WhiteBalance', 'label': 'WhiteBalance', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:WhiteBalance'],
        'copy_arg': '-ExifIFD:WhiteBalance<ExifIFD:WhiteBalance',
    },
    {
        'key': 'ColorSpace', 'label': 'ColorSpace', 'group': 'ExifIFD', 'default': True,
        'read_keys': ['ExifIFD:ColorSpace'],
        # Final is canonical sRGB.  This field is therefore written as the
        # standard EXIF sRGB value (1), not trusted to a donor application.
        'copy_arg': None,
        'fixed_value': 'sRGB',
    },
]
_RETIRED_FIELD_KEYS = {
    'SubSecTimeOriginal', 'OffsetTimeOriginal', 'FocalLengthIn35mmFormat',
    'LensInfo', 'XResolution', 'YResolution', 'ResolutionUnit', 'Software',
}
_FIELD_BY_KEY = {item['key']: item for item in _FIELD_DEFS}

_CUSTOM_TAG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
_RESTRICTED_CUSTOM_TAGS = {
    'all', 'icc_profile', 'icc-profile', 'adobe', 'photoshop', 'makernotes',
    'thumbnailimage', 'previewimage', 'orientation', 'pixelxdimension',
    'pixelydimension', 'filemodifydate', 'jfif', 'jfifversion',
}

_PLAN_LOCK = threading.Lock()
_PLANS = {}
_TASK_LOCK = threading.Lock()
_TASKS = {}


def _settings_path():
    return Path(__file__).resolve().with_name(_SETTINGS_FILENAME)


def _default_settings():
    return {
        'selected_fields': [item['key'] for item in _FIELD_DEFS if item.get('default')],
        'additional_tags': [],
    }


def _normalize_custom_tags(values):
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = re.split(r'[,\n]+', values)
    elif isinstance(values, list):
        raw_values = values
    else:
        raise ValueError('Additional tags 必须是字符串或列表')

    result = []
    seen = set()
    for value in raw_values:
        tag = str(value or '').strip()
        if not tag:
            continue
        if not _CUSTOM_TAG_RE.fullmatch(tag):
            raise ValueError(f'Additional tag 格式无效：{tag}')
        lowered = tag.casefold()
        base_name = lowered.split(':')[-1]
        if lowered.endswith(':all') or base_name in _RESTRICTED_CUSTOM_TAGS or lowered in _RESTRICTED_CUSTOM_TAGS:
            raise ValueError(f'Additional tag 不允许破坏 canonical metadata 边界：{tag}')
        if lowered not in seen:
            seen.add(lowered)
            result.append(tag)
    return result


def _normalize_settings(data):
    defaults = _default_settings()
    raw_fields = data.get('selected_fields') if isinstance(data, dict) else None
    if raw_fields is None:
        selected = defaults['selected_fields']
    elif not isinstance(raw_fields, list):
        raise ValueError('selected_fields 必须是列表')
    else:
        selected = []
        seen = set()
        for value in raw_fields:
            key = str(value or '').strip()
            if key in _RETIRED_FIELD_KEYS:
                continue
            if key not in _FIELD_BY_KEY:
                raise ValueError(f'未知字段：{key}')
            if key not in seen:
                seen.add(key)
                selected.append(key)
    if not selected:
        raise ValueError('至少保留一个 metadata 字段')

    additional = _normalize_custom_tags((data or {}).get('additional_tags'))
    return {'selected_fields': selected, 'additional_tags': additional}


def _load_settings():
    path = _settings_path()
    if not path.exists():
        return _default_settings()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return _normalize_settings(data)
    except Exception as exc:
        raise RuntimeError(f'读取 Final Metadata 设置失败：{exc}') from exc


def _save_settings(settings):
    normalized = _normalize_settings(settings)
    path = _settings_path()
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return normalized


def _field_catalog_public():
    return [
        {
            'key': item['key'],
            'label': item['label'],
            'group': item['group'],
            'default': bool(item.get('default')),
            'note': item.get('note', ''),
        }
        for item in _FIELD_DEFS
    ]


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


def _get_plan(plan_id, source_id, set_rel):
    _cleanup_state()
    with _PLAN_LOCK:
        plan = _PLANS.get(str(plan_id))
    if not plan:
        raise ValueError('Metadata Preview 已过期，请重新打开')
    if plan.get('kind') != 'final_metadata' or int(plan.get('source_id')) != int(source_id) or plan.get('set_rel') != set_rel:
        raise ValueError('Metadata Preview 与当前 Set 不匹配，请重新打开')
    return plan


def _new_task(total):
    _cleanup_state()
    task_id = uuid.uuid4().hex
    now = time.time()
    task = {
        'id': task_id,
        'kind': 'final_metadata',
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
            task['logs'] = task['logs'][-160:]
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
    for path_str, expected in signatures.items():
        path = Path(path_str)
        try:
            current = _file_signature(path)
        except OSError:
            changed.append(path.name)
            continue
        if current != expected:
            changed.append(path.name)
    if changed:
        names = ', '.join(changed[:8])
        if len(changed) > 8:
            names += f' 等 {len(changed)} 个文件'
        raise RuntimeError(f'文件在 Preview 后发生变化，请重新 Preview：{names}')


def _logical_id(filename):
    return Path(filename).stem.split('-', 1)[0]


def _natural_key(text):
    parts = re.split(r'(\d+)', str(text))
    return [int(part) if part.isdigit() else part.casefold() for part in parts]


def _is_appledouble(path: Path):
    # macOS resource-fork sidecars such as ._IMG_0001.jpg are not images and
    # must never participate in Final/donor matching.
    return path.name.startswith('._')


def _final_files(set_dir: Path):
    final_dir = set_dir / '05_Final'
    if not final_dir.is_dir():
        return []
    return sorted(
        [path for path in final_dir.iterdir() if path.is_file() and not _is_appledouble(path) and path.suffix.lower() in _FINAL_EXTENSIONS],
        key=lambda path: _natural_key(path.name),
    )


def _base_files(set_dir: Path):
    base_dir = set_dir / '02_Base_Edit'
    if not base_dir.is_dir():
        return []
    result = []
    for path in base_dir.rglob('*'):
        if not path.is_file() or _is_appledouble(path) or path.suffix.lower() not in _DONOR_EXTENSIONS:
            continue
        relative_parts = [part.casefold() for part in path.relative_to(base_dir).parts[:-1]]
        if 'discards' in relative_parts:
            continue
        result.append(path)
    return sorted(result, key=lambda path: _natural_key(path.relative_to(base_dir).as_posix()))


def _original_jpg_files(set_dir: Path):
    original_dir = set_dir / '01_Original' / 'JPG'
    if not original_dir.is_dir():
        return []
    return sorted(
        [path for path in original_dir.rglob('*') if path.is_file() and not _is_appledouble(path) and path.suffix.lower() in _FINAL_EXTENSIONS],
        key=lambda path: _natural_key(path.relative_to(original_dir).as_posix()),
    )


def _index_by_logical_id(paths):
    result = {}
    for path in paths:
        result.setdefault(_logical_id(path.name).casefold(), []).append(path)
    return result


def _choose_candidate(final_path: Path, candidates, stage_label):
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0], None

    exact = [path for path in candidates if path.stem.casefold() == final_path.stem.casefold()]
    if len(exact) == 1:
        return exact[0], None

    names = ', '.join(path.name for path in candidates[:6])
    if len(candidates) > 6:
        names += f' 等 {len(candidates)} 个'
    return None, f'{stage_label} 同 logical ID 存在多个 donor，无法唯一确定：{names}'


def _resolve_donor(final_path: Path, base_index, original_index):
    key = _logical_id(final_path.name).casefold()

    # Original/JPG is the canonical metadata donor.  Edited PNGs can legally
    # omit or reshape EXIF, so Base Edit is only a file-level fallback when the
    # corresponding Original/JPG does not exist.
    original_path, original_error = _choose_candidate(final_path, original_index.get(key, []), 'Original/JPG')
    if original_error:
        return None, None, original_error
    if original_path is not None:
        return original_path, 'Original/JPG', None

    base_path, base_error = _choose_candidate(final_path, base_index.get(key, []), 'Base Edit')
    if base_error:
        return None, None, base_error
    if base_path is not None:
        return base_path, 'Base Edit', None

    return None, None, '01_Original/JPG 没有对应文件，且 Base Edit 也没有可用 donor'


def _probe_exiftool(path):
    try:
        result = subprocess.run(
            [str(path), '-ver'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ''
    except Exception:
        return ''


def _dependency_status():
    # The official macOS ExifTool package installs to /usr/local/bin, while an
    # older Homebrew copy may still win PATH on Apple Silicon.  Probe both and
    # deliberately choose the frozen version when it is present.
    raw_candidates = [
        shutil.which('exiftool'),
        '/usr/local/bin/exiftool',
        '/opt/homebrew/bin/exiftool',
    ]
    candidates = []
    seen = set()
    for candidate in raw_candidates:
        if not candidate:
            continue
        path = str(Path(candidate))
        if path in seen or not Path(path).is_file():
            continue
        seen.add(path)
        candidates.append(path)

    detected = []
    for path in candidates:
        detected.append((path, _probe_exiftool(path)))

    exact = next(((path, version) for path, version in detected if version == _EXIFTOOL_VERSION), None)
    selected = exact or (detected[0] if detected else ('', ''))
    path, version = selected

    messages = []
    path_default = shutil.which('exiftool') or ''
    if not detected:
        messages.append('ExifTool 未安装或未在常见路径中找到')
    elif exact:
        if path_default and str(Path(path_default)) != path:
            default_version = next((ver for p, ver in detected if p == str(Path(path_default))), '')
            messages.append(
                f'PATH 中 exiftool 为 {default_version or "unknown"}；本 Workflow 固定使用 {path} ({version})'
            )
    else:
        detail = ', '.join(f'{p} = {v or "unknown"}' for p, v in detected)
        messages.append(f'需要 ExifTool {_EXIFTOOL_VERSION}；检测到：{detail}')

    return {
        'ready': bool(path and version == _EXIFTOOL_VERSION),
        'exiftool_path': path,
        'exiftool_version': version,
        'exiftool_required': _EXIFTOOL_VERSION,
        'icc_required_sha256': _FINAL_SRGB_ICC_SHA256,
        'messages': messages,
    }


def _requested_tag_names(settings):
    # Curated fields are requested with their explicit source group.  This is
    # important for cameras such as Canon where MakerNotes reuse standard EXIF
    # names (for example WhiteBalance) with a different numeric value domain.
    names = set()
    for key in settings['selected_fields']:
        field = _FIELD_BY_KEY[key]
        read_keys = field.get('read_keys', [])
        if read_keys:
            names.update(read_keys)
        else:
            names.add(field['key'])
    for tag in settings['additional_tags']:
        names.add(tag)
    return sorted(names, key=str.casefold)


def _run_exiftool_json(exiftool_path, files, settings, numeric=False):
    if not files:
        return {}
    args = [
        exiftool_path,
        '-j', '-a', '-G1', '-s',
        '-charset', 'filename=UTF8',
    ]
    if numeric:
        args.append('-n')
    for tag in _requested_tag_names(settings):
        args.append(f'-{tag}')
    args.extend(str(path) for path in files)
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=max(30, min(180, 15 + len(files) * 2)),
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or 'ExifTool 读取 metadata 失败')
    try:
        records = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'ExifTool 返回无效 JSON：{exc}') from exc

    by_path = {}
    for record in records:
        source = record.get('SourceFile')
        if not source:
            continue
        path = Path(source)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        by_path[key] = record
    return by_path


def _value_text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        return ', '.join(_value_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _record_value(record, field):
    # Do not fall back to an arbitrary same-name group.  Canon MakerNotes, for
    # example, use WhiteBalance=9 for Kelvin mode while standard EXIF
    # WhiteBalance only defines 0/1.  Every curated fallback must be listed
    # explicitly in read_keys so Preview and Build use the same provenance.
    for key in field.get('read_keys', []):
        if key in record:
            text = _value_text(record.get(key))
            if text != '':
                return text, key
    return '', ''


def _record_custom_value(record, tag):
    if ':' in tag:
        for key, value in record.items():
            if key.casefold() == tag.casefold():
                text = _value_text(value)
                if text != '':
                    return text, key
    name = tag.split(':')[-1].casefold()
    for key, value in record.items():
        if ':' in key and key.split(':')[-1].casefold() == name:
            text = _value_text(value)
            if text != '':
                return text, key
    return '', ''


def _field_rows(final_record, donor_record, settings):
    rows = []
    for key in settings['selected_fields']:
        field = _FIELD_BY_KEY[key]
        final_value, final_source_tag = _record_value(final_record, field)
        donor_value, donor_source_tag = _record_value(donor_record, field)
        rows.append({
            'key': key,
            'label': field['label'],
            'group': field['group'],
            'final_value': final_value,
            'final_source_tag': final_source_tag,
            'source_value': donor_value,
            'source_tag': donor_source_tag,
            'output_fixed': field.get('fixed_value', ''),
        })
    for tag in settings['additional_tags']:
        final_value, final_source_tag = _record_custom_value(final_record, tag)
        donor_value, donor_source_tag = _record_custom_value(donor_record, tag)
        rows.append({
            'key': f'custom:{tag}',
            'label': tag,
            'group': 'Custom',
            'final_value': final_value,
            'final_source_tag': final_source_tag,
            'source_value': donor_value,
            'source_tag': donor_source_tag,
            'output_fixed': '',
        })
    return rows


def _metadata_plan(source_id, set_dir: Path, set_rel: str):
    settings = _load_settings()
    dependency = _dependency_status()
    finals = _final_files(set_dir)
    base_index = _index_by_logical_id(_base_files(set_dir))
    original_index = _index_by_logical_id(_original_jpg_files(set_dir))

    signatures = {}
    rows_internal = []
    read_paths = list(finals)
    donors = []

    for final_path in finals:
        donor_path, donor_stage, donor_error = _resolve_donor(final_path, base_index, original_index)
        if donor_path is not None:
            donors.append(donor_path)
            read_paths.append(donor_path)
        rows_internal.append({
            'id': uuid.uuid4().hex,
            'final_path': str(final_path),
            'donor_path': str(donor_path) if donor_path else '',
            'donor_stage': donor_stage or '',
            'donor_error': donor_error or '',
        })
        signatures[str(final_path)] = _file_signature(final_path)
        if donor_path is not None:
            signatures[str(donor_path)] = _file_signature(donor_path)

    records = {}
    if dependency['exiftool_path'] and read_paths:
        # Preview is allowed even when the installed version is not the frozen
        # execution version so the user can inspect mappings before upgrading.
        records = _run_exiftool_json(dependency['exiftool_path'], read_paths, settings, numeric=False)

    public_rows = []
    base_count = 0
    original_count = 0
    blocked_count = 0
    for row in rows_internal:
        final_path = Path(row['final_path'])
        donor_path = Path(row['donor_path']) if row['donor_path'] else None
        final_record = records.get(str(final_path.resolve()), {}) if records else {}
        donor_record = records.get(str(donor_path.resolve()), {}) if donor_path and records else {}
        fields = _field_rows(final_record, donor_record, settings)
        final_present = sum(1 for field in fields if field['final_value'])
        donor_present = sum(1 for field in fields if field['source_value'] or field['output_fixed'])
        if row['donor_stage'] == 'Base Edit':
            base_count += 1
        elif row['donor_stage'] == 'Original/JPG':
            original_count += 1

        errors = []
        if row['donor_error']:
            errors.append(row['donor_error'])
        if donor_path is not None and not donor_path.exists():
            errors.append('Metadata donor 已不存在')
        try:
            jfif_sha = _jfif_segment_sha256(final_path)
        except Exception as exc:
            errors.append(f'Final JFIF 校验失败：{exc}')
            jfif_sha = ''
        try:
            if dependency['exiftool_path']:
                icc_sha = _icc_sha256(dependency['exiftool_path'], final_path)
                if icc_sha != _FINAL_SRGB_ICC_SHA256:
                    errors.append('Final ICC 不是 canonical sRGB IEC61966-2.1')
            else:
                icc_sha = ''
        except Exception as exc:
            errors.append(f'Final ICC 校验失败：{exc}')
            icc_sha = ''
        status = 'blocked' if errors else 'ready'
        if status == 'blocked':
            blocked_count += 1

        public_rows.append({
            'id': row['id'],
            'final_name': final_path.name,
            'final_relative_path': final_path.relative_to(set_dir).as_posix(),
            'donor_name': donor_path.name if donor_path else '',
            'donor_relative_path': donor_path.relative_to(set_dir).as_posix() if donor_path else '',
            'donor_stage': row['donor_stage'],
            'status': status,
            'errors': errors,
            'final_present': final_present,
            'source_present': donor_present,
            'field_count': len(fields),
            'fields': fields,
            'final_jfif_sha256': jfif_sha,
            'final_icc_sha256': icc_sha,
        })

    plan = {
        'kind': 'final_metadata',
        'source_id': source_id,
        'set_rel': set_rel,
        'set_dir': str(set_dir),
        'settings': settings,
        'dependency': dependency,
        'signatures': signatures,
        'rows_internal': rows_internal,
        'rows': public_rows,
        'summary': {
            'final_count': len(finals),
            'base_source_count': base_count,
            'original_source_count': original_count,
            'blocked_count': blocked_count,
            'field_count': len(settings['selected_fields']) + len(settings['additional_tags']),
        },
    }
    plan['can_execute'] = bool(finals and blocked_count == 0 and dependency['ready'])
    return plan


def _run_process(args, label, timeout=60, binary=False):
    result = subprocess.run(
        [str(arg) for arg in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        encoding=None if binary else 'utf-8',
        errors=None if binary else 'replace',
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if not binary else (result.stderr or b'').decode('utf-8', errors='replace')
        stdout = result.stdout if not binary else (result.stdout or b'').decode('utf-8', errors='replace')
        detail = (stderr or stdout or '').strip()
        raise RuntimeError(f'{label}失败：{detail or f"exit {result.returncode}"}')
    return result


def _image_data_md5(exiftool_path, path: Path):
    result = _run_process(
        [exiftool_path, '-s3', '-ImageDataMD5', str(path)],
        '读取 JPEG ImageDataMD5',
        timeout=30,
    )
    value = (result.stdout or '').strip()
    if not re.fullmatch(r'[0-9A-Fa-f]{32}', value):
        raise RuntimeError(f'无法获得 JPEG ImageDataMD5：{path.name}')
    return value.lower()


def _display_rgb_sha256(path: Path):
    # Metadata removal can change appearance even when the JPEG entropy-coded
    # image data is byte-identical (for example Orientation, or an Adobe APP14
    # transform marker).  Hash the fully decoded/display-oriented RGB pixels as
    # a second guard so metadata canonicalization cannot silently change sight.
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        rgb = image.convert('RGB')
        digest = hashlib.sha256()
        digest.update(f'{rgb.width}x{rgb.height}:RGB\0'.encode('ascii'))
        digest.update(rgb.tobytes())
        return digest.hexdigest()


def _clear_staged_metadata(exiftool_path, staged_path: Path):
    # Delete everything ExifTool considers metadata, but explicitly exclude the
    # canonical JFIF APP0 segment and ICC profile from the mass delete.  Adobe
    # APP14 is intentionally removed because ExifTool normally protects it from
    # -all=.  The staged copy is later validated to prove that JFIF and ICC are
    # byte-identical to the original Final.
    _run_process(
        [
            exiftool_path,
            '-overwrite_original',
            '-all=',
            '--JFIF:all',
            '--ICC_Profile:all',
            '-Adobe=',
            str(staged_path),
        ],
        '清空 Final metadata（保留 JFIF / ICC）',
        timeout=60,
    )


def _rebuild_staged_metadata(exiftool_path, staged_path: Path, donor_path: Path, settings):
    args = [
        exiftool_path,
        '-overwrite_original',
        '-n',
        '-tagsfromfile', str(donor_path),
    ]
    for key in settings['selected_fields']:
        field = _FIELD_BY_KEY[key]
        if field.get('copy_args'):
            args.extend(field['copy_args'])
        elif field.get('copy_arg'):
            args.append(field['copy_arg'])
    for tag in settings['additional_tags']:
        args.append(f'-{tag}')
    if 'ColorSpace' in settings['selected_fields']:
        args.append('-ExifIFD:ColorSpace=1')
    args.append(str(staged_path))
    _run_process(args, '重建 Final metadata', timeout=60)


def _jfif_segment_sha256(path: Path):
    data = path.read_bytes()
    if len(data) < 4 or data[:2] != b'\xff\xd8':
        raise RuntimeError(f'不是有效 JPEG：{path.name}')

    jfif_segments = []
    pos = 2
    size = len(data)
    while pos < size:
        if data[pos] != 0xFF:
            raise RuntimeError(f'JPEG marker 结构异常：{path.name}')
        segment_start = pos
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            break
        marker = data[pos]
        pos += 1

        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            if marker == 0xD9:
                break
            continue
        if marker == 0xDA:  # Start of Scan; APP metadata must be before this.
            break
        if pos + 2 > size:
            raise RuntimeError(f'JPEG segment 长度缺失：{path.name}')
        segment_length = int.from_bytes(data[pos:pos + 2], 'big')
        if segment_length < 2:
            raise RuntimeError(f'JPEG segment 长度无效：{path.name}')
        segment_end = pos + segment_length
        if segment_end > size:
            raise RuntimeError(f'JPEG segment 越界：{path.name}')
        payload = data[pos + 2:segment_end]
        if marker == 0xE0 and payload.startswith(b'JFIF\x00'):
            jfif_segments.append(data[segment_start:segment_end])
        pos = segment_end

    if len(jfif_segments) != 1:
        raise RuntimeError(f'要求且仅允许 1 个 JFIF APP0，实际 {len(jfif_segments)} 个：{path.name}')
    return hashlib.sha256(jfif_segments[0]).hexdigest()


def _icc_sha256(exiftool_path, path: Path):
    result = _run_process(
        [exiftool_path, '-b', '-ICC_Profile', str(path)],
        '读取 Final ICC',
        timeout=30,
        binary=True,
    )
    data = result.stdout or b''
    if not data:
        raise RuntimeError(f'Final 缺少 ICC profile：{path.name}')
    return hashlib.sha256(data).hexdigest()


def _forbidden_metadata(exiftool_path, path: Path):
    result = _run_process(
        [exiftool_path, '-j', '-a', '-G1', '-s', str(path)],
        '验证 Final metadata',
        timeout=60,
    )
    try:
        records = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'验证 metadata 时 ExifTool JSON 无效：{exc}') from exc
    record = records[0] if records else {}
    forbidden = []
    for key in record:
        if ':' not in key:
            continue
        group, tag = key.split(':', 1)
        group_cf = group.casefold()
        tag_cf = tag.casefold()
        if group_cf.startswith('xmp') or group_cf in {'iptc', 'photoshop', 'adobe', 'makernotes', 'ifd1'}:
            forbidden.append(key)
        elif tag_cf in {'thumbnailimage', 'previewimage', 'comment'}:
            forbidden.append(key)
    return sorted(set(forbidden), key=str.casefold)


def _validate_staged(exiftool_path, original_final: Path, staged_path: Path):
    before_md5 = _image_data_md5(exiftool_path, original_final)
    after_md5 = _image_data_md5(exiftool_path, staged_path)
    if before_md5 != after_md5:
        raise RuntimeError(f'JPEG image data 发生变化，拒绝发布：{original_final.name}')

    before_display = _display_rgb_sha256(original_final)
    after_display = _display_rgb_sha256(staged_path)
    if before_display != after_display:
        raise RuntimeError(f'metadata 清理导致显示像素发生变化，拒绝发布：{original_final.name}')

    before_icc_sha = _icc_sha256(exiftool_path, original_final)
    if before_icc_sha != _FINAL_SRGB_ICC_SHA256:
        raise RuntimeError(f'原 Final ICC 不是 canonical profile，拒绝处理：{original_final.name}')
    after_icc_sha = _icc_sha256(exiftool_path, staged_path)
    if after_icc_sha != before_icc_sha:
        raise RuntimeError(f'ICC 在 metadata workflow 中发生变化，拒绝发布：{original_final.name}')

    before_jfif_sha = _jfif_segment_sha256(original_final)
    after_jfif_sha = _jfif_segment_sha256(staged_path)
    if after_jfif_sha != before_jfif_sha:
        raise RuntimeError(f'JFIF APP0 在 metadata workflow 中发生变化，拒绝发布：{original_final.name}')

    forbidden = _forbidden_metadata(exiftool_path, staged_path)
    if forbidden:
        preview = ', '.join(forbidden[:8])
        raise RuntimeError(f'清理后仍存在禁止 metadata：{preview}')


def _publish_replacements(staged_items):
    published = []
    try:
        for item in staged_items:
            staged_path = item['staged_path']
            final_path = item['final_path']
            os.replace(staged_path, final_path)
            published.append(item)
        return [item['final_path'] for item in published]
    except Exception as exc:
        rollback_errors = []
        for item in reversed(published):
            try:
                os.replace(item['backup_path'], item['final_path'])
            except Exception as rollback_exc:
                rollback_errors.append(f'{item["final_path"].name}: {rollback_exc}')
        if rollback_errors:
            raise RuntimeError(
                f'发布 Final metadata 失败：{exc}；且回滚失败：{"; ".join(rollback_errors)}'
            ) from exc
        raise


def _thumbnail(path: Path):
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
        # Keep metadata previews aligned with Final Builder previews: Retina-friendly
        # 512 px square, Lanczos resampling, and Q85. This is UI-only and never
        # modifies Final or donor source files.
        image = ImageOps.fit(image, (512, 512), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        buffer = io.BytesIO()
        image.save(buffer, 'JPEG', quality=85, optimize=True)
        buffer.seek(0)
        return buffer


def create_final_metadata_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('final_metadata', __name__)

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

    @bp.route('/api/library/final-metadata/settings', methods=['GET'])
    def final_metadata_settings_get():
        denied = admin_guard()
        if denied:
            return denied
        try:
            return jsonify({
                'settings': _load_settings(),
                'fields': _field_catalog_public(),
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/final-metadata/settings', methods=['POST'])
    def final_metadata_settings_save():
        denied = admin_guard()
        if denied:
            return denied
        try:
            data = request.get_json(silent=True) or {}
            settings = _save_settings(data.get('settings') or {})
            return jsonify({'settings': settings, 'fields': _field_catalog_public()})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/final-metadata/sources/<int:source_id>/plan', methods=['POST'])
    def final_metadata_plan(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            plan = _metadata_plan(source_id, set_dir, set_rel)
            plan_id = _remember_plan(plan)
            return jsonify({
                'plan_id': plan_id,
                'rows': plan['rows'],
                'summary': plan['summary'],
                'dependency': plan['dependency'],
                'settings': plan['settings'],
                'fields': _field_catalog_public(),
                'can_execute': plan['can_execute'],
                'profile': {
                    'name': 'final-metadata-v4',
                    'exiftool': _EXIFTOOL_VERSION,
                    'clear': '-all= --JFIF:all --ICC_Profile:all -Adobe=',
                    'jfif': 'preserve existing JFIF APP0 byte-for-byte',
                    'icc': 'preserve existing canonical ICC byte-for-byte',
                    'icc_sha256': _FINAL_SRGB_ICC_SHA256,
                    'donor_rule': 'Original/JPG -> Base Edit -> Block',
                },
            })
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/final-metadata/sources/<int:source_id>/thumbnail/<plan_id>/<row_id>/<side>', methods=['GET'])
    def final_metadata_thumbnail(source_id, plan_id, row_id, side):
        denied = admin_guard()
        if denied:
            return denied
        try:
            with _PLAN_LOCK:
                plan = _PLANS.get(str(plan_id))
            if not plan or plan.get('kind') != 'final_metadata' or int(plan.get('source_id')) != int(source_id):
                raise FileNotFoundError('Metadata Preview 已过期')
            row = next((item for item in plan['rows_internal'] if item['id'] == str(row_id)), None)
            if not row:
                raise FileNotFoundError('Preview row 不存在')
            if side == 'final':
                path = Path(row['final_path'])
            elif side == 'source':
                if not row['donor_path']:
                    raise FileNotFoundError('没有 metadata donor')
                path = Path(row['donor_path'])
            else:
                raise ValueError('thumbnail side 无效')
            expected = plan['signatures'].get(str(path))
            if not expected or _file_signature(path) != expected:
                raise RuntimeError('图片在 Preview 后发生变化，请重新 Preview')
            return send_file(_thumbnail(path), mimetype='image/jpeg', max_age=120)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 404

    @bp.route('/api/library/final-metadata/sources/<int:source_id>/start', methods=['POST'])
    def final_metadata_start(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            plan = _get_plan(str(data.get('plan_id') or ''), source_id, set_rel)
            dependency = _dependency_status()
            if not dependency['ready']:
                raise RuntimeError(f'需要固定 ExifTool {_EXIFTOOL_VERSION}，请处理 Runtime 后重新 Preview')
            if any(row['status'] == 'blocked' for row in plan['rows']):
                raise RuntimeError('Metadata Plan 中仍有阻断项')
            _verify_signatures(plan['signatures'])

            task_id = _new_task(len(plan['rows_internal']))

            def worker():
                try:
                    _update_task(task_id, status='running', message='开始重建 Final metadata…')
                    with tempfile.TemporaryDirectory(prefix='.final-metadata-', dir=str(set_dir)) as temp_name:
                        temp_dir = Path(temp_name)
                        staged_dir = temp_dir / 'staged'
                        backup_dir = temp_dir / 'backup'
                        staged_dir.mkdir()
                        backup_dir.mkdir()
                        staged_items = []

                        for index, row in enumerate(plan['rows_internal'], start=1):
                            final_path = Path(row['final_path'])
                            donor_path = Path(row['donor_path'])
                            message = f'Metadata {index}/{len(plan["rows_internal"])} · {final_path.name}'
                            _update_task(task_id, current=index, message=message, log=message)

                            staged_path = staged_dir / final_path.name
                            backup_path = backup_dir / final_path.name
                            shutil.copy2(final_path, staged_path)
                            shutil.copy2(final_path, backup_path)

                            _clear_staged_metadata(dependency['exiftool_path'], staged_path)
                            _rebuild_staged_metadata(
                                dependency['exiftool_path'],
                                staged_path,
                                donor_path,
                                plan['settings'],
                            )
                            _validate_staged(dependency['exiftool_path'], final_path, staged_path)
                            staged_items.append({
                                'staged_path': staged_path,
                                'backup_path': backup_path,
                                'final_path': final_path,
                            })
                            _update_task(task_id, completed=index, current=index, message=message)

                        # Do not publish a batch if any Final or donor changed while
                        # ExifTool was working on the staged copies.
                        _verify_signatures(plan['signatures'])
                        published = _publish_replacements(staged_items)

                    result = {
                        'count': len(published),
                        'profile': 'final-metadata-v4',
                        'exiftool_version': _EXIFTOOL_VERSION,
                    }
                    _update_task(
                        task_id,
                        status='done',
                        completed=len(plan['rows_internal']),
                        message=f'完成：重建 {len(published)} 张 Final metadata',
                        result=result,
                        log=f'Published metadata for {len(published)} Final file(s)',
                    )
                except Exception as exc:
                    _update_task(
                        task_id,
                        status='error',
                        message='Final Metadata 失败',
                        error=str(exc),
                        log=f'ERROR: {exc}',
                    )

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 409

    @bp.route('/api/library/final-metadata/tasks/<task_id>', methods=['GET'])
    def final_metadata_task_status(task_id):
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
