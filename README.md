# 蜂群授粉履约采证台

蜂箱跨果园进出场、花期窗口、喷药禁入与授粉覆盖决定服务是否可结算；离线扫描卡、田间观察表和合同附件可以乱序到达。

三套子系统通过原始证据、SQLite 状态与版本化决定互相影响。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pollinate init --db work.db
python -m pollinate demo --db work.db
python -m unittest discover -s tests -v
```

命令：dispatch, bloom, settle。每个命令通过 `--fixture` 读取单一子系统的 JSON；`demo` 依次执行正常样例。每项报告列证据 ID 和决定版本。


## 证据和状态语义

`event_time` 是证据所述事实发生时间，`received_at` 是本机收到时间。健康记录的 `valid_from/valid_to`、花期的 `window`、派驻的 `start/end` 与喷药的 `start/end/delivered_at` 共用整数时间轴；区间右端不包含在内。每个分区把替补蜂箱区间合并，避免同一额度重叠计费。喷药通知只有送达后的时段进入禁入区间。

结算按有效分区蜂箱小时与合同最低量比较。争议事件按 `dispute_id` 冻结或释放款项；释放时有效结算额须覆盖冻结额。新证据生成新版本，不删除旧结论。`python -m pollinate replay --db work.db --subject case-1` 从原始证据独立重算十项决定并与持久化的最新版本核对，返回 `match` 与差异。

## 运输安全适载（weigh / load / dispatch）

桥秤总重合格不等于车辆适载。固定副本路径 `pollinate/weigh.py::import_ticket` 原样保存称重票（可挂 HMAC 验签），`pollinate/load.py::assess` 按 `gross_kg−tare_kg` 算净重并把净重对半写入 `trailer_load.front_kg/rear_kg`。该均分算式不掌握任何托盘几何，只是总重口径的**常规解**，不得作为逐轴适载结论。

运输安全结论走独立审计账，与“蜂箱检疫/健康放行”分离（健康放行不证明车辆适载）：

- `transport_event`：轴位几何、逐托盘清单（重量/重心/坐标误差/箱数）、急刹工况（重心高、减速度 g）、复称、半途卸货、跨车交接、绑扎签认、处置路线，全部事件化、幂等去重、冲突拒绝、可乱序到达。
- `axle_envelope`：按静力平衡 `前轴=W·(xr−x)/(xr−xf)`、`后轴=W·(x−xf)/(xr−xf)` 与制动转移 `Δ=W·a·h/L` 输出带几何、托盘来源和工况范围的逐轴包络；坐标有误差区间时核最坏轴载。缺几何、缺托盘清单或缺有效急刹工况时结论为“暂停”，不会静默放行。
- `load_disposition`：处置版本（路线 A 重排 / 路线 B 跨车分运），含车次、每车托盘与重量、原货物去向 lineage 和总量对账（跨车转移不双计，交接凭据缺失即暂停）。
- `transport_decision`：`车辆运输安全适载` 独立决定面，版本化并附 proof 证据 ID。

合成例 L2（`fixtures/l2_transport.json`，由 `pollinate.weigh.run_l2_case` 采录）：W1 净重 1800kg，轴距 3m，三托盘合成重心 x=2.7m，每轴上限 1200kg，急刹 0.35g、重心高 1.2m。

| 口径 | 前轴 | 后轴 | 结论 |
| --- | --- | --- | --- |
| 桥秤总重 + 均分常规解 | 900 | 900 | 误放行（1800 < 2400 合计容量） |
| L2 静力反力 | 180 | 1620 | 后轴超限 420 |
| L2 急刹包络（Δ=252） | 432 | 1368 | 后轴仍超 168 |
| 路线 A：重排至重心 x=1.5，重绑+复称 | 1152（刹）/900（静） | 648（刹）/900（静） | 单车可发 |
| 路线 B：后端 600kg 转 V2（Δ=168） | 348（刹）/180（静） | 852（刹）/1020（静） | 两车可发，需交接账 |

`load.replay_transport(db, ticket_id)` 在空库从原始秤票与事件流重建全部账册并逐表比对，可检测被篡改的包络；同一桥秤票重复校验无法补回缺失的重心信息。

