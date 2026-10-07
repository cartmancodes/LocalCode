//! Helpers the source-checking tests share.
use std::path::{Path, PathBuf};

/// The repository root.
pub fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// Every file under `dir` that `keep` accepts, skipping build output and
/// version control.
pub fn files_under(dir: &Path, keep: &dyn Fn(&Path) -> bool) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut dirs = vec![dir.to_path_buf()];
    while let Some(dir) = dirs.pop() {
        for entry in std::fs::read_dir(&dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
            let path = entry.expect("directory entry").path();
            let skipped = path
                .file_name()
                .is_some_and(|name| name == "target" || name == ".git");
            if path.is_dir() {
                if !skipped {
                    dirs.push(path);
                }
            } else if keep(&path) {
                found.push(path);
            }
        }
    }
    found
}

/// Rust sources.
pub fn is_rust(path: &Path) -> bool {
    path.extension().is_some_and(|ext| ext == "rs")
}
