//! The command line, parsed once into `Args`. Usage errors (exit 2) are kept
//! apart from failures while running (exit 1), so a script driving
//! `--print` can tell a bad flag from a failed turn.
use octet_core::{Config, Engine, Mode};
use std::{ffi::OsString, path::PathBuf, time::Duration};
use thiserror::Error;

/// Why `octet` stopped with an error.
#[derive(Debug, Error, PartialEq, Eq)]
pub(crate) enum CliError {
    /// The command line is wrong.
    #[error("{0}")]
    Usage(String),
    /// Running failed.
    #[error("{0}")]
    Run(String),
}

impl CliError {
    /// The process's exit code: 2 for usage, as shells expect, 1 otherwise.
    pub(crate) fn code(&self) -> i32 {
        match self {
            CliError::Usage(_) => 2,
            CliError::Run(_) => 1,
        }
    }
}

/// What the command line asks for.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Parsed {
    Help,
    Version,
    Run(Box<Args>),
}

/// What to run.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Run {
    /// The terminal interface.
    Interface,
    /// One prompt; the reply (or, with `json`, every event) on stdout.
    Print { prompt: Prompt, json: bool },
    /// JSON-line commands in, events out.
    Rpc,
}

/// Where a `--print` prompt comes from.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Prompt {
    Text(String),
    /// `-`: all of stdin.
    Stdin,
}

/// The options, checked. The workspace and journal directory, which need
/// the filesystem, are resolved by the caller.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Args {
    pub(crate) engine: Engine,
    pub(crate) binary: Option<PathBuf>,
    pub(crate) cwd: Option<PathBuf>,
    pub(crate) model: Option<String>,
    pub(crate) resume: Option<String>,
    pub(crate) journal_dir: Option<PathBuf>,
    pub(crate) mode: Mode,
    pub(crate) approval_timeout: Duration,
    pub(crate) effort: Option<String>,
    pub(crate) run: Run,
}

impl Args {
    /// The session's configuration, in workspace `cwd`.
    pub(crate) fn config(&self, cwd: PathBuf) -> Config {
        let binary = self
            .binary
            .clone()
            .unwrap_or_else(|| PathBuf::from(self.engine.provider().default_binary));
        Config {
            model: self.model.clone(),
            resume: self.resume.clone(),
            mode: self.mode,
            approval_timeout: self.approval_timeout,
            effort: self.effort.clone(),
            ..Config::new(self.engine, binary, cwd)
        }
    }
}

fn usage(message: impl Into<String>) -> CliError {
    CliError::Usage(message.into())
}

/// The next argument as `name`'s value. A value never starts with "--", so
/// a forgotten value cannot swallow the next option.
fn value(args: &mut impl Iterator<Item = OsString>, name: &str) -> Result<OsString, CliError> {
    args.next()
        .filter(|value| !value.to_string_lossy().starts_with("--"))
        .ok_or_else(|| usage(format!("{name} requires a value. Use --help.")))
}

/// `name`'s value as text.
fn text(args: &mut impl Iterator<Item = OsString>, name: &str) -> Result<String, CliError> {
    value(args, name)?
        .into_string()
        .map_err(|_| usage(format!("{name} must be UTF-8 text")))
}

