import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from flask import Blueprint, current_app, jsonify, request, send_file

from core.auth import is_admin_request
from core.database import get_db_connection
from core.filesystem import LIBRARY_CACHE_FOLDER
from core.image import generate_compressed, generate_thumbnail, get_image_exif_simple
from core.manifest import MANIFEST_FILENAME, _order_manifest_keys as order_manifest_keys, read_manifest
from core.settings import site_config_enabled
from features.mapped_library.autofill import get_original_jpg_time_range

bp = Blueprint('mapped_library', __name__)

LIBRARY_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
LIBRARY_COVER_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
SET_FOLDER_RE = re.compile(r'^\d{8}-.+-.+$')


def _is_admin_request():
    return is_admin_request()


def _library_admin_guard():
    if not _is_admin_request():
        return jsonify({'error': 'Admin access required.'}), 401
    return None


def _get_library_source(source_id, include_disabled=False):
    conn = get_db_connection()
    if include_disabled:
        row = conn.execute('SELECT * FROM library_sources WHERE id = ?', (source_id,)).fetchone()
    else:
        row = conn.execute('SELECT * FROM library_sources WHERE id = ? AND enabled = 1', (source_id,)).fetchone()
    conn.close()
    return row


def _normalize_relative_path(value):
    value = (value or '').replace('\\', '/').strip('/')
    if value in ('', '.'):
        return ''
    parts = [part for part in value.split('/') if part not in ('', '.')]
    if any(part == '..' for part in parts):
        raise ValueError('Invalid path.')
    return '/'.join(parts)


def _resolve_library_path(source, relative_path='', require_exists=True):
    root = Path(source['root_path']).expanduser().resolve()
    rel = _normalize_relative_path(relative_path)
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError('Path is outside the Source root.')
    if _is_library_deleted_path(root, target):
        raise FileNotFoundError('Deleted is only available from the local filesystem.')
    if require_exists and not target.exists():
        raise FileNotFoundError('Path not found.')
    return root, target, rel


def _find_library_set_root(root, target):
    """Return the outermost Set ancestor for one mapped path.

    Intermediates intentionally mirrors the Set name, so choosing the nearest
    matching folder can point at Intermediates/<Set>.  The canonical Set root is
    the outermost matching ancestor that still lives below the mapped Source.
    """
    root = Path(root).resolve()
    target = Path(target).resolve()
    current = target if target.is_dir() else target.parent
    matches = []

    while True:
        try:
            current.relative_to(root)
        except ValueError:
            break
        if _is_set_folder_name(current.name):
            matches.append(current)
        if current == root:
            break
        current = current.parent

    return matches[-1] if matches else None


def _is_library_deleted_path(root, target):
    """Hide every Set/Deleted subtree from all web Library endpoints."""
    set_root = _find_library_set_root(root, target)
    if set_root is None:
        return False
    try:
        relative = Path(target).resolve().relative_to(set_root)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0].casefold() == 'deleted')


