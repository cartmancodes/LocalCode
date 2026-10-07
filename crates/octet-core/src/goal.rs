//! Durable, provider-neutral goal state. A vendor turn is never resumed merely
//! because a goal file exists; the user explicitly resumes after restart.
use crate::{GoalError, Outcome};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// Turns a goal may take before it pauses for the user.
pub const MAX_GOAL_TURNS: u32 = 200;
/// How every goal prompt starts. The fake vendors recognise goal prompts
/// by it (`octet_testkit::scenario::GOAL_PROMPT_PREFIX`).
pub const PROMPT_PREFIX: &str = "Octet active goal: ";
/// The line a vendor ends with when it claims the goal is done.
pub const COMPLETION_MARKER: &str = "[[OCTET_GOAL_COMPLETE]]";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
/// Where a goal stands.
pub enum Status {
    /// Turns continue by themselves.
    Active,
    /// Waiting for `/goal resume`.
    Paused,
    /// Finished, with evidence.
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
    /// The first turn.
    Begin,
    /// A later turn.
    Continue,
    /// Re-check the whole objective before claiming completion.
    Audit,
}

#[derive(Clone, Debug)]
/// A multi-turn objective and its progress. Its fields stay within what a
/// goal file can hold, so every goal saved can be loaded again.
pub struct Goal {
    objective: String,
    status: Status,
    turns: u32,
    evidence: String,
}

/// The most evidence a goal keeps, in characters.
const EVIDENCE_CHARS: usize = 4096;

/// The last `EVIDENCE_CHARS` characters of `text`.
fn evidence_tail(text: &str) -> String {
    let skip = text.chars().count().saturating_sub(EVIDENCE_CHARS);
    text.chars().skip(skip).collect()
}

