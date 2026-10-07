"""Removable local utility for comparing two arbitrary folders by filename stem."""
from collections import defaultdict
from io import BytesIO
from pathlib import Path
import platform
import subprocess

from PIL import Image, ImageOps, UnidentifiedImageError
from send2trash import send2trash



_PREVIEW_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif', '.psd'}
_IGNORED_NAMES = {'.DS_Store'}


def _folder_files(folder: Path):
    """Return top-level regular files, excluding macOS metadata noise."""
    files = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.name in _IGNORED_NAMES or path.name.startswith('._'):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


def _stem_map(folder: Path):
    groups = defaultdict(list)
    for path in _folder_files(folder):
        groups[path.stem.casefold()].append(path.name)
    return {
        stem: sorted(names, key=str.casefold)
        for stem, names in groups.items()
    }


def _resolved_directory(value):
    text = str(value or '').strip()
    if not text:
        raise ValueError('Select a folder')
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f'Folder not found: {path}')
    return path


def _resolved_preview_file(folder_path, filename):
    """Resolve one top-level image for preview without allowing path traversal."""
    folder = _resolved_directory(folder_path)
    name = str(filename or '').strip()
    if not name or Path(name).name != name:
        raise ValueError('Invalid preview filename')
    if name in _IGNORED_NAMES or name.startswith('._'):
        raise ValueError('File is not part of this comparison')

    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise ValueError('Preview file changed or is missing')
    if path.suffix.casefold() not in _PREVIEW_EXTENSIONS:
        raise ValueError('Unsupported preview format')
    return path


def render_image_preview(folder_path, filename, max_edge=1400):
    """Render an in-memory preview; never modify the source file."""
    path = _resolved_preview_file(folder_path, filename)
    with Image.open(path) as image:
        icc_profile = image.info.get('icc_profile')
        if getattr(image, 'is_animated', False):
            image.seek(0)
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        output = BytesIO()
        has_alpha = image.mode in {'RGBA', 'LA'} or 'transparency' in image.info
        if has_alpha:
            image.convert('RGBA').save(output, format='PNG', icc_profile=icc_profile)
            mimetype = 'image/png'
        else:
            image.convert('RGB').save(
                output,
                format='JPEG',
                quality=92,
                subsampling=0,
                progressive=False,
                optimize=True,
                icc_profile=icc_profile,
            )
            mimetype = 'image/jpeg'
        output.seek(0)
        return output, mimetype


def compare_folders(left_path, right_path):
    left = _resolved_directory(left_path)
    right = _resolved_directory(right_path)
    left_map = _stem_map(left)
    right_map = _stem_map(right)

    left_stems = set(left_map)
    right_stems = set(right_map)
    only_left = sorted(left_stems - right_stems)
    only_right = sorted(right_stems - left_stems)
    matched = left_stems & right_stems

    def rows(stems, groups):
        return [
            {
                'stem': stem,
                'files': groups[stem],
            }
            for stem in stems
        ]

    return {
        'left_path': str(left),
        'right_path': str(right),
        'summary': {
            'left_file_count': sum(len(names) for names in left_map.values()),
            'right_file_count': sum(len(names) for names in right_map.values()),
            'left_stem_count': len(left_map),
            'right_stem_count': len(right_map),
            'matched_stem_count': len(matched),
            'only_left_count': len(only_left),
            'only_right_count': len(only_right),
        },
        'only_left': rows(only_left, left_map),
        'only_right': rows(only_right, right_map),
    }


def choose_directory(prompt='Select Folder'):
    """Open a native folder picker on the machine running Photo Library."""
    system = platform.system()
    if system == 'Darwin':
        script = 'POSIX path of (choose folder with prompt ' + _apple_script_string(prompt) + ')'
        completed = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or '').strip()
            # AppleScript returns -128 when the user cancels the picker.
            if '(-128)' in stderr or 'User canceled' in stderr:
                return None
            raise RuntimeError(stderr or 'Cannot open folder picker')
        selected = (completed.stdout or '').strip()
        return str(_resolved_directory(selected)) if selected else None

    # Keep the uncommon non-macOS fallback isolated here instead of adding a
    # GUI dependency to the main application.
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError('No native folder picker available') from exc

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(title=prompt, mustexist=True)
    finally:
        root.destroy()
    return str(_resolved_directory(selected)) if selected else None


def _apple_script_string(value):
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _difference_rows(left_path, right_path, side):
    result = compare_folders(left_path, right_path)
    key = 'only_left' if side == 'left' else 'only_right'
    return result, result[key]


def _normalize_expected_rows(rows):
    normalized = []
    for row in rows or []:
        stem = str(row.get('stem') or '').strip().casefold()
        files = sorted(
            [str(name) for name in (row.get('files') or []) if str(name)],
            key=str.casefold,
        )
        if stem:
            normalized.append({'stem': stem, 'files': files})
    return sorted(normalized, key=lambda item: item['stem'])


def trash_extra_files(left_path, right_path, side, expected_rows):
    """Move the already-previewed side-only files to the system Trash."""
    if side not in {'left', 'right'}:
        raise ValueError('Invalid folder side')

    result, current_rows = _difference_rows(left_path, right_path, side)
    if _normalize_expected_rows(current_rows) != _normalize_expected_rows(expected_rows):
        raise ValueError('Folder contents changed. Compare again before deleting.')

    root = Path(result['left_path'] if side == 'left' else result['right_path'])
    targets = []
    for row in current_rows:
        for name in row['files']:
            path = root / name
            if not path.is_file() or path.parent != root:
                raise ValueError(f'File changed. Compare again: {name}')
            targets.append(path)

    moved = []
    for path in targets:
        send2trash(str(path))
        moved.append(path.name)

    return {
        'side': side,
        'folder': str(root),
        'moved_count': len(moved),
        'moved_files': moved,
    }


def create_folder_compare_blueprint(admin_guard):
    from flask import Blueprint, jsonify, request, send_file

    bp = Blueprint('folder_compare', __name__)

    @bp.route('/api/tools/folder-compare/pick', methods=['POST'])
    def pick_directory():
        denied = admin_guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        prompt = (data.get('prompt') or 'Select Folder').strip() or 'Select Folder'
        try:
            path = choose_directory(prompt)
            return jsonify({'path': path, 'cancelled': path is None})
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/tools/folder-compare/preview', methods=['GET'])
    def preview():
        denied = admin_guard()
        if denied:
            return denied
        try:
            data, mimetype = render_image_preview(
                request.args.get('folder'),
                request.args.get('name'),
            )
            response = send_file(data, mimetype=mimetype, max_age=0)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/tools/folder-compare/trash-extra', methods=['POST'])
    def trash_extra():
        denied = admin_guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        try:
            result = trash_extra_files(
                data.get('left_path'),
                data.get('right_path'),
                data.get('side'),
                data.get('expected_rows') or [],
            )
            return jsonify(result)
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/tools/folder-compare/compare', methods=['POST'])
    def compare():
        denied = admin_guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(compare_folders(data.get('left_path'), data.get('right_path')))
        except (OSError, ValueError) as exc:
            return jsonify({'error': str(exc)}), 400

    return bp
