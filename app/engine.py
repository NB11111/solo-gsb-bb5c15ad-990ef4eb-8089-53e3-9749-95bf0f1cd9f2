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

失败时（超量、时间逆序、未知样本）返回已完成的步骤、当时各管状态和
结构化错误信息，便于定位问题，不抛出业务异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import SampleIn, SimulationRequest, TransferIn, parse_time

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


def run_simulation(request: SimulationRequest) -> dict:
    """执行整次模拟，返回可直接序列化的结果字典（含 status / error）。"""
    samples: list[SampleIn] = request.samples
    transfers: list[TransferIn] = request.transfers

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

    steps: list[dict] = []
    error: Optional[dict] = None
    last_time: Optional[float] = None

    for index, transfer in enumerate(transfers):
        loc = f"transfers[{index}]"

        if transfer.source == transfer.target:
            error = _build_error(
                "SAME_SOURCE_TARGET",
                f"转移的源管与目标管相同（均为 {transfer.source!r}），不允许自转移",
                index,
                loc,
            )
            break

        source = tubes.get(transfer.source)
        if source is None:
            error = _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的源管 "
                f"{transfer.source!r}",
                index,
                f"{loc}.source",
            )
            break

        target = tubes.get(transfer.target)
        if target is None:
            error = _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的目标管 "
                f"{transfer.target!r}",
                index,
                f"{loc}.target",
            )
            break

        current_time = parse_time(transfer.time)
        if last_time is not None and current_time < last_time - ZERO_EPS:
            error = _build_error(
                "TIME_NOT_ORDERED",
                f"transfers[{index}] 的时间 {transfer.time!r} 早于上一次转移的时间"
                f"（transfers[{index - 1}]），转移必须按时间非递减排序",
                index,
                f"{loc}.time",
            )
            break

        available = source.volume
        # 超量仍按从源管吸取的 amount 校验
        if transfer.amount > available + ZERO_EPS:
            error = _build_error(
                "INSUFFICIENT_VOLUME",
                f"transfers[{index}] 试图从 {transfer.source!r} 吸取 "
                f"{transfer.amount}，但该管当前仅有 {_clean(available)}",
                index,
                f"{loc}.amount",
            )
            break

        # amount：实际吸出（移出源管）体积；delivered：实际进入目标管体积；
        # 二者差额即本步移液损耗。缺省 delivered_amount 时无损。
        amount = min(transfer.amount, available)
        if transfer.delivered_amount is None:
            delivered = amount
        else:
            delivered = min(transfer.delivered_amount, amount)
        lost_volume = amount - delivered

        # —— 转移前快照（用于守恒校验与转出液描述）——
        tube_vol_before = sum(t.volume for t in tubes.values())
        tube_mass_before = _total_marker_amount(tubes, all_markers)
        sys_vol_before = tube_vol_before + waste_volume
        sys_mass_before = {
            name: tube_mass_before[name] + waste_marker[name] for name in all_markers
        }
        source_concentrations = _concentrations(source, all_markers)

        # 吸出液的来源构成：按源管各来源占比分摊；进入目标管与计入
        # waste 的两部分各自按 delivered / lost 体积拆分
        delivered_shares: dict[str, float] = {}
        lost_shares: dict[str, float] = {}
        if available > ZERO_EPS:
            for src_id, src_vol in list(source.origins.items()):
                aspirated_share = src_vol * (amount / available)
                delivered_share = src_vol * (delivered / available)
                # 用残差得到损耗份额，减少浮点误差
                lost_share = aspirated_share - delivered_share
                source.origins[src_id] = src_vol - aspirated_share
                target.origins[src_id] = (
                    target.origins.get(src_id, 0.0) + delivered_share
                )
                waste_origins[src_id] = waste_origins.get(src_id, 0.0) + lost_share
                delivered_shares[src_id] = delivered_share
                lost_shares[src_id] = lost_share

        # —— 更新质量与体积（源管按 amount 移出；目标管按 delivered 进入；
        #    差额进入累计 waste）——
        for name in all_markers:
            concentration = source_concentrations[name]
            source.marker_amount[name] -= concentration * amount
            target.marker_amount[name] += concentration * delivered
            waste_marker[name] += concentration * lost_volume

        source.volume -= amount
        target.volume += delivered
        waste_volume += lost_volume

        tube_vol_after = sum(t.volume for t in tubes.values())
        tube_mass_after = _total_marker_amount(tubes, all_markers)
        sys_vol_after = tube_vol_after + waste_volume
        sys_mass_after = {
            name: tube_mass_after[name] + waste_marker[name] for name in all_markers
        }

        transferred = {
            "volume": _clean(delivered),
            "concentrations": {
                name: _clean(source_concentrations[name]) for name in all_markers
            },
            "origins": _origins_view(delivered_shares, delivered),
        }
        lost = {
            "volume": _clean(lost_volume),
            "concentrations": {
                name: _clean(source_concentrations[name]) for name in all_markers
            },
            "marker_amount": {
                name: _clean(source_concentrations[name] * lost_volume)
                for name in all_markers
            },
            "origins": _origins_view(lost_shares, lost_volume),
        }

        steps.append(
            {
                "index": index,
                "transfer_id": transfer.id,
                "time": transfer.time,
                "source": transfer.source,
                "target": transfer.target,
                "amount": _clean(amount),
                "delivered_amount": _clean(delivered),
                "transferred": transferred,
                "lost": lost,
                "source_after": _snapshot(source, all_markers),
                "target_after": _snapshot(target, all_markers),
                "conservation": _conservation(
                    sys_vol_before,
                    sys_vol_after,
                    tube_vol_after - tube_vol_before,
                    sys_mass_before,
                    sys_mass_after,
                    {
                        name: _clean(
                            tube_mass_after[name] - tube_mass_before[name]
                        )
                        for name in all_markers
                    },
                ),
            }
        )
        last_time = current_time

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
        "initial_states": initial_states,
        "steps": steps,
        "final_states": final_states,
        "waste": _waste_snapshot(
            waste_volume, waste_marker, waste_origins, all_markers
        ),
        "conservation": conservation,
        "error": error,
    }
