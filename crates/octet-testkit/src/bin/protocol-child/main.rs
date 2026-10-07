//! A fake Codex/Claude vendor for tests: it speaks just enough of each
//! protocol to drive Octet through scripted scenarios (`scenario`), and has
//! transport modes for octet-proc's tests.
// Test fixture: a panic here fails the test that started it.
#![allow(clippy::unwrap_used)]
mod claude;
mod codex;
mod wire;

use serde_json::{Value, json};
use std::{
    env,
    io::{self, BufRead, Write},
    process::{self, Command, Stdio},
    thread,
    time::Duration,
};
use wire::emit;

fn main() {
    let mode = env::args().nth(1).expect("mode");
    match mode.as_str() {
        "app-server" => codex::run(),
        "--print" => claude::run(),
        "split" => {
            let mut stdout = io::stdout().lock();
            stdout.write_all(b"{\"part\":").unwrap();
            stdout.flush().unwrap();
            thread::sleep(Duration::from_millis(25));
            stdout.write_all(b"\"complete\"}\n").unwrap();
        }
        "oversize" => {
            let mut stdout = io::stdout().lock();
            stdout.write_all(b"{\"x\":\"").unwrap();
            stdout.write_all(&vec![b'x'; 8192]).unwrap();
            stdout.write_all(b"\"}\n").unwrap();
        }
        "malformed" => println!("{{invalid\n{{\"valid\":true}}"),
        "blank-lines" => print!("{{\"a\":1}}\n\n  \r\n{{\"b\":2}}\n"),
        "valid-then-invalid" => print!("{{\"a\":1}}\n{{\"b\":2}}\nnot json\n{{\"c\":3}}\n"),
        "exit-at-once" => {}
        "partial" => {
            io::stdout().write_all(b"{\"unfinished\":").unwrap();
        }
        "stderr" => {
            let mut stderr = io::stderr().lock();
            stderr.write_all(&vec![b'z'; 65_536]).unwrap();
            stderr.write_all(b"END-OF-STDERR\n").unwrap();
            stderr.flush().unwrap();
            emit(&json!({"ready":true}));
            thread::sleep(Duration::from_secs(5));
        }
        "stderr-exit" => {
            // Explain on stderr and exit at once, like a CLI rejecting its arguments.
            let mut stderr = io::stderr().lock();
            stderr.write_all(&vec![b'z'; 300_000]).unwrap();
            stderr.write_all(b"FINAL-REASON\n").unwrap();
        }
        "grandchild" => {
            // This fixture intentionally exits before reaping its child so the
            // supervisor must clean up a process group whose leader is gone.
            #[allow(clippy::zombie_processes)]
            let child = Command::new(env::current_exe().unwrap())
                .arg("sleeper")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            emit(&json!({"pid":child.id()}));
        }
        "sleeper" => loop {
            thread::sleep(Duration::from_secs(10));
        },
        "echo" | "flood" => {
            let stdin = io::stdin();
            for line in stdin.lock().lines() {
                let value: Value = serde_json::from_str(&line.unwrap()).unwrap();
                match value["op"].as_str() {
                    Some("exit") => process::exit(0),
                    Some("echo") => emit(&value),
                    Some("flood") if mode == "flood" => {
                        thread::spawn(|| {
                            for i in 0..200 {
                                emit(&json!({"data":i}));
                            }
                        });
                    }
                    Some("interrupt") => emit(&json!({"ack":"interrupt"})),
                    _ => {}
                }
            }
        }
        _ => panic!("invalid mode"),
    }
}
