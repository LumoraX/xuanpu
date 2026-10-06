//! 窗口、导航、下载及原生菜单；不向远程网页暴露文件系统能力。
use crate::DesktopState;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    webview::{DownloadEvent, NewWindowResponse, PageLoadEvent},
    AppHandle, Manager, WebviewUrl, WebviewWindowBuilder,
};
use xuanpu_desktop::policy::{download_path, is_bootstrap, Navigation};

/// 创建窗口；关闭后从 Dock 再次打开仍使用同一个持久化数据目录。
pub fn create_window(app: &AppHandle) -> tauri::Result<()> {
    let state = app.state::<DesktopState>();
    let config = state.deployment.as_ref().ok().cloned();
    let popup_config = config.clone();
    let popup_app = app.clone();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("玄圃").inner_size(1280.0, 860.0).min_inner_size(800.0, 600.0)
        .disable_drag_drop_handler()
        .on_navigation(move |url| {
            if is_bootstrap(url) { return true; }
            match config.as_ref().map(|value| value.navigation(url)) {
                Some(Navigation::Internal) => true,
                Some(Navigation::External) => { open_external(url); false }
                _ => false,
            }
        })
        .on_new_window(move |url, _| {
            match popup_config.as_ref().map(|value| value.navigation(&url)) {
                Some(Navigation::Internal) => {
                    if let Some(window) = popup_app.get_webview_window("main") {
                        if window.navigate(url).is_err() { eprintln!("无法打开业务页面"); }
                    }
                }
                Some(Navigation::External) => open_external(&url),
                _ => {},
            }
            NewWindowResponse::Deny
        })
        .on_page_load(|window, payload| {
            let state = window.state::<DesktopState>();
            if matches!(payload.event(), PageLoadEvent::Finished)
                && state.deployment.as_ref().is_ok_and(|config| {
                    config.navigation(payload.url()) == Navigation::Internal
                }) {
                state.connection.complete();
            }
        })
        .on_download(|webview, event| {
            match event {
                DownloadEvent::Requested { url, destination } => {
                    let state = webview.state::<DesktopState>();
                    let valid = state.deployment.as_ref().is_ok_and(|config| url.origin() == config.service_url.origin());
                    if !valid { return false; }
                    match webview.path().download_dir() {
                        Ok(directory) if directory.is_dir() => *destination = download_path(&directory, destination),
                        _ => { eprintln!("下载文件夹不可用"); return false; }
                    }
                }
                DownloadEvent::Finished { success, .. } => {
                    let message = if success { "文件已保存到下载文件夹" } else { "下载失败，请重试" };
                    // 只注入固定状态文本，不执行文件名、URL 或服务端返回的脚本。
                    let script = format!("(()=>{{const n=document.createElement('div');n.setAttribute('role','status');n.textContent='{message}';Object.assign(n.style,{{position:'fixed',bottom:'28px',right:'28px',padding:'16px 22px',background:'#183c2a',color:'#fff',borderRadius:'12px',zIndex:'2147483647'}});document.body.append(n);setTimeout(()=>n.remove(),5000)}})()");
                    if webview.eval(&script).is_err() { eprintln!("无法显示下载结果"); }
                }
                _ => {},
            }
            true
        }).build()?;
    Ok(())
}

/// 外部文档交给默认浏览器；协议已由导航策略限制为 HTTPS。
fn open_external(url: &url::Url) {
    if open::that_detached(url.as_str()).is_err() {
        eprintln!("无法打开系统浏览器");
    }
}

/// 提供常用系统编辑动作和明确的重新连接入口。
pub fn install_menu(app: &tauri::App) -> tauri::Result<()> {
    let handle = app.handle();
    let app_menu = Submenu::with_items(
        handle,
        "玄圃",
        true,
        &[
            &PredefinedMenuItem::about(handle, Some("关于玄圃"), None)?,
            &PredefinedMenuItem::separator(handle)?,
            &PredefinedMenuItem::hide(handle, Some("隐藏玄圃"))?,
            &PredefinedMenuItem::quit(handle, Some("退出玄圃"))?,
        ],
    )?;
    let edit = Submenu::with_items(
        handle,
        "编辑",
        true,
        &[
            &PredefinedMenuItem::undo(handle, Some("撤销"))?,
            &PredefinedMenuItem::redo(handle, Some("重做"))?,
            &PredefinedMenuItem::separator(handle)?,
            &PredefinedMenuItem::cut(handle, Some("剪切"))?,
            &PredefinedMenuItem::copy(handle, Some("复制"))?,
            &PredefinedMenuItem::paste(handle, Some("粘贴"))?,
            &PredefinedMenuItem::select_all(handle, Some("全选"))?,
        ],
    )?;
    let workspace = Submenu::with_items(
        handle,
        "工作站",
        true,
        &[
            &MenuItem::with_id(
                handle,
                "reconnect",
                "重新连接",
                true,
                Some("CmdOrCtrl+Home"),
            )?,
            &MenuItem::with_id(handle, "reload", "刷新", true, Some("CmdOrCtrl+R"))?,
            &MenuItem::with_id(handle, "back", "返回上一页", true, Some("CmdOrCtrl+["))?,
            &MenuItem::with_id(
                handle,
                "downloads",
                "打开下载文件夹",
                true,
                Some("CmdOrCtrl+Shift+J"),
            )?,
        ],
    )?;
    app.set_menu(Menu::with_items(handle, &[&app_menu, &edit, &workspace])?)?;
    Ok(())
}

/// 菜单只使用固定目标；不接收远程页面传入的路径或脚本。
pub fn menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let result = match event.id().as_ref() {
        "reconnect" => window.navigate("tauri://localhost/index.html".parse().expect("固定地址")),
        "reload" => window.reload(),
        "back" => window.eval("history.back()"),
        "downloads" => {
            match app.path().download_dir() {
                Ok(directory) => {
                    if open::that_detached(directory).is_err() {
                        eprintln!("无法打开下载文件夹");
                    }
                }
                Err(_) => eprintln!("下载文件夹不可用"),
            }
            Ok(())
        }
        _ => Ok(()),
    };
    if let Err(error) = result {
        eprintln!("菜单操作失败：{error}");
    }
}
