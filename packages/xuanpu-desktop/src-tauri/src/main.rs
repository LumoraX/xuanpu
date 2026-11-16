//! 原生客户端入口；企业页面与授权页面没有通用原生权限。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod shell;

use serde::Serialize;
use std::{
    sync::{Arc, Mutex, RwLock},
    time::Duration,
};
use tauri::{Manager, WebviewWindow};
use xuanpu_desktop::applog::Logger;
use xuanpu_desktop::connection::Connection;
use xuanpu_desktop::policy::{is_bootstrap, Deployment};
use xuanpu_desktop::settings::{self, Bundled, Paths, UserSettings};

/// 客户端状态：内置登录来源、用户设置合并后的生效配置、日志与连接代次。
pub struct DesktopState {
    bundled: Result<Bundled, String>,
    deployment: RwLock<Result<Arc<Deployment>, String>>,
    paths: Option<Paths>,
    pub log: Logger,
    connection: Connection,
    error: Mutex<Option<String>>,
    open_settings: Mutex<bool>,
}

impl DesktopState {
    /// 当前生效的部署配置。
    pub fn current(&self) -> Result<Arc<Deployment>, String> {
        match self.deployment.read() {
            Ok(guard) => guard.clone(),
            Err(_) => Err("客户端配置状态不可用，请重启玄圃。".into()),
        }
    }

    /// 从 `~/.xuanpu/desktop.json` 与内置登录来源重新计算生效配置并记日志。
    fn reload(&self) {
        let user = self
            .paths
            .as_ref()
            .map(|paths| settings::read_user(&paths.config))
            .transpose()
            .map(Option::flatten);
        let result = user.and_then(|user| {
            settings::effective(self.bundled.as_ref().ok(), user.as_ref()).map(Arc::new)
        });
        match &result {
            Ok(config) => self.log.info(&format!("生效服务地址 {}", config.service_url)),
            Err(error) => self.log.error(&format!("配置不可用：{error}")),
        }
        match self.deployment.write() {
            Ok(mut guard) => *guard = result,
            Err(_) => self.log.error("配置状态锁已损坏"),
        }
    }
}

/// 启动页显示的设置摘要；只含公开信息。`needs_setup` 表示首次使用、尚未填写服务器。
#[derive(Serialize)]
struct SettingsView {
    service_url: Option<String>,
    needs_setup: bool,
    error: Option<String>,
    config_path: Option<String>,
    log_path: Option<String>,
    open_settings: bool,
}

/// 连接当前服务地址；请求前后都确认调用者仍是本地启动页。
#[tauri::command]
async fn connect_service(
    window: WebviewWindow,
    state: tauri::State<'_, DesktopState>,
) -> Result<(), String> {
    verify_bootstrap(&window)?;
    if let Some(error) = state
        .error
        .lock()
        .map_err(|_| "连接状态不可用，请重启客户端。")?
        .take()
    {
        return Err(error);
    }
    let config = state.current()?;
    let generation = state.connection.begin();
    let health = config
        .service_url
        .join("/healthz")
        .map_err(|_| "企业服务地址无效。")?;
    state.log.info(&format!("检查服务 {health}"));
    check_health(&state.log, health).await?;
    verify_bootstrap(&window)?;
    schedule_navigation(window, config, generation)?;
    Ok(())
}

/// 健康检查；失败时把完整原因写日志，界面给分类提示。
async fn check_health(log: &Logger, health: url::Url) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(8))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| {
            log.error(&format!("无法初始化 HTTP 客户端：{error}"));
            "无法初始化连接，请重新打开玄圃。".to_string()
        })?;
    let response = client.get(health.clone()).send().await.map_err(|error| {
        log.error(&format!("连接 {health} 失败：{}", error_chain(&error)));
        let kind = if error.is_timeout() {
            "连接超时"
        } else if error.is_connect() {
            "无法建立连接（域名解析、网络或证书问题）"
        } else {
            "请求失败"
        };
        format!("{kind}：{}。请确认服务器地址与网络，详情见日志。", health.origin().ascii_serialization())
    })?;
    if !response.status().is_success() {
        log.error(&format!("{health} 返回 {}", response.status()));
        return Err(format!("玄圃后台暂未就绪（HTTP {}），请稍后重新连接。", response.status().as_u16()));
    }
    log.info(&format!("{health} 返回 {}", response.status()));
    Ok(())
}

