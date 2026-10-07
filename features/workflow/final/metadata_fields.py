import hashlib
import json
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from core.filesystem import PROJECT_ROOT


_CONFIG_FILENAME = 'final_metadata_settings.json'
_TAG_REF_RE = re.compile(r'^[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$')
_TAG_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# ExifTool may create these structural tags while rebuilding EXIF. They are
# allowed around the configured contract but are never counted as Key EXIF.
FINAL_STRUCTURAL_FIELDS = {
    'IFD0:XResolution',
    'IFD0:YResolution',
    'IFD0:ResolutionUnit',
    'IFD0:YCbCrPositioning',
    'ExifIFD:ExifVersion',
    'ExifIFD:ComponentsConfiguration',
}

_TAG_CATALOG_LOCK = threading.Lock()
_TAG_CATALOG_CACHE = {}


def metadata_fields_path():
    return PROJECT_ROOT / 'data' / _CONFIG_FILENAME


def _tag_parts(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f'Metadata field config {label} must be a non-empty ExifTool Group:Tag.')
    if value != value.strip() or not _TAG_REF_RE.fullmatch(value):
        raise RuntimeError(f'Metadata field config {label} invalid: {value!r}')
    return value.split(':', 1)


def _normalize_fixed(value, index):
    if not isinstance(value, dict):
        raise RuntimeError(f'Metadata field config fields[{index}].fixed must be an object.')
    extra = sorted(set(value) - {'raw', 'display'})
    if extra:
        raise RuntimeError(
            f'Metadata field config fields[{index}].fixed has unknown keys: {", ".join(extra)}'
        )
    if 'raw' not in value or value['raw'] is None or isinstance(value['raw'], (dict, list)):
        raise RuntimeError(f'Metadata field config fields[{index}].fixed.raw must be scalar.')
    display = value.get('display')
    if not isinstance(display, str) or not display.strip() or display != display.strip():
        raise RuntimeError(f'Metadata field config fields[{index}].fixed.display must be a non-empty string.')
    return {'raw': value['raw'], 'display': display}


def _normalize_contract(data):
    if not isinstance(data, dict):
        raise RuntimeError('Metadata field config must be a JSON object.')
    extra_top = sorted(set(data) - {'fields'})
    if extra_top:
        raise RuntimeError(f'Metadata field config has unknown top-level keys: {", ".join(extra_top)}')
    raw_fields = data.get('fields')
    if not isinstance(raw_fields, list) or not raw_fields:
        raise RuntimeError('Metadata field config fields must be a non-empty list.')

    fields = []
    seen_keys = set()
    seen_outputs = set()
    for index, raw in enumerate(raw_fields):
        if not isinstance(raw, dict):
            raise RuntimeError(
                f'Metadata field config fields[{index}] must be an object.'
            )
        extra = sorted(set(raw) - {'key', 'output', 'sources', 'fixed'})
        if extra:
            raise RuntimeError(
                f'Metadata field config fields[{index}] has unknown keys: {", ".join(extra)}'
            )

        key = raw.get('key')
        if not isinstance(key, str) or not key.strip() or key != key.strip() or not _TAG_NAME_RE.fullmatch(key):
            raise RuntimeError(f'Metadata field config fields[{index}].key invalid: {key!r}')
        output = raw.get('output')
        _output_group, output_tag = _tag_parts(output, f'fields[{index}].output')
        if key != output_tag:
            raise RuntimeError(
                f'Metadata field config field key must match output tag: {key} != {output_tag}'
            )
        if key in seen_keys:
            raise RuntimeError(f'Metadata field config has duplicate field: {key}')
        output_cf = output.casefold()
        if output_cf in seen_outputs:
            raise RuntimeError(f'Metadata field config has duplicate output: {output}')
        seen_keys.add(key)
        seen_outputs.add(output_cf)

        raw_sources = raw.get('sources')
        if raw_sources is None:
            sources = []
        else:
            if not isinstance(raw_sources, list) or not raw_sources:
                raise RuntimeError(f'Metadata field config fields[{index}].sources must be a non-empty list.')
            sources = []
            seen_sources = set()
            for source_index, source in enumerate(raw_sources):
                _tag_parts(source, f'fields[{index}].sources[{source_index}]')
                source_cf = source.casefold()
                if source_cf in seen_sources:
                    raise RuntimeError(f'Metadata field {key} has duplicate source: {source}')
                seen_sources.add(source_cf)
                sources.append(source)
            if sources[0].casefold() != output.casefold():
                raise RuntimeError(
                    f'Metadata field {key} first source must be output: {output}'
                )

        fixed = None
        if 'fixed' in raw:
            fixed = _normalize_fixed(raw['fixed'], index)
        elif not sources:
            raise RuntimeError(f'Metadata field {key} requires sources or fixed.')

        field = {
            'key': key,
            'label': key,
            'group': output.split(':', 1)[0],
            'output': output,
            'sources': sources,
        }
        if fixed is not None:
            field['fixed'] = fixed
        fields.append(field)

    return {'fields': fields}


