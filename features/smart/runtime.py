import ast
import collections
import functools
import itertools
import math
import multiprocessing
from multiprocessing.connection import wait
import random
import re
import statistics
import traceback
from datetime import date, datetime, timedelta


class AttrMap:
    """Read-only dict wrapper with convenient attribute access.

    Dot access is tolerant and returns None for missing keys. Exact bracket
    access keeps normal dict semantics, which is useful for EXIF group/tag keys.
    """

    __slots__ = ('_data', '_aliases')

    def __init__(self, data=None, *, exif_aliases=False):
        object.__setattr__(self, '_data', {})
        object.__setattr__(self, '_aliases', {})
        for key, value in (data or {}).items():
            self._data[str(key)] = _wrap(value, exif_aliases=exif_aliases)
        if exif_aliases:
            aliases = {}
            for key in self._data:
                terminal = key.rsplit(':', 1)[-1]
                if terminal.isidentifier():
                    aliases.setdefault(terminal, []).append(key)
            object.__setattr__(self, '_aliases', aliases)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        alias_keys = self._aliases.get(name, ())
        if len(alias_keys) == 1:
            return self._data[alias_keys[0]]
        if len(alias_keys) > 1:
            values = [self._data[key] for key in alias_keys]
            first = values[0]
            if all(value == first for value in values[1:]):
                return first
        return None

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data

    def __repr__(self):
        return f'AttrMap({self._data!r})'

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def to_dict(self):
        return _unwrap(self)

    def __setattr__(self, name, value):
        raise AttributeError('Smart Album photo data is read-only')


class PhotoRecord(AttrMap):
    __slots__ = ()


class SetRecord(AttrMap):
    __slots__ = ()




class _ExploreGrouping:
    __slots__ = ('kind', 'population_ids', 'buckets', 'photo_scope_ids')

    def __init__(self, kind, population_ids, buckets, photo_scope_ids=None):
        self.kind = kind
        self.population_ids = population_ids
        self.buckets = buckets
        self.photo_scope_ids = photo_scope_ids


def _normalize_explore_bucket_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Explore bucket key 不能是 NaN 或 Infinity。')
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError('Explore bucket key 只支持字符串、数字、布尔值、日期或 None。')


def _explore_bucket_token(value):
    # Type prefixes keep 1 / True / "1" as three distinct buckets without
    # exposing Python object identity outside the isolated worker.
    if value is None:
        return 'none:'
    if isinstance(value, bool):
        return f'bool:{1 if value else 0}'
    if isinstance(value, int):
        return f'int:{value}'
    if isinstance(value, float):
        return f'float:{value!r}'
    return f'str:{value}'


def _explore_bucket_label(value, label_func, missing_label):
    if value is None:
        return str(missing_label or '未记录')
    if label_func is None:
        return str(value)
    if not callable(label_func):
        raise TypeError('label 必须是 callable 或 None。')
    label = str(label_func(value) or '').strip()
    return label or str(missing_label or '未记录')


