import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from flask import Flask, request, jsonify, send_file, send_from_directory, session
from flask_cors import CORS

from auth_utils import verify_auth_token, generate_auth_token, album_token_expire_minutes, generate_admin_token, \
    verify_admin_token, admin_required
from image_utils import generate_thumbnail, generate_compressed, get_image_exif_simple
from gps_utils import extract_gps_from_image
from manifest_autofill import build_manifest_reference_index, get_original_jpg_time_range
from workflow_tools import create_workflow_blueprint
from final_builder import create_final_builder_blueprint
from final_metadata import create_final_metadata_blueprint
from set_insights import create_set_insights_blueprint

app = Flask(__name__)


def _load_or_create_session_secret():
    secret_file = Path('.photo_library_session_secret')
    env_secret = os.environ.get('PHOTO_LIBRARY_SESSION_SECRET')
    if env_secret:
        return env_secret
    if secret_file.exists():
        return secret_file.read_text(encoding='utf-8').strip()
    secret = secrets.token_urlsafe(48)
    secret_file.write_text(secret, encoding='utf-8')
    return secret


app.secret_key = _load_or_create_session_secret()
CORS(app)


# 服务前端页面
@app.route('/')
def index():
    return send_file('static/index.html', max_age=0)


# 可选的：服务静态文件（如果需要额外的CSS/JS文件）
@app.route('/<path:path>')
def serve_static(path):
    # send_from_directory performs safe path joining and rejects traversal outside
    # the static directory (for example ../photo_library.db or encoded variants).
    return send_from_directory(app.static_folder, path, max_age=0)


# 配置
UPLOAD_FOLDER = 'uploads'
THUMBNAIL_FOLDER = 'thumbnails'
COMPRESSED_FOLDER = 'compressed'
LIBRARY_CACHE_FOLDER = 'library_cache'
DATABASE = 'photo_library.db'

image_processing_queue = queue.Queue()
processing_lock = threading.Lock()
current_processing = {}

# 确保目录存在
for folder in [UPLOAD_FOLDER, THUMBNAIL_FOLDER, COMPRESSED_FOLDER, LIBRARY_CACHE_FOLDER]:
    os.makedirs(folder, exist_ok=True)


# 初始化数据库
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # 相册表
    c.execute('''
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cover_image_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT,
            shoot_date TEXT,
            model_name TEXT,
            location TEXT
        )
    ''')

    # 图片表
    c.execute('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            file_size INTEGER,
            width INTEGER,
            height INTEGER,
            description TEXT,
            file_hash TEXT,
            is_favorited BOOLEAN DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (album_id) REFERENCES albums (id)
        )
    ''')

    # 相册密码表
    c.execute('''
            CREATE TABLE IF NOT EXISTS album_passwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (album_id) REFERENCES albums (id) ON DELETE CASCADE,
                UNIQUE(album_id)
            )
        ''')

    # 确保管理员表存在
    conn.execute('''
        CREATE TABLE IF NOT EXISTS site_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 插入默认配置（如果不存在）
    default_configs = [
        ('site_title', 'Photo Library'),
        ('password', hashlib.md5('admin'.encode()).hexdigest()),  # 管理员密码
        ('allow_guest_upload', '0'),  # 游客是否可上传，0=否，1=是
        ('show_exif_on_hover', '1'),  # 新增：默认显示EXIF
        ('show_library_folder_covers', '1'),  # 映射目录封面，1=显示，0=仅 Folder icon
    ]

    for key, value in default_configs:
        c.execute('''
                INSERT OR IGNORE INTO site_config (key, value) VALUES (?, ?)
            ''', (key, value))

    # 分组
    # 在初始化数据库的 init_db() 函数中添加以下表

    # 分组表
    c.execute('''
        CREATE TABLE IF NOT EXISTS album_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 相册-分组关联表（多对多关系）
    c.execute('''
        CREATE TABLE IF NOT EXISTS album_group_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (album_id) REFERENCES albums (id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES album_groups (id) ON DELETE CASCADE,
            UNIQUE(album_id, group_id)
        )
    ''')

    # 本地目录 Source：DB 只保存映射配置，不复制图片记录。
    c.execute('''
        CREATE TABLE IF NOT EXISTS library_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS library_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            source_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL DEFAULT '',
            title TEXT,
            password_hash TEXT,
            allow_select INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES library_sources (id) ON DELETE CASCADE
        )
    ''')
    # v1 曾按分享 session 保存选片；v2 改为图片本身的全局收藏状态。
    c.execute('DROP TABLE IF EXISTS library_share_selections')
    # 本地映射图片的全局状态。分享页和管理员目录共用，不按分享 session 拆分。
    c.execute('''
        CREATE TABLE IF NOT EXISTS library_image_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            is_favorited INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES library_sources (id) ON DELETE CASCADE,
            UNIQUE(source_id, relative_path)
        )
    ''')
    conn.commit()
    conn.close()


# 数据库连接
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# API路由

# 获取所有相册
@app.route('/api/albums', methods=['GET'])
def get_albums():
    conn = get_db_connection()
    albums = conn.execute('''
        SELECT a.*, i.filename as cover_filename,
        (SELECT COUNT(*) FROM images WHERE album_id = a.id) as image_count,
        CASE WHEN ap.id IS NOT NULL THEN 1 ELSE 0 END as has_password
        FROM albums a 
        LEFT JOIN images i ON a.cover_image_id = i.id
        LEFT JOIN album_passwords ap ON a.id = ap.album_id
    ''').fetchall()
    conn.close()

    return jsonify([dict(album) for album in albums])


# 创建相册
@app.route('/api/albums', methods=['POST'])
@admin_required
def create_album():
    data = request.get_json()
    name = data.get('name')
    description = data.get('description')
    shoot_date = data.get('shoot_date')
    model_name = data.get('model_name')
    location = data.get('location')

    group_ids = data.get('group_ids', [])  # 新增：分组ID列表

    if not name:
        return jsonify({'error': '相册名称不能为空'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO albums (name, description, shoot_date, model_name, location)
        VALUES (?, ?, ?, ?, ?)
    ''', (name, description, shoot_date, model_name, location))
    album_id = cursor.lastrowid

    # 设置分组
    for group_id in group_ids:
        cursor.execute('''
             INSERT INTO album_group_relations (album_id, group_id)
             VALUES (?, ?)
         ''', (album_id, group_id))

    conn.commit()
    conn.close()

    return jsonify({'id': album_id, 'message': '相册创建成功'})


