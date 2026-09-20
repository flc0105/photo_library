#!/usr/bin/env python3
"""Create a small local portrait-library tree and optionally register it in gallery.db.

Example:
  python create_demo_library.py --root /tmp/gallery-demo/Completed --db gallery.db --register
"""
import argparse
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def create_image(path: Path, index: int, label: str, width=1200, height=800):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Synthetic test image: no external download/network dependency.
    img = Image.new('RGB', (width, height), (32 + index * 17 % 180, 58 + index * 29 % 160, 90 + index * 13 % 140))
    draw = ImageDraw.Draw(img)
    draw.rectangle((70, 70, width - 70, height - 70), outline=(235, 235, 235), width=5)
    draw.text((100, 120), 'Gallery Local Source Demo', fill=(255, 255, 255))
    draw.text((100, 175), label, fill=(255, 255, 255))
    draw.text((100, 230), f'Image {index:02d}', fill=(255, 255, 255))
    draw.text((100, height - 130), path.name, fill=(255, 255, 255))
    img.save(path, 'JPEG', quality=90)


def create_set(root: Path, folder: str, date: str, model: str, theme: str, location: str):
    set_dir = root / folder
    dirs = [
        set_dir / '01_Original' / 'JPG',
        set_dir / '01_Original' / 'RAW',
        set_dir / '02_Base_Edit',
        set_dir / '03_Model_Edit',
        set_dir / '04_Revision',
        set_dir / '05_Final',
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    manifest = {
        'schema_version': 1,
        'set_id': str(uuid.uuid4()),
        'title': theme,
        'shoot': {
            'date': date,
            'start_time': '14:00',
            'end_time': '16:30',
            'type': 'Studio' if '室内' in theme else 'Outdoor',
            'location': location,
            'weather': 'Sunny'
        },
        'subjects': [{'name': model, 'role': 'model'}],
        'theme': {
            'name': theme,
            'style': 'Portrait',
            'source_category': '',
            'source_ip': '',
            'character': '',
            'clothing': ''
        },
        'production': {
            'collaboration_type': 'TF',
            'is_lead_photographer': True,
            'model_fee': 0,
            'venue_fee': 0,
            'fee_payer': ''
        },
        'lighting_setup': 'Demo: key light + rim light',
        'props': [],
        'set_props': [],
        'cover': None,
        'notes': '由 create_demo_library.py 自动生成，仅用于本地目录映射测试。',
        'created_at': datetime.now().astimezone().isoformat(timespec='seconds')
    }
    (set_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    for i in range(1, 7):
        create_image(set_dir / '01_Original' / 'JPG' / f'DEMO_{i:04d}.JPG', i, f'{model} / {theme}')
    for i in range(1, 3):
        create_image(set_dir / '05_Final' / f'FINAL_{i:04d}.JPG', i + 10, f'FINAL / {theme}')

    # RAW 目录放占位文件，验证“非预览文件正常列出”，不会伪装成可预览 RAW。
    for i in range(1, 4):
        (set_dir / '01_Original' / 'RAW' / f'DEMO_{i:04d}.ARW').write_bytes(b'DEMO RAW PLACEHOLDER\n')


def register_source(db_path: Path, root: Path, name: str):
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS library_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        INSERT INTO library_sources (name, root_path, enabled)
        VALUES (?, ?, 1)
        ON CONFLICT(root_path) DO UPDATE SET name=excluded.name, enabled=1
    ''', (name, str(root.resolve())))
    conn.commit()
    row = conn.execute('SELECT id, name, root_path FROM library_sources WHERE root_path=?', (str(root.resolve()),)).fetchone()
    conn.close()
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/tmp/gallery-demo/Completed')
    parser.add_argument('--db', default='gallery.db')
    parser.add_argument('--name', default='Demo Completed')
    parser.add_argument('--register', action='store_true')
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    create_set(root, '20260919-测试模特-室内肖像', '2026-09-19', '测试模特', '室内肖像', 'Demo Studio')
    create_set(root, '20260920-测试模特-外景JK', '2026-09-20', '测试模特', '外景JK', 'Demo Park')

    print(f'Demo library created: {root}')
    if args.register:
        row = register_source(Path(args.db), root, args.name)
        print(f'Registered source: id={row[0]} name={row[1]} root={row[2]}')


if __name__ == '__main__':
    main()
