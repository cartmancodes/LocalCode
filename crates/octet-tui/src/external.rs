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

/// Why the draft could not go to the editor, or come back. Every message
/// says the draft is safe where that matters.
#[derive(Debug, thiserror::Error)]
pub enum EditorError {
    #[error("No editor is set; set $EDITOR")]
    NoEditor,
    #[error("Cannot create {}: {source}", path.display())]
    Create {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("Cannot write {}: {source}", path.display())]
    Write {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("The editor exited with an error; the draft is unchanged")]
    EditorFailed,
    #[error("Cannot read the edited draft: {0}; the draft is unchanged")]
    Read(#[source] std::io::Error),
    #[error(
        "The edited prompt is over the {} KiB limit; the draft is unchanged",
        octet_core::PROMPT_LIMIT / 1024
    )]
    TooLarge,
    #[error("The edited prompt is not UTF-8 text; the draft is unchanged")]
    NotUtf8,
}

pub fn prepare(draft: &str, mut command: Vec<String>) -> Result<Edit, EditorError> {
    if command.is_empty() {
        return Err(EditorError::NoEditor);
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
        .map_err(|source| EditorError::Create {
            path: path.clone(),
            source,
        })?;
    let program = command.remove(0);
    let edit = Edit {
        path,
        program,
        args: command,
    };
    file.write_all(draft.as_bytes())
        .map_err(|source| EditorError::Write {
            path: edit.path.clone(),
            source,
        })?;
    Ok(edit)
}

impl Edit {
    /// The edited draft; the original stays when the editor failed or the
    /// result is over the prompt limit. The file goes when `self` drops.
    pub fn finish(self, success: bool) -> Result<String, EditorError> {
        if !success {
            return Err(EditorError::EditorFailed);
        }
        use std::io::Read;
        // Read no more than the limit plus a trailing newline and one byte,
        // so an enormous result is refused without loading it.
        let mut bytes = Vec::new();
        std::fs::File::open(&self.path)
            .and_then(|file| {
                file.take(octet_core::PROMPT_LIMIT as u64 + 3)
                    .read_to_end(&mut bytes)
            })
            .map_err(EditorError::Read)?;
        if bytes.len() > octet_core::PROMPT_LIMIT + 2 {
            return Err(EditorError::TooLarge);
        }
        let text = String::from_utf8(bytes).map_err(|_| EditorError::NotUtf8)?;
        let text = text
            .strip_suffix("\r\n")
            .or_else(|| text.strip_suffix('\n'))
            .unwrap_or(&text)
            .to_owned();
        if text.len() > octet_core::PROMPT_LIMIT {
            return Err(EditorError::TooLarge);
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
    fn editor_messages_keep_their_wording() {
        let io = || std::io::Error::other("disk full");
        let path = std::path::PathBuf::from("/tmp/p.md");
        for (error, text) in [
            (EditorError::NoEditor, "No editor is set; set $EDITOR"),
            (
                EditorError::Create {
                    path: path.clone(),
                    source: io(),
                },
                "Cannot create /tmp/p.md: disk full",
            ),
            (
                EditorError::Write {
                    path: path.clone(),
                    source: io(),
                },
                "Cannot write /tmp/p.md: disk full",
            ),
            (
                EditorError::EditorFailed,
                "The editor exited with an error; the draft is unchanged",
            ),
            (
                EditorError::Read(io()),
                "Cannot read the edited draft: disk full; the draft is unchanged",
            ),
            (
                EditorError::TooLarge,
                "The edited prompt is over the 64 KiB limit; the draft is unchanged",
            ),
            (
                EditorError::NotUtf8,
                "The edited prompt is not UTF-8 text; the draft is unchanged",
            ),
        ] {
            assert_eq!(error.to_string(), text);
        }
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
            .to_string()
            .contains("draft is unchanged"));
        assert!(!path.exists());
        let big = edit("keep");
        std::fs::write(&big.path, "x".repeat(octet_core::PROMPT_LIMIT + 1)).unwrap();
        assert!(matches!(big.finish(true), Err(EditorError::TooLarge)));
    }
    #[test]
    fn reading_the_edit_stops_at_the_limit() {
        // A pipe that never closes stands in for a file too large to read
        // whole: an unbounded read would never return.
        let edit = edit("keep");
        std::fs::remove_file(&edit.path).unwrap();
        let path = std::ffi::CString::new(edit.path.to_str().unwrap()).unwrap();
        // SAFETY: creates a FIFO at a path this test owns.
        assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
        let fifo = edit.path.clone();
        std::thread::spawn(move || {
            let mut writer = std::fs::OpenOptions::new().write(true).open(fifo).unwrap();
            let _ = writer.write_all(&vec![b'x'; octet_core::PROMPT_LIMIT + 10]);
            std::thread::sleep(std::time::Duration::from_secs(10));
        });
        let (done, result) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = done.send(edit.finish(true));
        });
        let outcome = result
            .recv_timeout(std::time::Duration::from_secs(3))
            .expect("finish read the whole stream");
        assert!(matches!(outcome, Err(EditorError::TooLarge)));
    }
    #[test]
    fn an_empty_command_is_refused() {
        assert!(prepare("x", Vec::new()).is_err());
    }
}