def _group_explore_records(
    items,
    key,
    *,
    many,
    label,
    missing,
    include_missing,
    record_type,
    allowed_ids,
    kind,
):
    if not callable(key):
        raise TypeError('key 必须是 callable。')
    materialized = list(items)
    population_ids = []
    seen_population = set()
    buckets = {}
    bucket_order = []
    missing_label = str(missing or '未记录').strip() or '未记录'

    for item in materialized:
        if not isinstance(item, record_type):
            expected = 'Set' if record_type is SetRecord else 'Photo'
            raise TypeError(f'group_{kind}() 的输入只能包含 {expected} 对象。')
        item_id = item.id
        if item_id not in allowed_ids:
            raise ValueError(f'group_{kind}() 包含不属于当前候选池的对象。')

        raw_value = key(item)
        if many:
            if raw_value is None:
                raw_values = [None]
            elif isinstance(raw_value, (str, bytes)):
                raw_values = [raw_value]
            else:
                try:
                    raw_values = list(raw_value)
                except TypeError:
                    raw_values = [raw_value]
                if not raw_values:
                    raw_values = [None]
        else:
            raw_values = [raw_value]

        item_buckets = []
        seen_item_buckets = set()
        for raw_bucket in raw_values:
            value = _normalize_explore_bucket_value(raw_bucket)
            # Treat the configured missing label itself as missing too.  This
            # keeps older blocks that explicitly returned "未记录" compatible
            # with the new include_missing switch.
            if isinstance(value, str) and value == missing_label:
                value = None
            if value is None and not include_missing:
                continue
            token = _explore_bucket_token(value)
            if token in seen_item_buckets:
                continue
            seen_item_buckets.add(token)
            item_buckets.append((token, value))

        # When missing values are excluded they must also leave the grouping
        # population; otherwise the visible buckets would no longer add up to
        # 100% for a normal single-value grouping.
        if not item_buckets:
            continue
        if item_id not in seen_population:
            seen_population.add(item_id)
            population_ids.append(item_id)

        for token, value in item_buckets:
            if token not in buckets:
                buckets[token] = {
                    'bucket_id': token,
                    'value': value,
                    'label': _explore_bucket_label(value, label, missing_label),
                    'ids': [],
                }
                bucket_order.append(token)
            buckets[token]['ids'].append(item_id)

    return _ExploreGrouping(
        kind=kind,
        population_ids=population_ids,
        buckets=[buckets[token] for token in bucket_order],
    )

class ExifMap(AttrMap):
    __slots__ = ()

    def __init__(self, data=None):
        super().__init__(data, exif_aliases=True)


def _wrap(value, *, exif_aliases=False):
    if isinstance(value, AttrMap):
        return value
    if isinstance(value, dict):
        if value.get('__smart_datetime__') is not None and len(value) == 1:
            try:
                return datetime.fromisoformat(value['__smart_datetime__'])
            except (TypeError, ValueError):
                return None
        if value.get('__smart_date__') is not None and len(value) == 1:
            try:
                return date.fromisoformat(value['__smart_date__'])
            except (TypeError, ValueError):
                return None
        if value.get('__smart_exif__') is not None and len(value) == 1:
            raw = value.get('__smart_exif__')
            return ExifMap(raw if isinstance(raw, dict) else {})
        return AttrMap(value, exif_aliases=exif_aliases)
    if isinstance(value, list):
        return [_wrap(item, exif_aliases=exif_aliases) for item in value]
    if isinstance(value, tuple):
        return tuple(_wrap(item, exif_aliases=exif_aliases) for item in value)
    return value


