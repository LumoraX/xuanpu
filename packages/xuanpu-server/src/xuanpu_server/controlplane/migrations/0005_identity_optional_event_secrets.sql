-- 飞书登录与加密事件独立配置：登录只要求 App Secret，事件的两个引用可为空。
-- 保留已有引用与企业绑定，空引用时应用层拒绝事件；不允许回退到明文事件。
ALTER TABLE "${schema}"."${prefix}identity_config"
    ALTER COLUMN encrypt_key_ref DROP NOT NULL,
    ALTER COLUMN verification_token_ref DROP NOT NULL;
