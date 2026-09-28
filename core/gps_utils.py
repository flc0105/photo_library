import json
import subprocess
from pathlib import Path

from PIL import Image

from core.external_tools import resolve_exiftool


GPS_IFD_TAG = 34853


def _text(value):
    if isinstance(value, bytes):
        try:
            return value.decode('ascii', errors='ignore')
        except Exception:
            return ''
    return '' if value is None else str(value)


def _number(value):
    """Convert Pillow IFDRational / rational tuples / numbers to float."""
    if value is None:
        raise ValueError('missing numeric GPS value')
    if hasattr(value, 'numerator') and hasattr(value, 'denominator'):
        denominator = float(value.denominator)
        if denominator == 0:
            raise ValueError('invalid GPS rational')
        return float(value.numerator) / denominator
    if isinstance(value, (tuple, list)) and len(value) == 2:
        denominator = float(value[1])
        if denominator == 0:
            raise ValueError('invalid GPS rational')
        return float(value[0]) / denominator
    return float(value)


def _dms_to_decimal(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        # Some tools/plugins may already expose decimal coordinates.
        return _number(value)
    degrees = _number(value[0])
    minutes = _number(value[1])
    seconds = _number(value[2])
    return degrees + minutes / 60.0 + seconds / 3600.0


def _apply_ref(value, ref, negative_ref):
    value = float(value)
    ref = _text(ref).strip().upper()
    if ref in ('N', 'E'):
        return abs(value)
    if ref in ('S', 'W'):
        return -abs(value)
    # If the ref is absent, preserve an already-signed decimal value.
    return value


def _validate(lat, lng):
    lat = float(lat)
    lng = float(lng)
    if not -90 <= lat <= 90:
        raise ValueError('invalid latitude')
    if not -180 <= lng <= 180:
        raise ValueError('invalid longitude')
    return lat, lng


def _extract_with_exiftool(path):
    executable = resolve_exiftool()
    if not executable:
        return None

    command = [
        executable,
        '-j',
        '-n',
        '-GPSLatitude',
        '-GPSLatitudeRef',
        '-GPSLongitude',
        '-GPSLongitudeRef',
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        rows = json.loads(result.stdout)
        data = rows[0] if rows else {}
        if 'GPSLatitude' not in data or 'GPSLongitude' not in data:
            return None
        lat = _apply_ref(data['GPSLatitude'], data.get('GPSLatitudeRef'), 'S')
        lng = _apply_ref(data['GPSLongitude'], data.get('GPSLongitudeRef'), 'W')
        lat, lng = _validate(lat, lng)
        return {'lat': lat, 'lng': lng, 'engine': 'exiftool'}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _extract_with_pillow(path):
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None

            try:
                gps = exif.get_ifd(GPS_IFD_TAG)
            except Exception:
                gps = exif.get(GPS_IFD_TAG)

            if not gps:
                return None

            # GPS IFD numeric tags: 1/2 = latitude ref/value, 3/4 = longitude ref/value.
            lat_value = gps.get(2) if hasattr(gps, 'get') else None
            lng_value = gps.get(4) if hasattr(gps, 'get') else None
            if lat_value is None or lng_value is None:
                return None

            lat = _apply_ref(_dms_to_decimal(lat_value), gps.get(1), 'S')
            lng = _apply_ref(_dms_to_decimal(lng_value), gps.get(3), 'W')
            lat, lng = _validate(lat, lng)
            return {'lat': lat, 'lng': lng, 'engine': 'pillow'}
    except (OSError, ValueError, TypeError):
        return None


def extract_gps_from_image(path, precision=5):
    """Extract GPS from a photo and return signed decimal coordinates.

    ExifTool is preferred because it supports iPhone HEIC/HEIF when installed.
    Pillow is the zero-extra-dependency fallback for JPEG/TIFF and other formats
    supported by the local Pillow build.
    """
    path = Path(path)
    result = _extract_with_exiftool(path) or _extract_with_pillow(path)
    if not result:
        if path.suffix.lower() in {'.heic', '.heif'} and not resolve_exiftool():
            raise ValueError('未读取到 GPS。HEIC/HEIF 建议在运行 Photo Library 的 Mac 上安装 ExifTool，或上传带 GPS 的 JPEG。')
        raise ValueError('照片中未读取到 GPS 经纬度。请确认照片保留了定位信息。')

    return {
        'lat': round(result['lat'], precision),
        'lng': round(result['lng'], precision),
        'precision': precision,
        'engine': result['engine'],
    }
