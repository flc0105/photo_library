import hashlib
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image

from core.external_tools import (
    EXIFTOOL_REQUIRED_VERSION,
    IMAGEMAGICK_REQUIRED_QUANTUM,
    IMAGEMAGICK_REQUIRED_VERSION,
)
from core.final_delivery_contract import (
    FINAL_JPEG_BASELINE,
    FINAL_JPEG_CHROMA_SAMPLING,
    FINAL_JPEG_HUFFMAN_OPTIMIZE,
    FINAL_JPEG_QUALITY,
    FINAL_SRGB_ICC_SHA256,
    FINAL_SRGB_PROFILE_DESCRIPTION,
)
from core.final_metadata_fields import load_metadata_field_keys


_MANIFEST_FILENAME = 'build.json'

_PROFILE_BASE = {
    'crop': {
        'mode': 'center',
        'portrait': '2:3',
        'landscape': '3:2',
    },
    'resize': {
        'filter': 'Lanczos',
        'lobes': 3,
    },
    'jpeg': {
        'quality': FINAL_JPEG_QUALITY,
        'sampling': FINAL_JPEG_CHROMA_SAMPLING,
        'baseline': FINAL_JPEG_BASELINE,
        'huffman_optimize': FINAL_JPEG_HUFFMAN_OPTIMIZE,
    },
    'icc': {
        'name': FINAL_SRGB_PROFILE_DESCRIPTION,
        'sha256': FINAL_SRGB_ICC_SHA256,
    },
}


def _current_profile(metadata_fields=None):
    # 顶层 profile 只记录所有 Final 共用、长期稳定的构建参数。
    # 实际输出尺寸属于单张图片的结果，记录在 images.<id>.geometry.target。
    profile = json.loads(json.dumps(_PROFILE_BASE))
    profile['metadata_fields'] = list(metadata_fields if metadata_fields is not None else load_metadata_field_keys())
    return profile


_TOOLS = {
    'imagemagick': f'{IMAGEMAGICK_REQUIRED_VERSION} {IMAGEMAGICK_REQUIRED_QUANTUM}',
    'libjpeg-turbo': '3.2.0',
    'exiftool': EXIFTOOL_REQUIRED_VERSION,
}

_LOCK = threading.Lock()


