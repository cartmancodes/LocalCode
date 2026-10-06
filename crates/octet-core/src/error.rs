//! The errors `octet-core` returns. Their text is what the interface shows, so
//! each message is fixed here and pinned by a test.
use std::{io, path::PathBuf};
use thiserror::Error;

/// A goal could not be set, loaded, saved or changed.
#[derive(Debug, Error)]
pub enum GoalError {
    #[error("Goal must be 1–8192 bytes on one line")]
    InvalidObjective,
    #[error("Cannot read goal: {0}")]
    Read(#[source] io::Error),
    #[error("Goal file exceeds 48 KiB")]
    TooLarge,
    #[error("Invalid goal file: {0}")]
    Parse(#[source] serde_json::Error),
    #[error("Goal objective missing")]
    MissingObjective,
    #[error("Invalid goal status")]
    InvalidStatus,
    #[error("Invalid goal turn count")]
    InvalidTurns,
    #[error("Invalid goal path")]
    InvalidPath,
    #[error("Cannot save goal: {0}")]
    Save(#[source] io::Error),
    #[error("Cannot clear goal: {0}")]
    Clear(#[source] io::Error),
    #[error("Pause or clear the active goal before replacing it")]
    AlreadyActive,
    #[error("No goal set")]
    NoGoal,
    #[error("Goal is already complete; set a new goal to continue.")]
    Complete,
    #[error("Goal reached the 200-turn guard. Set a new goal to continue.")]
    TurnGuard,
    /// A new goal was not kept because saving it failed.
    #[error("Goal persistence failed: {0}")]
    Persist(#[source] Box<GoalError>),
    /// Resuming failed to save, so the goal stays paused.
    #[error("Goal persistence failed; paused: {0}")]
    PersistPaused(#[source] Box<GoalError>),
}

/// A `/model` argument that names no usable model.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum SelectionError {
    #[error("Use /model <name>, /model codex <name>, /model claude <name>, or /model <provider> default")]
    Empty,
    #[error("Use /model <name> or /model <codex|claude> <name>")]
    Syntax,
    #[error("Choose a real provider: /model codex or /model claude. Demo has no model.")]
    Demo,
    #[error("Model must be a non-empty vendor model name, at most 256 bytes")]
    InvalidModel,
}

/// A session's journal could not be started.
#[derive(Debug, Error)]
pub enum SessionError {
    #[error("Cannot create transcript journal: {0}")]
    Create(#[source] io::Error),
    #[error("Cannot write transcript journal: {0}")]
    Write(#[source] io::Error),
}

/// `/export` could not copy the journal.
#[derive(Debug, Error)]
pub enum ExportError {
    #[error("Cannot read journal {}: {source}", path.display())]
    Read { path: PathBuf, source: io::Error },
    #[error("Cannot create {}: {source}", path.display())]
    Create { path: PathBuf, source: io::Error },
    #[error("Cannot write {}: {source}", path.display())]
    Write { path: PathBuf, source: io::Error },
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn messages_keep_their_wording() {
        let io = || std::io::Error::other("disk full");
        let save = || Box::new(GoalError::Save(io()));
        let cases: Vec<(String, &str)> = vec![
            (GoalError::InvalidObjective.to_string(), "Goal must be 1–8192 bytes on one line"),
            (GoalError::Read(io()).to_string(), "Cannot read goal: disk full"),
            (GoalError::TooLarge.to_string(), "Goal file exceeds 48 KiB"),
            (GoalError::MissingObjective.to_string(), "Goal objective missing"),
            (GoalError::InvalidStatus.to_string(), "Invalid goal status"),
            (GoalError::InvalidTurns.to_string(), "Invalid goal turn count"),
            (GoalError::InvalidPath.to_string(), "Invalid goal path"),
            (GoalError::Save(io()).to_string(), "Cannot save goal: disk full"),
            (GoalError::Clear(io()).to_string(), "Cannot clear goal: disk full"),
            (
                GoalError::AlreadyActive.to_string(),
                "Pause or clear the active goal before replacing it",
            ),
            (GoalError::NoGoal.to_string(), "No goal set"),
            (
                GoalError::Complete.to_string(),
                "Goal is already complete; set a new goal to continue.",
            ),
            (
                GoalError::TurnGuard.to_string(),
                "Goal reached the 200-turn guard. Set a new goal to continue.",
            ),
            (
                GoalError::Persist(save()).to_string(),
                "Goal persistence failed: Cannot save goal: disk full",
            ),
            (
                GoalError::PersistPaused(save()).to_string(),
                "Goal persistence failed; paused: Cannot save goal: disk full",
            ),
            (
                SelectionError::Empty.to_string(),
                "Use /model <name>, /model codex <name>, /model claude <name>, or /model <provider> default",
            ),
            (
                SelectionError::Syntax.to_string(),
                "Use /model <name> or /model <codex|claude> <name>",
            ),
            (
                SelectionError::Demo.to_string(),
                "Choose a real provider: /model codex or /model claude. Demo has no model.",
            ),
            (
                SelectionError::InvalidModel.to_string(),
                "Model must be a non-empty vendor model name, at most 256 bytes",
            ),
            (
                SessionError::Create(io()).to_string(),
                "Cannot create transcript journal: disk full",
            ),
            (
                SessionError::Write(io()).to_string(),
                "Cannot write transcript journal: disk full",
            ),
            (
                ExportError::Read { path: "/a".into(), source: io() }.to_string(),
                "Cannot read journal /a: disk full",
            ),
            (
                ExportError::Create { path: "/b".into(), source: io() }.to_string(),
                "Cannot create /b: disk full",
            ),
            (
                ExportError::Write { path: "/b".into(), source: io() }.to_string(),
                "Cannot write /b: disk full",
            ),
        ];
        for (got, want) in cases {
            assert_eq!(got, want);
        }
    }
}
