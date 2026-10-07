//! Work that reads the disk or runs programs, kept off the event loop so
//! vendor events and approvals keep flowing. One job runs at a time.
use crate::{app::App, remote};
use octet_core::{ExportError, RecentSession};
use std::{future::Future, path::PathBuf, pin::Pin};

/// What a finished job reports.
pub(crate) enum Done {
    Remote(remote::Checks),
    Sessions(Vec<RecentSession>),
    Exported {
        path: PathBuf,
        result: Result<(), ExportError>,
    },
}

/// A job a command asked for: its name for the status line, and its work.
pub(crate) struct Job {
    pub(crate) label: &'static str,
    pub(crate) work: Pin<Box<dyn Future<Output = Done> + Send>>,
}

impl Job {
    /// The `/remote-control` checks.
    pub(crate) fn remote() -> Self {
        Self {
            label: "Checking phone access…",
            work: Box::pin(async { Done::Remote(remote::probe().await) }),
        }
    }
    /// The journals beside this one, for `/sessions`.
    pub(crate) fn sessions(directory: PathBuf, workspace: PathBuf, limit: usize) -> Self {
        Self {
            label: "Reading recent sessions…",
            work: Box::pin(async move {
                Done::Sessions(octet_core::recent_sessions(&directory, &workspace, limit).await)
            }),
        }
    }
    /// A copy of the journal, for `/export`.
    pub(crate) fn export(journal: PathBuf, path: PathBuf) -> Self {
        Self {
            label: "Exporting the journal…",
            work: Box::pin(async move {
                let result = octet_core::export_journal(&journal, &path).await;
                Done::Exported { path, result }
            }),
        }
    }
}

/// Shows what a finished job found.
pub(crate) fn apply(app: &mut App, done: Done) {
    match done {
        Done::Remote(checks) => {
            let report = remote::report(&checks);
            app.note(report.text.as_str());
            // The status line, one row high, gets the count of problems.
            app.status_line = remote::summary(&report);
        }
        Done::Sessions(listed) => crate::commands::show_sessions(app, listed),
        Done::Exported { path, result } => match result {
            Ok(()) => app.note(format!("Exported journal to {}", path.display())),
            Err(error) => app.error(format!("Export failed: {error}")),
        },
    }
}
