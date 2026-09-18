"""混样模拟核心引擎。

物理模型
--------

* 每次转移前假设源管内已充分混匀，转出的液体携带源管当前的混合浓度与
  来源构成；
* 一次转移的 ``amount`` 是从源管吸取（移出源管）的体积；可选的
  ``delivered_amount`` 是真正进入目标管的体积，二者差额
  ``amount - delivered_amount`` 记为该步移液损耗（``lost``），损耗液的
  浓度与来源构成与吸出液一致，并累计到系统总量 ``waste``；
  不传 ``delivered_amount`` 时按无损处理（``delivered_amount == amount``）；
* 系统体积守恒：``sum(各管体积) + 累计 waste 体积`` 全程不变；
* 标记物质量守恒：对每种标记物，
  ``sum(管内体积 × 该管浓度) + 累计 waste 中该标记物质量`` 全程不变；
* 仅看管内口径：管内总体积 / 总质量随损耗下降，下降量恰好等于累计
  waste；结果中以 ``tube_volume_change`` / ``tube_marker_amount_change``
  单独给出，与含 waste 的系统总量差（应恒为 0）区分；
* 来源占比按“来自各初始样本管的净体积 / 当前口径总体积”计算
  （管内占比以管内体积为分母，waste 占比以累计损耗体积为分母）。

执行模式
--------

* ``sequential``（默认）：转移按数组顺序逐条即时落地，完全保持旧版行为，
  每条转移自成一个批次（``batch_index == index``）；
* ``time_batch``：解析后 time 相等的**连续**转移合并为一个原子批次：
  1. 先做整批校验（自转移 / 未知管 / 按源管汇总的吸取总量超量），任一
     操作无效则整批不落地，错误仍定位到原 ``transfers`` 下标，之前批次的
     结果保留；
  2. 所有吸取液的浓度与来源构成都取自**批次开始快照**（同一源管被多次
     吸取时浓度一致，且不受同批入管影响），与数组顺序无关；
  3. 校验通过后统一写入：源管按汇总吸取量扣减、目标管按 delivered
     汇总额增加、差额计入累计 waste；同一支管在批内同时收发时，发出按
     快照计算、收入正常累加，二者净额落地。
* 两种模式下响应都保留原 ``transfers`` 的步骤顺序，并为每步给出
  ``batch_index``（同批共享）。

失败时（超量、时间逆序、未知样本）返回已完成的步骤、当时各管状态和
结构化错误信息，便于定位问题，不抛出业务异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import (
    ExecutionMode,
    SampleIn,
    SimulationRequest,
    TransferIn,
    parse_time,
)

# 浮点清理精度：小于该阈值的残余量按 0 处理
ZERO_EPS = 1e-12
ROUND_DIGITS = 12


class SimulationError(Exception):
    """输入层面的错误（理论上由校验器拦截，引擎内作为兜底）。"""

    def __init__(self, code: str, message: str, location: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.location = location


@dataclass
class _Tube:
    """引擎内部使用的管状态。"""

    id: str
    volume: float
    # 每种标记物的质量（浓度 × 体积），混匀计算都在质量域进行
    marker_amount: dict[str, float]
    # 来源构成：初始样本 id -> 净转入体积
    origins: dict[str, float] = field(default_factory=dict)


def _clean(value: float) -> float:
    """统一浮点输出：吞掉接近 0 的残差并截断噪声。"""
    if abs(value) < ZERO_EPS:
        return 0.0
    return round(value, ROUND_DIGITS)


def _concentrations(tube: _Tube, all_markers: list[str]) -> dict[str, float]:
    if tube.volume <= ZERO_EPS:
        return {name: 0.0 for name in all_markers}
    return {
        name: _clean(tube.marker_amount.get(name, 0.0) / tube.volume)
        for name in all_markers
    }


def _origins_view(shares: dict[str, float], total: float) -> list[dict]:
    """把 {初始样本 id: 净体积} 转为对外的来源构成列表。"""
    origins_out: list[dict] = []
    if total > ZERO_EPS:
        for source_id in sorted(shares):
            vol = shares[source_id]
            if vol <= ZERO_EPS:
                continue
            origins_out.append(
                {
                    "source_id": source_id,
                    "volume": _clean(vol),
                    "volume_fraction": _clean(vol / total),
                }
            )
    return origins_out


def _snapshot(tube: _Tube, all_markers: list[str]) -> dict:
    """对外的管状态视图（来源占比之和恒为 1，空管除外）。"""
    return {
        "id": tube.id,
        "volume": _clean(tube.volume),
        "concentrations": _concentrations(tube, all_markers),
        "origins": _origins_view(tube.origins, tube.volume),
    }


def _waste_snapshot(
    volume: float,
    marker_amount: dict[str, float],
    origins: dict[str, float],
    all_markers: list[str],
) -> dict:
    """累计移液损耗（waste）的对外视图。"""
    return {
        "volume": _clean(volume),
        "marker_amount": {
            name: _clean(marker_amount.get(name, 0.0)) for name in all_markers
        },
        "origins": _origins_view(origins, volume),
    }


def _total_marker_amount(
    tubes: dict[str, _Tube], all_markers: list[str]
) -> dict[str, float]:
    return {
        name: _clean(sum(t.marker_amount.get(name, 0.0) for t in tubes.values()))
        for name in all_markers
    }


def _conservation(
    before_vol: float,
    after_vol: float,
    tube_volume_change: float,
    before_amount: dict[str, float],
    after_amount: dict[str, float],
    tube_amount_change: dict[str, float],
) -> dict:
    """守恒视图：before/after 均为“所有管内 + 累计 waste”的系统口径。"""
    return {
        "volume_before": _clean(before_vol),
        "volume_after": _clean(after_vol),
        "volume_difference": _clean(after_vol - before_vol),
        "tube_volume_change": _clean(tube_volume_change),
        "marker_amount_before": before_amount,
        "marker_amount_after": after_amount,
        "marker_amount_difference": {
            name: _clean(after_amount.get(name, 0.0) - before_amount.get(name, 0.0))
            for name in before_amount | after_amount
        },
        "tube_marker_amount_change": tube_amount_change,
    }


def _build_error(
    code: str, message: str, transfer_index: int, location: str
) -> dict:
    return {
        "code": code,
        "message": message,
        "transfer_index": transfer_index,
        "location": location,
    }



def _execute_batch(
    *,
    group: list[int],
    transfers: list[TransferIn],
    tubes: dict[str, _Tube],
    all_markers: list[str],
    waste_volume: float,
    waste_marker: dict[str, float],
    waste_origins: dict[str, float],
    batch_index: int,
) -> tuple[list[dict], float]:
    """原子执行一个批次，原地更新 tubes / waste*。

    返回 ``(按原顺序排列的步骤, 执行后累计 waste 体积)``。批次内所有吸取液的
    浓度与来源构成都取自批次开始快照；目标管写入、源管扣减与 waste 累计在
    全部数值算清后统一落地。同批中一支管既收又发时，发出按快照计算、收入
    正常累加，两个方向的增量互不影响。
    """
    # —— 批次开始快照（体积 / 浓度 / 来源构成）——
    start_vol = {tid: t.volume for tid, t in tubes.items()}
    start_conc = {
        tid: {
            name: (t.marker_amount.get(name, 0.0) / t.volume if t.volume > ZERO_EPS else 0.0)
            for name in all_markers
        }
        for tid, t in tubes.items()
    }
    start_origins = {tid: dict(t.origins) for tid, t in tubes.items()}

    tube_vol_before = sum(start_vol.values())
    tube_mass_before = {
        name: sum(t.marker_amount.get(name, 0.0) for t in tubes.values())
        for name in all_markers
    }
    sys_vol_before = tube_vol_before + waste_volume
    sys_mass_before = {
        name: tube_mass_before[name] + waste_marker[name] for name in all_markers
    }

    # 各操作的明细（均基于批次开始快照，与数组顺序无关）
    prepared: list[dict] = []
    # 汇总增量：源管扣减 / 目标管收入 / waste
    src_vol_delta: dict[str, float] = {}
    tgt_vol_delta: dict[str, float] = {}
    src_origin_delta: dict[str, dict[str, float]] = {}
    tgt_origin_delta: dict[str, dict[str, float]] = {}
    src_marker_delta: dict[str, dict[str, float]] = {}
    tgt_marker_delta: dict[str, dict[str, float]] = {}

    for index in group:
        transfer = transfers[index]
        sid, tid = transfer.source, transfer.target
        available = start_vol[sid]
        amount = transfer.amount
        delivered = (
            amount if transfer.delivered_amount is None
            else min(transfer.delivered_amount, amount)
        )
        lost_volume = amount - delivered
        conc = start_conc[sid]

        # 吸出液来源构成按快照占比分摊；入管 / 损耗两部分按体积拆分
        delivered_shares: dict[str, float] = {}
        lost_shares: dict[str, float] = {}
        if available > ZERO_EPS:
            for origin_id, origin_vol in start_origins[sid].items():
                aspirated_share = origin_vol * (amount / available)
                delivered_share = origin_vol * (delivered / available)
                # 用残差得到损耗份额，减少浮点误差
                lost_share = aspirated_share - delivered_share
                delivered_shares[origin_id] = delivered_share
                lost_shares[origin_id] = lost_share

                src_origin_delta.setdefault(sid, {})
                src_origin_delta[sid][origin_id] = (
                    src_origin_delta[sid].get(origin_id, 0.0) + aspirated_share
                )
                tgt_origin_delta.setdefault(tid, {})
                tgt_origin_delta[tid][origin_id] = (
                    tgt_origin_delta[tid].get(origin_id, 0.0) + delivered_share
                )
                waste_origins[origin_id] = (
                    waste_origins.get(origin_id, 0.0) + lost_share
                )

        src_vol_delta[sid] = src_vol_delta.get(sid, 0.0) + amount
        tgt_vol_delta[tid] = tgt_vol_delta.get(tid, 0.0) + delivered
        for name in all_markers:
            c = conc[name]
            src_marker_delta.setdefault(sid, {})
            src_marker_delta[sid][name] = (
                src_marker_delta[sid].get(name, 0.0) + c * amount
            )
            tgt_marker_delta.setdefault(tid, {})
            tgt_marker_delta[tid][name] = (
                tgt_marker_delta[tid].get(name, 0.0) + c * delivered
            )
            waste_marker[name] += c * lost_volume
        waste_volume += lost_volume

        prepared.append(
            {
                "index": index,
                "transfer": transfer,
                "amount": amount,
                "delivered": delivered,
                "lost_volume": lost_volume,
                "concentrations": conc,
                "delivered_shares": delivered_shares,
                "lost_shares": lost_shares,
            }
        )

    # —— 全部算清后统一写入（同管收发时两个方向独立叠加）——
    for sid, delta in src_vol_delta.items():
        tubes[sid].volume -= delta
    for tid, delta in tgt_vol_delta.items():
        tubes[tid].volume += delta
    for sid, deltas in src_origin_delta.items():
        for origin_id, value in deltas.items():
            tubes[sid].origins[origin_id] = (
                tubes[sid].origins.get(origin_id, 0.0) - value
            )
    for tid, deltas in tgt_origin_delta.items():
        for origin_id, value in deltas.items():
            tubes[tid].origins[origin_id] = (
                tubes[tid].origins.get(origin_id, 0.0) + value
            )
    for sid, deltas in src_marker_delta.items():
        for name, value in deltas.items():
            tubes[sid].marker_amount[name] -= value
    for tid, deltas in tgt_marker_delta.items():
        for name, value in deltas.items():
            tubes[tid].marker_amount[name] += value

    # 批次级守恒视图：批次是原子单位，同批各步共享 before→after 边界
    tube_vol_after = sum(t.volume for t in tubes.values())
    tube_mass_after = {
        name: sum(t.marker_amount.get(name, 0.0) for t in tubes.values())
        for name in all_markers
    }
    sys_vol_after = tube_vol_after + waste_volume
    sys_mass_after = {
        name: tube_mass_after[name] + waste_marker[name] for name in all_markers
    }
    conservation = _conservation(
        sys_vol_before,
        sys_vol_after,
        tube_vol_after - tube_vol_before,
        sys_mass_before,
        sys_mass_after,
        {
            name: tube_mass_after[name] - tube_mass_before[name]
            for name in all_markers
        },
    )

    steps: list[dict] = []
    for item in prepared:
        transfer = item["transfer"]
        conc = item["concentrations"]
        lost_volume = item["lost_volume"]
        transferred = {
            "volume": _clean(item["delivered"]),
            "concentrations": {name: _clean(conc[name]) for name in all_markers},
            "origins": _origins_view(item["delivered_shares"], item["delivered"]),
        }
        lost = {
            "volume": _clean(lost_volume),
            "concentrations": {name: _clean(conc[name]) for name in all_markers},
            "marker_amount": {
                name: _clean(conc[name] * lost_volume) for name in all_markers
            },
            "origins": _origins_view(item["lost_shares"], lost_volume),
        }
        steps.append(
            {
                "index": item["index"],
                "batch_index": batch_index,
                "transfer_id": transfer.id,
                "time": transfer.time,
                "source": transfer.source,
                "target": transfer.target,
                "amount": _clean(item["amount"]),
                "delivered_amount": _clean(item["delivered"]),
                "transferred": transferred,
                "lost": lost,
                "source_after": _snapshot(tubes[transfer.source], all_markers),
                "target_after": _snapshot(tubes[transfer.target], all_markers),
                "conservation": conservation,
            }
        )
    return steps, waste_volume


def run_simulation(request: SimulationRequest) -> dict:
    """执行整次模拟，返回可直接序列化的结果字典（含 status / error）。"""
    samples: list[SampleIn] = request.samples
    transfers: list[TransferIn] = request.transfers
    batched = request.execution_mode == ExecutionMode.TIME_BATCH

    # 所有出现过的标记物取并集，保证每支管子的浓度字段完整
    marker_set: set[str] = set()
    for sample in samples:
        marker_set.update(sample.normalized_markers().keys())
    all_markers = sorted(marker_set)

    tubes: dict[str, _Tube] = {}
    for sample in samples:
        concentrations = sample.normalized_markers()
        tubes[sample.id] = _Tube(
            id=sample.id,
            volume=sample.initial_volume,
            marker_amount={
                name: concentrations.get(name, 0.0) * sample.initial_volume
                for name in all_markers
            },
            origins={sample.id: sample.initial_volume},
        )

    initial_states = [_snapshot(tubes[s.id], all_markers) for s in samples]
    initial_total_volume = sum(s.initial_volume for s in samples)
    initial_marker_amount = _total_marker_amount(tubes, all_markers)

    # 累计移液损耗：独立于管外的系统总量（waste），初始为 0
    waste_volume = 0.0
    waste_marker: dict[str, float] = {name: 0.0 for name in all_markers}
    waste_origins: dict[str, float] = {}

    # 解析后的时间：用于时间非递减校验与 time_batch 连续等时归组
    parsed_times = [parse_time(t.time) for t in transfers]

    steps: list[dict] = []
    error: Optional[dict] = None
    batch_index = -1

    cursor = 0
    n = len(transfers)
    while cursor < n:
        t0 = parsed_times[cursor]

        # 确定本批范围：sequential 每步自成一批；time_batch 归并解析后
        # time 相等的连续转移。
        end = cursor + 1
        if batched:
            while end < n and parsed_times[end] == t0:
                end += 1
        group = list(range(cursor, end))

        # —— 批次级校验（任一操作无效：整批不落地，停在批次前状态）——
        batch_error: Optional[dict] = None
        for index in group:
            transfer = transfers[index]
            loc = f"transfers[{index}]"

            if transfer.source == transfer.target:
                batch_error = _build_error(
                    "SAME_SOURCE_TARGET",
                    f"转移的源管与目标管相同（均为 {transfer.source!r}），不允许自转移",
                    index,
                    loc,
                )
                break

            if tubes.get(transfer.source) is None:
                batch_error = _build_error(
                    "UNKNOWN_SAMPLE",
                    f"transfers[{index}] 引用了未在 samples 中声明的源管 "
                    f"{transfer.source!r}",
                    index,
                    f"{loc}.source",
                )
                break

            if tubes.get(transfer.target) is None:
                batch_error = _build_error(
                    "UNKNOWN_SAMPLE",
                    f"transfers[{index}] 引用了未在 samples 中声明的目标管 "
                    f"{transfer.target!r}",
                    index,
                    f"{loc}.target",
                )
                break

        # 时间非递减：本批首条与上一批末条（即 cursor-1）比较
        if batch_error is None and cursor > 0 and t0 < parsed_times[cursor - 1] - ZERO_EPS:
            prev_index = cursor - 1
            batch_error = _build_error(
                "TIME_NOT_ORDERED",
                f"transfers[{cursor}] 的时间 {transfers[cursor].time!r} "
                f"早于上一次转移的时间（transfers[{prev_index}]），"
                f"转移必须按时间非递减排序",
                cursor,
                f"transfers[{cursor}].time",
            )

        # 按源管汇总吸取量，统一对照批次开始快照校验（同批顺序无关）
        group_totals: dict[str, float] = {}
        if batch_error is None:
            for index in group:
                transfer = transfers[index]
                group_totals[transfer.source] = (
                    group_totals.get(transfer.source, 0.0) + transfer.amount
                )
            for index in group:
                transfer = transfers[index]
                available = tubes[transfer.source].volume
                total_ask = group_totals[transfer.source]
                if total_ask > available + ZERO_EPS:
                    if len(group) == 1:
                        message = (
                            f"transfers[{index}] 试图从 {transfer.source!r} 吸取 "
                            f"{transfer.amount}，但该管当前仅有 {_clean(available)}"
                        )
                    else:
                        message = (
                            f"批次 transfers[{group[0]}..{group[-1]}]（time="
                            f"{transfer.time!r}）合计要从 {transfer.source!r} 吸取 "
                            f"{_clean(total_ask)}，但批次开始时该管仅有 "
                            f"{_clean(available)}"
                        )
                    batch_error = _build_error(
                        "INSUFFICIENT_VOLUME",
                        message,
                        index,
                        f"transfers[{index}].amount",
                    )
                    break

        if batch_error is not None:
            error = batch_error
            break

        # —— 校验通过：原子执行整批 ——
        batch_index += 1
        batch_steps, waste_volume = _execute_batch(
            group=group,
            transfers=transfers,
            tubes=tubes,
            all_markers=all_markers,
            waste_volume=waste_volume,
            waste_marker=waste_marker,
            waste_origins=waste_origins,
            batch_index=batch_index,
        )
        steps.extend(batch_steps)
        cursor = end

    final_states = [_snapshot(tubes[s.id], all_markers) for s in samples]
    final_tube_volume = sum(t.volume for t in tubes.values())
    final_tube_marker = _total_marker_amount(tubes, all_markers)
    final_sys_volume = final_tube_volume + waste_volume
    final_sys_marker = {
        name: final_tube_marker[name] + waste_marker[name] for name in all_markers
    }

    conservation = {
        "initial_total_volume": _clean(initial_total_volume),
        "final_total_volume": _clean(final_sys_volume),
        "volume_difference": _clean(final_sys_volume - initial_total_volume),
        "tube_volume_change": _clean(final_tube_volume - initial_total_volume),
        "initial_marker_amount": initial_marker_amount,
        "final_marker_amount": final_sys_marker,
        "marker_amount_difference": {
            name: _clean(
                final_sys_marker[name] - initial_marker_amount.get(name, 0.0)
            )
            for name in all_markers
        },
        "tube_marker_amount_change": {
            name: _clean(
                final_tube_marker[name] - initial_marker_amount.get(name, 0.0)
            )
            for name in all_markers
        },
    }

    return {
        "status": "failed" if error else "success",
        "name": request.name,
        "execution_mode": request.execution_mode.value,
        "initial_states": initial_states,
        "steps": steps,
        "final_states": final_states,
        "waste": _waste_snapshot(
            waste_volume, waste_marker, waste_origins, all_markers
        ),
        "conservation": conservation,
        "error": error,
    }
