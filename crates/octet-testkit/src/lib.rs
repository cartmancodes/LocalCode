//! Test support shared by the workspace: the fake vendor binary and
//! self-cleaning temporary directories.
use std::{
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicU64, Ordering},
        OnceLock,
    },
};

/// Builds `protocol-child` for the profile the calling test runs in and
/// returns its path. Release and custom target directories get their own copy.
pub fn protocol_child() -> PathBuf {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY
        .get_or_init(|| {
            // Test executables live in <target>/<profile>/deps/.
            let profile_dir = std::env::current_exe()
                .expect("test executable path")
                .parent()
                .and_then(Path::parent)
                .expect("test executable inside a profile directory")
                .to_path_buf();
            let target_dir = profile_dir.parent().expect("target directory");
            let profile = match profile_dir.file_name().and_then(|name| name.to_str()) {
                Some("debug") | None => "dev",
                Some(other) => other,
            };
            let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
            let workspace = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
            let status = Command::new(cargo)
                .args(["build", "--quiet", "--locked", "-p", "octet-testkit"])
                .args(["--bin", "protocol-child", "--profile", profile])
                .arg("--target-dir")
                .arg(target_dir)
                .current_dir(workspace)
                .status()
                .expect("run cargo to build protocol-child");
            assert!(status.success(), "building protocol-child failed");
            profile_dir.join("protocol-child")
        })
        .clone()
}

/// A unique path under the system temp dir, removed when dropped, so a
/// failing test does not leave it behind. The path is not created.
pub struct TempDir(PathBuf);
impl TempDir {
    pub fn new(prefix: &str) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("{prefix}-{}-{n}", std::process::id()));
        Self(path)
    }
    pub fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        // Some tests put a file at the path to make writes fail.
        if std::fs::remove_dir_all(&self.0).is_err() {
            let _ = std::fs::remove_file(&self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn temp_dirs_are_unique_and_removed() {
        let first = TempDir::new("octet-testkit");
        let second = TempDir::new("octet-testkit");
        assert_ne!(first.path(), second.path());
        std::fs::create_dir_all(first.path()).unwrap();
        let path = first.path().to_path_buf();
        drop(first);
        assert!(!path.exists());
    }
}
