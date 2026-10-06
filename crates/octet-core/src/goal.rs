//! Durable, provider-neutral goal state. A vendor turn is never resumed merely
//! because a goal file exists; the user explicitly resumes after restart.
use crate::Outcome;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub const MAX_GOAL_TURNS: u32 = 200;
pub const COMPLETION_MARKER: &str = "[[OCTET_GOAL_COMPLETE]]";

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
            GoalStep::Audit => {
                "Audit the entire objective against the work and tests. Fix remaining gaps before claiming completion."
            }
            GoalStep::Continue => {
                "Continue the objective from the existing vendor conversation. Make concrete progress and verify it."
            }
        };
        format!(
            "Octet active goal: {}\n\n{direction}\n{}",
            self.objective,
            format_args!(
                "If and only if the entire objective is achieved, explain the evidence and put {COMPLETION_MARKER} \
                 alone on the final line. Otherwise describe progress and what remains. \
                 Do not claim completion without verification."
            )
        )
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
        let mut file = octet_store::create_private(&temp).await.map_err(failed)?;
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

/// What the UI does after a goal turn ends.
#[derive(Debug, PartialEq, Eq)]
pub enum Next {
    /// Not a goal turn.
    Idle,
    /// Send this continuation.
    Continue { prompt: String, display: String },
    /// The goal stopped working (complete, paused, guard); show this.
    Stopped(String),
}

const OUTPUT_LIMIT: usize = 64 * 1024;

/// A goal, where it is stored, and the vendor turn working on it. Every
/// persistence failure is returned so the UI can say so; none is dropped.
#[derive(Default)]
pub struct GoalRunner {
    pub goal: Option<Goal>,
    store: Option<GoalStore>,
    running: bool,
    output: String,
}

