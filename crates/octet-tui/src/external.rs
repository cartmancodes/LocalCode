//! Ctrl+G: write the draft in the user's editor. The temp file sits in a
//! private directory of its own and goes, with the directory, whatever
//! happens.
use std::{
    ffi::OsString,
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

/// The draft on disk, and the editor to open it with.
pub struct Edit {
    /// The draft's file, inside `dir`.
    pub path: PathBuf,
    /// The private (0700) directory holding it.
    dir: PathBuf,
    /// `$VISUAL` or `$EDITOR` as set, run by the shell.
    pub editor: String,
}

impl Drop for Edit {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// `$VISUAL`, then `$EDITOR`, then `vi`: a shell command, as git takes it,
/// so `code --wait` and a quoted path with spaces both work.
pub fn editor_command() -> String {
    ["VISUAL", "EDITOR"]
        .iter()
        .filter_map(|name| std::env::var(name).ok())
        .find(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "vi".into())
}

/// Why the draft could not go to the editor, or come back. Every message
/// says the draft is safe where that matters.
#[derive(Debug, thiserror::Error)]
pub enum EditorError {
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
    /// The shell could not find or run the editor (exit 127 or 126).
    #[error("Cannot start {0}: not found or not executable; the draft is unchanged")]
    NoEditor(String),
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

/// Writes `draft` to a new file in a new private directory, for `editor`.
///
/// # Errors
///
/// `Create` if the directory or file cannot be made, `Write` if the draft
/// cannot be written.
pub fn prepare(draft: &str, editor: &str) -> Result<Edit, EditorError> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    // Unpredictable, and created exclusively: in a shared temp directory
    // nobody else can pre-create it, or read the draft through it.
    let dir = loop {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |since| since.subsec_nanos());
        let dir = std::env::temp_dir().join(format!(
            "octet-edit-{}-{}-{nonce:08x}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        match std::fs::DirBuilder::new().mode(0o700).create(&dir) {
            Ok(()) => break dir,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(source) => return Err(EditorError::Create { path: dir, source }),
        }
    };
    let edit = Edit {
        path: dir.join("prompt.md"),
        dir,
        editor: editor.to_owned(),
    };
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&edit.path)
        .map_err(|source| EditorError::Create {
            path: edit.path.clone(),
            source,
        })?;
    file.write_all(draft.as_bytes())
        .map_err(|source| EditorError::Write {
            path: edit.path.clone(),
            source,
        })?;
    Ok(edit)
}

impl Edit {
    /// The program and arguments that open the draft: the shell runs the
    /// editor command with the file as its last argument, as git does.
    pub fn command(&self) -> (&'static str, Vec<OsString>) {
        let script = format!("{} \"$@\"", self.editor);
        (
            "sh",
            vec![
                "-c".into(),
                script.into(),
                "octet-editor".into(),
                self.path.clone().into_os_string(),
            ],
        )
    }
    /// The edited draft; the original stays when the editor failed or the
    /// result is over the prompt limit. The file goes when `self` drops.
    pub fn finish(self, code: Option<i32>) -> Result<String, EditorError> {
        match code {
            Some(0) => {}
            // The shell's codes for a command it cannot find or run.
            Some(126 | 127) => return Err(EditorError::NoEditor(self.editor.clone())),
            _ => return Err(EditorError::EditorFailed),
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
        prepare(draft, "true").unwrap()
    }
    #[test]
    fn editor_messages_keep_their_wording() {
        let io = || std::io::Error::other("disk full");
        let path = std::path::PathBuf::from("/tmp/p.md");
        for (error, text) in [
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
                EditorError::NoEditor("/no/editor".into()),
                "Cannot start /no/editor: not found or not executable; the draft is unchanged",
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
        assert_eq!(edit.finish(Some(0)).unwrap(), "second line");
        assert!(!path.exists(), "the temp file is removed");
    }
    #[test]
    fn a_failed_or_oversized_edit_keeps_the_draft() {
        let failed = edit("keep");
        let path = failed.path.clone();
        assert!(
            failed
                .finish(Some(1))
                .unwrap_err()
                .to_string()
                .contains("draft is unchanged")
        );
        assert!(!path.exists());
        let big = edit("keep");
        std::fs::write(&big.path, "x".repeat(octet_core::PROMPT_LIMIT + 1)).unwrap();
        assert!(matches!(big.finish(Some(0)), Err(EditorError::TooLarge)));
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
            let _ = done.send(edit.finish(Some(0)));
        });
        let outcome = result
            .recv_timeout(std::time::Duration::from_secs(3))
            .expect("finish read the whole stream");
        assert!(matches!(outcome, Err(EditorError::TooLarge)));
    }
    #[test]
    fn editor_file_is_in_a_private_directory() {
        use std::os::unix::fs::PermissionsExt;
        let edit = edit("draft");
        let dir = edit.path.parent().unwrap().to_path_buf();
        let mode = std::fs::metadata(&dir).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o700, "{}", dir.display());
        assert_ne!(
            dir,
            std::env::temp_dir(),
            "not loose in the shared temp dir"
        );
        drop(edit);
        assert!(!dir.exists(), "the directory goes with the file");
    }
    #[test]
    fn an_editor_path_with_spaces_runs() {
        let tools = octet_testkit::TempDir::new("octet editor with spaces");
        std::fs::create_dir_all(tools.path()).unwrap();
        let script = octet_testkit::write_script(
            tools.path(),
            "my editor",
            "printf 'edited:%s' \"$1\" > \"$2\"\n",
        );
        // Quoted as a user sets $EDITOR for a path with spaces, plus a flag.
        let editor = format!("'{}' --wait", script.display());
        let edit = prepare("draft", &editor).unwrap();
        let (program, args) = edit.command();
        let status = std::process::Command::new(program)
            .args(&args)
            .status()
            .unwrap();
        assert!(status.success());
        assert_eq!(edit.finish(Some(0)).unwrap(), "edited:--wait");
    }
}
