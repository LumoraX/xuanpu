# 配置组织与职责

当前版本已在acme试点灰度部署；真实管理员目录导入预览尚未验收。部门和目录导入均不作为直接到人协作的前置条件。

直接指定员工协作不要求部门或岗位。员工选择目录只提供本企业启用员工的姓名及稳定编号，企微可见范围不自动提供业务职责。

员工在「功能 → 组织与职责 → 查看组织与职责」查看本人的部门、岗位和职责。页面不依赖模型。获授权的组织管理者可配置部门、岗位、任职和部门业务职责；同名人员须按员工编号核对。任职不自动提供管理员或审批权限。

所有配置先显示确认卡。网络失败重试保留原操作号，版本冲突则刷新后重新核对，不自动覆盖新记录。关闭organization新写后保留读取、撤销和停用。

目录导入流程：

1. 由租户管理员在「企业连接」本人确认通讯录只读授权。
2. 在「组织与职责」配置字段来源，预览影响数量后确认。
3. 选择已核定安装，采集并预览应用可见目录。
4. 核对分页错误、字段冲突、身份映射，明确选中本批变更。
5. 确认后系统重新核对来源摘要和本地版本，再提交本批。

未选中差异不会应用。未出现成员不会被停用。未知企微账号先明确关联既有玄圃员工，不按姓名猜测。R3-A安装若只核定本人资料，还须独立核定目录能力。

停用使旧任职和职责立即失效；重新启用后需重新明确授予。历史记录保留，不自动改派未决工作的责任人。

SDK提供organization_me、organization_directory、list_organization、get_organization、create_organization、change_organization、organization_authority、preview_organization_authority、apply_organization_authority、preview_organization_import、get_organization_import、apply_organization_import。

CLI入口为`organization --credentials <权限600的私有JSON档案>`，支持me、directory、list、get、create、change、authority、preview-authority、apply-authority、preview-import、get-import、apply-import。写操作从`--body`文件读取固定操作号、版本和明确选中批次；身份只来自凭据档案。
