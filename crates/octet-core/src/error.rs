//! The errors `octet-core` returns. Their text is what the interface shows, so
//! each message is fixed here and pinned by a test. A message that includes
//! its cause does not also return it as `source()`, so a chain reporter
//! does not print the cause twice.
use std::{io, path::PathBuf};
use thiserror::Error;

/// A goal could not be set, loaded, saved or changed.
#[derive(Debug, Error)]
pub enum GoalError {
    /// The objective is empty, too long, or not one line.
    #[error("Goal must be 1–8192 bytes on one line")]
    InvalidObjective,
    /// The goal file could not be read.
    #[error("Cannot read goal: {0}")]
    Read(io::Error),
    /// The goal file is larger than a goal can be.
    #[error("Goal file exceeds 48 KiB")]
    TooLarge,
    /// The goal file is not valid JSON.
    #[error("Invalid goal file: {0}")]
    Parse(serde_json::Error),
    /// The goal file has no objective.
    #[error("Goal objective missing")]
    MissingObjective,
    /// The goal file's status is not one Octet writes.
    #[error("Invalid goal status")]
    InvalidStatus,
    /// The goal file's turn count is missing or out of range.
    #[error("Invalid goal turn count")]
    InvalidTurns,
    /// The goal path has no parent directory.
    #[error("Invalid goal path")]
    InvalidPath,
    /// The goal could not be written.
    #[error("Cannot save goal: {0}")]
    Save(io::Error),
    /// The goal file could not be removed.
    #[error("Cannot clear goal: {0}")]
    Clear(io::Error),
    /// A goal is already active.
    #[error("Pause or clear the active goal before replacing it")]
    AlreadyActive,
    /// There is no goal to act on.
    #[error("No goal set")]
    NoGoal,
    /// The goal is already complete.
    #[error("Goal is already complete; set a new goal to continue.")]
    Complete,
    /// The goal used all its turns.
    #[error("Goal reached the 200-turn guard. Set a new goal to continue.")]
    TurnGuard,
    /// A new goal was not kept because saving it failed.
    #[error("Goal persistence failed: {0}")]
    Persist(Box<GoalError>),
    /// Resuming failed to save, so the goal stays paused.
    #[error("Goal persistence failed; paused: {0}")]
    PersistPaused(Box<GoalError>),
}

/// A `/model` argument that names no usable model.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum SelectionError {
    /// No model or provider was given.
    #[error("{}", crate::model::usage_empty(&crate::model::vendor_names()))]
    Empty,
    /// The words do not form a model selection.
    #[error("{}", crate::model::usage_syntax(&crate::model::vendor_names()))]
    Syntax,
    /// The demo has no models to select.
    #[error("{}", crate::model::usage_demo(&crate::model::vendor_names()))]
    Demo,
    /// The model name is empty, too long, or has control characters.
    #[error("Model must be a non-empty vendor model name, at most 256 bytes")]
    InvalidModel,
}

/// A session's journal could not be started.
#[derive(Debug, Error)]
pub enum SessionError {
    /// The journal file could not be created.
    #[error("Cannot create transcript journal: {0}")]
    Create(io::Error),
    /// The journal's first record could not be written.
    #[error("Cannot write transcript journal: {0}")]
    Write(io::Error),
}

/// `/export` could not copy the journal.
#[derive(Debug, Error)]
pub enum ExportError {
    /// The journal could not be read.
    #[error("Cannot read journal {}: {error}", path.display())]
    Read {
        /// The file involved.
        path: PathBuf,
        /// What the OS said.
        error: io::Error,
    },
    /// The export file could not be created (it may already exist).
    #[error("Cannot create {}: {error}", path.display())]
    Create {
        /// The file involved.
        path: PathBuf,
        /// What the OS said.
        error: io::Error,
    },
    /// The export file could not be written.
    #[error("Cannot write {}: {error}", path.display())]
    Write {
        /// The file involved.
        path: PathBuf,
        /// What the OS said.
        error: io::Error,
    },
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn messages_keep_their_wording() {
        let io = || std::io::Error::other("disk full");
        let save = || Box::new(GoalError::Save(io()));
        let cases: Vec<(String, &str)> = vec![
            (
                GoalError::InvalidObjective.to_string(),
                "Goal must be 1–8192 bytes on one line",
            ),
            (
                GoalError::Read(io()).to_string(),
                "Cannot read goal: disk full",
            ),
            (GoalError::TooLarge.to_string(), "Goal file exceeds 48 KiB"),
            (
                GoalError::MissingObjective.to_string(),
                "Goal objective missing",
            ),
            (GoalError::InvalidStatus.to_string(), "Invalid goal status"),
            (
                GoalError::InvalidTurns.to_string(),
                "Invalid goal turn count",
            ),
            (GoalError::InvalidPath.to_string(), "Invalid goal path"),
            (
                GoalError::Save(io()).to_string(),
                "Cannot save goal: disk full",
            ),
            (
                GoalError::Clear(io()).to_string(),
                "Cannot clear goal: disk full",
            ),
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
                ExportError::Read {
                    path: "/a".into(),
                    error: io(),
                }
                .to_string(),
                "Cannot read journal /a: disk full",
            ),
            (
                ExportError::Create {
                    path: "/b".into(),
                    error: io(),
                }
                .to_string(),
                "Cannot create /b: disk full",
            ),
            (
                ExportError::Write {
                    path: "/b".into(),
                    error: io(),
                }
                .to_string(),
                "Cannot write /b: disk full",
            ),
        ];
        for (got, want) in cases {
            assert_eq!(got, want);
        }
    }
}