def _unwrap(value):
    if isinstance(value, AttrMap):
        return {key: _unwrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    if isinstance(value, tuple):
        return [_unwrap(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


_BLOCKED_NAMES = {
    '__builtins__', '__import__', 'open', 'exec', 'eval', 'compile', 'input',
    'breakpoint', 'globals', 'locals', 'vars', 'dir', 'help', 'getattr',
    'setattr', 'delattr', 'type', 'object', 'super', 'classmethod',
    'staticmethod', 'property', 'memoryview',
}


def _validate_script(code):
    if not isinstance(code, str) or not code.strip():
        raise ValueError('Python code 不能为空')
    if len(code) > 100_000:
        raise ValueError('Python code 过长')
    try:
        tree = ast.parse(code, mode='exec')
    except SyntaxError as exc:
        location = f'line {exc.lineno}' if exc.lineno else 'unknown line'
        raise ValueError(f'Python 语法错误 ({location}): {exc.msg}') from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError('Smart Album Python 不开放 import；常用模块已经预置。')
        if isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            raise ValueError('Smart Album Python 不开放 dunder attribute。')
        if isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            raise ValueError(f'Smart Album Python 不开放 {node.id}。')

    return tree




def _mapping_value(mapping, key, default=None):
    if isinstance(mapping, (dict, AttrMap)):
        return mapping.get(key, default)
    return default


def _coerce_shoot_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
        try:
            candidate = text[:8] if fmt == '%Y%m%d' else text[:10]
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace(' ', 'T')).date()
    except ValueError:
        return None


def _coerce_capture_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or '').strip()
    if not text:
        return None
    candidates = [text]
    if len(text) >= 19 and re.match(r'^\d{4}:\d{2}:\d{2}', text):
        candidates.append(text[:10].replace(':', '-') + text[10:])
    for candidate in candidates:
        cleaned = candidate.strip()
        try:
            return datetime.fromisoformat(cleaned.replace(' ', 'T'))
        except ValueError:
            pass
        for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(cleaned[:19], fmt)
            except ValueError:
                continue
    return None


def resolve_shoot_time(manifest, capture_time, set_name):
    """Resolve the canonical Smart Album shoot time as a datetime or None.

    Manifest ``shoot.date`` is the date authority.  When capture EXIF time is
    available, its time-of-day is kept on that manifest date.  Without a
    manifest date, capture time is used directly; the final fallback is the
    required ``YYYYMMDD`` prefix of the Set name.
    """
    shoot = _mapping_value(manifest, 'shoot')
    manifest_date = _coerce_shoot_date(_mapping_value(shoot, 'date'))
    capture_dt = _coerce_capture_datetime(capture_time)

    if manifest_date is not None:
        if capture_dt is not None:
            return datetime.combine(manifest_date, capture_dt.timetz())
        return datetime.combine(manifest_date, datetime.min.time())

    if capture_dt is not None:
        return capture_dt

    match = re.match(r'^(\d{8})(?:-|$)', str(set_name or '').strip())
    if match:
        try:
            set_date = datetime.strptime(match.group(1), '%Y%m%d').date()
            return datetime.combine(set_date, datetime.min.time())
        except ValueError:
            pass
    return None




def _validate_custom_helpers_source(code):
    """Validate the user-editable helper module without executing it."""
    if not isinstance(code, str):
        raise ValueError('Custom Helpers source 必须是字符串。')
    if len(code) > 100_000:
        raise ValueError('Custom Helpers source 过长。')
    if not code.strip():
        return ast.parse('', mode='exec')
    tree = _validate_script(code)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise ValueError('Custom Helper 顶层函数不支持 decorator。')
            continue
        if isinstance(node, ast.Assign):
            if not all(isinstance(target, ast.Name) for target in node.targets):
                raise ValueError('Custom Helper 顶层常量只能使用简单名称赋值。')
            try:
                ast.literal_eval(node.value)
            except Exception as exc:
                raise ValueError('Custom Helper 顶层常量只能使用字面量。') from exc
            continue
        if isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name) or node.value is None:
                raise ValueError('Custom Helper 顶层常量只能使用简单名称赋值。')
            try:
                ast.literal_eval(node.value)
            except Exception as exc:
                raise ValueError('Custom Helper 顶层常量只能使用字面量。') from exc
            continue
        raise ValueError('Custom Helper 顶层只允许函数定义、模块说明和字面量常量。')
    return tree


def custom_helper_docs(code):
    tree = _validate_custom_helpers_source(code)
    docs = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith('_'):
            continue
        args_text = ast.unparse(node.args)
        doc = (ast.get_docstring(node) or '').strip()
        docs.append({
            'name': node.name,
            'signature': f'{node.name}({args_text})',
            'description': doc.splitlines()[0] if doc else '',
        })
    return docs

_SAFE_BUILTINS = {
    'abs': abs,
    'all': all,
    'any': any,
    'bool': bool,
    'bin': bin,
    'chr': chr,
    'dict': dict,
    'divmod': divmod,
    'enumerate': enumerate,
    'filter': filter,
    'float': float,
    'format': format,
    'frozenset': frozenset,
    'int': int,
    'hex': hex,
    'isinstance': isinstance,
    'len': len,
    'list': list,
    'map': map,
    'max': max,
    'min': min,
    'next': next,
    'oct': oct,
    'ord': ord,
    'pow': pow,
    'range': range,
    'reversed': reversed,
    'round': round,
    'set': set,
    'slice': slice,
    'sorted': sorted,
    'str': str,
    'sum': sum,
    'tuple': tuple,
    'zip': zip,
    'Exception': Exception,
    'ValueError': ValueError,
    'TypeError': TypeError,
    'KeyError': KeyError,
}