# 更新相册
@app.route('/api/albums/<int:album_id>', methods=['PUT'])
@admin_required
def update_album(album_id):
    data = request.get_json()
    name = data.get('name')
    description = data.get('description')
    shoot_date = data.get('shoot_date')
    model_name = data.get('model_name')
    location = data.get('location')
    cover_image_id = data.get('cover_image_id')

    group_ids = data.get('group_ids', [])  # 新增：分组ID列表

    conn = get_db_connection()
    cursor = conn.cursor()

    # 构建更新语句
    update_fields = []
    values = []

    if name is not None:
        update_fields.append("name = ?")
        values.append(name)
    if description is not None:
        update_fields.append("description = ?")
        values.append(description)
    if shoot_date is not None:
        update_fields.append("shoot_date = ?")
        values.append(shoot_date)
    if model_name is not None:
        update_fields.append("model_name = ?")
        values.append(model_name)
    if location is not None:
        update_fields.append("location = ?")
        values.append(location)
    if cover_image_id is not None:
        update_fields.append("cover_image_id = ?")
        values.append(cover_image_id)

    if update_fields:
        values.append(album_id)
        cursor.execute(f'''
            UPDATE albums SET {', '.join(update_fields)} WHERE id = ?
        ''', values)

        # 删除原有的分组关联
    cursor.execute('DELETE FROM album_group_relations WHERE album_id = ?', (album_id,))

    # 添加新的分组关联（如果 group_ids 为空，则不添加）
    for group_id in group_ids:
        cursor.execute('''
                    INSERT INTO album_group_relations (album_id, group_id)
                    VALUES (?, ?)
                ''', (album_id, group_id))

    conn.commit()
    conn.close()
    return jsonify({'message': '相册更新成功'})


# 删除相册
@app.route('/api/albums/<int:album_id>', methods=['DELETE'])
@admin_required
def delete_album(album_id):
    conn = get_db_connection()

    # 获取相册中的所有图片
    images = conn.execute('SELECT * FROM images WHERE album_id = ?', (album_id,)).fetchall()

    # 删除图片文件
    for image in images:
        original_path = os.path.join(UPLOAD_FOLDER, image['filename'])
        thumb_path = os.path.join(THUMBNAIL_FOLDER, image['filename'])
        compressed_path = os.path.join(COMPRESSED_FOLDER, image['filename'])

        for path in [original_path, thumb_path, compressed_path]:
            if os.path.exists(path):
                os.remove(path)

    # 删除数据库记录
    conn.execute('DELETE FROM images WHERE album_id = ?', (album_id,))
    conn.execute('DELETE FROM albums WHERE id = ?', (album_id,))
    conn.commit()
    conn.close()

    return jsonify({'message': '相册删除成功'})


# 获取相册中的图片
@app.route('/api/albums/<int:album_id>/images', methods=['GET'])
def get_album_images(album_id):
    conn = get_db_connection()

    # 密码验证
    password_record = conn.execute(
        'SELECT id FROM album_passwords WHERE album_id = ?', (album_id,)
    ).fetchone()
    # 如果有密码，验证访问权限
    if password_record:
        # 检查请求头中是否有验证token
        auth_token = request.headers.get('X-Album-Auth')
        admin_token = request.headers.get('X-Admin-Token')  # 获取管理员token

        # 先验证管理员token
        is_admin = False
        if admin_token:
            # 使用您现有的verify_admin_token函数验证
            is_admin = verify_admin_token(admin_token)

        # 如果是管理员，允许访问；否则验证相册密码
        if not is_admin and (not auth_token or not verify_auth_token(auth_token, album_id)):
            conn.close()
            return jsonify({'error': '无权访问此加密相册'}), 403

        # if not auth_token or not verify_auth_token(auth_token, album_id):
        #     conn.close()
        #     return jsonify({'error': '无权访问此加密相册'}), 403

    images = conn.execute('''
        SELECT * FROM images WHERE album_id = ? ORDER BY uploaded_at DESC
    ''', (album_id,)).fetchall()

    return jsonify([dict(image) for image in images])


@app.route('/api/images/<int:image_id>/file')
def get_image_file(image_id):
    file_type = request.args.get('type', 'compressed')  # compressed, thumbnail, original

    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()
    conn.close()

    if not image:
        return jsonify({'error': '图片不存在'}), 404

    # 获取文件路径
    original_path = os.path.join(UPLOAD_FOLDER, image['filename'])
    thumb_path = os.path.join(THUMBNAIL_FOLDER, image['filename'])
    compressed_path = os.path.join(COMPRESSED_FOLDER, image['filename'])

    if file_type == 'original':
        file_path = original_path
    elif file_type == 'thumbnail':
        file_path = thumb_path
    else:  # compressed
        file_path = compressed_path

    # 检查请求的文件是否存在
    if os.path.exists(file_path):
        return send_file(file_path)

    # 如果请求的文件不存在，但原图存在，重新生成
    if file_type != 'original' and os.path.exists(original_path):
        try:
            if file_type == 'thumbnail':
                generate_thumbnail(original_path, thumb_path)
            else:  # compressed
                generate_compressed(original_path, compressed_path)

            # 检查是否生成成功
            if os.path.exists(file_path):
                return send_file(file_path)
        except Exception as e:
            # 生成失败，返回错误
            return jsonify({'error': f'文件生成失败: {str(e)}'}), 500

    # 其他情况返回文件不存在
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/images/<int:image_id>/exif', methods=['GET'])
def get_image_exif(image_id):
    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if not image:
        conn.close()
        return jsonify({'error': '图片不存在'}), 404

    conn.close()

    # 获取原图路径
    original_path = os.path.join(UPLOAD_FOLDER, image['filename'])

    if not os.path.exists(original_path):
        return jsonify({'error': '原图文件不存在'}), 404

    try:
        exif = get_image_exif_simple(original_path)
        return jsonify({'exif': exif}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/albums/<int:album_id>/images', methods=['POST'])
def upload_image(album_id):
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '没有选择文件'}), 400


    #config start

    # 检查上传权限
    admin_token = request.headers.get('X-Admin-Token')
    is_admin = admin_token and verify_admin_token(admin_token)

    # 如果不是管理员，检查是否允许游客上传
    if not is_admin:
        conn = get_db_connection()
        config = conn.execute(
            'SELECT value FROM site_config WHERE key = ?',
            ('allow_guest_upload',)
        ).fetchone()
        conn.close()

        if not config or config['value'] != '1':
            return jsonify({'error': '游客不允许上传图片'}), 403

    #config end






    # 流式计算MD5，避免将整个文件读入内存
    md5_hash = hashlib.md5()
    # 创建一个临时文件来存储内容，以便后续使用
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1])

    try:
        # 流式读取并计算MD5，同时保存到临时文件
        chunk_size = 8192  # 8KB chunks
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            md5_hash.update(chunk)
            temp_file.write(chunk)

        file_md5 = md5_hash.hexdigest()
        temp_file.close()  # 关闭临时文件

        # 重置文件指针以便后续读取
        file.seek(0)

        # 检查数据库中是否已存在相同MD5的图片
        conn = get_db_connection()
        existing_image = conn.execute('''
            SELECT i.id, i.original_filename, a.name as album_name 
            FROM images i 
            JOIN albums a ON i.album_id = a.id 
            WHERE i.file_hash = ?
        ''', (file_md5,)).fetchone()

        if existing_image:
            conn.close()
            os.unlink(temp_file.name)  # 删除临时文件
            return jsonify({
                'error': f'图片已存在于相册 "{existing_image["album_name"]}" 中',
                'existing_filename': existing_image['original_filename'],
                'album_name': existing_image['album_name']
            }), 409

        conn.close()

        # 生成唯一文件名
        filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"

        # 修改任务参数，传递临时文件路径而不是文件对象
        task = (album_id, filename, file_md5, temp_file.name, file.filename, file.content_length)
        image_processing_queue.put(task)

        return jsonify({
            'message': '图片已加入处理队列',
            'filename': filename,
            'queue_position': image_processing_queue.qsize(),
            'immediate_response': True
        })

    except Exception as e:
        # 清理临时文件
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)
        return jsonify({'error': f'上传失败: {str(e)}'}), 500


