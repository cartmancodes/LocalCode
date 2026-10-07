//! What the next connection looks like after a session ends: the
//! configuration, whether the interface is kept, and what it says. Pure, so
//! each kind of exit is tested without a terminal.
use crate::Exit;
use octet_core::{Config, Engine, Mode};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};

/// What the interface knew about the session that just ended.
pub(crate) struct Ended<'a> {
    /// The vendor session ID; empty if the vendor never named one.
    pub(crate) session: &'a str,
    pub(crate) journal: &'a Path,
    /// The last mode the vendor confirmed.
    pub(crate) mode: Mode,
    pub(crate) effort: Option<String>,
}

/// The next connection.
pub(crate) struct Plan {
    pub(crate) config: Config,
    /// Keep the interface (transcript, history); false opens a fresh one.
    pub(crate) keep_app: bool,
    /// Pause an active goal first, naming why: a new connection never
    /// continues a goal by itself.
    pub(crate) pause: Option<&'static str>,
    /// Notes for the kept interface.
    pub(crate) notes: Vec<String>,
    /// A note to open a fresh interface with.
    pub(crate) opening: Option<String>,
}

/// The plan after `exit`, or `None` to quit. `binaries` remembers each
/// provider's CLI for this run.
pub(crate) fn plan(
    exit: Exit,
    config: &Config,
    ended: &Ended,
    binaries: &mut HashMap<Engine, PathBuf>,
) -> Option<Plan> {
    let mut config = config.clone();
    // Carry the last vendor-confirmed mode, never an unconfirmed pending one.
    config.mode = ended.mode;
    config.effort.clone_from(&ended.effort);
    settle_fork(&mut config, ended.session);
    let kept = |config: Config, pause: Option<&'static str>, notes: Vec<String>| Plan {
        config,
        keep_app: true,
        pause,
        notes,
        opening: None,
    };
    let resume = resume_id(ended.session, config.engine);
    Some(match exit {
        Exit::Quit => return None,
        Exit::Model(selection) => {
            let cross_provider = selection.provider != config.engine;
            let binary = binaries.get(&selection.provider).cloned();
            let next = selection.configure(&config, ended.session, binary);
            binaries.insert(next.engine, next.binary.clone());
            let mut notes: Vec<String> = dropped_effort(
                config.effort.as_deref(),
                next.engine,
                next.effort.as_deref(),
            )
            .into_iter()
            .collect();
            notes.push(format!(
                "Model → {} / {}. {} Previous journal: {}",
                next.engine,
                next.model.as_deref().unwrap_or("vendor default"),
                if cross_provider {
                    "New provider context; earlier displayed messages are not sent to this provider."
                } else {
                    "Resuming the same vendor context."
                },
                ended.journal.display()
            ));
            kept(next, Some("model switch"), notes)
        }
        Exit::Mode(mode) => {
            let note = full_access_notice(mode, config.engine, ended.session);
            config.mode = mode;
            if resume.is_some() {
                config.resume = resume;
            }
            kept(config, Some("mode switch"), vec![note])
        }
        Exit::Effort(level) => {
            let note = format!(
                "Reasoning effort → {}. Reconnecting to the same session…",
                level.as_deref().unwrap_or("vendor default")
            );
            config.effort = level;
            if resume.is_some() {
                config.resume = resume;
            }
            kept(config, Some("effort change"), vec![note])
        }
        Exit::Fork => {
            let note = format!("Forking from {}…", ended.session);
            config.resume = Some(ended.session.to_owned());
            config.fork = true;
            kept(config, Some("fork"), vec![note])
        }
        Exit::Resume {
            engine,
            session,
            model,
        } => {
            let binary = binaries
                .get(&engine)
                .cloned()
                .unwrap_or_else(|| PathBuf::from(engine.provider().default_binary));
            binaries.insert(engine, binary.clone());
            let resumed = format!(
                "Resumed {engine} session {session}. Previous journal: {}",
                ended.journal.display()
            );
            let effort = resume_into(&mut config, engine, session, model, binary);
            Plan {
                config,
                keep_app: false,
                pause: Some("resume"),
                notes: Vec::new(),
                opening: Some(match effort {
                    Some(effort) => format!("{resumed}\n{effort}"),
                    None => resumed,
                }),
            }
        }
        Exit::New => {
            config.resume = None;
            config.fork = false;
            Plan {
                config,
                keep_app: false,
                pause: None,
                notes: Vec::new(),
                opening: None,
            }
        }
        Exit::Reconnect => {
            if resume.is_some() {
                config.resume = resume;
            }
            let note = reconnect_notice(config.resume.is_some(), ended.journal);
            kept(config, Some("reconnect"), vec![note])
        }
    })
}

