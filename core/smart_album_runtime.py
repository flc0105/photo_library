import ast
import collections
import functools
import itertools
import math
import multiprocessing
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


_DEFAULT_PREFERRED_STAGE_ORDER = ('revision', 'model_edit', 'base_edit')


def logical_photo_key(photo):
    """Return the stable logical-image key used by Smart Album helpers.

    Library photos are grouped only when they belong to the same Source, the
    same Set, and the same logical stem.  If logical_id is unavailable, the
    file path is used as a conservative fallback so unrelated files are not
    merged accidentally.
    """
    if not isinstance(photo, PhotoRecord):
        raise TypeError('logical_photo_key() 只接受 Photo 对象。')

    source_id = photo.source.id if photo.source is not None else None
    set_path = photo.set.path if photo.set is not None else None
    logical_id = photo.logical_id
    fallback_path = photo.file.path if photo.file is not None else photo.id
    return (source_id, set_path, logical_id or fallback_path)


def preferred_versions(items, stage_order=_DEFAULT_PREFERRED_STAGE_ORDER):
    """Prefer the highest available stage for each logical photo.

    Default order is Revision > Model Edit > Base Edit.  Matching is by
    ``source + set.path + logical_id`` via :func:`logical_photo_key`.  Multiple
    files at the same winning stage are intentionally retained; this helper
    only resolves cross-stage duplicates and never guesses between same-stage
    variants.

    ``stage_order`` can be overridden from Smart Album Python, for example::

        preferred_versions(
            photos,
            stage_order=('final', 'revision', 'model_edit', 'base_edit'),
        )
    """
    if isinstance(stage_order, str):
        raise TypeError('stage_order 必须是 stage 名称序列，不能是单个字符串。')

    order = []
    seen_stages = set()
    for stage in stage_order:
        stage_name = str(stage)
        if stage_name in seen_stages:
            continue
        seen_stages.add(stage_name)
        order.append(stage_name)

    if not order:
        return []

    rank = {stage: index for index, stage in enumerate(order)}
    candidates = []
    best_rank_by_key = {}

    for photo in items:
        if not isinstance(photo, PhotoRecord):
            raise TypeError('preferred_versions() 的输入只能包含 Photo 对象。')
        stage = photo.stage
        if stage not in rank:
            continue
        key = logical_photo_key(photo)
        photo_rank = rank[stage]
        candidates.append((photo, key, photo_rank))
        current = best_rank_by_key.get(key)
        if current is None or photo_rank < current:
            best_rank_by_key[key] = photo_rank

    return [
        photo
        for photo, key, photo_rank in candidates
        if best_rank_by_key.get(key) == photo_rank
    ]


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


def _query_globals(photos):
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
        'logical_photo_key': logical_photo_key,
        'preferred_versions': preferred_versions,
    }


def _execute_query(code, payloads):
    tree = _validate_script(code)
    photos = []
    by_id = {}
    for payload in payloads:
        photo = PhotoRecord({key: _wrap(value) for key, value in payload.items()})
        photos.append(photo)
        by_id[photo.id] = photo

    globals_dict = _query_globals(photos)
    locals_dict = {}
    compiled = compile(tree, '<smart-album>', 'exec')
    exec(compiled, globals_dict, locals_dict)

    if 'result' not in locals_dict and 'result' not in globals_dict:
        raise ValueError('Python code 必须给变量 result 赋值。')
    result = locals_dict.get('result', globals_dict.get('result'))
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


def _worker_main(conn, code, payloads):
    try:
        ordered_ids = _execute_query(code, payloads)
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


def run_query(code, payloads, timeout_seconds=10):
    """Execute one Smart Album script in an isolated Python process."""
    _validate_script(code)
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker_main,
        args=(child_conn, code, payloads),
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
