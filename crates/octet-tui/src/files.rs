//! `@` mentions: the workspace's files, listed once in the background and
//! ranked for what the user typed.
use std::path::{Path, PathBuf};

/// Paths kept per workspace.
const LIMIT: usize = 50_000;
/// Suggestions shown at once.
pub const SHOWN: usize = 8;

pub struct Index {
    paths: Vec<String>,
    /// The workspace had more than `LIMIT` files.
    pub capped: bool,
}

/// Where the session's index is.
pub enum Files {
    Unbuilt,
    /// Asked for; the session loop starts the build.
    Wanted,
    Building,
    Ready(Index),
}

impl Index {
    /// Git's view of the workspace when it is a work tree, else a walk.
    pub fn build(root: &Path) -> Index {
        git(root, LIMIT).unwrap_or_else(|| walk(root, LIMIT))
    }
    #[cfg(test)]
    pub fn from_paths(paths: Vec<String>) -> Index {
        Index {
            paths,
            capped: false,
        }
    }
    pub fn rank(&self, query: &str) -> Vec<&str> {
        let query = query.to_lowercase();
        let mut scored: Vec<((bool, usize), &str)> = self
            .paths
            .iter()
            .filter_map(|path| score(path, &query).map(|score| (score, path.as_str())))
            .collect();
        scored.sort_by(|a, b| {
            a.0.cmp(&b.0)
                .then_with(|| a.1.len().cmp(&b.1.len()))
                .then_with(|| a.1.cmp(b.1))
        });
        scored
            .into_iter()
            .take(SHOWN)
            .map(|(_, path)| path)
            .collect()
    }
}

/// Lower is better: (outside the file name, gaps between matched characters).
fn score(path: &str, query: &str) -> Option<(bool, usize)> {
    if query.is_empty() {
        return Some((false, 0));
    }
    let lower = path.to_lowercase();
    let name = &lower[lower.rfind('/').map_or(0, |slash| slash + 1)..];
    subsequence(name, query)
        .map(|gaps| (false, gaps))
        .or_else(|| subsequence(&lower, query).map(|gaps| (true, gaps)))
}

/// Gaps between `query`'s characters found in order in `text`, or None.
fn subsequence(text: &str, query: &str) -> Option<usize> {
    let mut chars = text.chars().enumerate();
    let mut gaps = 0;
    let mut last: Option<usize> = None;
    for wanted in query.chars() {
        let (at, _) = chars.by_ref().find(|(_, c)| *c == wanted)?;
        if last.is_some_and(|last| at != last + 1) {
            gaps += 1;
        }
        last = Some(at);
    }
    Some(gaps)
}

fn git(root: &Path, limit: usize) -> Option<Index> {
    let output = std::process::Command::new("git")
        .args([
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ])
        .current_dir(root)
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let mut paths = Vec::new();
    let mut capped = false;
    for path in output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|p| !p.is_empty())
    {
        if paths.len() == limit {
            capped = true;
            break;
        }
        paths.push(String::from_utf8_lossy(path).into_owned());
    }
    paths.sort();
    paths.dedup();
    Some(Index { paths, capped })
}

fn walk(root: &Path, limit: usize) -> Index {
    let mut paths = Vec::new();
    let mut capped = false;
    let mut pending = vec![PathBuf::new()];
    'walk: while let Some(dir) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(root.join(&dir)) else {
            continue;
        };
        let mut entries: Vec<_> = entries.filter_map(Result::ok).collect();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') || name == "target" || name == "node_modules" {
                continue;
            }
            let path = dir.join(&name);
            // Symlinks are skipped, so a link loop cannot trap the walk.
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => pending.push(path),
                Ok(kind) if kind.is_file() => {
                    if paths.len() == limit {
                        capped = true;
                        break 'walk;
                    }
                    paths.push(path.to_string_lossy().into_owned());
                }
                _ => {}
            }
        }
    }
    paths.sort();
    Index { paths, capped }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn index(paths: &[&str]) -> Index {
        Index::from_paths(paths.iter().map(|p| p.to_string()).collect())
    }
    #[test]
    fn file_name_matches_come_first_then_fewer_gaps_then_shorter_paths() {
        let all = index(&[
            "lib/x.rs",
            "a/lib.rs",
            "xmainx_long_name.rs",
            "m_a_i_n.rs",
            "src/main.rs",
        ]);
        assert_eq!(all.rank("lib"), ["a/lib.rs", "lib/x.rs"]);
        assert_eq!(
            all.rank("main"),
            ["src/main.rs", "xmainx_long_name.rs", "m_a_i_n.rs"]
        );
        assert_eq!(all.rank("MAIN")[0], "src/main.rs", "case is ignored");
        assert!(all.rank("zzz").is_empty());
        assert_eq!(all.rank("").len(), 5);
    }
    #[test]
    fn shows_at_most_eight() {
        let many: Vec<String> = (0..20).map(|i| format!("f{i}.rs")).collect();
        assert_eq!(Index::from_paths(many).rank("f").len(), SHOWN);
    }
    #[test]
    fn walks_a_plain_folder_skipping_hidden_and_build_output() {
        let dir = octet_testkit::TempDir::new("octet-files-walk");
        for path in [
            "a.rs",
            "sub/b.rs",
            ".hidden/c.rs",
            "target/d.rs",
            "node_modules/e.js",
        ] {
            let path = dir.path().join(path);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, "").unwrap();
        }
        let index = walk(dir.path(), LIMIT);
        assert_eq!(index.paths, ["a.rs", "sub/b.rs"]);
        assert!(!index.capped);
        let capped = walk(dir.path(), 1);
        assert_eq!(capped.paths.len(), 1);
        assert!(capped.capped);
    }
    #[test]
    fn lists_tracked_and_untracked_files_but_not_ignored_ones() {
        let dir = octet_testkit::TempDir::new("octet-files-git");
        std::fs::create_dir_all(dir.path()).unwrap();
        let run_git = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(dir.path())
                .output()
                .unwrap()
                .status
                .success());
        };
        run_git(&["init", "-q"]);
        for (name, text) in [
            ("tracked.rs", ""),
            ("untracked.rs", ""),
            ("ignored.log", ""),
            (".gitignore", "*.log\n"),
        ] {
            std::fs::write(dir.path().join(name), text).unwrap();
        }
        run_git(&["add", "tracked.rs"]);
        let index = git(dir.path(), LIMIT).expect("a work tree lists through git");
        assert!(index.paths.contains(&"tracked.rs".to_string()));
        assert!(index.paths.contains(&"untracked.rs".to_string()));
        assert!(!index.paths.iter().any(|p| p == "ignored.log"));
        let plain = octet_testkit::TempDir::new("octet-files-plain");
        std::fs::create_dir_all(plain.path()).unwrap();
        assert!(git(plain.path(), LIMIT).is_none());
    }
}
