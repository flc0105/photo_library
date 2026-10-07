import hashlib
import json
import os
import math
import re
import subprocess
from pathlib import Path

from PIL import Image
from flask import Blueprint, jsonify, request

from core.external_tools import probe_exiftool_version, resolve_exiftool
from features.workflow.final.contract import (
    FINAL_CROP_WARNING_PERCENT, FINAL_JPEG_CHROMA_SAMPLING, FINAL_JPEG_QUALITY,
    FINAL_SRGB_ICC_BYTES, FINAL_SRGB_ICC_SHA256, FINAL_SRGB_PROFILE_DESCRIPTION,
    public_final_delivery_contract,
)
from features.workflow.final.metadata_fields import (
    FINAL_STRUCTURAL_FIELDS, field_output_key, field_read_keys, load_metadata_fields,
    validate_metadata_fields_with_exiftool,
)
from features.workflow.final.resolution import allowed_final_dimensions, choose_target_for_crop

_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
_SET_RE = re.compile(r'^\d{8}-.+-.+$')

_IMAGE_INSPECTION_STAGES = [
    ('base_edit', 'Base Edit', '02_Base_Edit'),
    ('model_edit', 'Model Edit', '03_Model_Edit'),
    ('revision', 'Revision', '04_Revision'),
    ('final', 'Final', '05_Final'),
]

_IMAGE_INSPECTION_EXCLUDED_META_GROUPS = {'ExifTool', 'File', 'System', 'Composite'}

_IMAGE_INSPECTION_BATCH_SIZE = 120

_IMAGE_INSPECTION_JPEG_ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]

_IMAGE_INSPECTION_JPEG_LUMA_BASE = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]

_IMAGE_INSPECTION_JPEG_CHROMA_BASE = [
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
]
def _image_inspection_files(set_dir: Path):
    """Recursively scan Base / Model / Revision / Final for read-only inspection.

    01_Original is intentionally not inspected here. Nested directories remain
    supported, while discard/deleted side paths and AppleDouble files are not
    part of the active image stages.
    """
    entries = []
    for stage_key, stage_label, stage_dir_name in _IMAGE_INSPECTION_STAGES:
        stage_dir = set_dir / stage_dir_name
        if not stage_dir.is_dir():
            continue
        for current_root, dir_names, file_names in os.walk(stage_dir):
            dir_names[:] = [name for name in dir_names if name.casefold() not in {'discards', 'deleted'}]
            root_path = Path(current_root)
            for name in file_names:
                if name.startswith('._'):
                    continue
                path = root_path / name
                if path.suffix.lower() not in _IMAGE_EXTENSIONS:
                    continue
                entries.append({
                    'stage': stage_key,
                    'stage_label': stage_label,
                    'stage_dir_name': stage_dir_name,
                    'stage_dir': stage_dir,
                    'path': path,
                })
    return sorted(
        entries,
        key=lambda item: (
            next(i for i, stage in enumerate(_IMAGE_INSPECTION_STAGES) if stage[0] == item['stage']),
            item['path'].relative_to(item['stage_dir']).as_posix().lower(),
        ),
    )


def _exiftool_group(tag_name: str):
    return tag_name.split(':', 1)[0] if ':' in tag_name else ''


def _embedded_metadata_fields(record):
    fields = []
    for key in record:
        if key == 'SourceFile':
            continue
        group = _exiftool_group(key)
        if group in _IMAGE_INSPECTION_EXCLUDED_META_GROUPS:
            continue
        fields.append(key)
    return sorted(fields, key=str.lower)


