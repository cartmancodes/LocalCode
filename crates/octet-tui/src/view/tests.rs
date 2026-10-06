use super::*;
use octet_core::Event;
use ratatui::{backend::TestBackend, Terminal};
fn screen(width: u16, height: u16, app: &mut App) -> Vec<String> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| draw(frame, app)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
        .collect()
}
#[test]
fn following_the_latest_cancels_a_pending_catalog_scroll() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.conn.models = (0..30)
        .map(|i| octet_core::ModelInfo {
            selection: format!("m{i}"),
            id: Some(format!("m{i}")),
            name: format!("Model {i}"),
            description: String::new(),
        })
        .collect();
    app.show_models(1);
    app.follow_latest();
    screen(120, 36, &mut app);
    assert_eq!(
        app.chat.scroll, 0,
        "Ctrl+End before the next frame still wins"
    );
}
#[test]
fn the_popup_keeps_the_selected_row_in_sight_on_a_short_screen() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.composer.completion = Some(crate::composer::Completion {
        kind: crate::composer::Kind::File,
        items: (0..8).map(|i| format!("file{i}.rs")).collect(),
        selected: 7,
        start: 0,
    });
    let rows = screen(80, 14, &mut app);
    assert!(
        rows.iter().any(|row| row.contains(" › file7.rs")),
        "{}",
        rows.join("\n")
    );
}
#[test]
fn the_popup_lists_suggestions_above_the_prompt() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.composer.completion = Some(crate::composer::Completion {
        kind: crate::composer::Kind::File,
        items: vec!["src/main.rs".into(), "src/model.rs".into()],
        selected: 1,
        start: 0,
    });
    let rows = screen(100, 30, &mut app);
    assert!(rows.iter().any(|row| row.contains("   src/main.rs")));
    assert!(rows.iter().any(|row| row.contains(" › src/model.rs")));
    app.composer.completion.as_mut().unwrap().items.clear();
    let rows = screen(100, 30, &mut app);
    assert!(rows.iter().any(|row| row.contains("Indexing files…")));
}
#[test]
fn sidebar_lines_fit_the_panel_without_wrapping() {
    // Every sidebar line starts with a space; a wrapped remainder would
    // start in the first column, against the border.
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    let rows = screen(120, 48, &mut app);
    let title = rows.iter().find(|row| row.contains("╭ Workspace")).unwrap();
    let border = title.chars().position(|c| c == '╭').unwrap();
    let first = border + 1;
    let inside: Vec<&String> = rows
        .iter()
        .filter(|row| row.chars().nth(border) == Some('│'))
        .collect();
    assert!(inside.len() > 20, "sidebar not drawn");
    for row in inside {
        let start = row.chars().nth(first).unwrap();
        assert_eq!(start, ' ', "wrapped sidebar line: {row}");
    }
}
#[test]
fn an_empty_composer_gives_rows_back_to_the_conversation() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.event(Event::User("hello".into()));
    let rows = screen(44, 16, &mut app);
    // The composer's top border sits 4 rows above the status line.
    assert!(rows[16 - 6].starts_with(" ╭ Prompt"), "{}", rows[16 - 6]);
}
#[test]
fn help_keeps_its_last_line_on_narrow_and_short_terminals() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.overlay.help = true;
    for (width, height) in [(100, 30), (75, 40), (70, 40), (60, 40), (100, 32)] {
        let rows = screen(width, height, &mut app);
        assert!(
            rows.iter().any(|row| row.contains("Esc or F1 closes help")),
            "{width}x{height}:\n{}",
            rows.join("\n")
        );
    }
}
#[test]
fn help_names_the_command_palette_key() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.overlay.help = true;
    let rows = screen(100, 32, &mut app);
    assert!(
        rows.iter().any(|row| row.contains("Ctrl+P commands")),
        "{}",
        rows.join("\n")
    );
    assert!(rows.iter().any(|row| row.contains("Esc or F1 closes help")));
}
#[test]
fn the_draft_shows_at_the_minimum_size() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert!(app.composer.editor.insert("VISIBLE"));
    for width in [38, 80] {
        let rows = screen(width, 12, &mut app);
        assert!(
            rows.iter().any(|row| row.contains("VISIBLE")),
            "{width}x12:\n{}",
            rows.join("\n")
        );
    }
}
#[test]
fn the_palette_shows_every_command_and_its_keys() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.overlay.palette = true;
    let rows = screen(80, 24, &mut app);
    let mut columns = Vec::new();
    for (name, description) in palette_entries() {
        // " /mode " must not match the "/model" row.
        let row = rows
            .iter()
            .find(|row| row.contains(&format!(" {name} ")))
            .unwrap_or_else(|| panic!("{name} missing"));
        assert!(row.contains(description), "{name}: description cut: {row}");
        // Columns, not bytes: the selected row's "›" is three bytes wide.
        columns.push(
            row.find(description)
                .map(|byte| row[..byte].chars().count()),
        );
    }
    assert!(
        columns.windows(2).all(|pair| pair[0] == pair[1]),
        "descriptions start in different columns: {columns:?}"
    );
    assert!(rows.iter().any(|row| row.contains("Esc close")));
}
#[test]
fn empty_conversation_leaves_the_centre_blank() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    for (width, height) in [(80, 24), (60, 20)] {
        let mut app = App::new(&config, "journal".into());
        app.monochrome = false;
        app.event(Event::Ready {
            session: "demo".into(),
        });
        let rows = screen(width, height, &mut app);
        // Rows between the 3-row banner (after the margin) and the empty
        // 4-row composer, the status line and the bottom margin.
        for row in &rows[4..rows.len() - 6] {
            let inside: String = row.chars().skip(1).take(width as usize - 2).collect();
            assert!(inside.trim().is_empty(), "{width}×{height}: {row:?}");
        }
    }
}
#[test]
fn header_banner_shows_mini_version_mode_activity_and_workspace() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    for (width, height) in [(160, 42), (80, 24), (60, 20), (40, 12)] {
        let mut app = App::new(&config, "journal".into());
        app.monochrome = false;
        for event in [
            Event::Ready {
                session: "demo".into(),
            },
            Event::User("fix the failing test".into()),
            Event::Started,
            Event::Tool("Bash\n{}".into()),
        ] {
            app.event(event);
        }
        let rows = screen(width, height, &mut app);
        // The header starts inside the one-cell margin.
        for row in &rows[1..4] {
            let mini: String = row.chars().skip(1).take(9).collect();
            assert_eq!(mini, "▀".repeat(9), "{width}×{height}: {row}");
        }
        let at = |row: usize| format!("{width}×{height}: {}", rows[row]);
        let version = format!("Octet v{}", env!("CARGO_PKG_VERSION"));
        assert!(rows[1].contains(&version), "{}", at(1));
        assert!(rows[1].contains("working"), "{}", at(1));
        assert!(rows[2].contains("ask"), "{}", at(2));
        assert!(
            rows[3].contains("CODING") && rows[3].contains("/tmp"),
            "{}",
            at(3)
        );
    }
}
#[test]
fn permission_mode_stays_visible_on_narrow_headers() {
    let config = octet_core::Config {
        model: Some("gpt-5.5-codex-max-preview-long-name".into()),
        mode: octet_core::Mode::FullAccess,
        ..octet_core::Config::new(octet_core::Engine::Codex, "codex", "/tmp")
    };
    for width in [80, 60, 40, 38] {
        let mut app = App::new(&config, "journal".into());
        app.monochrome = false;
        let rows = screen(width, 24, &mut app);
        assert!(
            rows[2].contains("full-access"),
            "{width} columns: {}",
            rows[2]
        );
    }
}
#[test]
fn status_shows_in_full_on_narrow_terminals() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    for width in [59, 50, 40] {
        let mut app = App::new(&config, "journal".into());
        app.monochrome = false;
        for event in [
            Event::Ready {
                session: "demo".into(),
            },
            Event::Started,
            Event::Finished {
                outcome: octet_core::Outcome::Interrupted,
            },
        ] {
            app.event(event);
        }
        let rows = screen(width, 12, &mut app);
        assert!(rows[1].contains("● interrupted"), "{width}: {}", rows[1]);
    }
}
#[test]
fn no_color_keeps_the_text_header() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.monochrome = true;
    let rows = screen(80, 24, &mut app);
    let mark = format!("◇ Octet v{}", env!("CARGO_PKG_VERSION"));
    assert!(rows[1].contains(&mark), "{}", rows[1]);
    assert!(!rows[1..4].iter().any(|row| row.contains('▀')));
}
#[test]
fn mascot_tracks_activity_and_keeps_errors_visible_after_disconnect() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.monochrome = false;
    for (event, expected) in [
        (
            Event::Ready {
                session: "demo".into(),
            },
            "IDLE",
        ),
        (Event::Started, "THINKING"),
        (
            Event::Tool("Read\n{\"file_path\":\"src/main.rs\"}".into()),
            "SEARCHING",
        ),
        (
            Event::Tool("Bash\n{\"command\":\"cargo check\"}".into()),
            "CODING",
        ),
        (Event::Tool("dispatch_subagent\n{}".into()), "DELEGATING"),
        (
            Event::Approval {
                id: 1,
                detail: "Write src/main.rs".into(),
            },
            "APPROVAL",
        ),
        (Event::ApprovalClosed(1), "DELEGATING"),
        (
            Event::Finished {
                outcome: octet_core::Outcome::Completed,
            },
            "SUCCESS",
        ),
        (
            Event::Finished {
                outcome: octet_core::Outcome::Interrupted,
            },
            "SLEEPING",
        ),
        (Event::Error("provider disconnected".into()), "ERROR"),
        (Event::Stopped, "ERROR"),
    ] {
        app.event(event);
        // The banner's third row starts with the activity name; the
        // transcript's own ERROR label must not satisfy this check.
        let rows = screen(160, 42, &mut app);
        let activity = rows[3].chars().skip(11).collect::<String>();
        assert!(
            activity.trim_start().starts_with(expected),
            "mascot should show {expected}: {}",
            rows[3]
        );
    }
}
#[test]
fn renders_narrow_wide_and_approval_without_panics() {
    for (w, h) in [(30, 8), (40, 12), (80, 24), (120, 36)] {
        let c = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp/project");
        let mut a = App::new(&c, "journal".into());
        let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
        t.draw(|f| draw(f, &mut a)).unwrap();
        a.event(Event::Approval {
            id: 7,
            detail: "edit hello.txt".into(),
        });
        t.draw(|f| draw(f, &mut a)).unwrap();
    }
}

