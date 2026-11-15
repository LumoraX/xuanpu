//! 用户级客户端设置：`~/.xuanpu/desktop.json` 覆盖应用内置的企业服务地址。
//!
//! 内置配置随应用包发布；用户文件只放公开的连接信息（服务地址、登录来源），
//! 不放密钥。用户文件格式不对时明确报错，不悄悄退回内置地址。
use crate::policy::Deployment;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use url::Url;

/// 客户端在用户目录下使用的路径。
#[derive(Clone, Debug)]
pub struct Paths {
    pub root: PathBuf,
    pub config: PathBuf,
    pub logs: PathBuf,
}

impl Paths {
    /// 以用户主目录为根：`~/.xuanpu/desktop.json` 与 `~/.xuanpu/logs/`。
    pub fn under(home: &Path) -> Self {
        let root = home.join(".xuanpu");
        Paths {
            config: root.join("desktop.json"),
            logs: root.join("logs"),
            root,
        }
    }
}

/// 用户文件内容；两项都可省略，省略的项使用内置值。
#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UserSettings {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub service_url: Option<Url>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth_origins: Option<Vec<Url>>,
}

/// 生效配置的来源，供启动页说明。
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    User,
    Bundled,
}

/// 读用户文件：不存在返回 None；存在但读不了或格式不对返回错误。
pub fn read_user(path: &Path) -> Result<Option<UserSettings>, String> {
    let data = match fs::read(path) {
        Ok(data) => data,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("无法读取 {}：{error}", path.display())),
    };
    serde_json::from_slice(&data)
        .map(Some)
        .map_err(|error| format!("{} 格式不正确：{error}", path.display()))
}

/// 合并内置与用户配置并校验；内置缺失时必须由用户文件给出服务地址。
pub fn effective(
    bundled: Option<&Deployment>,
    user: Option<&UserSettings>,
) -> Result<(Deployment, Source), String> {
    let service_url = user
        .and_then(|value| value.service_url.clone())
        .or_else(|| bundled.map(|value| value.service_url.clone()))
        .ok_or("尚未配置玄圃服务器地址，请在下方填写。")?;
    let auth_origins = user
        .and_then(|value| value.auth_origins.clone())
        .or_else(|| bundled.map(|value| value.auth_origins.clone()))
        .unwrap_or_default();
    let source = if user.is_some_and(|value| value.service_url.is_some()) {
        Source::User
    } else {
        Source::Bundled
    };
    let config = Deployment {
        service_url,
        auth_origins,
    };
    config.validate()?;
    Ok((config, source))
}

/// 把用户输入整理成工作站地址：补协议与 `/console/`，拒绝查询串与片段。
pub fn normalize_service(input: &str) -> Result<Url, String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err("请填写玄圃服务器地址。".into());
    }
    let with_scheme = if trimmed.contains("://") {
        trimmed.to_string()
    } else {
        format!("https://{trimmed}")
    };
    let mut url: Url = with_scheme
        .parse()
        .map_err(|_| "服务器地址格式不正确。".to_string())?;
    if matches!(url.path(), "" | "/") {
        url.set_path("/console/");
    } else if !url.path().ends_with('/') {
        let path = format!("{}/", url.path());
        url.set_path(&path);
    }
    let probe = Deployment {
        service_url: url.clone(),
        auth_origins: vec![],
    };
    probe
        .validate()
        .map_err(|_| "服务器地址必须是 HTTPS（本机调试可用 http://127.0.0.1），且不能带账号、查询串或 #。".to_string())?;
    Ok(url)
}

/// 写用户文件：目录 0700、文件 0600，先写临时文件再改名，避免写一半。
pub fn write_user(paths: &Paths, settings: &UserSettings) -> std::io::Result<()> {
    create_private_dir(&paths.root)?;
    let data = serde_json::to_vec_pretty(settings).map_err(std::io::Error::other)?;
    let temporary = paths.config.with_extension("json.next");
    {
        let mut file = private_file(&temporary)?;
        file.write_all(&data)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&temporary, &paths.config)
}