@app.route('/api/images/<int:image_id>/rename', methods=['POST'])
def rename_image(image_id):
    data = request.get_json()
    new_filename = data.get('new_filename')

    if not new_filename:
        return jsonify({'error': '新文件名不能为空'}), 400

    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if not image:
        conn.close()
        return jsonify({'error': '图片不存在'}), 404

    # 检查新文件名是否已存在
    existing = conn.execute('SELECT id FROM images WHERE original_filename = ? AND id != ?',
                            (new_filename, image_id)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': '文件名已存在'}), 400

    # 更新数据库
    conn.execute('UPDATE images SET original_filename = ? WHERE id = ?',
                 (new_filename, image_id))
    conn.commit()
    conn.close()

    return jsonify({'message': '重命名成功'})


# 更新图片描述API
@app.route('/api/images/<int:image_id>/description', methods=['PUT'])
def update_image_description(image_id):
    data = request.get_json()
    description = data.get('description', '')

    conn = get_db_connection()

    # 检查图片是否存在
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()
    if not image:
        conn.close()
        return jsonify({'error': '图片不存在'}), 404

    # 更新描述
    conn.execute('UPDATE images SET description = ? WHERE id = ?',
                 (description, image_id))
    conn.commit()
    conn.close()

    return jsonify({'message': '描述更新成功'})


# 添加收藏
@app.route('/api/images/<int:image_id>/favorite', methods=['POST'])
def toggle_favorite(image_id):
    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if not image:
        conn.close()
        return jsonify({'error': '图片不存在'}), 404

    # 切换收藏状态
    new_favorite_state = not image['is_favorited']
    conn.execute('UPDATE images SET is_favorited = ? WHERE id = ?',
                 (new_favorite_state, image_id))
    conn.commit()
    conn.close()

    return jsonify({
        'is_favorited': new_favorite_state,
        'message': '操作成功'
    })


# 删除图片
@app.route('/api/images/<int:image_id>', methods=['DELETE'])
@admin_required
def delete_image(image_id):
    conn = get_db_connection()

    # 获取图片信息
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if image:
        # 删除文件
        original_path = os.path.join(UPLOAD_FOLDER, image['filename'])
        thumb_path = os.path.join(THUMBNAIL_FOLDER, image['filename'])
        compressed_path = os.path.join(COMPRESSED_FOLDER, image['filename'])

        for path in [original_path, thumb_path, compressed_path]:
            if os.path.exists(path):
                os.remove(path)

        # 删除数据库记录
        conn.execute('DELETE FROM images WHERE id = ?', (image_id,))
        conn.commit()

    conn.close()
    return jsonify({'message': '图片删除成功'})


# 验证相册密码API
@app.route('/api/albums/<int:album_id>/verify-password', methods=['POST'])
def verify_album_password(album_id):
    data = request.get_json()
    password = data.get('password')

    if not password:
        return jsonify({'error': '密码不能为空'}), 400

    conn = get_db_connection()

    # 检查相册是否存在
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_id,)).fetchone()
    if not album:
        conn.close()
        return jsonify({'error': '相册不存在'}), 404

    # 获取密码哈希
    password_record = conn.execute(
        'SELECT * FROM album_passwords WHERE album_id = ?',
        (album_id,)
    ).fetchone()
    conn.close()

    if not password_record:
        return jsonify({'error': '此相册未设置密码'}), 400

    # 简单密码验证（实际应该使用加密哈希）
    token = generate_auth_token(album_id)

    if password_record['password_hash'] == password:
        return jsonify({
            'success': True,
            'message': '密码验证成功',
            'token': token,
            'expires_in': album_token_expire_minutes * 60  # 返回有效期（秒）
        })
    else:
        return jsonify({'error': '密码错误'}), 401


# 设置/更新相册密码
@app.route('/api/albums/<int:album_id>/password', methods=['POST'])
def set_album_password(album_id):
    data = request.get_json()
    password = data.get('password')

    if not password:
        return jsonify({'error': '密码不能为空'}), 400

    conn = get_db_connection()

    # 检查相册是否存在
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_id,)).fetchone()
    if not album:
        conn.close()
        return jsonify({'error': '相册不存在'}), 404

    # 检查是否已设置密码
    existing_password = conn.execute(
        'SELECT id FROM album_passwords WHERE album_id = ?',
        (album_id,)
    ).fetchone()

    if existing_password:
        # 更新密码
        conn.execute(
            'UPDATE album_passwords SET password_hash = ? WHERE album_id = ?',
            (password, album_id)
        )
    else:
        # 插入新密码
        conn.execute(
            'INSERT INTO album_passwords (album_id, password_hash) VALUES (?, ?)',
            (album_id, password)
        )

    conn.commit()
    conn.close()

    return jsonify({'message': '密码设置成功'})


# 移除相册密码
@app.route('/api/albums/<int:album_id>/password', methods=['DELETE'])
def remove_album_password(album_id):
    conn = get_db_connection()

    # 检查相册是否存在
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_id,)).fetchone()
    if not album:
        conn.close()
        return jsonify({'error': '相册不存在'}), 404

    # 删除密码记录
    conn.execute('DELETE FROM album_passwords WHERE album_id = ?', (album_id,))
    conn.commit()
    conn.close()

    return jsonify({'message': '密码已移除'})


# 检查相册是否有密码
@app.route('/api/albums/<int:album_id>/has-password')
def check_album_password(album_id):
    conn = get_db_connection()

    password_record = conn.execute(
        'SELECT id FROM album_passwords WHERE album_id = ?',
        (album_id,)
    ).fetchone()
    conn.close()

    return jsonify({'has_password': password_record is not None})


# 添加token验证接口
@app.route('/api/albums/<int:album_id>/verify-token', methods=['POST'])
def verify_album_token(album_id):
    data = request.get_json()
    token = data.get('token')

    if not token:
        return jsonify({'valid': False, 'error': 'Token不能为空'}), 400

    # 使用之前的verify_auth_token函数验证
    if verify_auth_token(token, album_id):
        return jsonify({'valid': True, 'message': 'Token有效'})
    else:
        return jsonify({'valid': False, 'error': 'Token无效或已过期'})


# 标题
@app.route('/api/albums/title', methods=['PUT'])
def update_site_title():
    data = request.get_json()
    new_title = data.get('title', '').strip()

    if not new_title:
        return jsonify({'error': '标题不能为空'}), 400

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

        return jsonify({'message': '标题更新成功', 'title': new_title})
    except Exception as e:
        return jsonify({'error': f'更新标题失败: {str(e)}'}), 500


