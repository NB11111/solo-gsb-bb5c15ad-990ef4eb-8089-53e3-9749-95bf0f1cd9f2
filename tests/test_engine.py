"""引擎层测试：混合浓度、来源占比、守恒与各类业务错误。"""
from app.engine import run_simulation
from app.models import SampleIn, SimulationRequest, TransferIn


def make_request():
    return SimulationRequest(
        name="demo",
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=40.0),
            TransferIn(time=2, source="B", target="A", amount=70.0),
        ],
    )


def test_basic_mixing_concentrations():
    result = run_simulation(make_request())

    assert result["status"] == "success"
    assert len(result["steps"]) == 2

    # 第一步：A(100, c=10) -> B 转移 40
    # B 变为 140，质量 400 => 浓度 400/140 = 2.8571...
    step1 = result["steps"][0]
    assert step1["source_after"]["volume"] == 60
    assert step1["source_after"]["concentrations"]["default"] == 10.0
    assert step1["target_after"]["volume"] == 140
    assert abs(step1["target_after"]["concentrations"]["default"] - 400 / 140) < 1e-9

    # 第二步：B(140, c=2.8571) -> A 转移 70（转出 B 的一半）
    step2 = result["steps"][1]
    assert step2["source_after"]["volume"] == 70
    assert step2["target_after"]["volume"] == 130
    # A：质量 600 + 200 = 800，体积 130
    assert abs(step2["target_after"]["concentrations"]["default"] - 800 / 130) < 1e-9
    # B 剩余：质量 200，体积 70
    assert abs(step2["source_after"]["concentrations"]["default"] - 200 / 70) < 1e-9


def test_origin_fractions_and_conservation():
    result = run_simulation(make_request())

    final_a = next(t for t in result["final_states"] if t["id"] == "A")
    final_b = next(t for t in result["final_states"] if t["id"] == "B")

    # 第二步后 A 的来源：原 A 剩 60 + 从 B 回流入 A 的 20(A)+50(B)
    # => A 来源 80，B 来源 50，总体积 130
    origins_a = {o["source_id"]: o for o in final_a["origins"]}
    assert abs(origins_a["A"]["volume"] - 80) < 1e-9
    assert abs(origins_a["B"]["volume"] - 50) < 1e-9
    assert abs(origins_a["A"]["volume_fraction"] - 80 / 130) < 1e-9
    assert abs(sum(o["volume_fraction"] for o in final_a["origins"]) - 1.0) < 1e-9

    # B 剩余：A 20 + B 50
    origins_b = {o["source_id"]: o for o in final_b["origins"]}
    assert abs(origins_b["A"]["volume"] - 20) < 1e-9
    assert abs(origins_b["B"]["volume"] - 50) < 1e-9

    conservation = result["conservation"]
    assert conservation["initial_total_volume"] == 200
    assert conservation["final_total_volume"] == 200
    assert conservation["volume_difference"] == 0
    assert conservation["initial_marker_amount"]["default"] == 1000
    assert conservation["final_marker_amount"]["default"] == 1000
    assert conservation["marker_amount_difference"]["default"] == 0

    for step in result["steps"]:
        assert step["conservation"]["volume_difference"] == 0
        assert step["conservation"]["marker_amount_difference"]["default"] == 0


def test_multi_marker_mixing():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, markers={"X": 2.0, "Y": 8.0}),
            SampleIn(id="B", initial_volume=10.0, markers={"X": 6.0}),
        ],
        transfers=[TransferIn(time=1, source="A", target="B", amount=10.0)],
    )
    result = run_simulation(request)
    assert result["status"] == "success"

    b = result["steps"][0]["target_after"]
    # B：X 质量 60 + 20 = 80；Y 质量 80；体积 20
    assert b["concentrations"]["X"] == 4.0
    assert b["concentrations"]["Y"] == 4.0
    # A 已空，所有标记物浓度按 0 返回
    a = result["steps"][0]["source_after"]
    assert a["volume"] == 0
    assert a["concentrations"] == {"X": 0.0, "Y": 0.0}
    assert a["origins"] == []


def test_full_transfer_conservation():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=5.0, concentration=3.0)],
        transfers=[],  # 空操作序列也合法
    )
    result = run_simulation(request)
    assert result["status"] == "success"
    assert result["steps"] == []
    assert result["conservation"]["volume_difference"] == 0


def test_insufficient_volume_error():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[TransferIn(time=1, source="A", target="B", amount=11.0)],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    error = result["error"]
    assert error["code"] == "INSUFFICIENT_VOLUME"
    assert error["transfer_index"] == 0
    assert error["location"] == "transfers[0].amount"
    # 失败时不产生步骤，状态停留在初始
    assert result["steps"] == []