impl GoalRunner {
    pub fn is_attached(&self) -> bool {
        self.store.is_some()
    }
    /// Attaches the store and loads its goal; a stored goal is always paused.
    /// On error the store stays attached and no goal is loaded, so the file is
    /// kept until the user clears or replaces it.
    pub async fn attach(&mut self, store: GoalStore) -> Result<(), String> {
        let loaded = store.load().await;
        self.store = Some(store);
        self.goal = loaded?;
        Ok(())
    }
    pub fn is_active(&self) -> bool {
        self.goal
            .as_ref()
            .is_some_and(|goal| goal.status == Status::Active)
    }
    pub async fn save(&self) -> Result<(), String> {
        match (&self.store, &self.goal) {
            (None, _) => Ok(()),
            (Some(store), Some(goal)) => store.save(goal).await,
            (Some(store), None) => store.clear().await,
        }
    }
    async fn save_or_pause(&mut self) -> Result<(), String> {
        let saved = self.save().await;
        if saved.is_err() {
            if let Some(goal) = &mut self.goal {
                goal.status = Status::Paused;
            }
        }
        saved
    }
    /// Streamed assistant text of a goal turn; the completion marker is read
    /// from the end, so only the last 64 KiB is kept.
    pub fn observe_text(&mut self, text: &str) {
        if !self.running {
            return;
        }
        self.output.push_str(text);
        if self.output.len() > OUTPUT_LIMIT {
            let start = self
                .output
                .ceil_char_boundary(self.output.len() - OUTPUT_LIMIT);
            self.output.drain(..start);
        }
    }
    /// The user sent an ordinary prompt; it works on the goal only while active.
    pub fn user_prompt_sent(&mut self) {
        self.running = self.is_active();
        self.output.clear();
    }
    pub fn goal_prompt_sent(&mut self) {
        self.running = true;
        self.output.clear();
    }
    /// A new connection: no turn is running.
    pub fn reset_turn(&mut self) {
        self.running = false;
        self.output.clear();
    }
    pub fn prompt_display(&self) -> String {
        self.goal
            .as_ref()
            .map(|goal| format!("Goal: {}", goal.objective))
            .unwrap_or_else(|| "Goal audit".into())
    }
    /// The vendor reported an error. Adapters send it before the failed
    /// terminal event, so the turn is counted here and the later finish ignored.
    pub async fn turn_failed(&mut self) -> Result<(), String> {
        if !self.running {
            return Ok(());
        }
        self.running = false;
        if let Some(goal) = &mut self.goal {
            goal.finish_turn(&Outcome::Failed, &self.output);
        }
        self.save_or_pause().await
    }
    pub async fn turn_finished(&mut self, outcome: &Outcome) -> Result<Next, String> {
        if !self.running {
            return Ok(Next::Idle);
        }
        self.running = false;
        let Some(goal) = &mut self.goal else {
            return Ok(Next::Idle);
        };
        let more = goal.finish_turn(outcome, &self.output);
        self.save_or_pause().await?;
        let Some(goal) = &self.goal else {
            return Ok(Next::Idle);
        };
        Ok(if more {
            Next::Continue {
                prompt: goal.prompt(GoalStep::Continue),
                display: format!("Goal continuation · turn {}", goal.turns + 1),
            }
        } else {
            Next::Stopped(goal.summary())
        })
    }
    /// Esc/Ctrl+C during a goal turn: the goal must not continue by itself.
    pub async fn pause_running_turn(&mut self) -> Result<(), String> {
        if !self.running {
            return Ok(());
        }
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await
    }
    /// A new connection never continues a goal by itself. True if it paused one.
    pub async fn pause_active(&mut self) -> Result<bool, String> {
        if !self.is_active() {
            return Ok(false);
        }
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await.map(|()| true)
    }
    /// The goal prompt could not be sent.
    pub async fn send_failed(&mut self) -> Result<(), String> {
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await
    }
    /// `/goal <objective>`: the first prompt to send.
    pub async fn start(&mut self, objective: &str) -> Result<String, String> {
        if self.is_active() {
            return Err("Pause or clear the active goal before replacing it".into());
        }
        let goal = Goal::new(objective)?;
        let prompt = goal.prompt(GoalStep::Begin);
        self.goal = Some(goal);
        if let Err(error) = self.save().await {
            self.goal = None;
            return Err(format!("Goal persistence failed: {error}"));
        }
        Ok(prompt)
    }
    /// `/goal resume` (`Continue`) or `/goal complete` (`Audit`).
    pub async fn resume(&mut self, step: GoalStep) -> Result<String, String> {
        let goal = self.goal.as_mut().ok_or("No goal set")?;
        if goal.status == Status::Complete {
            return Err("Goal is already complete; set a new goal to continue.".into());
        }
        if goal.turns >= MAX_GOAL_TURNS {
            return Err("Goal reached the 200-turn guard. Set a new goal to continue.".into());
        }
        goal.status = Status::Active;
        let prompt = goal.prompt(step);
        self.save_or_pause()
            .await
            .map_err(|error| format!("Goal persistence failed; paused: {error}"))?;
        Ok(prompt)
    }
    /// `/goal pause`: the notice to show.
    pub async fn pause(&mut self) -> Result<&'static str, String> {
        match &mut self.goal {
            None => Ok("No goal set"),
            Some(goal) if goal.status == Status::Complete => {
                Ok("Goal is already complete; set a new goal to continue.")
            }
            Some(goal) => {
                goal.status = Status::Paused;
                self.save().await?;
                Ok("Goal paused. Current vendor turn may finish; no next turn will start.")
            }
        }
    }
    pub async fn clear(&mut self) -> Result<(), String> {
        self.goal = None;
        self.running = false;
        self.save().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn runner_with_goal() -> GoalRunner {
        GoalRunner {
            goal: Some(Goal::new("Ship the app").unwrap()),
            ..GoalRunner::default()
        }
    }
    #[tokio::test]
    async fn failed_turn_counts_once_and_the_late_finish_is_ignored() {
        let mut runner = runner_with_goal();
        runner.goal_prompt_sent();
        runner.turn_failed().await.unwrap();
        assert_eq!(
            runner.turn_finished(&Outcome::Failed).await.unwrap(),
            Next::Idle
        );
        let goal = runner.goal.as_ref().unwrap();
        assert_eq!((goal.turns, goal.status), (1, Status::Paused));
    }
    #[tokio::test]
    async fn completed_turn_continues_until_the_marker() {
        let mut runner = runner_with_goal();
        runner.goal_prompt_sent();
        runner.observe_text("Still working");
        let Next::Continue { prompt, display } =
            runner.turn_finished(&Outcome::Completed).await.unwrap()
        else {
            panic!("expected a continuation");
        };
        assert!(prompt.contains("Continue the objective"));
        assert_eq!(display, "Goal continuation · turn 2");
        runner.goal_prompt_sent();
        runner.observe_text("Verified tests.\n[[OCTET_GOAL_COMPLETE]]");
        assert!(matches!(
            runner.turn_finished(&Outcome::Completed).await.unwrap(),
            Next::Stopped(summary) if summary.contains("Status: Complete")
        ));
    }
    #[test]
    fn goal_output_keeps_the_last_64_kib_on_a_char_boundary() {
        let mut runner = runner_with_goal();
        runner.goal_prompt_sent();
        runner.observe_text(&"界".repeat(30_000));
        assert!(runner.output.len() <= 64 * 1024);
        assert!(runner.output.starts_with('界'));
    }
    #[tokio::test]
    async fn pause_active_reports_persistence_failure() {
        let dir = octet_testkit::TempDir::new("octet-goal-unwritable");
        // A file where the store expects its directory makes every save fail.
        std::fs::write(dir.path(), b"not a directory").unwrap();
        let mut runner = GoalRunner::default();
        runner
            .attach(GoalStore::new(dir.path(), Path::new("/project")))
            .await
            .unwrap_err();
        runner.goal = Some(Goal::new("Ship the app").unwrap());
        assert!(runner.pause_active().await.is_err());
        assert_eq!(runner.goal.as_ref().unwrap().status, Status::Paused);
    }
    #[tokio::test]
    async fn cancelling_pauses_only_a_running_goal_turn() {
        let mut runner = runner_with_goal();
        runner.pause_running_turn().await.unwrap();
        assert!(runner.is_active());
        runner.goal_prompt_sent();
        runner.pause_running_turn().await.unwrap();
        assert!(!runner.is_active());
    }
    #[test]
    fn the_goal_prompt_keeps_its_wording() {
        assert_eq!(
            Goal::new("Ship").unwrap().prompt(GoalStep::Begin),
            "Octet active goal: Ship\n\nBegin the objective.\nIf and only if the entire objective is achieved, \
             explain the evidence and put [[OCTET_GOAL_COMPLETE]] alone on the final line. Otherwise describe \
             progress and what remains. Do not claim completion without verification."
        );
    }
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
            "progress [[OCTET_GOAL_COMPLETE]] quoted here"
        ));
        assert!(goal.finish_turn(&Outcome::Completed, "Progress [[OCTET_GOAL_COMPLETE]]"));
        assert!(!goal.finish_turn(
            &Outcome::Completed,
            "Verified build and tests.\n[[OCTET_GOAL_COMPLETE]]"
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
            "Verified tests.\n[[OCTET_GOAL_COMPLETE]]"
        ));
        assert_eq!((goal.turns, &goal.status), (3, &Status::Complete));
    }
    #[tokio::test]
    async fn maximum_unicode_goal_and_completion_evidence_survive_restart() {
        let temp = octet_testkit::TempDir::new("octet-goal-unicode");
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
        let temp = octet_testkit::TempDir::new("octet-goal");
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