/// Parses the arguments after the program name.
///
/// # Errors
///
/// A usage error naming the first problem: an unknown option, a missing or
/// invalid value, or options that cannot be combined.
pub(crate) fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<Parsed, CliError> {
    let mut args = args.into_iter();
    let mut engine = "codex".to_owned();
    let mut binary = None;
    let mut cwd = None;
    let mut model = None;
    let mut resume = None;
    let mut journal_dir = None;
    let mut mode = Mode::Ask;
    let mut approval_timeout = octet_core::DEFAULT_APPROVAL_TIMEOUT;
    let mut effort = None;
    let mut print = None;
    let mut output = None;
    let mut rpc = false;
    while let Some(arg) = args.next() {
        let arg = arg.into_string().map_err(|arg| {
            usage(format!(
                "Unknown option {}. Use --help.",
                arg.to_string_lossy()
            ))
        })?;
        let name = if arg == "-p" { "--print" } else { arg.as_str() };
        match name {
            "--help" | "-h" => return Ok(Parsed::Help),
            "--version" => return Ok(Parsed::Version),
            "--rpc" => rpc = true,
            "--engine" => engine = text(&mut args, name)?,
            "--binary" => binary = Some(PathBuf::from(value(&mut args, name)?)),
            "--cwd" => cwd = Some(PathBuf::from(value(&mut args, name)?)),
            "--model" => model = Some(text(&mut args, name)?),
            "--resume" => resume = Some(text(&mut args, name)?),
            "--journal-dir" => journal_dir = Some(PathBuf::from(value(&mut args, name)?)),
            "--mode" => {
                let value = text(&mut args, name)?;
                mode = Mode::parse(&value).ok_or_else(|| {
                    usage(format!(
                        "Unknown mode {value}. Use ask, accept-edits, auto or full-access."
                    ))
                })?;
            }
            "--approval-timeout" => {
                approval_timeout = text(&mut args, name)?
                    .parse::<u64>()
                    .ok()
                    .filter(|seconds| (10..=3600).contains(seconds))
                    .map(Duration::from_secs)
                    .ok_or_else(|| {
                        usage("Approval timeout must be a whole number of seconds from 10 to 3600")
                    })?;
            }
            "--effort" => {
                let value = text(&mut args, name)?;
                if !octet_core::valid_effort(&value) {
                    return Err(usage("Effort must be one word, at most 64 bytes"));
                }
                effort = Some(value);
            }
            "--print" => print = Some(text(&mut args, name)?),
            "--output" => {
                output = Some(match text(&mut args, name)?.as_str() {
                    "text" => false,
                    "json" => true,
                    _ => return Err(usage("Output must be text or json")),
                });
            }
            other => return Err(usage(format!("Unknown option {other}. Use --help."))),
        }
    }
    let engine = Engine::parse(&engine).ok_or_else(|| {
        usage(format!(
            "Engine must be {}",
            octet_core::model::engine_choices()
        ))
    })?;
    if let Some(level) = &effort {
        engine.check_effort(level).map_err(usage)?;
    }
    let run = match (print, rpc) {
        (Some(_), true) => return Err(usage("--print and --rpc cannot be combined")),
        (Some(prompt), false) => Run::Print {
            prompt: if prompt == "-" {
                Prompt::Stdin
            } else {
                check_prompt(&prompt)?;
                Prompt::Text(prompt)
            },
            json: output.unwrap_or(false),
        },
        (None, _) if output.is_some() => return Err(usage("--output applies to --print")),
        (None, true) => Run::Rpc,
        (None, false) => Run::Interface,
    };
    Ok(Parsed::Run(Box::new(Args {
        engine,
        binary,
        cwd,
        model,
        resume,
        journal_dir,
        mode,
        approval_timeout,
        effort,
        run,
    })))
}

/// Refuses a prompt the session would refuse, before one opens.
///
/// # Errors
///
/// A usage error for an empty prompt or one over the prompt limit.
pub(crate) fn check_prompt(prompt: &str) -> Result<(), CliError> {
    if prompt.trim().is_empty() {
        return Err(usage("The prompt is empty"));
    }
    check_prompt_size(prompt.len())
}

/// Refuses a prompt of `bytes` over the prompt limit.
///
/// # Errors
///
/// A usage error naming the limit.
pub(crate) fn check_prompt_size(bytes: usize) -> Result<(), CliError> {
    if bytes > octet_core::PROMPT_LIMIT {
        return Err(usage(format!(
            "The prompt is over {} KiB",
            octet_core::PROMPT_LIMIT / 1024
        )));
    }
    Ok(())
}