impl Goal {
    /// A new active goal for `objective`, trimmed.
    ///
    /// ```
    /// use octet_core::goal::{Goal, Status};
    ///
    /// let goal = Goal::new("  Ship the release  ").unwrap();
    /// assert_eq!(goal.objective(), "Ship the release");
    /// assert_eq!(goal.status(), Status::Active);
    /// assert!(Goal::new("two\nlines").is_err());
    /// ```
    /// # Errors
    ///
    /// `InvalidObjective` if it is empty, over 8192 bytes, or has control
    /// characters.
    pub fn new(objective: &str) -> Result<Self, GoalError> {
        let objective = objective.trim();
        if objective.is_empty() || objective.len() > 8192 || objective.chars().any(char::is_control)
        {
            return Err(GoalError::InvalidObjective);
        }
        Ok(Self {
            objective: objective.into(),
            status: Status::Active,
            turns: 0,
            evidence: String::new(),
        })
    }
    /// What to achieve, on one line.
    pub fn objective(&self) -> &str {
        &self.objective
    }
    /// Where it stands.
    pub fn status(&self) -> Status {
        self.status
    }
    /// Vendor turns spent on it.
    pub fn turns(&self) -> u32 {
        self.turns
    }
    /// The vendor's evidence when it claimed completion.
    pub fn evidence(&self) -> &str {
        &self.evidence
    }
    /// Sets the status, to set up a test's state.
    #[cfg(any(test, feature = "testing"))]
    pub fn set_status(&mut self, status: Status) {
        self.status = status;
    }
    /// Sets the turn count, to set up a test's state.
    #[cfg(any(test, feature = "testing"))]
    pub fn set_turns(&mut self, turns: u32) {
        self.turns = turns;
    }
    /// The prompt for one goal turn, asking for the completion marker only
    /// with evidence.
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
            "{PROMPT_PREFIX}{}\n\n{direction}\n{}",
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
                // The text just before the marker is the evidence; a long
                // turn's opening is not.
                self.evidence = evidence_tail(evidence);
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
    /// A short status for `/goal status`.
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

/// FNV-1a's 64-bit offset basis and prime.
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0100_0000_01b3;

#[derive(Clone, Debug)]
/// Where one workspace's goal is saved.
pub struct GoalStore {
    path: PathBuf,
}
impl GoalStore {
    /// The store for `workspace`, under `directory`.
    pub fn new(directory: &Path, workspace: &Path) -> Self {
        // Stable FNV-1a hash keeps workspace names out of the file name.
        let hash = workspace
            .as_os_str()
            .as_encoded_bytes()
            .iter()
            .fold(FNV_OFFSET, |hash, byte| {
                (hash ^ u64::from(*byte)).wrapping_mul(FNV_PRIME)
            });
        Self {
            path: directory.join(format!("goal-{hash:016x}.json")),
        }
    }
    /// The saved goal, if any; a goal that was active loads paused.
    /// # Errors
    ///
    /// Fails if the file cannot be read, is over 48 KiB, or is not a valid
    /// goal.
    pub async fn load(&self) -> Result<Option<Goal>, GoalError> {
        use tokio::io::AsyncReadExt;
        // An 8 KiB objective plus 4096 evidence characters can exceed 16 KiB;
        // JSON escaping can expand the evidence to six bytes per character.
        const LIMIT: u64 = 48 * 1024;
        let file = match tokio::fs::File::open(&self.path).await {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(GoalError::Read(e)),
        };
        // Read one byte past the limit at most: a huge file is refused, not
        // read whole.
        let mut bytes = Vec::new();
        file.take(LIMIT + 1)
            .read_to_end(&mut bytes)
            .await
            .map_err(GoalError::Read)?;
        if bytes.len() as u64 > LIMIT {
            return Err(GoalError::TooLarge);
        }
        let v: Value = serde_json::from_slice(&bytes).map_err(GoalError::Parse)?;
        let objective = v["objective"].as_str().ok_or(GoalError::MissingObjective)?;
        let mut goal = Goal::new(objective)?;
        goal.status = match v["status"].as_str() {
            Some("active" | "paused") => Status::Paused,
            Some("complete") => Status::Complete,
            _ => return Err(GoalError::InvalidStatus),
        };
        goal.turns = v["turns"]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or(GoalError::InvalidTurns)?;
        goal.evidence = evidence_tail(v["evidence"].as_str().unwrap_or(""));
        Ok(Some(goal))
    }
    /// Saves `goal` atomically and durably (write, sync, rename, sync the
    /// directory): a failure leaves the previous file as it was.
    /// # Errors
    ///
    /// `InvalidPath` or `Save` if the file cannot be written.
    pub async fn save(&self, goal: &Goal) -> Result<(), GoalError> {
        let failed = GoalError::Save;
        octet_store::create_private_dir(self.path.parent().ok_or(GoalError::InvalidPath)?)
            .await
            .map_err(failed)?;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|e| GoalError::Save(std::io::Error::other(e)))?
            .as_nanos();
        let temp = self
            .path
            .with_extension(format!("{}.{}.tmp", std::process::id(), nonce));
        let bytes = serde_json::to_vec(&json!({
            "objective": goal.objective,
            "status": goal.status.as_str(),
            "turns": goal.turns,
            "evidence": goal.evidence,
        }))
        .map_err(|e| GoalError::Save(std::io::Error::other(e)))?;
        let written = async {
            use tokio::io::AsyncWriteExt;
            let mut file = octet_store::create_private(&temp).await?;
            // `flush` reports a failed write; `sync_data` alone would not.
            file.write_all(&bytes).await?;
            file.flush().await?;
            file.sync_data().await?;
            drop(file);
            tokio::fs::rename(&temp, &self.path).await?;
            match self.path.parent() {
                Some(directory) => octet_store::sync_directory(directory).await,
                None => Ok(()),
            }
        }
        .await;
        if written.is_err() {
            // A failed save must not leave its temp file behind.
            let _ = tokio::fs::remove_file(&temp).await;
        }
        written.map_err(failed)
    }
    /// Removes the stored goal; a missing file is already cleared.
    ///
    /// # Errors
    ///
    /// `Clear` if the file exists and cannot be removed.
    pub async fn clear(&self) -> Result<(), GoalError> {
        match tokio::fs::remove_file(&self.path).await {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(GoalError::Clear(e)),
        }
    }
}

/// What the UI does after a goal turn ends.
#[derive(Debug, PartialEq, Eq)]
pub enum Next {
    /// Not a goal turn.
    Idle,
    /// Send this continuation.
    Continue {
        /// The continuation prompt the vendor receives.
        prompt: String,
        /// What the transcript shows for it.
        display: String,
    },
    /// The goal stopped working (complete, paused, guard); show this.
    Stopped(String),
}

const OUTPUT_LIMIT: usize = 64 * 1024;

/// A goal, where it is stored, and the vendor turn working on it. Every
/// persistence failure is returned so the UI can say so; none is dropped.
#[derive(Debug, Default)]
pub struct GoalRunner {
    goal: Option<Goal>,
    store: Option<GoalStore>,
    /// The goal turn running, with its streamed text so far; `None` when no
    /// turn works on the goal.
    turn: Option<String>,
}

