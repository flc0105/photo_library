import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify

from core.filesystem import original_stem_key

_SET_RE = re.compile(r'^\d{8}-.+-.+$')
_STAGE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}
_ORIGINAL_JPG_EXTENSIONS = {'.jpg', '.jpeg'}
_RAW_EXTENSIONS = {'.cr3'}
_IGNORED_CONTROL_NAMES = {'manifest.json', 'manifest.json.bak', 'manifest.json.tmp'}
_REQUIRED_SET_DIRS = {'01_Original', '02_Base_Edit', '03_Model_Edit', '04_Revision', '05_Final'}
_ALLOWED_ORIGINAL_DIRS = {'JPG', 'RAW'}


def _is_hidden_or_control(name):
    return name.startswith('.') or name in _IGNORED_CONTROL_NAMES

def _top_level_subdirs(path: Path):
    if not path.is_dir():
        return set()
    result = set()
    try:
        for item in path.iterdir():
            if item.name.startswith('.'):
                continue
            if item.is_dir() and not item.is_symlink():
                result.add(item.name)
    except OSError:
        return result
    return result


def _direct_files(path: Path, extensions=None):
    if not path.is_dir():
        return []
    extensions = {ext.lower() for ext in extensions} if extensions else None
    result = []
    try:
        for item in path.iterdir():
            if _is_hidden_or_control(item.name) or not item.is_file():
                continue
            if extensions is not None and item.suffix.lower() not in extensions:
                continue
            result.append(item)
    except OSError:
        return result
    return sorted(result, key=lambda item: item.name.casefold())


def _direct_invalid_files(path: Path, allowed_extensions):
    return [
        file_path for file_path in _direct_files(path)
        if file_path.suffix.lower() not in allowed_extensions
    ]


def _set_structure_findings(set_dir: Path):
    missing = []
    unexpected = []

    top_dirs = _top_level_subdirs(set_dir)
    missing.extend(sorted(_REQUIRED_SET_DIRS - top_dirs))
    unexpected.extend(sorted(top_dirs - _REQUIRED_SET_DIRS))

    original_dir = set_dir / '01_Original'
    if original_dir.is_dir():
        original_subdirs = _top_level_subdirs(original_dir)
        missing.extend(
            f'01_Original/{name}'
            for name in sorted(_ALLOWED_ORIGINAL_DIRS - original_subdirs)
        )
        unexpected.extend(
            f'01_Original/{name}'
            for name in sorted(original_subdirs - _ALLOWED_ORIGINAL_DIRS)
        )

    # Every standard leaf directory is file-only. Reporting the first unexpected
    # child is enough; descendants are already outside the standard structure.
    leaf_dirs = (
        '01_Original/JPG',
        '01_Original/RAW',
        '02_Base_Edit',
        '03_Model_Edit',
        '04_Revision',
        '05_Final',
    )
    for relative in leaf_dirs:
        for name in sorted(_top_level_subdirs(set_dir / relative)):
            unexpected.append(f'{relative}/{name}')

    return missing, unexpected


def _format_file_preview(names):
    names = list(names)
    if len(names) <= 3:
        return ', '.join(names)
    return f'{", ".join(names[:2])} · {len(names)} total'


def _format_stem_counter(counter, labels):
    items = []
    for stem, count in sorted(counter.items()):
        label = labels.get(stem, stem)
        items.append(f'{label} ×{count}' if count > 1 else label)
    return ', '.join(items)


def _structure_affects(paths, roots):
    roots = set(roots)
    return any(path.split('/', 1)[0] in roots for path in paths)


def _stage_filename_violations(paths, original_stems):
    """Return stage images whose stem is not an exact Original file stem."""
    if not original_stems:
        return []
    return [path for path in paths if path.stem.casefold() not in original_stems]


def _format_stage_filename_warning(stage_label, paths):
    names = [path.name for path in paths]
    return f'{stage_label} filename does not match Original stem: {_format_file_preview(names)}'


