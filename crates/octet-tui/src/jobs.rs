//! Work that reads the disk or runs programs, kept off the event loop so
//! vendor events and approvals keep flowing. One job runs at a time, and a
//! session that ends waits for it, so an export is never cut short.
use crate::{app::App, remote};
use octet_core::{ExportError, RecentSession};
use std::{future::Future, path::PathBuf, pin::Pin, time::Duration};
use tokio::task::JoinHandle;

/// How long an ending session waits for its job.
pub(crate) const GRACE: Duration = Duration::from_secs(5);

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

/// The job running off the loop.
pub(crate) struct Running {
    pub(crate) label: &'static str,
    task: JoinHandle<Done>,
}

impl Running {
    pub(crate) fn spawn(job: Job) -> Self {
        Self {
            label: job.label,
            task: tokio::spawn(job.work),
        }
    }
    /// Waits for the job to end.
    pub(crate) async fn wait(&mut self) -> Ended {
        match (&mut self.task).await {
            Ok(done) => Ended::Done(done),
            Err(error) => Ended::Failed(error.to_string()),
        }
    }
}

/// How a job ended.
pub(crate) enum Ended {
    Done(Done),
    /// The task panicked or was cancelled.
    Failed(String),
    /// It outlived its session's grace period and was stopped.
    Stopped(&'static str),
}

/// Waits up to `grace` for `running` to end, stopping it after that.
pub(crate) async fn finish(running: Option<Running>, grace: Duration) -> Option<Ended> {
    let mut running = running?;
    Some(match tokio::time::timeout(grace, running.wait()).await {
        Ok(ended) => ended,
        Err(_) => {
            running.task.abort();
            Ended::Stopped(running.label)
        }
    })
}

/// Shows how a job ended.
pub(crate) fn apply(app: &mut App, ended: Ended) {
    let done = match ended {
        Ended::Done(done) => done,
        Ended::Failed(error) => return app.error(format!("A background task failed: {error}")),
        Ended::Stopped(label) => {
            return app.error(format!("{label} did not finish in time and was stopped"))
        }
    };
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
