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

