//! Ctrl+G: write the draft in the user's editor. The temp file is private
//! and removed whatever happens.
use std::{
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

/// The draft on disk, and the editor to open it with.
pub struct Edit {
    pub path: PathBuf,
    pub program: String,
    pub args: Vec<String>,
}

impl Drop for Edit {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// `$VISUAL`, then `$EDITOR`, then `vi`, split on whitespace so
/// `code --wait` works.
pub fn editor_command() -> Vec<String> {
    ["VISUAL", "EDITOR"]
        .iter()
        .filter_map(|name| std::env::var(name).ok())
        .find(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "vi".into())
        .split_whitespace()
        .map(str::to_owned)
        .collect()
}

pub fn prepare(draft: &str, mut command: Vec<String>) -> Result<Edit, String> {
    if command.is_empty() {
        return Err("No editor is set; set $EDITOR".into());
    }
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let path = std::env::temp_dir().join(format!(
        "octet-prompt-{}-{}.md",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(|e| format!("Cannot create {}: {e}", path.display()))?;
    let program = command.remove(0);
    let edit = Edit {
        path,
        program,
        args: command,
    };
    file.write_all(draft.as_bytes())
        .map_err(|e| format!("Cannot write {}: {e}", edit.path.display()))?;
    Ok(edit)
}

impl Edit {
    /// The edited draft; the original stays when the editor failed or the
    /// result is over the prompt limit. The file goes when `self` drops.
    pub fn finish(self, success: bool) -> Result<String, String> {
        if !success {
            return Err("The editor exited with an error; the draft is unchanged".into());
        }
        let text = std::fs::read_to_string(&self.path)
            .map_err(|e| format!("Cannot read the edited draft: {e}; the draft is unchanged"))?;
        let text = text
            .strip_suffix("\r\n")
            .or_else(|| text.strip_suffix('\n'))
            .unwrap_or(&text)
            .to_owned();
        if text.len() > octet_core::PROMPT_LIMIT {
            return Err(
                "The edited prompt is over the 64 KiB limit; the draft is unchanged".into(),
            );
        }
        Ok(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn edit(draft: &str) -> Edit {
        prepare(draft, vec!["true".into()]).unwrap()
    }
    #[test]
    fn round_trips_an_edited_draft() {
        let edit = edit("first");
        assert_eq!(std::fs::read_to_string(&edit.path).unwrap(), "first");
        let mode = std::fs::metadata(&edit.path).unwrap();
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(mode.permissions().mode() & 0o777, 0o600);
        std::fs::write(&edit.path, "second line\n").unwrap();
        let path = edit.path.clone();
        assert_eq!(edit.finish(true).unwrap(), "second line");
        assert!(!path.exists(), "the temp file is removed");
    }
    #[test]
    fn a_failed_or_oversized_edit_keeps_the_draft() {
        let failed = edit("keep");
        let path = failed.path.clone();
        assert!(failed
            .finish(false)
            .unwrap_err()
            .contains("draft is unchanged"));
        assert!(!path.exists());
        let big = edit("keep");
        std::fs::write(&big.path, "x".repeat(octet_core::PROMPT_LIMIT + 1)).unwrap();
        assert!(big.finish(true).unwrap_err().contains("64 KiB"));
    }
    #[test]
    fn an_empty_command_is_refused() {
        assert!(prepare("x", Vec::new()).is_err());
    }
}