# 添加获取标题的API
@app.route('/api/albums/title', methods=['GET'])
def get_site_title():
    try:
        conn = get_db_connection()

        title_record = conn.execute(
            'SELECT value FROM site_config WHERE key = ?', ('site_title',)
        ).fetchone()

        conn.close()

        default_title = '我的相册'
        if title_record and title_record['value']:
            return jsonify({'title': title_record['value']})
        else:
            return jsonify({'title': default_title})
    except Exception as e:
        return jsonify({'title': '我的相册'})


# 移动图片到其他相册
@app.route('/api/images/move', methods=['POST'])
def move_images():
    data = request.get_json()
    image_ids = data.get('image_ids', [])
    target_album_id = data.get('target_album_id')

    if not image_ids:
        return jsonify({'error': '请选择要移动的图片'}), 400

    if not target_album_id:
        return jsonify({'error': '请选择目标相册'}), 400

    conn = get_db_connection()

    try:
        # 检查目标相册是否存在
        target_album = conn.execute('SELECT id FROM albums WHERE id = ?', (target_album_id,)).fetchone()
        if not target_album:
            conn.close()
            return jsonify({'error': '目标相册不存在'}), 404

        # 检查所有图片是否存在
        placeholders = ','.join(['?'] * len(image_ids))
        existing_images = conn.execute(f'''
            SELECT id, album_id, filename FROM images WHERE id IN ({placeholders})
        ''', image_ids).fetchall()

        if len(existing_images) != len(image_ids):
            conn.close()
            return jsonify({'error': '部分图片不存在'}), 404

        # 移动图片
        moved_count = 0
        for image in existing_images:
            # 如果图片已经在目标相册中，跳过
            if image['album_id'] == target_album_id:
                continue

            # 更新图片的album_id
            conn.execute('UPDATE images SET album_id = ? WHERE id = ?', (target_album_id, image['id']))
            moved_count += 1

        conn.commit()
        conn.close()

        return jsonify({
            'message': f'成功移动 {moved_count} 张图片',
            'moved_count': moved_count,
            'total_count': len(image_ids)
        })

    except Exception as e:
        conn.close()
        return jsonify({'error': f'移动图片失败: {str(e)}'}), 500


# 获取处理状态
@app.route('/api/upload/status/<filename>', methods=['GET'])
def get_upload_status(filename):
    """获取图片处理状态"""
    with processing_lock:
        if filename in current_processing:
            return jsonify({
                'status': 'processing',
                'message': '正在处理中'
            })

    # 检查是否已处理完成
    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE filename = ?', (filename,)).fetchone()
    conn.close()

    if image:
        return jsonify({
            'status': 'completed',
            'image_id': image['id'],
            'filename': image['filename'],
            'original_filename': image['original_filename']
        })
    else:
        return jsonify({
            'status': 'queued',
            'queue_position': 'unknown'
        }), 404


def image_processing_worker():
    while True:
        try:
            task = image_processing_queue.get()
            if task is None:  # 停止信号
                break

            album_id, filename, file_md5, temp_file_path, original_filename, file_size = task

            with processing_lock:
                current_processing[filename] = True

            try:
                original_path = os.path.join(UPLOAD_FOLDER, filename)
                thumb_path = os.path.join(THUMBNAIL_FOLDER, filename)
                compressed_path = os.path.join(COMPRESSED_FOLDER, filename)

                # 移动临时文件到目标位置
                shutil.move(temp_file_path, original_path)

                # 获取图片尺寸
                with Image.open(original_path) as img:
                    width, height = img.size
                    # 这里已经获取了文件大小，不需要再次获取
                    actual_file_size = os.path.getsize(original_path)

                # 顺序处理，避免并发
                generate_thumbnail(original_path, thumb_path)
                generate_compressed(original_path, compressed_path)

                # 保存到数据库
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO images (album_id, filename, original_filename, file_size, width, height, file_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (album_id, filename, original_filename, actual_file_size, width, height, file_md5))
                image_id = cursor.lastrowid
                conn.commit()
                conn.close()

                print(f"Successfully processed image: {filename}")

            except Exception as e:
                print(f"Error processing image {filename}: {str(e)}")
                # 清理可能存在的部分文件
                for path in [original_path, thumb_path, compressed_path]:
                    if os.path.exists(path):
                        os.remove(path)
            finally:
                with processing_lock:
                    if filename in current_processing:
                        del current_processing[filename]

                image_processing_queue.task_done()

        except Exception as e:
            print(f"Worker error: {str(e)}")


# 分组相关
# 获取所有分组及分组下的相册

@app.route('/api/album-groups', methods=['GET'])
def get_album_groups():
    conn = get_db_connection()

    # 获取所有分组
    groups = conn.execute('''
        SELECT * FROM album_groups 
        ORDER BY sort_order, created_at
    ''').fetchall()

    result = []
    for group in groups:
        # 获取该分组下的相册
        albums = conn.execute('''
            SELECT a.*, i.filename as cover_filename,
            (SELECT COUNT(*) FROM images WHERE album_id = a.id) as image_count,
            CASE WHEN ap.id IS NOT NULL THEN 1 ELSE 0 END as has_password
            FROM albums a 
            LEFT JOIN images i ON a.cover_image_id = i.id
            LEFT JOIN album_passwords ap ON a.id = ap.album_id
            JOIN album_group_relations agr ON a.id = agr.album_id
            WHERE agr.group_id = ?
            ORDER BY a.name COLLATE NOCASE ASC  -- 按名称排序，忽略大小写
            --ORDER BY agr.sort_order, a.created_at
        ''', (group['id'],)).fetchall()

        result.append({
            'id': group['id'],
            'name': group['name'],
            'sort_order': group['sort_order'],
            'album_count': len(albums),
            'albums': [dict(album) for album in albums]
        })

    # 添加"未分组"作为一个特殊分组
    ungrouped_albums = conn.execute('''
        SELECT a.*, i.filename as cover_filename,
        (SELECT COUNT(*) FROM images WHERE album_id = a.id) as image_count,
        CASE WHEN ap.id IS NOT NULL THEN 1 ELSE 0 END as has_password
        FROM albums a 
        LEFT JOIN images i ON a.cover_image_id = i.id
        LEFT JOIN album_passwords ap ON a.id = ap.album_id
        WHERE a.id NOT IN (
            SELECT DISTINCT album_id FROM album_group_relations
        )
        ORDER BY a.name COLLATE NOCASE ASC  -- 未分组相册也按名称排序
        --ORDER BY a.created_at DESC
    ''').fetchall()

    if ungrouped_albums:
        result.append({
            'id': -1,  # 使用-1表示未分组
            'name': '未分组',
            'sort_order': 9999,  # 排在最后
            'album_count': len(ungrouped_albums),
            'albums': [dict(album) for album in ungrouped_albums],
            'is_ungrouped': True  # 添加标记
        })

    conn.close()
    return jsonify(result)


