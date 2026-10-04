import math
from collections import defaultdict
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.smart_album_runtime import _validate_script, run_explore_block, run_query, run_set_query
from core.smart_albums import (
    PHOTO_CONTRACT_GROUPS,
    SMART_ALBUM_DB_FILENAME,
    SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    _connect,
    _asset_payloads,
    _discover_sets,
    _index_status,
    _now_iso,
)
from core.smart_sets import SMART_SET_CONTRACT_GROUPS, _manifest_info, _set_candidates, _shoot_date, _source_scope


_MISSING_LABEL = '未记录'
_SET_DIMENSIONS = {'year', 'year_month', 'model', 'environment', 'theme', 'location'}
_EXPLORE_BLOCK_ENGINE_VERSION = 2
_EXPLORE_BLOCK_METRICS = {'sets', 'photos'}
DEFAULT_EXPLORE_BLOCK_CODE = '''result = group_sets(
    sets,
    key=lambda item: item.manifest.model,
    include_missing=True,
)
'''



def _init_explore_db(db_path):
    conn = _connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS explore_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            python_code TEXT NOT NULL,
            default_metric TEXT NOT NULL DEFAULT 'sets',
            presentation TEXT NOT NULL DEFAULT 'list',
            display_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            engine_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def _block_dict(row):
    if not row:
        return None
    data = dict(row)
    data['enabled'] = bool(data.get('enabled'))
    data['default_metric'] = 'photos' if data.get('default_metric') == 'photos' else 'sets'
    data['presentation'] = data.get('presentation') or 'list'
    return data


def _block_row(conn, block_id):
    return conn.execute('SELECT * FROM explore_blocks WHERE id=?', (block_id,)).fetchone()


def _list_block_rows(smart_db_path, *, enabled_only=False):
    conn = _connect(smart_db_path)
    where = 'WHERE enabled=1' if enabled_only else ''
    rows = conn.execute(
        f'SELECT * FROM explore_blocks {where} ORDER BY display_order ASC, id ASC'
    ).fetchall()
    conn.close()
    return rows


def _next_block_order(conn):
    row = conn.execute('SELECT COALESCE(MAX(display_order), 0) AS max_order FROM explore_blocks').fetchone()
    return int(row['max_order'] or 0) + 10


def _normalize_block_metric(value):
    metric = str(value or 'sets').strip().lower()
    if metric not in _EXPLORE_BLOCK_METRICS:
        raise ValueError('默认查看只能是 sets 或 photos。')
    return metric


def _block_card_payload(row, execution, total_sets):
    block = _block_dict(row)
    default_metric = block['default_metric']
    source_kind = execution.get('kind') or 'sets'
    population_set_ids = list(execution.get('population_set_ids') or [])
    population_photo_ids = list(execution.get('population_photo_ids') or [])
    set_total = len(population_set_ids) if source_kind == 'sets' else int(total_sets)
    image_total = len(population_photo_ids)

    rows = []
    for bucket in execution.get('buckets') or []:
        set_count = len(bucket.get('set_ids') or [])
        image_count = len(bucket.get('photo_ids') or [])
        row_payload = {
            'bucket_id': str(bucket.get('bucket_id') or ''),
            'value': bucket.get('value'),
            'label': str(bucket.get('label') or _MISSING_LABEL),
            'set_count': set_count,
            'image_count': image_count,
            'set_percentage': _percentage(set_count, set_total),
            'image_percentage': _percentage(image_count, image_total),
            'primary_metric': 'photo' if default_metric == 'photos' else 'set',
        }
        row_payload['count'] = image_count if default_metric == 'photos' else set_count
        row_payload['percentage'] = row_payload['image_percentage'] if default_metric == 'photos' else row_payload['set_percentage']
        rows.append(row_payload)

    metric_field = 'image_count' if default_metric == 'photos' else 'set_count'
    rows.sort(key=lambda item: (-int(item[metric_field]), str(item['label']).casefold()))
    return {
        'id': block['id'],
        'name': block['name'],
        'description': block['description'],
        'default_metric': default_metric,
        'display_order': block['display_order'],
        'enabled': block['enabled'],
        'presentation': block['presentation'],
        'source_kind': source_kind,
        'rows': rows,
        'error': '',
    }