impl GoalRunner {
    /// The current goal, if any.
    pub fn goal(&self) -> Option<&Goal> {
        self.goal.as_ref()
    }
    /// The current goal, to set up a test's state.
    #[cfg(any(test, feature = "testing"))]
    pub fn goal_mut(&mut self) -> Option<&mut Goal> {
        self.goal.as_mut()
    }
    /// Replaces the goal, unsaved, to set up a test's state.
    #[cfg(any(test, feature = "testing"))]
    pub fn set_goal(&mut self, goal: Option<Goal>) {
        self.goal = goal;
    }
    /// A store is attached, so the goal is saved.
    pub fn is_attached(&self) -> bool {
        self.store.is_some()
    }
    /// Attaches the store and loads its goal; a stored goal is always paused.
    /// On error the store stays attached and no goal is loaded, so the file is
    /// kept until the user clears or replaces it.
    ///
    /// # Errors
    ///
    /// Fails if the stored goal cannot be loaded; the store stays attached.
    pub async fn attach(&mut self, store: GoalStore) -> Result<(), GoalError> {
        let loaded = store.load().await;
        self.store = Some(store);
        self.goal = loaded?;
        Ok(())
    }
    /// A goal is set and active.
    pub fn is_active(&self) -> bool {
        self.goal
            .as_ref()
            .is_some_and(|goal| goal.status == Status::Active)
    }
    /// Saves the goal, or clears the file when there is none.
    /// # Errors
    ///
    /// Fails if the store cannot be written.
    pub async fn save(&self) -> Result<(), GoalError> {
        match (&self.store, &self.goal) {
            (None, _) => Ok(()),
            (Some(store), Some(goal)) => store.save(goal).await,
            (Some(store), None) => store.clear().await,
        }
    }
    async fn save_or_pause(&mut self) -> Result<(), GoalError> {
        let saved = self.save().await;
        if saved.is_err()
            && let Some(goal) = &mut self.goal
        {
            goal.status = Status::Paused;
        }
        saved
    }
    /// Streamed assistant text of a goal turn; the completion marker is read
    /// from the end, so only the last 64 KiB is needed. It is trimmed back to
    /// that once it reaches twice as much, so streaming small deltas does not
    /// move 64 KiB each time.
    pub fn observe_text(&mut self, text: &str) {
        let Some(output) = &mut self.turn else {
            return;
        };
        output.push_str(text);
        if output.len() > 2 * OUTPUT_LIMIT {
            let start = output.ceil_char_boundary(output.len() - OUTPUT_LIMIT);
            output.drain(..start);
        }
    }
    /// The user sent an ordinary prompt; it works on the goal only while active.
    pub fn user_prompt_sent(&mut self) {
        self.turn = self.is_active().then(String::new);
    }
    /// A goal prompt was sent; its turn works on the goal.
    pub fn goal_prompt_sent(&mut self) {
        self.turn = Some(String::new());
    }
    /// A new connection: no turn is running.
    pub fn reset_turn(&mut self) {
        self.turn = None;
    }
    /// What the transcript shows for a goal prompt.
    pub fn prompt_display(&self) -> String {
        self.goal.as_ref().map_or_else(
            || "Goal audit".into(),
            |goal| format!("Goal: {}", goal.objective),
        )
    }
    /// The vendor reported an error. Adapters send it before the failed
    /// terminal event, so the turn is counted here and the later finish ignored.
    ///
    /// # Errors
    ///
    /// Fails if saving fails; the goal is then paused.
    pub async fn turn_failed(&mut self) -> Result<(), GoalError> {
        let Some(output) = self.turn.take() else {
            return Ok(());
        };
        if let Some(goal) = &mut self.goal {
            goal.finish_turn(&Outcome::Failed, &output);
        }
        self.save_or_pause().await
    }
    /// A turn ended: counts it, and says whether to continue.
    /// # Errors
    ///
    /// Fails if saving fails; the goal is then paused.
    pub async fn turn_finished(&mut self, outcome: &Outcome) -> Result<Next, GoalError> {
        let Some(output) = self.turn.take() else {
            return Ok(Next::Idle);
        };
        let Some(goal) = &mut self.goal else {
            return Ok(Next::Idle);
        };
        let more = goal.finish_turn(outcome, &output);
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
    ///
    /// # Errors
    ///
    /// Fails if saving fails; the goal is paused in memory anyway.
    pub async fn pause_running_turn(&mut self) -> Result<(), GoalError> {
        if self.turn.is_none() {
            return Ok(());
        }
        self.pause_and_save().await
    }
    /// Pauses the goal, if any, and saves it; paused in memory either way.
    async fn pause_and_save(&mut self) -> Result<(), GoalError> {
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await
    }
    /// A new connection never continues a goal by itself. True if it paused one.
    ///
    /// # Errors
    ///
    /// Fails if saving fails; the goal is paused in memory anyway.
    pub async fn pause_active(&mut self) -> Result<bool, GoalError> {
        if !self.is_active() {
            return Ok(false);
        }
        self.pause_and_save().await.map(|()| true)
    }
    /// The goal prompt could not be sent.
    ///
    /// # Errors
    ///
    /// Fails if saving fails; the goal is paused in memory anyway.
    pub async fn send_failed(&mut self) -> Result<(), GoalError> {
        self.pause_and_save().await
    }
    /// `/goal <objective>`: the first prompt to send.
    ///
    /// # Errors
    ///
    /// `AlreadyActive` while a goal is active, `InvalidObjective` for a bad
    /// objective, or `Persist` if it cannot be saved (it is then dropped).
    pub async fn start(&mut self, objective: &str) -> Result<String, GoalError> {
        if self.is_active() {
            return Err(GoalError::AlreadyActive);
        }
        let goal = Goal::new(objective)?;
        let prompt = goal.prompt(GoalStep::Begin);
        // On a failed save the previous goal, still in the file, stays.
        let previous = self.goal.replace(goal);
        if let Err(error) = self.save().await {
            self.goal = previous;
            return Err(GoalError::Persist(Box::new(error)));
        }
        Ok(prompt)
    }
    /// `/goal resume` (`Continue`) or `/goal complete` (`Audit`).
    ///
    /// # Errors
    ///
    /// `NoGoal`, `Complete` or `TurnGuard` when there is nothing to resume,
    /// or `PersistPaused` if saving fails.
    pub async fn resume(&mut self, step: GoalStep) -> Result<String, GoalError> {
        let goal = self.goal.as_mut().ok_or(GoalError::NoGoal)?;
        if goal.status == Status::Complete {
            return Err(GoalError::Complete);
        }
        if goal.turns >= MAX_GOAL_TURNS {
            return Err(GoalError::TurnGuard);
        }
        goal.status = Status::Active;
        let prompt = goal.prompt(step);
        self.save_or_pause()
            .await
            .map_err(|error| GoalError::PersistPaused(Box::new(error)))?;
        Ok(prompt)
    }
    /// `/goal pause`: the notice to show.
    ///
    /// # Errors
    ///
    /// Fails if saving fails; the goal is paused in memory anyway.
    pub async fn pause(&mut self) -> Result<String, GoalError> {
        match &self.goal {
            None => Ok(GoalError::NoGoal.to_string()),
            Some(goal) if goal.status == Status::Complete => Ok(GoalError::Complete.to_string()),
            Some(_) => {
                self.pause_and_save().await?;
                Ok("Goal paused. Current vendor turn may finish; no next turn will start.".into())
            }
        }
    }
    /// Drops the goal and its file.
    /// # Errors
    ///
    /// Fails if the file cannot be removed.
    pub async fn clear(&mut self) -> Result<(), GoalError> {
        self.goal = None;
        self.turn = None;
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
        // 150 KB: past the 128 KiB that triggers a trim back to 64 KiB.
        runner.observe_text(&"界".repeat(50_000));
        let output = runner.turn.as_deref().unwrap();
        assert!(output.len() <= 64 * 1024);
        assert!(output.starts_with('界'));
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
    fn goal_prompts_start_with_the_pinned_prefix() {
        // The fake vendors recognise goal prompts by this prefix.
        assert_eq!(PROMPT_PREFIX, octet_testkit::scenario::GOAL_PROMPT_PREFIX);
        let prompt = Goal::new("fixture-goal").unwrap().prompt(GoalStep::Begin);
        assert!(octet_testkit::scenario::is_goal_prompt(
            &prompt,
            "fixture-goal"
        ));
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
        assert!(
            goal.prompt(GoalStep::Begin)
                .contains("Begin the objective.")
        );
        assert!(
            goal.prompt(GoalStep::Continue)
                .contains("Continue the objective")
        );
        assert!(
            goal.prompt(GoalStep::Audit)
                .contains("Audit the entire objective")
        );
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
    #[tokio::test]
    async fn a_failed_goal_save_leaves_no_temp_file() {
        let temp = octet_testkit::TempDir::new("octet-goal-temp");
        let store = GoalStore::new(temp.path(), Path::new("/project"));
        // A directory where the goal file goes makes the final rename fail.
        std::fs::create_dir_all(&store.path).unwrap();
        let goal = Goal::new("Ship the app").unwrap();
        assert!(store.save(&goal).await.is_err());
        let left: Vec<_> = std::fs::read_dir(temp.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .filter(|name| name.to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(left.is_empty(), "{left:?}");
    }
    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }
    #[test]
    fn a_failed_goal_save_keeps_the_old_file() {
        let temp = octet_testkit::TempDir::new("octet-goal-full");
        let dir = temp.path().to_path_buf();
        let store = GoalStore::new(&dir, Path::new("/work"));
        // The first goal fits; a later one with long evidence does not.
        runtime()
            .block_on(store.save(&Goal::new("Ship").unwrap()))
            .unwrap();
        octet_testkit::with_file_limit(
            "goal::tests::a_failed_goal_save_keeps_the_old_file",
            1024,
            || {
                let mut long = Goal::new("Ship more").unwrap();
                long.evidence = "e".repeat(4000);
                let saved = runtime().block_on(store.save(&long));
                assert!(saved.is_err(), "a truncated goal was installed");
                let kept = runtime().block_on(store.load()).unwrap().unwrap();
                assert_eq!(kept.objective, "Ship");
            },
        );
    }
    #[test]
    fn evidence_is_the_text_before_the_marker() {
        let mut goal = Goal::new("Ship").unwrap();
        let reply = format!(
            "{}\nTests pass and the release is tagged.\n{COMPLETION_MARKER}",
            "Working on it. ".repeat(1000)
        );
        goal.finish_turn(&Outcome::Completed, &reply);
        assert_eq!(goal.status, Status::Complete);
        assert!(
            goal.evidence
                .ends_with("Tests pass and the release is tagged."),
            "{}",
            &goal.evidence[..80]
        );
        assert!(goal.evidence.chars().count() <= 4096);
    }
    #[tokio::test]
    async fn a_failed_start_keeps_the_previous_goal() {
        let temp = octet_testkit::TempDir::new("octet-goal-start");
        std::fs::create_dir_all(temp.path()).unwrap();
        // The goal's directory is a file, so saving fails.
        let blocked = temp.path().join("blocked");
        std::fs::write(&blocked, b"").unwrap();
        let mut runner = GoalRunner {
            goal: Some(Goal::new("First").unwrap()),
            store: Some(GoalStore::new(&blocked, Path::new("/work"))),
            ..GoalRunner::default()
        };
        runner.goal.as_mut().unwrap().status = Status::Paused;
        assert!(runner.start("Second").await.is_err());
        assert_eq!(runner.goal().unwrap().objective, "First");
    }
    #[tokio::test]
    async fn an_oversized_goal_file_is_not_read_whole() {
        let temp = octet_testkit::TempDir::new("octet-goal-huge");
        std::fs::create_dir_all(temp.path()).unwrap();
        let store = GoalStore::new(temp.path(), Path::new("/work"));
        // A FIFO that never ends: reading it whole would wait forever.
        assert!(
            std::process::Command::new("mkfifo")
                .arg(&store.path)
                .status()
                .unwrap()
                .success()
        );
        let fifo = store.path.clone();
        std::thread::spawn(move || {
            use std::io::Write;
            let mut writer = std::fs::OpenOptions::new().write(true).open(fifo).unwrap();
            let _ = writer.write_all(&vec![b' '; 64 * 1024]);
            std::thread::sleep(std::time::Duration::from_secs(10));
        });
        let loaded = tokio::time::timeout(std::time::Duration::from_secs(5), store.load())
            .await
            .expect("the load read the whole file");
        assert!(matches!(loaded, Err(GoalError::TooLarge)), "{loaded:?}");
    }
    #[test]
    fn observe_text_trims_amortized() {
        let mut runner = runner_with_goal();
        runner.goal_prompt_sent();
        let chunk = "x".repeat(1024);
        for _ in 0..65 {
            runner.observe_text(&chunk);
        }
        // Past the limit, but not yet twice it: nothing is moved yet.
        assert_eq!(runner.turn.as_ref().unwrap().len(), 65 * 1024);
        for _ in 0..64 {
            runner.observe_text(&chunk);
        }
        runner.observe_text("\nThe end");
        let kept = runner.turn.as_ref().unwrap();
        assert!(kept.len() <= 2 * OUTPUT_LIMIT, "{}", kept.len());
        assert!(kept.len() >= OUTPUT_LIMIT);
        assert!(kept.ends_with("\nThe end"));
    }
}