def test_unknown_source_error_location():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[TransferIn(time=1, source="C", target="A", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["location"] == "transfers[0].source"


def test_unknown_target_error_location():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=5.0, concentration=0.0),
        ],
        transfers=[TransferIn(time=1, source="A", target="C", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["location"] == "transfers[0].target"


def test_time_reversal_error():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=2, source="A", target="B", amount=1.0),
            TransferIn(time=1, source="A", target="B", amount=1.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIME_NOT_ORDERED"
    assert result["error"]["transfer_index"] == 1
    assert result["error"]["location"] == "transfers[1].time"
    # 第一步成功保留
    assert len(result["steps"]) == 1


def test_iso_time_ordering_supported():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time="2026-09-18T10:00:00Z", source="A", target="B", amount=1.0),
            TransferIn(time="2026-09-18T11:00:00Z", source="A", target="B", amount=1.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"


def test_same_source_target_rejected():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[TransferIn(time=1, source="A", target="A", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "SAME_SOURCE_TARGET"


def test_pipetting_loss_tracked():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=40.0, delivered_amount=36.0
            )
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"

    step = result["steps"][0]
    # amount 仍是吸出量；目标管只收到 delivered_amount
    assert step["amount"] == 40.0
    assert step["delivered_amount"] == 36.0
    assert step["source_after"]["volume"] == 60.0
    assert step["target_after"]["volume"] == 136.0
    assert abs(step["target_after"]["concentrations"]["default"] - 360 / 136) < 1e-9

    # 进入目标管的部分
    transferred = step["transferred"]
    assert transferred["volume"] == 36.0
    assert transferred["concentrations"]["default"] == 10.0
    assert transferred["origins"] == [
        {"source_id": "A", "volume": 36.0, "volume_fraction": 1.0}
    ]

    # 损耗 4：按源管当时浓度携带质量，来源占比 100% A
    lost = step["lost"]
    assert lost["volume"] == 4.0
    assert lost["concentrations"]["default"] == 10.0
    assert lost["marker_amount"]["default"] == 40.0
    assert lost["origins"] == [
        {"source_id": "A", "volume": 4.0, "volume_fraction": 1.0}
    ]

    # 该步守恒：系统口径（管内 + 累计 waste）差值为 0；
    # 仅看管内则恰好下降 lost 部分
    conservation = step["conservation"]
    assert conservation["volume_before"] == 200.0
    assert conservation["volume_after"] == 200.0
    assert conservation["volume_difference"] == 0.0
    assert conservation["tube_volume_change"] == -4.0
    assert conservation["marker_amount_before"]["default"] == 1000.0
    assert conservation["marker_amount_after"]["default"] == 1000.0
    assert conservation["marker_amount_difference"]["default"] == 0.0
    assert conservation["tube_marker_amount_change"]["default"] == -40.0

    # 累计 waste
    waste = result["waste"]
    assert waste["volume"] == 4.0
    assert waste["marker_amount"]["default"] == 40.0
    assert waste["origins"] == [
        {"source_id": "A", "volume": 4.0, "volume_fraction": 1.0}
    ]

    # 全程守恒（系统口径为 0，管内口径下降量等于 waste）
    total = result["conservation"]
    assert total["initial_total_volume"] == 200.0
    assert total["final_total_volume"] == 200.0
    assert total["volume_difference"] == 0.0
    assert total["tube_volume_change"] == -4.0
    assert total["initial_marker_amount"]["default"] == 1000.0
    assert total["final_marker_amount"]["default"] == 1000.0
    assert total["marker_amount_difference"]["default"] == 0.0
    assert total["tube_marker_amount_change"]["default"] == -40.0


def test_loss_origins_proportional_and_cumulative():
    # step1: A->B 吸 40 到 30（损耗 10，全部来自 A）
    # step2: B(体积130，来源 A30/B100) -> A 吸 65 到 60（损耗 5，
    #        按 B 内来源比例分摊：A 5*30/130，B 5*100/130）
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=40.0, delivered_amount=30.0
            ),
            TransferIn(
                time=2, source="B", target="A", amount=65.0, delivered_amount=60.0
            ),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"

    step2_lost = result["steps"][1]["lost"]
    assert abs(step2_lost["volume"] - 5.0) < 1e-9
    assert abs(step2_lost["marker_amount"]["default"] - 5.0 * 300 / 130) < 1e-9
    lost_origins = {o["source_id"]: o for o in step2_lost["origins"]}
    assert abs(lost_origins["A"]["volume"] - 5.0 * 30 / 130) < 1e-9
    assert abs(lost_origins["B"]["volume"] - 5.0 * 100 / 130) < 1e-9

    # 累计 waste：体积 15；来源 A 10+1.1538...，B 3.846...
    waste = result["waste"]
    assert abs(waste["volume"] - 15.0) < 1e-9
    waste_origins = {o["source_id"]: o for o in waste["origins"]}
    assert abs(waste_origins["A"]["volume"] - (10.0 + 5.0 * 30 / 130)) < 1e-9
    assert abs(waste_origins["B"]["volume"] - 5.0 * 100 / 130) < 1e-9
    assert (
        abs(sum(o["volume_fraction"] for o in waste["origins"]) - 1.0) < 1e-9
    )

    # 管内总体积 185 + waste 15 = 200；系统质量差恒为 0
    final_volumes = sum(t["volume"] for t in result["final_states"])
    assert abs(final_volumes - 185.0) < 1e-9
    total = result["conservation"]
    assert total["final_total_volume"] == 200.0
    assert total["volume_difference"] == 0.0
    assert total["tube_volume_change"] == -15.0
    assert total["marker_amount_difference"]["default"] == 0.0
    assert abs(total["tube_marker_amount_change"]["default"] - (-100 - 5 * 300 / 130)) < 1e-9

    for step in result["steps"]:
        assert step["conservation"]["volume_difference"] == 0.0
        assert step["conservation"]["marker_amount_difference"]["default"] == 0.0
        assert (
            step["conservation"]["tube_volume_change"]
            == -step["lost"]["volume"]
        )


def test_default_delivered_amount_is_lossless():
    # 旧请求不传 delivered_amount：视为无损，响应仍包含新字段但损耗为 0
    result = run_simulation(make_request())
    for step in result["steps"]:
        assert step["delivered_amount"] == step["amount"]
        assert step["transferred"]["volume"] == step["amount"]
        assert step["lost"]["volume"] == 0.0
        assert step["lost"]["marker_amount"]["default"] == 0.0
        assert step["lost"]["origins"] == []
        assert step["conservation"]["tube_volume_change"] == 0.0
        assert step["conservation"]["tube_marker_amount_change"]["default"] == 0.0

    assert result["waste"]["volume"] == 0.0
    assert result["waste"]["marker_amount"]["default"] == 0.0
    assert result["waste"]["origins"] == []
    assert result["conservation"]["tube_volume_change"] == 0.0
    assert result["conservation"]["tube_marker_amount_change"]["default"] == 0.0


def test_overdraw_still_validated_by_amount():
    # 即使 delivered_amount 较小，超量仍按 amount（吸出量）校验
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=11.0, delivered_amount=1.0
            )
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INSUFFICIENT_VOLUME"
    assert result["error"]["location"] == "transfers[0].amount"
    assert result["waste"]["volume"] == 0.0


# ---------- execution_mode: time_batch ----------


def _run_time_batch(samples, transfers):
    return run_simulation(
        SimulationRequest(
            samples=samples, transfers=transfers, execution_mode="time_batch"
        )
    )


def test_sequential_mode_batch_index_equals_index():
    result = run_simulation(make_request())
    assert [step["batch_index"] for step in result["steps"]] == [0, 1]
    assert [step["index"] for step in result["steps"]] == [0, 1]


def test_time_batch_groups_equal_time_and_assigns_batch_index():
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
            SampleIn(id="C", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=10.0),
            TransferIn(time=1, source="A", target="C", amount=20.0),
            TransferIn(time=2, source="B", target="A", amount=5.0),
        ],
    )
    assert result["status"] == "success"
    assert [step["index"] for step in result["steps"]] == [0, 1, 2]
    assert [step["batch_index"] for step in result["steps"]] == [0, 0, 1]

    volumes = {t["id"]: t["volume"] for t in result["final_states"]}
    # 第一批：A 基于批次开始快照被合计吸取 30，B、C 各收 10 / 20；
    # 第二批：B -> A 5，A 收回 5
    assert volumes == {"A": 75.0, "B": 105.0, "C": 120.0}

    # 同批各步的 after / conservation 都是整批后的同一视图
    assert result["steps"][0]["source_after"] == result["steps"][1]["source_after"]
    assert (
        result["steps"][0]["conservation"] == result["steps"][1]["conservation"]
    )
    batch_conservation = result["steps"][0]["conservation"]
    assert batch_conservation["volume_before"] == 300.0
    assert batch_conservation["volume_after"] == 300.0
    assert batch_conservation["volume_difference"] == 0.0