/// 展开错误链，供日志定位（不含请求体或 Cookie）。
fn error_chain(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(" ← ");
        text.push_str(&cause.to_string());
        source = cause.source();
    }
    text
}

/// 启动页读取当前设置与日志位置。
#[tauri::command]
fn get_settings(window: WebviewWindow, state: tauri::State<'_, DesktopState>) -> Result<SettingsView, String> {
    verify_bootstrap(&window)?;
    let open_settings = state.open_settings.lock().map(|mut flag| std::mem::take(&mut *flag)).unwrap_or(false);
    let (service_url, error) = match state.current() {
        Ok(config) => (Some(config.service_url.to_string()), None),
        Err(error) => (None, Some(error)),
    };
    // 用户文件读不了或格式不对时不算首次使用，按错误展示
    let needs_setup = match state.paths.as_ref().map(|paths| settings::read_user(&paths.config)) {
        Some(Ok(user)) => settings::needs_setup(user.as_ref()),
        _ => false,
    };
    Ok(SettingsView {
        service_url,
        needs_setup,
        error,
        config_path: state.paths.as_ref().map(|paths| paths.config.display().to_string()),
        log_path: state.log.path().map(|path| path.display().to_string()),
        open_settings,
    })
}

/// 保存服务器地址到 `~/.xuanpu/desktop.json`；校验通过才写入并生效。
#[tauri::command]
fn save_settings(
    window: WebviewWindow,
    state: tauri::State<'_, DesktopState>,
    service_url: String,
) -> Result<String, String> {
    verify_bootstrap(&window)?;
    let paths = state.paths.as_ref().ok_or("无法定位用户目录，不能保存设置。")?;
    let url = settings::normalize_service(&service_url)?;
    let previous = settings::read_user(&paths.config).ok().flatten().unwrap_or_default();
    let next = UserSettings { service_url: Some(url.clone()), ..previous };
    settings::effective(state.bundled.as_ref().ok(), Some(&next))?;
    settings::write_user(paths, &next).map_err(|error| {
        state.log.error(&format!("写入 {} 失败：{error}", paths.config.display()));
        format!("无法保存设置：{error}")
    })?;
    state.log.info(&format!("用户把服务地址设为 {url}"));
    state.reload();
    Ok(url.to_string())
}

/// 删除用户设置，清除已保存的服务器地址；之后启动页要求重新填写。
#[tauri::command]
fn reset_settings(window: WebviewWindow, state: tauri::State<'_, DesktopState>) -> Result<(), String> {
    verify_bootstrap(&window)?;
    let paths = state.paths.as_ref().ok_or("无法定位用户目录。")?;
    settings::remove_user(paths).map_err(|error| format!("无法删除设置文件：{error}"))?;
    state.log.info("用户清除了服务器地址");
    state.reload();
    Ok(())
}

/// 在访达中打开日志目录。
#[tauri::command]
fn open_logs(window: WebviewWindow, state: tauri::State<'_, DesktopState>) -> Result<(), String> {
    verify_bootstrap(&window)?;
    shell::open_log_folder(&state);
    Ok(())
}

/// 在主线程校验代次并提交导航，旧健康检查结果不得改变新窗口。
fn schedule_navigation(
    window: WebviewWindow,
    config: Arc<Deployment>,
    generation: u64,
) -> Result<(), String> {
    let target = window.clone();
    window
        .run_on_main_thread(move || {
            let state = target.state::<DesktopState>();
            if verify_bootstrap(&target).is_err() {
                return;
            }
            let submitted = state
                .connection
                .navigate(generation, || target.navigate(config.service_url.clone()));
            if submitted.is_some() {
                schedule_timeout(target.clone(), generation);
            }
        })
        .map_err(|_| "无法打开工作站，请重试。".into())
}

