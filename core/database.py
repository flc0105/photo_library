import hashlib
import sqlite3

from core.filesystem import PROJECT_ROOT

DATABASE = str(PROJECT_ROOT / "data" / "photo_library.db")


def _open_database():
    conn = sqlite3.connect(DATABASE)
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


# 初始化数据库
def init_db():
    conn = _open_database()
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
    conn = _open_database()
    conn.row_factory = sqlite3.Row
    return conn
