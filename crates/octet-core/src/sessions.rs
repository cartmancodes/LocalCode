//! Recent vendor sessions in a workspace, read from Octet's own journals.
use crate::Engine;
use octet_store::{journal_stamp, read_summary, JournalSummary};
use std::path::{Path, PathBuf};

/// The most journals [`recent_sessions`] reads, newest first, so a long
/// history does not slow the listing down.
const SCAN_LIMIT: usize = 400;

/// One resumable vendor session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentSession {
    /// The provider that owns the session.
    pub engine: Engine,
    /// The newest journal for it.
    pub journal: PathBuf,
    /// What that journal says about it; `first_prompt` comes from an older
    /// journal of the same session when the newest has none.
    pub summary: JournalSummary,
}

/// Up to `limit` vendor sessions run in `workspace`, newest first, one entry
/// per session. Journals of other workspaces, the offline demo, sessions
/// the vendor never named, and files that are not journals are skipped.
pub async fn recent_sessions(
    directory: &Path,
    workspace: &Path,
    limit: usize,
) -> Vec<RecentSession> {
    let Ok(mut entries) = tokio::fs::read_dir(directory).await else {
        return Vec::new();
    };
    let mut journals = Vec::new();
    while let Ok(Some(entry)) = entries.next_entry().await {
        let path = entry.path();
        if let Some(stamp) = journal_stamp(&path) {
            journals.push((stamp, path));
        }
    }
    journals.sort_unstable_by(|a, b| b.cmp(a));
    let mut found: Vec<RecentSession> = Vec::new();
    for (_, journal) in journals.into_iter().take(SCAN_LIMIT) {
        let Some(summary) = read_summary(&journal).await else {
            continue;
        };
        let Some(engine) = Engine::parse(&summary.engine).filter(|e| e.is_vendor()) else {
            continue;
        };
        if summary.session.is_empty() || summary.cwd != workspace {
            continue;
        }
        // Reconnects write a journal per connection; the newest stands for
        // the session, and an older one may know its first prompt.
        if let Some(known) = found
            .iter_mut()
            .find(|s| s.engine == engine && s.summary.session == summary.session)
        {
            if known.summary.first_prompt.is_none() {
                known.summary.first_prompt = summary.first_prompt;
            }
            continue;
        }
        if found.len() == limit {
            break;
        }
        found.push(RecentSession {
            engine,
            journal,
            summary,
        });
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A journal in `dir` for `engine` in `cwd`, naming `session`, with an
    /// optional first prompt.
    fn journal(
        dir: &Path,
        stamp: u64,
        engine: &str,
        cwd: &str,
        session: &str,
        prompt: Option<&str>,
    ) {
        let header = json!({"engine":engine,"cwd":cwd,"resume":null,"model":null,"mode":"ask"});
        let mut records = vec![
            json!({"format":"octet-preview-1","seq":0,"type":"session","data":header}),
            json!({"format":"octet-preview-1","seq":1,"type":"ready","data":session}),
        ];
        if let Some(prompt) = prompt {
            records.push(json!({"format":"octet-preview-1","seq":2,"type":"user","data":prompt}));
        }
        let text: String = records.iter().map(|r| format!("{r}\n")).collect();
        std::fs::write(dir.join(format!("session-{stamp}-1.jsonl")), text).unwrap();
    }

    #[tokio::test]
    async fn recent_sessions_lists_newest_first_for_this_workspace() {
        let temp = octet_testkit::TempDir::new("octet-core-sessions");
        let dir = temp.path();
        std::fs::create_dir_all(dir).unwrap();
        journal(dir, 1, "codex", "/work", "t-1", Some("one"));
        journal(
            dir,
            2,
            "codex",
            "/elsewhere",
            "t-9",
            Some("other workspace"),
        );
        journal(dir, 3, "claude", "/work", "c-1", Some("two"));
        journal(dir, 4, "demo", "/work", "demo · offline", Some("demo"));
        journal(dir, 5, "codex", "/work", "t-1", None);
        journal(dir, 6, "claude", "/work", "", None);
        std::fs::write(dir.join("notes.txt"), "not a journal").unwrap();
        let found = recent_sessions(dir, Path::new("/work"), 20).await;
        let listed: Vec<(&str, &str, Option<&str>)> = found
            .iter()
            .map(|s| {
                (
                    s.engine.as_str(),
                    s.summary.session.as_str(),
                    s.summary.first_prompt.as_deref(),
                )
            })
            .collect();
        assert_eq!(
            listed,
            [
                ("codex", "t-1", Some("one")),
                ("claude", "c-1", Some("two"))
            ]
        );
        assert!(found[0].journal.ends_with("session-5-1.jsonl"));
        assert_eq!(recent_sessions(dir, Path::new("/work"), 1).await.len(), 1);
        assert!(
            recent_sessions(&dir.join("missing"), Path::new("/work"), 20)
                .await
                .is_empty()
        );
    }
}
