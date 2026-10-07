//! The prompt box's attachment and completion logic, free of terminal I/O.
use crate::shell::{Ran, CUT};
use std::path::Path;

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Kind {
    File,
    Path,
    Command,
}

/// The suggestion popup above the prompt box.
#[derive(Debug)]
pub struct Completion {
    pub kind: Kind,
    pub items: Vec<String>,
    pub selected: usize,
    /// Where the word being completed starts in the draft.
    pub start: usize,
}

/// What Tab does to the draft.
#[derive(Debug)]
pub enum Tab {
    /// Replace the word; with several matches, also open the popup.
    Replace {
        start: usize,
        text: String,
        popup: Option<Completion>,
    },
    Popup(Completion),
    /// Open the `@` popup for the mention starting here.
    Mention(usize),
    Nothing,
}

/// The start of the word ending at `cursor`.
fn word_start(text: &str, cursor: usize) -> usize {
    text[..cursor]
        .char_indices()
        .rev()
        .find(|(_, c)| c.is_whitespace())
        .map_or(0, |(at, c)| at + c.len_utf8())
}

/// The `@` mention ending at the cursor: where it starts and the query after
/// the `@`.
pub fn mention_at(text: &str, cursor: usize) -> Option<(usize, &str)> {
    let start = word_start(text, cursor);
    let query = text[start..cursor].strip_prefix('@')?;
    // The word must end at the cursor, or the user moved past it.
    if text[cursor..]
        .chars()
        .next()
        .is_some_and(|c| !c.is_whitespace())
    {
        return None;
    }
    (!query.contains('"')).then_some((start, query))
}

/// The text an accepted file suggestion puts in the draft.
pub fn mention(path: &str) -> String {
    if path.contains(char::is_whitespace) {
        format!("@\"{path}\" ")
    } else {
        format!("@{path} ")
    }
}

/// What Tab does with the word ending at the cursor.
pub fn tab(text: &str, cursor: usize, root: &Path, home: Option<&Path>) -> Tab {
    let start = word_start(text, cursor);
    let word = &text[start..cursor];
    if word.starts_with('@') {
        return Tab::Mention(start);
    }
    let first_word = text[..start].trim().is_empty();
    if first_word && word.starts_with('/') && !word[1..].contains('/') {
        let names: Vec<String> = crate::registry::COMMANDS
            .iter()
            .map(|spec| spec.name.to_owned())
            .filter(|name| name.starts_with(word))
            .collect();
        return choose(start, word, names, Kind::Command, " ");
    }
    if word.contains('/') || word.starts_with('~') {
        return choose(
            start,
            word,
            path_candidates(root, home, word),
            Kind::Path,
            "",
        );
    }
    Tab::Nothing
}

/// One candidate fills in; several fill their common prefix and open the
/// popup.
fn choose(start: usize, word: &str, items: Vec<String>, kind: Kind, suffix: &str) -> Tab {
    match items.len() {
        0 => Tab::Nothing,
        1 => Tab::Replace {
            start,
            popup: None,
            text: format!(
                "{}{}",
                items[0],
                if items[0].ends_with('/') { "" } else { suffix }
            ),
        },
        _ => {
            let prefix = common_prefix(&items);
            let popup = Completion {
                kind,
                items: items.into_iter().take(crate::files::SHOWN).collect(),
                selected: 0,
                start,
            };
            if prefix.len() > word.len() {
                Tab::Replace {
                    start,
                    text: prefix,
                    popup: Some(popup),
                }
            } else {
                Tab::Popup(popup)
            }
        }
    }
}

fn common_prefix(items: &[String]) -> String {
    let first = &items[0];
    let mut end = first.len();
    for item in &items[1..] {
        end = first
            .char_indices()
            .zip(item.chars())
            .find(|((_, a), b)| a != b)
            .map_or(end.min(item.len()), |((at, _), _)| at.min(end));
    }
    first[..end].to_owned()
}