def test_time_batch_order_independent_with_same_tube_send_and_receive():
    # 同一时刻 A->B 与 B->A：无论数组顺序如何，吸取都基于批次开始快照
    samples = [
        SampleIn(id="A", initial_volume=10.0, concentration=10.0),
        SampleIn(id="B", initial_volume=10.0, concentration=0.0),
    ]
    order_one = [
        TransferIn(time=1, source="A", target="B", amount=6.0),
        TransferIn(time=1, source="B", target="A", amount=6.0),
    ]
    order_two = [
        TransferIn(time=1, source="B", target="A", amount=6.0),
        TransferIn(time=1, source="A", target="B", amount=6.0),
    ]

    result_one = _run_time_batch(samples, order_one)
    result_two = _run_time_batch(samples, order_two)

    assert result_one["final_states"] == result_two["final_states"]
    assert result_one["waste"] == result_two["waste"]
    assert result_one["conservation"] == result_two["conservation"]

    final = {t["id"]: t for t in result_one["final_states"]}
    # 体积各自一吸一收保持 10；A 质量 100-60=40（收自 B 的为 0），B 质量 60
    assert final["A"]["volume"] == 10.0
    assert final["B"]["volume"] == 10.0
    assert final["A"]["concentrations"]["default"] == 4.0
    assert final["B"]["concentrations"]["default"] == 6.0
    # 来源构成：A 剩 A 来源 4 + 收 B 来源 6；B 剩 B 来源 4 + 收 A 来源 6
    origins_a = {o["source_id"]: o for o in final["A"]["origins"]}
    assert origins_a["A"]["volume"] == 4.0
    assert origins_a["B"]["volume"] == 6.0
    origins_b = {o["source_id"]: o for o in final["B"]["origins"]}
    assert origins_b["A"]["volume"] == 6.0
    assert origins_b["B"]["volume"] == 4.0

    # 每步看到的都是整批后的视图：A->B 的 source_after 与 B->A 的
    # target_after 同为终态 A
    assert (
        result_one["steps"][0]["source_after"]
        == result_one["steps"][1]["target_after"]
        == final["A"]
    )


