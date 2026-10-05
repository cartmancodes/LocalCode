use octet_core::Config;
use std::{io::IsTerminal, path::PathBuf};
const HELP:&str="Octet — terminal coding workspace (Rust preview)\n\nUsage: octet [--engine codex|claude|demo] [--cwd PATH]\n                 [--model MODEL] [--mode MODE] [--resume VENDOR_SESSION_ID]\n                 [--binary PATH] [--journal-dir PATH]\n                 [--approval-timeout SECONDS]\n\nDefaults: Codex, current directory. Vendor CLI installation and login required.\nModes: ask (default) · accept-edits · auto (vendor auto-review) · full-access\nUse --engine demo for an offline interactive preview.\n\nKeys: Enter send · Alt+Enter / Ctrl+J newline · Esc cancel · Ctrl+C twice quit\n      PageUp/PageDown scroll · Ctrl+P commands · F1 help · Shift+Tab mode\n      @ mention a file · Tab complete · !cmd run (attach) · !!cmd run only\n      Ctrl+G edit in $EDITOR · Ctrl+X copy the last reply\n\nCommands: /model, /mode, /goal, /session, /new, /reconnect, /export,\n          /copy, /remote-control (check phone access)\nJournals are separate JSONL files; see --journal-dir.\nFleet, full plugin/hook parity, v3 browsing and legacy RPC compatibility remain pending.\n";
#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("octet: {error}");
        std::process::exit(1);
    }
}
async fn run() -> Result<(), String> {
    let mut args = std::env::args().skip(1);
    let mut engine = "codex".to_owned();
    let mut binary = None;
    let mut model = None;
    let mut resume = None;
    let mut cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    let mut directory = None;
    let mut mode = octet_core::Mode::Ask;
    let mut approval_timeout = octet_core::DEFAULT_APPROVAL_TIMEOUT;
    while let Some(arg) = args.next() {
        if arg == "--help" || arg == "-h" {
            print!("{HELP}");
            return Ok(());
        }
        if arg == "--version" {
            println!("octet {} (Rust preview)", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        let value = args
            .next()
            .ok_or_else(|| format!("{arg} requires a value. Use --help."))?;
        match arg.as_str() {
            "--engine" => engine = value,
            "--binary" => binary = Some(PathBuf::from(value)),
            "--cwd" => cwd = PathBuf::from(value),
            "--model" => model = Some(value),
            "--resume" => resume = Some(value),
            "--journal-dir" => directory = Some(PathBuf::from(value)),
            "--mode" => {
                mode = octet_core::Mode::parse(&value).ok_or_else(|| {
                    format!("Unknown mode {value}. Use ask, accept-edits, auto or full-access.")
                })?
            }
            "--approval-timeout" => {
                approval_timeout = value
                    .parse::<u64>()
                    .ok()
                    .filter(|seconds| (10..=3600).contains(seconds))
                    .map(std::time::Duration::from_secs)
                    .ok_or("Approval timeout must be a whole number of seconds from 10 to 3600")?
            }
            _ => return Err(format!("Unknown option {arg}. Use --help.")),
        }
    }
    let engine =
        octet_core::Engine::parse(&engine).ok_or("Engine must be codex, claude or demo")?;
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        return Err(
            "The terminal UI requires an interactive terminal. Use --help for options.".into(),
        );
    }
    cwd = cwd
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    if !cwd.is_dir() {
        return Err("Workspace must be a directory".into());
    }
    let directory = directory
        .or_else(|| {
            std::env::var_os("XDG_DATA_HOME")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .map(|p| p.join("octet/rust-preview"))
        })
        .or_else(|| {
            std::env::var_os("HOME")
                .map(|p| PathBuf::from(p).join(".local/share/octet/rust-preview"))
        })
        .ok_or("Set --journal-dir or HOME to choose transcript storage")?;
    let config = Config {
        binary: binary.unwrap_or_else(|| PathBuf::from(engine.as_str())),
        engine,
        cwd,
        model,
        resume,
        mode,
        approval_timeout,
    };
    octet_tui::run(config, directory)
        .await
        .map_err(|e| e.to_string())
}