def _validate_one_set(set_dir: Path):
    issues = []
    warnings = []
    info = []

    missing, unexpected = _set_structure_findings(set_dir)
    if missing:
        issues.append('Missing standard directories: ' + ', '.join(missing))
    if unexpected:
        issues.append('Unexpected directories: ' + ', '.join(unexpected))

    jpg_dir = set_dir / '01_Original' / 'JPG'
    raw_dir = set_dir / '01_Original' / 'RAW'
    jpg_files = _direct_files(jpg_dir, _ORIGINAL_JPG_EXTENSIONS)
    raw_files = _direct_files(raw_dir, _RAW_EXTENSIONS)
    invalid_jpg = _direct_invalid_files(jpg_dir, _ORIGINAL_JPG_EXTENSIONS)
    invalid_raw = _direct_invalid_files(raw_dir, _RAW_EXTENSIONS)
    if invalid_jpg:
        issues.append(
            'Original JPG contains non-JPG files: '
            + _format_file_preview(path.name for path in invalid_jpg)
        )
    if invalid_raw:
        issues.append(
            'Original RAW contains non-CR3 files: '
            + _format_file_preview(path.name for path in invalid_raw)
        )

    jpg_count = len(jpg_files)
    raw_count = len(raw_files)
    jpg_stems = Counter(original_stem_key(path, is_jpg=True) for path in jpg_files)
    raw_stems = Counter(original_stem_key(path, is_jpg=False) for path in raw_files)
    jpg_stem_labels = {}
    raw_stem_labels = {}
    for path in jpg_files:
        stem = original_stem_key(path, is_jpg=True)
        label = path.stem[:-4] if path.stem.casefold().endswith('-dpp') else path.stem
        jpg_stem_labels.setdefault(stem, label)
    for path in raw_files:
        raw_stem_labels.setdefault(original_stem_key(path, is_jpg=False), path.stem)

    if raw_dir.is_dir() and raw_count == 0:
        issues.append('Original RAW is empty')
    if jpg_dir.is_dir() and raw_count > 0 and jpg_count == 0:
        info.append(f'Original JPG is empty (RAW ×{raw_count})')
    if jpg_count > 0 and raw_count > 0 and jpg_stems != raw_stems:
        jpg_extra = jpg_stems - raw_stems
        raw_extra = raw_stems - jpg_stems
        if jpg_extra:
            count = sum(jpg_extra.values())
            issues.append(
                f'JPG/RAW mismatch, RAW missing ({count}): '
                f'{_format_stem_counter(jpg_extra, jpg_stem_labels)}'
            )
        if raw_extra:
            count = sum(raw_extra.values())
            issues.append(
                f'JPG/RAW mismatch, JPG missing ({count}): '
                f'{_format_stem_counter(raw_extra, raw_stem_labels)}'
            )

    dpp_files = [path.name for path in jpg_files if path.stem.casefold().endswith('-dpp')]

    # Validation counts only files directly inside each standard stage. Any
    # nested directory is already reported as a structure Issue.
    base_files = _direct_files(set_dir / '02_Base_Edit', _STAGE_IMAGE_EXTENSIONS)
    model_files = _direct_files(set_dir / '03_Model_Edit', _STAGE_IMAGE_EXTENSIONS)
    revision_files = _direct_files(set_dir / '04_Revision', _STAGE_IMAGE_EXTENSIONS)
    final_files = _direct_files(set_dir / '05_Final', _ORIGINAL_JPG_EXTENSIONS)

    base_count = len(base_files)
    model_count = len(model_files)

    original_stems = set(jpg_stems) | set(raw_stems)
    filename_warning_by_stage = {}
    for stage_label, files in (
        ('Base Edit', base_files),
        ('Model Edit', model_files),
        ('Revision', revision_files),
    ):
        violations = _stage_filename_violations(files, original_stems)
        filename_warning_by_stage[stage_label] = bool(violations)
        if violations:
            warnings.append(_format_stage_filename_warning(stage_label, violations))

    base_stems = Counter(path.stem.casefold() for path in base_files)
    model_stems = Counter(path.stem.casefold() for path in model_files)

    non_png_revision = [path for path in revision_files if path.suffix.lower() != '.png']
    if non_png_revision:
        issues.append(
            'Revision must be PNG: '
            + _format_file_preview(path.name for path in non_png_revision)
        )

    base_stem_labels = {}
    model_stem_labels = {}
    for path in base_files:
        base_stem_labels.setdefault(path.stem.casefold(), path.stem)
    for path in model_files:
        model_stem_labels.setdefault(path.stem.casefold(), path.stem)

    if base_count > 0 and model_count > 0 and base_stems != model_stems:
        base_extra = base_stems - model_stems
        model_extra = model_stems - base_stems
        if base_extra:
            count = sum(base_extra.values())
            warnings.append(
                f'Base/Model mismatch, Model missing ({count}): '
                f'{_format_stem_counter(base_extra, base_stem_labels)}'
            )
        if model_extra:
            count = sum(model_extra.values())
            warnings.append(
                f'Base/Model mismatch, Base missing ({count}): '
                f'{_format_stem_counter(model_extra, model_stem_labels)}'
            )
    elif base_count > 0 and model_count == 0:
        info.append(f'Model Edit is empty (Base Edit ×{base_count})')
    elif base_count == 0 and model_count > 0:
        info.append(f'Base Edit is empty (Model Edit ×{model_count})')
    elif base_count == 0 and model_count == 0:
        info.append('Base Edit and Model Edit are both empty')

    structure_paths = missing + unexpected
    original_issue = (
        _structure_affects(structure_paths, {'01_Original'})
        or bool(invalid_jpg)
        or bool(invalid_raw)
        or (raw_dir.is_dir() and raw_count == 0)
        or (jpg_count > 0 and raw_count > 0 and jpg_stems != raw_stems)
    )
    base_model_issue = _structure_affects(structure_paths, {'02_Base_Edit', '03_Model_Edit'})
    revision_issue = (
        _structure_affects(structure_paths, {'04_Revision'})
        or bool(non_png_revision)
    )
    final_issue = _structure_affects(structure_paths, {'05_Final'})

    if original_issue:
        original_status = 'issue'
    elif jpg_dir.is_dir() and raw_count > 0 and jpg_count == 0:
        original_status = 'info'
    else:
        original_status = 'ok'

    if base_model_issue:
        base_model_status = 'issue'
    elif (
        (base_count > 0 and model_count > 0 and base_stems != model_stems)
        or filename_warning_by_stage.get('Base Edit')
        or filename_warning_by_stage.get('Model Edit')
    ):
        base_model_status = 'warning'
    elif base_count == 0 or model_count == 0:
        base_model_status = 'info'
    else:
        base_model_status = 'ok'

    if revision_issue:
        revision_status = 'issue'
    elif filename_warning_by_stage.get('Revision'):
        revision_status = 'warning'
    else:
        revision_status = 'ok'

    final_status = 'issue' if final_issue else 'ok'

    if issues:
        status = 'issue'
    elif warnings:
        status = 'warning'
    elif info:
        status = 'info'
    else:
        status = 'ok'

    return {
        'name': set_dir.name,
        'date_key': set_dir.name[:8],
        'status': status,
        'jpg_count': jpg_count,
        'raw_count': raw_count,
        'original_status': original_status,
        'original_count_match': jpg_count > 0 and raw_count > 0 and jpg_stems == raw_stems,
        'base_count': base_count,
        'model_count': model_count,
        'base_model_status': base_model_status,
        'revision_count': len(revision_files),
        'revision_status': revision_status,
        'final_count': len(final_files),
        'final_status': final_status,
        'base_model_match': base_count > 0 and model_count > 0 and base_stems == model_stems,
        'dpp_files': dpp_files,
        'issues': issues,
        'warnings': warnings,
        'info': info,
    }


