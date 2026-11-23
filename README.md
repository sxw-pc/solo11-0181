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

## 排班发布边界（workers.csv / orchard_jobs.csv）

合作社司机与果园门岗必须按同一版排班作业。直接覆盖两张固定文件名会在崩溃时留下“司机 v2、门岗 v1、库 v1”的裂脑状态，因此发布走三段式协议：

- **稳定身份**：`(publish_dir, 单调 version)`，内容身份为规范排班 JSON 的 SHA-256。版本号只增不复用，持久化在三处：SQLite `roster_publish`、只追加磁盘日志 `<dir>/.publish/journal/vN.json`、不可变载荷 `<dir>/.publish/versions/vN/`（含 `manifest.json` 与两张 CSV，原子 tmp+rename 写入）。
- **事务内 staging / 提交后切换**：journal 先落盘，再写版本载荷和 SQLite `staged` 行；外层事务提交后才执行激活。任何提交前故障都回滚业务库，而磁盘 journal 保证版本号不被复用、重启可重建。
- **路线一 `pointer`（新一代门岗/司机）**：`current.json` 指针在提交后原子翻转，读者每次只解析到完整的 v1 或 v2；故障期间两端继续执行上一完整版本，恢复成功才一起切到新版。
- **路线二 `fixed`（老门岗固定文件名）**：发布前先挂 `STOP_WORK` 停工令，live CSV 从版本目录原子替换，只有两张 live 文件、manifest 与司机/门岗双端回执全部一致，`try_resume` 才撤令复工；故障点两端读到的是明确“排班暂停”，而不是半份清单。
- **现场回执**：`roster_receipt` 与 O_EXCL 磁盘回执只追加、不改写。已观察过的最高版次构成单调下限——任何把旧版重新呈现为当前版的尝试都抛 `VersionRollbackError`，v2 被现场看过之后不可能偷偷回填 v1。
- **恢复**：`recover_publish(db, publish_dir)` 对账 journal、补全缺失载荷、重建回滚的发布行、重指指针或重推 live 文件并对账磁盘回执；重放同一 `field_day` fixture 幂等去重，不复制现场任务、不抹签认历史。

fixture 可用 `fault` 字段（或旧的 `fail_after_workers`）注入 `after_workers`、`after_jobs`、`before_commit`、`one_receipt` 四类合成故障；`revoked_tasks` 表达迟到撤销，撤销以更高版本发布且保留全部历史版本。

