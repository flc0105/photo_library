"""Final output resolution policy.

当前只需要改 ACTIVE_FINAL_RESOLUTION_POLICY 这一行即可切换：
    1 = 固定单档 3600×5400 / 5400×3600
    2 = 三档最高可达：4000×6000 → 3600×5400 → 3200×4800（默认）

单档和三档故意保留为两套独立算法。单档不是“只有一个元素的 tiers”，
这样以后确定最终方案时，可以把另一整套逻辑直接删除，不留下兼容层。

【以后如果确定只保留单档 3600】
1. 先把 ACTIVE_FINAL_RESOLUTION_POLICY 固定为 1，并确认实际 Build / QC 正常。
2. 删除 _POLICY_TIERED_4000_3600_3200。
3. 删除 _tiered_info()。
4. 删除 _choose_tiered()。
5. 把 current_policy() 简化为：return _single_3600_info()。
6. 把 choose_target_for_crop() 简化为：return _choose_single_3600(...)。
7. 最后删除 ACTIVE_FINAL_RESOLUTION_POLICY 以及 current_policy()/choose_target_for_crop()
   中已经不再需要的策略分支判断。
除本文件外，Final Builder、Image Inspection、Final QC 不需要
为了“切掉三档”再各自修改尺寸逻辑。build.json 只记录每张图实际 target，
不会记录或依赖这套临时策略。

【以后如果确定只保留三档】
1. 保持 ACTIVE_FINAL_RESOLUTION_POLICY = 2，并确认实际 Build / QC 正常。
2. 删除 _POLICY_SINGLE_3600。
3. 删除 _single_3600_info()。
4. 删除 _choose_single_3600()。
5. 把 current_policy() 简化为：return _tiered_info()。
6. 把 choose_target_for_crop() 简化为：return _choose_tiered(...)。
7. 最后删除 ACTIVE_FINAL_RESOLUTION_POLICY 以及 current_policy()/choose_target_for_crop()
   中已经不再需要的策略分支判断。
除本文件外，其他运行模块仍继续通过统一入口取得 target / allowed dimensions，
不需要重新散落写死 4000、3600、3200；build.json 只保存实际 target。
"""

# 唯一日常切换开关：1 = 单档 3600；2 = 三档（默认）。
ACTIVE_FINAL_RESOLUTION_POLICY = 2

_POLICY_SINGLE_3600 = 1
_POLICY_TIERED_4000_3600_3200 = 2


def _single_3600_info():
    return {
        'id': _POLICY_SINGLE_3600,
        'key': 'single_3600',
        'mode': 'single',
        'label': '3600×5400 / 5400×3600',
        'portrait_targets': [(3600, 5400)],
        'landscape_targets': [(5400, 3600)],
    }


def _tiered_info():
    return {
        'id': _POLICY_TIERED_4000_3600_3200,
        'key': 'tiered_4000_3600_3200',
        'mode': 'tiered',
        'label': '4000×6000 / 3600×5400 / 3200×4800',
        'portrait_targets': [(4000, 6000), (3600, 5400), (3200, 4800)],
        'landscape_targets': [(6000, 4000), (5400, 3600), (4800, 3200)],
    }


def current_policy():
    if ACTIVE_FINAL_RESOLUTION_POLICY == _POLICY_SINGLE_3600:
        return _single_3600_info()
    if ACTIVE_FINAL_RESOLUTION_POLICY == _POLICY_TIERED_4000_3600_3200:
        return _tiered_info()
    raise RuntimeError(
        f'Unsupported ACTIVE_FINAL_RESOLUTION_POLICY: {ACTIVE_FINAL_RESOLUTION_POLICY}'
    )


def _choose_single_3600(crop_width: int, crop_height: int, portrait: bool):
    target_width, target_height = (3600, 5400) if portrait else (5400, 3600)
    return {
        'target_width': target_width,
        'target_height': target_height,
        'pixel_insufficient': crop_width < target_width or crop_height < target_height,
    }


def _choose_tiered(crop_width: int, crop_height: int, portrait: bool):
    targets = (
        ((4000, 6000), (3600, 5400), (3200, 4800))
        if portrait
        else ((6000, 4000), (5400, 3600), (4800, 3200))
    )
    for target_width, target_height in targets:
        if crop_width >= target_width and crop_height >= target_height:
            return {
                'target_width': target_width,
                'target_height': target_height,
                'pixel_insufficient': False,
            }

    target_width, target_height = targets[-1]
    return {
        'target_width': target_width,
        'target_height': target_height,
        'pixel_insufficient': True,
    }


def choose_target_for_crop(crop_width: int, crop_height: int, portrait: bool):
    if ACTIVE_FINAL_RESOLUTION_POLICY == _POLICY_SINGLE_3600:
        return _choose_single_3600(crop_width, crop_height, portrait)
    if ACTIVE_FINAL_RESOLUTION_POLICY == _POLICY_TIERED_4000_3600_3200:
        return _choose_tiered(crop_width, crop_height, portrait)
    raise RuntimeError(
        f'Unsupported ACTIVE_FINAL_RESOLUTION_POLICY: {ACTIVE_FINAL_RESOLUTION_POLICY}'
    )


def allowed_final_dimensions():
    policy = current_policy()
    return set(policy['portrait_targets']) | set(policy['landscape_targets'])