# 创建分组
@app.route('/api/album-groups', methods=['POST'])
def create_album_group():
    data = request.get_json()
    name = data.get('name')
    sort_order = data.get('sort_order', 0)

    if not name:
        return jsonify({'error': '分组名称不能为空'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('''
            INSERT INTO album_groups (name, sort_order) 
            VALUES (?, ?)
        ''', (name, sort_order))
        group_id = cursor.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': '分组名称已存在'}), 400

    conn.close()
    return jsonify({'id': group_id, 'message': '分组创建成功'})


# 更新分组
@app.route('/api/album-groups/<int:group_id>', methods=['PUT'])
def update_album_group(group_id):
    data = request.get_json()
    name = data.get('name')
    sort_order = data.get('sort_order')

    conn = get_db_connection()
    cursor = conn.cursor()

    update_fields = []
    values = []

    if name is not None:
        update_fields.append("name = ?")
        values.append(name)
    if sort_order is not None:
        update_fields.append("sort_order = ?")
        values.append(sort_order)

    if update_fields:
        values.append(group_id)
        try:
            cursor.execute(f'''
                UPDATE album_groups SET {', '.join(update_fields)} WHERE id = ?
            ''', values)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'error': '分组名称已存在'}), 400

    conn.close()
    return jsonify({'message': '分组更新成功'})


@app.route('/api/album-groups/<int:group_id>', methods=['DELETE'])
def delete_album_group(group_id):
    data = request.get_json() if request.data else {}
    move_to_ungrouped = data.get('move_to_ungrouped', True)  # 默认移到未分组

    conn = get_db_connection()

    # 检查分组是否存在
    group = conn.execute('SELECT * FROM album_groups WHERE id = ?', (group_id,)).fetchone()
    if not group:
        conn.close()
        return jsonify({'error': '分组不存在'}), 404

    try:
        # 先获取该分组下的相册ID
        album_ids = conn.execute('''
            SELECT album_id FROM album_group_relations WHERE group_id = ?
        ''', (group_id,)).fetchall()
        album_id_list = [row['album_id'] for row in album_ids]

        # 手动删除关联表中的数据（确保删除）
        conn.execute('DELETE FROM album_group_relations WHERE group_id = ?', (group_id,))

        # 删除分组
        conn.execute('DELETE FROM album_groups WHERE id = ?', (group_id,))

        conn.commit()

        conn.close()

        if move_to_ungrouped:
            return jsonify({
                'message': '分组删除成功，相册已移到未分组',
                'affected_albums': album_id_list,
                'album_count': len(album_id_list)
            })
        else:
            return jsonify({
                'message': '分组删除成功',
                'affected_albums': album_id_list,
                'album_count': len(album_id_list)
            })

    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': f'删除失败: {str(e)}'}), 500


# 为相册设置分组
@app.route('/api/albums/<int:album_id>/groups', methods=['POST'])
def set_album_groups(album_id):
    data = request.get_json()
    group_ids = data.get('group_ids', [])  # 可以属于多个分组

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 删除原有的分组关联
        cursor.execute('DELETE FROM album_group_relations WHERE album_id = ?', (album_id,))

        # 添加新的分组关联
        for group_id in group_ids:
            cursor.execute('''
                INSERT INTO album_group_relations (album_id, group_id)
                VALUES (?, ?)
            ''', (album_id, group_id))

        conn.commit()
        conn.close()
        return jsonify({'message': '分组设置成功'})
    except Exception as e:
        conn.close()
        return jsonify({'error': f'设置失败: {str(e)}'}), 500


# 获取相册的分组信息
@app.route('/api/albums/<int:album_id>/groups', methods=['GET'])
def get_album_groups_info(album_id):
    conn = get_db_connection()

    groups = conn.execute('''
        SELECT g.* FROM album_groups g
        JOIN album_group_relations agr ON g.id = agr.group_id
        WHERE agr.album_id = ?
        ORDER BY g.sort_order
    ''', (album_id,)).fetchall()

    conn.close()
    return jsonify([dict(group) for group in groups])

# admin start
# 管理员密码验证接口（只验证，不限制其他API）
@app.route('/api/admin/verify-password', methods=['POST'])
def verify_admin_password():
    data = request.get_json()
    password = data.get('password')

    if not password:
        return jsonify({'error': '密码不能为空'}), 400

    # 这里从数据库获取正确密码（简化示例）
    conn = get_db_connection()

    admin = conn.execute('SELECT * FROM site_config where key="password"').fetchone()
    conn.close()

    if not admin:
        return jsonify({'error': '系统未配置管理员'}), 500

    # 验证密码
    password_hash = hashlib.md5(password.encode()).hexdigest()
    if password_hash == admin['value']:
        # 生成管理员token，并建立同源 session 供本地目录图片直接请求使用。
        token = generate_admin_token()
        session['photo_library_admin'] = True
        session.modified = True
        return jsonify({
            'success': True,
            'message': '密码验证成功',
            'token': token,
            # 'expires_in': 24 * 60 * 60,  # 24小时
        })
    else:
        return jsonify({'error': '密码错误'}), 401


# 验证管理员token API
@app.route('/api/admin/verify-token', methods=['POST'])
def verify_admin_token_api():
    data = request.get_json()
    token = data.get('token')

    if not token:
        return jsonify({'valid': False, 'error': 'Token不能为空'}), 400

    if verify_admin_token(token):
        session['photo_library_admin'] = True
        session.modified = True
        return jsonify({'valid': True, 'message': 'Token有效'})
    else:
        return jsonify({'valid': False, 'error': 'Token无效或已过期'})


#config

def _site_config_admin_guard():
    # The normal browser login establishes this same-origin session.  Keep
    # X-Admin-Token support for direct/API access as well.
    if session.get('photo_library_admin') is True:
        return None

    token = request.headers.get('X-Admin-Token')
    if token and verify_admin_token(token):
        return None

    return jsonify({'error': '需要管理员权限'}), 401

# 获取所有配置（管理员专用）
@app.route('/api/site-config', methods=['GET'])
def get_site_config():
    denied = _site_config_admin_guard()
    if denied:
        return denied

    conn = get_db_connection()
    configs = conn.execute('SELECT key, value FROM site_config').fetchall()
    conn.close()

    config_dict = {row['key']: row['value'] for row in configs}
    return jsonify(config_dict)


# 更新配置（管理员专用）
@app.route('/api/site-config', methods=['PUT'])
def update_site_config():
    denied = _site_config_admin_guard()
    if denied:
        return denied

    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'error': '配置数据格式错误'}), 400

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

    return jsonify({'success': True, 'message': '配置更新成功'})


# 获取特定配置（管理员专用）
@app.route('/api/site-config/<string:key>', methods=['GET'])
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
        return jsonify({'error': '配置不存在'}), 404



# ==================== 本地目录映射 / Library Source ====================

LIBRARY_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
MANIFEST_FILENAME = 'manifest.json'

