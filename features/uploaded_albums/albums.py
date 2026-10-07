import os
import sqlite3

from flask import Blueprint, jsonify, request, send_file

from core.auth import admin_required, album_token_expire_minutes, generate_auth_token, is_admin_request, verify_auth_token
from core.database import get_db_connection
from core.filesystem import COMPRESSED_FOLDER, THUMBNAIL_FOLDER, UPLOAD_FOLDER
from core.image import generate_compressed, generate_thumbnail, get_image_exif_simple

bp = Blueprint('uploaded_albums', __name__)


@bp.route('/api/albums', methods=['GET'])
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


@bp.route('/api/albums', methods=['POST'])
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
        return jsonify({'error': 'Album name required.'}), 400

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

    return jsonify({'id': album_id, 'message': 'Album created.'})


@bp.route('/api/albums/<int:album_id>', methods=['PUT'])
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
    return jsonify({'message': 'Album saved.'})


@bp.route('/api/albums/<int:album_id>', methods=['DELETE'])
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

    return jsonify({'message': 'Album deleted.'})


@bp.route('/api/albums/<int:album_id>/images', methods=['GET'])
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
        # 管理员登录同时建立 same-origin session；也继续兼容 X-Admin-Token。
        # 如果是管理员，允许访问；否则验证相册密码。
        if not is_admin_request() and (not auth_token or not verify_auth_token(auth_token, album_id)):
            conn.close()
            return jsonify({'error': 'Album access denied.'}), 403

        # if not auth_token or not verify_auth_token(auth_token, album_id):
        #     conn.close()
        #     return jsonify({'error': 'Album access denied.'}), 403

    images = conn.execute('''
        SELECT * FROM images WHERE album_id = ? ORDER BY uploaded_at DESC
    ''', (album_id,)).fetchall()

    return jsonify([dict(image) for image in images])


@bp.route('/api/images/<int:image_id>/file')
def get_image_file(image_id):
    file_type = request.args.get('type', 'compressed')  # compressed, thumbnail, original

    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()
    conn.close()

    if not image:
        return jsonify({'error': 'Photo not found.'}), 404

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
            return jsonify({'error': f'File generation failed: {str(e)}'}), 500

    # 其他情况返回文件不存在
    return jsonify({'error': 'File not found.'}), 404


@bp.route('/api/images/<int:image_id>/exif', methods=['GET'])
def get_image_exif(image_id):
    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if not image:
        conn.close()
        return jsonify({'error': 'Photo not found.'}), 404

    conn.close()

    # 获取原图路径
    original_path = os.path.join(UPLOAD_FOLDER, image['filename'])

    if not os.path.exists(original_path):
        return jsonify({'error': 'Original file not found.'}), 404

    try:
        exif = get_image_exif_simple(original_path)
        return jsonify({'exif': exif}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/images/<int:image_id>/rename', methods=['POST'])
def rename_image(image_id):
    data = request.get_json()
    new_filename = data.get('new_filename')

    if not new_filename:
        return jsonify({'error': 'Filename required.'}), 400

    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if not image:
        conn.close()
        return jsonify({'error': 'Photo not found.'}), 404

    # 检查新文件名是否已存在
    existing = conn.execute('SELECT id FROM images WHERE original_filename = ? AND id != ?',
                            (new_filename, image_id)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Filename already exists.'}), 400

    # 更新数据库
    conn.execute('UPDATE images SET original_filename = ? WHERE id = ?',
                 (new_filename, image_id))
    conn.commit()
    conn.close()

    return jsonify({'message': 'Renamed.'})


@bp.route('/api/images/<int:image_id>/description', methods=['PUT'])
def update_image_description(image_id):
    data = request.get_json()
    description = data.get('description', '')

    conn = get_db_connection()

    # 检查图片是否存在
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()
    if not image:
        conn.close()
        return jsonify({'error': 'Photo not found.'}), 404

    # 更新描述
    conn.execute('UPDATE images SET description = ? WHERE id = ?',
                 (description, image_id))
    conn.commit()
    conn.close()

    return jsonify({'message': 'Description saved.'})


@bp.route('/api/images/<int:image_id>/favorite', methods=['POST'])
def toggle_favorite(image_id):
    conn = get_db_connection()
    image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()

    if not image:
        conn.close()
        return jsonify({'error': 'Photo not found.'}), 404

    # 切换收藏状态
    new_favorite_state = not image['is_favorited']
    conn.execute('UPDATE images SET is_favorited = ? WHERE id = ?',
                 (new_favorite_state, image_id))
    conn.commit()
    conn.close()

    return jsonify({
        'is_favorited': new_favorite_state,
        'message': 'Done.'
    })


@bp.route('/api/images/<int:image_id>', methods=['DELETE'])
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
    return jsonify({'message': 'Photo deleted.'})


@bp.route('/api/albums/<int:album_id>/verify-password', methods=['POST'])
def verify_album_password(album_id):
    data = request.get_json()
    password = data.get('password')

    if not password:
        return jsonify({'error': 'Password required.'}), 400

    conn = get_db_connection()

    # 检查相册是否存在
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_id,)).fetchone()
    if not album:
        conn.close()
        return jsonify({'error': 'Album not found.'}), 404

    # 获取密码哈希
    password_record = conn.execute(
        'SELECT * FROM album_passwords WHERE album_id = ?',
        (album_id,)
    ).fetchone()
    conn.close()

    if not password_record:
        return jsonify({'error': 'Album has no password.'}), 400

    # 简单密码验证（实际应该使用加密哈希）
    token = generate_auth_token(album_id)

    if password_record['password_hash'] == password:
        return jsonify({
            'success': True,
            'message': 'Password verified.',
            'token': token,
            'expires_in': album_token_expire_minutes * 60  # 返回有效期（秒）
        })
    else:
        return jsonify({'error': 'Incorrect password.'}), 401


