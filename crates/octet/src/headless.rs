//! Print, JSON and RPC modes: one session without the terminal interface.
//! Approvals fail closed: print mode denies them, RPC clients answer them.
use octet_core::{event_json, Command, Config, Event, Mode, Outcome, Session};
use serde_json::Value;
use std::path::PathBuf;
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    signal::unix::{signal, SignalKind},
    sync::mpsc,
};

/// The longest RPC command line: a full prompt, JSON-escaped, with room.
const LINE_LIMIT: usize = 4 * octet_core::PROMPT_LIMIT;
/// Exit code after SIGINT, as shells report it.
const INTERRUPTED: i32 = 130;

/// Runs `prompt` as one turn. Text mode writes the reply to stdout; JSON
/// mode writes every event as a line. Notices and errors go to stderr.
/// Exits 0 when the turn completes, 130 when interrupted, 1 otherwise.
pub(crate) async fn print(
    config: Config,
    directory: PathBuf,
    prompt: String,
    json: bool,
) -> Result<i32, String> {
    let mut interrupts = signal(SignalKind::interrupt()).map_err(|e| e.to_string())?;
    let mut session = Session::open(config, directory)
        .await
        .map_err(|e| e.to_string())?;
    let mut out = Reply::default();
    let mut sent = false;
    let mut interrupted = false;
    let code = loop {
        let event = tokio::select! {
            event = session.events.recv() => event,
            _ = interrupts.recv() => {
                // The first ^C cancels a running turn; otherwise stop now.
                if sent && !interrupted {
                    interrupted = true;
                    session.handle.interrupt();
                    continue;
                }
                break INTERRUPTED;
            }
        };
        let Some(event) = event else { break 1 };
        if json {
            write_line(&event_json(&event)).await?;
        }
        match event {
            Event::Ready { .. } if !sent => {
                sent = true;
                session
                    .handle
                    .send(Command::Prompt(prompt.clone()))
                    .map_err(|e| e.to_string())?;
            }
            Event::Text(text) if !json => out.text(&text).await?,
            Event::Approval { id, detail } => {
                let what = detail.lines().next().unwrap_or_default();
                eprintln!(
                    "octet: denied an approval ({what}); print mode cannot ask. \
                     Use --mode auto or --mode full-access for unattended runs."
                );
                session
                    .handle
                    .send(Command::Answer { id, allow: false })
                    .map_err(|e| e.to_string())?;
            }
            Event::Notice(text) | Event::Error(text) if !json => eprintln!("octet: {text}"),
            Event::Finished { outcome } => {
                break match outcome {
                    Outcome::Completed => 0,
                    Outcome::Interrupted => INTERRUPTED,
                    Outcome::Failed | Outcome::Other(_) => 1,
                };
            }
            Event::Stopped => break 1,
            _ => {}
        }
    };
    if !json {
        out.end_text().await?;
    }
    session.shutdown().await;
    Ok(code)
}

/// Reads JSON-line commands from stdin and writes events as JSON lines to
/// stdout. Commands sent before `ready`, and prompts sent while a turn runs,
/// wait their turn in order. At the end of stdin the waiting work finishes,
/// with approvals nobody can answer any more denied. Exits 0 on `quit`, 0 at
/// the end of stdin if every turn completed, and 1 otherwise or if the
/// vendor stops.
pub(crate) async fn rpc(config: Config, directory: PathBuf) -> Result<i32, String> {
    let mut session = Session::open(config, directory)
        .await
        .map_err(|e| e.to_string())?;
    let mut requests = read_lines(BufReader::new(tokio::io::stdin()));
    let mut state = RpcState::default();
    let code = loop {
        if state.input_closed && !state.busy && state.waiting.is_empty() {
            break i32::from(state.failed);
        }
        tokio::select! {
            line = requests.recv(), if !state.input_closed => {
                let Some(line) = line else {
                    state.input_closed = true;
                    for id in std::mem::take(&mut state.approvals) {
                        send_or_report(&session, Command::Answer { id, allow: false }).await?;
                    }
                    continue;
                };
                match line.and_then(|line| parse(&line)) {
                    Ok(Request::Quit) => break 0,
                    Ok(Request::Interrupt) => session.handle.interrupt(),
                    Ok(Request::Send(_)) if state.waiting.len() >= RPC_WAITING => {
                        let message = format!("Too many commands waiting ({RPC_WAITING})");
                        write_line(&event_json(&Event::Error(message))).await?;
                    }
                    Ok(Request::Send(command)) => state.waiting.push_back(command),
                    Err(message) => write_line(&event_json(&Event::Error(message))).await?,
                }
            }
            event = session.events.recv() => {
                let Some(event) = event else { break 1 };
                write_line(&event_json(&event)).await?;
                match event {
                    Event::Ready { .. } => state.ready = true,
                    Event::Finished { outcome } => {
                        state.busy = false;
                        state.failed |= matches!(outcome, Outcome::Failed | Outcome::Other(_));
                    }
                    Event::Approval { id, .. } if state.input_closed => {
                        send_or_report(&session, Command::Answer { id, allow: false }).await?;
                    }
                    Event::Approval { id, .. } => state.approvals.push(id),
                    Event::ApprovalClosed(id) => state.approvals.retain(|open| *open != id),
                    Event::Stopped => break 1,
                    _ => {}
                }
            }
        }
        state.flush(&session).await?;
    };
    session.shutdown().await;
    Ok(code)
}