class _HelperNamespace:
    __slots__ = ('_functions',)

    def __init__(self, functions):
        object.__setattr__(self, '_functions', dict(functions))

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        try:
            return self._functions[name]
        except KeyError as exc:
            raise AttributeError(f'Custom Helper 不存在：{name}') from exc

    def __setattr__(self, name, value):
        raise AttributeError('Custom Helpers namespace is read-only')

    def __dir__(self):
        return sorted(self._functions)


def _helper_globals():
    return {
        '__builtins__': _SAFE_BUILTINS,
        'math': math,
        're': re,
        'statistics': statistics,
        'itertools': itertools,
        'functools': functools,
        'collections': collections,
        'random': random,
        'datetime': datetime,
        'date': date,
        'timedelta': timedelta,
    }


def _load_custom_helpers(helper_source):
    tree = _validate_custom_helpers_source(helper_source)
    namespace = _helper_globals()
    compiled = compile(tree, '<custom-helpers>', 'exec')
    exec(compiled, namespace, namespace)
    functions = {}
    for item in tree.body:
        if isinstance(item, ast.FunctionDef) and not item.name.startswith('_'):
            value = namespace.get(item.name)
            if not callable(value):
                raise ValueError(f'Custom Helper {item.name} 不是可调用函数。')
            functions[item.name] = value
    return _HelperNamespace(functions)


def _query_globals(photos, helper_source):
    return {
        '__builtins__': _SAFE_BUILTINS,
        'photos': photos,
        'math': math,
        're': re,
        'statistics': statistics,
        'itertools': itertools,
        'functools': functools,
        'collections': collections,
        'datetime': datetime,
        'date': date,
        'timedelta': timedelta,
        'helpers': _load_custom_helpers(helper_source),
    }


def _execute_query(code, payloads, helper_source):
    tree = _validate_script(code)
    photos = []
    by_id = {}
    for payload in payloads:
        photo = PhotoRecord({key: _wrap(value) for key, value in payload.items()})
        photos.append(photo)
        by_id[photo.id] = photo

    # Execute the script like a normal Python module: top-level assignments and
    # functions share one namespace.  Using separate globals/locals breaks real
    # Python semantics because functions cannot see variables assigned at the
    # script top level (for example ``seed`` or ``threshold``).
    namespace = _query_globals(photos, helper_source)
    compiled = compile(tree, '<smart-album>', 'exec')
    exec(compiled, namespace, namespace)

    if 'result' not in namespace:
        raise ValueError('Python code 必须给变量 result 赋值。')
    result = namespace.get('result')
    if result is None:
        return []
    if isinstance(result, PhotoRecord):
        result = [result]
    try:
        result_items = list(result)
    except TypeError as exc:
        raise ValueError('result 必须是 Photo 对象或 Photo 对象 iterable。') from exc

    ordered_ids = []
    seen = set()
    for item in result_items:
        if not isinstance(item, PhotoRecord):
            raise ValueError('result 只能包含 photos 中的 Photo 对象。')
        photo_id = item.id
        if photo_id not in by_id:
            raise ValueError('result 包含不属于当前候选池的 Photo 对象。')
        if photo_id in seen:
            continue
        seen.add(photo_id)
        ordered_ids.append(photo_id)
    return ordered_ids


def _worker_main(conn, code, payloads, helper_source):
    try:
        ordered_ids = _execute_query(code, payloads, helper_source)
        conn.send({'ok': True, 'ids': ordered_ids})
    except Exception as exc:
        trace = traceback.format_exc(limit=8)
        conn.send({
            'ok': False,
            'error': str(exc),
            'error_type': type(exc).__name__,
            'traceback': trace,
        })
    finally:
        conn.close()