def file_sha256(path: Path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _now_text():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _manifest_path(set_dir: Path):
    return Path(set_dir) / _MANIFEST_FILENAME


def _final_dir_has_files(set_dir: Path):
    final_dir = Path(set_dir) / '05_Final'
    if not final_dir.is_dir():
        return False
    try:
        return any(
            path.is_file() and not path.name.startswith('.') and not path.name.startswith('._')
            for path in final_dir.iterdir()
        )
    except OSError:
        # 读取失败时不要把 build.json 当成“空目录”误删。
        return True


def _prune_missing_final_records(set_dir: Path, data):
    """Remove image provenance whose recorded Final file no longer exists."""
    images = data.get('images')
    if not isinstance(images, dict):
        return False

    changed = False
    for stem, image in list(images.items()):
        if not isinstance(image, dict):
            continue
        final = image.get('final')
        relative_path = final.get('path') if isinstance(final, dict) else None
        if not isinstance(relative_path, str) or not relative_path.strip():
            images.pop(stem, None)
            changed = True
            continue
        final_path = Path(set_dir) / relative_path
        if final_path.is_file():
            continue
        images.pop(stem, None)
        changed = True
    return changed


def prepare_build_manifest(set_dir: Path, metadata_fields=None):
    """Clean stale provenance immediately before a new Final build.

    An empty 05_Final means the previous build.json describes a Final set that no
    longer exists, so start clean.  For a partially populated 05_Final, retain
    existing records only when their recorded Final file is still present.
    This cleanup is provenance-only and must never block Final Builder.
    """
    set_dir = Path(set_dir)
    path = _manifest_path(set_dir)
    try:
        with _LOCK:
            if not _final_dir_has_files(set_dir):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return True, ''

            if not path.exists():
                return True, ''

            data = _load_manifest(set_dir, metadata_fields)
            _write_manifest(set_dir, data)
        return True, ''
    except Exception as exc:
        return False, str(exc)


def _empty_manifest(metadata_fields=None):
    return {
        'profile': _current_profile(metadata_fields),
        'tools': dict(_TOOLS),
        'images': {},
    }


_SOURCE_RECORD_KEYS = {
    'stage',
    'path',
    'sha256',
    'width',
    'height',
    'orientation',
}
_COMMANDS_BUILD_KEYS = {'steps', 'pipeline'}
_COMMANDS_METADATA_KEYS = {'steps'}
_COMMAND_STEP_KEYS = {'name', 'tool', 'argv', 'cwd', 'stdin', 'stdout'}
_PIPELINE_KEYS = {'replay_shell', 'cwd'}
_BUILD_RECORD_KEYS = {'time', 'status', 'exit_code', 'error', 'commands'}
_METADATA_RECORD_KEYS = {
    'time', 'donor', 'donor_sha256', 'values', 'status', 'exit_code', 'error', 'commands'
}


def _validate_commands(section_name, commands):
    if commands is None:
        return
    if not isinstance(commands, dict):
        raise RuntimeError(f'build.json {section_name}.commands 结构无效')

    allowed = _COMMANDS_BUILD_KEYS if section_name == 'build' else _COMMANDS_METADATA_KEYS
    extra = sorted(set(commands) - allowed)
    if extra:
        raise RuntimeError(
            f'build.json {section_name}.commands 包含当前 schema 未定义字段：{", ".join(extra)}'
        )

    steps = commands.get('steps')
    if not isinstance(steps, list):
        raise RuntimeError(f'build.json {section_name}.commands.steps 结构无效')
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise RuntimeError(f'build.json {section_name}.commands.steps[{index}] 结构无效')
        extra_step = sorted(set(step) - _COMMAND_STEP_KEYS)
        if extra_step:
            raise RuntimeError(
                f'build.json {section_name}.commands.steps[{index}] 包含当前 schema 未定义字段：'
                f'{", ".join(extra_step)}'
            )
        if not isinstance(step.get('argv'), list):
            raise RuntimeError(f'build.json {section_name}.commands.steps[{index}].argv 结构无效')

    if section_name == 'build':
        pipeline = commands.get('pipeline')
        if not isinstance(pipeline, dict):
            raise RuntimeError('build.json build.commands.pipeline 结构无效')
        extra_pipeline = sorted(set(pipeline) - _PIPELINE_KEYS)
        if extra_pipeline:
            raise RuntimeError(
                'build.json build.commands.pipeline 包含当前 schema 未定义字段：'
                + ', '.join(extra_pipeline)
            )
        if not isinstance(pipeline.get('replay_shell'), str) or not pipeline['replay_shell'].strip():
            raise RuntimeError('build.json build.commands.pipeline.replay_shell 结构无效')


def _validate_current_manifest(data):
    for stem, image in data['images'].items():
        if not isinstance(image, dict):
            raise RuntimeError(f'build.json images.{stem} 结构无效')

        source = image.get('source')
        if source is not None:
            if not isinstance(source, dict):
                raise RuntimeError(f'build.json images.{stem}.source 结构无效')
            extra_source = sorted(set(source) - _SOURCE_RECORD_KEYS)
            if extra_source:
                raise RuntimeError(
                    f'build.json images.{stem}.source 包含当前 schema 未定义字段：'
                    + ', '.join(extra_source)
                )

        for section_name in ('build', 'metadata'):
            section = image.get(section_name)
            if section is None:
                continue
            if not isinstance(section, dict):
                raise RuntimeError(f'build.json images.{stem}.{section_name} 结构无效')
            allowed_section = _BUILD_RECORD_KEYS if section_name == 'build' else _METADATA_RECORD_KEYS
            extra_section = sorted(set(section) - allowed_section)
            if extra_section:
                raise RuntimeError(
                    f'build.json images.{stem}.{section_name} 包含当前 schema 未定义字段：'
                    + ', '.join(extra_section)
                )
            if 'commands' in section:
                _validate_commands(section_name, section.get('commands'))


def _load_manifest(set_dir: Path, metadata_fields=None):
    path = _manifest_path(set_dir)
    if not path.exists():
        return _empty_manifest(metadata_fields)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        # build.json is optional provenance only.  Do not guess around a damaged
        # user-visible file; callers simply skip the manifest update.
        raise RuntimeError(f'无法读取 {path.name}')
    if not isinstance(data, dict) or not isinstance(data.get('images'), dict):
        raise RuntimeError(f'{path.name} 结构无效')
    _validate_current_manifest(data)
    data['profile'] = _current_profile(metadata_fields)
    data['tools'] = dict(_TOOLS)
    _prune_missing_final_records(set_dir, data)
    return data


def _write_manifest(set_dir: Path, data):
    path = _manifest_path(set_dir)
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    payload = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    try:
        temp.write_text(payload, encoding='utf-8')
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _update_manifest(set_dir: Path, updater, metadata_fields=None):
    try:
        with _LOCK:
            data = _load_manifest(set_dir, metadata_fields)
            updater(data)
            _write_manifest(set_dir, data)
        return True, ''
    except Exception as exc:
        # Manifest writing must never make Final Builder / Write Metadata fail.
        return False, str(exc)


def _source_orientation(path: Path):
    try:
        with Image.open(path) as image:
            value = image.getexif().get(274, 1)
            return int(value or 1)
    except Exception:
        return None


def _build_source_record(item):
    source_path = Path(item['source_path'])
    return {
        'stage': item.get('stage_label') or item.get('stage') or '',
        'path': item.get('source_relative_path') or source_path.name,
        'sha256': file_sha256(source_path),
        'width': int(item.get('source_width') or 0),
        'height': int(item.get('source_height') or 0),
        'orientation': _source_orientation(source_path),
    }


def _build_geometry_record(item):
    geometry = item.get('geometry') or {}
    return {
        'crop': {
            'left': int(geometry.get('crop_left') or 0),
            'right': int(geometry.get('crop_right') or 0),
            'top': int(geometry.get('crop_top') or 0),
            'bottom': int(geometry.get('crop_bottom') or 0),
            'loss_percent': float(geometry.get('crop_percent') or 0),
        },
        'target': {
            'width': int(geometry.get('target_width') or 0),
            'height': int(geometry.get('target_height') or 0),
        },
        'resize': bool(geometry.get('resize_required')),
    }


def record_build_success(
    set_dir: Path,
    items,
    built_at_by_stem,
    commands_by_stem=None,
    metadata_fields=None,
):
    set_dir = Path(set_dir)

    def updater(data):
        for item in items:
            stem = str(item.get('logical_id') or '').strip()
            if not stem:
                continue
            built_at = (built_at_by_stem or {}).get(stem)
            if not built_at:
                raise RuntimeError(f'缺少逐张 Final build time：{stem}')
            final_path = set_dir / str(item.get('output_relative_path') or f'05_Final/{stem}.jpg')
            data['images'][stem] = {
                'source': _build_source_record(item),
                'geometry': _build_geometry_record(item),
                'final': {
                    'path': final_path.relative_to(set_dir).as_posix(),
                    'sha256': file_sha256(final_path),
                    # Filled by Write Metadata when ExifTool is already part of
                    # the active workflow.  Final Builder does not gain an
                    # ExifTool dependency just for provenance bookkeeping.
                    'image_data_md5': None,
                },
                'build': {
                    'time': built_at,
                    'status': 'ok',
                    'exit_code': 0,
                    'error': None,
                    'commands': (commands_by_stem or {}).get(stem),
                },
                'metadata': None,
            }

    return _update_manifest(set_dir, updater, metadata_fields)


def record_build_failure(set_dir: Path, items, error, commands_by_stem=None, metadata_fields=None):
    set_dir = Path(set_dir)
    failed_at = _now_text()
    error_text = str(error or '')

    def updater(data):
        for item in items:
            stem = str(item.get('logical_id') or '').strip()
            if not stem:
                continue
            existing = data['images'].get(stem)
            # Do not replace a valid current Final record with a failed retry.
            if isinstance(existing, dict) and (existing.get('build') or {}).get('status') == 'ok':
                continue
            data['images'][stem] = {
                'source': _build_source_record(item),
                'geometry': _build_geometry_record(item),
                'final': {
                    'path': str(item.get('output_relative_path') or f'05_Final/{stem}.jpg'),
                    'sha256': None,
                    'image_data_md5': None,
                },
                'build': {
                    'time': failed_at,
                    'status': 'error',
                    'exit_code': 1,
                    'error': error_text,
                    'commands': (commands_by_stem or {}).get(stem),
                },
                'metadata': None,
            }

    return _update_manifest(set_dir, updater, metadata_fields)


def _metadata_values(public_row):
    values = {}
    for field in public_row.get('fields') or []:
        key = str(field.get('key') or '').strip()
        if not key:
            continue
        if key.startswith('custom:'):
            key = key[len('custom:'):]
        value = field.get('output_fixed') or field.get('source_value') or ''
        values[key] = value
    return values


def record_metadata_success(
    set_dir: Path,
    rows,
    public_by_id,
    image_data_md5_by_path,
    written_at_by_row_id,
    commands_by_row_id=None,
    metadata_fields=None,
):
    set_dir = Path(set_dir)

    def updater(data):
        for row in rows:
            written_at = (written_at_by_row_id or {}).get(row['id'])
            if not written_at:
                raise RuntimeError(f'缺少逐张 Final metadata time：{row["id"]}')
            final_path = Path(row['final_path'])
            stem = final_path.stem.split('-', 1)[0]
            public_row = public_by_id.get(row['id']) or {}
            donor_path = Path(row['donor_path']) if row.get('donor_path') else None
            existing = data['images'].get(stem)
            if not isinstance(existing, dict):
                existing = {
                    'source': None,
                    'geometry': None,
                    'final': None,
                    'build': None,
                    'metadata': None,
                }
                data['images'][stem] = existing

            existing['final'] = {
                'path': final_path.relative_to(set_dir).as_posix(),
                'sha256': file_sha256(final_path),
                'image_data_md5': image_data_md5_by_path.get(str(final_path), None),
            }
            existing['metadata'] = {
                'time': written_at,
                'donor': donor_path.relative_to(set_dir).as_posix() if donor_path else None,
                'donor_sha256': file_sha256(donor_path) if donor_path else None,
                'values': _metadata_values(public_row),
                'status': 'ok',
                'exit_code': 0,
                'error': None,
                'commands': (commands_by_row_id or {}).get(row['id']),
            }

    return _update_manifest(set_dir, updater, metadata_fields)


def record_metadata_failure(set_dir: Path, rows, error, commands_by_row_id=None, metadata_fields=None):
    set_dir = Path(set_dir)
    failed_at = _now_text()
    error_text = str(error or '')

    def updater(data):
        for row in rows:
            final_path = Path(row['final_path'])
            stem = final_path.stem.split('-', 1)[0]
            existing = data['images'].get(stem)
            if not isinstance(existing, dict):
                existing = {
                    'source': None,
                    'geometry': None,
                    'final': {
                        'path': final_path.relative_to(set_dir).as_posix(),
                        'sha256': file_sha256(final_path) if final_path.exists() else None,
                        'image_data_md5': None,
                    },
                    'build': None,
                    'metadata': None,
                }
                data['images'][stem] = existing
            if isinstance(existing.get('metadata'), dict) and existing['metadata'].get('status') == 'ok':
                continue
            donor_path = Path(row['donor_path']) if row.get('donor_path') else None
            existing['metadata'] = {
                'time': failed_at,
                'donor': donor_path.relative_to(set_dir).as_posix() if donor_path else None,
                'donor_sha256': file_sha256(donor_path) if donor_path and donor_path.exists() else None,
                'values': {},
                'status': 'error',
                'exit_code': 1,
                'error': error_text,
                'commands': (commands_by_row_id or {}).get(row['id']),
            }

    return _update_manifest(set_dir, updater, metadata_fields)
