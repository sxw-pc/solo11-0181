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

## 蜂箱出勤取证

原 `pollinate/audio.py` 的 `import_mic`/`import_audio` 与 `pollinate/activity.py` 的
`assess` 保留：RMS ≥ 0.4 仍写 `active`，但该行只是**暂计响度**，逐次变化写入
`hive_activity_log` 版本日志，`provisional_is_flight_fact` 恒为 false。

新增采录走 `import_capture`（声学/阵列/泵控）、`import_pump_events` 与
`import_infrared`。每个采录带采样序号、抗混叠滤波状态、共用时钟 epoch（`hive_clock`，
整段开机期一个映射，逐片段改相会被 `clock_epoch_mismatch` 拒收）、阵列几何与箱口遮挡。
断帧、序号缺口、时钟未闭合写入 `rejection`，绝不以零填充成“蜂群安静”。

`pollinate/flight_proof.py` 的 `assess_flight` 只输出可确证时段（`confirmed_acoustic`）、
待核时段（`pending` + `retest_capture_ids`）与安静时段，并写版本化
`flight_assessment`。规则：

- 200 Hz 采录的 180 Hz 箱口频段在 Nyquist 之外。20 Hz 泵与 180 Hz 蜂群折叠为同样的
  200 个采样，RMS、波形与 `active` 完全相同，频谱只显示 20 Hz；这类记录一律待核，
  不据谱峰写成蜂群事实。
- 泵运行/停止是证据，不是安静；断帧与时钟缺口不会被补零。
- 1 kHz 阵列用通道相位差估计方位，箱口方位（含遮挡通道）与泵方位可分辨时，
  方可确证箱口声源；泵与箱口同方位则仍待核。
- 红外计数器只确证其覆盖窗口内的进出数（`complete_within_coverage`），
  `extrapolated_beyond_coverage=false`，且不推断花粉落果。
- 任何路径都不把声学出勤写成合同已完成授粉（`contract_pollination_claim: none`）。

`explain` 返回原始采录哈希、被拒记录、暂计版本与出勤版本，供回溯。
`fixtures/hive-audio-alias.json` 提供两个逐点相同的合成世界（20 Hz 泵 vs 180 Hz 蜂群）、
1 kHz 阵列与两小时红外计数样例。
