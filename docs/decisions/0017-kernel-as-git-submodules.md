# ADR 0017：内核与适配包以 git submodule 纳入 —— taifeng、taifeng-sandbox、taifeng-stores 放在 external/

- 状态：Accepted
- 日期：2026-11-15
- 关联：ADR 0001（taifeng 是内核、xuanpu 是发行版；只依赖稳定层）

## 背景

此前内核 `taifeng` 从 PyPI 安装，`taifeng-sandbox`、`taifeng-stores` 尚未发布，开发期要求三个仓库检出在同一个父目录下，`tool.uv.sources` 用 `../taifeng-sandbox` 这样的相对路径取源码。问题：

- 平台需要内核补接口时（缺口清单），改内核、改平台要在两三个互不关联的检出之间来回，验证一次组合要先发布内核到 PyPI；
- 工作区不是自包含的：git worktree、CI 都得另外检出同级仓库，CI 里还要在两个 job 里各写一遍 commit；
- 平台验证过的到底是哪一组内核与适配包版本，只记在 CI 配置的 `ref:` 里，不随代码走。

## 决策

1. 三个仓库以 git submodule 放在 `external/taifeng`、`external/taifeng-sandbox`、`external/taifeng-stores`。子模块指针就是本仓库验证过的组合，随平台提交一起变更。
2. `tool.uv.sources` 把三者都指向子模块（editable）。它们**不**列入 uv workspace members：各自保留自己的 pyproject、锁文件、门禁与发布节奏，可以进入子模块目录独立开发、提交、推送。
3. 发布产物不变：`xuanpu-server` 继续用版本下限声明依赖；镜像构建从子模块打 wheel。
4. 红线不变：仍只 import `taifeng.__all__`；内核缺接口回 taifeng 仓库立项提交，不在子模块里留未推送的私改。子模块指针只能指向上游仓库里已推送的 commit。

## 后果

- 新检出需要 `git clone --recurse-submodules`，或检出后 `git submodule update --init`；CI 的 checkout 带 `submodules: true`。
- 升级内核或适配包 = 在子模块里切到目标 commit，`uv lock`，跑全量门禁，连同子模块指针一起提交。
- `taifeng-sandbox` 当前经验证的提交在上游分支 `ci/xuanpu-runtime-20261029` 上，尚未合入其 main；`.gitmodules` 为它记了该分支，合入 main 后改回默认。
- 不再从 PyPI 取 taifeng，锁文件里三者都是 editable 源；发布到 PyPI 后若要回到版本约束，写新 ADR 取代本条。
