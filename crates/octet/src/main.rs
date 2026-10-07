//! `octet`: parses the command line and starts the terminal interface, or
//! runs headless (`--print`, `--rpc`).
use octet_core::Config;
use std::{io::IsTerminal, path::PathBuf};
mod headless;

/// What the process runs once the options are read.
enum Run {
    /// The terminal interface.
    Interface,
    /// One prompt; the reply (or, with `json`, every event) on stdout.
    Print { prompt: String, json: bool },
    /// JSON-line commands in, events out.
    Rpc,
}

/// `--help`: fixed usage and keys, then every command from the registry.
fn help() -> String {
    const HEAD: &[&str] = &[
        "                 [--model MODEL] [--mode MODE] [--resume VENDOR_SESSION_ID]",
        "                 [--binary PATH] [--journal-dir PATH]",
        "                 [--approval-timeout SECONDS] [--effort LEVEL]",
        "                 [--print PROMPT|- [--output text|json] | --rpc]",
        "",
        "Headless: --print (-p) runs one prompt and writes the reply; - reads it",
        "from stdin. --output json writes every event as a JSON line. --rpc reads",
        "JSON-line commands (prompt, answer, interrupt, mode, effort, quit) from",
        "stdin and writes events. Print mode denies approvals; exit codes are 0",
        "(completed), 1 (failed) and 130 (interrupted).",
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
    let engines: Vec<&str> = octet_core::Engine::ALL
        .iter()
        .map(|engine| engine.as_str())
        .collect();
    let mut text = format!(
        "Octet — terminal coding workspace (Rust preview)\n\nUsage: octet [--engine {}] [--cwd PATH]\n",
        engines.join("|")
    );
    text.push_str(&HEAD.join("\n"));
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
    match run().await {
        Ok(0) => {}
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("octet: {error}");
            std::process::exit(1);
        }
    }
}
/// Reads the options and runs; the process's exit code.
async fn run() -> Result<i32, String> {
    let mut args = std::env::args().skip(1);
    let mut engine = "codex".to_owned();
    let mut binary = None;
    let mut model = None;
    let mut resume = None;
    let mut cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    let mut directory = None;
    let mut mode = octet_core::Mode::Ask;
    let mut approval_timeout = octet_core::DEFAULT_APPROVAL_TIMEOUT;
    let mut effort = None;
    let mut print = None;
    let mut output = None;
    let mut rpc = false;
    while let Some(arg) = args.next() {
        if arg == "--help" || arg == "-h" {
            print!("{}", help());
            return Ok(0);
        }
        if arg == "--version" {
            println!("octet {} (Rust preview)", env!("CARGO_PKG_VERSION"));
            return Ok(0);
        }
        if arg == "--rpc" {
            rpc = true;
            continue;
        }
        let arg = if arg == "-p" {
            "--print".to_owned()
        } else {
            arg
        };
        const OPTIONS: [&str; 11] = [
            "--engine",
            "--binary",
            "--cwd",
            "--model",
            "--resume",
            "--journal-dir",
            "--mode",
            "--approval-timeout",
            "--effort",
            "--print",
            "--output",
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
            "--effort" => {
                if !octet_core::valid_effort(&value) {
                    return Err("Effort must be one word, at most 64 bytes".into());
                }
                effort = Some(value);
            }
            "--print" => print = Some(value),
            "--output" => {
                output = Some(match value.as_str() {
                    "text" => false,
                    "json" => true,
                    _ => return Err("Output must be text or json".into()),
                })
            }
            _ => unreachable!("{arg} is checked against OPTIONS"),
        }
    }
    let engine = octet_core::Engine::parse(&engine).ok_or_else(|| {
        let names: Vec<&str> = octet_core::Engine::ALL
            .iter()
            .map(|engine| engine.as_str())
            .collect();
        format!("Engine must be {}", octet_core::model::or_list(&names))
    })?;
    let run = match (print, rpc) {
        (Some(_), true) => return Err("--print and --rpc cannot be combined".into()),
        (Some(prompt), false) => Run::Print {
            prompt: if prompt == "-" {
                headless::read_prompt().await?
            } else {
                prompt
            },
            json: output.unwrap_or(false),
        },
        (None, _) if output.is_some() => return Err("--output applies to --print".into()),
        (None, true) => Run::Rpc,
        (None, false) => Run::Interface,
    };
    if matches!(run, Run::Print { ref prompt, .. } if prompt.trim().is_empty()) {
        return Err("The prompt is empty".into());
    }
    if matches!(run, Run::Interface)
        && (!std::io::stdin().is_terminal() || !std::io::stdout().is_terminal())
    {
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
        effort,
        fork: false,
    };
    match run {
        Run::Interface => octet_tui::run(config, directory)
            .await
            .map(|()| 0)
            .map_err(|e| e.to_string()),
        Run::Print { prompt, json } => headless::print(config, directory, prompt, json).await,
        Run::Rpc => headless::rpc(config, directory).await,
    }
}
