//! Append-only preview journals, deliberately separate from existing v3 sessions.
use std::{
    io,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::{
    fs::{self, File, OpenOptions},
    io::AsyncWriteExt,
};
const LIMIT: u64 = 64 * 1024 * 1024;
/// Opens a new owner-only file for writing; fails if `path` exists.
///
/// # Errors
///
/// Fails if `path` exists or cannot be created.
pub async fn create_private(path: &Path) -> io::Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .await
}
/// One session's append-only JSONL record, owner-only and capped at 64 MiB.
pub struct Journal {
    file: File,
    /// Where the journal is on disk.
    pub path: PathBuf,
    bytes: u64,
    sequence: u64,
}
impl Journal {
    /// Creates a new journal in `directory`, named by time and process ID so
    /// two sessions never share one.
    ///
    /// # Errors
    ///
    /// Fails if `directory` cannot be created or the file cannot be opened.
    pub async fn create(directory: &Path) -> io::Result<Self> {
        fs::create_dir_all(directory).await?;
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let path = directory.join(format!("session-{nonce}-{}.jsonl", std::process::id()));
        let file = create_private(&path).await?;
        Ok(Self {
            file,
            path,
            bytes: 0,
            sequence: 0,
        })
    }
    /// Appends one record of type `kind`; `durable` also flushes it to disk,
    /// for records that must survive a crash (turn ends, shutdown).
    ///
    /// # Errors
    ///
    /// Fails if the write fails or would take the journal past 64 MiB.
    pub async fn append(
        &mut self,
        kind: &str,
        data: serde_json::Value,
        durable: bool,
    ) -> io::Result<()> {
        let value = serde_json::json!({"format":"octet-preview-1","seq":self.sequence,"type":kind,"data":data});
        let mut bytes = serde_json::to_vec(&value)?;
        bytes.push(b'\n');
        if self.bytes + bytes.len() as u64 > LIMIT {
            return Err(io::Error::other(
                "64 MiB session journal limit reached; session stopped",
            ));
        }
        self.file.write_all(&bytes).await?;
        self.bytes += bytes.len() as u64;
        self.sequence += 1;
        if durable {
            self.file.sync_data().await?;
        }
        Ok(())
    }
}
/// How much of a journal [`read_summary`] reads.
const SUMMARY_BYTES: u64 = 64 * 1024;
/// What a session browser shows for one journal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalSummary {
    /// When the journal was created, from its file name.
    pub started: SystemTime,
    /// The provider's name, as the header recorded it.
    pub engine: String,
    /// The workspace the session ran in.
    pub cwd: PathBuf,
    /// The model requested, if any.
    pub model: Option<String>,
    /// The vendor session ID; empty if the vendor never named one.
    pub session: String,
    /// The first prompt the user sent, if any.
    pub first_prompt: Option<String>,
}
/// Summarises the journal at `path` from at most its first 64 KiB. `None` if
/// it is not a journal: a name other than `session-<nanos>-<pid>.jsonl`, or
/// a first record that is not a session header.
///
/// Reading stops at the first line that does not parse (a torn tail), at
/// 64 KiB, or once both the session ID and the first prompt are known.
/// Claude names its session only after the first prompt, so both are read.
pub async fn read_summary(path: &Path) -> Option<JournalSummary> {
    use tokio::io::AsyncReadExt;
    let nanos: u64 = path
        .file_name()?
        .to_str()?
        .strip_prefix("session-")?
        .strip_suffix(".jsonl")?
        .split('-')
        .next()?
        .parse()
        .ok()?;
    let mut bytes = Vec::new();
    File::open(path)
        .await
        .ok()?
        .take(SUMMARY_BYTES)
        .read_to_end(&mut bytes)
        .await
        .ok()?;
    // Only whole lines: the last one may be cut by the limit or a crash.
    let complete = bytes.iter().rposition(|&b| b == b'\n').map_or(0, |i| i + 1);
    let mut records = bytes[..complete]
        .split(|&b| b == b'\n')
        .map_while(|line| serde_json::from_slice::<serde_json::Value>(line).ok());
    let header = records.next()?;
    if header["format"] != "octet-preview-1" || header["type"] != "session" {
        return None;
    }
    let data = &header["data"];
    let mut summary = JournalSummary {
        started: UNIX_EPOCH + std::time::Duration::from_nanos(nanos),
        engine: data["engine"].as_str()?.to_owned(),
        cwd: PathBuf::from(data["cwd"].as_str()?),
        model: data["model"].as_str().map(str::to_owned),
        session: String::new(),
        first_prompt: None,
    };
    for record in records {
        match (record["type"].as_str(), record["data"].as_str()) {
            (Some("ready"), Some(session)) if !session.is_empty() => {
                summary.session = session.to_owned();
            }
            (Some("user"), Some(text)) if summary.first_prompt.is_none() => {
                summary.first_prompt = Some(text.to_owned());
            }
            _ => {}
        }
        if !summary.session.is_empty() && summary.first_prompt.is_some() {
            break;
        }
    }
    Some(summary)
}
#[cfg(test)]
mod tests {
    use super::*;
    /// Writes `lines` (records as `(type, data)`) to a journal named for
    /// `stamp` in `dir`, then `tail` unterminated.
    fn journal_file(
        dir: &Path,
        stamp: u128,
        lines: &[(&str, serde_json::Value)],
        tail: &str,
    ) -> PathBuf {
        std::fs::create_dir_all(dir).unwrap();
        let path = dir.join(format!("session-{stamp}-1.jsonl"));
        let mut text = String::new();
        for (seq, (kind, data)) in lines.iter().enumerate() {
            let record =
                serde_json::json!({"format":"octet-preview-1","seq":seq,"type":kind,"data":data});
            text.push_str(&format!("{record}\n"));
        }
        text.push_str(tail);
        std::fs::write(&path, text).unwrap();
        path
    }
    fn header(engine: &str) -> (&'static str, serde_json::Value) {
        (
            "session",
            serde_json::json!({"engine":engine,"cwd":"/work","resume":null,"model":"m1","mode":"ask"}),
        )
    }
    #[tokio::test]
    async fn summary_survives_a_torn_tail() {
        let temp = octet_testkit::TempDir::new("octet-store-summary");
        // Claude names its session only after the first prompt.
        let path = journal_file(
            temp.path(),
            1_700_000_000_000_000_000,
            &[
                header("claude"),
                ("ready", serde_json::json!("")),
                ("user", serde_json::json!("fix the build")),
                ("ready", serde_json::json!("claude-1")),
            ],
            r#"{"format":"octet-preview-1","seq":4,"ty"#,
        );
        let summary = read_summary(&path).await.unwrap();
        assert_eq!(summary.engine, "claude");
        assert_eq!(summary.cwd, Path::new("/work"));
        assert_eq!(summary.model.as_deref(), Some("m1"));
        assert_eq!(summary.session, "claude-1");
        assert_eq!(summary.first_prompt.as_deref(), Some("fix the build"));
        assert_eq!(
            summary.started,
            UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000)
        );
    }
    #[tokio::test]
    async fn summary_stops_at_a_bad_line_and_after_64_kib() {
        let temp = octet_testkit::TempDir::new("octet-store-bad");
        let path = journal_file(
            temp.path(),
            1,
            &[header("codex"), ("ready", serde_json::json!("t-1"))],
            "not json\n",
        );
        let summary = read_summary(&path).await.unwrap();
        assert_eq!(summary.session, "t-1");
        assert_eq!(summary.first_prompt, None);
        let long = "x".repeat(70 * 1024);
        let path = journal_file(
            temp.path(),
            2,
            &[
                header("codex"),
                ("user", serde_json::json!(long)),
                ("ready", serde_json::json!("too-late")),
            ],
            "",
        );
        assert_eq!(read_summary(&path).await.unwrap().session, "");
    }
    #[tokio::test]
    async fn non_journals_are_ignored() {
        let temp = octet_testkit::TempDir::new("octet-store-other");
        let dir = temp.path();
        std::fs::create_dir_all(dir).unwrap();
        let notes = dir.join("notes.txt");
        std::fs::write(&notes, "hello\n").unwrap();
        assert!(read_summary(&notes).await.is_none());
        let headless = journal_file(dir, 3, &[("ready", serde_json::json!("x"))], "");
        assert!(read_summary(&headless).await.is_none());
        let empty = journal_file(dir, 4, &[], "");
        assert!(read_summary(&empty).await.is_none());
        assert!(read_summary(&dir.join("session-5-1.jsonl")).await.is_none());
    }
    #[tokio::test]
    async fn private_files_are_new_and_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = octet_testkit::TempDir::new("octet-store-private");
        fs::create_dir_all(dir.path()).await.unwrap();
        let path = dir.path().join("file");
        drop(create_private(&path).await.unwrap());
        let mode = fs::metadata(&path).await.unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        assert_eq!(
            create_private(&path).await.unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
    }
    #[tokio::test]
    async fn journal_is_private_unique_and_preserves_unicode() {
        let temp = octet_testkit::TempDir::new("octet-store-test");
        let dir = temp.path();
        let mut first = Journal::create(dir).await.unwrap();
        let second = Journal::create(dir).await.unwrap();
        assert_ne!(first.path, second.path);
        first
            .append("text", serde_json::json!("hello 世界\n"), true)
            .await
            .unwrap();
        let record: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&first.path).await.unwrap()).unwrap();
        assert_eq!(record["data"], "hello 世界\n");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&first.path)
                    .await
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        drop(first);
        drop(second);
    }
}
