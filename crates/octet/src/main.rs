//! `octet`: parses the command line and starts the terminal interface, or
//! runs headless (`--print`, `--rpc`).
use args::{CliError, Parsed, Prompt, Run};
use std::{io::IsTerminal, path::PathBuf};
mod args;
mod headless;

/// Where journals and goals go under the data directory. Named when the Rust
/// port was a preview; kept so existing journals and goals are still found.
const JOURNAL_DIR: &str = "octet/rust-preview";

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
        "(completed), 1 (failed), 2 (usage error) and 130 (interrupted).",
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
        "Octet — terminal coding workspace (preview)\n\nUsage: octet [--engine {}] [--cwd PATH]\n",
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
            std::process::exit(error.code());
        }
    }
}
/// Reads the options and runs; the process's exit code.
async fn run() -> Result<i32, CliError> {
    let args = match args::parse_args(std::env::args_os().skip(1))? {
        Parsed::Help => {
            print!("{}", help());
            return Ok(0);
        }
        Parsed::Version => {
            println!("octet {}", env!("CARGO_PKG_VERSION"));
            return Ok(0);
        }
        Parsed::Run(args) => args,
    };
    let stdin_prompt = match &args.run {
        Run::Print {
            prompt: Prompt::Stdin,
            ..
        } => {
            let prompt = headless::read_prompt().await.map_err(CliError::Usage)?;
            args::check_prompt(&prompt)?;
            Some(prompt)
        }
        _ => None,
    };
    if args.run == Run::Interface
        && (!std::io::stdin().is_terminal() || !std::io::stdout().is_terminal())
    {
        return Err(CliError::Usage(
            "The terminal UI requires an interactive terminal. Use --help for options.".into(),
        ));
    }
    let cwd = match &args.cwd {
        Some(cwd) => cwd.clone(),
        None => std::env::current_dir().map_err(|e| CliError::Run(e.to_string()))?,
    };
    let config = args.config(args::workspace(&cwd)?);
    let directory = args
        .journal_dir
        .clone()
        .or_else(|| {
            std::env::var_os("XDG_DATA_HOME")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .map(|p| p.join(JOURNAL_DIR))
        })
        .or_else(|| {
            std::env::var_os("HOME")
                .map(|p| PathBuf::from(p).join(".local/share").join(JOURNAL_DIR))
        })
        .ok_or_else(|| {
            CliError::Usage("Set --journal-dir or HOME to choose transcript storage".into())
        })?;
    let result = match args.run {
        Run::Interface => octet_tui::run(config, directory)
            .await
            .map(|()| 0)
            .map_err(|e| e.to_string()),
        Run::Print { prompt, json } => {
            let prompt = match prompt {
                Prompt::Text(text) => text,
                Prompt::Stdin => stdin_prompt.unwrap_or_default(),
            };
            headless::print(config, directory, prompt, json).await
        }
        Run::Rpc => headless::rpc(config, directory).await,
    };
    result.map_err(CliError::Run)
}