def _soft_delete_library_image(source, relative_path):
    """Move one mapped image to Set/Deleted while preserving its stage path.

    This is the only web deletion path for Library images.  The first deletion
    keeps the original filename.  If that exact Deleted path already exists, a
    deletion timestamp is appended so repeated soft deletes never overwrite an
    earlier archived copy.  Deleted itself remains inaccessible through the
    Library API so permanent removal remains a local-only operation.
    """
    root, target, rel = _resolve_library_path(source, relative_path)
    if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
        raise FileNotFoundError('Photo not found or unsupported.')

    set_root = _find_library_set_root(root, target)
    if set_root is None:
        raise ValueError('Only photos inside a Set can be moved to Deleted.')

    source_relative_to_set = target.relative_to(set_root)
    if not source_relative_to_set.parts or source_relative_to_set.parts[0].casefold() == 'deleted':
        raise ValueError('Files in Deleted cannot be removed from the web UI.')

    destination = set_root / 'Deleted' / source_relative_to_set
    destination.parent.mkdir(parents=True, exist_ok=True)

    reserved = False
    try:
        candidate = destination
        collision_index = 0
        while True:
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                destination = candidate
                reserved = True
                break
            except FileExistsError:
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                collision_index += 1
                suffix = '' if collision_index == 1 else f'_{collision_index}'
                candidate = destination.with_name(
                    f'{destination.stem}__deleted_{timestamp}{suffix}{destination.suffix}'
                )

        os.replace(target, destination)
        reserved = False
    except Exception:
        if reserved and destination.exists() and target.exists():
            try:
                destination.unlink()
            except OSError:
                pass
        raise

    # A new file restored later to the same source path must not inherit stale
    # favorite/description state from the soft-deleted file.  File movement is
    # already complete, so DB cleanup is deliberately best-effort.
    try:
        conn = get_db_connection()
        conn.execute(
            'DELETE FROM library_image_states WHERE source_id=? AND relative_path=?',
            (source['id'], rel)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        current_app.logger.warning('soft delete succeeded but library state cleanup failed: %s', exc)

    return {
        'relative_path': rel,
        'deleted_relative_path': destination.relative_to(root).as_posix(),
    }


def _find_nearest_manifest(root, target):
    current = target if target.is_dir() else target.parent
    while True:
        result = read_manifest(current)
        if result is not None:
            result['relative_path'] = '' if current == root else current.relative_to(root).as_posix()
            return result
        if current == root:
            break
        current = current.parent
    return {'exists': False, 'valid': False, 'data': None, 'error': None, 'relative_path': None}


def _is_set_folder_name(name):
    return bool(SET_FOLDER_RE.fullmatch(name or ''))


def _parse_set_folder_name(name):
    parts = name.split('-', 2)
    date = ''
    model = ''
    theme = ''
    if len(parts) >= 1 and len(parts[0]) == 8 and parts[0].isdigit():
        date = f'{parts[0][0:4]}-{parts[0][4:6]}-{parts[0][6:8]}'
    if len(parts) >= 2:
        model = parts[1]
    if len(parts) >= 3:
        theme = parts[2]
    return date, model, theme


def _suggest_manifest(target, include_times=False):
    """Return a clean manifest template.

    Folder browsing must stay cheap, so EXIF time scanning is opt-in and is
    performed only when the manifest editor explicitly asks for it.
    """
    date, model, theme = _parse_set_folder_name(target.name)
    times = get_original_jpg_time_range(target) if include_times else {'start_time': '', 'end_time': ''}
    return {
        'model': model,
        'shoot': {
            'date': date,
            'start_time': times['start_time'],
            'end_time': times['end_time'],
            'environment': '',
            'scene': '',
            'weather': ''
        },
        'location': {
            'name': '',
            'address': '',
            'lat': None,
            'lng': None
        },
        'theme': {
            'name': theme,
            'genre': 'cosplay',
            'source_title': '',
            'source_type': '',
            'character': '',
            'variant': '',
            'reference_type': '',
            'reference': ''
        },
        'production': {
            'collaboration_type': '',
            'lead_photographer': True,
            'model_fee': 0,
            'venue_fee': None,
            'venue_fee_payer': ''
        },
        'props': {
            'subject': [],
            'set': []
        },
        'lighting': []
    }


def _validate_new_set_text(value, label, allow_hyphen=True):
    text = str(value or '').strip()
    if not text:
        raise ValueError(f'{label} required.')
    if text in {'.', '..'} or any(char in text for char in ('/', '\\', '\x00', '\n', '\r')):
        raise ValueError(f'{label} contains invalid path characters.')
    if not allow_hyphen and '-' in text:
        raise ValueError(f'{label} cannot contain hyphens.')
    return text


def _new_set_manifest(target):
    """Create the canonical blank manifest used by New Set.

    Only model, shoot.date and theme.name are derived from the Set folder name.
    The remaining descriptive fields stay blank/default so directory creation
    never invents metadata that the user has not entered yet.
    """
    manifest = _suggest_manifest(target, include_times=False)
    manifest['theme']['genre'] = ''
    manifest['production']['venue_fee'] = 0
    return order_manifest_keys(manifest)


def _create_new_set(root, date_text, model, theme):
    """Create one complete Set atomically below a Source root."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError('Source root not found.')
    if _is_set_folder_name(root.name):
        raise ValueError('New Set is only available at the Set root.')

    try:
        shoot_date = datetime.strptime(str(date_text or '').strip(), '%Y-%m-%d').date()
    except ValueError as exc:
        raise ValueError('Date must use YYYY-MM-DD.') from exc

    # The first hyphen separates date/model in the canonical Set name, so a
    # model containing '-' would make the existing Set parser ambiguous.
    model = _validate_new_set_text(model, 'Model', allow_hyphen=False)
    theme = _validate_new_set_text(theme, 'Theme', allow_hyphen=True)
    set_name = f'{shoot_date:%Y%m%d}-{model}-{theme}'
    set_path = root / set_name

    if set_path.exists():
        raise FileExistsError(f'Set already exists: {set_name}')

    set_path.mkdir()
    try:
        directories = [
            set_path / '01_Original' / 'JPG',
            set_path / '01_Original' / 'RAW',
            set_path / '02_Base_Edit',
            set_path / '03_Model_Edit',
            set_path / '04_Revision',
            set_path / '05_Final',
        ]

        # Intermediates is intentionally temporary and mirrors the Set name so
        # the whole subtree can later be moved outside Completed without losing
        # which Set it belongs to.
        intermediate_root = set_path / 'Intermediates' / set_name
        directories.extend([
            # RAW development: ACR / Canon DPP / similar tools, including
            # profile application, basic color work and RAW -> TIFF/PNG output.
            intermediate_root / '01_Develop',
            intermediate_root / '02_PixCake',
            intermediate_root / '03_PSD' / 'Base_Edit',
            intermediate_root / '03_PSD' / 'Revision',
            intermediate_root / '90_Discards' / 'Base_Edit',
            intermediate_root / '90_Discards' / 'Model_Edit',
            intermediate_root / '90_Discards' / 'Revision',
        ])

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=False)

        manifest = _new_set_manifest(set_path)
        manifest_path = set_path / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8'
        )
    except Exception:
        # A New Set is one unit. Never leave a half-created directory tree.
        shutil.rmtree(set_path, ignore_errors=True)
        raise

    return {
        'name': set_name,
        'path': set_name,
        'manifest': manifest,
    }


def _collect_manifest_array(root):
    """Read every descendant manifest.json and return one date-sorted array.

    The operation is read-only and all-or-nothing: a malformed manifest stops
    the collection so the copied array can never silently omit a Set.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError('Source root not found.')

    manifest_paths = []
    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        dir_names[:] = [name for name in dir_names if not name.startswith('.')]
        if MANIFEST_FILENAME in file_names:
            manifest_paths.append(Path(current_root) / MANIFEST_FILENAME)

    manifest_paths.sort(key=lambda path: path.relative_to(root).as_posix().casefold())
    manifests = []
    errors = []
    for path in manifest_paths:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Manifest root must be a JSON object.')
            manifests.append((path, data))
        except Exception as exc:
            errors.append({
                'path': path.relative_to(root).as_posix(),
                'error': str(exc),
            })

    if errors:
        error = ValueError('Invalid manifest.json found.')
        error.manifest_errors = errors
        raise error

    def sort_key(item):
        path, data = item
        shoot = data.get('shoot') if isinstance(data.get('shoot'), dict) else {}
        date_text = str(shoot.get('date') or '').strip()
        start_text = str(shoot.get('start_time') or '').strip()
        try:
            date_value = datetime.strptime(date_text, '%Y-%m-%d').date()
        except ValueError:
            date_value = datetime.max.date()
        try:
            start_value = datetime.strptime(start_text, '%H:%M').time()
        except ValueError:
            start_value = datetime.max.time()
        return (
            date_value,
            start_value,
            path.relative_to(root).as_posix().casefold(),
        )

    manifests.sort(key=sort_key)
    return [data for _, data in manifests]


def _remove_source_dotfiles(root):
    """Recursively remove macOS AppleDouble files and .DS_Store only.

    Other dotfiles are never touched, and directory symlinks are not followed.
    Individual failures are reported without preventing other junk files from
    being cleaned.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError('Source root not found.')

    removed_count = 0
    failures = []
    for current_root, _, file_names in os.walk(root, followlinks=False):
        current_root = Path(current_root)
        for file_name in file_names:
            if file_name != '.DS_Store' and not file_name.startswith('._'):
                continue
            file_path = current_root / file_name
            try:
                file_path.unlink()
                removed_count += 1
            except OSError as exc:
                failures.append({
                    'path': file_path.relative_to(root).as_posix(),
                    'error': str(exc),
                })

    return {
        'removed_count': removed_count,
        'failed_count': len(failures),
        'failures': failures[:50],
    }


def _library_state_map(source_id, relative_paths):
    paths = [p for p in relative_paths if p]
    if not paths:
        return {}
    placeholders = ','.join('?' for _ in paths)
    conn = get_db_connection()
    rows = conn.execute(
        f'''SELECT relative_path, is_favorited, description
            FROM library_image_states
            WHERE source_id = ? AND relative_path IN ({placeholders})''',
        [source_id, *paths]
    ).fetchall()
    conn.close()
    return {
        row['relative_path']: {
            'is_favorited': bool(row['is_favorited']),
            'description': row['description'] or ''
        }
        for row in rows
    }


def _get_library_image_state(source_id, relative_path):
    conn = get_db_connection()
    row = conn.execute(
        'SELECT is_favorited, description FROM library_image_states WHERE source_id=? AND relative_path=?',
        (source_id, relative_path)
    ).fetchone()
    conn.close()
    return {
        'is_favorited': bool(row['is_favorited']) if row else False,
        'description': (row['description'] or '') if row else ''
    }


def _set_library_favorite(source_id, relative_path, value=None):
    conn = get_db_connection()
    row = conn.execute(
        'SELECT id, is_favorited FROM library_image_states WHERE source_id=? AND relative_path=?',
        (source_id, relative_path)
    ).fetchone()
    current = bool(row['is_favorited']) if row else False
    new_value = (not current) if value is None else bool(value)
    if row:
        conn.execute(
            'UPDATE library_image_states SET is_favorited=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (1 if new_value else 0, row['id'])
        )
    else:
        conn.execute(
            'INSERT INTO library_image_states (source_id, relative_path, is_favorited) VALUES (?, ?, ?)',
            (source_id, relative_path, 1 if new_value else 0)
        )
    conn.commit()
    conn.close()
    return new_value


def _set_library_description(source_id, relative_path, description):
    conn = get_db_connection()
    row = conn.execute(
        'SELECT id FROM library_image_states WHERE source_id=? AND relative_path=?',
        (source_id, relative_path)
    ).fetchone()
    if row:
        conn.execute(
            'UPDATE library_image_states SET description=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (description, row['id'])
        )
    else:
        conn.execute(
            'INSERT INTO library_image_states (source_id, relative_path, description) VALUES (?, ?, ?)',
            (source_id, relative_path, description)
        )
    conn.commit()
    conn.close()


def _library_image_info(source, relative_path):
    _, target, rel = _resolve_library_path(source, relative_path)
    if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
        raise FileNotFoundError('Photo not found or unsupported.')
    stat = target.stat()
    width = height = None
    try:
        with Image.open(target) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
    except Exception:
        pass
    state = _get_library_image_state(source['id'], rel)
    return {
        'source_type': 'library',
        'source_id': source['id'],
        'id': f"library:{source['id']}:{rel}",
        'relative_path': rel,
        'original_filename': target.name,
        'file_size': stat.st_size,
        'width': width,
        'height': height,
        'uploaded_at': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
        'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
        'is_favorited': state['is_favorited'],
        'description': state['description']
    }


def _natural_cover_sort_key(path, base):
    """Natural A-Z / 1-9 sort for deterministic Set cover selection."""
    path = Path(path)
    base = Path(base)

    def split_key(value):
        return tuple(
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r'(\d+)', value)
        )

    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = path
    return (split_key(path.name), tuple(split_key(part) for part in rel.parts))


def _first_cover_image(directory, recursive=False):
    """Return the first JPG/PNG using natural filename order."""
    directory = Path(directory)
    try:
        if recursive:
            candidates = [
                path for path in directory.rglob('*')
                if path.is_file()
                and not any(part.startswith('.') for part in path.relative_to(directory).parts)
                and path.suffix.lower() in LIBRARY_COVER_EXTENSIONS
            ]
        else:
            candidates = [
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in LIBRARY_COVER_EXTENSIONS
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda path: _natural_cover_sort_key(path, directory))
    except OSError:
        return None


def _directory_cover_path(root, directory, direct_manifest=None):
    """Return a cover only for a Set card; every other mapped folder stays icon-only.

    An explicit manifest cover remains authoritative unless it points into
    01_Original. Otherwise Set covers follow 04_Revision -> 03_Model_Edit ->
    02_Base_Edit and use the first direct JPG/PNG in natural filename order
    (A-Z, 1-9). If those edited stages contain no image, the Set has no cover.
    """
    root = Path(root).resolve()
    directory = Path(directory).resolve()
    if not _is_set_folder_name(directory.name):
        return None

    candidates = []
    if direct_manifest and direct_manifest.get('valid'):
        cover = direct_manifest.get('data', {}).get('cover')
        if isinstance(cover, str) and cover.strip():
            try:
                relative_cover = _normalize_relative_path(cover)
                if not Path(relative_cover).parts or Path(relative_cover).parts[0] != '01_Original':
                    candidates.append(directory / relative_cover)
            except ValueError:
                pass

    if not candidates:
        for stage_name in ('04_Revision', '03_Model_Edit', '02_Base_Edit'):
            stage = directory / stage_name
            if not stage.is_dir():
                continue
            candidate = _first_cover_image(stage, recursive=False)
            if candidate is not None:
                candidates.append(candidate)
                break

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            if resolved.is_file() and resolved.suffix.lower() in LIBRARY_COVER_EXTENSIONS:
                return resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
    return None


def _directory_content_counts(directory):
    """Return recursive, metadata-only counts for a folder card.

    This deliberately never opens/decodes image files. Hidden/control files are
    ignored so the numbers describe the actual photo tree rather than Photo Library
    bookkeeping. Symlinked directories are not traversed.
    """
    counts = {
        'directory_count': 0,
        'image_count': 0,
        'file_count': 0
    }
    ignored_names = {MANIFEST_FILENAME, MANIFEST_FILENAME + '.bak', MANIFEST_FILENAME + '.tmp'}
    stack = [Path(directory)]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name.startswith('.') or entry.name in ignored_names:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            entry_path = Path(entry.path)
                            if entry.name.casefold() == 'deleted' and _is_set_folder_name(entry_path.parent.name):
                                continue
                            counts['directory_count'] += 1
                            stack.append(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            counts['file_count'] += 1
                            if Path(entry.name).suffix.lower() in LIBRARY_IMAGE_EXTENSIONS:
                                counts['image_count'] += 1
                    except OSError:
                        continue
        except OSError:
            continue

    return counts


def _list_library_directory(source, relative_path=''):
    root, target, rel = _resolve_library_path(source, relative_path)
    if not target.is_dir():
        raise NotADirectoryError('Target is not a directory.')

    # When disabled, skip cover discovery entirely.  This both avoids filesystem
    # scanning and ensures the frontend never receives a thumbnail URL to request.
    show_folder_covers = site_config_enabled('show_library_folder_covers', True)

    items = []
    stats = {
        'directory_count': 0,
        'image_count': 0,
        'unsupported_file_count': 0,
        'total_file_count': 0
    }

    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        if child.name.startswith('.') or child.name in {MANIFEST_FILENAME, MANIFEST_FILENAME + '.bak', MANIFEST_FILENAME + '.tmp'}:
            continue
        if child.is_dir() and child.name.casefold() == 'deleted' and _is_set_folder_name(target.name):
            continue
        child_rel = child.relative_to(root).as_posix()
        try:
            stat = child.stat()
        except OSError:
            continue

        if child.is_dir():
            stats['directory_count'] += 1
            direct_manifest = read_manifest(child)
            content_counts = _directory_content_counts(child)
            items.append({
                'type': 'directory',
                'name': child.name,
                'is_set': _is_set_folder_name(child.name),
                'relative_path': child_rel,
                'has_manifest': direct_manifest is not None,
                'manifest_valid': bool(direct_manifest and direct_manifest.get('valid')),
                'manifest_model': (
                    str((direct_manifest.get('data') or {}).get('model') or '').strip()
                    if direct_manifest and direct_manifest.get('valid') else ''
                ),
                'cover_path': (_directory_cover_path(root, child, direct_manifest) if show_folder_covers else None),
                'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                'directory_count': content_counts['directory_count'],
                'image_count': content_counts['image_count'],
                'file_count': content_counts['file_count']
            })
            continue

        if not child.is_file():
            continue

        stats['total_file_count'] += 1
        suffix = child.suffix.lower()
        if suffix not in LIBRARY_IMAGE_EXTENSIONS:
            # RAW/PSD/etc. are deliberately invisible in Photo Library. Keep only a
            # count so the UI can distinguish an empty directory from one that has
            # files but no displayable images.
            stats['unsupported_file_count'] += 1
            continue

        stats['image_count'] += 1
        items.append({
            'type': 'image',
            'name': child.name,
            'relative_path': child_rel,
            'size': stat.st_size,
            'extension': suffix,
            'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')
        })

    image_paths = [item['relative_path'] for item in items if item['type'] == 'image']
    states = _library_state_map(source['id'], image_paths)
    for item in items:
        if item['type'] == 'image':
            state = states.get(item['relative_path'], {})
            item['is_favorited'] = bool(state.get('is_favorited', False))
            item['description'] = state.get('description', '')

    is_set = _is_set_folder_name(target.name)
    direct_manifest = read_manifest(target)
    if direct_manifest is not None:
        manifest = direct_manifest
        manifest['relative_path'] = rel
    elif is_set:
        # A Set owns its own manifest. Do not inherit an accidental manifest from
        # a parent Source/folder when deciding whether this Set has metadata.
        manifest = {'exists': False, 'valid': False, 'data': None, 'error': None, 'relative_path': None}
    else:
        # Nested stage folders may still resolve the nearest Set manifest for
        # image/share context, but they never expose create/edit controls.
        manifest = _find_nearest_manifest(root, target)

    return {
        'source': {'id': source['id'], 'name': source['name'], 'root_path': source['root_path']},
        'path': rel,
        'parent_path': '/'.join(rel.split('/')[:-1]) if rel else None,
        'name': target.name if rel else source['name'],
        'is_set': is_set,
        'items': items,
        'stats': stats,
        'manifest': manifest,
        'suggested_manifest': _suggest_manifest(target) if is_set and direct_manifest is None else None
    }


def _make_library_variant(source, relative_path, variant='compressed'):
    root, target, rel = _resolve_library_path(source, relative_path)
    if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
        raise FileNotFoundError('Photo not found or unsupported.')
    if variant == 'original':
        return target

    stat = target.stat()
    cache_key = hashlib.sha256(f"{source['id']}|{rel}|{stat.st_mtime_ns}|{stat.st_size}|{variant}|library-exif-orientation".encode()).hexdigest()
    cache_dir = Path(LIBRARY_CACHE_FOLDER) / str(source['id'])
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f'{cache_key}.jpg'
    if cache_path.exists():
        return cache_path

    if variant == 'thumbnail':
        generate_thumbnail(str(target), str(cache_path), size=(360, 360), apply_exif_orientation=True)
    else:
        generate_compressed(str(target), str(cache_path), max_size=2000, apply_exif_orientation=True)
    return cache_path


@bp.route('/api/library/sources', methods=['GET'])
def get_library_sources():
    denied = _library_admin_guard()
    if denied:
        return denied
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM library_sources ORDER BY sort_order, id').fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item['available'] = Path(row['root_path']).expanduser().is_dir()
        result.append(item)
    return jsonify(result)


@bp.route('/api/library/sources', methods=['POST'])
def create_library_source():
    denied = _library_admin_guard()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    root_path = (data.get('root_path') or '').strip()
    if not name or not root_path:
        return jsonify({'error': '名称和目录不能为空'}), 400
    path = Path(root_path).expanduser().resolve()
    if not path.is_dir():
        return jsonify({'error': f'目录不存在: {path}'}), 400
    conn = get_db_connection()
    try:
        cur = conn.execute('INSERT INTO library_sources (name, root_path) VALUES (?, ?)', (name, str(path)))
        conn.commit()
        source_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': '这个目录已经添加过了'}), 409
    conn.close()
    return jsonify({'id': source_id, 'name': name, 'root_path': str(path)}), 201


@bp.route('/api/library/sources/<int:source_id>', methods=['PUT'])
def update_library_source(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    source = conn.execute('SELECT * FROM library_sources WHERE id = ?', (source_id,)).fetchone()
    if not source:
        conn.close()
        return jsonify({'error': 'Source 不存在'}), 404
    name = data.get('name', source['name'])
    root_path = data.get('root_path', source['root_path'])
    enabled = 1 if data.get('enabled', bool(source['enabled'])) else 0
    path = Path(root_path).expanduser().resolve()
    current_path = Path(source['root_path']).expanduser().resolve()
    root_path_changed = path != current_path
    enabling_source = enabled and not bool(source['enabled'])
    # 重命名不依赖目录在线；启用 Source 或修改启用中的映射路径时仍校验目录。
    if enabled and (enabling_source or root_path_changed) and not path.is_dir():
        conn.close()
        return jsonify({'error': f'目录不存在: {path}'}), 400
    try:
        conn.execute('UPDATE library_sources SET name=?, root_path=?, enabled=? WHERE id=?',
                     (name, str(path), enabled, source_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': '这个目录已经被其他 Source 使用'}), 409
    conn.close()
    return jsonify({'success': True})


@bp.route('/api/library/sources/<int:source_id>', methods=['DELETE'])
def delete_library_source(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    conn = get_db_connection()
    conn.execute('DELETE FROM library_image_states WHERE source_id = ?', (source_id,))
    conn.execute('DELETE FROM library_shares WHERE source_id = ?', (source_id,))
    conn.execute('DELETE FROM library_sources WHERE id = ?', (source_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@bp.route('/api/library/sources/<int:source_id>/browse', methods=['GET'])
def browse_library_source(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    try:
        return jsonify(_list_library_directory(source, request.args.get('path', '')))
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/sets', methods=['POST'])
def create_library_set(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404

    data = request.get_json(silent=True) or {}
    try:
        root, _, _ = _resolve_library_path(source, '')
        result = _create_new_set(
            root,
            data.get('date'),
            data.get('model'),
            data.get('theme'),
        )
        return jsonify(result), 201
    except FileExistsError as exc:
        return jsonify({'error': str(exc)}), 409
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        return jsonify({'error': str(exc)}), 400
    except OSError as exc:
        return jsonify({'error': f'Create Set failed: {exc}'}), 500


@bp.route('/api/library/sources/<int:source_id>/manifests', methods=['GET'])
def collect_library_manifests(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    try:
        root, _, _ = _resolve_library_path(source, '')
        manifests = _collect_manifest_array(root)
        payload = {'count': len(manifests), 'manifests': manifests}
        # Use json.dumps directly so hand-maintained manifest key order survives
        # the round-trip into the copyable textarea.
        return current_app.response_class(
            json.dumps(payload, ensure_ascii=False),
            mimetype='application/json'
        )
    except ValueError as exc:
        errors = getattr(exc, 'manifest_errors', None)
        if errors:
            return jsonify({'error': str(exc), 'errors': errors}), 400
        return jsonify({'error': str(exc)}), 400
    except (FileNotFoundError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/remove-dotfiles', methods=['POST'])
def remove_library_dotfiles(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    try:
        root, _, _ = _resolve_library_path(source, '')
        return jsonify(_remove_source_dotfiles(root))
    except (FileNotFoundError, OSError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/asset', methods=['GET'])
def get_library_asset(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    try:
        file_path = _make_library_variant(source, request.args.get('path', ''), request.args.get('variant', 'compressed'))
        return send_file(file_path)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/sources/<int:source_id>/image-info', methods=['GET'])
def get_library_image_info(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    try:
        return jsonify(_library_image_info(source, request.args.get('path', '')))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/sources/<int:source_id>/exif', methods=['GET'])
def get_library_image_exif(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    try:
        _, target, _ = _resolve_library_path(source, request.args.get('path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('Photo not found.')
        return jsonify({'exif': get_image_exif_simple(str(target))})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/sources/<int:source_id>/favorite', methods=['POST'])
def toggle_library_image_favorite(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        _, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('Photo not found.')
        value = data.get('is_favorited') if 'is_favorited' in data else None
        favorited = _set_library_favorite(source_id, rel, value)
        return jsonify({'is_favorited': favorited})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/soft-delete', methods=['POST'])
def soft_delete_library_image(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        result = _soft_delete_library_image(source, data.get('relative_path', ''))
        return jsonify({'success': True, **result})
    except FileExistsError as exc:
        return jsonify({'error': str(exc)}), 409
    except (ValueError, FileNotFoundError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/description', methods=['PUT'])
def update_library_image_description(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        _, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('Photo not found.')
        description = str(data.get('description') or '')
        _set_library_description(source_id, rel, description)
        return jsonify({'description': description})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@bp.route('/api/library/sources/<int:source_id>/rename', methods=['POST'])
def rename_library_image(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source not found or disabled.'}), 404
    data = request.get_json(silent=True) or {}
    new_filename = str(data.get('new_filename') or '').strip()
    if not new_filename or new_filename in {'.', '..'} or Path(new_filename).name != new_filename or '/' in new_filename or '\\' in new_filename:
        return jsonify({'error': 'Invalid filename.'}), 400
    try:
        root, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('Photo not found.')
        destination = target.with_name(new_filename)
        destination.resolve().relative_to(root)
        if destination.exists() and destination != target:
            return jsonify({'error': 'Filename already exists.'}), 409
        old_rel = rel
        new_rel = destination.relative_to(root).as_posix()
        conn = get_db_connection()
        # 目标文件不存在时，若数据库里残留了同名旧状态，可以安全清理。
        conn.execute(
            'DELETE FROM library_image_states WHERE source_id=? AND relative_path=? AND relative_path<>?',
            (source_id, new_rel, old_rel)
        )
        target.rename(destination)
        conn.execute(
            'UPDATE library_image_states SET relative_path=?, updated_at=CURRENT_TIMESTAMP WHERE source_id=? AND relative_path=?',
            (new_rel, source_id, old_rel)
        )
        conn.commit()
        conn.close()
        return jsonify(_library_image_info(source, new_rel))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


# Public module boundary used by sibling feature modules and main assembly.
library_admin_guard = _library_admin_guard
get_library_source = _get_library_source
normalize_relative_path = _normalize_relative_path
resolve_library_path = _resolve_library_path
find_library_set_root = _find_library_set_root
is_set_folder_name = _is_set_folder_name
suggest_manifest = _suggest_manifest
library_image_info = _library_image_info
set_library_favorite = _set_library_favorite
list_library_directory = _list_library_directory
make_library_variant = _make_library_variant
directory_content_counts = _directory_content_counts