/// Entries of the word's folder starting with its last part, as whole words
/// (folders end in `/`). Hidden entries only when the part starts with `.`.
fn path_candidates(root: &Path, home: Option<&Path>, word: &str) -> Vec<String> {
    let (folder, part) = match word.rfind('/') {
        Some(slash) => (&word[..=slash], &word[slash + 1..]),
        None => ("", word),
    };
    let base = if let Some(rest) = folder.strip_prefix("~/") {
        match home {
            Some(home) => home.join(rest),
            None => return Vec::new(),
        }
    } else if folder.starts_with('/') {
        std::path::PathBuf::from(folder)
    } else {
        root.join(folder)
    };
    let Ok(entries) = std::fs::read_dir(base) else {
        return Vec::new();
    };
    let mut found: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            let visible = !name.starts_with('.') || part.starts_with('.');
            (visible && name.starts_with(part)).then(|| {
                let folder_mark = if entry.path().is_dir() { "/" } else { "" };
                format!("{folder}{name}{folder_mark}")
            })
        })
        // Gathered in full, then sorted, so the common prefix is right; the
        // bound only stops a pathological folder.
        .take(10_000)
        .collect();
    found.sort();
    found
}

/// The prompt box title's note of waiting attachments.
pub fn chip(attachments: &[Ran]) -> Option<String> {
    match attachments {
        [] => None,
        [one] => Some(format!("+ {} ({})", one.command, one.summary())),
        many => Some(format!("+{} attached", many.len())),
    }
}

/// The text sent to the vendor and the text shown, for a draft with `!`
/// outputs attached. Outputs lose their beginnings until the wire fits.
pub fn with_attachments(draft: &str, attachments: &[Ran], limit: usize) -> (String, String) {
    let mut outputs: Vec<String> = attachments.iter().map(|a| a.output.clone()).collect();
    let build = |outputs: &[String]| {
        let mut wire = draft.to_owned();
        for (attachment, output) in attachments.iter().zip(outputs) {
            let fence = fence_for(output);
            let body = output.strip_suffix('\n').unwrap_or(output);
            wire.push_str(&format!(
                "\n\nOutput of `{}` ({}):\n{fence}\n{body}\n{fence}",
                attachment.command,
                attachment.summary()
            ));
        }
        wire
    };
    let mut wire = build(&outputs);
    while wire.len() > limit {
        let over = wire.len() - limit;
        let Some(output) = outputs.iter_mut().find(|o| o.len() > CUT.len()) else {
            break;
        };
        let body = output.strip_prefix(CUT).unwrap_or(output);
        let drop = body.ceil_char_boundary((over + CUT.len()).min(body.len()));
        *output = format!("{CUT}{}", &body[drop..]);
        wire = build(&outputs);
    }
    let display = attachments.iter().fold(draft.to_owned(), |text, a| {
        format!("{text}\n\n[+ {}]", a.command)
    });
    (wire, display)
}

