use super::*;

#[test]
fn copy_keeps_the_last_reply_until_new_text_arrives() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.event(Event::Started);
    app.event(Event::Text("first".into()));
    app.event(Event::Finished {
        outcome: octet_core::Outcome::Completed,
    });
    app.event(Event::Started);
    assert_eq!(app.last_reply(), Some("first"), "the reply on screen");
    app.event(Event::Text("second".into()));
    assert_eq!(app.last_reply(), Some("second"));
}
#[test]
fn copy_takes_the_whole_reply_with_its_tabs() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.event(Event::Started);
    app.event(Event::Text("one".into()));
    app.event(Event::Tool("Read\nsrc/main.rs".into()));
    app.event(Event::Text("two\tcolumns".into()));
    assert_eq!(app.last_reply(), Some("one\n\ntwo\tcolumns"));
    app.attach(crate::shell::Ran {
        command: "cat Makefile".into(),
        status: crate::shell::Status::Exited(0),
        output: "all:\n\tcargo build\n".into(),
    });
    assert_eq!(app.attachments[0].output, "all:\n\tcargo build\n");
}
#[test]
fn a_new_connection_settles_the_old_sessions_command_and_popup() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.shell_running = true;
    app.completion = Some(crate::composer::Completion {
        kind: crate::composer::Kind::File,
        items: Vec::new(),
        selected: 0,
        start: 0,
    });
    app.connection(&config, "journal-2".into());
    assert!(!app.shell_running, "the old session's command is gone");
    assert!(app.completion.is_none());
    assert!(
        app.entries_text()
            .contains("The running command stopped when the session changed"),
        "{}",
        app.entries_text()
    );
}
#[test]
fn a_shell_that_cannot_start_is_an_error_entry() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.error("Cannot run /no/such/shell: No such file or directory");
    assert!(app.last_role() == Some(Role::Error));
    assert!(app.notice.starts_with("Cannot run"));
}
#[test]
fn shell_output_is_cleaned_and_attachments_stay_bounded() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    let ran = crate::shell::Ran {
        command: "ls -G".into(),
        status: crate::shell::Status::Exited(1),
        output: "\x1b[31mred\x1b[0m\r\nplain\n".into(),
    };
    app.shell_output(&ran);
    let text = app.entries_text();
    assert!(text.contains("$ ls -G\nred\nplain\nexit 1"), "{text:?}");
    assert!(!text.contains('\x1b'));
    app.attach(ran);
    assert!(!app.attachments[0].output.contains('\x1b'));
    let big = crate::shell::Ran {
        command: "big".into(),
        status: crate::shell::Status::Exited(0),
        output: "x".repeat(crate::shell::OUTPUT_LIMIT),
    };
    app.attach(big);
    assert_eq!(app.attachments.len(), 1, "the oldest attachment is dropped");
    assert_eq!(app.attachments[0].command, "big");
    assert_eq!(
        app.notice,
        "Dropped the oldest attachment to stay within 32 KiB"
    );
}
#[test]
fn history_skips_repeats_and_keeps_fifty() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.remember("same".into());
    app.remember("same".into());
    assert_eq!(app.history.len(), 1);
    for i in 0..60 {
        app.remember(format!("p{i}"));
    }
    assert_eq!(app.history.len(), 50);
    assert_eq!(app.history.back().map(String::as_str), Some("p59"));
}
#[test]
fn a_full_prompt_warns_instead_of_inserting() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert!(!app.insert_or_warn(&"x".repeat(octet_core::PROMPT_LIMIT + 1)));
    assert_eq!(app.notice, "Prompt limit reached");
    assert!(app.insert_or_warn("ok"));
}
#[test]
fn a_finished_turn_clears_the_cancelling_notice() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.event(Event::Started);
    app.notice = CANCELLING.into();
    app.event(Event::Finished {
        outcome: octet_core::Outcome::Interrupted,
    });
    assert_eq!(app.notice, "");
}
#[test]
fn transcript_memory_is_bounded() {
    let c = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut a = App::new(&c, "journal".into());
    for _ in 0..100 {
        a.event(Event::User("x".repeat(64 * 1024)));
        a.event(Event::Text("界".repeat(64 * 1024)));
    }
    assert!(a.bytes <= MAX_BYTES);
    assert!(a.entries.len() <= 160);
    assert!(!a.transcript(60, 20).is_empty());
}
