//! `@` mentions: the workspace's files, listed once in the background and
//! ranked for what the user typed.
use std::path::{Path, PathBuf};

/// Paths kept per workspace.
const LIMIT: usize = 50_000;
/// Suggestions shown at once.
pub const SHOWN: usize = 8;

pub struct Index {
    paths: Vec<String>,
    /// `paths` lowercased once, for ranking on every keystroke.
    lower: Vec<String>,
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
    fn new(paths: Vec<String>, capped: bool) -> Index {
        let lower = paths.iter().map(|path| path.to_lowercase()).collect();
        Index {
            paths,
            lower,
            capped,
        }
    }
    #[cfg(test)]
    pub fn from_paths(paths: Vec<String>) -> Index {
        Index::new(paths, false)
    }
    pub fn rank(&self, query: &str) -> Vec<&str> {
        let query = query.to_lowercase();
        let mut scored: Vec<((bool, usize), &str)> = self
            .paths
            .iter()
            .zip(&self.lower)
            .filter_map(|(path, lower)| score(lower, &query).map(|score| (score, path.as_str())))
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

/// Lower is better: (outside the file name, gaps between matched
/// characters). `lower` and `query` are already lowercase.
fn score(lower: &str, query: &str) -> Option<(bool, usize)> {
    if query.is_empty() {
        return Some((false, 0));
    }
    let name = &lower[lower.rfind('/').map_or(0, |slash| slash + 1)..];
    subsequence(name, query)
        .map(|gaps| (false, gaps))
        .or_else(|| subsequence(lower, query).map(|gaps| (true, gaps)))
}

/// The fewest gaps with which `query`'s characters appear in order in
/// `text`, or None when they don't.
fn subsequence(text: &str, query: &str) -> Option<usize> {
    let query: Vec<char> = query.chars().collect();
    let none = usize::MAX;
    // ending[j]: fewest gaps matching query[..j] with its last character at
    // the previous position; best[j]: the same, ending anywhere before.
    let mut ending = vec![none; query.len() + 1];
    let mut best = vec![none; query.len() + 1];
    for c in text.chars() {
        let mut here = vec![none; query.len() + 1];
        for j in 1..=query.len() {
            if c != query[j - 1] {
                continue;
            }
            here[j] = if j == 1 {
                0
            } else {
                ending[j - 1].min(best[j - 1].saturating_add(1))
            };
        }
        for j in 1..=query.len() {
            best[j] = best[j].min(here[j]);
        }
        ending = here;
    }
    (best[query.len()] != none).then_some(best[query.len()])
}

fn git(root: &Path, limit: usize) -> Option<Index> {
    use std::io::BufRead;
    let mut child = std::process::Command::new("git")
        .args([
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ])
        .current_dir(root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    // Read paths as they come, so a huge tree costs no more than the cap.
    let stdout = std::io::BufReader::new(child.stdout.take()?);
    let mut paths = Vec::new();
    let mut capped = false;
    for path in stdout
        .split(0)
        .map_while(Result::ok)
        .filter(|p| !p.is_empty())
    {
        if paths.len() == limit {
            capped = true;
            let _ = child.kill();
            break;
        }
        paths.push(String::from_utf8_lossy(&path).into_owned());
    }
    let finished = child.wait().ok()?;
    if !capped && !finished.success() {
        return None;
    }
    paths.sort();
    paths.dedup();
    Some(Index::new(paths, capped))
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
    Index::new(paths, capped)
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
    fn matching_finds_the_fewest_gaps_not_the_first_letters() {
        // Greedy matching takes "ma" from "max" and then has a gap.
        assert_eq!(subsequence("maxmain", "main"), Some(0));
        assert_eq!(subsequence("m_a_i_n", "main"), Some(3));
        assert_eq!(subsequence("nope", "main"), None);
        let all = index(&["maxmain.rs", "mainly_long_name.rs"]);
        assert_eq!(all.rank("main")[0], "maxmain.rs", "no gaps, shorter");
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