# Canonical manifest key order. Unknown/custom fields are deliberately NOT
# listed here: they are preserved at their existing position while only the
# known fields are re-ordered around them.
_MANIFEST_KEY_ORDERS = {
    'root': (
        'model', 'shoot', 'location', 'theme', 'production', 'props', 'lighting'
    ),
    'shoot': (
        'date', 'start_time', 'end_time', 'environment', 'scene', 'weather',
        'additional_sessions'
    ),
    'additional_sessions': ('date', 'start_time', 'end_time', 'weather'),
    'location': ('name', 'address', 'lat', 'lng'),
    'theme': (
        'name', 'genre', 'source_title', 'source_type', 'character', 'variant',
        'reference_type', 'reference', 'outfit'
    ),
    # Credits are optional and intentionally come last when present.
    'production': (
        'collaboration_type', 'lead_photographer', 'model_fee', 'venue_fee',
        'venue_fee_payer', 'primary_photographer', 'assistants'
    ),
    'props': ('subject', 'set'),
    'lighting': (
        'role', 'light_type', 'fixture', 'modifier', 'count', 'position', 'note'
    ),
}


def _order_known_keys_preserving_unknown_positions(mapping, preferred_order):
    """Re-order known keys without moving or deleting unknown/custom keys.

    Example:
        {'lighting': ..., 'project': ..., 'model': ...}
    becomes:
        {'model': ..., 'project': ..., 'lighting': ...}

    `project` stays in the slot where the user inserted it. Missing optional
    keys are never created.
    """
    if not isinstance(mapping, dict):
        return mapping

    preferred = [key for key in preferred_order if key in mapping]
    preferred_set = set(preferred_order)
    preferred_iter = iter(preferred)
    result = {}
    for key, value in mapping.items():
        if key in preferred_set:
            ordered_key = next(preferred_iter)
            result[ordered_key] = mapping[ordered_key]
        else:
            result[key] = value
    return result


def _order_manifest_keys(payload):
    """Apply canonical ordering to known manifest fields only."""
    if not isinstance(payload, dict):
        return payload

    result = dict(payload)

    shoot = result.get('shoot')
    if isinstance(shoot, dict):
        shoot = dict(shoot)
        additional = shoot.get('additional_sessions')
        if isinstance(additional, list):
            shoot['additional_sessions'] = [
                _order_known_keys_preserving_unknown_positions(
                    item, _MANIFEST_KEY_ORDERS['additional_sessions']
                ) if isinstance(item, dict) else item
                for item in additional
            ]
        result['shoot'] = _order_known_keys_preserving_unknown_positions(
            shoot, _MANIFEST_KEY_ORDERS['shoot']
        )

    for section in ('location', 'theme', 'production', 'props'):
        value = result.get(section)
        if isinstance(value, dict):
            result[section] = _order_known_keys_preserving_unknown_positions(
                value, _MANIFEST_KEY_ORDERS[section]
            )

    lighting = result.get('lighting')
    if isinstance(lighting, list):
        result['lighting'] = [
            _order_known_keys_preserving_unknown_positions(
                item, _MANIFEST_KEY_ORDERS['lighting']
            ) if isinstance(item, dict) else item
            for item in lighting
        ]

    return _order_known_keys_preserving_unknown_positions(
        result, _MANIFEST_KEY_ORDERS['root']
    )


def _is_admin_request():
    # Library 图片会被 <img> 直接请求，所以同时支持管理员 session 与旧 X-Admin-Token。
    if session.get('photo_library_admin') is True:
        return True
    token = request.headers.get('X-Admin-Token')
    return bool(token and verify_admin_token(token))


def _library_admin_guard():
    if not _is_admin_request():
        return jsonify({'error': '需要管理员权限'}), 401
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
        raise ValueError('非法路径')
    return '/'.join(parts)


def _resolve_library_path(source, relative_path='', require_exists=True):
    root = Path(source['root_path']).expanduser().resolve()
    rel = _normalize_relative_path(relative_path)
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError('路径超出 Source 根目录')
    if require_exists and not target.exists():
        raise FileNotFoundError('路径不存在')
    return root, target, rel


def _read_manifest(path):
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        raw = manifest_path.read_text(encoding='utf-8')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('manifest 根节点必须是 JSON object')
        # Keep the original text alongside parsed data so Raw JSON editing can
        # preserve the file's exact field order and formatting on open.
        return {'exists': True, 'valid': True, 'data': data, 'raw': raw, 'error': None}
    except Exception as exc:
        return {'exists': True, 'valid': False, 'data': None, 'raw': None, 'error': str(exc)}


def _find_nearest_manifest(root, target):
    current = target if target.is_dir() else target.parent
    while True:
        result = _read_manifest(current)
        if result is not None:
            result['relative_path'] = '' if current == root else current.relative_to(root).as_posix()
            return result
        if current == root:
            break
        current = current.parent
    return {'exists': False, 'valid': False, 'data': None, 'error': None, 'relative_path': None}


SET_FOLDER_RE = re.compile(r'^\d{8}-.+-.+$')


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
        raise ValueError(f'{label}不能为空')
    if text in {'.', '..'} or any(char in text for char in ('/', '\\', '\x00', '\n', '\r')):
        raise ValueError(f'{label}包含非法路径字符')
    if not allow_hyphen and '-' in text:
        raise ValueError(f'{label}不能包含连字符 -')
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
    return _order_manifest_keys(manifest)


