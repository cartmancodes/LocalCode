//! Leaves its process group, then sleeps: a stand-in for a background job
//! that escaped the group a `!` command runs in.
use std::time::Duration;

fn main() {
    // SAFETY: setsid only changes this process's session and group.
    unsafe {
        libc::setsid();
    }
    let seconds = std::env::args()
        .nth(1)
        .and_then(|value| value.parse().ok())
        .unwrap_or(60);
    std::thread::sleep(Duration::from_secs(seconds));
}