/// Three backticks, or one more than the longest run in `text`.
fn fence_for(text: &str) -> String {
    let longest = text.split(|c| c != '`').map(str::len).max().unwrap_or(0);
    "`".repeat(longest.max(2) + 1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shell::Status;
    fn ran(command: &str, output: &str, code: i32) -> Ran {
        Ran {
            command: command.into(),
            status: Status::Exited(code),
            output: output.into(),
        }
    }
    #[test]
    fn the_chip_names_one_attachment_or_counts_several() {
        assert_eq!(chip(&[]), None);
        assert_eq!(
            chip(&[ran("git status", "", 0)]).as_deref(),
            Some("+ git status (exit 0)")
        );
        assert_eq!(
            chip(&[ran("a", "", 0), ran("b", "", 1)]).as_deref(),
            Some("+2 attached")
        );
    }
    #[test]
    fn the_vendor_gets_the_output_and_the_transcript_a_marker() {
        let (wire, display) =
            with_attachments("explain", &[ran("git status", "clean\n", 0)], 1 << 16);
        assert_eq!(
            wire,
            "explain\n\nOutput of `git status` (exit 0):\n```\nclean\n```"
        );
        assert_eq!(display, "explain\n\n[+ git status]");
    }
    #[test]
    fn the_fence_outgrows_backticks_in_the_output() {
        let (wire, _) = with_attachments(
            "x",
            &[ran("cat a.md", "```rust\nfn main() {}\n```\n", 0)],
            1 << 16,
        );
        assert!(wire.contains("\n````\n```rust"), "{wire}");
        assert!(wire.ends_with("```\n````"), "{wire}");
    }
    #[test]
    fn finds_the_mention_being_typed() {
        assert_eq!(mention_at("@ma", 3), Some((0, "ma")));
        assert_eq!(mention_at("see @src/ma", 11), Some((4, "src/ma")));
        assert_eq!(mention_at("héllo @ma", "héllo @ma".len()), Some((7, "ma")));
        assert_eq!(
            mention_at("me@example.com", 14),
            None,
            "mid-word @ is not a mention"
        );
        assert_eq!(mention_at("@ma rest", 8), None, "the cursor left the token");
    }
    #[test]
    fn mentions_quote_paths_with_spaces() {
        assert_eq!(mention("src/main.rs"), "@src/main.rs ");
        assert_eq!(mention("my notes.md"), "@\"my notes.md\" ");
    }
    #[test]
    fn tab_completes_commands_paths_and_mentions() {
        let dir = octet_testkit::TempDir::new("octet-tab");
        std::fs::create_dir_all(dir.path().join("src/bin")).unwrap();
        std::fs::write(dir.path().join("src/main.rs"), "").unwrap();
        std::fs::write(dir.path().join("src/model.rs"), "").unwrap();
        let root = dir.path();
        let tab = |text: &str| super::tab(text, text.len(), root, None);
        assert!(
            matches!(tab("/rem"), Tab::Replace { start: 0, ref text, .. } if text == "/remote-control ")
        );
        assert!(matches!(
            tab("/re"),
            Tab::Popup(Completion {
                kind: Kind::Command,
                ..
            })
        ));
        assert!(
            matches!(tab("look at src/ma"), Tab::Replace { start: 8, ref text, .. } if text == "src/main.rs")
        );
        assert!(
            matches!(tab("src/b"), Tab::Replace { start: 0, ref text, .. } if text == "src/bin/")
        );
        assert!(
            matches!(tab("src/m"), Tab::Popup(Completion { kind: Kind::Path, ref items, .. }) if items.len() == 2)
        );
        assert!(matches!(tab("héllo @ma"), Tab::Mention(7)));
        assert!(matches!(tab("plain words"), Tab::Nothing));
        assert!(
            matches!(tab("nowhere/zz"), Tab::Nothing),
            "missing folders complete to nothing"
        );
        assert!(
            matches!(tab("/usr/li"), Tab::Replace { ref text, .. } if text.starts_with("/usr/lib")),
            "a second / makes it a path, not a command"
        );
    }
    #[test]
    fn several_matches_fill_their_prefix_and_open_the_popup() {
        let dir = octet_testkit::TempDir::new("octet-tab-prefix");
        std::fs::create_dir_all(dir.path().join("src")).unwrap();
        std::fs::write(dir.path().join("src/model.rs"), "").unwrap();
        std::fs::write(dir.path().join("src/modem.rs"), "").unwrap();
        let tab = |text: &str| super::tab(text, text.len(), dir.path(), None);
        assert!(matches!(
            tab("src/m"),
            Tab::Replace { ref text, popup: Some(Completion { kind: Kind::Path, ref items, .. }), .. }
                if text == "src/mode" && items.len() == 2
        ));
        assert!(matches!(
            tab("/r"),
            Tab::Replace { ref text, popup: Some(Completion { kind: Kind::Command, .. }), .. }
                if text == "/re"
        ));
    }
    #[test]
    fn every_match_counts_before_sorting() {
        let dir = octet_testkit::TempDir::new("octet-tab-many");
        std::fs::create_dir_all(dir.path()).unwrap();
        for i in 0..250 {
            std::fs::write(dir.path().join(format!("aa{i:03}")), "").unwrap();
        }
        std::fs::write(dir.path().join("ab"), "").unwrap();
        assert_eq!(path_candidates(dir.path(), None, "a").len(), 251);
    }
    #[test]
    fn outputs_are_cut_from_the_start_to_fit_the_limit() {
        let output = "x".repeat(5000) + "END\n";
        let (wire, _) = with_attachments("q", &[ran("big", &output, 0)], 1000);
        assert!(wire.len() <= 1000, "{}", wire.len());
        assert!(wire.contains(CUT.trim_end()));
        assert!(wire.contains("END"));
    }
}