/// The workspace: `path` as an existing directory with a UTF-8 path, which
/// the journal records.
///
/// # Errors
///
/// A usage error for a path that is not UTF-8, cannot be resolved, or is not
/// a directory.
pub(crate) fn workspace(path: &std::path::Path) -> Result<PathBuf, CliError> {
    let utf8 = |path: &std::path::Path| {
        path.to_str()
            .map(|_| ())
            .ok_or_else(|| usage("Workspace path must be UTF-8"))
    };
    utf8(path)?;
    let path = path
        .canonicalize()
        .map_err(|e| usage(format!("Invalid workspace: {e}")))?;
    utf8(&path)?;
    if !path.is_dir() {
        return Err(usage("Workspace must be a directory"));
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Parsed, CliError> {
        parse_args(args.iter().map(OsString::from))
    }
    fn args(list: &[&str]) -> Args {
        match parse(list) {
            Ok(Parsed::Run(args)) => *args,
            other => panic!("{other:?}"),
        }
    }
    fn error(list: &[&str]) -> String {
        match parse(list) {
            Err(CliError::Usage(message)) => message,
            other => panic!("{list:?}: {other:?}"),
        }
    }

    #[test]
    fn defaults_are_codex_in_ask_mode_with_the_interface() {
        let parsed = args(&[]);
        assert_eq!(parsed.engine, Engine::CODEX);
        assert_eq!(parsed.mode, Mode::Ask);
        assert_eq!(
            parsed.approval_timeout,
            octet_core::DEFAULT_APPROVAL_TIMEOUT
        );
        assert_eq!(parsed.run, Run::Interface);
    }

    #[test]
    fn help_and_version_win_wherever_they_are() {
        assert_eq!(parse(&["--engine", "demo", "-h"]), Ok(Parsed::Help));
        assert_eq!(parse(&["--version"]), Ok(Parsed::Version));
    }

    #[test]
    fn every_option_reaches_the_configuration() {
        let parsed = args(&[
            "--engine",
            "claude",
            "--binary",
            "/bin/claude",
            "--model",
            "opus",
            "--resume",
            "s-1",
            "--mode",
            "auto",
            "--approval-timeout",
            "10",
            "--effort",
            "max",
        ]);
        let config = parsed.config(PathBuf::from("/work"));
        assert_eq!(config.engine, Engine::CLAUDE);
        assert_eq!(config.binary, PathBuf::from("/bin/claude"));
        assert_eq!(config.model.as_deref(), Some("opus"));
        assert_eq!(config.resume.as_deref(), Some("s-1"));
        assert_eq!(config.mode, Mode::Auto);
        assert_eq!(config.approval_timeout, Duration::from_secs(10));
        assert_eq!(config.effort.as_deref(), Some("max"));
        assert!(!config.fork);
    }

    #[test]
    fn the_binary_defaults_to_the_providers() {
        let config = args(&["--engine", "claude"]).config(PathBuf::from("/w"));
        assert_eq!(config.binary, PathBuf::from("claude"));
    }

    #[test]
    fn print_and_rpc_runs() {
        assert_eq!(
            args(&["-p", "hi", "--output", "json"]).run,
            Run::Print {
                prompt: Prompt::Text("hi".into()),
                json: true
            }
        );
        assert_eq!(
            args(&["--print", "-"]).run,
            Run::Print {
                prompt: Prompt::Stdin,
                json: false
            }
        );
        assert_eq!(args(&["--rpc"]).run, Run::Rpc);
    }

    #[test]
    fn every_usage_error_names_the_problem() {
        let engine_error = format!("Engine must be {}", octet_core::model::engine_choices());
        let long = "x".repeat(octet_core::PROMPT_LIMIT + 1);
        for (list, message) in [
            (&["--bogus"][..], "Unknown option --bogus. Use --help."),
            (&["stray"][..], "Unknown option stray. Use --help."),
            (&["--cwd"][..], "--cwd requires a value. Use --help."),
            (
                &["--cwd", "--journal-dir", "/tmp"][..],
                "--cwd requires a value. Use --help.",
            ),
            (&["--engine", "gemini"][..], &engine_error),
            (
                &["--mode", "bogus"][..],
                "Unknown mode bogus. Use ask, accept-edits, auto or full-access.",
            ),
            (
                &["--approval-timeout", "5"][..],
                "Approval timeout must be a whole number of seconds from 10 to 3600",
            ),
            (
                &["--approval-timeout", "abc"][..],
                "Approval timeout must be a whole number of seconds from 10 to 3600",
            ),
            (
                &["--approval-timeout", "3601"][..],
                "Approval timeout must be a whole number of seconds from 10 to 3600",
            ),
            (
                &["--effort", ""][..],
                "Effort must be one word, at most 64 bytes",
            ),
            (
                &["--effort", "two words"][..],
                "Effort must be one word, at most 64 bytes",
            ),
            (
                &["--engine", "claude", "--effort", "bogus"][..],
                "Claude takes effort low, medium, high, xhigh or max",
            ),
            (
                &["--output", "xml", "-p", "x"][..],
                "Output must be text or json",
            ),
            (&["--output", "json"][..], "--output applies to --print"),
            (
                &["-p", "x", "--rpc"][..],
                "--print and --rpc cannot be combined",
            ),
            (&["-p", "  "][..], "The prompt is empty"),
            (&["-p", &long][..], "The prompt is over 64 KiB"),
        ] {
            assert_eq!(error(list), message, "{list:?}");
        }
    }

    #[test]
    fn usage_errors_exit_2_and_failures_1() {
        assert_eq!(usage("x").code(), 2);
        assert_eq!(CliError::Run("x".into()).code(), 1);
    }

    #[test]
    fn a_non_utf8_cwd_is_refused() {
        use std::os::unix::ffi::OsStringExt;
        let bad = OsString::from_vec(b"/tmp/octet-\xff".to_vec());
        let parsed = parse_args([OsString::from("--cwd"), bad]).unwrap();
        let Parsed::Run(args) = parsed else {
            panic!("{parsed:?}")
        };
        let error = workspace(&args.cwd.unwrap()).unwrap_err();
        assert_eq!(error, usage("Workspace path must be UTF-8"));
    }

    #[test]
    fn a_workspace_must_be_an_existing_directory() {
        assert!(matches!(
            workspace(std::path::Path::new("/nonexistent/octet")),
            Err(CliError::Usage(message)) if message.starts_with("Invalid workspace")
        ));
        assert_eq!(
            workspace(std::path::Path::new("/etc/hosts")),
            Err(usage("Workspace must be a directory"))
        );
    }
}
