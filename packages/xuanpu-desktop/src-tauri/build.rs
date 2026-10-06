//! 生成 Tauri 资源和仅供本地启动页使用的命令权限。
fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["connect_service"])),
    )
    .expect("无法生成玄圃桌面资源");
}
