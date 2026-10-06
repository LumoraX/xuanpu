//! 管理员部署配置、来源边界和下载文件名。
use serde::Deserialize;
use std::path::PathBuf;
use url::Url;

/// 发布时写入的非敏感连接信息。
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Deployment {
    pub service_url: Url,
    pub auth_origins: Vec<Url>,
}

/// 链接在客户端中的处置方式。
#[derive(Debug, PartialEq)]
pub enum Navigation {
    Internal,
    External,
    Deny,
}

impl Deployment {
    /// 校验部署配置，禁止不安全的远程明文服务。
    pub fn validate(&self) -> Result<(), String> {
        if !safe_endpoint(&self.service_url)
            || self.service_url.query().is_some()
            || self.service_url.fragment().is_some()
        {
            return Err("企业服务地址无效，请联系管理员更新客户端。".into());
        }
        if self.auth_origins.iter().any(|url| {
            !safe_endpoint(url)
                || url.path() != "/"
                || url.query().is_some()
                || url.fragment().is_some()
        }) {
            return Err("登录服务地址无效，请联系管理员更新客户端。".into());
        }
        Ok(())
    }
    /// 将链接归类为业务页面、外部浏览器或禁止访问。
    pub fn navigation(&self, url: &Url) -> Navigation {
        if url.scheme() == "blob" {
            return if url.origin() == self.service_url.origin() {
                Navigation::Internal
            } else {
                Navigation::Deny
            };
        }
        if !safe_endpoint(url) {
            return Navigation::Deny;
        }
        if url.origin() == self.service_url.origin()
            || self
                .auth_origins
                .iter()
                .any(|origin| origin.origin() == url.origin())
        {
            Navigation::Internal
        } else if url.scheme() == "https" {
            Navigation::External
        } else {
            Navigation::Deny
        }
    }
}

/// 生成安全的独立下载路径，避免服务端文件名逃逸和覆盖旧文件。
pub fn download_path(directory: &std::path::Path, suggested: &std::path::Path) -> PathBuf {
    let stem: String = suggested
        .file_stem()
        .and_then(|name| name.to_str())
        .unwrap_or("文件")
        .chars()
        .filter(|c| !c.is_control() && !"/\\:<>|?*".contains(*c))
        .take(60)
        .collect();
    let stem = if stem.trim_matches('.').is_empty() {
        "文件"
    } else {
        &stem
    };
    let extension: String = suggested
        .extension()
        .and_then(|name| name.to_str())
        .unwrap_or("")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .take(12)
        .collect();
    let suffix = if extension.is_empty() {
        String::new()
    } else {
        format!(".{extension}")
    };
    directory.join(format!("{stem}-{}{suffix}", uuid::Uuid::new_v4().simple()))
}

/// 仅允许 HTTPS 或本机回环 HTTP，且地址不可携带凭据。
fn safe_endpoint(url: &Url) -> bool {
    let loopback = match url.host() {
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
        Some(url::Host::Domain(host)) => host == "localhost",
        None => false,
    };
    url.username().is_empty()
        && url.password().is_none()
        && url.host().is_some()
        && (url.scheme() == "https" || (url.scheme() == "http" && loopback))
}

/// 本地启动页是唯一能够调用连接命令的页面。
pub fn is_bootstrap(url: &Url) -> bool {
    url.scheme() == "tauri"
        && url.host_str() == Some("localhost")
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && matches!(url.path(), "" | "/" | "/index.html")
        && url.query().is_none()
        && url.fragment().is_none()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn deployment(service: &str) -> Deployment {
        Deployment {
            service_url: service.parse().unwrap(),
            auth_origins: vec!["https://accounts.feishu.cn/".parse().unwrap()],
        }
    }

    #[test]
    fn bootstrap_rejects_lookalikes_and_credentials() {
        assert!(is_bootstrap(
            &"tauri://localhost/index.html".parse().unwrap()
        ));
        assert!(is_bootstrap(&"tauri://localhost".parse().unwrap()));
        for value in [
            "tauri://user@localhost/index.html",
            "tauri://localhost.evil/index.html",
            "https://localhost/index.html",
            "tauri://localhost/index.html?url=evil",
        ] {
            assert!(!is_bootstrap(&value.parse().unwrap()), "{value}");
        }
    }

    #[test]
    fn rejects_insecure_remote_and_credentials() {
        for value in [
            "http://example.com/console/",
            "file:///etc/passwd",
            "https://user:pass@example.com/console/",
            "https://example.com/console/?key=secret",
        ] {
            assert!(deployment(value).validate().is_err(), "{value}");
        }
    }

    #[test]
    fn accepts_loopback_and_https() {
        for value in [
            "http://127.0.0.1:8420/console/",
            "http://[::1]:8420/console/",
            "https://work.example.com/console/",
        ] {
            assert!(deployment(value).validate().is_ok());
        }
    }

    #[test]
    fn rejects_non_origin_auth_allowlist() {
        let mut config = deployment("http://127.0.0.1:8420/console/");
        for origin in [
            "http://accounts.feishu.cn/",
            "https://accounts.feishu.cn/path",
            "https://user:pass@accounts.feishu.cn/",
        ] {
            config.auth_origins = vec![origin.parse().unwrap()];
            assert!(config.validate().is_err(), "{origin}");
        }
    }

    #[test]
    fn confines_navigation_to_exact_origins() {
        let config = deployment("http://127.0.0.1:8420/console/");
        for value in [
            "http://127.0.0.1:8420/console/",
            "http://127.0.0.1:8420/v1/auth/callback?code=abc",
            "https://accounts.feishu.cn/login",
            "blob:http://127.0.0.1:8420/123",
        ] {
            assert_eq!(
                config.navigation(&value.parse().unwrap()),
                Navigation::Internal,
                "{value}"
            );
        }
        for value in [
            "https://docs.feishu.cn/abc",
            "https://accounts.feishu.cn.evil.example/",
        ] {
            assert_eq!(
                config.navigation(&value.parse().unwrap()),
                Navigation::External
            );
        }
        for value in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://evil.example/",
            "blob:https://evil.example/123",
            "https://user:pass@example.com/",
        ] {
            assert_eq!(config.navigation(&value.parse().unwrap()), Navigation::Deny);
        }
    }

    #[test]
    fn downloads_never_escape_or_reuse_paths() {
        let directory = Path::new("/Users/example/Downloads");
        let first = download_path(directory, Path::new("../../report.pdf"));
        let second = download_path(directory, Path::new("../../report.pdf"));
        assert_eq!(first.parent().unwrap(), directory);
        assert_eq!(first.extension().unwrap(), "pdf");
        assert_ne!(first, second);
        assert!(first
            .file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("report-"));
    }
}
