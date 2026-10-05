//! The prompt box's attachment and completion logic, free of terminal I/O.
use crate::shell::{Ran, CUT};

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
    fn outputs_are_cut_from_the_start_to_fit_the_limit() {
        let output = "x".repeat(5000) + "END\n";
        let (wire, _) = with_attachments("q", &[ran("big", &output, 0)], 1000);
        assert!(wire.len() <= 1000, "{}", wire.len());
        assert!(wire.contains(CUT.trim_end()));
        assert!(wire.contains("END"));
    }
}
