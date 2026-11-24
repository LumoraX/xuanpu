//! 运行日志：追加写 `~/.xuanpu/logs/desktop.log`，超过上限轮转为 `desktop.log.1`。
//!
//! 只记连接、导航、下载与配置事件及错误原因；不记 Cookie、登录码、页面内容或文件内容。
//! 日志目录不可用时退回标准错误输出，并在第一次失败时说明原因。
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

/// 单个日志文件上限，超过后轮转。
pub const MAX_BYTES: u64 = 5 * 1024 * 1024;

/// 线程安全的追加日志。
pub struct Logger {
    file: Option<PathBuf>,
    lock: Mutex<bool>,
}

impl Logger {
    /// 在给定目录下记日志；目录建不了时只写标准错误输出。
    pub fn new(directory: Option<&Path>) -> Self {
        let file = directory.and_then(|dir| match crate::settings::create_private_dir(dir) {
            Ok(()) => Some(dir.join("desktop.log")),
            Err(error) => {
                eprintln!("无法创建日志目录 {}：{error}", dir.display());
                None
            }
        });
        Logger {
            file,
            lock: Mutex::new(false),
        }
    }

    /// 日志文件路径（可能尚未创建）。
    pub fn path(&self) -> Option<&Path> {
        self.file.as_deref()
    }

    /// 记一条日志；写入失败只在标准错误输出提示一次，不影响客户端运行。
    pub fn log(&self, level: &str, message: &str) {
        let line = format!("{} {level} {}\n", timestamp(now_seconds()), message.replace('\n', " "));
        let Ok(mut warned) = self.lock.lock() else {
            eprint!("{line}");
            return;
        };
        let Some(path) = self.file.as_deref() else {
            eprint!("{line}");
            return;
        };
        if let Err(error) = append(path, &line) {
            if !*warned {
                eprintln!("无法写入日志 {}：{error}", path.display());
                *warned = true;
            }
            eprint!("{line}");
        }
    }

    /// 信息级日志。
    pub fn info(&self, message: &str) {
        self.log("INFO", message);
    }

    /// 错误级日志。
    pub fn error(&self, message: &str) {
        self.log("ERROR", message);
    }
}

/// 轮转后追加一行；新文件权限 0600。
fn append(path: &Path, line: &str) -> std::io::Result<()> {
    if fs::metadata(path).map(|meta| meta.len()).unwrap_or(0) >= MAX_BYTES {
        fs::rename(path, path.with_extension("log.1"))?;
    }
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)?.write_all(line.as_bytes())
}

/// 当前 UNIX 秒数；系统时钟早于 1970 时记为 0。
fn now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or(0)
}

/// UTC 时间戳 `YYYY-MM-DDTHH:MM:SSZ`（按公历换算，不依赖外部时间库）。
pub fn timestamp(seconds: u64) -> String {
    let days = (seconds / 86_400) as i64;
    let rest = seconds % 86_400;
    // 由天数还原公历日期（Howard Hinnant 的 civil_from_days）
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamps_are_utc_civil_dates() {
        assert_eq!(timestamp(0), "1970-01-01T00:00:00Z");
        assert_eq!(timestamp(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(timestamp(1_794_618_061), "2026-11-14T01:01:01Z");
    }

    #[test]
    fn appends_and_rotates() {
        let dir = std::env::temp_dir().join(format!("xuanpu-log-{}", uuid::Uuid::new_v4()));
        let logger = Logger::new(Some(&dir));
        logger.info("第一行");
        logger.error("第二行\n换行被压平");
        let path = logger.path().unwrap().to_path_buf();
        let text = fs::read_to_string(&path).unwrap();
        assert_eq!(text.lines().count(), 2);
        assert!(text.contains(" ERROR 第二行 换行被压平"));
        fs::write(&path, vec![b'x'; MAX_BYTES as usize]).unwrap();
        logger.info("轮转后");
        assert!(path.with_extension("log.1").exists());
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 1);
        fs::remove_dir_all(dir).unwrap();
    }
}
