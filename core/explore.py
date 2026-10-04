import math
from collections import defaultdict
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.smart_album_runtime import run_query, run_set_query
from core.smart_albums import (
    SMART_ALBUM_DB_FILENAME,
    SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    _asset_payloads,
    _discover_sets,
    _index_status,
)
from core.smart_sets import _manifest_info, _shoot_date, _source_scope


_MISSING_LABEL = '未记录'
_SET_DIMENSIONS = {'year', 'year_month', 'model', 'environment', 'theme', 'location'}


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

    return {
        'total_images': total_images,
        'total_sets': total_sets,
        'sources': [
            {'id': int(source['id']), 'name': source['name']}
            for source in sorted(sources, key=lambda item: str(item['name']).casefold())
        ],
        'years': _year_rows(set_payloads, total_sets, total_images),
        'models': _set_metric_rows(set_payloads, total_sets, total_images, 'model'),
        'environments': _set_metric_rows(set_payloads, total_sets, total_images, 'environment'),
        'themes': _set_metric_rows(cosplay_sets, len(cosplay_sets), cosplay_images, 'theme'),
        'locations': _set_metric_rows(set_payloads, total_sets, total_images, 'location'),
        'focal_lengths': _focal_rows(
            photo_payloads,
            total_sets,
            total_images,
            valid_set_keys={(int(item['source']['id']), str(item['path'])) for item in set_payloads},
        ),
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
    discover real Sets from the same indexed/enabled/mounted Source scope. Explore
    owns no second index and never persists its temporary query definitions.
    """
    main_db_path = Path(main_db_path).resolve()
    smart_db_path = main_db_path.parent / SMART_ALBUM_DB_FILENAME
    bp = Blueprint('explore', __name__)

    def guard():
        return admin_guard()

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

    return bp
