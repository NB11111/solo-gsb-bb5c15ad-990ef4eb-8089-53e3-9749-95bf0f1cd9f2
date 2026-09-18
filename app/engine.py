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

执行模式（``execution_mode``）
------------------------------

* ``sequential``（默认）：逐条顺序执行，同一下标的转移按数组顺序先后
  生效，保持旧版行为；每条转移自成一批（``batch_index == index``）；
* ``time_batch``：解析后 ``time`` 相等的连续转移视为**一批原子操作**。
  批次内：

  * 所有吸取均基于**批次开始时的源管快照**计算浓度与来源构成，因此同批
    操作的计算结果与数组顺序无关（即便同一支管既收又发）；
  * 先按源管汇总本批 ``amount`` 与批次开始可用量比较，任一源管超量则
    整批不落地，错误定位到原 ``transfers`` 下标（首个导致累计超量的
    操作）；此前已提交批次的结果保留；
  * 校验通过后再把全部转移的目标管增量、源管减量与 waste 增量统一写入；
  * 同批各步的 ``source_after`` / ``target_after`` / ``conservation``
    均为整批执行后的视图（守恒以批次为单位，覆盖多源、多目标、同管收发
    及 delivered_amount 损耗），``batch_index`` 相同。

失败时（超量、时间逆序、未知样本）返回已完成批次 / 步骤、当时各管状态和
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


def _validate_batch_structure(
    batch: list[TransferIn],
    start_index: int,
    tubes: dict[str, _Tube],
) -> Optional[dict]:
    """逐操作检查自转移与未知源 / 目标管（不改变任何状态）。"""
    for offset, transfer in enumerate(batch):
        index = start_index + offset
        loc = f"transfers[{index}]"

        if transfer.source == transfer.target:
            return _build_error(
                "SAME_SOURCE_TARGET",
                f"转移的源管与目标管相同（均为 {transfer.source!r}），不允许自转移",
                index,
                loc,
            )
        if tubes.get(transfer.source) is None:
            return _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的源管 "
                f"{transfer.source!r}",
                index,
                f"{loc}.source",
            )
        if tubes.get(transfer.target) is None:
            return _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的目标管 "
                f"{transfer.target!r}",
                index,
                f"{loc}.target",
            )
    return None


def _validate_batch_volume(
    batch: list[TransferIn],
    start_index: int,
    tubes: dict[str, _Tube],
    batch_size: int,
    mode: str,
) -> Optional[dict]:
    """基于批次开始快照，按源管汇总 amount 校验可用量。

    错误下标定位到原 transfers 序列中**首个**使某源管累计吸取量超过其
    批次开始可用量的操作。
    """
    requested: dict[str, float] = {}
    for offset, transfer in enumerate(batch):
        source_id = transfer.source
        total = requested.get(source_id, 0.0) + transfer.amount
        requested[source_id] = total
        available = tubes[source_id].volume
        if total > available + ZERO_EPS:
            index = start_index + offset
            if mode == "time_batch" and batch_size > 1:
                message = (
                    f"transfers[{index}] 使批次（batch 起始于 "
                    f"transfers[{start_index}]，共 {batch_size} 个同时刻操作）"
                    f"从 {source_id!r} 的累计吸取量达到 {_clean(total)}，"
                    f"但该管批次开始时仅有 {_clean(available)}"
                )
            else:
                message = (
                    f"transfers[{index}] 试图从 {source_id!r} 吸取 "
                    f"{transfer.amount}，但该管当前仅有 {_clean(available)}"
                )
            return _build_error(
                "INSUFFICIENT_VOLUME",
                message,
                index,
                f"transfers[{index}].amount",
            )
    return None