def _create_new_set(root, date_text, model, theme):
    """Create one complete Set atomically below a Source root."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError('Source 根目录不存在')
    if _is_set_folder_name(root.name):
        raise ValueError('New Set 只能在 Set 父目录创建')

    try:
        shoot_date = datetime.strptime(str(date_text or '').strip(), '%Y-%m-%d').date()
    except ValueError as exc:
        raise ValueError('日期格式必须为 YYYY-MM-DD') from exc

    # The first hyphen separates date/model in the canonical Set name, so a
    # model containing '-' would make the existing Set parser ambiguous.
    model = _validate_new_set_text(model, '模特', allow_hyphen=False)
    theme = _validate_new_set_text(theme, '主题', allow_hyphen=True)
    set_name = f'{shoot_date:%Y%m%d}-{model}-{theme}'
    set_path = root / set_name

    if set_path.exists():
        raise FileExistsError(f'Set 已存在: {set_name}')

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
        raise FileNotFoundError('Source 根目录不存在')

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
                raise ValueError('manifest 根节点必须是 JSON object')
            manifests.append((path, data))
        except Exception as exc:
            errors.append({
                'path': path.relative_to(root).as_posix(),
                'error': str(exc),
            })

    if errors:
        error = ValueError('存在无法解析的 manifest.json')
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
        raise FileNotFoundError('图片不存在或格式不支持')
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


LIBRARY_COVER_EXTENSIONS = {'.jpg', '.jpeg', '.png'}


def _site_config_enabled(key, default=True):
    """Read one boolean site setting without exposing it to the browser."""
    conn = get_db_connection()
    row = conn.execute('SELECT value FROM site_config WHERE key = ?', (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    return str(row['value']).strip().lower() in {'1', 'true', 'yes', 'on'}


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
                            counts['directory_count'] += 1
                            stack.append(Path(entry.path))
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
        raise NotADirectoryError('目标不是目录')

    # When disabled, skip cover discovery entirely.  This both avoids filesystem
    # scanning and ensures the frontend never receives a thumbnail URL to request.
    show_folder_covers = _site_config_enabled('show_library_folder_covers', True)

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
        child_rel = child.relative_to(root).as_posix()
        try:
            stat = child.stat()
        except OSError:
            continue

        if child.is_dir():
            stats['directory_count'] += 1
            direct_manifest = _read_manifest(child)
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
    direct_manifest = _read_manifest(target)
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
        raise FileNotFoundError('图片不存在或格式不支持')
    if variant == 'original':
        return target

    stat = target.stat()
    cache_key = hashlib.sha256(f"{source['id']}|{rel}|{stat.st_mtime_ns}|{stat.st_size}|{variant}|library-exif-orientation-v1".encode()).hexdigest()
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


@app.route('/api/library/sources', methods=['GET'])
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


@app.route('/api/library/sources', methods=['POST'])
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


@app.route('/api/library/sources/<int:source_id>', methods=['PUT'])
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
    if not path.is_dir():
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


@app.route('/api/library/sources/<int:source_id>', methods=['DELETE'])
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


@app.route('/api/library/sources/<int:source_id>/browse', methods=['GET'])
def browse_library_source(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        return jsonify(_list_library_directory(source, request.args.get('path', '')))
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/library/sources/<int:source_id>/sets', methods=['POST'])
def create_library_set(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404

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
        return jsonify({'error': f'创建 Set 失败: {exc}'}), 500


@app.route('/api/library/sources/<int:source_id>/manifests', methods=['GET'])
def collect_library_manifests(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        root, _, _ = _resolve_library_path(source, '')
        manifests = _collect_manifest_array(root)
        payload = {'count': len(manifests), 'manifests': manifests}
        # Use json.dumps directly so hand-maintained manifest key order survives
        # the round-trip into the copyable textarea.
        return app.response_class(
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


@app.route('/api/library/sources/<int:source_id>/asset', methods=['GET'])
def get_library_asset(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        file_path = _make_library_variant(source, request.args.get('path', ''), request.args.get('variant', 'compressed'))
        return send_file(file_path)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404




@app.route('/api/library/sources/<int:source_id>/manifest-suggestion', methods=['GET'])
def get_library_manifest_suggestion(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        _, target, _ = _resolve_library_path(source, request.args.get('path', ''))
        if not target.is_dir():
            return jsonify({'error': '目标不是目录'}), 400
        if not _is_set_folder_name(target.name):
            return jsonify({'error': '目标不是 Set 目录'}), 400
        return jsonify(_suggest_manifest(target, include_times=True))
    except (ValueError, FileNotFoundError, NotADirectoryError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/library/sources/<int:source_id>/manifest-reference', methods=['GET'])
def get_library_manifest_reference(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        conn = get_db_connection()
        rows = conn.execute('SELECT root_path FROM library_sources WHERE enabled = 1').fetchall()
        conn.close()
        roots = []
        for row in rows:
            root = Path(row['root_path']).expanduser().resolve()
            if root.is_dir():
                roots.append(root)
        return jsonify(build_manifest_reference_index(roots))
    except (ValueError, FileNotFoundError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/tools/extract-gps', methods=['POST'])
def extract_gps_from_uploaded_photo():
    denied = _library_admin_guard()
    if denied:
        return denied

    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify({'error': '请选择一张照片'}), 400

    # iPhone originals are commonly JPEG or HEIC. ExifTool, when installed,
    # handles HEIC/HEIF; Pillow remains the fallback for supported formats.
    suffix = Path(photo.filename).suffix.lower() or '.img'
    allowed = {'.jpg', '.jpeg', '.heic', '.heif', '.tif', '.tiff', '.png'}
    if suffix not in allowed:
        return jsonify({'error': '仅支持 JPG/JPEG/HEIC/HEIF/TIFF/PNG 照片'}), 400

    if request.content_length and request.content_length > 100 * 1024 * 1024:
        return jsonify({'error': '照片过大，最大支持 100 MB'}), 413

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix='photo-library-gps-', suffix=suffix, delete=False) as temp_file:
            temp_path = temp_file.name
            photo.save(temp_file)

        gps = extract_gps_from_image(temp_path, precision=5)
        return jsonify(gps)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f'读取 GPS 失败: {str(exc)}'}), 500
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.route('/api/library/sources/<int:source_id>/manifest', methods=['PUT'])
def save_library_manifest(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'manifest 必须是 JSON object'}), 400
    # Enforce only the agreed ordering. Custom fields stay exactly where the
    # user inserted them relative to the available slots, and no missing
    # optional field is synthesized.
    payload = _order_manifest_keys(payload)
    try:
        root, target, rel = _resolve_library_path(source, request.args.get('path', ''))
        if not target.is_dir():
            return jsonify({'error': 'manifest 只能保存到目录'}), 400
        manifest_path = target / MANIFEST_FILENAME
        backup_path = target / (MANIFEST_FILENAME + '.bak')
        temp_path = target / (MANIFEST_FILENAME + '.tmp')
        if manifest_path.exists():
            shutil.copy2(manifest_path, backup_path)
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, manifest_path)
        # The backup is only a transactional safety copy. Once the atomic
        # replacement succeeds, remove it so manifest.json.bak never lingers
        # in a healthy Set directory.
        try:
            if backup_path.exists():
                backup_path.unlink()
        except OSError as exc:
            app.logger.warning('manifest saved but backup cleanup failed: %s', exc)
        return jsonify({'success': True, 'path': rel, 'manifest': payload})
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/library/sources/<int:source_id>/image-info', methods=['GET'])
def get_library_image_info(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        return jsonify(_library_image_info(source, request.args.get('path', '')))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/api/library/sources/<int:source_id>/exif', methods=['GET'])
def get_library_image_exif(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        _, target, _ = _resolve_library_path(source, request.args.get('path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('图片不存在')
        return jsonify({'exif': get_image_exif_simple(str(target))})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/api/library/sources/<int:source_id>/favorite', methods=['POST'])
def toggle_library_image_favorite(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    data = request.get_json(silent=True) or {}
    try:
        _, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('图片不存在')
        value = data.get('is_favorited') if 'is_favorited' in data else None
        favorited = _set_library_favorite(source_id, rel, value)
        return jsonify({'is_favorited': favorited})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/library/sources/<int:source_id>/description', methods=['PUT'])
def update_library_image_description(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    data = request.get_json(silent=True) or {}
    try:
        _, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('图片不存在')
        description = str(data.get('description') or '')
        _set_library_description(source_id, rel, description)
        return jsonify({'description': description})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/library/sources/<int:source_id>/rename', methods=['POST'])
def rename_library_image(source_id):
    denied = _library_admin_guard()
    if denied:
        return denied
    source = _get_library_source(source_id)
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    data = request.get_json(silent=True) or {}
    new_filename = str(data.get('new_filename') or '').strip()
    if not new_filename or new_filename in {'.', '..'} or Path(new_filename).name != new_filename or '/' in new_filename or '\\' in new_filename:
        return jsonify({'error': '文件名不合法'}), 400
    try:
        root, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('图片不存在')
        destination = target.with_name(new_filename)
        destination.resolve().relative_to(root)
        if destination.exists() and destination != target:
            return jsonify({'error': '目标文件名已存在'}), 409
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


def _share_contains_path(share, relative_path):
    base = _normalize_relative_path(share['relative_path'])
    requested = _normalize_relative_path(relative_path)
    return requested == base or bool(base and requested.startswith(base + '/'))


def _shared_directory_listing(share, requested_path=None):
    """Return one browsable directory inside a Set share without leaking Source paths."""
    source = _share_source_dict(share)
    base = _normalize_relative_path(share['relative_path'])
    requested = base if requested_path in (None, '') else _normalize_relative_path(requested_path)
    if not _share_contains_path(share, requested):
        raise ValueError('目录不属于该分享 Set')

    listing = _list_library_directory(source, requested)
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
    _, set_dir, _ = _resolve_library_path(source, share['relative_path'])
    manifest = _read_manifest(set_dir)
    if manifest and manifest.get('valid'):
        return manifest.get('data')
    return None


@app.route('/api/library/shares', methods=['POST'])
def create_library_share():
    denied = _library_admin_guard()
    if denied:
        return denied
    from werkzeug.security import generate_password_hash
    data = request.get_json(silent=True) or {}
    source = _get_library_source(data.get('source_id'))
    if not source:
        return jsonify({'error': 'Source 不存在或已禁用'}), 404
    try:
        _, target, rel = _resolve_library_path(source, data.get('relative_path', ''))
        if not target.is_dir() or not _is_set_folder_name(target.name):
            return jsonify({'error': '只能分享完整 Set'}), 400
        counts = _directory_content_counts(target)
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


@app.route('/api/library/shares/<token>/unlock', methods=['POST'])
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


@app.route('/api/library/shares/<token>', methods=['GET'])
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


@app.route('/api/library/shares/<token>/browse', methods=['GET'])
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


@app.route('/api/library/shares/<token>/asset', methods=['GET'])
def get_library_share_asset(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    source = _share_source_dict(share)
    requested = request.args.get('path', '')
    try:
        requested_rel = _normalize_relative_path(requested)
        if not _share_contains_path(share, requested_rel):
            raise ValueError('资源不属于该分享 Set')
        file_path = _make_library_variant(source, requested_rel, request.args.get('variant', 'compressed'))
        return send_file(file_path)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/api/library/shares/<token>/image-info', methods=['GET'])
def get_library_share_image_info(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    source = _share_source_dict(share)
    try:
        requested_rel = _normalize_relative_path(request.args.get('path', ''))
        if not _share_contains_path(share, requested_rel):
            raise ValueError('图片不属于该分享 Set')
        info = _library_image_info(source, requested_rel)
        info['source_type'] = 'library-share'
        info['share_token'] = token
        info['id'] = f"share:{token}:{requested_rel}"
        return jsonify(info)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/api/library/shares/<token>/exif', methods=['GET'])
def get_library_share_exif(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    source = _share_source_dict(share)
    try:
        requested_rel = _normalize_relative_path(request.args.get('path', ''))
        if not _share_contains_path(share, requested_rel):
            raise ValueError('图片不属于该分享 Set')
        _, target, _ = _resolve_library_path(source, requested_rel)
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise FileNotFoundError('图片不存在')
        return jsonify({'exif': get_image_exif_simple(str(target))})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@app.route('/api/library/shares/<token>/selection', methods=['POST'])
def toggle_library_share_selection(token):
    share = _share_row(token)
    if not share or not _share_is_authorized(share):
        return jsonify({'error': '无权访问'}), 401
    if not share['allow_select']:
        return jsonify({'error': '此分享不允许选片'}), 403
    data = request.get_json(silent=True) or {}
    try:
        requested_rel = _normalize_relative_path(data.get('relative_path', ''))
        if not _share_contains_path(share, requested_rel):
            raise ValueError('图片不属于该分享 Set')
        source = _share_source_dict(share)
        _, target, _ = _resolve_library_path(source, requested_rel)
        if not target.is_file() or target.suffix.lower() not in LIBRARY_IMAGE_EXTENSIONS:
            raise ValueError('图片不存在')
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    selected = _set_library_favorite(share['source_id'], requested_rel)
    return jsonify({'selected': selected, 'is_favorited': selected})


# Advanced per-Set workflow tools live in a separate module so the Photo Library core
# remains focused on browsing/state management.
app.register_blueprint(create_workflow_blueprint(
    _library_admin_guard,
    _get_library_source,
    _resolve_library_path,
    get_db_connection,
))

# Final JPEG generation is isolated from the general workflow module so it can
# be removed without touching the existing rename/sync/import tools.
app.register_blueprint(create_final_builder_blueprint(
    _library_admin_guard,
    _get_library_source,
    _resolve_library_path,
))

# Final metadata rebuilding is isolated from JPEG generation so it can be
# removed or revised without changing Final pixel/encoding behavior.
app.register_blueprint(create_final_metadata_blueprint(
    _library_admin_guard,
    _get_library_source,
    _resolve_library_path,
))

# Detail-only derived statistics/equipment scan and Source-root validation live
# outside the manifest so they can be recalculated or discarded safely.
app.register_blueprint(create_set_insights_blueprint(
    _library_admin_guard,
    _get_library_source,
    _resolve_library_path,
    get_db_connection,
))


@app.route('/api/admin/logout', methods=['POST'])
def admin_logout_server():
    session.pop('photo_library_admin', None)
    return jsonify({'success': True})


def add_md5_to_existing_images():
    """为已有图片计算并添加MD5值（一次性运行）"""
    conn = get_db_connection()
    images = conn.execute('SELECT id, filename FROM images WHERE file_hash IS NULL').fetchall()

    for image in images:
        original_path = os.path.join(UPLOAD_FOLDER, image['filename'])
        if os.path.exists(original_path):
            try:
                with open(original_path, 'rb') as f:
                    file_content = f.read()
                    md5_hash = hashlib.md5(file_content).hexdigest()

                conn.execute('UPDATE images SET file_hash = ? WHERE id = ?',
                             (md5_hash, image['id']))
                print(f"Updated MD5 for image {image['id']}")
            except Exception as e:
                print(f"Error processing image {image['id']}: {e}")

    conn.commit()
    conn.close()
    print("MD5 migration completed")


if __name__ == '__main__':
    init_db()

    # 在应用启动时启动工作线程
    worker_thread = threading.Thread(target=image_processing_worker, daemon=True)
    worker_thread.start()

    # add_md5_to_existing_images()
    app.run(debug=False, host='0.0.0.0', port=8081)
