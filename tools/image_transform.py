#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


def _non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be zero or greater')
    return number


def _parser():
    parser = argparse.ArgumentParser(
        description='Apply deterministic libvips image transforms and write binary P6 PPM to stdout.',
        epilog='Pipe stdout directly to a PPM-capable encoder such as cjpeg.',
    )
    parser.add_argument('--source', required=True, help='Source image path.')
    parser.add_argument('--expected-width', required=True, type=_positive_int)
    parser.add_argument('--expected-height', required=True, type=_positive_int)
    parser.add_argument('--source-mode', required=True, help='Preview-time source mode, for example RGB or RGBA.')
    parser.add_argument('--has-alpha', action='store_true')
    parser.add_argument('--alpha-has-transparency', action='store_true')
    parser.add_argument('--crop-left', required=True, type=_non_negative_int)
    parser.add_argument('--crop-top', required=True, type=_non_negative_int)
    parser.add_argument('--crop-width', required=True, type=_positive_int)
    parser.add_argument('--crop-height', required=True, type=_positive_int)
    parser.add_argument('--target-width', required=True, type=_positive_int)
    parser.add_argument('--target-height', required=True, type=_positive_int)
    parser.add_argument('--kernel', default='lanczos3')
    return parser


def _render(args):
    import pyvips

    source_path = Path(args.source).expanduser()
    image = pyvips.Image.new_from_file(str(source_path))
    image = image.autorot()
    if image.width != args.expected_width or image.height != args.expected_height:
        raise RuntimeError(
            f'旋正后尺寸与 Preview 不一致：{image.width}×{image.height}'
        )

    if args.alpha_has_transparency:
        raise RuntimeError('源图存在实际透明像素，Preview 应已阻断')
    if args.has_alpha:
        # Preview has already verified that alpha is fully opaque.
        image = image.flatten()

    if args.source_mode == 'CMYK':
        raise RuntimeError('CMYK 不符合 Final v1 的 sRGB 输入契约')
    if image.bands == 1:
        image = image.bandjoin([image, image])
    if image.bands != 3:
        raise RuntimeError(f'sRGB 输入通道数异常：{image.bands}')
    image = image.copy(interpretation='srgb')

    crop_required = (
        args.crop_left != 0
        or args.crop_top != 0
        or args.crop_width != image.width
        or args.crop_height != image.height
    )
    if crop_required:
        image = image.crop(
            args.crop_left,
            args.crop_top,
            args.crop_width,
            args.crop_height,
        )
    elif image.width != args.crop_width or image.height != args.crop_height:
        raise RuntimeError('Crop no-op 检查失败：当前尺寸与 Preview 不一致')

    resize_required = (
        args.target_width != args.crop_width
        or args.target_height != args.crop_height
    )
    if resize_required:
        hscale = args.target_width / args.crop_width
        vscale = args.target_height / args.crop_height
        image = image.resize(hscale, vscale=vscale, kernel=args.kernel)

    if image.width != args.target_width or image.height != args.target_height:
        raise RuntimeError(
            f'libvips Resize 输出尺寸异常：{image.width}×{image.height}，'
            f'期望 {args.target_width}×{args.target_height}'
        )
    if image.bands != 3:
        raise RuntimeError(f'sRGB 转换后通道数异常：{image.bands}')
    if image.format != 'uchar':
        image = image.cast('uchar', shift=True)

    pixels = image.write_to_memory()
    expected_bytes = image.width * image.height * 3
    if len(pixels) != expected_bytes:
        raise RuntimeError(
            f'libvips RGB 像素缓冲区大小异常：{len(pixels)} bytes，期望 {expected_bytes} bytes'
        )

    output = sys.stdout.buffer
    output.write(f'P6\n{image.width} {image.height}\n255\n'.encode('ascii'))
    output.write(pixels)
    output.flush()


def main():
    args = _parser().parse_args()
    try:
        _render(args)
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
