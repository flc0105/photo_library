import hashlib
import os
import queue
import shutil
import tempfile
import threading
from datetime import datetime

from PIL import Image
from flask import Blueprint, jsonify, request

from core.auth import is_admin_request
from core.database import get_db_connection
from core.filesystem import COMPRESSED_FOLDER, THUMBNAIL_FOLDER, UPLOAD_FOLDER
from core.image import generate_compressed, generate_thumbnail

bp = Blueprint('uploaded_album_upload', __name__)

image_processing_queue = queue.Queue()
processing_lock = threading.Lock()
current_processing = {}


@bp.route('/api/albums/<int:album_id>/images', methods=['POST'])
def upload_image(album_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected.'}), 400


    #config start

    # 检查上传权限
    # Element Plus upload uses XHR rather than the page's fetch wrapper, so a
    # logged-in same-origin admin must be recognized through the admin session
    # as well as the legacy X-Admin-Token header.
    if not is_admin_request():
        conn = get_db_connection()
        config = conn.execute(
            'SELECT value FROM site_config WHERE key = ?',
            ('allow_guest_upload',)
        ).fetchone()
        conn.close()

        if not config or config['value'] != '1':
            return jsonify({'error': 'Guest uploads disabled.'}), 403

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
                'error': f'Photo already exists in album "{existing_image["album_name"]}".',
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
            'message': 'Queued.',
            'filename': filename,
            'queue_position': image_processing_queue.qsize(),
            'immediate_response': True
        })

    except Exception as e:
        # 清理临时文件
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


@bp.route('/api/upload/status/<filename>', methods=['GET'])
def get_upload_status(filename):
    """获取图片处理状态"""
    with processing_lock:
        if filename in current_processing:
            return jsonify({
                'status': 'processing',
                'message': 'Processing…'
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


def start_upload_worker():
    worker_thread = threading.Thread(target=image_processing_worker, daemon=True)
    worker_thread.start()
    return worker_thread