def _metadata_value_text(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(_metadata_value_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _configured_metadata_value(record, field):
    # Use the exact same source-tag precedence as Write Metadata.
    for key in field_read_keys(field):
        if key not in record:
            continue
        text = _metadata_value_text(record.get(key))
        if text != '':
            return {'present': True, 'value': text, 'source_tag': key}
    return {'present': False, 'value': '', 'source_tag': ''}


def _record_group_value(record, group: str, tag: str):
    return record.get(f'{group}:{tag}')


def _record_first_text(record, keys):
    for key in keys:
        text = _metadata_value_text(record.get(key)).strip()
        if text:
            return text
    return ''


def _is_srgb_label(value: str):
    normalized = re.sub(r'[^a-z0-9]+', '', value.casefold())
    return 'nonsrgb' not in normalized and ('srgb' in normalized or 'iec6196621' in normalized)


def _image_color_space_analysis(record):
    """Return conservative color-space information for Image Inspection.

    An embedded ICC profile is the strongest named-space evidence. With no ICC,
    only explicit sRGB / Adobe RGB declarations are treated as conclusive;
    missing, uncalibrated, or otherwise ambiguous EXIF stays unknown so the UI
    does not raise a false non-sRGB warning.
    """
    profile_description = _record_first_text(record, [
        'ICC_Profile:ProfileDescription',
        'ICC_Profile:ProfileName',
    ])
    if profile_description:
        if _is_srgb_label(profile_description):
            return {
                'status': 'srgb',
                'display': 'sRGB',
                'tag': '',
                'evidence': 'ICC profile',
            }
        return {
            'status': 'non_srgb',
            'display': profile_description,
            'tag': profile_description,
            'evidence': 'ICC profile',
        }

    # A PNG sRGB chunk is an explicit standard declaration.
    if _record_first_text(record, ['PNG:SRGBRendering', 'PNG:sRGBRendering']):
        return {
            'status': 'srgb',
            'display': 'sRGB',
            'tag': '',
            'evidence': 'PNG sRGB',
        }

    interop_index = _record_first_text(record, [
        'InteropIFD:InteropIndex',
        'EXIF:InteropIndex',
    ])
    interop_upper = interop_index.upper()
    if interop_upper.startswith('R98'):
        return {
            'status': 'srgb',
            'display': 'sRGB',
            'tag': '',
            'evidence': 'EXIF InteropIndex',
        }
    if interop_upper.startswith('R03'):
        return {
            'status': 'non_srgb',
            'display': 'Adobe RGB',
            'tag': 'Adobe RGB',
            'evidence': 'EXIF InteropIndex',
        }

    explicit_srgb = False
    explicit_non_srgb = []
    for key, value in record.items():
        if key.rsplit(':', 1)[-1].casefold() != 'colorspace':
            continue
        color_space = _metadata_value_text(value).strip()
        if not color_space:
            continue
        color_cf = color_space.casefold()
        if _is_srgb_label(color_space):
            explicit_srgb = True
            continue
        if any(token in color_cf for token in (
            'adobe rgb',
            'wide gamut rgb',
            'display p3',
            'dci-p3',
            'prophoto',
            'romm',
            'rec.2020',
            'bt.2020',
        )):
            explicit_non_srgb.append(color_space)

    if explicit_non_srgb and not explicit_srgb:
        color_space = explicit_non_srgb[0]
        return {
            'status': 'non_srgb',
            'display': color_space,
            'tag': color_space,
            'evidence': 'ColorSpace tag',
        }
    if explicit_srgb and not explicit_non_srgb:
        return {
            'status': 'srgb',
            'display': 'sRGB',
            'tag': '',
            'evidence': 'ColorSpace tag',
        }

    return {
        'status': 'unknown',
        'display': 'Unknown',
        'tag': '',
        'evidence': '',
    }


def _image_bit_depth_analysis(record):
    raw_value = None
    for key in (
        'File:BitsPerSample',
        'PNG:BitDepth',
        'JPEG:BitsPerSample',
        'IFD0:BitsPerSample',
        'ExifIFD:BitsPerSample',
    ):
        if key in record and _metadata_value_text(record.get(key)).strip():
            raw_value = record.get(key)
            break

    if raw_value is None:
        return {'status': 'unknown', 'display': 'Unknown', 'tag': ''}

    text = _metadata_value_text(raw_value).strip()
    values = [int(value) for value in re.findall(r'(?<![.\d])\d+(?![.\d])', text)]
    if not values:
        return {'status': 'unknown', 'display': text or 'Unknown', 'tag': ''}

    unique = sorted(set(values))
    if len(unique) == 1:
        display = f'{unique[0]}-bit'
    else:
        display = '/'.join(str(value) for value in unique) + '-bit'

    if all(value == 8 for value in values):
        return {'status': 'standard', 'display': '8-bit', 'tag': ''}
    return {'status': 'warning', 'display': display, 'tag': display}


def _canonical_final_exif_analysis(record, metadata_fields, field_defs):
    fields = []
    missing = []
    expected_keys = set()
    for field in field_defs:
        output_key = field_output_key(field)
        expected_keys.add(output_key)
        text = _metadata_value_text(record.get(output_key))
        present = text != ''
        fields.append({
            'name': field['key'],
            'label': field['label'],
            'present': present,
            'value': text,
            'source_tag': output_key if present else '',
        })
        if not present:
            missing.append(field['key'])

    extras = []
    for tag_name in metadata_fields:
        group = _exiftool_group(tag_name)
        if tag_name in expected_keys or tag_name in FINAL_STRUCTURAL_FIELDS:
            continue
        if group == 'JFIF' or group.startswith('ICC'):
            continue
        extras.append(tag_name)

    return {
        'fields': fields,
        'present': len(fields) - len(missing),
        'total': len(fields),
        'missing': missing,
        'extras': sorted(extras, key=str.lower),
        'exact': not missing and not extras,
    }


def _parse_exiftool_full_output(text: str):
    groups = {}
    order = []
    previous = None
    for line in text.splitlines():
        match = re.match(r'^\[([^\]]+)\]\s+(\S+)\s*:\s?(.*)$', line)
        if match:
            group, tag, value = match.groups()
            if group not in groups:
                groups[group] = []
                order.append(group)
            field = {
                'tag': tag,
                'full_tag': f'{group}:{tag}',
                'value': value,
            }
            groups[group].append(field)
            previous = field
            continue
        if previous is not None and line.strip():
            previous['value'] += '\n' + line.rstrip()
    return [{'name': group, 'fields': groups[group]} for group in order]


def _jpeg_quality_table(base, quality: int):
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    natural = [max(1, min(255, (value * scale + 50) // 100)) for value in base]
    return [natural[index] for index in _IMAGE_INSPECTION_JPEG_ZIGZAG]


def _inspect_final_jpeg_structure(path: Path):
    result = {
        'jfif': None,
        'icc_sha256': '',
        'icc_bytes': 0,
        'has_adobe_app14': False,
        'quant_tables': {},
        'huffman_tables': {},
        'error': '',
    }
    try:
        data = path.read_bytes()
        if len(data) < 4 or data[:2] != b'\xff\xd8':
            result['error'] = 'Missing JPEG SOI'
            return result

        icc_chunks = {}
        pos = 2
        while pos < len(data):
            if data[pos] != 0xFF:
                next_marker = data.find(b'\xff', pos)
                if next_marker < 0:
                    break
                pos = next_marker
            while pos < len(data) and data[pos] == 0xFF:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker in {0xD9, 0xDA}:
                break
            if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            if pos + 2 > len(data):
                result['error'] = 'Truncated JPEG marker length'
                break
            length = int.from_bytes(data[pos:pos + 2], 'big')
            if length < 2 or pos + length > len(data):
                result['error'] = 'Invalid JPEG marker length'
                break
            payload = data[pos + 2:pos + length]
            pos += length

            if marker == 0xE0 and payload.startswith(b'JFIF\x00') and len(payload) >= 14:
                result['jfif'] = {
                    'version': f'{payload[5]}.{payload[6]:02d}',
                    'unit': payload[7],
                    'x_density': int.from_bytes(payload[8:10], 'big'),
                    'y_density': int.from_bytes(payload[10:12], 'big'),
                    'thumb_width': payload[12],
                    'thumb_height': payload[13],
                }
            elif marker == 0xE2 and payload.startswith(b'ICC_PROFILE\x00') and len(payload) >= 14:
                sequence = payload[12]
                count = payload[13]
                icc_chunks[sequence] = (count, payload[14:])
            elif marker == 0xEE and payload.startswith(b'Adobe'):
                result['has_adobe_app14'] = True
            elif marker == 0xDB:
                offset = 0
                while offset < len(payload):
                    info = payload[offset]
                    offset += 1
                    precision = info >> 4
                    table_id = info & 0x0F
                    value_bytes = 128 if precision else 64
                    if offset + value_bytes > len(payload):
                        result['error'] = 'Truncated JPEG quantization table'
                        break
                    if precision:
                        values = [
                            int.from_bytes(payload[offset + index * 2:offset + index * 2 + 2], 'big')
                            for index in range(64)
                        ]
                    else:
                        values = list(payload[offset:offset + 64])
                    result['quant_tables'][table_id] = values
                    offset += value_bytes
            elif marker == 0xC4:
                offset = 0
                while offset < len(payload):
                    if offset + 17 > len(payload):
                        result['error'] = 'Truncated JPEG Huffman table'
                        break
                    info = payload[offset]
                    offset += 1
                    table_class = info >> 4
                    table_id = info & 0x0F
                    code_counts = list(payload[offset:offset + 16])
                    offset += 16
                    symbol_count = sum(code_counts)
                    if offset + symbol_count > len(payload):
                        result['error'] = 'Truncated JPEG Huffman symbols'
                        break
                    symbols = list(payload[offset:offset + symbol_count])
                    offset += symbol_count
                    result['huffman_tables'][f'{table_class}:{table_id}'] = {
                        'code_counts': code_counts,
                        'symbols': symbols,
                    }

        if icc_chunks:
            expected_count = next(iter(icc_chunks.values()))[0]
            if all(index in icc_chunks for index in range(1, expected_count + 1)):
                icc = b''.join(icc_chunks[index][1] for index in range(1, expected_count + 1))
                result['icc_sha256'] = hashlib.sha256(icc).hexdigest()
                result['icc_bytes'] = len(icc)
            else:
                result['error'] = result['error'] or 'Incomplete ICC APP2 sequence'
    except OSError as exc:
        result['error'] = str(exc)
    return result


def _final_delivery_analysis(path: Path, record, width: int, height: int, canonical_exif):
    """Validate distribution-only invariants for a Final image.

    Normal results stay compact in the UI. Detailed packaging rules are retained
    here and only failed rules are surfaced to the user.
    """
    file_type = _metadata_value_text(_record_group_value(record, 'File', 'FileType'))
    encoding = _metadata_value_text(_record_group_value(record, 'File', 'EncodingProcess'))
    bits = _metadata_value_text(_record_group_value(record, 'File', 'BitsPerSample'))
    subsampling = _metadata_value_text(_record_group_value(record, 'File', 'YCbCrSubSampling'))
    profile_description = _metadata_value_text(_record_group_value(record, 'ICC_Profile', 'ProfileDescription'))
    jpeg_structure = _inspect_final_jpeg_structure(path)
    jfif = jpeg_structure.get('jfif') or {}
    jfif_ok = (
        jfif.get('version') == '1.01'
        and jfif.get('unit') == 0
        and jfif.get('x_density') == 1
        and jfif.get('y_density') == 1
        and jfif.get('thumb_width') == 0
        and jfif.get('thumb_height') == 0
    )
    icc_ok = (
        profile_description == FINAL_SRGB_PROFILE_DESCRIPTION
        and jpeg_structure.get('icc_sha256') == FINAL_SRGB_ICC_SHA256
        and jpeg_structure.get('icc_bytes') == FINAL_SRGB_ICC_BYTES
    )
    quant_tables = jpeg_structure.get('quant_tables') or {}
    quality_ok = (
        quant_tables.get(0) == _jpeg_quality_table(_IMAGE_INSPECTION_JPEG_LUMA_BASE, FINAL_JPEG_QUALITY)
        and quant_tables.get(1) == _jpeg_quality_table(_IMAGE_INSPECTION_JPEG_CHROMA_BASE, FINAL_JPEG_QUALITY)
    )

    exact_dimensions = (width, height) in allowed_final_dimensions()
    exact_ratio = bool(
        width and height and (
            (height >= width and width * 3 == height * 2)
            or (width > height and width * 2 == height * 3)
        )
    )
    huffman_keys = set((jpeg_structure.get('huffman_tables') or {}).keys())
    huffman_ok = huffman_keys == {'0:0', '1:0', '0:1', '1:1'}
    exif_details = []
    if canonical_exif['missing']:
        exif_details.append('Missing: ' + ', '.join(canonical_exif['missing']))
    if canonical_exif['extras']:
        exif_details.append('Extra: ' + ', '.join(canonical_exif['extras']))
    exif_value = (
        f"{canonical_exif['present']}/{canonical_exif['total']}"
        if canonical_exif['exact']
        else ' · '.join(exif_details) or 'Not exact'
    )

    subsampling_ok = FINAL_JPEG_CHROMA_SAMPLING in subsampling
    if jfif:
        jfif_unit = {0: 'unitless', 1: 'dpi', 2: 'dpcm'}.get(
            jfif.get('unit'),
            f"unit {jfif.get('unit')}" if jfif.get('unit') is not None else 'unit missing',
        )
        jfif_value = (
            f"v{jfif.get('version') or '?'} · {jfif_unit} · "
            f"{jfif.get('x_density', '?')}×{jfif.get('y_density', '?')}"
        )
        thumb_width = int(jfif.get('thumb_width') or 0)
        thumb_height = int(jfif.get('thumb_height') or 0)
        jfif_value += (
            ' · no thumbnail'
            if not thumb_width and not thumb_height
            else f' · thumbnail {thumb_width}×{thumb_height}'
        )
    else:
        jfif_value = 'Missing'

    exact_ratio_value = ('2:3' if height >= width else '3:2') if exact_ratio else 'Not exact'
    if icc_ok:
        icc_value = FINAL_SRGB_PROFILE_DESCRIPTION
    elif not jpeg_structure.get('icc_sha256'):
        icc_value = 'Missing'
    elif profile_description == FINAL_SRGB_PROFILE_DESCRIPTION:
        icc_value = f'{profile_description} · profile mismatch'
    else:
        icc_value = profile_description or 'Profile mismatch'

    checks = [
        ('jpeg', 'File Format', file_type.upper() == 'JPEG', 'JPEG' if file_type.upper() == 'JPEG' else (file_type or 'Missing')),
        ('dimensions', 'Pixel Dimensions', exact_dimensions, f'{width}×{height}' if width and height else 'Missing'),
        ('ratio', 'Aspect Ratio', exact_ratio, exact_ratio_value),
        ('baseline', 'JPEG Encoding', encoding.startswith('Baseline DCT'), 'Baseline DCT' if encoding.startswith('Baseline DCT') else (encoding or 'Missing')),
        ('bits', 'Bit Depth', bits == '8', '8-bit' if bits == '8' else (f'{bits}-bit' if bits else 'Missing')),
        ('subsampling', 'Chroma Sampling', subsampling_ok, FINAL_JPEG_CHROMA_SAMPLING if subsampling_ok else (subsampling or 'Missing')),
        ('icc', 'ICC Profile', icc_ok, icc_value),
        ('quantization', 'JPEG Quality', quality_ok, f'Q{FINAL_JPEG_QUALITY}' if quality_ok else f'Not Q{FINAL_JPEG_QUALITY}'),
        ('huffman', 'Huffman Tables', huffman_ok, f'{len(huffman_keys)} DHT tables' if huffman_keys else 'Missing'),
        ('jfif', 'JFIF Header', jfif_ok, jfif_value),
        ('adobe_app14', 'Adobe APP14', not jpeg_structure.get('has_adobe_app14'), 'Absent' if not jpeg_structure.get('has_adobe_app14') else 'Present'),
        ('canonical_exif', 'Metadata Contract', canonical_exif['exact'], exif_value),
    ]
    if jpeg_structure.get('error'):
        checks.append(('jpeg_structure', 'JPEG structure', False, jpeg_structure['error']))

    failures = [
        {'name': name, 'label': label, 'value': value}
        for name, label, ok, value in checks
        if not ok
    ]
    passed = sum(1 for _name, _label, ok, _value in checks if ok)
    total = len(checks)
    return {
        'final_qc_checks': [
            {'name': name, 'label': label, 'ok': ok, 'value': value}
            for name, label, ok, value in checks
        ],
        'final_qc_failures': failures,
        'final_qc_issue_count': len(failures),
        'final_qc_present': passed,
        'final_qc_total': total,
        'final_qc_missing': len(failures),
        'final_qc_status_level': 'success' if not failures else 'error',
        'final_qc_status_text': 'Final PASS' if not failures else 'Final FAILED',
        'final_bit_depth': f'{bits}-bit' if bits else 'Missing',
        'final_canonical_exif_present': canonical_exif['present'],
        'final_canonical_exif_total': canonical_exif['total'],
        'final_canonical_exif_missing': canonical_exif['missing'],
        'final_canonical_exif_extras': canonical_exif['extras'],
        'final_canonical_exif_exact': canonical_exif['exact'],
    }


def _extract_dimension(record, tag):
    preferred = [
        f'File:{tag}',
        f'EXIF:{tag}',
        f'PNG:{tag}',
        f'JPEG:{tag}',
    ]
    for key in preferred:
        if key in record:
            try:
                return int(record[key])
            except (TypeError, ValueError):
                pass
    for key, value in record.items():
        if key.split(':', 1)[-1] == tag:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def _format_ratio_part(numerator: int, denominator: int, exact_value: int):
    """Format a normalized ratio component without rounding it into a false exact ratio.

    Start with four decimal places, truncating rather than rounding. If a non-exact
    ratio is so close to the target integer that four places would collapse to the
    exact-looking value, increase precision up to six places.
    """
    if denominator <= 0:
        return '—'

    for decimals in range(4, 7):
        scale = 10 ** decimals
        scaled = numerator * scale // denominator
        integer = scaled // scale
        fraction = scaled % scale
        text = f'{integer}.{fraction:0{decimals}d}'.rstrip('0').rstrip('.')
        if text != str(exact_value):
            return text

    # With ordinary image dimensions six decimal places is already more than enough.
    # Keep a bounded display even for pathological dimensions while still making it
    # explicit that the ratio is not exact.
    return f'>{exact_value}' if numerator > exact_value * denominator else f'<{exact_value}'


def _ratio_analysis(width: int, height: int):
    if width <= 0 or height <= 0:
        return {
            'orientation': 'unknown',
            'ratio_value': None,
            'ratio_display': '—',
            'target_ratio': '',
            'exact_ratio': False,
            'crop_width': 0,
            'crop_height': 0,
            'crop_width_px': 0,
            'crop_height_px': 0,
            'crop_left_px': 0,
            'crop_right_px': 0,
            'crop_top_px': 0,
            'crop_bottom_px': 0,
            'crop_area_percent': None,
            'crop_adjustment': '—',
            'crop_detail': '—',
            'final_target': '',
            'perfect_dimensions': False,
            'resize_scale': None,
            'resize_scale_display': '—',
            'resize_pixel_loss': None,
            'resize_pixel_loss_percent': None,
            'resize_pixel_loss_display': '—',
            'pixel_insufficient': True,
            'crop_loss_excessive': False,
            'display_status_level': 'error',
            'display_status_text': 'Read Error',
            'status_ok': False,
        }

    portrait = height >= width
    if portrait:
        orientation = 'portrait' if height > width else 'square'
        target_ratio = '2:3'
        unit = min(width // 2, height // 3)
        crop_width = unit * 2
        crop_height = unit * 3
        exact = width * 3 == height * 2
        ratio_display = '2:3' if exact else f'{_format_ratio_part(width * 3, height, 2)}:3'
    else:
        orientation = 'landscape'
        target_ratio = '3:2'
        unit = min(width // 3, height // 2)
        crop_width = unit * 3
        crop_height = unit * 2
        exact = width * 2 == height * 3
        ratio_display = '3:2' if exact else f'3:{_format_ratio_part(height * 3, width, 2)}'

    target = choose_target_for_crop(crop_width, crop_height, portrait)
    final_width = int(target['target_width'])
    final_height = int(target['target_height'])

    crop_width_px = max(0, width - crop_width)
    crop_height_px = max(0, height - crop_height)
    crop_left_px = crop_width_px // 2
    crop_right_px = crop_width_px - crop_left_px
    crop_top_px = crop_height_px // 2
    crop_bottom_px = crop_height_px - crop_top_px
    source_area = width * height
    crop_area = max(0, source_area - crop_width * crop_height)
    crop_area_percent = (crop_area / source_area * 100.0) if source_area else 0.0

    crop_parts = []
    if crop_width_px:
        crop_parts.append(f'W -{crop_width_px}px')
    if crop_height_px:
        crop_parts.append(f'H -{crop_height_px}px')
    crop_adjustment = 'Exact' if exact else (' · '.join(crop_parts) if crop_parts else 'Crop required')
    crop_detail = (
        'No crop' if exact else
        f'L {crop_left_px}px · R {crop_right_px}px · T {crop_top_px}px · B {crop_bottom_px}px'
    )

    # Pixel sufficiency uses the exact center-crop dimensions because the active
    # Final policy is defined on the pixels that can actually reach Final.
    pixel_insufficient = bool(target['pixel_insufficient'])
    crop_loss_excessive = (not exact) and crop_area_percent > FINAL_CROP_WARNING_PERCENT
    perfect_dimensions = exact and (width, height) in allowed_final_dimensions()

    crop_pixel_count = crop_width * crop_height
    target_pixel_count = final_width * final_height
    resize_scale = (final_width / crop_width) if crop_width else None
    if pixel_insufficient:
        resize_pixel_loss = None
        resize_pixel_loss_percent = None
        resize_pixel_loss_display = '— · upscale blocked'
    else:
        resize_pixel_loss = max(0, crop_pixel_count - target_pixel_count)
        resize_pixel_loss_percent = (
            resize_pixel_loss / crop_pixel_count * 100.0
            if crop_pixel_count else 0.0
        )
        resize_pixel_loss_display = f'{resize_pixel_loss:,} px · {resize_pixel_loss_percent:.2f}%'
    resize_scale_display = f'{resize_scale:.4f}×' if resize_scale is not None else '—'

    # Perfect is reserved for files already matching a canonical Final tier.
    # Exact-ratio source files at any other sufficient size still need resize.
    if pixel_insufficient:
        display_status_level = 'error'
        display_status_text = 'Low Res'
    elif perfect_dimensions:
        display_status_level = 'success'
        display_status_text = 'Perfect'
    elif exact:
        display_status_level = 'success'
        display_status_text = 'Exact Ratio'
    elif crop_loss_excessive:
        display_status_level = 'error'
        display_status_text = 'Ratio Error'
    else:
        display_status_level = 'warning'
        display_status_text = 'Slight Off'

    return {
        'orientation': orientation,
        'ratio_value': round(width / height, 6),
        'ratio_display': ratio_display,
        'target_ratio': target_ratio,
        'exact_ratio': exact,
        'crop_width': crop_width,
        'crop_height': crop_height,
        'crop_width_px': crop_width_px,
        'crop_height_px': crop_height_px,
        'crop_left_px': crop_left_px,
        'crop_right_px': crop_right_px,
        'crop_top_px': crop_top_px,
        'crop_bottom_px': crop_bottom_px,
        'crop_area_percent': round(crop_area_percent, 4),
        'crop_adjustment': crop_adjustment,
        'crop_detail': crop_detail,
        'final_target': f'{final_width}×{final_height}',
        'perfect_dimensions': perfect_dimensions,
        'resize_scale': round(resize_scale, 6) if resize_scale is not None else None,
        'resize_scale_display': resize_scale_display,
        'resize_pixel_loss': resize_pixel_loss,
        'resize_pixel_loss_percent': round(resize_pixel_loss_percent, 4) if resize_pixel_loss_percent is not None else None,
        'resize_pixel_loss_display': resize_pixel_loss_display,
        'pixel_insufficient': pixel_insufficient,
        'crop_loss_excessive': crop_loss_excessive,
        'display_status_level': display_status_level,
        'display_status_text': display_status_text,
        'status_ok': display_status_level == 'success',
    }


def _run_exiftool_records(exiftool_path: str, files):
    records = []
    for offset in range(0, len(files), _IMAGE_INSPECTION_BATCH_SIZE):
        chunk = files[offset:offset + _IMAGE_INSPECTION_BATCH_SIZE]
        command = [
            exiftool_path,
            '-j',
            '-a',
            '-G1',
            '-s',
            '-charset',
            'filename=UTF8',
            *[str(path) for path in chunk],
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
            check=False,
        )
        if not result.stdout.strip():
            detail = result.stderr.strip() or f'ExifTool exited with code {result.returncode}'
            raise RuntimeError(f'ExifTool scan failed: {detail}')
        try:
            batch_records = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'Invalid ExifTool output: {exc}') from exc
        if not isinstance(batch_records, list):
            raise RuntimeError('Invalid ExifTool result format')
        records.extend(batch_records)
    return records


def _run_exiftool_full_metadata(exiftool_path: str, path: Path):
    command = [
        exiftool_path,
        '-G1',
        '-a',
        '-u',
        '-s',
        '-charset',
        'filename=UTF8',
        str(path),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
        check=False,
    )
    if not result.stdout.strip():
        detail = result.stderr.strip() or f'ExifTool exited with code {result.returncode}'
        raise RuntimeError(f'ExifTool metadata failed: {detail}')
    groups = _parse_exiftool_full_output(result.stdout)
    if not groups:
        raise RuntimeError('ExifTool metadata is empty')
    return groups


def _natural_text_key(value: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', value)]


def _build_image_inspection(set_dir: Path):
    field_defs = load_metadata_fields()
    key_exif_fields = [
        {'key': field['key'], 'label': field['label']}
        for field in field_defs
    ]

    exiftool_path = resolve_exiftool()
    if not exiftool_path:
        raise FileNotFoundError('ExifTool unavailable. Pillow fallback is disabled.')
    validate_metadata_fields_with_exiftool(exiftool_path, field_defs)

    version = probe_exiftool_version(exiftool_path)

    entries = _image_inspection_files(set_dir)
    if not entries:
        return {
            'exiftool_version': version,
            'contract': public_final_delivery_contract(),
            'summary': {
                'file_count': 0,
                'perfect_count': 0,
                'exact_ratio_count': 0,
                'ratio_error_count': 0,
                'pixel_insufficient_count': 0,
            },
            'key_exif_fields': key_exif_fields,
            'rows': [],
        }

    files = [entry['path'] for entry in entries]
    records = _run_exiftool_records(exiftool_path, files)
    by_path = {}
    for record in records:
        source = record.get('SourceFile')
        if not source:
            continue
        try:
            key = str(Path(source).resolve())
        except OSError:
            key = str(Path(source))
        by_path[key] = record

    items = []
    perfect_count = 0
    exact_ratio_count = 0
    ratio_error_count = 0
    pixel_insufficient_count = 0

    for entry in entries:
        path = entry['path']
        stage_dir = entry['stage_dir']
        record = by_path.get(str(path.resolve()), {})
        width = _extract_dimension(record, 'ImageWidth')
        height = _extract_dimension(record, 'ImageHeight')
        ratio = _ratio_analysis(width, height)

        if ratio['pixel_insufficient']:
            pixel_insufficient_count += 1
        elif ratio['perfect_dimensions']:
            perfect_count += 1
        elif ratio['exact_ratio']:
            exact_ratio_count += 1
        else:
            ratio_error_count += 1

        focus_fields = []
        focus_present = 0
        for field in field_defs:
            value = _configured_metadata_value(record, field)
            focus_fields.append({'name': field['key'], 'label': field['label'], **value})
            if value['present']:
                focus_present += 1
        metadata_fields = _embedded_metadata_fields(record)
        color_space = _image_color_space_analysis(record)
        bit_depth = _image_bit_depth_analysis(record)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = None
        final_qc = {}
        if entry['stage'] == 'final':
            canonical_exif = _canonical_final_exif_analysis(record, metadata_fields, field_defs)
            focus_fields = canonical_exif['fields']
            focus_present = canonical_exif['present']
            final_qc = _final_delivery_analysis(path, record, width, height, canonical_exif)

        stage_relative_path = path.relative_to(stage_dir).as_posix()
        items.append({
            'stage': entry['stage'],
            'stage_label': entry['stage_label'],
            'stage_dir_name': entry['stage_dir_name'],
            'file': path.name,
            'stem': path.stem,
            'match_key': path.stem.casefold(),
            'stage_relative_path': stage_relative_path,
            'relative_path': path.relative_to(set_dir).as_posix(),
            'width': width,
            'height': height,
            'resolution': f'{width}×{height}' if width and height else '—',
            'size_bytes': size_bytes,
            'color_space_status': color_space['status'],
            'color_space_display': color_space['display'],
            'color_space_tag': color_space['tag'],
            'color_space_evidence': color_space['evidence'],
            'bit_depth_status': bit_depth['status'],
            'bit_depth_display': bit_depth['display'],
            'bit_depth_tag': bit_depth['tag'],
            **ratio,
            'metadata_field_count': len(metadata_fields),
            'metadata_focus_present': focus_present,
            'metadata_focus_total': len(field_defs),
            'metadata_focus_missing': len(field_defs) - focus_present,
            'metadata_status_level': 'success' if focus_present == len(field_defs) else 'error',
            'metadata_status_text': (
                'EXIF Complete' if focus_present == len(field_defs)
                else 'EXIF Missing'
            ),
            'focus_fields': focus_fields,
            'focus_field_map': {field['name']: field for field in focus_fields},
            **final_qc,
            'metadata_error': _metadata_value_text(record.get('ExifTool:Error') or record.get('File:Error') or ''),
        })

    groups = {}
    for item in items:
        group = groups.setdefault(item['match_key'], {
            'display_stem': item['stem'],
            **{stage_key: [] for stage_key, _stage_label, _stage_dir_name in _IMAGE_INSPECTION_STAGES},
        })
        group[item['stage']].append(item)

    rows = []
    for match_key, group in groups.items():
        for stage_key, _stage_label, _stage_dir_name in _IMAGE_INSPECTION_STAGES:
            group[stage_key].sort(key=lambda item: _natural_text_key(item['stage_relative_path']))
        max_count = max(len(group[stage_key]) for stage_key, _, _ in _IMAGE_INSPECTION_STAGES)
        for index in range(max_count):
            row = {
                'row_key': f'{match_key}:{index}',
                'stem': group['display_stem'],
            }
            for stage_key, _stage_label, _stage_dir_name in _IMAGE_INSPECTION_STAGES:
                row[stage_key] = group[stage_key][index] if index < len(group[stage_key]) else None
            rows.append(row)

    rows.sort(key=lambda row: _natural_text_key(row['row_key']))

    return {
        'exiftool_version': version,
        'contract': public_final_delivery_contract(),
        'key_exif_fields': key_exif_fields,
        'summary': {
            'file_count': len(items),
            'perfect_count': perfect_count,
            'exact_ratio_count': exact_ratio_count,
            'ratio_error_count': ratio_error_count,
            'pixel_insufficient_count': pixel_insufficient_count,
        },
        'rows': rows,
    }


def create_blueprint(admin_guard, get_source, resolve_path):
    bp = Blueprint('workflow_image_inspection', __name__)

    def resolve_set(source_id, path_value):
        source = get_source(source_id)
        if not source:
            raise FileNotFoundError('Source unavailable')
        root, target, rel = resolve_path(source, path_value)
        if not target.is_dir() or not _SET_RE.fullmatch(target.name):
            raise ValueError('Current folder is not a Set')
        return source, root, target, rel

    def require_set(source_id):
        return resolve_set(source_id, (request.get_json(silent=True) or {}).get('path', ''))

    @bp.route('/api/library/workflow/sources/<int:source_id>/image-inspection', methods=['POST'])
    def image_inspection(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            return jsonify(_build_image_inspection(set_dir))
        except FileNotFoundError as exc:
            return jsonify({'error': str(exc)}), 503
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @bp.route('/api/library/workflow/sources/<int:source_id>/image-inspection/metadata', methods=['POST'])
    def image_inspection_metadata(source_id):
        denied = admin_guard()
        if denied:
            return denied
        try:
            source, root, set_dir, set_rel = require_set(source_id)
            data = request.get_json(silent=True) or {}
            relative_path = str(data.get('relative_path') or '').strip().replace('\\', '/')
            if not relative_path:
                raise ValueError('Image path required')

            candidate = (set_dir / relative_path).resolve()
            set_resolved = set_dir.resolve()
            try:
                candidate.relative_to(set_resolved)
            except ValueError as exc:
                raise ValueError('Image path is outside the current Set') from exc

            relative = candidate.relative_to(set_resolved)
            if not relative.parts or relative.parts[0] not in {stage[2] for stage in _IMAGE_INSPECTION_STAGES}:
                raise ValueError('Image is outside inspection stages')
            if any(part.casefold() in {'deleted', 'discards'} for part in relative.parts):
                raise ValueError('Deleted / discards are outside inspection scope')
            if candidate.name.startswith('._') or candidate.suffix.lower() not in _IMAGE_EXTENSIONS or not candidate.is_file():
                raise FileNotFoundError('Image not found or unsupported')

            exiftool_path = resolve_exiftool()
            if not exiftool_path:
                raise FileNotFoundError('ExifTool unavailable')
            groups = _run_exiftool_full_metadata(exiftool_path, candidate)
            return jsonify({
                'file': candidate.name,
                'relative_path': relative.as_posix(),
                'field_count': sum(len(group['fields']) for group in groups),
                'groups': groups,
                'command': 'exiftool -G1 -a -u -s',
            })
        except FileNotFoundError as exc:
            return jsonify({'error': str(exc)}), 404
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    return bp
