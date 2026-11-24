-- 交付描述与安装只保存配置事实，不创建任何执行授权或发布。
CREATE TABLE "${schema}"."${prefix}capability_root" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_key TEXT NOT NULL,
 package_name TEXT NOT NULL,entry TEXT NOT NULL,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,capability_key),UNIQUE(tenant_id,package_name,entry),
 FOREIGN KEY(tenant_id) REFERENCES "${schema}"."${prefix}tenant"(tenant_id)
);
CREATE TABLE "${schema}"."${prefix}capability_revision" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,capability_id,revision),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}capability_preview" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,revision INTEGER NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,capability_id,revision) REFERENCES "${schema}"."${prefix}capability_revision"(tenant_id,capability_id,revision)
);
CREATE TABLE "${schema}"."${prefix}capability_config" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,revision INTEGER NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,capability_id,revision) REFERENCES "${schema}"."${prefix}capability_revision"(tenant_id,capability_id,revision)
);
CREATE TABLE "${schema}"."${prefix}capability_installation" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,config_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,capability_id),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id),
 FOREIGN KEY(tenant_id,config_id) REFERENCES "${schema}"."${prefix}capability_config"(tenant_id,id)
);
