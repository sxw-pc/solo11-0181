# 蜂群授粉履约采证台

蜂箱跨果园进出场、花期窗口、喷药禁入与授粉覆盖决定服务是否可结算；离线扫描卡、田间观察表和合同附件可以乱序到达。

三套子系统通过原始证据、SQLite 状态与版本化决定互相影响。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m pollinate init --db work.db
python -m pollinate demo --db work.db
python -m unittest discover -s tests -v
```

命令：dispatch, bloom, settle, ingest。每个子系统命令通过 `--fixture` 读取单一子系统的 JSON；`demo` 依次执行正常样例；`ingest --fixture <跨子系统 JSON>` 可一次载入混合证据（例如争议账本场景）并重算全部决定。每项报告列证据 ID 和决定版本。

```powershell
# 合成争议账本场景：20 元合同、D1/D2 各申请 12，再补入迟到喷药
python -m pollinate init --db ledger.db
python -m pollinate ingest --db ledger.db --fixture fixtures/ledger_base.json
python -m pollinate ingest --db ledger.db --fixture fixtures/ledger_late_spray.json
python -m pollinate report --db ledger.db --subject ledger-1
python -m pollinate replay --db ledger.db --subject ledger-1
```


## 证据和状态语义

`event_time` 是证据所述事实发生时间，`received_at` 是本机收到时间。健康记录的 `valid_from/valid_to`、花期的 `window`、派驻的 `start/end` 与喷药的 `start/end/delivered_at` 共用整数时间轴；区间右端不包含在内。每个分区把替补蜂箱区间合并，避免同一额度重叠计费。喷药通知只有送达后的时段进入禁入区间。

结算按有效分区蜂箱小时与合同最低量比较。争议事件按 `dispute_id` 冻结或释放款项；释放时有效结算额须覆盖冻结额。新证据生成新版本，不删除旧结论。`python -m pollinate replay --db work.db --subject case-1` 从原始证据独立重算十项决定并与持久化的最新版本核对，返回 `match` 与差异。

## 争议→保留份额账本

`settle` 的“争议款冻结与释放”不再只回报一个冻结总额，而是一张**带版本的逐争议份额表**。金额内部以分为单位计算，报告同时给出整数分与 `*_label` 元字符串。

同一笔应付额被多个争议同时保留时，按一份**签认的分配协议**切分，协议版本不会因后到事件被重新解释：

| 协议 | `protocol_version` | 规则 |
| --- | --- | --- |
| 先受理优先 | `precedence-v1` | 按 `(approved_at, 事件 ID)` 排序，逐笔取 `min(申请额, 剩余)` |
| 平等分摊 | `equal-v1` | 按批准金额比例、以分为单位最大余数法分配；余分按协议顺序固定回放 |

契约示例（应付 20 元，D1、D2 各申请 12 元）：

| 阶段 | precedence-v1 | equal-v1 |
| --- | --- | --- |
| 初始保留 | D1 12，D2 8 | D1 10，D2 10 |
| D1 释放后 D2 保留 / 可付 | 8 / 12 | 10 / 10 |

逐争议字段：

- `claim` 签认的申请额；`entitlement` 若按合同面额计应得份额；
- `funded`（别名 `share`）按**当前** `fee` 重算的份额；`secured` 实际仍被保留的金额（释放/撤回后为 0）；
- `revoked` 因覆盖不足而失去资金的份额，转入待追偿，绝不当作可付资产；
- `status`：`reserved` / `released` / `withdrawn` / `revoked` / `partially_funded` / `unfunded`。

恒等式在每次计算后断言成立：

```
reserved_total + payable == fee
reserved_total + paid_total + available_payable - overpaid_total == fee
0 <= secured <= funded <= claim
```

关键语义：

- **释放只释放该笔已占份额**，不会重算或重分其他争议的份额；重复释放、冻结前释放都是记录在案的 no-op。
- **释放门控**：释放到达时该争议若并不持有份额（例如喷药已使 `fee=0`），释放记为受阻、冻结保留；覆盖恢复后再来的释放才生效。
- **迟到喷药**：`received_at` 可以晚于 `delivered_at/start/end`。`placement_hours` 按 `max(start, delivered_at)` 剪除禁入时段；`bloom → settle` 使 `fee` 从 20 变 0，原保留转入 `revoked`/`recoverable_total`。
- **流水留痕**：已结算的付款不会被迟到证据抹掉；超出当前 `payable` 的部分记入 `overpaid_total` 待追偿。旧账本版本与旧付款行原样保留。

SQLite 中：

- `decision` 保存每项决定的版本化 JSON 结果；
- `dispute_share` 每个 `(subject, surface, version, dispute_id)` 一行，永不就地更新；
- `payment` 记录每笔付款（接受/拒绝）及其到达时可见的 `payable_then`；
- `store.ledger_history(db, subject)` 按版本回放逐争议份额与付款，证明新证据只追加版本、不覆盖历史凭证。
