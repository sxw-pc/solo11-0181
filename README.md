# 蜂群授粉履约采证台

蜂箱跨果园进出场、花期窗口、喷药禁入与授粉覆盖决定服务是否可结算；离线扫描卡、田间观察表和合同附件可以乱序到达。

三套子系统通过原始证据、SQLite 状态与版本化决定互相影响。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pollinate init --db work.db
python -m pollinate demo --db work.db
python -m unittest discover -s tests -v
```

命令：dispatch, bloom, settle, ingest。每个命令通过 `--fixture` 读取单一子系统的 JSON；`demo` 依次执行正常样例；`ingest --fixture <跨子系统 JSON>` 一次载入混合证据（例如联合喷药事故）并重算全部决定。每项报告列证据 ID 和决定版本。

```powershell
# 合成联合喷药事故：Z1 的 A 与 Z2 的 B 同夜作业，H1 在 t=2 死亡
python -m pollinate init --db joint.db
python -m pollinate ingest --db joint.db --fixture fixtures/joint-spray.json
python -m pollinate report --db joint.db --subject joint-1
python -m pollinate replay --db joint.db --subject joint-1
```


## 证据和状态语义

`event_time` 是证据所述事实发生时间，`received_at` 是本机收到时间。健康记录的 `valid_from/valid_to`、花期的 `window`、派驻的 `start/end` 与喷药的 `start/end/delivered_at` 共用整数时间轴；区间右端不包含在内。每个分区把替补蜂箱区间合并，避免同一额度重叠计费。喷药通知只有送达后的时段进入禁入区间。

结算按有效分区蜂箱小时与合同最低量比较。争议事件按 `dispute_id` 冻结或释放款项；释放时有效结算额须覆盖冻结额。新证据生成新版本，不删除旧结论。`python -m pollinate replay --db work.db --subject case-1` 从原始证据独立重算十项决定并与持久化的最新版本核对，返回 `match` 与差异。


## 联合喷药：批次、剂量与必要原因

`dispatch` 的“蜂群损失事故归因”和 `settle` 的“事故赔付责任裁定”读取同一份证据、调用同一个纯函数 `pollinate.joint.snapshot`，因此两个入口的责任输出不可能不一致。未声明任何有效剂量的旧输入仍按送达时段得到原来的“果园方 / 待调查 / 无事故”标量；正常样例无死亡，保持“无事故”。

一旦 `spray_notice` 声明了在蜂箱处的有效剂量（`dose` 或 `dose_range`），结果变成结构化的因果责任图：

| 字段 | 含义 |
| --- | --- |
| `batch_ids` / `batches` | 每个药剂批次的主体、分区、有效剂量、去除该批后的反事实剂量、采样/校准/漂移凭证 |
| `causes` | 构成原因（必要或充分）的批次；`必要` = 总量去掉该批后低于阈值 |
| `combined` | 唯一的合计暴露边，`counted_once=true`，合计剂量不会复制到各批次边上 |
| `edges` | 批次→死亡事件边（携带剂量与 `counterfactual_dose`）+ 一条合计边 |
| `threshold` / `threshold_source` | 致死阈值及来源凭证 |
| `shares` / `recovery` | 按实际贡献剂量计算的内部追偿份额与金额 |
| `claim` | 对受害方开出的请求：见下 |
| `freeze_keys` / `funds` | 冻结键与资金流水（冻结、付款、驳回） |

### 两种签认协议

合同附件里的两种方式都面对同一剂量与同一必要原因图；“先签认并生效”的版本（`approved_at` 排序，显式 `effective` 优先）被固定，之后上传的喷药包只能形成新版本，不能改变法定份额。

| 协议 | 版本 | 对外请求 | 内部结算 | 冻结键 |
| --- | --- | --- | --- | --- |
| 共同必要原因 | `joint-necessary-v1` | 按主体各一张，金额来自各自份额，总额只开一次 | A=7/13、B=6/13 | `joint-necessary:<死亡事件>:<主体>` |
| 联合且分别负责 | `joint-several-v1` | 一张总额不重复的联带请求，A、B 共同对外 | 仍为 A=7/13、B=6/13 | `joint-several:<死亡事件>` |

### 资金与不确定性

- 只有“合计剂量确定达到阈值 + 至少一条已确认原因 + 协议已签认”三者同时满足才 `payment_allowed=true`；否则冻结/付款一律 `blocked`。
- 剂量区间横跨阈值时，批次只标 `possible`，不得按中心值付款。
- 采样链缺失（无 `sample_id`/`calibration_id`，跨区无 `drift_id`）的批次仍计入观测总量，但不能被指名；此时结果为 `attribution_incomplete`，不会把另一家硬判成唯一责任方。
- 时间不相交的批次标 `time_disjoint`，不进入因果边。
- 冻结键与当前签认协议不匹配时被驳回（`foreign_freeze_key`），不会出现两张全额单或重复计费。
