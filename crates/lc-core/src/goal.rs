//! Durable, provider-neutral goal state. A vendor turn is never resumed merely
//! because a goal file exists; the user explicitly resumes after restart.
use crate::Outcome;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub const MAX_GOAL_TURNS: u32 = 200;
pub const COMPLETION_MARKER: &str = "[[LOCALCODE_GOAL_COMPLETE]]";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Active,
    Paused,
    Complete,
}
impl Status {
    /// The goal file's spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Active => "active",
            Status::Paused => "paused",
            Status::Complete => "complete",
        }
    }
}
impl std::fmt::Display for Status {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Status::Active => "Active",
            Status::Paused => "Paused",
            Status::Complete => "Complete",
        })
    }
}

/// Which instruction a goal prompt carries.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GoalStep {
    Begin,
    Continue,
    /// Re-check the whole objective before claiming completion.
    Audit,
}

#[derive(Clone, Debug)]
pub struct Goal {
    pub objective: String,
    pub status: Status,
    pub turns: u32,
    pub evidence: String,
}

impl Goal {
    pub fn new(objective: &str) -> Result<Self, String> {
        let objective = objective.trim();
        if objective.is_empty() || objective.len() > 8192 || objective.chars().any(char::is_control)
        {
            return Err("Goal must be 1–8192 bytes on one line".into());
        }
        Ok(Self {
            objective: objective.into(),
            status: Status::Active,
            turns: 0,
            evidence: String::new(),
        })
    }
    pub fn prompt(&self, step: GoalStep) -> String {
        let direction = match step {
            GoalStep::Begin => "Begin the objective.",
            GoalStep::Audit => "Audit the entire objective against the work and tests. Fix remaining gaps before claiming completion.",
            GoalStep::Continue => "Continue the objective from the existing vendor conversation. Make concrete progress and verify it.",
        };
        format!("LocalCode active goal: {}\n\n{}\nIf and only if the entire objective is achieved, explain the evidence and put {} alone on the final line. Otherwise describe progress and what remains. Do not claim completion without verification.", self.objective, direction, COMPLETION_MARKER)
    }
    /// Count one completed vendor turn and decide whether another turn is needed.
    /// A turn that ends after a pause still counts and may record completion,
    /// but never continues.
    pub fn finish_turn(&mut self, outcome: &Outcome, assistant: &str) -> bool {
        if self.status == Status::Complete {
            return false;
        }
        self.turns = self.turns.saturating_add(1);
        if *outcome != Outcome::Completed {
            self.status = Status::Paused;
            return false;
        }
        let trimmed = assistant.trim_end();
        if let Some((evidence, _marker)) = trimmed
            .rsplit_once('\n')
            .filter(|(_, marker)| *marker == COMPLETION_MARKER)
        {
            let evidence = evidence.trim();
            if !evidence.is_empty() {
                self.evidence = evidence.chars().take(4096).collect();
                self.status = Status::Complete;
                return false;
            }
        }
        if self.turns >= MAX_GOAL_TURNS {
            self.status = Status::Paused;
            return false;
        }
        self.status == Status::Active
    }
    pub fn summary(&self) -> String {
        format!(
            "Goal: {}\nStatus: {} · turns: {}/{}{}",
            self.objective,
            self.status,
            self.turns,
            MAX_GOAL_TURNS,
            if self.evidence.is_empty() {
                String::new()
            } else {
                format!("\nCompletion evidence: {}", self.evidence)
            }
        )
    }
}

