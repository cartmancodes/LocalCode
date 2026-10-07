//! Octet never reads vendor credentials or holds an API key: each vendor
//! CLI authenticates itself. This guard fails the build if harness source
//! names a credential store or a secret-looking environment variable.
//! It replaces the Python guard (`backend/app/invariants.py`) removed with the
//! Python code; the marker list is unchanged.
use std::path::{Path, PathBuf};
mod common;

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

/// A name ending in a secret suffix, anywhere on the line: in a literal
/// (`.env(`, `set_var(`, an env list, a call rustfmt split across lines, a
/// name built at run time such as `"_API_KEY"` or
/// `format!("{vendor}_API_KEY")`), on a continued string's next line, or
/// bare, as workflows and scripts write names. A suffix in the middle of a
/// name (`API_KEY_HELP`) is not a secret name.
fn secret_names(line: &str) -> Vec<String> {
    line.split(|c: char| !(c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_'))
        .filter(|token| SECRET_SUFFIXES.iter().any(|suffix| token.ends_with(suffix)))
        .map(str::to_owned)
        .collect()
}

/// Markers are matched in the whole text, comments included: no harness file
/// has a reason to name a credential store at all, and a whole-text match has
/// no parser to confuse (char literals, continued or multi-line strings).
fn violations(source: &str) -> Vec<(usize, String)> {
    let mut found = Vec::new();
    for (index, line) in source.lines().enumerate() {
        for marker in MARKERS {
            if line.contains(marker) {
                found.push((index + 1, format!("credential store {marker:?}")));
            }
        }
        for name in secret_names(line) {
            found.push((index + 1, format!("secret variable {name:?}")));
        }
    }
    found
}

/// Everything the guard reads: the crates' Rust sources, and the scripts
/// and workflows that run with the repository's secrets nearby.
fn guarded_files() -> Vec<PathBuf> {
    let root = common::root();
    let mut files = common::files_under(&root.join("crates"), &common::is_rust);
    for dir in ["scripts", ".github"] {
        files.extend(common::files_under(&root.join(dir), &|_| true));
    }
    files
}

#[test]
fn sample_violations_are_detected() {
    let sample = concat!(
        "let p = home.join(\".claude/.credentials.json\");\n",
        "cmd.env(\"ANTHROPIC_API_KEY\", key);\n",
        "cmd.env(\"MY_VENDOR_AUTH_TOKEN\", key);\n",
        "// auth.json in a comment counts too\n",
        "let ok = \"no secrets here\";\n",
    );
    let found = violations(sample);
    let lines: Vec<usize> = found.iter().map(|(line, _)| *line).collect();
    for line in [1, 2, 3, 4] {
        assert!(lines.contains(&line), "line {line}: {found:?}");
    }
    assert!(!lines.contains(&5), "{found:?}");
}

#[test]
fn raw_strings_are_scanned() {
    let found =
        violations("let a = r#\"~/.codex/auth.json\"#; let b = r\"find-generic-password\";\n");
    assert_eq!(found.len(), 4, "{found:?}"); // .codex/auth, auth.json, /auth.json, find-generic-password
}

#[test]
fn workspace_names_no_credential_store() {
    let this = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/credentials.rs");
    let files = guarded_files();
    assert!(files.len() > 10, "found only {} files", files.len());
    let mut report = Vec::new();
    for file in files {
        if file.canonicalize().unwrap() == this.canonicalize().unwrap() {
            continue; // the rule's own data
        }
        // A file the guard cannot read is a failure, never a skip.
        // Read as bytes: a file that is not UTF-8 is still scanned.
        let bytes = std::fs::read(&file)
            .unwrap_or_else(|error| panic!("cannot read {}: {error}", file.display()));
        let source = String::from_utf8_lossy(&bytes);
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

#[test]
fn secret_variables_are_caught_in_every_shape() {
    let shapes = [
        // rustfmt splits a long call so the name lands on its own line.
        "    cmd.env(\n        \"CODEX_API_KEY\",\n        key_from_somewhere_long,\n    );\n",
        "std::env::set_var(\"CODEX_API_KEY\", key);\n",
        "cmd.envs([(\"CODEX_API_KEY\", key)]);\n",
        "cmd.env(\"PATH\", path).env(\"VENDOR_OAUTH_TOKEN\", token);\n",
        "let env = vec![(\"VENDOR_SESSION_KEY\".into(), key)];\n",
    ];
    for shape in shapes {
        assert!(!violations(shape).is_empty(), "missed: {shape}");
    }
    assert!(violations("let name = \"API_KEY_HELP\"; let path = \"PATH\";\n").is_empty());
}

#[test]
fn markers_hidden_from_a_line_parser_are_caught() {
    let shapes = [
        "if c == '\"' { p.push(\"auth.json\") }\n",
        "let s = \"first line \\\n    ~/.codex/auth\";\n",
        "let s = r#\"\nnot a quote here: .credentials.json\n\"#;\n",
    ];
    for shape in shapes {
        assert!(!violations(shape).is_empty(), "missed: {shape}");
    }
}

#[test]
fn no_keychain_crate_is_a_dependency() {
    let lock = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.lock");
    let lock = std::fs::read_to_string(lock).expect("read Cargo.lock");
    for name in ["keyring", "security-framework"] {
        assert!(
            !lock.contains(&format!("name = \"{name}\"")),
            "{name} would let harness code read the OS keychain"
        );
    }
}

#[test]
fn secret_names_built_at_run_time_are_caught() {
    for shape in [
        "let name = \"_API_KEY\";\n",
        "let name = format!(\"{vendor}_API_KEY\");\n",
        "cmd.env(format!(\"{}_OAUTH_TOKEN\", prefix), token);\n",
    ] {
        assert!(!violations(shape).is_empty(), "missed: {shape}");
    }
}

#[test]
fn the_guard_reads_scripts_and_workflows() {
    let files = guarded_files();
    for expected in ["scripts/rust-env.sh", ".github/workflows/release.yml"] {
        assert!(
            files.iter().any(|file| file.ends_with(expected)),
            "{expected} is not guarded"
        );
    }
}

#[test]
fn unquoted_secret_names_are_caught() {
    for shape in [
        // Workflows and scripts name variables without quotes.
        "        env:\n          VENDOR_API_KEY: ${{ secrets.KEY }}\n",
        "export VENDOR_OAUTH_TOKEN=abc\n",
        "echo \"$VENDOR_SESSION_KEY\"\n",
        // The continuation line of a multi-line Rust string.
        "let s = \"first line \\\n    then VENDOR_AUTH_TOKEN\";\n",
    ] {
        assert!(!violations(shape).is_empty(), "missed: {shape}");
    }
}