def _run_block_card(row, set_payloads, photo_payloads, total_sets):
    execution = run_explore_block(
        row['python_code'],
        set_payloads,
        photo_payloads,
        timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    )
    return _block_card_payload(row, execution, total_sets)


def _block_disabled_payload(row):
    block = _block_dict(row)
    return {
        'id': block['id'],
        'name': block['name'],
        'description': block['description'],
        'default_metric': block['default_metric'],
        'display_order': block['display_order'],
        'enabled': False,
        'presentation': block['presentation'],
        'source_kind': None,
        'rows': [],
        'error': '',
    }


def _block_error_payload(row, exc):
    block = _block_dict(row)
    return {
        'id': block['id'],
        'name': block['name'],
        'description': block['description'],
        'default_metric': block['default_metric'],
        'display_order': block['display_order'],
        'enabled': block['enabled'],
        'presentation': block['presentation'],
        'source_kind': None,
        'rows': [],
        'error': str(exc) or 'Explore Block 执行失败',
    }


def _block_execution_context(smart_db_path, main_db_path):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')
    photo_payloads, photo_rows = _asset_payloads(smart_db_path, main_db_path)
    set_payloads, set_rows, set_status = _set_candidates(
        smart_db_path,
        main_db_path,
        photo_payloads=photo_payloads,
    )
    return photo_payloads, photo_rows, set_payloads, set_rows, set_status




def _clean_text(value):
    text = str(value or '').strip()
    return text or None


def _percentage(count, total):
    if total <= 0:
        return 0.0
    return round((count / total) * 100.0, 1)


def _focal_value(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 1)


def _focal_label(value):
    if value is None:
        return _MISSING_LABEL
    number = float(value)
    if number.is_integer():
        return f'{int(number)}mm'
    return f'{number:g}mm'


def _photo_set_key(photo):
    source = photo.get('source') or {}
    set_info = photo.get('set') or {}
    source_id = source.get('id')
    set_path = str(set_info.get('path') or '')
    if source_id is None or not set_path:
        return None
    return int(source_id), set_path


def _raw_shoot_date(payload):
    value = payload.get('shoot_date')
    if isinstance(value, dict):
        return str(value.get('__smart_date__') or '').strip()
    return str(value or '').strip()


def _explore_set_candidates(smart_db_path, main_db_path, photo_payloads=None):
    """Build lightweight real-Set candidates for Explore.

    Unlike Smart Set's full contract builder, Explore does not need stage counts or
    favorite aggregation just to calculate statistics. It still reuses exactly the
    same indexed/enabled/mounted Source scope and the same Set discovery/manifest
    rules, while grouping the existing Smart Album photo candidates under each Set.
    """
    sources, status = _source_scope(main_db_path, smart_db_path)
    if photo_payloads is None:
        photo_payloads, _ = _asset_payloads(smart_db_path, main_db_path)

    photos_by_set = defaultdict(list)
    for photo in photo_payloads:
        key = _photo_set_key(photo)
        if key is not None:
            photos_by_set[key].append(photo)

    payloads = []
    rows = {}
    for source in sources:
        source_id = int(source['id'])
        root = Path(source['root_path']).expanduser().resolve()
        for set_dir in _discover_sets(root):
            set_path = set_dir.relative_to(root).as_posix()
            manifest, _, _ = _manifest_info(root, set_path)
            shoot_date = _shoot_date(manifest, set_dir.name)
            set_id = f'library-set:{source_id}:{set_path}'
            photo_items = photos_by_set.get((source_id, set_path), [])
            payload = {
                'id': set_id,
                'source': {'id': source_id, 'name': source['name']},
                'name': set_dir.name,
                'path': set_path,
                'manifest': manifest,
                'shoot_date': {'__smart_date__': shoot_date} if shoot_date else None,
                'photos': photo_items,
                'indexed_photo_count': len(photo_items),
            }
            payloads.append(payload)
            rows[set_id] = {
                'id': set_id,
                'source_id': source_id,
                'source_name': source['name'],
                'name': set_dir.name,
                'set_path': set_path,
                'shoot_date': shoot_date,
                'model': str(manifest.get('model') or '').strip(),
                'indexed_photo_count': len(photo_items),
            }

    return payloads, rows, status, sources