def run_query(code, payloads, helper_source, timeout_seconds=10):
    """Execute one Smart Album script in an isolated Python process."""
    _validate_script(code)
    _validate_custom_helpers_source(helper_source)
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker_main,
        args=(child_conn, code, payloads, helper_source),
        daemon=True,
    )
    process.start()
    child_conn.close()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        parent_conn.close()
        raise TimeoutError(f'Smart Album Python 执行超过 {timeout_seconds} 秒，已停止。')
    if not parent_conn.poll():
        exit_code = process.exitcode
        parent_conn.close()
        raise RuntimeError(f'Smart Album Python worker 异常退出 (exit={exit_code})。')
    message = parent_conn.recv()
    parent_conn.close()
    if not message.get('ok'):
        error = RuntimeError(message.get('error') or 'Smart Album Python 执行失败')
        error.smart_traceback = message.get('traceback') or ''
        raise error
    return message.get('ids') or []

def _set_query_globals(sets, helper_source):
    namespace = _query_globals([], helper_source)
    namespace.pop('photos', None)
    namespace['sets'] = sets
    return namespace


def _execute_set_query(code, payloads, helper_source):
    tree = _validate_script(code)
    sets = []
    by_id = {}
    for payload in payloads:
        wrapped = {key: _wrap(value) for key, value in payload.items() if key != 'photos'}
        wrapped['photos'] = [
            PhotoRecord({key: _wrap(value) for key, value in photo.items()})
            for photo in (payload.get('photos') or [])
        ]
        item = SetRecord(wrapped)
        sets.append(item)
        by_id[item.id] = item

    namespace = _set_query_globals(sets, helper_source)
    compiled = compile(tree, '<smart-set>', 'exec')
    exec(compiled, namespace, namespace)

    if 'result' not in namespace:
        raise ValueError('Python code 必须给变量 result 赋值。')
    result = namespace.get('result')
    if result is None:
        return []
    if isinstance(result, SetRecord):
        result = [result]
    try:
        result_items = list(result)
    except TypeError as exc:
        raise ValueError('result 必须是 Set 对象或 Set 对象 iterable。') from exc

    ordered_ids = []
    seen = set()
    for item in result_items:
        if not isinstance(item, SetRecord):
            raise ValueError('result 只能包含 sets 中的 Set 对象。')
        set_id = item.id
        if set_id not in by_id:
            raise ValueError('result 包含不属于当前候选池的 Set 对象。')
        if set_id in seen:
            continue
        seen.add(set_id)
        ordered_ids.append(set_id)
    return ordered_ids


def _set_worker_main(conn, code, payloads, helper_source):
    try:
        ordered_ids = _execute_set_query(code, payloads, helper_source)
        conn.send({'ok': True, 'ids': ordered_ids})
    except Exception as exc:
        trace = traceback.format_exc(limit=8)
        conn.send({
            'ok': False,
            'error': str(exc),
            'error_type': type(exc).__name__,
            'traceback': trace,
        })
    finally:
        conn.close()


def run_set_query(code, payloads, helper_source, timeout_seconds=10):
    """Execute one Smart Set script using the existing Smart Album sandbox rules."""
    _validate_script(code)
    _validate_custom_helpers_source(helper_source)
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_set_worker_main,
        args=(child_conn, code, payloads, helper_source),
        daemon=True,
    )
    process.start()
    child_conn.close()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        parent_conn.close()
        raise TimeoutError(f'Smart Set Python 执行超过 {timeout_seconds} 秒，已停止。')
    if not parent_conn.poll():
        exit_code = process.exitcode
        parent_conn.close()
        raise RuntimeError(f'Smart Set Python worker 异常退出 (exit={exit_code})。')
    message = parent_conn.recv()
    parent_conn.close()
    if not message.get('ok'):
        error = RuntimeError(message.get('error') or 'Smart Set Python 执行失败')
        error.smart_traceback = message.get('traceback') or ''
        raise error
    return message.get('ids') or []

