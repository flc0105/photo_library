"""Compatibility auth helpers for the original Gallery application.

The old project imported these helpers from a file that was not included in the
source bundle.  This implementation preserves the public function names and
uses Flask's bundled itsdangerous package for signed, expiring tokens.
"""
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def _secret():
    env = os.environ.get('GALLERY_AUTH_SECRET')
    if env:
        return env
    p = Path('.gallery_auth_secret')
    if p.exists():
        return p.read_text(encoding='utf-8').strip()
    value = secrets.token_urlsafe(48)
    p.write_text(value, encoding='utf-8')
    return value


_serializer = URLSafeTimedSerializer(_secret(), salt='gallery-auth-v1')
album_token_expire_minutes = int(os.environ.get('GALLERY_ALBUM_TOKEN_MINUTES', '1440'))
admin_token_expire_seconds = int(os.environ.get('GALLERY_ADMIN_TOKEN_SECONDS', str(7 * 24 * 3600)))


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


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = request.headers.get('X-Admin-Token')
        if not token or not verify_admin_token(token):
            return jsonify({'error': '需要管理员权限'}), 401
        return func(*args, **kwargs)
    return wrapper