def _set_value(payload, dimension):
    manifest = payload.get('manifest') if isinstance(payload.get('manifest'), dict) else {}
    shoot = manifest.get('shoot') if isinstance(manifest.get('shoot'), dict) else {}
    theme = manifest.get('theme') if isinstance(manifest.get('theme'), dict) else {}
    location = manifest.get('location') if isinstance(manifest.get('location'), dict) else {}

    if dimension == 'model':
        return _clean_text(manifest.get('model'))
    if dimension == 'environment':
        return _clean_text(shoot.get('environment'))
    if dimension == 'theme':
        return _clean_text(theme.get('source_title'))
    if dimension == 'location':
        return _clean_text(location.get('name'))
    raise ValueError('不支持的 Set 统计维度。')


def _is_cosplay_set(payload):
    manifest = payload.get('manifest') if isinstance(payload.get('manifest'), dict) else {}
    theme = manifest.get('theme') if isinstance(manifest.get('theme'), dict) else {}
    return str(theme.get('genre') or '').strip().casefold() == 'cosplay'


def _set_metric_rows(set_payloads, total_sets, total_images, dimension):
    buckets = defaultdict(lambda: {'set_count': 0, 'image_count': 0})
    for item in set_payloads:
        value = _set_value(item, dimension)
        bucket = buckets[value]
        bucket['set_count'] += 1
        bucket['image_count'] += int(item.get('indexed_photo_count') or 0)

    rows = []
    for value, counts in buckets.items():
        set_count = counts['set_count']
        image_count = counts['image_count']
        label = _MISSING_LABEL if value is None else str(value)
        rows.append({
            'value': value,
            'label': label,
            'set_count': set_count,
            'image_count': image_count,
            'set_percentage': _percentage(set_count, total_sets),
            'image_percentage': _percentage(image_count, total_images),
            # Compatibility aliases describe the primary metric of this row.
            'count': set_count,
            'percentage': _percentage(set_count, total_sets),
            'primary_metric': 'set',
        })
    rows.sort(key=lambda row: (-row['set_count'], row['label'].casefold()))
    return rows


def _year_rows(set_payloads, total_sets, total_images):
    years = defaultdict(lambda: {'set_count': 0, 'image_count': 0, 'months': defaultdict(lambda: {'set_count': 0, 'image_count': 0})})
    for item in set_payloads:
        shoot_date = _raw_shoot_date(item)
        if len(shoot_date) < 7:
            continue
        try:
            year = int(shoot_date[:4])
            month = int(shoot_date[5:7])
        except (TypeError, ValueError):
            continue
        if not (1 <= month <= 12):
            continue
        image_count = int(item.get('indexed_photo_count') or 0)
        years[year]['set_count'] += 1
        years[year]['image_count'] += image_count
        years[year]['months'][month]['set_count'] += 1
        years[year]['months'][month]['image_count'] += image_count

    output = []
    for year, data in years.items():
        year_set_count = data['set_count']
        year_image_count = data['image_count']
        months = []
        for month, counts in data['months'].items():
            set_count = counts['set_count']
            image_count = counts['image_count']
            months.append({
                'value': {'year': year, 'month': month},
                'label': f'{month}月',
                'set_count': set_count,
                'image_count': image_count,
                # Month percentages describe the selected year, so the visible months
                # form a complete distribution instead of a fraction of the whole library.
                'set_percentage': _percentage(set_count, year_set_count),
                'image_percentage': _percentage(image_count, year_image_count),
                'count': set_count,
                'percentage': _percentage(set_count, year_set_count),
                'primary_metric': 'set',
            })
        months.sort(key=lambda row: (-row['set_count'], row['value']['month']))
        output.append({
            'value': year,
            'label': str(year),
            'set_count': year_set_count,
            'image_count': year_image_count,
            'set_percentage': _percentage(year_set_count, total_sets),
            'image_percentage': _percentage(year_image_count, total_images),
            'count': year_set_count,
            'percentage': _percentage(year_set_count, total_sets),
            'primary_metric': 'set',
            'months': months,
        })

    # The selector is chronological rather than ranked; rows inside each year remain
    # ranked by Set count, as do all other statistic cards.
    output.sort(key=lambda row: int(row['value']), reverse=True)
    return output


