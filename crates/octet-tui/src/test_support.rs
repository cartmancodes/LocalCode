//! Helpers shared by the interface's tests.
use crate::app::App;

/// An interface for `engine`, in /tmp, before any connection.
pub(crate) fn app_for(engine: octet_core::Engine) -> App {
    let config = octet_core::Config::new(engine, engine.as_str(), "/tmp");
    App::new(&config, "journal".into())
}

/// A real `!` command that runs until dropped (which kills it), standing in
/// for one the user started.
pub(crate) fn running_shell(attach: bool) -> crate::shell::Running {
    crate::shell::Running::spawn("sleep 30".into(), ".".into(), attach)
}
