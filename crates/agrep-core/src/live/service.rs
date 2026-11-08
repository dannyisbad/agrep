use super::{Snapshot, Watcher};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    thread,
    time::Duration,
};

/// One foreground-owned watcher shared by every browser request / board frame.
pub struct Service {
    shared: Arc<(Mutex<(u64, Snapshot)>, Condvar)>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Default for Service {
    fn default() -> Self {
        Self::new()
    }
}
impl Service {
    pub fn new() -> Self {
        let shared = Arc::new((Mutex::new((0, Snapshot::default())), Condvar::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let out = shared.clone();
        let done = stop.clone();
        let thread = thread::spawn(move || {
            let mut watcher = Watcher::new();
            while !done.load(Ordering::Relaxed) {
                let _ = watcher.poll(&mut |_| Ok(()));
                let (lock, cv) = &*out;
                let mut shared = lock.lock().unwrap();
                shared.0 += 1;
                shared.1 = watcher.snapshot();
                drop(shared);
                cv.notify_all();
                // No watch covers agent stores, so they are polled every 700 ms.
                let guard = lock.lock().unwrap();
                let _ = cv.wait_timeout(guard, Duration::from_millis(700)).unwrap();
            }
        });
        Self {
            shared,
            stop,
            thread: Some(thread),
        }
    }
    pub fn snapshot(&self, wait: Duration) -> Snapshot {
        let (lock, cv) = &*self.shared;
        let guard = lock.lock().unwrap();
        let (guard, _) = cv.wait_timeout_while(guard, wait, |s| s.1.booting).unwrap();
        guard.1.clone()
    }
    pub fn snapshot_if_changed(&self, generation: &mut u64, snapshot: &mut Snapshot) -> bool {
        let guard = self.shared.0.lock().unwrap();
        if *generation == guard.0 {
            return false;
        }
        *generation = guard.0;
        *snapshot = guard.1.clone();
        true
    }
}
impl Drop for Service {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.shared.1.notify_all();
        // Cancellation never extends a caller's bounded one-shot wait. A detached
        // census finishes its current read, sees stop, and drops all owned state.
        self.thread.take();
    }
}
