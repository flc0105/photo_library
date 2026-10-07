"""User-editable Smart View / Explore business helpers.

Public top-level functions (names not starting with ``_``) are exposed to Smart
Python as ``helpers.<name>``. This file runs inside the existing restricted
Smart worker process: imports, filesystem, network, process and database access
are not available.
"""


def set_key(photo):
    """Return the stable Source + Set grouping key for one Photo."""
    source_id = photo.source.id if photo.source is not None else None
    set_path = photo.set.path if photo.set is not None else None
    return (source_id, set_path)


def finals(items):
    """Return only Final-stage Photos while preserving input order."""
    return [photo for photo in items if photo.stage == 'final']


def _coerce_shoot_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or '').strip()
    if not text:
        return None
    normalized = text[:10].replace('/', '-')
    if len(normalized) == 8 and '-' not in normalized:
        normalized = f'{normalized[:4]}-{normalized[4:6]}-{normalized[6:8]}'
    try:
        return date.fromisoformat(normalized)
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
    if len(text) >= 19 and re.match(r'^\d{4}:\d{2}:\d{2}', text):
        text = text[:10].replace(':', '-') + text[10:]
    try:
        return datetime.fromisoformat(text.replace(' ', 'T'))
    except ValueError:
        return None


def shoot_time(photo):
    """Resolve one Photo's canonical shoot datetime.

    Manifest ``shoot.date`` is the date authority. If capture EXIF time exists,
    its time-of-day is retained on that manifest date. Without a manifest date,
    capture time is used directly; the final fallback is the Set name's
    ``YYYYMMDD`` prefix.
    """
    manifest = photo.set.manifest if photo.set is not None else None
    shoot = manifest.shoot if manifest is not None else None
    manifest_date = _coerce_shoot_date(shoot.date if shoot is not None else None)
    capture_time = photo.capture.time if photo.capture is not None else None
    capture_dt = _coerce_capture_datetime(capture_time)

    if manifest_date is not None:
        if capture_dt is not None:
            return datetime.combine(manifest_date, capture_dt.timetz())
        return datetime.combine(manifest_date, datetime.min.time())

    if capture_dt is not None:
        return capture_dt

    set_name = photo.set.name if photo.set is not None else None
    match = re.match(r'^(\d{8})(?:-|$)', str(set_name or '').strip())
    if match:
        digits = match.group(1)
        try:
            set_date = date.fromisoformat(f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}')
            return datetime.combine(set_date, datetime.min.time())
        except ValueError:
            pass
    return None


def sample_per_set(items, count=1, seed=None):
    """Randomly keep up to ``count`` Photos from every Source + Set group."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('sample_per_set() count must be an integer >= 1.')

    materialized = list(items)
    groups = {}
    for index, photo in enumerate(materialized):
        groups.setdefault(set_key(photo), []).append(index)

    rng = random.Random(seed)
    selected_indexes = set()
    for indexes in groups.values():
        if len(indexes) <= count:
            selected_indexes.update(indexes)
        else:
            selected_indexes.update(rng.sample(indexes, count))

    return [
        photo
        for index, photo in enumerate(materialized)
        if index in selected_indexes
    ]


def logical_photo_key(photo):
    """Return Source + Set + logical stem used for version matching."""
    source_id = photo.source.id if photo.source is not None else None
    set_path = photo.set.path if photo.set is not None else None
    logical_id = photo.logical_id
    fallback_path = photo.file.path if photo.file is not None else photo.id
    return (source_id, set_path, logical_id or fallback_path)


def preferred_versions(items, stage_order=('revision', 'model_edit', 'base_edit')):
    """Keep the highest available stage for every logical Photo.

    The default is Revision > Model Edit > Base Edit. Multiple files at the
    same winning stage are intentionally retained. Pass a different
    ``stage_order`` to change the policy, for example to include Final.
    """
    if isinstance(stage_order, str):
        raise TypeError('stage_order must be a sequence of stage names, not a string.')

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