def _execute_batch(
    batch: list[TransferIn],
    start_index: int,
    batch_number: int,
    tubes: dict[str, _Tube],
    waste_volume: float,
    waste_marker: dict[str, float],
    waste_origins: dict[str, float],
    all_markers: list[str],
) -> tuple[list[dict], float, dict[str, float], dict[str, float]]:
    """原子执行一批转移，返回 (steps, 新 waste 体积, 新 waste 质量, 新 waste 来源)。

    批次内所有吸取均基于批次开始快照：各操作的浓度与来源分摊先全部计算
    为按管汇总的增量，再统一写入，结果与批次内数组顺序无关。
    """
    # —— 批次开始快照（只读基线）——
    start_volume = {tid: t.volume for tid, t in tubes.items()}
    start_origins = {tid: dict(t.origins) for tid, t in tubes.items()}
    start_concentrations = {
        tid: _concentrations(t, all_markers) for tid, t in tubes.items()
    }

    tube_vol_before = sum(start_volume.values())
    tube_mass_before = _total_marker_amount(tubes, all_markers)
    sys_vol_before = tube_vol_before + waste_volume
    sys_mass_before = {
        name: tube_mass_before[name] + waste_marker[name] for name in all_markers
    }

    # 按管汇总的统一写入增量
    vol_delta: dict[str, float] = {tid: 0.0 for tid in tubes}
    mass_delta: dict[str, float] = {
        tid: {name: 0.0 for name in all_markers} for tid in tubes
    }
    origins_delta: dict[str, dict[str, float]] = {
        tid: {} for tid in tubes
    }
    new_waste_volume = waste_volume
    new_waste_marker = dict(waste_marker)
    new_waste_origins = dict(waste_origins)

    records: list[dict] = []

    for offset, transfer in enumerate(batch):
        index = start_index + offset
        source = tubes[transfer.source]
        target = tubes[transfer.target]

        available = start_volume[source.id]
        # 超量已在批次校验阶段拦截；min 仅作为浮点兜底
        amount = min(transfer.amount, available)
        if transfer.delivered_amount is None:
            delivered = amount
        else:
            delivered = min(transfer.delivered_amount, amount)
        lost_volume = amount - delivered

        source_concs = start_concentrations[source.id]

        # 吸出液的来源构成：按源管**批次开始时**的来源占比分摊；
        # 进入目标管与计入 waste 的两部分各自按 delivered / lost 体积拆分
        delivered_shares: dict[str, float] = {}
        lost_shares: dict[str, float] = {}
        if available > ZERO_EPS:
            for src_id, src_vol in start_origins[source.id].items():
                aspirated_share = src_vol * (amount / available)
                delivered_share = src_vol * (delivered / available)
                # 用残差得到损耗份额，减少浮点误差
                lost_share = aspirated_share - delivered_share
                origins_delta[source.id][src_id] = (
                    origins_delta[source.id].get(src_id, 0.0) - aspirated_share
                )
                origins_delta[target.id][src_id] = (
                    origins_delta[target.id].get(src_id, 0.0) + delivered_share
                )
                new_waste_origins[src_id] = (
                    new_waste_origins.get(src_id, 0.0) + lost_share
                )
                delivered_shares[src_id] = delivered_share
                lost_shares[src_id] = lost_share

        # —— 源管按 amount 移出；目标管按 delivered 进入；差额进入 waste ——
        vol_delta[source.id] -= amount
        vol_delta[target.id] += delivered
        new_waste_volume += lost_volume
        for name in all_markers:
            concentration = source_concs[name]
            mass_delta[source.id][name] -= concentration * amount
            mass_delta[target.id][name] += concentration * delivered
            new_waste_marker[name] = (
                new_waste_marker.get(name, 0.0) + concentration * lost_volume
            )

        records.append(
            {
                "index": index,
                "transfer": transfer,
                "amount": amount,
                "delivered": delivered,
                "lost_volume": lost_volume,
                "source_concentrations": source_concs,
                "delivered_shares": delivered_shares,
                "lost_shares": lost_shares,
            }
        )

    # —— 全部操作计算完毕，统一写入 ——
    for tid, tube in tubes.items():
        tube.volume += vol_delta[tid]
        if abs(tube.volume) < ZERO_EPS:
            tube.volume = 0.0
        for name in all_markers:
            tube.marker_amount[name] += mass_delta[tid][name]
        for src_id, delta in origins_delta[tid].items():
            tube.origins[src_id] = tube.origins.get(src_id, 0.0) + delta

    tube_vol_after = sum(t.volume for t in tubes.values())
    tube_mass_after = _total_marker_amount(tubes, all_markers)
    sys_vol_after = tube_vol_after + new_waste_volume
    sys_mass_after = {
        name: tube_mass_after[name] + new_waste_marker[name] for name in all_markers
    }

    # 整批执行后的统一视图与批次守恒：同批各步共享
    snapshot_after = {tid: _snapshot(t, all_markers) for tid, t in tubes.items()}
    conservation = _conservation(
        sys_vol_before,
        sys_vol_after,
        tube_vol_after - tube_vol_before,
        sys_mass_before,
        sys_mass_after,
        {
            name: _clean(tube_mass_after[name] - tube_mass_before[name])
            for name in all_markers
        },
    )

    steps: list[dict] = []
    for record in records:
        transfer = record["transfer"]
        source_concs = record["source_concentrations"]
        lost_volume = record["lost_volume"]
        transferred = {
            "volume": _clean(record["delivered"]),
            "concentrations": {
                name: _clean(source_concs[name]) for name in all_markers
            },
            "origins": _origins_view(
                record["delivered_shares"], record["delivered"]
            ),
        }
        lost = {
            "volume": _clean(lost_volume),
            "concentrations": {
                name: _clean(source_concs[name]) for name in all_markers
            },
            "marker_amount": {
                name: _clean(source_concs[name] * lost_volume)
                for name in all_markers
            },
            "origins": _origins_view(record["lost_shares"], lost_volume),
        }
        steps.append(
            {
                "index": record["index"],
                "batch_index": batch_number,
                "transfer_id": transfer.id,
                "time": transfer.time,
                "source": transfer.source,
                "target": transfer.target,
                "amount": _clean(record["amount"]),
                "delivered_amount": _clean(record["delivered"]),
                "transferred": transferred,
                "lost": lost,
                "source_after": snapshot_after[transfer.source],
                "target_after": snapshot_after[transfer.target],
                "conservation": conservation,
            }
        )

    return steps, new_waste_volume, new_waste_marker, new_waste_origins


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
    batch_count = 0

    # —— 按执行模式切批：sequential 每条一批；time_batch 合并解析后 time
    #    相等的连续转移 ——
    position = 0
    total = len(transfers)
    while position < total:
        if request.execution_mode == "time_batch":
            batch_time = parse_time(transfers[position].time)
            end = position + 1
            while end < total and parse_time(transfers[end].time) == batch_time:
                end += 1
        else:
            batch_time = parse_time(transfers[position].time)
            end = position + 1
        batch = transfers[position:end]
        batch_number = batch_count

        # 结构性校验：自转移 / 未知样本（与 sequential 旧版一致，优先于
        # 时间逆序检查）
        structure_error = _validate_batch_structure(batch, position, tubes)
        if structure_error is not None:
            error = structure_error
            break

        # 时间逆序检查（以上一批最后一个 time 为准）
        if last_time is not None and batch_time < last_time - ZERO_EPS:
            error = _build_error(
                "TIME_NOT_ORDERED",
                f"transfers[{position}] 的时间 {transfers[position].time!r} 早于上一批"
                f"转移的时间（截至 transfers[{position - 1}]），转移必须按时间"
                f"非递减排序",
                position,
                f"transfers[{position}].time",
            )
            break

        # 可用量校验：time_batch 下按源管汇总批次内全部 amount
        volume_error = _validate_batch_volume(
            batch, position, tubes, len(batch), request.execution_mode
        )
        if volume_error is not None:
            error = volume_error
            break

        # 校验全部通过：原子落地整批
        batch_steps, waste_volume, waste_marker, waste_origins = _execute_batch(
            batch,
            position,
            batch_number,
            tubes,
            waste_volume,
            waste_marker,
            waste_origins,
            all_markers,
        )
        steps.extend(batch_steps)
        last_time = batch_time
        position = end
        batch_count += 1

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