def _focal_rows(photo_payloads, total_sets, total_images, valid_set_keys=None):
    buckets = defaultdict(lambda: {'image_count': 0, 'set_keys': set()})
    for photo in photo_payloads:
        capture = photo.get('capture') or {}
        value = _focal_value(capture.get('focal_length_mm'))
        bucket = buckets[value]
        bucket['image_count'] += 1
        key = _photo_set_key(photo)
        if key is not None and (valid_set_keys is None or key in valid_set_keys):
            bucket['set_keys'].add(key)

    rows = []
    for value, data in buckets.items():
        image_count = data['image_count']
        set_count = len(data['set_keys'])
        label = _focal_label(value)
        rows.append({
            'value': value,
            'label': label,
            'set_count': set_count,
            'image_count': image_count,
            'set_percentage': _percentage(set_count, total_sets),
            'image_percentage': _percentage(image_count, total_images),
            'count': image_count,
            'percentage': _percentage(image_count, total_images),
            'primary_metric': 'photo',
        })
    rows.sort(key=lambda row: (-row['image_count'], row['label'].casefold()))
    return rows


def _build_stats(smart_db_path, main_db_path):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')

    photo_payloads, _ = _asset_payloads(smart_db_path, main_db_path)
    set_payloads, _, set_status, sources = _explore_set_candidates(
        smart_db_path,
        main_db_path,
        photo_payloads=photo_payloads,
    )
    total_images = len(photo_payloads)
    total_sets = len(set_payloads)
    cosplay_sets = [item for item in set_payloads if _is_cosplay_set(item)]
    cosplay_images = sum(int(item.get('indexed_photo_count') or 0) for item in cosplay_sets)

    custom_blocks = []
    block_rows = _list_block_rows(smart_db_path)
    enabled_block_rows = [row for row in block_rows if bool(row['enabled'])]
    block_set_payloads = None
    if enabled_block_rows:
        # Only enabled Blocks need the full Smart Set contract. Build it once
        # and share it across enabled Blocks; disabled Blocks remain visible but
        # execute no Python when Explore opens.
        block_set_payloads, _, _ = _set_candidates(
            smart_db_path,
            main_db_path,
            photo_payloads=photo_payloads,
        )
    for block_row in block_rows:
        if not bool(block_row['enabled']):
            custom_blocks.append(_block_disabled_payload(block_row))
            continue
        try:
            custom_blocks.append(_run_block_card(block_row, block_set_payloads, photo_payloads, total_sets))
        except Exception as exc:
            custom_blocks.append(_block_error_payload(block_row, exc))

    return {
        'total_images': total_images,
        'total_sets': total_sets,
        'sources': [
            {'id': int(source['id']), 'name': source['name']}
            for source in sorted(sources, key=lambda item: str(item['name']).casefold())
        ],
        'years': _year_rows(set_payloads, total_sets, total_images),
        'models': _set_metric_rows(set_payloads, total_sets, total_images, 'model'),
        'themes': _set_metric_rows(cosplay_sets, len(cosplay_sets), cosplay_images, 'theme'),
        'locations': _set_metric_rows(set_payloads, total_sets, total_images, 'location'),
        'custom_blocks': custom_blocks,
        'index': set_status,
    }


def _set_query_code(dimension, value):
    if dimension == 'year':
        if value is None:
            return 'result = [set for set in sets if set.shoot_date is None]\n'
        year = int(value)
        return (
            'result = [\n'
            '    set\n'
            '    for set in sets\n'
            f'    if set.shoot_date is not None and set.shoot_date.year == {year}\n'
            ']\n'
        )

    if dimension == 'year_month':
        if not isinstance(value, dict):
            raise ValueError('月份筛选参数无效。')
        year = int(value.get('year'))
        month = int(value.get('month'))
        if month < 1 or month > 12:
            raise ValueError('月份筛选参数无效。')
        return (
            'result = [\n'
            '    set\n'
            '    for set in sets\n'
            '    if set.shoot_date is not None\n'
            f'    and set.shoot_date.year == {year}\n'
            f'    and set.shoot_date.month == {month}\n'
            ']\n'
        )

    if dimension == 'theme':
        expression = 'set.manifest.theme.source_title'
        genre_condition = 'str(set.manifest.theme.genre or "").strip().casefold() == "cosplay"'
        if value is None:
            condition = f'not str({expression} or "").strip()'
        else:
            target = repr(str(value).strip())
            condition = f'str({expression} or "").strip() == {target}'
        return f'result = [set for set in sets if {genre_condition} and {condition}]\n'

    text_fields = {
        'model': 'set.manifest.model',
        'environment': 'set.manifest.shoot.environment',
        'location': 'set.manifest.location.name',
    }
    if dimension in text_fields:
        expression = text_fields[dimension]
        if value is None:
            condition = f'not str({expression} or "").strip()'
        else:
            target = repr(str(value).strip())
            condition = f'str({expression} or "").strip() == {target}'
        return f'result = [set for set in sets if {condition}]\n'

    if dimension == 'focal_length':
        if value is None:
            condition = 'photo.capture.focal_length_mm is None'
        else:
            target = round(float(value), 1)
            condition = (
                'photo.capture.focal_length_mm is not None '
                f'and round(float(photo.capture.focal_length_mm), 1) == {target!r}'
            )
        return (
            'result = [\n'
            '    set\n'
            '    for set in sets\n'
            f'    if any({condition} for photo in set.photos)\n'
            ']\n'
        )

    raise ValueError('不支持的 Explore 统计维度。')


