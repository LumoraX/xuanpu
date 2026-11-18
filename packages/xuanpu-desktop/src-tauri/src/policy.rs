//! 生效连接配置、来源边界和下载文件名。
use std::path::PathBuf;
use url::Url;

/// 生效的连接信息：服务地址只来自用户在本机的设置，登录来源可由应用内置。
#[derive(Clone, Debug)]
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
    /// 校验连接配置，禁止不安全的远程明文服务。
    pub fn validate(&self) -> Result<(), String> {
        if !safe_endpoint(&self.service_url)
            || self.service_url.query().is_some()
            || self.service_url.fragment().is_some()
        {
            return Err("服务器地址无效，请在启动页「设置」中重新填写。".into());
        }
        validate_auth_origins(&self.auth_origins)
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

    /// 新窗口（`window.open` / `target=_blank`）的处置：登录来源交给系统浏览器完成授权，
    /// 企业服务同源仍在主窗口打开，其余与普通导航一致。
    pub fn popup(&self, url: &Url) -> Navigation {
        match self.navigation(url) {
            // 同源优先：服务地址恰好也在登录来源中时，仍按业务页面处理
            Navigation::Internal if url.origin() != self.service_url.origin() => {
                Navigation::External
            }
            other => other,
        }
    }

    /// 浏览器接力登录：把企业服务的飞书登录入口改写为带 `client=desktop` 的桌面模式地址。
    ///
    /// 仅当链接与服务同源、路径恰好为登录入口且尚未带 `client=desktop` 时返回改写结果；
    /// 已是桌面模式则返回 `None` 照常放行，避免反复改写。其他查询参数原样保留。
    pub fn desktop_login(&self, url: &Url) -> Option<Url> {
        if !self.is_service_path(url, FEISHU_LOGIN_PATH) {
            return None;
        }
        if url
            .query_pairs()
            .any(|(key, value)| key == "client" && value == "desktop")
        {
            return None;
        }
        // 去掉其他取值的 client 参数，避免服务端看到相互矛盾的客户端类型
        let kept: Vec<(String, String)> = url
            .query_pairs()
            .filter(|(key, _)| key != "client")
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();
        let mut target = url.clone();
        target
            .query_pairs_mut()
            .clear()
            .extend_pairs(kept)
            .append_pair("client", "desktop");
        Some(target)
    }

    /// 是否为浏览器接力登录的换取地址；命中时客户端应把主窗口切到前台。
    pub fn is_login_redeem(&self, url: &Url) -> bool {
        self.is_service_path(url, FEISHU_REDEEM_PATH)
    }

    /// 链接与企业服务同源且路径恰好等于给定路径。
    fn is_service_path(&self, url: &Url, path: &str) -> bool {
        safe_endpoint(url) && url.origin() == self.service_url.origin() && url.path() == path
    }
}

/// 桌面客户端在 WebView 用户代理末尾附加的标记；服务端据此为登录入口走浏览器接力，
/// 即使导航改写没有生效（例如网页用别的方式发起登录）也不会退回网页登录。
pub const USER_AGENT_TOKEN: &str = "XuanpuDesktop";

/// WebView 的用户代理：沿用 macOS WebKit 的默认写法，末尾附加 `XuanpuDesktop/<版本>`。
pub fn user_agent() -> String {
    format!(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) {}/{}",
        USER_AGENT_TOKEN,
        env!("CARGO_PKG_VERSION")
    )
}

/// 企业服务的飞书登录入口路径。
const FEISHU_LOGIN_PATH: &str = "/v1/auth/feishu/login";
/// 浏览器接力登录完成后桌面端换取会话的路径。
const FEISHU_REDEEM_PATH: &str = "/v1/auth/feishu/desktop/redeem";

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

