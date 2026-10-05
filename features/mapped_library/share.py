import secrets

from flask import Blueprint, jsonify, request, send_file, session

from core.database import get_db_connection
from core.image import get_image_exif_simple
from core.manifest import read_manifest
from features.mapped_library.browser import (
    LIBRARY_IMAGE_EXTENSIONS, directory_content_counts, get_library_source,
    is_set_folder_name, library_admin_guard, library_image_info,
    list_library_directory, make_library_variant, normalize_relative_path,
    resolve_library_path, set_library_favorite,
)

bp = Blueprint('mapped_library_share', __name__)

def _share_row(token):
    conn = get_db_connection()
    row = conn.execute('''
        SELECT s.*, ls.name AS source_name, ls.root_path, ls.enabled AS source_enabled
        FROM library_shares s
        JOIN library_sources ls ON ls.id = s.source_id
        WHERE s.token = ?
    ''', (token,)).fetchone()
    conn.close()
    return row


def _share_is_authorized(share):
    if not share or not share['source_enabled']:
        return False
    if not share['password_hash']:
        return True
    unlocked = session.get('library_share_unlocked', [])
    return share['token'] in unlocked


def _share_source_dict(share):
    return {'id': share['source_id'], 'name': share['source_name'], 'root_path': share['root_path']}


def _share_contains_path(share, relative_path):
    base = normalize_relative_path(share['relative_path'])
    requested = normalize_relative_path(relative_path)
    return requested == base or bool(base and requested.startswith(base + '/'))


def _shared_directory_listing(share, requested_path=None):
    """Return one browsable directory inside a Set share without leaking Source paths."""
    source = _share_source_dict(share)
    base = normalize_relative_path(share['relative_path'])
    requested = base if requested_path in (None, '') else normalize_relative_path(requested_path)
    if not _share_contains_path(share, requested):
        raise ValueError('目录不属于该分享 Set')

    listing = list_library_directory(source, requested)
    parent = listing.get('parent_path')
    if requested == base:
        parent = None
    elif parent and not _share_contains_path(share, parent):
        parent = base

    # Public payload deliberately omits Source root_path. Item paths stay
    # Source-relative because the asset endpoints already validate them against
    # the shared Set boundary.
    return {
        'path': listing.get('path') or base,
        'parent_path': parent,
        'name': listing.get('name') or share['title'],
        'is_share_root': requested == base,
        'items': listing.get('items') or [],
        'stats': listing.get('stats') or {},
    }


def _share_manifest_data(share):
    source = _share_source_dict(share)
    _, set_dir, _ = resolve_library_path(source, share['relative_path'])
    manifest = read_manifest(set_dir)
    if manifest and manifest.get('valid'):
        return manifest.get('data')
    return None