def _photo_query_code(dimension, value, matching_set_rows=None):
    if dimension in _SET_DIMENSIONS:
        rows = list(matching_set_rows or [])
        if not rows:
            return 'result = []\n'
        keys = {
            (int(row['source_id']), str(row['set_path']))
            for row in rows
            if row.get('source_id') is not None and row.get('set_path')
        }
        return (
            f'keys = {keys!r}\n'
            'result = [photo for photo in photos if set_key(photo) in keys]\n'
        )

    if dimension == 'focal_length':
        if value is None:
            return 'result = [photo for photo in photos if photo.capture.focal_length_mm is None]\n'
        target = round(float(value), 1)
        return (
            'result = [\n'
            '    photo\n'
            '    for photo in photos\n'
            '    if photo.capture.focal_length_mm is not None\n'
            f'    and round(float(photo.capture.focal_length_mm), 1) == {target!r}\n'
            ']\n'
        )

    raise ValueError('不支持的 Explore 统计维度。')


def _query_title(dimension, label):
    section_names = {
        'year': '年度',
        'year_month': '月份',
        'model': '模特',
        'environment': '环境',
        'theme': '题材',
        'location': '地点',
        'focal_length': '焦段',
    }
    section = section_names.get(dimension, 'Explore')
    display = str(label or _MISSING_LABEL)
    return f'{section} · {display}'


