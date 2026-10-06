use octet_core::Config;
use std::{io::IsTerminal, path::PathBuf};
/// `--help`: fixed usage and keys, then every command from the registry.
fn help() -> String {
    const HEAD: &[&str] = &[
        "Octet — terminal coding workspace (Rust preview)",
        "",
        "Usage: octet [--engine codex|claude|demo] [--cwd PATH]",
        "                 [--model MODEL] [--mode MODE] [--resume VENDOR_SESSION_ID]",
        "                 [--binary PATH] [--journal-dir PATH]",
        "                 [--approval-timeout SECONDS]",
        "",
        "Defaults: Codex, current directory. Vendor CLI installation and login required.",
        "Modes: ask (default) · accept-edits · auto (vendor auto-review) · full-access",
        "Use --engine demo for an offline interactive preview.",
        "",
        "Keys: Enter send · Alt+Enter / Ctrl+J newline · Esc cancel · Ctrl+C twice quit",
        "      PageUp/PageDown scroll · Ctrl+P commands · F1 help · Shift+Tab mode",
        "      @ mention a file · Tab complete · !cmd run (attach) · !!cmd run only",
        "      Ctrl+G edit in $EDITOR · Ctrl+X copy the last reply",
        "",
    ];
    let mut text = HEAD.join("\n");
    // "Commands: " then the names, wrapped at 76 columns under the first.
    let mut line = String::from("\nCommands: ");
    for (i, name) in octet_tui::command_names().enumerate() {
        let item = if i == 0 {
            name.to_owned()
        } else {
            format!(", {name}")
        };
        if line.chars().count() + item.chars().count() > 77 {
            text.push_str(line.trim_end());
            text.push(',');
            line = format!("\n          {}", &item[2..]);
        } else {
            line.push_str(&item);
        }
    }
    text.push_str(&line);
    text.push_str("\nJournals are separate JSONL files; see --journal-dir.\n");
    text
}
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
            print!("{}", help());
            return Ok(());
        }
        if arg == "--version" {
            println!("octet {} (Rust preview)", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        const OPTIONS: [&str; 8] = [
            "--engine",
            "--binary",
            "--cwd",
            "--model",
            "--resume",
            "--journal-dir",
            "--mode",
            "--approval-timeout",
        ];
        if !OPTIONS.contains(&arg.as_str()) {
            return Err(format!("Unknown option {arg}. Use --help."));
        }
        // A value never starts with "--", so a forgotten value can't swallow
        // the next option.
        let value = args
            .next()
            .filter(|value| !value.starts_with("--"))
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
            _ => unreachable!("{arg} is checked against OPTIONS"),
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
