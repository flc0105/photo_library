import json

MANIFEST_FILENAME = 'manifest.json'

# Canonical manifest key order. Unknown/custom fields are deliberately NOT
# listed here: they are preserved at their existing position while only the
# known fields are re-ordered around them.
_MANIFEST_KEY_ORDERS = {
    'root': (
        'model', 'shoot', 'location', 'theme', 'production', 'props', 'lighting'
    ),
    'shoot': (
        'date', 'start_time', 'end_time', 'environment', 'scene', 'weather',
        'additional_sessions'
    ),
    'additional_sessions': ('date', 'start_time', 'end_time', 'weather'),
    'location': ('name', 'address', 'lat', 'lng'),
    'theme': (
        'name', 'genre', 'source_title', 'source_type', 'character', 'variant',
        'reference_type', 'reference', 'outfit'
    ),
    # Credits are optional and intentionally come last when present.
    'production': (
        'collaboration_type', 'lead_photographer', 'model_fee', 'venue_fee',
        'venue_fee_payer', 'primary_photographer', 'assistants'
    ),
    'props': ('subject', 'set'),
    'lighting': (
        'role', 'light_type', 'fixture', 'modifier', 'count', 'position', 'note'
    ),
}


def _order_known_keys_preserving_unknown_positions(mapping, preferred_order):
    """Re-order known keys without moving or deleting unknown/custom keys.

    Example:
        {'lighting': ..., 'project': ..., 'model': ...}
    becomes:
        {'model': ..., 'project': ..., 'lighting': ...}

    `project` stays in the slot where the user inserted it. Missing optional
    keys are never created.
    """
    if not isinstance(mapping, dict):
        return mapping

    preferred = [key for key in preferred_order if key in mapping]
    preferred_set = set(preferred_order)
    preferred_iter = iter(preferred)
    result = {}
    for key, value in mapping.items():
        if key in preferred_set:
            ordered_key = next(preferred_iter)
            result[ordered_key] = mapping[ordered_key]
        else:
            result[key] = value
    return result


def _order_manifest_keys(payload):
    """Apply canonical ordering to known manifest fields only."""
    if not isinstance(payload, dict):
        return payload

    result = dict(payload)

    shoot = result.get('shoot')
    if isinstance(shoot, dict):
        shoot = dict(shoot)
        additional = shoot.get('additional_sessions')
        if isinstance(additional, list):
            shoot['additional_sessions'] = [
                _order_known_keys_preserving_unknown_positions(
                    item, _MANIFEST_KEY_ORDERS['additional_sessions']
                ) if isinstance(item, dict) else item
                for item in additional
            ]
        result['shoot'] = _order_known_keys_preserving_unknown_positions(
            shoot, _MANIFEST_KEY_ORDERS['shoot']
        )

    for section in ('location', 'theme', 'production', 'props'):
        value = result.get(section)
        if isinstance(value, dict):
            result[section] = _order_known_keys_preserving_unknown_positions(
                value, _MANIFEST_KEY_ORDERS[section]
            )

    lighting = result.get('lighting')
    if isinstance(lighting, list):
        result['lighting'] = [
            _order_known_keys_preserving_unknown_positions(
                item, _MANIFEST_KEY_ORDERS['lighting']
            ) if isinstance(item, dict) else item
            for item in lighting
        ]

    return _order_known_keys_preserving_unknown_positions(
        result, _MANIFEST_KEY_ORDERS['root']
    )

def read_manifest(path):
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        raw = manifest_path.read_text(encoding='utf-8')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Manifest root must be a JSON object.')
        # Keep the original text alongside parsed data so Raw JSON editing can
        # preserve the file's exact field order and formatting on open.
        return {'exists': True, 'valid': True, 'data': data, 'raw': raw, 'error': None}
    except Exception as exc:
        return {'exists': True, 'valid': False, 'data': None, 'raw': None, 'error': str(exc)}