/// The most RPC commands that may wait for `ready` or a running turn.
const RPC_WAITING: usize = 64;

/// What the RPC loop tracks between commands and events.
#[derive(Default)]
struct RpcState {
    /// The vendor session is ready for commands.
    ready: bool,
    /// A prompt is running.
    busy: bool,
    /// Commands waiting for `ready`, and prompts waiting for the turn.
    waiting: std::collections::VecDeque<Command>,
    /// Approvals the client has not answered.
    approvals: Vec<u64>,
    /// Stdin has ended.
    input_closed: bool,
    /// A turn failed.
    failed: bool,
}
impl RpcState {
    /// Sends waiting commands in order, stopping at a prompt while one runs.
    async fn flush(&mut self, session: &Session) -> Result<(), String> {
        while self.ready {
            let prompt = matches!(
                self.waiting.front(),
                Some(Command::Prompt(_) | Command::PromptWithDisplay { .. } | Command::Compact)
            );
            if prompt && self.busy {
                break;
            }
            let Some(command) = self.waiting.pop_front() else {
                break;
            };
            if send_or_report(session, command).await? && prompt {
                self.busy = true;
            }
        }
        Ok(())
    }
}

/// Sends `command`; whether it was accepted (a refusal is an error line).
async fn send_or_report(session: &Session, command: Command) -> Result<bool, String> {
    match session.handle.send(command) {
        Ok(()) => Ok(true),
        Err(error) => {
            write_line(&event_json(&Event::Error(error.to_string()))).await?;
            Ok(false)
        }
    }
}

/// The prompt for `--print -`: all of stdin, less one trailing newline.
pub(crate) async fn read_prompt() -> Result<String, String> {
    let mut text = String::new();
    tokio::io::stdin()
        .take(octet_core::PROMPT_LIMIT as u64 + 2)
        .read_to_string(&mut text)
        .await
        .map_err(|e| format!("Cannot read the prompt from stdin: {e}"))?;
    let text = text.strip_suffix('\n').unwrap_or(&text);
    Ok(text.strip_suffix('\r').unwrap_or(text).to_owned())
}

/// One RPC command.
#[derive(Debug, PartialEq)]
enum Request {
    Send(Command),
    Interrupt,
    Quit,
}

