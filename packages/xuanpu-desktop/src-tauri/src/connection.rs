//! 连接转换共用锁，防止旧请求和超时动作覆盖新页面。
use std::sync::Mutex;

/// 一次连接的当前代次、已提交导航与终态。
#[derive(Default)]
struct Phase {
    generation: u64,
    loading: Option<u64>,
    settled: bool,
}

/// 由原生导航回调与主线程恢复任务共享的状态。
#[derive(Default)]
pub struct Connection(Mutex<Phase>);

impl Connection {
    /// 开始新连接，作废尚未完成的旧请求。
    pub fn begin(&self) -> u64 {
        let mut phase = self.0.lock().expect("连接状态锁已损坏");
        phase.generation += 1;
        phase.settled = false;
        phase.generation
    }

    /// 校验代次并提交导航；状态与动作在同一个临界区内。
    pub fn navigate<T>(&self, generation: u64, action: impl FnOnce() -> T) -> Option<T> {
        let mut phase = self.0.lock().expect("连接状态锁已损坏");
        if phase.generation != generation || phase.settled {
            return None;
        }
        phase.loading = Some(generation);
        Some(action())
    }

    /// 只完成已为当前代次提交的页面加载，旧页面不能完成新健康检查。
    pub fn complete(&self) {
        let mut phase = self.0.lock().expect("连接状态锁已损坏");
        if phase.loading == Some(phase.generation) {
            phase.settled = true;
        }
    }

    /// 原子认领超时并提交恢复；与成功、新请求互斥。
    pub fn timeout<T>(&self, generation: u64, action: impl FnOnce() -> T) -> Option<T> {
        let mut phase = self.0.lock().expect("连接状态锁已损坏");
        if phase.generation != generation || phase.settled {
            return None;
        }
        phase.settled = true;
        Some(action())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stale_health_result_cannot_navigate_or_complete_new_attempt() {
        let connection = Connection::default();
        let first = connection.begin();
        let second = connection.begin();
        assert!(connection
            .navigate(first, || panic!("旧请求不应导航"))
            .is_none());
        connection.complete();
        assert!(connection.timeout(second, || true).is_some());
    }
    #[test]
    fn success_and_timeout_claim_the_same_attempt_once() {
        let connection = Connection::default();
        let generation = connection.begin();
        assert_eq!(connection.navigate(generation, || 1), Some(1));
        connection.complete();
        assert!(connection
            .timeout(generation, || panic!("成功页不应超时"))
            .is_none());
        let next = connection.begin();
        assert_eq!(connection.timeout(next, || 2), Some(2));
        assert!(connection
            .navigate(next, || panic!("超时请求不应导航"))
            .is_none());
        assert!(connection
            .timeout(next, || panic!("超时不得重复恢复"))
            .is_none());
    }
    #[test]
    fn stale_page_completion_does_not_complete_pending_health_check() {
        let connection = Connection::default();
        let first = connection.begin();
        connection.navigate(first, || ());
        let second = connection.begin();
        connection.complete();
        assert!(connection.timeout(second, || true).is_some());
        assert!(connection
            .timeout(first, || panic!("旧请求不应恢复"))
            .is_none());
    }
}