def _validate_root(root: Path):
    sets = []
    try:
        children = sorted(
            [path for path in root.iterdir() if path.is_dir() and _SET_RE.fullmatch(path.name)],
            key=lambda path: path.name.casefold(),
        )
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc

    for set_dir in children:
        sets.append(_validate_one_set(set_dir))

    status_counts = Counter(item['status'] for item in sets)

    return {
        'root': str(root),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'set_count': len(sets),
            'issue_count': status_counts.get('issue', 0),
            'warning_count': status_counts.get('warning', 0),
            'info_count': status_counts.get('info', 0),
            'ok_count': status_counts.get('ok', 0),
        },
        'sets': sets,
    }


def create_validate_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('library_validate', __name__)

    @bp.route('/api/library/insights/sources/<int:source_id>/validate-root', methods=['POST'])
    def validate_root(source_id):
        denied = admin_guard()
        if denied:
            return denied
        source = get_source(source_id)
        if not source:
            return jsonify({'error': 'Source unavailable'}), 404
        try:
            root, target, rel = resolve_path(source, '')
            if rel or target != root:
                return jsonify({'error': 'Validation is only available at the Source root'}), 400
            return jsonify(_validate_root(root))
        except Exception as exc:
            return jsonify({'error': f'Validation failed: {exc}'}), 500

    return bp
