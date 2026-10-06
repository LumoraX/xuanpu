//! 原生客户端入口；企业页面与授权页面没有通用原生权限。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod shell;

use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tauri::{Manager, WebviewWindow};
use xuanpu_desktop::connection::Connection;
use xuanpu_desktop::policy::{is_bootstrap, Deployment};

/// 启动时读取的只读部署配置；配置错误在界面说明，不静默回退。
struct DesktopState {
    deployment: Result<Arc<Deployment>, String>,
    connection: Connection,
    error: Mutex<Option<String>>,
}

/// 连接固定企业地址；请求前后都确认调用者仍是本地启动页。
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
    let config = state.deployment.as_ref().map_err(Clone::clone)?.clone();
    let generation = state.connection.begin();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "无法初始化连接，请重新打开玄圃。")?;
    let health = config
        .service_url
        .join("/healthz")
        .map_err(|_| "企业服务地址无效。")?;
    let response = client
        .get(health)
        .send()
        .await
        .map_err(|_| "请确认玄圃后台服务已启动且网络正常，然后重新连接。")?;
    if !response.status().is_success() {
        return Err("玄圃后台暂未就绪，请稍后重新连接。".into());
    }
    verify_bootstrap(&window)?;
    schedule_navigation(window, config, generation)?;
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
                match state.error.lock() {
                    Ok(mut error) => {
                        *error = Some("工作站页面加载超时，请确认网络正常后重新连接。".into())
                    }
                    Err(_) => eprintln!("无法保存连接超时状态"),
                }
                if target
                    .navigate("tauri://localhost/index.html".parse().expect("固定地址"))
                    .is_err()
                {
                    eprintln!("无法返回连接页面");
                }
            });
        });
        if result.is_err() {
            eprintln!("无法提交连接超时检查");
        }
    });
}

/// 远程页面及其他窗口不能触发原生连接动作。
fn verify_bootstrap(window: &WebviewWindow) -> Result<(), String> {
    if window.label() == "main" && window.url().map(|url| is_bootstrap(&url)).unwrap_or(false) {
        Ok(())
    } else {
        Err("此页面无权发起客户端连接。".into())
    }
}

/// 从签名应用包读取管理员配置，不读取用户密钥或浏览器数据。
fn deployment(app: &tauri::App) -> Result<Arc<Deployment>, String> {
    let directory = app
        .path()
        .resource_dir()
        .map_err(|_| "无法读取客户端资源。")?;
    let data = std::fs::read(directory.join("desktop.json"))
        .map_err(|_| "缺少企业连接信息，请联系管理员重新安装客户端。")?;
    let config: Deployment = serde_json::from_slice(&data)
        .map_err(|_| "企业连接信息格式不正确，请联系管理员更新客户端。")?;
    config.validate()?;
    Ok(Arc::new(config))
}

/// 初始化单窗口、标准菜单与保留会话的系统 WebView。
fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![connect_service])
        .setup(|app| {
            app.manage(DesktopState {
                deployment: deployment(app),
                connection: Connection::default(),
                error: Mutex::new(None),
            });
            shell::create_window(app.handle())?;
            shell::install_menu(app)?;
            Ok(())
        })
        .on_menu_event(shell::menu_event)
        .build(tauri::generate_context!())
        .expect("无法启动玄圃桌面客户端")
        .run(|app, event| {
            if let tauri::RunEvent::Reopen { .. } = event {
                if let Some(window) = app.get_webview_window("main") {
                    if let Err(error) = window.show().and_then(|_| window.set_focus()) {
                        eprintln!("无法显示玄圃窗口：{error}");
                    }
                } else if let Err(error) = shell::create_window(app) {
                    eprintln!("无法重新打开玄圃窗口：{error}");
                }
            }
        });
}
