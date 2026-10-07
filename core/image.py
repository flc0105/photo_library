import json
import subprocess

from PIL import Image, ImageOps

from core.external_tools import resolve_exiftool


def generate_thumbnail(image_path, output_path, size=(250, 250), apply_exif_orientation=False):
    with Image.open(image_path) as img:
        if apply_exif_orientation:
            img = ImageOps.exif_transpose(img)
        # 转换为RGB模式
        if img.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background

        # 计算裁剪区域，保持居中裁剪
        width, height = img.size
        # 选择较短的边作为裁剪尺寸
        crop_size = min(width, height)
        # 计算裁剪区域（居中）
        left = (width - crop_size) // 2
        top = (height - crop_size) // 2
        right = left + crop_size
        bottom = top + crop_size

        # 裁剪为正方形
        img_cropped = img.crop((left, top, right, bottom))
        # 调整到目标尺寸
        img_resized = img_cropped.resize(size, Image.Resampling.LANCZOS)
        img_resized.save(output_path, 'JPEG', quality=80)


# 生成压缩图
def generate_compressed(image_path, output_path, max_size=1200, apply_exif_orientation=False):
    with Image.open(image_path) as img:
        if apply_exif_orientation:
            img = ImageOps.exif_transpose(img)
        # 转换为RGB模式
        if img.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background

        # 计算新尺寸，保持宽高比
        if img.width > max_size or img.height > max_size:
            if img.width > img.height:
                new_width = max_size
                new_height = int(img.height * max_size / img.width)
            else:
                new_height = max_size
                new_width = int(img.width * max_size / img.height)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        img.save(output_path, 'JPEG', quality=80)


def get_image_exif_all(image_path):
    try:
        # 使用统一解析到的 ExifTool 获取 EXIF 信息。
        exiftool_path = resolve_exiftool()
        if not exiftool_path:
            raise FileNotFoundError('ExifTool unavailable.')
        result = subprocess.run(
            [exiftool_path, '-j', '-s', '-EXIF:All', image_path],
            capture_output=True,
            text=True,
            check=True
        )

        exif_info = json.loads(result.stdout)
        if exif_info and len(exif_info) > 0:
            return exif_info[0]
        else:
            return {}
    except Exception as e:
        raise Exception(f'Failed to read EXIF: {str(e)}')


def get_image_exif_simple(image_path):
    try:
        # 定义精简的EXIF字段
        simple_fields = [
            'Make', 'Model', 'LensModel', 'DateTimeOriginal',
            'FocalLength', 'FNumber', 'ExposureTime', 'ISO'
        ]

        # 构建命令获取指定字段
        exiftool_path = resolve_exiftool()
        if not exiftool_path:
            raise FileNotFoundError('ExifTool unavailable.')
        cmd = [exiftool_path, '-j', '-s']
        for field in simple_fields:
            cmd.append(f'-{field}')
        cmd.append(image_path)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )

        exif_info = json.loads(result.stdout)
        if exif_info and len(exif_info) > 0:
            return format_exif(exif_info[0])
        else:
            return {}

    except Exception as e:
        raise Exception(f'Failed to read EXIF: {str(e)}')


def format_exif(exif_data):
    """格式化EXIF数据（只处理日期）"""
    if not exif_data:
        return {}

    # 字段翻译
    field_translation = {
        'Make': 'Camera Make',
        'Model': 'Camera Model',
        'LensModel': 'Lens Model',
        'DateTimeOriginal': 'Date Taken',
        'FocalLength': 'Focal Length',
        'FNumber': 'Aperture',
        'ExposureTime': 'Shutter Speed',
        'ISO': 'ISO'
    }

    filtered_exif = {}
    for key, value in exif_data.items():
        if key in field_translation.keys() and value not in (None, ''):
            # 只对DateTimeOriginal进行日期格式化
            if key == 'DateTimeOriginal' and ':' in str(value):
                value = value.replace(':', '-', 2)

            filtered_exif[field_translation[key]] = value

    return filtered_exif


# GPS extraction helpers
from pathlib import Path

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
            raise ValueError('GPS not found. For HEIC/HEIF, install ExifTool on the Mac running Photo Library or use a JPEG with GPS.')
        raise ValueError('GPS coordinates not found.')

    return {
        'lat': round(result['lat'], precision),
        'lng': round(result['lng'], precision),
        'precision': precision,
        'engine': result['engine'],
    }