/// Parses `{"type": …}` commands: prompt, answer, interrupt, mode, effort
/// and quit.
fn parse(line: &str) -> Result<Request, String> {
    let v: Value = serde_json::from_str(line).map_err(|e| format!("Invalid JSON: {e}"))?;
    let send = |command| Ok(Request::Send(command));
    match v["type"].as_str().unwrap_or_default() {
        "prompt" => match v["text"].as_str() {
            Some(text) => send(Command::Prompt(text.to_owned())),
            None => Err(r#"prompt needs "text""#.into()),
        },
        "answer" => match (v["id"].as_u64(), v["allow"].as_bool()) {
            (Some(id), Some(allow)) => send(Command::Answer { id, allow }),
            _ => Err(r#"answer needs "id" and "allow""#.into()),
        },
        "interrupt" => Ok(Request::Interrupt),
        "mode" => match v["mode"].as_str().and_then(Mode::parse) {
            Some(mode) => send(Command::SetMode(mode)),
            None => Err(r#"mode needs "mode": ask, accept-edits, auto or full-access"#.into()),
        },
        "effort" => match &v["level"] {
            Value::Null => send(Command::SetEffort(None)),
            Value::String(level) if octet_core::valid_effort(level) => {
                send(Command::SetEffort(Some(level.clone())))
            }
            _ => Err(r#"effort needs "level": one word, or null for the vendor default"#.into()),
        },
        "quit" => Ok(Request::Quit),
        other => Err(format!(
            "Unknown command type {other:?}; use prompt, answer, interrupt, mode, effort or quit"
        )),
    }
}

/// Reads lines on a task of their own, so a partly read line survives the
/// select loop. A line over `LINE_LIMIT` is skipped whole and reported.
fn read_lines(
    mut reader: impl AsyncBufRead + Unpin + Send + 'static,
) -> mpsc::Receiver<Result<String, String>> {
    let (tx, rx) = mpsc::channel(16);
    tokio::spawn(async move {
        loop {
            let line = match read_line(&mut reader).await {
                Ok(Some(line)) => line,
                Ok(None) | Err(_) => break,
            };
            if matches!(&line, Ok(text) if text.trim().is_empty()) {
                continue;
            }
            if tx.send(line).await.is_err() {
                break;
            }
        }
    });
    rx
}

/// One line without its ending; `Err` for a line too long or not UTF-8.
async fn read_line(
    reader: &mut (impl AsyncBufRead + Unpin),
) -> std::io::Result<Option<Result<String, String>>> {
    let mut line = Vec::new();
    let limit = LINE_LIMIT as u64 + 1;
    if (&mut *reader)
        .take(limit)
        .read_until(b'\n', &mut line)
        .await?
        == 0
    {
        return Ok(None);
    }
    if line.last() != Some(&b'\n') && line.len() > LINE_LIMIT {
        // Skip the rest of the line, in bounded pieces.
        let mut rest = Vec::new();
        loop {
            rest.clear();
            let read = (&mut *reader)
                .take(limit)
                .read_until(b'\n', &mut rest)
                .await?;
            if read == 0 || rest.last() == Some(&b'\n') {
                break;
            }
        }
        return Ok(Some(Err(format!(
            "A command line is over {LINE_LIMIT} bytes"
        ))));
    }
    Ok(Some(
        String::from_utf8(line)
            .map(|text| text.trim_end_matches(['\n', '\r']).to_owned())
            .map_err(|_| "A command line is not UTF-8".to_owned()),
    ))
}

/// The reply text in print mode, streamed to stdout.
#[derive(Default)]
struct Reply {
    /// Whether the last text written left a line open.
    open_line: bool,
}
impl Reply {
    async fn text(&mut self, text: &str) -> Result<(), String> {
        if !text.is_empty() {
            self.open_line = !text.ends_with('\n');
        }
        write_out(text.as_bytes()).await
    }
    /// Ends the reply with a newline, if it did not end with one.
    async fn end_text(&mut self) -> Result<(), String> {
        if std::mem::take(&mut self.open_line) {
            write_out(b"\n").await?;
        }
        Ok(())
    }
}

/// Writes `value` as one JSON line.
async fn write_line(value: &Value) -> Result<(), String> {
    write_out(format!("{value}\n").as_bytes()).await
}

/// Writes to stdout and flushes, so a reader sees each line as it happens.
async fn write_out(bytes: &[u8]) -> Result<(), String> {
    let mut stdout = tokio::io::stdout();
    stdout
        .write_all(bytes)
        .await
        .and(stdout.flush().await)
        .map_err(|e| format!("Cannot write to stdout: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rpc_commands_parse_or_say_what_is_wrong() {
        assert_eq!(
            parse(r#"{"type":"prompt","text":"hi"}"#),
            Ok(Request::Send(Command::Prompt("hi".into())))
        );
        assert_eq!(
            parse(r#"{"type":"answer","id":3,"allow":false}"#),
            Ok(Request::Send(Command::Answer {
                id: 3,
                allow: false
            }))
        );
        assert_eq!(
            parse(r#"{"type":"mode","mode":"auto"}"#),
            Ok(Request::Send(Command::SetMode(Mode::Auto)))
        );
        assert_eq!(
            parse(r#"{"type":"effort","level":null}"#),
            Ok(Request::Send(Command::SetEffort(None)))
        );
        assert_eq!(parse(r#"{"type":"interrupt"}"#), Ok(Request::Interrupt));
        assert_eq!(parse(r#"{"type":"quit"}"#), Ok(Request::Quit));
        assert!(parse(r#"{"type":"prompt"}"#).unwrap_err().contains("text"));
        assert!(parse(r#"{"type":"effort","level":"two words"}"#).is_err());
        assert!(parse("not json").unwrap_err().starts_with("Invalid JSON"));
    }

    #[tokio::test]
    async fn long_lines_are_skipped_whole() {
        let long = "x".repeat(LINE_LIMIT + 10);
        let input = format!("{long}\nshort\r\n");
        let mut reader = BufReader::new(input.as_bytes());
        assert!(read_line(&mut reader).await.unwrap().unwrap().is_err());
        assert_eq!(
            read_line(&mut reader).await.unwrap().unwrap(),
            Ok("short".to_owned())
        );
        assert_eq!(read_line(&mut reader).await.unwrap(), None);
    }
}
