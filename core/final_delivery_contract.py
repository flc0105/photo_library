from core.final_resolution import current_policy


FINAL_CROP_WARNING_PERCENT = 3.0

FINAL_JPEG_QUALITY = 95
FINAL_JPEG_CHROMA_SAMPLING = '4:4:4'
FINAL_JPEG_CJPEG_SAMPLE = '1x1'
FINAL_JPEG_PIL_SAMPLING = 0
FINAL_JPEG_BASELINE = True
FINAL_JPEG_HUFFMAN_OPTIMIZE = True
FINAL_JPEG_PROGRESSIVE = False

FINAL_SRGB_ICC_FILENAME = 'sRGB_IEC61966-2.1.icc'
FINAL_SRGB_PROFILE_DESCRIPTION = 'sRGB IEC61966-2.1'
FINAL_SRGB_ICC_SHA256 = '2b3aa1645779a9e634744faf9b01e9102b0c9b88fd6deced7934df86b949af7e'
FINAL_SRGB_ICC_BYTES = 3144


def public_final_delivery_contract():
    policy = current_policy()
    return {
        'crop_warning_percent': FINAL_CROP_WARNING_PERCENT,
        'jpeg_quality': FINAL_JPEG_QUALITY,
        'chroma_sampling': FINAL_JPEG_CHROMA_SAMPLING,
        'icc_profile': FINAL_SRGB_PROFILE_DESCRIPTION,
        'resolution_label': policy['label'],
    }
