import math
from collections import Counter, defaultdict
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.smart_album_runtime import run_query
from core.smart_albums import (
    SMART_ALBUM_DB_FILENAME,
    SMART_ALBUM_QUERY_TIMEOUT_SECONDS,
    _asset_payloads,
    _index_status,
)


_MISSING_LABEL = '未记录'


def _dict_value(mapping, key, default=None):
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    return default


def _clean_text(value):
    text = str(value or '').strip()
    return text or None


def _percentage(count, total):
    if total <= 0:
        return 0.0
    return round((count / total) * 100.0, 1)


def _counter_items(counter, total, *, labeler=None):
    labeler = labeler or (lambda value: _MISSING_LABEL if value is None else str(value))
    rows = [
        {
            'value': value,
            'label': labeler(value),
            'count': count,
            'percentage': _percentage(count, total),
        }
        for value, count in counter.items()
    ]
    rows.sort(key=lambda item: (-item['count'], item['label'].casefold()))
    return rows


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


def _build_stats(smart_db_path, main_db_path):
    status = _index_status(smart_db_path)
    if not status['ready']:
        raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')

    payloads, result_rows = _asset_payloads(smart_db_path, main_db_path)
    total = len(payloads)

    years = Counter()
    months_by_year = defaultdict(Counter)
    models = Counter()
    environments = Counter()
    themes = Counter()
    locations = Counter()
    focals = Counter()
    active_sources = {}

    for photo in payloads:
        photo_id = photo.get('id')
        source = photo.get('source') or {}
        source_id = source.get('id')
        source_name = _clean_text(source.get('name')) or str(source_id or '')
        if source_id is not None:
            active_sources[int(source_id)] = source_name

        manifest = _dict_value(photo.get('set') or {}, 'manifest', {}) or {}
        shoot = _dict_value(manifest, 'shoot', {}) or {}
        theme = _dict_value(manifest, 'theme', {}) or {}
        location = _dict_value(manifest, 'location', {}) or {}

        models[_clean_text(_dict_value(manifest, 'model'))] += 1
        environments[_clean_text(_dict_value(shoot, 'environment'))] += 1
        themes[_clean_text(_dict_value(theme, 'source_title'))] += 1
        locations[_clean_text(_dict_value(location, 'name'))] += 1

        capture = photo.get('capture') or {}
        focals[_focal_value(capture.get('focal_length_mm'))] += 1

        capture_sort_time = _dict_value(result_rows.get(photo_id, {}), 'capture_sort_time')
        if capture_sort_time:
            try:
                year_text, month_text = str(capture_sort_time)[:7].split('-', 1)
                year = int(year_text)
                month = int(month_text)
                if 1 <= month <= 12:
                    years[year] += 1
                    months_by_year[year][month] += 1
                    continue
            except (TypeError, ValueError):
                pass
        years[None] += 1

    year_rows = []
    for year, count in years.items():
        if year is None:
            months = []
        else:
            months = [
                {
                    'value': {'year': year, 'month': month},
                    'label': f'{month}月',
                    'count': month_count,
                    'percentage': _percentage(month_count, total),
                }
                for month, month_count in months_by_year.get(year, {}).items()
            ]
            months.sort(key=lambda item: (-item['count'], item['value']['month']))
        year_rows.append({
            'value': year,
            'label': _MISSING_LABEL if year is None else str(year),
            'count': count,
            'percentage': _percentage(count, total),
            'months': months,
        })
    year_rows.sort(key=lambda item: (-item['count'], item['label']))

    return {
        'total_images': total,
        'sources': [
            {'id': source_id, 'name': active_sources[source_id]}
            for source_id in sorted(active_sources, key=lambda sid: active_sources[sid].casefold())
        ],
        'years': year_rows,
        'models': _counter_items(models, total),
        'environments': _counter_items(environments, total),
        'themes': _counter_items(themes, total),
        'locations': _counter_items(locations, total),
        'focal_lengths': _counter_items(focals, total, labeler=_focal_label),
        'index': status,
    }


def _query_code(dimension, value):
    if dimension == 'year':
        if value is None:
            return 'result = [photo for photo in photos if shoot_time(photo) is None]'
        year = int(value)
        return (
            'def match(photo):\n'
            '    value = shoot_time(photo)\n'
            f'    return value is not None and value.year == {year}\n\n'
            'result = [photo for photo in photos if match(photo)]\n'
        )

    if dimension == 'year_month':
        if not isinstance(value, dict):
            raise ValueError('月份筛选参数无效。')
        year = int(value.get('year'))
        month = int(value.get('month'))
        if month < 1 or month > 12:
            raise ValueError('月份筛选参数无效。')
        return (
            'def match(photo):\n'
            '    value = shoot_time(photo)\n'
            f'    return value is not None and value.year == {year} and value.month == {month}\n\n'
            'result = [photo for photo in photos if match(photo)]\n'
        )

    text_fields = {
        'model': 'photo.set.manifest.model',
        'environment': 'photo.set.manifest.shoot.environment',
        'theme': 'photo.set.manifest.theme.source_title',
        'location': 'photo.set.manifest.location.name',
    }
    if dimension in text_fields:
        expression = text_fields[dimension]
        if value is None:
            condition = f'not str({expression} or "").strip()'
        else:
            target = repr(str(value).strip())
            condition = f'str({expression} or "").strip() == {target}'
        return f'result = [photo for photo in photos if {condition}]\n'

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


def _query_title(dimension, value, label):
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
    """Create a read-only Explore surface backed by the Smart Album index.

    Explore owns no photo facts and no second index. Statistics and drill-down
    both use the same current Smart Album candidate pool, so displayed counts
    and temporary result views share one source/scope definition.
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
        try:
            status = _index_status(smart_db_path)
            if not status['ready']:
                raise RuntimeError('SMART_ALBUM_INDEX_REQUIRED')
            code = _query_code(dimension, value)
            payloads, result_rows = _asset_payloads(smart_db_path, main_db_path)
            ordered_ids = run_query(code, payloads, timeout_seconds=SMART_ALBUM_QUERY_TIMEOUT_SECONDS)
            images = [dict(result_rows[photo_id]) for photo_id in ordered_ids if photo_id in result_rows]
            return jsonify({
                'title': _query_title(dimension, value, label),
                'dimension': dimension,
                'value': value,
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