/// Points `config` at vendor session `session` of `engine` for `/resume`:
/// a resume, never a fork, with an effort the provider takes. The notice
/// when the effort was dropped.
pub(crate) fn resume_into(
    config: &mut Config,
    engine: Engine,
    session: String,
    model: Option<String>,
    binary: PathBuf,
) -> Option<String> {
    let before = config.effort.take();
    config.effort = before
        .clone()
        .filter(|level| engine.check_effort(level).is_ok());
    let notice = dropped_effort(before.as_deref(), engine, config.effort.as_deref());
    config.engine = engine;
    config.binary = binary;
    config.model = model;
    config.resume = Some(session);
    config.fork = false;
    notice
}

/// The notice when a switch to `engine` dropped effort `before`.
pub(crate) fn dropped_effort(
    before: Option<&str>,
    engine: Engine,
    after: Option<&str>,
) -> Option<String> {
    match (before, after) {
        (Some(level), None) => Some(format!(
            "{} does not take effort {level}; using its default",
            engine.title()
        )),
        _ => None,
    }
}

/// A fork happens once: when the vendor has named the new session, later
/// reconnects resume it. Until then (Claude names a fork with its first
/// turn) the next connection forks again, so the original stays untouched.
pub(crate) fn settle_fork(config: &mut Config, session: &str) {
    if !session.is_empty() && config.resume.as_deref() != Some(session) {
        config.fork = false;
    }
}

/// The vendor session a reconnect resumes, once there is one.
fn resume_id(session: &str, engine: Engine) -> Option<String> {
    (!session.is_empty() && engine.is_vendor()).then(|| session.to_owned())
}

/// The retained screen spans two journals after a reconnect; say where the
/// earlier part is, because /export and /session cover only the new one.
pub(crate) fn reconnect_notice(resumed: bool, previous: &Path) -> String {
    format!(
        "{} Earlier messages stay visible. Previous journal: {}",
        if resumed {
            "Reconnecting to the same vendor session."
        } else {
            "Reconnecting. No vendor session ID yet, so the vendor starts fresh."
        },
        previous.display()
    )
}

