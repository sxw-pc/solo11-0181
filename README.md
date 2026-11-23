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

## 蜂蜜分装与罐级放行

`honey.import_blend` 只保存原配料表并写入整批暂评（按总公斤加权 ppm，阈值 3，见 `grade.assess`）；整批均值只是暂评，从不下推到任何成品罐，原配料表也不会被后续任何报告覆盖（重复 blend_id 拒绝写入）。

`fill.import_run(db, file, machine_keys)` 采入离线灌装机日志：阀门切换、罐号、逐脉冲（含当次阀门状态）与称重，每个事件以机器密钥 HMAC 签章。`flowgraph.solve_run` 据此求出来源桶→泵次→成品罐的有向质量流图，质量（克）与残留质量（微克）用有理数精确守恒；断脉冲、序号重复、签名不符、阀门状态矛盾、桶级不平衡、称重超差都会留下缺陷并使相关罐保持 unverified，已证范围体现为边的 `proven` 标记。

`lab.import_report` 采入实验室回执：带封签代表样的单罐结果（ppm=残留µg/样重g）或撤销回执。化验结论只作用于被抽样的那一罐，不外推给同车其他罐，也不能反推泵次流向；撤销使原报告失效，无有效报告的罐回到 unverified。

`jargrade.decide_jars` 把两路证据合并为版本化逐罐判定（release/hold/unverified，basis 为 flow/lab/flow+lab/conflict/none），`release_board` 给出整批暂评、逐罐状态、来源溯源与可售清单。`inventory` 按罐号执行出库/退回并派生库存状态（saleable/sold/held/frozen/recalled）：已出库罐判坏记 recalled，在库记 frozen/held——召回与冻结只按成品罐号执行，不改 B1 总状态。`replay_inventory` 从原始日志与回执重算判定和库存状态，与持久化版本核对。

合成 fixture 由 `tests/gen_honey_fixtures.py` 生成（机器演示密钥 `{'FILLER-1': 'fill-key-1'}`）：同一 B1（A 10kg×20ppm + B 90kg×0ppm，整批 2ppm/release）下，生产甲 J1=20ppm 扣留、J2≈1.0526ppm 放行，生产乙 J1=0ppm、J2≈2.1053ppm 两罐放行。
