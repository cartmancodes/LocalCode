//! Helpers the source-checking tests share.
use std::path::{Path, PathBuf};

/// The repository root.
pub(crate) fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// Every file under `dir` that `keep` accepts, skipping build output and
/// version control.
pub(crate) fn files_under(dir: &Path, keep: &dyn Fn(&Path) -> bool) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut dirs = vec![dir.to_path_buf()];
    while let Some(dir) = dirs.pop() {
        for entry in std::fs::read_dir(&dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
            let entry = entry.expect("directory entry");
            let path = entry.path();
            let skipped = path
                .file_name()
                .is_some_and(|name| name == "target" || name == ".git");
            // The entry's own type: a symlinked directory is not followed,
            // so a link loop cannot trap the walk.
            let kind = entry.file_type().expect("entry type");
            if kind.is_dir() {
                if !skipped {
                    dirs.push(path);
                }
            } else if kind.is_file() && keep(&path) {
                found.push(path);
            }
        }
    }
    found
}

/// Rust sources.
pub(crate) fn is_rust(path: &Path) -> bool {
    path.extension().is_some_and(|ext| ext == "rs")
}