@bp.route('/api/library/shares', methods=['POST'])
def create_library_share():
    denied = library_admin_guard()
    if denied:
        return denied
    from werkzeug.security import generate_password_hash
    data = request.get_json(silent=True) or {}
    source = get_library_source(data.get('source_id'))
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        _, target, rel = resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_dir() or not is_set_folder_name(target.name):
            return jsonify({'error': '只能分享完整 Set'}), 400
        counts = directory_content_counts(target)
        if counts.get('image_count', 0) <= 0:
            return jsonify({'error': '当前 Set 没有可分享的图片'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    token = secrets.token_urlsafe(24)
    password = (data.get('password') or '').strip()
    password_hash = generate_password_hash(password) if password else None
    title = (data.get('title') or target.name).strip()
    allow_select = 1 if data.get('allow_select', True) else 0
    conn = get_db_connection()
    cur = conn.execute('''
        INSERT INTO library_shares (token, source_id, relative_path, title, password_hash, allow_select)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (token, source['id'], rel, title, password_hash, allow_select))
    conn.commit()
    share_id = cur.lastrowid
    conn.close()
    return jsonify({
        'id': share_id,
        'token': token,
        'url': f'{request.host_url.rstrip("/")}/?share={token}',
        'has_password': bool(password_hash),
        'allow_select': bool(allow_select)
    }), 201


@bp.route('/api/library/shares/<token>/unlock', methods=['POST'])
def unlock_library_share(token):
    from werkzeug.security import check_password_hash
    share = _share_row(token)
    if not share or not share['source_enabled']:
        return jsonify({'error': '分享不存在'}), 404
    if not share['password_hash']:
        return jsonify({'success': True})
    data = request.get_json(silent=True) or {}
    if not check_password_hash(share['password_hash'], data.get('password') or ''):
        return jsonify({'error': '密码错误'}), 401
    unlocked = list(session.get('library_share_unlocked', []))
    if token not in unlocked:
        unlocked.append(token)
    session['library_share_unlocked'] = unlocked[-20:]
    session.modified = True
    return jsonify({'success': True})


@bp.route('/api/library/shares/<token>', methods=['GET'])
def get_library_share(token):
    share = _share_row(token)
    if not share or not share['source_enabled']:
        return jsonify({'error': '分享不存在'}), 404
    if share['password_hash'] and not _share_is_authorized(share):
        return jsonify({'error': '需要密码', 'needs_password': True, 'title': share['title']}), 401
    try:
        listing = _shared_directory_listing(share)
        manifest_data = _share_manifest_data(share)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404
    return jsonify({
        'token': token,
        'title': share['title'],
        'allow_select': bool(share['allow_select']),
        'has_password': bool(share['password_hash']),
        'listing': listing,
        'manifest': manifest_data,
    })


@bp.route('/api/library/shares/<token>/browse', methods=['GET'])
def browse_library_share(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    try:
        listing = _shared_directory_listing(share, request.args.get('path', ''))
        return jsonify({
            'token': token,
            'title': share['title'],
            'allow_select': bool(share['allow_select']),
            'has_password': bool(share['password_hash']),
            'listing': listing,
            'manifest': _share_manifest_data(share),
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/shares/<token>/asset', methods=['GET'])
def get_library_share_asset(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    source = _share_source_dict(share)
    requested = request.args.get('path', '')
    try:
        requested_rel = normalize_relative_path(requested)
        if not _share_contains_path(share, requested_rel):
            raise ValueError('资源不属于该分享 Set')
        file_path = make_library_variant(source, requested_rel, request.args.get('variant', 'compressed'))
        return send_file(file_path)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/shares/<token>/image-info', methods=['GET'])
def get_library_share_image_info(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    source = _share_source_dict(share)
    try:
        requested_rel = normalize_relative_path(request.args.get('path', ''))
        if not _share_contains_path(share, requested_rel):
            raise ValueError('图片不属于该分享 Set')
        info = library_image_info(source, requested_rel)
        info['source_type'] = 'library-share'
        info['share_token'] = token
        info['id'] = f"share:{token}:{requested_rel}"
        return jsonify(info)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/shares/<token>/exif', methods=['GET'])
def get_library_share_exif(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    source = _share_source_dict(share)
    try:
        requested_rel = normalize_relative_path(request.args.get('path', ''))
        if not _share_contains_path(share, requested_rel):
            raise ValueError('图片不属于该分享 Set')
        _, target, _ = resolve_library_path(source, requested_rel)
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('图片不存在')
        return jsonify({'exif': get_image_exif_simple(str(target))})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.route('/api/library/shares/<token>/selection', methods=['POST'])
def toggle_library_share_selection(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    if not share['allow_select']:
        return jsonify({'error': '此分享不允许选片'}), 403
    data = request.get_json(silent=True) or {}
    try:
        requested_rel = normalize_relative_path(data.get('relative_path', ''))
        if not _share_contains_path(share, requested_rel):
            raise ValueError('图片不属于该分享 Set')
        source = _share_source_dict(share)
        _, target, _ = resolve_library_path(source, requested_rel)
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise ValueError('图片不存在')
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    selected = set_library_favorite(share['source_id'], requested_rel)
    return jsonify({'selected': selected, 'is_favorited': selected})