/// 删除用户文件，回到内置配置；文件本来不存在不算错误。
pub fn remove_user(paths: &Paths) -> std::io::Result<()> {
    match fs::remove_file(&paths.config) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

/// 建只有本人可访问的目录。
pub fn create_private_dir(path: &Path) -> std::io::Result<()> {
    fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// 以 0600 新建（或截断）文件。
fn private_file(path: &Path) -> std::io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bundled() -> Deployment {
        Deployment {
            service_url: "https://xuanpu.example.com/console/".parse().unwrap(),
            auth_origins: vec!["https://accounts.feishu.cn/".parse().unwrap()],
        }
    }

    fn temp_home() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("xuanpu-settings-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn user_service_overrides_bundled_and_keeps_auth_origins() {
        let user = UserSettings {
            service_url: Some("https://work.example.com/console/".parse().unwrap()),
            auth_origins: None,
        };
        let (config, source) = effective(Some(&bundled()), Some(&user)).unwrap();
        assert_eq!(config.service_url.as_str(), "https://work.example.com/console/");
        assert_eq!(config.auth_origins, bundled().auth_origins);
        assert_eq!(source, Source::User);
        let (config, source) = effective(Some(&bundled()), None).unwrap();
        assert_eq!(config.service_url, bundled().service_url);
        assert_eq!(source, Source::Bundled);
    }

    #[test]
    fn missing_everything_and_insecure_user_value_are_errors() {
        assert!(effective(None, None).is_err());
        let user = UserSettings {
            service_url: Some("http://work.example.com/console/".parse().unwrap()),
            auth_origins: None,
        };
        assert!(effective(Some(&bundled()), Some(&user)).is_err());
    }

    #[test]
    fn normalize_adds_scheme_and_console_path() {
        for (input, expected) in [
            ("xuanpu.example.com", "https://xuanpu.example.com/console/"),
            (" https://xuanpu.example.com ", "https://xuanpu.example.com/console/"),
            ("https://xuanpu.example.com/console", "https://xuanpu.example.com/console/"),
            ("http://127.0.0.1:8420", "http://127.0.0.1:8420/console/"),
        ] {
            assert_eq!(normalize_service(input).unwrap().as_str(), expected, "{input}");
        }
        for input in ["", "http://example.com", "https://a:b@example.com", "https://x.com/?a=1", "ftp://x.com"] {
            assert!(normalize_service(input).is_err(), "{input}");
        }
    }

    #[test]
    fn write_read_remove_round_trip_with_private_modes() {
        let home = temp_home();
        let paths = Paths::under(&home);
        assert_eq!(read_user(&paths.config).unwrap(), None);
        let settings = UserSettings {
            service_url: Some("https://work.example.com/console/".parse().unwrap()),
            auth_origins: None,
        };
        write_user(&paths, &settings).unwrap();
        assert_eq!(read_user(&paths.config).unwrap(), Some(settings));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let file = fs::metadata(&paths.config).unwrap().permissions().mode() & 0o777;
            let dir = fs::metadata(&paths.root).unwrap().permissions().mode() & 0o777;
            assert_eq!((file, dir), (0o600, 0o700));
        }
        remove_user(&paths).unwrap();
        remove_user(&paths).unwrap();
        assert_eq!(read_user(&paths.config).unwrap(), None);
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn malformed_or_unknown_keys_are_reported_not_ignored() {
        let home = temp_home();
        let paths = Paths::under(&home);
        create_private_dir(&paths.root).unwrap();
        fs::write(&paths.config, b"{\"service_url\": 3}").unwrap();
        assert!(read_user(&paths.config).is_err());
        fs::write(&paths.config, b"{\"server\": \"https://x.com/\"}").unwrap();
        assert!(read_user(&paths.config).is_err());
        fs::remove_dir_all(home).unwrap();
    }
}