#[derive(Clone)]
pub struct GoalStore {
    path: PathBuf,
}
impl GoalStore {
    pub fn new(directory: &Path, workspace: &Path) -> Self {
        // Stable FNV-1a hash keeps workspace names out of the file name.
        let hash = workspace
            .as_os_str()
            .as_encoded_bytes()
            .iter()
            .fold(0xcbf29ce484222325u64, |hash, byte| {
                (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
            });
        Self {
            path: directory.join(format!("goal-{hash:016x}.json")),
        }
    }
    pub async fn load(&self) -> Result<Option<Goal>, String> {
        let bytes = match tokio::fs::read(&self.path).await {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(format!("Cannot read goal: {e}")),
        };
        // An 8 KiB objective plus 4096 evidence characters can exceed 16 KiB;
        // JSON escaping can expand the evidence to six bytes per character.
        if bytes.len() > 48 * 1024 {
            return Err("Goal file exceeds 48 KiB".into());
        }
        let v: Value =
            serde_json::from_slice(&bytes).map_err(|e| format!("Invalid goal file: {e}"))?;
        let objective = v["objective"].as_str().ok_or("Goal objective missing")?;
        let mut goal = Goal::new(objective)?;
        goal.status = match v["status"].as_str() {
            Some("active" | "paused") => Status::Paused,
            Some("complete") => Status::Complete,
            _ => return Err("Invalid goal status".into()),
        };
        goal.turns = v["turns"]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or("Invalid goal turn count")?;
        goal.evidence = v["evidence"]
            .as_str()
            .unwrap_or("")
            .chars()
            .take(4096)
            .collect();
        Ok(Some(goal))
    }
    pub async fn save(&self, goal: &Goal) -> Result<(), String> {
        let failed = |e: std::io::Error| format!("Cannot save goal: {e}");
        tokio::fs::create_dir_all(self.path.parent().ok_or("Invalid goal path")?)
            .await
            .map_err(failed)?;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|e| format!("Cannot save goal: {e}"))?
            .as_nanos();
        let temp = self
            .path
            .with_extension(format!("{}.{}.tmp", std::process::id(), nonce));
        let mut file = lc_store::create_private(&temp).await.map_err(failed)?;
        let bytes = serde_json::to_vec(&json!({
            "objective": goal.objective,
            "status": goal.status.as_str(),
            "turns": goal.turns,
            "evidence": goal.evidence,
        }))
        .map_err(|e| format!("Cannot save goal: {e}"))?;
        use tokio::io::AsyncWriteExt;
        file.write_all(&bytes).await.map_err(failed)?;
        file.sync_data().await.map_err(failed)?;
        drop(file);
        tokio::fs::rename(&temp, &self.path).await.map_err(failed)
    }
    /// Removes the stored goal; a missing file is already cleared.
    pub async fn clear(&self) -> Result<(), String> {
        match tokio::fs::remove_file(&self.path).await {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(format!("Cannot clear goal: {e}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prompts_name_their_step() {
        let goal = Goal::new("Ship the app").unwrap();
        assert!(goal
            .prompt(GoalStep::Begin)
            .contains("Begin the objective."));
        assert!(goal
            .prompt(GoalStep::Continue)
            .contains("Continue the objective"));
        assert!(goal
            .prompt(GoalStep::Audit)
            .contains("Audit the entire objective"));
        assert!(goal.summary().contains("Status: Active"));
    }
    #[test]
    fn completion_requires_final_marker_and_evidence() {
        let mut goal = Goal::new("Ship the app").unwrap();
        assert!(goal.finish_turn(
            &Outcome::Completed,
            "progress [[LOCALCODE_GOAL_COMPLETE]] quoted here"
        ));
        assert!(goal.finish_turn(&Outcome::Completed, "Progress [[LOCALCODE_GOAL_COMPLETE]]"));
        assert!(!goal.finish_turn(
            &Outcome::Completed,
            "Verified build and tests.\n[[LOCALCODE_GOAL_COMPLETE]]"
        ));
        assert_eq!(goal.status, Status::Complete);
        assert_eq!(goal.turns, 3);
    }
    #[test]
    fn turn_guard_and_failures_pause_without_continuing() {
        let mut goal = Goal::new("Ship the app").unwrap();
        goal.turns = MAX_GOAL_TURNS - 1;
        assert!(!goal.finish_turn(&Outcome::Completed, "Still working"));
        assert_eq!(goal.status, Status::Paused);
        let mut failed = Goal::new("Ship the app").unwrap();
        assert!(!failed.finish_turn(&Outcome::Failed, "Error"));
        assert_eq!(failed.status, Status::Paused);
    }
    #[test]
    fn turns_ending_after_a_pause_are_counted_without_continuing() {
        let mut goal = Goal::new("Ship the app").unwrap();
        goal.status = Status::Paused;
        assert!(!goal.finish_turn(&Outcome::Interrupted, ""));
        assert_eq!((goal.turns, &goal.status), (1, &Status::Paused));
        assert!(!goal.finish_turn(&Outcome::Completed, "Still working"));
        assert_eq!((goal.turns, &goal.status), (2, &Status::Paused));
        assert!(!goal.finish_turn(
            &Outcome::Completed,
            "Verified tests.\n[[LOCALCODE_GOAL_COMPLETE]]"
        ));
        assert_eq!((goal.turns, &goal.status), (3, &Status::Complete));
    }
    #[tokio::test]
    async fn maximum_unicode_goal_and_completion_evidence_survive_restart() {
        let temp = lc_testkit::TempDir::new("lc-goal-unicode");
        let dir = temp.path();
        let store = GoalStore::new(dir, Path::new("/project"));
        let mut goal = Goal::new(&"🦀".repeat(2048)).unwrap();
        let evidence = "🦀".repeat(4096);
        assert!(!goal.finish_turn(
            &Outcome::Completed,
            &format!("{evidence}\n{COMPLETION_MARKER}")
        ));
        store.save(&goal).await.unwrap();
        let restored = store.load().await.unwrap().unwrap();
        assert_eq!(restored.status, Status::Complete);
        assert_eq!(restored.objective, goal.objective);
        assert_eq!(restored.evidence, evidence);
    }
    #[tokio::test]
    async fn state_survives_restart_but_requires_explicit_resume() {
        let temp = lc_testkit::TempDir::new("lc-goal");
        let dir = temp.path();
        let store = GoalStore::new(dir, Path::new("/project"));
        let goal = Goal::new("Ship the app").unwrap();
        store.save(&goal).await.unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                tokio::fs::metadata(&store.path)
                    .await
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        assert_eq!(store.load().await.unwrap().unwrap().status, Status::Paused);
        store.clear().await.unwrap();
        assert!(store.load().await.unwrap().is_none());
    }
}
