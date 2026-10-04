//! LocalCode never reads vendor credentials or holds an API key: each vendor
//! CLI authenticates itself. This guard fails the build if harness source
//! names a credential store or assigns a secret-looking environment variable.
//! It replaces the Python guard (`backend/app/invariants.py`) removed with the
//! Python code; the marker list is unchanged.
use std::path::{Path, PathBuf};

const MARKERS: [&str; 12] = [
    ".credentials.json",
    "credentials.json",
    "/auth.json",
    "auth.json",
    ".claude/.credentials",
    ".codex/auth",
    "find-generic-password",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_SESSION_KEY",
    "ANTHROPIC_AUTH_TOKEN",
];
const SECRET_SUFFIXES: [&str; 4] = ["API_KEY", "OAUTH_TOKEN", "SESSION_KEY", "AUTH_TOKEN"];

/// String literals on one line: normal ("…" with escapes) and raw (r"…", r#"…"#).
fn literals(line: &str) -> Vec<String> {
    let bytes = line.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'r'
            && i + 1 < bytes.len()
            && (bytes[i + 1] == b'"' || bytes[i + 1] == b'#')
            && (i == 0 || !(bytes[i - 1].is_ascii_alphanumeric() || bytes[i - 1] == b'_'))
        {
            let mut j = i + 1;
            let mut hashes = 0;
            while j < bytes.len() && bytes[j] == b'#' {
                hashes += 1;
                j += 1;
            }
            if j < bytes.len() && bytes[j] == b'"' {
                let close = format!("\"{}", "#".repeat(hashes));
                if let Some(end) = line[j + 1..].find(&close) {
                    out.push(line[j + 1..j + 1 + end].to_owned());
                    i = j + 1 + end + close.len();
                    continue;
                }
            }
        }
        if bytes[i] == b'"' {
            let mut j = i + 1;
            let mut text = String::new();
            while j < bytes.len() && bytes[j] != b'"' {
                if bytes[j] == b'\\' && j + 1 < bytes.len() {
                    j += 1;
                }
                text.push(bytes[j] as char);
                j += 1;
            }
            out.push(text);
            i = j + 1;
            continue;
        }
        i += 1;
    }
    out
}

fn violations(source: &str) -> Vec<(usize, String)> {
    let mut found = Vec::new();
    for (index, line) in source.lines().enumerate() {
        for literal in literals(line) {
            for marker in MARKERS {
                if literal.contains(marker) {
                    found.push((index + 1, format!("credential store {marker:?}")));
                }
            }
        }
        if let Some(start) = line.find(".env(") {
            if let Some(name) = literals(&line[start..]).first() {
                if SECRET_SUFFIXES.iter().any(|suffix| name.ends_with(suffix)) {
                    found.push((index + 1, format!("assigns secret variable {name:?}")));
                }
            }
        }
    }
    found
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            if path.file_name().is_some_and(|name| name == "target") {
                continue;
            }
            rust_files(&path, out);
        } else if path.extension().is_some_and(|ext| ext == "rs") {
            out.push(path);
        }
    }
}

#[test]
fn sample_violations_are_detected() {
    let sample = concat!(
        "let p = home.join(\".claude/.credentials.json\");\n",
        "cmd.env(\"ANTHROPIC_API_KEY\", key);\n",
        "cmd.env(\"MY_VENDOR_AUTH_TOKEN\", key);\n",
        "// auth.json in a comment is not code\n",
        "let ok = \"no secrets here\";\n",
    );
    let found = violations(sample);
    let lines: Vec<usize> = found.iter().map(|(line, _)| *line).collect();
    assert!(
        lines.contains(&1) && lines.contains(&2) && lines.contains(&3),
        "{found:?}"
    );
    assert!(!lines.contains(&4) && !lines.contains(&5), "{found:?}");
}

#[test]
fn raw_strings_are_scanned() {
    let found =
        violations("let a = r#\"~/.codex/auth.json\"#; let b = r\"find-generic-password\";\n");
    assert_eq!(found.len(), 4, "{found:?}"); // .codex/auth, auth.json, /auth.json, find-generic-password
}

#[test]
fn workspace_names_no_credential_store() {
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let this = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/credentials.rs");
    let mut files = Vec::new();
    rust_files(&crates, &mut files);
    assert!(files.len() > 10, "found only {} Rust files", files.len());
    let mut report = Vec::new();
    for file in files {
        if file.canonicalize().unwrap() == this.canonicalize().unwrap() {
            continue; // the rule's own data
        }
        // A file the guard cannot read is a failure, never a skip.
        let source = std::fs::read_to_string(&file)
            .unwrap_or_else(|error| panic!("cannot read {}: {error}", file.display()));
        for (line, rule) in violations(&source) {
            report.push(format!("{}:{line}: {rule}", file.display()));
        }
    }
    assert!(
        report.is_empty(),
        "credential guard violations:\n{}",
        report.join("\n")
    );
}