def _execute_explore_block(code, set_payloads, photo_payloads, helper_source):
    tree = _validate_script(code)

    photos = []
    photos_by_id = {}
    for payload in photo_payloads:
        photo = PhotoRecord({key: _wrap(value) for key, value in payload.items()})
        photos.append(photo)
        photos_by_id[photo.id] = photo

    sets = []
    sets_by_id = {}
    photo_to_set_id = {}
    for payload in set_payloads:
        wrapped = {key: _wrap(value) for key, value in payload.items() if key != 'photos'}
        wrapped_photos = []
        for photo_payload in (payload.get('photos') or []):
            photo_id = photo_payload.get('id')
            photo = photos_by_id.get(photo_id)
            if photo is None:
                photo = PhotoRecord({key: _wrap(value) for key, value in photo_payload.items()})
            wrapped_photos.append(photo)
        wrapped['photos'] = wrapped_photos
        item = SetRecord(wrapped)
        sets.append(item)
        sets_by_id[item.id] = item
        for photo in wrapped_photos:
            photo_to_set_id[photo.id] = item.id

    allowed_set_ids = set(sets_by_id)
    allowed_photo_ids = set(photos_by_id)

    def group_sets(
        items, key, many=False, label=None, missing='未记录', include_missing=True,
        photo_scope=None,
    ):
        if not isinstance(include_missing, bool):
            raise TypeError('include_missing 必须是 bool。')
        grouping = _group_explore_records(
            items, key, many=bool(many), label=label, missing=missing,
            include_missing=include_missing,
            record_type=SetRecord, allowed_ids=allowed_set_ids, kind='sets',
        )
        if photo_scope is None:
            return grouping

        scope_ids = []
        seen_scope_ids = set()
        try:
            scope_items = list(photo_scope)
        except TypeError as exc:
            raise TypeError('photo_scope 必须是 Photo iterable 或 None。') from exc
        for photo in scope_items:
            if not isinstance(photo, PhotoRecord):
                raise TypeError('photo_scope 只能包含 Photo 对象。')
            photo_id = photo.id
            if photo_id not in allowed_photo_ids:
                raise ValueError('photo_scope 包含不属于当前候选池的 Photo 对象。')
            if photo_id in seen_scope_ids:
                continue
            seen_scope_ids.add(photo_id)
            scope_ids.append(photo_id)
        grouping.photo_scope_ids = scope_ids
        return grouping

    def group_photos(items, key, many=False, label=None, missing='未记录', include_missing=True):
        if not isinstance(include_missing, bool):
            raise TypeError('include_missing 必须是 bool。')
        return _group_explore_records(
            items, key, many=bool(many), label=label, missing=missing,
            include_missing=include_missing,
            record_type=PhotoRecord, allowed_ids=allowed_photo_ids, kind='photos',
        )

    namespace = _query_globals(photos, helper_source)
    namespace.update({
        'sets': sets,
        'group_sets': group_sets,
        'group_photos': group_photos,
    })
    compiled = compile(tree, '<explore-block>', 'exec')
    exec(compiled, namespace, namespace)

    if 'result' not in namespace:
        raise ValueError('Python code 必须给变量 result 赋值。')
    grouping = namespace.get('result')
    if not isinstance(grouping, _ExploreGrouping):
        raise ValueError('Explore Block 的 result 必须是 group_sets() 或 group_photos() 的返回值。')

    if grouping.kind == 'sets':
        population_set_ids = list(grouping.population_ids)
        population_set_id_set = set(population_set_ids)
        if grouping.photo_scope_ids is None:
            scoped_photos = photos
        else:
            scoped_photos = [
                photos_by_id[photo_id]
                for photo_id in grouping.photo_scope_ids
                if photo_id in photos_by_id
            ]
        population_photo_ids = [
            photo.id for photo in scoped_photos
            if photo_to_set_id.get(photo.id) in population_set_id_set
        ]
    else:
        population_photo_ids = list(grouping.population_ids)
        population_set_ids = []
        seen_set_ids = set()
        for photo_id in population_photo_ids:
            set_id = photo_to_set_id.get(photo_id)
            if set_id is not None and set_id not in seen_set_ids:
                seen_set_ids.add(set_id)
                population_set_ids.append(set_id)

    buckets = []
    for bucket in grouping.buckets:
        direct_ids = list(bucket.get('ids') or [])
        if grouping.kind == 'sets':
            set_ids = direct_ids
            set_id_set = set(set_ids)
            photo_ids = [
                photo.id for photo in scoped_photos
                if photo_to_set_id.get(photo.id) in set_id_set
            ]
        else:
            photo_ids = direct_ids
            set_ids = []
            seen_set_ids = set()
            for photo_id in photo_ids:
                set_id = photo_to_set_id.get(photo_id)
                if set_id is not None and set_id not in seen_set_ids:
                    seen_set_ids.add(set_id)
                    set_ids.append(set_id)
        buckets.append({
            'bucket_id': bucket['bucket_id'],
            'value': bucket['value'],
            'label': bucket['label'],
            'set_ids': set_ids,
            'photo_ids': photo_ids,
        })

    return {
        'kind': grouping.kind,
        'population_set_ids': population_set_ids,
        'population_photo_ids': population_photo_ids,
        'buckets': buckets,
    }


