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
pub struct Journal {
    file: File,
    pub path: PathBuf,
    bytes: u64,
    sequence: u64,
}
impl Journal {
    pub async fn create(directory: &Path) -> io::Result<Self> {
        fs::create_dir_all(directory).await?;
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let path = directory.join(format!("session-{nonce}-{}.jsonl", std::process::id()));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600);
        let file = options.open(&path).await?;
        Ok(Self {
            file,
            path,
            bytes: 0,
            sequence: 0,
        })
    }
    pub async fn append(
        &mut self,
        kind: &str,
        data: serde_json::Value,
        durable: bool,
    ) -> io::Result<()> {
        let value = serde_json::json!({"format":"localcode-preview-1","seq":self.sequence,"type":kind,"data":data});
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
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn journal_is_private_unique_and_preserves_unicode() {
        let temp = lc_testkit::TempDir::new("lc-store-test");
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
