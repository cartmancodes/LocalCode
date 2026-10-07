//! The command line as a process: help and usage errors. Each message is
//! pinned by the parser's unit tests; these check what reaches the shell.
// Test code: an unwrap that fails is the test failing.
#![allow(clippy::unwrap_used)]
use std::process::Command;

/// The invalid-engine error, from the provider table.
fn engine_error() -> String {
    let names: Vec<&str> = octet_core::Engine::ALL.iter().map(|e| e.as_str()).collect();
    format!("Engine must be {}", octet_core::model::or_list(&names))
}

#[test]
fn help_and_errors_name_exactly_the_providers() {
    let names: Vec<&str> = octet_core::Engine::ALL.iter().map(|e| e.as_str()).collect();
    let help = Command::new(env!("CARGO_BIN_EXE_octet"))
        .arg("--help")
        .output()
        .unwrap();
    let help = String::from_utf8_lossy(&help.stdout);
    assert!(
        help.contains(&format!("--engine {}", names.join("|"))),
        "{help}"
    );
    let bad = Command::new(env!("CARGO_BIN_EXE_octet"))
        .args(["--engine", "nope"])
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&bad.stderr).contains(&engine_error()));
}

#[test]
fn help_lists_every_command() {
    let output = Command::new(env!("CARGO_BIN_EXE_octet"))
        .arg("--help")
        .output()
        .unwrap();
    let help = String::from_utf8_lossy(&output.stdout);
    for command in octet_tui::command_names() {
        assert!(help.contains(command), "{command} missing from --help");
    }
}

#[test]
fn usage_errors_exit_2() {
    // Each message is pinned by the parser's unit tests; here, the process.
    let output = Command::new(env!("CARGO_BIN_EXE_octet"))
        .args(["--engine", "demo", "--mode", "bogus"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("octet: Unknown mode bogus"));
}