#[test]
fn composer_grows_with_the_draft() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert_eq!(
        composer_height(app.composer.editor.layout(draft_width(80)).0.len()),
        4
    );
    assert!(app.composer.editor.insert("one\ntwo\nthree"));
    assert_eq!(
        composer_height(app.composer.editor.layout(draft_width(80)).0.len()),
        6
    );
}
#[test]
fn composer_counts_wrapped_rows() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    // 80 columns leave 74 for text: 100 characters wrap onto a second row.
    assert!(app.composer.editor.insert(&"x".repeat(100)));
    assert_eq!(
        composer_height(app.composer.editor.layout(draft_width(80)).0.len()),
        5
    );
}
#[test]
fn composer_height_is_capped() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert!(app.composer.editor.insert(&"line\n".repeat(40)));
    assert_eq!(
        composer_height(app.composer.editor.layout(draft_width(80)).0.len()),
        7
    );
}

mod model_detail_tests {
    use super::*;
    fn app() -> App {
        App::new(
            &octet_core::Config {
                model: Some("sonnet".into()),
                ..octet_core::Config::new(octet_core::Engine::Claude, "claude", "/tmp")
            },
            "/tmp/journal".into(),
        )
    }
    #[test]
    fn full_model_details_survive_long_ids_and_reconnection_clears_metadata() {
        let mut a = app();
        let id = format!("provider-{}-version", "long".repeat(40));
        a.event(Event::Models(vec![octet_core::ModelInfo {
            selection: "sonnet".into(),
            id: Some(id.clone()),
            name: "Complete Provider Model Name".into(),
            description: "Provider details".into(),
        }]));
        assert!(a.model_details().contains("not yet reported"));
        a.event(Event::ModelSelected(id.clone()));
        a.show_models(1);
        let details = a
            .chat
            .entries
            .iter()
            .map(|e| e.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(details.contains(&id));
        assert!(details.contains("Complete Provider Model Name"));
        assert!(details.contains("Requested selection: sonnet"));
        a.connection(
            &octet_core::Config::new(octet_core::Engine::Codex, "codex", "/tmp"),
            "/tmp/new".into(),
        );
        assert!(a.conn.models.is_empty());
        assert!(a.conn.resolved_model.is_none());
        assert!(!a.model_details().contains(&id));
    }
    #[test]
    fn large_catalog_is_paged_without_evicting_current_details() {
        let mut a = app();
        a.event(Event::Models(
            (0..256)
                .map(|n| octet_core::ModelInfo {
                    selection: format!("model-{n}"),
                    id: Some(format!("full-id-{n}")),
                    name: format!("Name {n}"),
                    description: String::new(),
                })
                .collect(),
        ));
        a.show_models(13);
        let visible = a
            .visible_lines(72, 12)
            .iter()
            .map(|line| line.to_string())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(visible.contains("claude catalog"));
        assert!(visible.contains("256 entries"));
        assert!(visible.contains("Name 240"));
        assert!(a.chat.scroll > 0);
        assert!(a
            .chat
            .entries
            .iter()
            .any(|e| e.text.contains("full-id-255")));
        assert!(a
            .chat
            .entries
            .iter()
            .any(|e| e.text.contains("Requested selection")));
        assert!(!a
            .chat
            .entries
            .iter()
            .any(|e| e.text.contains("full-id-0\n")));
        let count = a.chat.entries.len();
        a.show_models(usize::MAX);
        assert_eq!(a.chat.entries.len(), count + 1);
    }
    #[test]
    fn stopped_session_clears_pending_mode() {
        let c = octet_core::Config::new(octet_core::Engine::Claude, "claude", "/tmp");
        let mut a = App::new(&c, "journal".into());
        a.conn.mode_pending = Some(octet_core::Mode::Auto);
        a.event(Event::ModeChanged(octet_core::Mode::Auto));
        assert_eq!(
            (a.conn.mode, a.conn.mode_pending),
            (octet_core::Mode::Auto, None)
        );
        a.conn.mode_pending = Some(octet_core::Mode::Ask);
        a.event(Event::Stopped);
        assert_eq!(
            (a.conn.mode, a.conn.mode_pending),
            (octet_core::Mode::Auto, None)
        );
    }
    #[test]
    fn header_shows_confirmed_and_pending_mode() {
        let c = octet_core::Config {
            mode: octet_core::Mode::Auto,
            ..octet_core::Config::new(octet_core::Engine::Codex, "codex", "/tmp/project")
        };
        let mut a = App::new(&c, "journal".into());
        let mut t = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        let screen = |t: &ratatui::Terminal<ratatui::backend::TestBackend>| {
            t.backend()
                .buffer()
                .content()
                .iter()
                .map(|c| c.symbol())
                .collect::<String>()
        };
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("auto"));
        assert_eq!(mode_chip(&a).style.fg, Some(ACCENT));
        a.conn.mode_pending = Some(octet_core::Mode::Ask);
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("ask…"));
        a.event(Event::ModeChanged(octet_core::Mode::FullAccess));
        assert_eq!(mode_chip(&a).style.fg, Some(AMBER));
        assert!(COMMANDS.iter().any(|spec| spec.name == "/mode"));
    }
}
