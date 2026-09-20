import json
import subprocess

from PIL import Image


def generate_thumbnail(image_path, output_path, size=(250, 250)):
    with Image.open(image_path) as img:
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
def generate_compressed(image_path, output_path, max_size=1200):
    with Image.open(image_path) as img:
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
        # 使用exiftool获取EXIF信息
        result = subprocess.run(
            ['exiftool', '-j', '-s', '-EXIF:All', image_path],
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
        raise Exception(f'获取EXIF信息失败: {str(e)}')


def get_image_exif_simple(image_path):
    try:
        # 定义精简的EXIF字段
        simple_fields = [
            'Make', 'Model', 'LensModel', 'DateTimeOriginal',
            'FocalLength', 'FNumber', 'ExposureTime', 'ISO'
        ]

        # 构建命令获取指定字段
        cmd = ['exiftool', '-j', '-s']
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
        raise Exception(f'获取EXIF信息失败: {str(e)}')


def format_exif(exif_data):
    """格式化EXIF数据（只处理日期）"""
    if not exif_data:
        return {}

    # 字段翻译
    field_translation = {
        'Make': '相机品牌',
        'Model': '相机型号',
        'LensModel': '镜头型号',
        'DateTimeOriginal': '拍摄时间',
        'FocalLength': '焦距',
        'FNumber': '光圈',
        'ExposureTime': '快门速度',
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
