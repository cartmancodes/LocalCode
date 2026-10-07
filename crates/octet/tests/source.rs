//! Hand-packed code that rustfmt can't reflow shows up as very long lines.
mod common;

/// Test infrastructure, not product code.
const NOT_PRODUCT: [&str; 2] = ["octet-gate", "octet-testkit"];

#[test]
fn product_code_has_no_line_over_120_characters_outside_a_lone_literal() {
    let mut long = Vec::new();
    for krate in std::fs::read_dir(common::root().join("crates")).unwrap() {
        let krate = krate.unwrap().path();
        if NOT_PRODUCT.iter().any(|name| krate.ends_with(name)) {
            continue;
        }
        for path in common::files_under(&krate.join("src"), &common::is_rust) {
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
    assert!(
        long.is_empty(),
        "lines over 120 characters:\n{}",
        long.join("\n")
    );
}