def test_time_batch_multi_source_multi_target_and_loss_conservation():
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, markers={"RFP": 8.0}),
            SampleIn(id="C", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="C", amount=40.0),
            TransferIn(
                time=1, source="B", target="C", amount=30.0, delivered_amount=28.0
            ),
        ],
    )
    assert result["status"] == "success"
    assert [step["batch_index"] for step in result["steps"]] == [0, 0]

    volumes = {t["id"]: t["volume"] for t in result["final_states"]}
    # A 出 40；B 吸出 30（只入管 28，损耗 2）；C 收 40+28
    assert volumes == {"A": 60.0, "B": 70.0, "C": 168.0}

    c = next(t for t in result["final_states"] if t["id"] == "C")
    assert abs(c["concentrations"]["default"] - 400 / 168) < 1e-9
    assert abs(c["concentrations"]["RFP"] - 224 / 168) < 1e-9

    # 批次级守恒覆盖多源、多目标与 delivered_amount 损耗
    for step in result["steps"]:
        conservation = step["conservation"]
        assert conservation["volume_before"] == 300.0
        assert conservation["volume_after"] == 300.0
        assert conservation["volume_difference"] == 0.0
        assert conservation["tube_volume_change"] == -2.0
        assert conservation["marker_amount_difference"]["default"] == 0.0
        assert conservation["marker_amount_difference"]["RFP"] == 0.0
        assert conservation["tube_marker_amount_change"]["RFP"] == -16.0

    waste = result["waste"]
    assert waste["volume"] == 2.0
    assert waste["marker_amount"]["RFP"] == 16.0
    total = result["conservation"]
    assert total["volume_difference"] == 0.0
    assert total["tube_volume_change"] == -2.0
    assert total["marker_amount_difference"]["RFP"] == 0.0


def test_time_batch_aggregate_overdraw_aborts_whole_batch():
    # 单条不超量、合计 110 > 批次开始可用量 100：整批不落地
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=60.0),
            TransferIn(time=1, source="A", target="B", amount=50.0),
        ],
    )
    assert result["status"] == "failed"
    error = result["error"]
    assert error["code"] == "INSUFFICIENT_VOLUME"
    assert error["transfer_index"] == 1
    assert error["location"] == "transfers[1].amount"
    # 原子性：整批没有任何一步落地，状态停留在批次开始（初始）
    assert result["steps"] == []
    assert {t["id"]: t["volume"] for t in result["final_states"]} == {
        "A": 100.0,
        "B": 100.0,
    }
    assert result["waste"]["volume"] == 0.0