@bp.route('/api/albums/<int:album_id>/password', methods=['POST'])
def set_album_password(album_id):
    data = request.get_json()
    password = data.get('password')

    if not password:
        return jsonify({'error': 'Password required.'}), 400

    conn = get_db_connection()

    # 检查相册是否存在
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_id,)).fetchone()
    if not album:
        conn.close()
        return jsonify({'error': 'Album not found.'}), 404

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

    return jsonify({'message': 'Password saved.'})


@bp.route('/api/albums/<int:album_id>/password', methods=['DELETE'])
def remove_album_password(album_id):
    conn = get_db_connection()

    # 检查相册是否存在
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_id,)).fetchone()
    if not album:
        conn.close()
        return jsonify({'error': 'Album not found.'}), 404

    # 删除密码记录
    conn.execute('DELETE FROM album_passwords WHERE album_id = ?', (album_id,))
    conn.commit()
    conn.close()

    return jsonify({'message': 'Password removed.'})


@bp.route('/api/albums/<int:album_id>/has-password')
def check_album_password(album_id):
    conn = get_db_connection()

    password_record = conn.execute(
        'SELECT id FROM album_passwords WHERE album_id = ?',
        (album_id,)
    ).fetchone()
    conn.close()

    return jsonify({'has_password': password_record is not None})


@bp.route('/api/albums/<int:album_id>/verify-token', methods=['POST'])
def verify_album_token(album_id):
    data = request.get_json()
    token = data.get('token')

    if not token:
        return jsonify({'valid': False, 'error': 'Token required.'}), 400

    # 使用之前的verify_auth_token函数验证
    if verify_auth_token(token, album_id):
        return jsonify({'valid': True, 'message': 'Token valid.'})
    else:
        return jsonify({'valid': False, 'error': 'Token invalid or expired.'})


@bp.route('/api/images/move', methods=['POST'])
def move_images():
    data = request.get_json()
    image_ids = data.get('image_ids', [])
    target_album_id = data.get('target_album_id')

    if not image_ids:
        return jsonify({'error': 'Select photos to move.'}), 400

    if not target_album_id:
        return jsonify({'error': 'Select a destination album.'}), 400

    conn = get_db_connection()

    try:
        # 检查目标相册是否存在
        target_album = conn.execute('SELECT id FROM albums WHERE id = ?', (target_album_id,)).fetchone()
        if not target_album:
            conn.close()
            return jsonify({'error': 'Destination album not found.'}), 404

        # 检查所有图片是否存在
        placeholders = ','.join(['?'] * len(image_ids))
        existing_images = conn.execute(f'''
            SELECT id, album_id, filename FROM images WHERE id IN ({placeholders})
        ''', image_ids).fetchall()

        if len(existing_images) != len(image_ids):
            conn.close()
            return jsonify({'error': 'Some photos were not found.'}), 404

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
            'message': f'Moved {moved_count} photos.',
            'moved_count': moved_count,
            'total_count': len(image_ids)
        })

    except Exception as e:
        conn.close()
        return jsonify({'error': f'Move failed: {str(e)}'}), 500


@bp.route('/api/album-groups', methods=['GET'])
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
            'name': 'Ungrouped',
            'sort_order': 9999,  # 排在最后
            'album_count': len(ungrouped_albums),
            'albums': [dict(album) for album in ungrouped_albums],
            'is_ungrouped': True  # 添加标记
        })

    conn.close()
    return jsonify(result)


@bp.route('/api/album-groups', methods=['POST'])
def create_album_group():
    data = request.get_json()
    name = data.get('name')
    sort_order = data.get('sort_order', 0)

    if not name:
        return jsonify({'error': 'Group name required.'}), 400

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
        return jsonify({'error': 'Group name already exists.'}), 400

    conn.close()
    return jsonify({'id': group_id, 'message': 'Group created.'})


@bp.route('/api/album-groups/<int:group_id>', methods=['PUT'])
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
            return jsonify({'error': 'Group name already exists.'}), 400

    conn.close()
    return jsonify({'message': 'Group saved.'})


@bp.route('/api/album-groups/<int:group_id>', methods=['DELETE'])
def delete_album_group(group_id):
    data = request.get_json() if request.data else {}
    move_to_ungrouped = data.get('move_to_ungrouped', True)  # 默认移到未分组

    conn = get_db_connection()

    # 检查分组是否存在
    group = conn.execute('SELECT * FROM album_groups WHERE id = ?', (group_id,)).fetchone()
    if not group:
        conn.close()
        return jsonify({'error': 'Group not found.'}), 404

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
                'message': 'Group deleted. Albums moved to Ungrouped.',
                'affected_albums': album_id_list,
                'album_count': len(album_id_list)
            })
        else:
            return jsonify({
                'message': 'Group deleted.',
                'affected_albums': album_id_list,
                'album_count': len(album_id_list)
            })

    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': f'Delete failed: {str(e)}'}), 500


@bp.route('/api/albums/<int:album_id>/groups', methods=['POST'])
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
        return jsonify({'message': 'Groups saved.'})
    except Exception as e:
        conn.close()
        return jsonify({'error': f'Save failed: {str(e)}'}), 500


@bp.route('/api/albums/<int:album_id>/groups', methods=['GET'])
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