/// 登录来源白名单只能是 HTTPS（或本机回环 HTTP）根地址。
pub fn validate_auth_origins(origins: &[Url]) -> Result<(), String> {
    if origins.iter().any(|url| {
        !safe_endpoint(url) || url.path() != "/" || url.query().is_some() || url.fragment().is_some()
    }) {
        return Err("登录来源配置无效，每项只能是 HTTPS 根地址。".into());
    }
    Ok(())
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
    fn user_agent_carries_desktop_token_and_version() {
        let agent = user_agent();
        assert!(agent.starts_with("Mozilla/5.0 (Macintosh;"));
        assert!(agent.ends_with(&format!("XuanpuDesktop/{}", env!("CARGO_PKG_VERSION"))));
    }

    #[test]
    fn rewrites_login_entry_to_desktop_mode() {
        let config = deployment("https://xuanpu.example.com/console/");
        let rewritten = |value: &str| {
            config
                .desktop_login(&value.parse().unwrap())
                .map(|url| url.to_string())
        };
        assert_eq!(
            rewritten("https://xuanpu.example.com/v1/auth/feishu/login").as_deref(),
            Some("https://xuanpu.example.com/v1/auth/feishu/login?client=desktop")
        );
        // 其他参数原样保留，追加在末尾
        assert_eq!(
            rewritten(
                "https://xuanpu.example.com/v1/auth/feishu/login?tenant=acme&return_to=%2Fconsole%2F"
            )
            .as_deref(),
            Some(
                "https://xuanpu.example.com/v1/auth/feishu/login?tenant=acme&return_to=%2Fconsole%2F&client=desktop"
            )
        );
        // 其他取值的 client 被替换为 desktop，而不是并存
        assert_eq!(
            rewritten("https://xuanpu.example.com/v1/auth/feishu/login?client=web&tenant=acme")
                .as_deref(),
            Some("https://xuanpu.example.com/v1/auth/feishu/login?tenant=acme&client=desktop")
        );
        for value in [
            // 已是桌面模式，放行以免死循环
            "https://xuanpu.example.com/v1/auth/feishu/login?client=desktop",
            "https://xuanpu.example.com/v1/auth/feishu/login?tenant=acme&client=desktop",
            // 不同源
            "https://other.example.com/v1/auth/feishu/login",
            "http://xuanpu.example.com/v1/auth/feishu/login",
            "https://xuanpu.example.com:8443/v1/auth/feishu/login",
            "https://accounts.feishu.cn/v1/auth/feishu/login",
            // 路径相似但不相等
            "https://xuanpu.example.com/v1/auth/feishu/login/",
            "https://xuanpu.example.com/v1/auth/feishu/loginx",
            "https://xuanpu.example.com/v1/auth/feishu/callback?code=abc",
            "https://xuanpu.example.com/console/v1/auth/feishu/login",
        ] {
            assert_eq!(rewritten(value), None, "{value}");
        }
    }

    #[test]
    fn auth_popups_open_in_system_browser() {
        let config = deployment("http://127.0.0.1:8420/console/");
        let popup = |value: &str| config.popup(&value.parse().unwrap());
        assert_eq!(
            popup("https://accounts.feishu.cn/open-apis/authen/v1/authorize?app_id=x"),
            Navigation::External
        );
        assert_eq!(popup("http://127.0.0.1:8420/console/files"), Navigation::Internal);
        assert_eq!(popup("https://docs.feishu.cn/abc"), Navigation::External);
        for value in ["http://evil.example/", "file:///etc/passwd", "javascript:alert(1)"] {
            assert_eq!(popup(value), Navigation::Deny, "{value}");
        }
        // 主窗口内的普通导航对登录来源仍是业务页面
        assert_eq!(
            config.navigation(&"https://accounts.feishu.cn/login".parse().unwrap()),
            Navigation::Internal
        );
        // 服务地址同时出现在登录来源时，同源优先，仍在主窗口打开
        let mut shared = deployment("https://xuanpu.example.com/");
        shared.auth_origins = vec!["https://xuanpu.example.com/".parse().unwrap()];
        assert_eq!(
            shared.popup(&"https://xuanpu.example.com/x".parse().unwrap()),
            Navigation::Internal
        );
    }

    #[test]
    fn detects_login_redeem_exactly() {
        let config = deployment("https://xuanpu.example.com/console/");
        let redeem = |value: &str| config.is_login_redeem(&value.parse().unwrap());
        assert!(redeem("https://xuanpu.example.com/v1/auth/feishu/desktop/redeem"));
        assert!(redeem(
            "https://xuanpu.example.com/v1/auth/feishu/desktop/redeem?ticket=abc"
        ));
        for value in [
            "https://other.example.com/v1/auth/feishu/desktop/redeem",
            "https://xuanpu.example.com:8443/v1/auth/feishu/desktop/redeem",
            "https://xuanpu.example.com/v1/auth/feishu/desktop/redeem/",
            "https://xuanpu.example.com/v1/auth/feishu/desktop/redeemer",
            "https://xuanpu.example.com/v1/auth/feishu/desktop",
            "https://xuanpu.example.com/v1/auth/feishu/login?client=desktop",
        ] {
            assert!(!redeem(value), "{value}");
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