def test_time_batch_failed_batch_keeps_previous_batches():
    # 第一批成功；第二批对 A 合计吸取 60，但第一批后 A 只剩 50
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=50.0),
            TransferIn(time=2, source="A", target="B", amount=30.0),
            TransferIn(time=2, source="A", target="B", amount=30.0),
        ],
    )
    assert result["status"] == "failed"
    assert result["error"]["transfer_index"] == 2
    assert result["error"]["location"] == "transfers[2].amount"

    # 之前批次（batch 0）结果保留，第二批完全不落地
    assert len(result["steps"]) == 1
    assert result["steps"][0]["batch_index"] == 0
    assert {t["id"]: t["volume"] for t in result["final_states"]} == {
        "A": 50.0,
        "B": 150.0,
    }


def test_time_batch_losses_from_batch_start_snapshot():
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=50.0, delivered_amount=48.0
            ),
            TransferIn(
                time=1, source="A", target="B", amount=10.0, delivered_amount=9.0
            ),
        ],
    )
    assert result["status"] == "success"

    assert result["steps"][0]["lost"]["volume"] == 2.0
    assert result["steps"][1]["lost"]["volume"] == 1.0
    # 吸出浓度都基于批次开始快照（10.0），与操作顺序无关
    assert result["steps"][0]["transferred"]["concentrations"]["default"] == 10.0
    assert result["steps"][1]["transferred"]["concentrations"]["default"] == 10.0

    volumes = {t["id"]: t["volume"] for t in result["final_states"]}
    assert volumes == {"A": 40.0, "B": 157.0}
    assert result["waste"]["volume"] == 3.0
    assert result["waste"]["marker_amount"]["default"] == 30.0

    for step in result["steps"]:
        conservation = step["conservation"]
        assert conservation["volume_difference"] == 0.0
        assert conservation["tube_volume_change"] == -3.0
        assert conservation["tube_marker_amount_change"]["default"] == -30.0


def test_time_batch_iso_times_grouped():
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time="2026-09-18T10:00:00Z", source="A", target="B", amount=1.0
            ),
            TransferIn(
                time="2026-09-18T10:00:00Z", source="A", target="B", amount=1.0
            ),
            TransferIn(
                time="2026-09-18T11:00:00Z", source="A", target="B", amount=1.0
            ),
        ],
    )
    assert result["status"] == "success"
    assert [step["batch_index"] for step in result["steps"]] == [0, 0, 1]


def test_time_batch_time_reversal_reports_original_index():
    result = _run_time_batch(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=2, source="A", target="B", amount=1.0),
            TransferIn(time=2, source="B", target="A", amount=1.0),
            TransferIn(time=1, source="A", target="B", amount=1.0),
        ],
    )
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIME_NOT_ORDERED"
    assert result["error"]["transfer_index"] == 2
    assert result["error"]["location"] == "transfers[2].time"
    # 逆序批次之前的第一批（两步）保留
    assert len(result["steps"]) == 2
    assert {step["batch_index"] for step in result["steps"]} == {0}


def test_time_batch_unknown_sample_aborts_whole_batch():
    # 结构性错误同样整批不落地；先批后错，之前批次保留
    result = _run_time_batch(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[
            TransferIn(time=1, source="A", target="X", amount=1.0),
            TransferIn(time=1, source="A", target="X", amount=1.0),
        ],
    )
    assert result["status"] == "failed"
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["transfer_index"] == 0
    assert result["error"]["location"] == "transfers[0].target"
    assert result["steps"] == []
    assert result["final_states"][0]["volume"] == 10.0


def test_structure_error_precedence_over_time_matches_sequential():
    # 同时存在“未知管”和“时间逆序”时，两种模式都先报结构错误
    def build(mode):
        return SimulationRequest(
            samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
            transfers=[
                TransferIn(time=2, source="A", target="X", amount=1.0),
                TransferIn(time=1, source="A", target="A", amount=1.0),
            ],
            execution_mode=mode,
        )

    sequential = run_simulation(build("sequential"))
    time_batch = run_simulation(build("time_batch"))
    assert sequential["error"]["code"] == "UNKNOWN_SAMPLE"
    assert sequential["error"]["transfer_index"] == 0
    assert time_batch["error"]["code"] == "UNKNOWN_SAMPLE"
    assert time_batch["error"]["transfer_index"] == 0
