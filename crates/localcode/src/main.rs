use lc_core::Config;
use std::{io::IsTerminal, path::PathBuf};
const HELP:&str="LocalCode — terminal coding workspace (Rust preview)\n\nUsage: localcode [--engine codex|claude|demo] [--cwd PATH]\n                 [--model MODEL] [--resume VENDOR_SESSION_ID]\n                 [--binary PATH] [--journal-dir PATH]\n\nDefaults: Codex, current directory. Vendor CLI installation and login required.\nUse --engine demo for an offline interactive preview.\n\nKeys: Enter send · Alt+Enter / Ctrl+J newline · Esc cancel · Ctrl+Q quit\n      PageUp/PageDown scroll · Ctrl+P commands · F1 help\n\nCommands: /model, /goal, /session, /new, /reconnect, /export\nThis preview writes separate JSONL journals; it does not modify legacy sessions.\nFleet, full plugin/hook parity, v3 browsing and legacy RPC compatibility remain pending.\n";
#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("localcode: {error}");
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
    while let Some(arg) = args.next() {
        if arg == "--help" || arg == "-h" {
            print!("{HELP}");
            return Ok(());
        }
        if arg == "--version" {
            println!("localcode {} (Rust preview)", env!("CARGO_PKG_VERSION"));
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
            _ => return Err(format!("Unknown option {arg}. Use --help.")),
        }
    }
    if !matches!(engine.as_str(), "claude" | "codex" | "demo") {
        return Err("Engine must be codex, claude or demo".into());
    }
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        return Err("The Rust preview requires an interactive terminal. Existing Python RPC/print modes remain unchanged. Use --help for options.".into());
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
                .map(|p| p.join("localcode/rust-preview"))
        })
        .or_else(|| {
            std::env::var_os("HOME")
                .map(|p| PathBuf::from(p).join(".local/share/localcode/rust-preview"))
        })
        .ok_or("Set --journal-dir or HOME to choose transcript storage")?;
    let config = Config {
        binary: binary.unwrap_or_else(|| PathBuf::from(&engine)),
        engine,
        cwd,
        model,
        resume,
    };
    lc_tui::run(config, directory)
        .await
        .map_err(|e| e.to_string())
}
