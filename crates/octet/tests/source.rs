//! Hand-packed code that rustfmt can't reflow shows up as very long lines.
use std::path::Path;

/// Product crates; the gate and the test kit are test infrastructure.
const CRATES: [&str; 6] = [
    "octet-proc",
    "octet-engine",
    "octet-store",
    "octet-core",
    "octet-tui",
    "octet",
];

#[test]
fn product_code_has_no_line_over_120_characters_outside_a_lone_literal() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut long = Vec::new();
    for krate in CRATES {
        let mut dirs = vec![root.join("crates").join(krate).join("src")];
        while let Some(dir) = dirs.pop() {
            for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    dirs.push(path);
                } else if path.extension().is_some_and(|e| e == "rs") {
                    let text = std::fs::read_to_string(&path).unwrap();
                    for (n, line) in text.lines().enumerate() {
                        let trimmed = line.trim_start();
                        let lone = trimmed.starts_with('"') || trimmed.starts_with("r#\"");
                        if line.chars().count() > 120 && !lone {
                            long.push(format!("{}:{}", path.display(), n + 1));
                        }
                    }
                }
            }
        }
    }
    assert!(
        long.is_empty(),
        "lines over 120 characters:\n{}",
        long.join("\n")
    );
}