/// 延时任务只负责排队，真正恢复时在主线程原子校验与提交。
fn schedule_timeout(window: WebviewWindow, generation: u64) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(12)).await;
        let target = window.clone();
        let result = window.run_on_main_thread(move || {
            let state = target.state::<DesktopState>();
            state.connection.timeout(generation, || {
                state.log.error("工作站页面 12 秒内未加载完成");
                match state.error.lock() {
                    Ok(mut error) => {
                        *error = Some("工作站页面加载超时，请确认网络正常后重新连接。".into())
                    }
                    Err(_) => state.log.error("无法保存连接超时状态"),
                }
                if target
                    .navigate("tauri://localhost/index.html".parse().expect("固定地址"))
                    .is_err()
                {
                    state.log.error("无法返回连接页面");
                }
            });
        });
        if result.is_err() {
            eprintln!("无法提交连接超时检查");
        }
    });
}

/// 远程页面及其他窗口不能触发原生动作。
fn verify_bootstrap(window: &WebviewWindow) -> Result<(), String> {
    if window.label() == "main" && window.url().map(|url| is_bootstrap(&url)).unwrap_or(false) {
        Ok(())
    } else {
        Err("此页面无权发起客户端操作。".into())
    }
}

/// 从签名应用包读取内置的登录来源白名单；应用包里不含服务器地址。
fn bundled(app: &tauri::App) -> Result<Bundled, String> {
    let directory = app
        .path()
        .resource_dir()
        .map_err(|_| "无法读取客户端资源。")?;
    let data = std::fs::read(directory.join("desktop.json"))
        .map_err(|_| "应用内缺少登录来源配置。")?;
    settings::parse_bundled(&data)
}

/// 组装状态：内置配置、用户目录、日志，再计算生效配置。
fn build_state(app: &tauri::App) -> DesktopState {
    let paths = app.path().home_dir().ok().map(|home| Paths::under(&home));
    // 先把 ~/.xuanpu 本身建成只有本人可访问，再在其下建日志目录
    let root_error = paths.as_ref().and_then(|paths| settings::create_private_dir(&paths.root).err());
    let log = Logger::new(paths.as_ref().map(|paths| paths.logs.as_path()));
    if let Some(error) = root_error {
        log.error(&format!("无法创建 ~/.xuanpu：{error}"));
    }
    log.info(&format!("玄圃桌面客户端 {} 启动", app.package_info().version));
    let bundled = bundled(app);
    if let Err(error) = &bundled {
        log.error(&format!("内置配置不可用：{error}"));
    }
    let state = DesktopState {
        bundled,
        deployment: RwLock::new(Err("尚未加载配置".into())),
        paths,
        log,
        connection: Connection::default(),
        error: Mutex::new(None),
        open_settings: Mutex::new(false),
    };
    state.reload();
    state
}

/// 菜单「服务器设置」：回到启动页并展开设置，不自动连接。
pub fn request_settings(state: &DesktopState) {
    match state.open_settings.lock() {
        Ok(mut flag) => *flag = true,
        Err(_) => state.log.error("无法记录打开设置的请求"),
    }
}

/// 初始化单窗口、标准菜单与保留会话的系统 WebView。
fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            connect_service,
            get_settings,
            save_settings,
            reset_settings,
            open_logs
        ])
        .setup(|app| {
            app.manage(build_state(app));
            shell::create_window(app.handle())?;
            shell::install_menu(app)?;
            Ok(())
        })
        .on_menu_event(shell::menu_event)
        .build(tauri::generate_context!())
        .expect("无法启动玄圃桌面客户端")
        .run(|app, event| {
            if let tauri::RunEvent::Reopen { .. } = event {
                let state = app.state::<DesktopState>();
                if let Some(window) = app.get_webview_window("main") {
                    if let Err(error) = window.show().and_then(|_| window.set_focus()) {
                        state.log.error(&format!("无法显示玄圃窗口：{error}"));
                    }
                } else if let Err(error) = shell::create_window(app) {
                    state.log.error(&format!("无法重新打开玄圃窗口：{error}"));
                }
            }
        });
}
