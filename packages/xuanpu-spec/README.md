# xuanpu-spec

xuanpu 业务包规范：定义业务方提交给平台的业务包清单——skills、连接器声明、策略、评测用例——及其版本规则与归档格式。

这是业务方与平台之间的对外契约，不依赖平台其他组件，也不依赖 taifeng。

```python
from pathlib import Path
from xuanpu_spec import pack_package, validate_package

report = validate_package(Path("./support-desk"))
if report.ok:
    archive = pack_package(Path("./support-desk"))
else:
    print(report.errors)
```

规范说明见仓库根目录的 `docs/architecture/package-spec.md`。