/// Says whether a full-access change resumes the vendor session or starts over.
pub(crate) fn full_access_notice(mode: Mode, engine: Engine, session: &str) -> String {
    let change = if mode == Mode::FullAccess {
        "Full access: the agent can run any command and edit any file without asking.".to_owned()
    } else {
        format!("Leaving full access for {}.", mode.label())
    };
    let next = if engine.offline() {
        "Restarting the offline demo…"
    } else if session.is_empty() {
        "Starting a new session (no session ID yet)…"
    } else {
        "Reconnecting to the same session…"
    };
    format!("{change} {next}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use octet_core::model::Selection;

    fn codex() -> Config {
        let mut config = Config::new(Engine::CODEX, "codex", "/tmp");
        config.effort = Some("minimal".into());
        config
    }
    fn ended<'a>(session: &'a str) -> Ended<'a> {
        Ended {
            session,
            journal: Path::new("/j/old.jsonl"),
            mode: Mode::Auto,
            effort: Some("minimal".into()),
        }
    }
    fn plan_for(exit: Exit, config: &Config, session: &str) -> Plan {
        plan(exit, config, &ended(session), &mut HashMap::new()).unwrap()
    }

    #[test]
    fn quit_has_no_plan() {
        assert!(plan(Exit::Quit, &codex(), &ended("t-1"), &mut HashMap::new()).is_none());
    }

    #[test]
    fn every_plan_carries_the_confirmed_mode_and_effort() {
        let plan = plan_for(
            Exit::Reconnect,
            &Config::new(Engine::CODEX, "codex", "/tmp"),
            "t-1",
        );
        assert_eq!(plan.config.mode, Mode::Auto);
        assert_eq!(plan.config.effort.as_deref(), Some("minimal"));
    }

    #[test]
    fn reconnect_resumes_a_named_session_and_keeps_the_view() {
        let plan = plan_for(Exit::Reconnect, &codex(), "t-1");
        assert_eq!(plan.config.resume.as_deref(), Some("t-1"));
        assert!(plan.keep_app);
        assert_eq!(plan.pause, Some("reconnect"));
        assert!(plan.notes[0].starts_with("Reconnecting to the same vendor session."));
        let fresh = plan_for(Exit::Reconnect, &codex(), "");
        assert_eq!(fresh.config.resume, None);
    }

    #[test]
    fn new_starts_fresh_without_a_fork() {
        let mut config = codex();
        config.resume = Some("t-1".into());
        config.fork = true;
        let plan = plan_for(Exit::New, &config, "");
        assert_eq!(plan.config.resume, None);
        assert!(!plan.config.fork);
        assert!(!plan.keep_app);
        assert_eq!(plan.pause, None);
    }

    #[test]
    fn a_mode_change_resumes_the_session() {
        let plan = plan_for(Exit::Mode(Mode::FullAccess), &codex(), "t-1");
        assert_eq!(plan.config.mode, Mode::FullAccess);
        assert_eq!(plan.config.resume.as_deref(), Some("t-1"));
        assert!(plan.notes[0].starts_with("Full access:"));
    }

    #[test]
    fn an_effort_change_resumes_with_the_new_level() {
        let plan = plan_for(Exit::Effort(Some("max".into())), &codex(), "t-1");
        assert_eq!(plan.config.effort.as_deref(), Some("max"));
        assert_eq!(plan.config.resume.as_deref(), Some("t-1"));
    }

    #[test]
    fn a_fork_opens_the_session_as_a_fork() {
        let plan = plan_for(Exit::Fork, &codex(), "t-1");
        assert_eq!(plan.config.resume.as_deref(), Some("t-1"));
        assert!(plan.config.fork);
        assert_eq!(plan.notes, ["Forking from t-1…"]);
    }

    #[test]
    fn a_model_switch_to_claude_drops_an_effort_it_does_not_take() {
        let selection = Selection {
            provider: Engine::CLAUDE,
            model: Some("opus".into()),
        };
        let mut binaries = HashMap::new();
        let plan = plan(
            Exit::Model(selection),
            &codex(),
            &ended("t-1"),
            &mut binaries,
        )
        .unwrap();
        assert_eq!(plan.config.engine, Engine::CLAUDE);
        assert_eq!(plan.config.effort, None);
        assert_eq!(
            plan.notes[0],
            "Claude does not take effort minimal; using its default"
        );
        assert!(plan.notes[1].starts_with("Model → claude / opus. New provider context"));
        assert!(binaries.contains_key(&Engine::CLAUDE));
    }

    #[test]
    fn resume_opens_a_fresh_view_and_never_forks() {
        let mut config = codex();
        config.resume = Some("original".into());
        config.fork = true;
        let exit = Exit::Resume {
            engine: Engine::CLAUDE,
            session: "c-1".into(),
            model: None,
        };
        // The fork was never named, so it would otherwise stay pending.
        let plan = plan_for(exit, &config, "");
        assert_eq!(plan.config.resume.as_deref(), Some("c-1"));
        assert!(!plan.config.fork);
        assert!(!plan.keep_app);
        let opening = plan.opening.unwrap();
        assert!(
            opening.starts_with("Resumed claude session c-1."),
            "{opening}"
        );
        assert!(opening.ends_with("Claude does not take effort minimal; using its default"));
    }
}