def load_metadata_contract():
    path = metadata_fields_path()
    if not path.exists():
        raise RuntimeError(f'Metadata field config not found: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise RuntimeError(f'Metadata field config JSON invalid: {exc}') from exc
    return _normalize_contract(data)


def load_metadata_fields():
    return load_metadata_contract()['fields']


def load_metadata_field_keys():
    return [field['key'] for field in load_metadata_fields()]


def metadata_contract_signature(fields=None):
    normalized_fields = fields if fields is not None else load_metadata_fields()
    payload = json.dumps(normalized_fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def field_output_key(field):
    return field['output']


def field_read_keys(field):
    return list(field.get('sources') or [field['output']])


def field_write_args(field):
    output = field['output']
    fixed = field.get('fixed')
    if fixed is not None:
        raw = fixed['raw']
        if isinstance(raw, bool):
            raw = 1 if raw else 0
        return [f'-{output}={raw}']
    # ExifTool applies assignments left-to-right.  Lower-priority fallbacks are
    # written first so the first configured source remains authoritative.
    return [f'-{output}<{source}' for source in reversed(field['sources'])]


def field_fixed_display(field):
    fixed = field.get('fixed')
    return fixed['display'] if fixed is not None else ''


def _run_exiftool_listx(exiftool_path):
    result = subprocess.run(
        [str(exiftool_path), '-listx'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(f'ExifTool field validation failed: {detail or f"exit {result.returncode}"}')
    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as exc:
        raise RuntimeError(f'ExifTool field list parse failed: {exc}') from exc

    readable = set()
    writable = set()
    for table in root.iter('table'):
        table_groups = {
            family: str(table.attrib.get(family) or '').strip()
            for family in ('g0', 'g1', 'g2')
        }
        for tag in table.findall('tag'):
            name = str(tag.attrib.get('name') or '').strip()
            if not name:
                continue
            is_writable = str(tag.attrib.get('writable') or '').strip().casefold() not in {
                '', 'false', 'no', '0'
            }
            # -listx table groups are defaults only. Individual tags may override
            # g0/g1/g2 (for example LensModel overrides g1 from IFD0 to ExifIFD).
            # Validate against the effective tag groups exactly as ExifTool exposes them.
            effective_groups = {
                str(tag.attrib.get(family) or table_groups.get(family) or '').strip()
                for family in ('g0', 'g1', 'g2')
            }
            effective_groups.discard('')
            for group in effective_groups:
                ref = f'{group}:{name}'.casefold()
                readable.add(ref)
                if is_writable:
                    writable.add(ref)
    return readable, writable


def _exiftool_tag_catalog(exiftool_path):
    path = str(Path(exiftool_path))
    with _TAG_CATALOG_LOCK:
        cached = _TAG_CATALOG_CACHE.get(path)
    if cached is not None:
        return cached
    catalog = _run_exiftool_listx(path)
    with _TAG_CATALOG_LOCK:
        _TAG_CATALOG_CACHE[path] = catalog
    return catalog


def validate_metadata_fields_with_exiftool(exiftool_path, fields=None):
    fields = fields if fields is not None else load_metadata_fields()
    readable, writable = _exiftool_tag_catalog(exiftool_path)
    for field in fields:
        for source in field_read_keys(field):
            if source.casefold() not in readable:
                raise RuntimeError(
                    f'Metadata field config has unknown ExifTool source tag: {source}'
                )
        output = field['output']
        if output.casefold() not in readable:
            raise RuntimeError(
                f'Metadata field config has unknown ExifTool output tag: {output}'
            )
        if output.casefold() not in writable:
            raise RuntimeError(
                f'Metadata field config output tag is not writable: {output}'
            )
    return True
