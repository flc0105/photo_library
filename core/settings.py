import hashlib

from flask import Blueprint, jsonify, request, session

from core.auth import generate_admin_token, is_admin_request, verify_admin_token
from core.database import get_db_connection

bp = Blueprint('site_settings', __name__)


@bp.route('/api/albums/title', methods=['PUT'])
def update_site_title():
    data = request.get_json()
    new_title = data.get('title', '').strip()

    if not new_title:
        return jsonify({'error': 'Title required.'}), 400

    # 这里可以将标题保存到数据库或者配置文件中
    # 为了方便，我们可以创建一个配置表，这里简化处理
    # 实际项目中可以创建一个config表来存储站点配置
    try:
        # 保存到数据库config表（需要先创建这个表）
        conn = get_db_connection()

        # 使用UPDATE更新标题
        conn.execute('''
                    UPDATE site_config SET value = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE key = 'site_title'
                ''', (new_title,))

        conn.commit()
        conn.close()

        return jsonify({'message': 'Updated.', 'title': new_title})
    except Exception as e:
        return jsonify({'error': f'Update failed: {str(e)}'}), 500


@bp.route('/api/albums/title', methods=['GET'])
def get_site_title():
    try:
        conn = get_db_connection()

        title_record = conn.execute(
            'SELECT value FROM site_config WHERE key = ?', ('site_title',)
        ).fetchone()

        conn.close()

        default_title = 'Photo Library'
        if title_record and title_record['value']:
            return jsonify({'title': title_record['value']})
        else:
            return jsonify({'title': default_title})
    except Exception as e:
        return jsonify({'title': 'Photo Library'})


@bp.route('/api/admin/verify-password', methods=['POST'])
def verify_admin_password():
    data = request.get_json()
    password = data.get('password')

    if not password:
        return jsonify({'error': 'Password required.'}), 400

    # 这里从数据库获取正确密码（简化示例）
    conn = get_db_connection()

    admin = conn.execute('SELECT * FROM site_config where key="password"').fetchone()
    conn.close()

    if not admin:
        return jsonify({'error': 'Admin not configured.'}), 500

    # 验证密码
    password_hash = hashlib.md5(password.encode()).hexdigest()
    if password_hash == admin['value']:
        # 生成管理员token，并建立同源 session 供本地目录图片直接请求使用。
        token = generate_admin_token()
        session['photo_library_admin'] = True
        session.modified = True
        return jsonify({
            'success': True,
            'message': 'Signed in.',
            'token': token,
            # 'expires_in': 24 * 60 * 60,  # 24小时
        })
    else:
        return jsonify({'error': 'Incorrect password.'}), 401


@bp.route('/api/admin/verify-token', methods=['POST'])
def verify_admin_token_api():
    data = request.get_json()
    token = data.get('token')

    if not token:
        return jsonify({'valid': False, 'error': 'Token required.'}), 400

    if verify_admin_token(token):
        session['photo_library_admin'] = True
        session.modified = True
        return jsonify({'valid': True, 'message': 'Token valid.'})
    else:
        return jsonify({'valid': False, 'error': 'Token invalid or expired.'})


def _site_config_admin_guard():
    if is_admin_request():
        return None
    return jsonify({'error': 'Admin access required.'}), 401


@bp.route('/api/site-config', methods=['GET'])
def get_site_config():
    denied = _site_config_admin_guard()
    if denied:
        return denied

    conn = get_db_connection()
    configs = conn.execute('SELECT key, value FROM site_config').fetchall()
    conn.close()

    config_dict = {row['key']: row['value'] for row in configs}
    return jsonify(config_dict)


@bp.route('/api/site-config', methods=['PUT'])
def update_site_config():
    denied = _site_config_admin_guard()
    if denied:
        return denied

    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid settings data.'}), 400

    conn = get_db_connection()

    # 处理密码修改
    if 'new_password' in data and data['new_password']:
        password_hash = hashlib.md5(data['new_password'].encode()).hexdigest()
        data['password'] = password_hash
        del data['new_password']



    for key, value in data.items():
        # 先检查配置项是否存在
        existing = conn.execute(
            'SELECT id FROM site_config WHERE key = ?',
            (key,)
        ).fetchone()

        if existing:
            # 存在则更新
            conn.execute('''
                            UPDATE site_config SET value = ?, updated_at = CURRENT_TIMESTAMP 
                            WHERE key = ?
                        ''', (str(value), key))
        else:
            # 不存在则插入（这种情况应该很少，因为init_db已经插入了默认值）
            conn.execute('''
                            INSERT INTO site_config (key, value) 
                            VALUES (?, ?)
                        ''', (key, str(value)))

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': 'Saved.'})


@bp.route('/api/site-config/<string:key>', methods=['GET'])
def get_site_config_by_key(key):
    denied = _site_config_admin_guard()
    if denied:
        return denied

    conn = get_db_connection()
    config = conn.execute(
        'SELECT value FROM site_config WHERE key = ?',
        (key,)
    ).fetchone()
    conn.close()

    if config:
        return jsonify({'key': key, 'value': config['value']})
    else:
        return jsonify({'error': 'Setting not found.'}), 404


@bp.route('/api/admin/logout', methods=['POST'])
def admin_logout_server():
    session.pop('photo_library_admin', None)
    return jsonify({'success': True})


def site_config_enabled(key, default=True):
    """Read one boolean site setting without exposing it to the browser."""
    conn = get_db_connection()
    row = conn.execute('SELECT value FROM site_config WHERE key = ?', (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    return str(row['value']).strip().lower() in {'1', 'true', 'yes', 'on'}