def _explore_worker_main(conn, code, set_payloads, photo_payloads, helper_source):
    try:
        result = _execute_explore_block(code, set_payloads, photo_payloads, helper_source)
        conn.send({'ok': True, 'result': result})
    except Exception as exc:
        trace = traceback.format_exc(limit=8)
        conn.send({
            'ok': False,
            'error': str(exc),
            'error_type': type(exc).__name__,
            'traceback': trace,
        })
    finally:
        conn.close()


def run_explore_block(code, set_payloads, photo_payloads, helper_source, timeout_seconds=10):
    """Execute one Explore grouping script in the existing isolated Python sandbox."""
    _validate_script(code)
    _validate_custom_helpers_source(helper_source)
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_explore_worker_main,
        args=(child_conn, code, set_payloads, photo_payloads, helper_source),
        daemon=True,
    )
    process.start()
    child_conn.close()

    # Explore results can contain the population IDs plus every bucket's Set /
    # Photo membership.  That payload can exceed the OS pipe buffer.  Waiting
    # for the worker to exit before reading the pipe deadlocks in that case:
    # the child blocks in send() while the parent blocks in join().  Wait for
    # either pipe data or worker exit, receive first, then reap the process.
    ready = wait([parent_conn, process.sentinel], timeout_seconds)
    if parent_conn in ready:
        try:
            message = parent_conn.recv()
        except EOFError as exc:
            process.join(2)
            parent_conn.close()
            raise RuntimeError(
                f'Explore Block Python worker 异常退出 (exit={process.exitcode})。'
            ) from exc
    elif process.sentinel in ready:
        process.join(2)
        if parent_conn.poll():
            message = parent_conn.recv()
        else:
            exit_code = process.exitcode
            parent_conn.close()
            raise RuntimeError(f'Explore Block Python worker 异常退出 (exit={exit_code})。')
    else:
        process.terminate()
        process.join(2)
        parent_conn.close()
        raise TimeoutError(f'Explore Block Python 执行超过 {timeout_seconds} 秒，已停止。')

    process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(2)
    parent_conn.close()
    if not message.get('ok'):
        error = RuntimeError(message.get('error') or 'Explore Block Python 执行失败')
        error.smart_traceback = message.get('traceback') or ''
        raise error
    return message.get('result') or {'kind': 'sets', 'population_set_ids': [], 'population_photo_ids': [], 'buckets': []}

