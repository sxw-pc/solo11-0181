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

## 育王同源核验（物理个体祖先图）

`queen.import_pair` 与 `kinship.assess` 是固定的临时筛查：只校验 `queen_id/drone_id` 非空、保存 pair 原件 `raw_json`，再对直接父母标签求交集。它不认识跨登记处短号，因此 Q1-D1（M1/F1 对 M2/F2）始终落 `allow`，该结论只代表“直接父母标签不相交”，不代表已证明无亲缘。

正式放行只消费按**物理育种个体**构建的有向祖先图（`pollinate/pedigree.py`、`homology.py`、`breeding.py`）。本地短号不是全局名字，每个身份以“登记机构 + 证书号”命名，实体合并边只能由两类独立证据建立：

- **纸档链（paper_chain）**：原始证书号、转场签收（签章编号必须落在签发机构 `seal_domain` 声明的前缀域内）与两端上代亲子记录齐全。缺页保留为带 `(gap_min, gap_max=null)` 的可达范围边；伪造签章、断链、同登记处冲突重发一律进 `lineage_rejection`，不产生合并。
- **校准遗传面板（genetic）**：高密度标记、覆盖率达到面板阈值且经校准。单一位点相似或覆盖率不足直接拒绝；同体分（≥0.95）建合并边，异体分（≤0.45）出具拆分结论，中间分挂起。阈值来源（`threshold_source`）随面板入库。

图节点为实体，弧由后代指向祖先。自环与祖先环在插入前拒绝，图恒为 DAG。每对育种对象计算共同祖先层级：两侧到同一祖先的世代距离区间同时落在合同禁配线（默认 `ancestor_limit=2`，可在 pair 原件覆盖）内才 `hold`；缺页导致上界无界、或裸短号在两条血系上指向未合并/未拆分的多个实体时 `pending`；完整图无禁配共同祖先、或校准面板证明异体且共享祖先上界低于禁配线时 `allow`。没有补证前维持待核，绝不把旧临时 `allow` 当作已证明无亲缘。

合并是可逆的一类证据（`merge_reversal`）。撤销错误合并不修改单个 pair，而是从原始证据重建整图、重算所有后代边与全部 pair 决定；图版本与决定版本均只增不改，同证据重放得到同版本号。`breeding-replay` 在数据库的临时备份副本上重算，不触碰在线库，并能识别被篡改的放行决定。

```powershell
python -m pollinate import-pair --db work.db --fixture fixtures/breeding_pair_q1d1.json
python -m pollinate lineage --db work.db --fixture fixtures/lineage_baseline.json --subject case-queen
python -m pollinate lineage --db work.db --fixture fixtures/lineage_paper_merge.json --subject case-queen   # 同名同体 -> hold
python -m pollinate breeding-report --db work.db
python -m pollinate breeding-replay --db work.db --subject case-queen
```

纸档烧毁时改用 `lineage_genetic_split.json`（异体证明 -> allow）。`lineage_missing_page.json`（断代 -> pending）、`lineage_forged_transfer.json`（伪造转场 -> 拒绝合并）、`lineage_cycle.json`（近亲环/自环 -> 拒绝入图）、`lineage_duplicate.json`（冲突重复登记 -> 拒绝合并）、`lineage_weak_locus.json`（单一位点 -> 拒绝判同）展示异常如何改变祖先图版本。所有育种原件、签章域、实体合并/拆分依据、基因面板与放行决定都在 SQLite 中可重放。
