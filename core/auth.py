"""Authentication helpers for Photo Library signed access tokens."""
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import jsonify, request, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def _secret():
    env = os.environ.get('PHOTO_LIBRARY_AUTH_SECRET')
    if env:
        return env
    p = Path('.photo_library_auth_secret')
    if p.exists():
        return p.read_text(encoding='utf-8').strip()
    value = secrets.token_urlsafe(48)
    p.write_text(value, encoding='utf-8')
    return value


_serializer = URLSafeTimedSerializer(_secret(), salt='photo-library-auth')
album_token_expire_minutes = int(os.environ.get('PHOTO_LIBRARY_ALBUM_TOKEN_MINUTES', '1440'))
admin_token_expire_seconds = int(os.environ.get('PHOTO_LIBRARY_ADMIN_TOKEN_SECONDS', str(7 * 24 * 3600)))


def generate_auth_token(album_id):
    return _serializer.dumps({'kind': 'album', 'album_id': int(album_id)})


def verify_auth_token(token, album_id):
    try:
        data = _serializer.loads(token, max_age=album_token_expire_minutes * 60)
        return data.get('kind') == 'album' and int(data.get('album_id')) == int(album_id)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False


def generate_admin_token():
    return _serializer.dumps({'kind': 'admin'})


def verify_admin_token(token):
    try:
        data = _serializer.loads(token, max_age=admin_token_expire_seconds)
        return data.get('kind') == 'admin'
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False


def is_admin_request():
    """Return True for either the same-origin admin session or a valid admin token."""
    if session.get('photo_library_admin') is True:
        return True
    token = request.headers.get('X-Admin-Token')
    return bool(token and verify_admin_token(token))


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not is_admin_request():
            return jsonify({'error': 'Admin access required.'}), 401
        return func(*args, **kwargs)
    return wrapper


def load_or_create_session_secret():
    secret_file = Path('.photo_library_session_secret')
    env_secret = os.environ.get('PHOTO_LIBRARY_SESSION_SECRET')
    if env_secret:
        return env_secret
    if secret_file.exists():
        return secret_file.read_text(encoding='utf-8').strip()
    secret = secrets.token_urlsafe(48)
    secret_file.write_text(secret, encoding='utf-8')
    return secret