def create_explore_blueprint(admin_guard, main_db_path):
    """Create Explore statistics and temporary Smart Album/Smart Set drill-downs.

    Photo statistics reuse the current Smart Album candidate pool. Set statistics
    discover real Sets from the same indexed/enabled/mounted Source scope. Custom
    Explore Block definitions are persisted in smart_albums.db, while drill-down
    queries remain temporary and Explore owns no second photo index.
    """
    main_db_path = Path(main_db_path).resolve()
    smart_db_path = main_db_path.parent / SMART_ALBUM_DB_FILENAME
    _init_explore_db(smart_db_path)
    bp = Blueprint('explore', __name__)

    def guard():
        return admin_guard()

    @bp.route('/api/explore/blocks', methods=['GET'])
    def list_explore_blocks():
        denied = guard()
        if denied:
            return denied
        return jsonify({'blocks': [_block_dict(row) for row in _list_block_rows(smart_db_path)]})

    @bp.route('/api/explore/blocks', methods=['POST'])
    def create_explore_block():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '')
        python_code = str(data.get('python_code') or DEFAULT_EXPLORE_BLOCK_CODE)
        enabled = 1 if data.get('enabled', True) else 0
        if not name:
            return jsonify({'error': '统计名称不能为空。'}), 400
        try:
            default_metric = _normalize_block_metric(data.get('default_metric'))
            _validate_script(python_code)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400
        now = _now_iso()
        conn = _connect(smart_db_path)
        display_order = _next_block_order(conn)
        cursor = conn.execute(
            '''INSERT INTO explore_blocks
               (name, description, python_code, default_metric, presentation,
                display_order, enabled, engine_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'list', ?, ?, ?, ?, ?)''',
            (name, description, python_code, default_metric, display_order, enabled,
             _EXPLORE_BLOCK_ENGINE_VERSION, now, now),
        )
        block_id = cursor.lastrowid
        conn.commit()
        row = _block_row(conn, block_id)
        conn.close()
        return jsonify({'block': _block_dict(row)}), 201

    @bp.route('/api/explore/blocks/<int:block_id>', methods=['PUT'])
    def update_explore_block(block_id):
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        conn = _connect(smart_db_path)
        existing = _block_row(conn, block_id)
        if not existing:
            conn.close()
            return jsonify({'error': 'Explore Block 不存在。'}), 404
        name = str(data.get('name', existing['name']) or '').strip()
        description = str(data.get('description', existing['description']) or '')
        python_code = str(data.get('python_code', existing['python_code']) or '')
        enabled = 1 if data.get('enabled', bool(existing['enabled'])) else 0
        try:
            default_metric = _normalize_block_metric(data.get('default_metric', existing['default_metric']))
            if not name:
                raise ValueError('统计名称不能为空。')
            _validate_script(python_code)
        except Exception as exc:
            conn.close()
            return jsonify({'error': str(exc)}), 400
        conn.execute(
            '''UPDATE explore_blocks
               SET name=?, description=?, python_code=?, default_metric=?, enabled=?,
                   engine_version=?, updated_at=?
               WHERE id=?''',
            (name, description, python_code, default_metric, enabled,
             _EXPLORE_BLOCK_ENGINE_VERSION, _now_iso(), block_id),
        )
        conn.commit()
        row = _block_row(conn, block_id)
        conn.close()
        return jsonify({'block': _block_dict(row)})

    @bp.route('/api/explore/blocks/<int:block_id>', methods=['DELETE'])
    def delete_explore_block(block_id):
        denied = guard()
        if denied:
            return denied
        conn = _connect(smart_db_path)
        row = _block_row(conn, block_id)
        if not row:
            conn.close()
            return jsonify({'error': 'Explore Block 不存在。'}), 404
        conn.execute('DELETE FROM explore_blocks WHERE id=?', (block_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @bp.route('/api/explore/blocks/preview', methods=['POST'])
    def preview_explore_block():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        python_code = str(data.get('python_code') or '')
        try:
            default_metric = _normalize_block_metric(data.get('default_metric'))
            _validate_script(python_code)
            photo_payloads, _, set_payloads, _, status = _block_execution_context(smart_db_path, main_db_path)
            execution = run_explore_block(
                python_code,
                set_payloads,
                photo_payloads,
                timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
            )
            fake_row = {
                'id': 0,
                'name': str(data.get('name') or '预览').strip() or '预览',
                'description': str(data.get('description') or ''),
                'python_code': python_code,
                'default_metric': default_metric,
                'presentation': 'list',
                'display_order': 0,
                'enabled': 1,
                'engine_version': _EXPLORE_BLOCK_ENGINE_VERSION,
                'created_at': '',
                'updated_at': '',
            }
            card = _block_card_payload(fake_row, execution, len(set_payloads))
            return jsonify({'card': card, 'index': status})
        except Exception as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Explore 使用 Smart Album 索引；请先建立索引。',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                }), 409
            return jsonify({'error': str(exc), 'traceback': getattr(exc, 'smart_traceback', '')}), 400

    @bp.route('/api/explore/runtime', methods=['GET'])
    def explore_runtime_contract():
        denied = guard()
        if denied:
            return denied
        return jsonify({
            'engine_version': _EXPLORE_BLOCK_ENGINE_VERSION,
            'default_code': DEFAULT_EXPLORE_BLOCK_CODE,
            'helpers': [
                'group_sets(items, key, many=False, label=None, missing="未记录", include_missing=True)',
                'group_photos(items, key, many=False, label=None, missing="未记录", include_missing=True)',
                'preferred_versions(items)',
                'finals(items)',
            ],
            'set_contract_groups': SMART_SET_CONTRACT_GROUPS,
            'photo_contract_groups': PHOTO_CONTRACT_GROUPS,
        })

    @bp.route('/api/explore/stats', methods=['GET'])
    def explore_stats():
        denied = guard()
        if denied:
            return denied
        try:
            return jsonify(_build_stats(smart_db_path, main_db_path))
        except Exception as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Explore 使用 Smart Album 索引；请先建立索引。',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                }), 409
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/explore/query', methods=['POST'])
    def explore_query():
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        dimension = str(data.get('dimension') or '').strip()
        value = data.get('value')
        label = data.get('label')
        target = str(data.get('target') or 'photos').strip().lower()
        if target not in {'photos', 'sets'}:
            return jsonify({'error': 'Explore 查询目标无效。'}), 400

        try:
            status = _index_status(smart_db_path)
            if not status['ready']:
                raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')

            photo_payloads, result_rows = _asset_payloads(smart_db_path, main_db_path)
            set_payloads, set_rows, status, _ = _explore_set_candidates(
                smart_db_path,
                main_db_path,
                photo_payloads=photo_payloads,
            )

            set_code = _set_query_code(dimension, value)
            ordered_set_ids = run_set_query(
                set_code,
                set_payloads,
                timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
            )
            matching_set_rows = [set_rows[item_id] for item_id in ordered_set_ids if item_id in set_rows]

            title = _query_title(dimension, label)
            if target == 'sets':
                return jsonify({
                    'title': title,
                    'dimension': dimension,
                    'value': value,
                    'result_type': 'sets',
                    'sets': [dict(row) for row in matching_set_rows],
                    'count': len(matching_set_rows),
                    'index': status,
                })

            photo_code = _photo_query_code(dimension, value, matching_set_rows=matching_set_rows)
            ordered_ids = run_query(
                photo_code,
                photo_payloads,
                timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
            )
            images = [dict(result_rows[photo_id]) for photo_id in ordered_ids if photo_id in result_rows]
            return jsonify({
                'title': title,
                'dimension': dimension,
                'value': value,
                'result_type': 'photos',
                'images': images,
                'count': len(images),
                'index': status,
            })
        except Exception as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Explore 使用 Smart Album 索引；请先建立索引。',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                    'traceback': '',
                }), 409
            return jsonify({
                'error': str(exc),
                'traceback': getattr(exc, 'smart_traceback', ''),
            }), 400

    @bp.route('/api/explore/blocks/<int:block_id>/query', methods=['POST'])
    def query_explore_block(block_id):
        denied = guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        bucket_id = str(data.get('bucket_id') or '')
        target = str(data.get('target') or 'sets').strip().lower()
        if not bucket_id:
            return jsonify({'error': 'Explore bucket 无效。'}), 400
        if target not in {'sets', 'photos'}:
            return jsonify({'error': 'Explore 查询目标无效。'}), 400

        conn = _connect(smart_db_path)
        row = _block_row(conn, block_id)
        conn.close()
        if not row:
            return jsonify({'error': 'Explore Block 不存在。'}), 404
        if not bool(row['enabled']):
            return jsonify({'error': '该统计已停用，请先启用后再查询。'}), 409

        try:
            photo_payloads, photo_rows, set_payloads, set_rows, status = _block_execution_context(
                smart_db_path,
                main_db_path,
            )
            execution = run_explore_block(
                row['python_code'],
                set_payloads,
                photo_payloads,
                timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
            )
            bucket = next(
                (item for item in (execution.get('buckets') or []) if str(item.get('bucket_id') or '') == bucket_id),
                None,
            )
            if bucket is None:
                return jsonify({'error': '该统计项已不存在，请刷新 Explore 后重试。'}), 409

            label = str(bucket.get('label') or _MISSING_LABEL)
            title = f"{row['name']} · {label}"
            if target == 'sets':
                ordered_ids = list(bucket.get('set_ids') or [])
                results = [dict(set_rows[item_id]) for item_id in ordered_ids if item_id in set_rows]
                return jsonify({
                    'title': title,
                    'block_id': block_id,
                    'bucket_id': bucket_id,
                    'result_type': 'sets',
                    'sets': results,
                    'count': len(results),
                    'index': status,
                })

            ordered_ids = list(bucket.get('photo_ids') or [])
            images = [dict(photo_rows[item_id]) for item_id in ordered_ids if item_id in photo_rows]
            return jsonify({
                'title': title,
                'block_id': block_id,
                'bucket_id': bucket_id,
                'result_type': 'photos',
                'images': images,
                'count': len(images),
                'index': status,
            })
        except Exception as exc:
            if str(exc) == 'SMART_ALBUM_INDEX_REQUIRED':
                return jsonify({
                    'error': 'Explore 使用 Smart Album 索引；请先建立索引。',
                    'code': 'smart_album_index_required',
                    'index': _index_status(smart_db_path),
                    'traceback': '',
                }), 409
            return jsonify({
                'error': str(exc),
                'traceback': getattr(exc, 'smart_traceback', ''),
            }), 400

    return bp
