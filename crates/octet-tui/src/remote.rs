//! `/remote-control`: read-only checks for reaching this session from a
//! phone over tmux, Tailscale SSH and mosh. Nothing here changes system,
//! tmux or tailnet settings. Setup guide: docs/remote-control.md; the
//! evaluation behind it: docs/rust/remote-control-ssh.md.
use std::{process::Stdio, time::Duration};
use tokio::{
    net::TcpStream,
    process::Command,
    time::{timeout, timeout_at, Instant},
};

/// All commands share one deadline, so the interface waits at most this
/// long for them, plus `SSH_TIMEOUT` for the connection check after.
const CHECK_TIMEOUT: Duration = Duration::from_secs(2);
/// The SSH check connects to this host's own tailnet address.
const SSH_TIMEOUT: Duration = Duration::from_millis(500);
const SESSION: &str = "octet";

/// This host's place on the tailnet.
pub struct Tailnet {
    pub name: String,
    pub address: String,
    /// Found through the App Store app, which can't run Tailscale SSH.
    pub app_store: bool,
}

/// Where Octet runs relative to tmux.
pub enum Tmux {
    Outside,
    Session(String),
    /// `$TMUX` is set but tmux didn't answer in time.
    NoAnswer,
}

/// What the phone needs from this host, as found.
pub struct Checks {
    pub user: String,
    pub tmux: Tmux,
    /// Whether tmux is configured for 24-bit colour; None when unknown.
    pub tmux_rgb: Option<bool>,
    /// None when the tailscale CLI is missing or not connected.
    pub tailnet: Option<Tailnet>,
    /// SSH answered on the tailnet address.
    pub ssh: bool,
    /// mosh-server's version; None when it isn't installed.
    pub mosh: Option<(u32, u32, u32)>,
}

/// Runs one read-only check with its own timeout.
#[cfg(test)]
async fn run(program: &str, args: &[&str]) -> Option<String> {
    run_until(Instant::now() + CHECK_TIMEOUT, program, args).await
}

/// Runs one read-only check: stdin and stderr closed, killed at the
/// deadline. None when the program is missing, fails or overruns.
async fn run_until(deadline: Instant, program: &str, args: &[&str]) -> Option<String> {
    let child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .ok()?;
    let output = timeout_at(deadline, child.wait_with_output())
        .await
        .ok()?
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

fn parse_tailnet(status_json: &str, app_store: bool) -> Option<Tailnet> {
    let status: serde_json::Value = serde_json::from_str(status_json).ok()?;
    if status["BackendState"] != "Running" {
        return None;
    }
    Some(Tailnet {
        name: status["Self"]["DNSName"]
            .as_str()?
            .trim_end_matches('.')
            .to_owned(),
        address: status["Self"]["TailscaleIPs"][0].as_str()?.to_owned(),
        app_store,
    })
}

/// Whether tmux's server options give terminals 24-bit colour: an `RGB`
/// terminal feature (tmux 3.2+) or a `Tc` override (older tmux). None when
/// tmux reported neither option, so nothing is known.
fn rgb_configured(features: &str, overrides: &str) -> Option<bool> {
    if features.trim().is_empty() && overrides.trim().is_empty() {
        return None;
    }
    let colour = |text: &str, flag: &str| {
        text.lines()
            .any(|line| line.split(':').skip(1).any(|part| part.trim() == flag))
    };
    Some(colour(features, "RGB") || colour(overrides, "Tc") || colour(overrides, "RGB"))
}

fn parse_mosh_version(output: &str) -> Option<(u32, u32, u32)> {
    let version = output.split("(mosh ").nth(1)?.split(')').next()?;
    let mut parts = version.split('.').map(|part| part.parse().ok());
    Some((
        parts.next()??,
        parts.next()??,
        parts.next().flatten().unwrap_or(0),
    ))
}

/// The programs the checks run; tests substitute stand-ins.
struct Programs<'a> {
    tmux: &'a str,
    /// The open-source CLI first, then the App Store app's bundled CLI.
    tailscale: &'a [&'a str],
    mosh_server: &'a str,
}
#[cfg(not(test))]
const PROGRAMS: Programs<'static> = Programs {
    tmux: "tmux",
    // The App Store build keeps its CLI inside the app bundle.
    tailscale: &[
        "tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ],
    mosh_server: "mosh-server",
};
/// Under test, names that never exist, so results don't depend on the
/// machine running the tests.
#[cfg(test)]
const PROGRAMS: Programs<'static> = Programs {
    tmux: "octet-test-absent-tmux",
    tailscale: &["octet-test-absent-tailscale"],
    mosh_server: "octet-test-absent-mosh-server",
};

/// Runs every check concurrently, each bounded by the check timeout.
pub async fn probe() -> Checks {
    probe_with(&PROGRAMS, std::env::var_os("TMUX").is_some()).await
}

async fn probe_with(programs: &Programs<'_>, inside_tmux: bool) -> Checks {
    let deadline = Instant::now() + CHECK_TIMEOUT;
    let tmux = async {
        if !inside_tmux {
            return (Tmux::Outside, None);
        }
        let (session, features, overrides) = tokio::join!(
            run_until(deadline, programs.tmux, &["display-message", "-p", "#S"]),
            run_until(
                deadline,
                programs.tmux,
                &["show-options", "-s", "terminal-features"],
            ),
            run_until(
                deadline,
                programs.tmux,
                &["show-options", "-s", "terminal-overrides"],
            ),
        );
        let tmux = match session.map(|name| name.trim().to_owned()) {
            Some(name) if !name.is_empty() => Tmux::Session(name),
            _ => Tmux::NoAnswer,
        };
        let rgb = rgb_configured(
            features.as_deref().unwrap_or(""),
            overrides.as_deref().unwrap_or(""),
        );
        (tmux, rgb)
    };
    let tailnet = async {
        for (index, program) in programs.tailscale.iter().enumerate() {
            if let Some(json) = run_until(deadline, program, &["status", "--json"]).await {
                return parse_tailnet(&json, index > 0);
            }
        }
        None
    };
    let mosh = async {
        run_until(deadline, programs.mosh_server, &["--version"])
            .await
            .as_deref()
            .and_then(parse_mosh_version)
    };
    let ((tmux, tmux_rgb), tailnet, mosh) = tokio::join!(tmux, tailnet, mosh);
    let ssh = match &tailnet {
        Some(tailnet) => matches!(
            timeout(
                SSH_TIMEOUT,
                TcpStream::connect((tailnet.address.as_str(), 22))
            )
            .await,
            Ok(Ok(_))
        ),
        None => false,
    };
    Checks {
        user: std::env::var("USER").unwrap_or_else(|_| "you".into()),
        tmux,
        tmux_rgb,
        tailnet,
        ssh,
        mosh,
    }
}

fn mark(ok: bool, text: &str) -> String {
    format!("{} {text}", if ok { "[ok]" } else { "[!!]" })
}

/// The checks as notice text: one line per check, each problem with its fix,
/// then the exact phone commands once the host is reachable.
pub fn report(checks: &Checks) -> String {
    let session = match &checks.tmux {
        Tmux::Session(name) => name.as_str(),
        Tmux::Outside | Tmux::NoAnswer => SESSION,
    };
    let mut lines = vec!["Remote control setup (read-only check)".to_owned()];
    lines.push(match &checks.tmux {
        Tmux::Session(name) => mark(true, &format!("Running in tmux session \"{name}\"")),
        Tmux::NoAnswer => mark(
            false,
            &format!(
                "Inside tmux, but tmux didn't answer; the phone command assumes session \"{SESSION}\". Check with: tmux new -A -s {SESSION}"
            ),
        ),
        Tmux::Outside => mark(
            false,
            &format!(
                "Not inside tmux. Quit and restart with: tmux new -A -s {SESSION} \"octet …\""
            ),
        ),
    });
    if checks.tmux_rgb == Some(false) {
        lines.push(mark(
            false,
            "tmux reduces colours. Add to ~/.tmux.conf: set -g default-terminal \"tmux-256color\" and set -as terminal-features \",xterm-256color:RGB\"",
        ));
    }
    lines.push(match &checks.tailnet {
        Some(tailnet) => mark(
            true,
            &format!(
                "Tailscale connected: {} ({})",
                tailnet.name, tailnet.address
            ),
        ),
        None => mark(
            false,
            "Tailscale isn't connected. Install it, then run: tailscale up",
        ),
    });
    if let Some(tailnet) = &checks.tailnet {
        lines.push(if checks.ssh {
            mark(true, "SSH answers on the tailnet address")
        } else if tailnet.app_store {
            // The App Store app can't run the Tailscale SSH server.
            mark(
                false,
                "SSH doesn't answer on the tailnet. Turn on Remote Login (System Settings → General → Sharing)",
            )
        } else {
            mark(
                false,
                "SSH doesn't answer on the tailnet. Turn on Remote Login (System Settings → General → Sharing) or run: tailscale set --ssh",
            )
        });
    }
    lines.push(match checks.mosh {
        Some((major, minor, patch)) if (major, minor, patch) >= (1, 4, 0) => {
            mark(true, &format!("mosh-server {major}.{minor}.{patch}"))
        }
        Some((major, minor, patch)) => mark(
            false,
            &format!(
                "mosh-server {major}.{minor}.{patch} is too old for 24-bit colour; install 1.4.0 or newer"
            ),
        ),
        None => mark(false, "mosh-server isn't installed: brew install mosh"),
    });
    if let (Some(tailnet), true) = (&checks.tailnet, checks.ssh) {
        lines.push(format!(
            "Phone (Blink):   mosh {}@{} -- tmux new -A -s {session}",
            checks.user, tailnet.name
        ));
        lines.push(format!(
            "Phone (Termius): host {}, user {}, Mosh on, startup: tmux new -A -s {session}",
            tailnet.name, checks.user
        ));
    }
    lines.push("Setup guide: docs/remote-control.md".into());
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    fn ready() -> Checks {
        Checks {
            user: "me".into(),
            tmux: Tmux::Session("octet".into()),
            tmux_rgb: Some(true),
            tailnet: Some(Tailnet {
                name: "my-mac.tail1234.ts.net".into(),
                address: "100.101.102.103".into(),
                app_store: false,
            }),
            ssh: true,
            mosh: Some((1, 4, 0)),
        }
    }
    #[test]
    fn a_ready_host_prints_the_phone_commands() {
        let text = report(&ready());
        assert!(!text.contains("[!!]"), "{text}");
        assert!(
            text.contains("mosh me@my-mac.tail1234.ts.net -- tmux new -A -s octet"),
            "{text}"
        );
        assert!(text.contains("Termius"), "{text}");
        assert!(
            text.ends_with("Setup guide: docs/remote-control.md"),
            "{text}"
        );
    }
    #[test]
    fn each_missing_piece_says_how_to_fix_it() {
        let checks = Checks {
            tmux: Tmux::Outside,
            tmux_rgb: None,
            tailnet: None,
            ssh: false,
            mosh: Some((1, 3, 2)),
            ..ready()
        };
        let text = report(&checks);
        assert!(text.contains("tmux new -A -s octet"), "{text}");
        assert!(text.contains("tailscale up"), "{text}");
        assert!(text.contains("1.4.0"), "{text}");
        assert!(
            !text.contains("mosh me@"),
            "no phone command until the host is reachable: {text}"
        );
        let no_ssh = report(&Checks {
            ssh: false,
            ..ready()
        });
        assert!(
            no_ssh.contains("Remote Login") && no_ssh.contains("tailscale set --ssh"),
            "{no_ssh}"
        );
    }
    #[test]
    fn tmux_that_does_not_answer_is_not_reported_as_missing() {
        let text = report(&Checks {
            tmux: Tmux::NoAnswer,
            ..ready()
        });
        assert!(text.contains("tmux didn't answer"), "{text}");
        assert!(!text.contains("Not inside tmux"), "{text}");
        assert!(text.contains("tmux new -A -s octet"), "{text}");
    }
    #[test]
    fn reads_the_colour_setting_from_tmux_options() {
        let features = "terminal-features[0] xterm*:clipboard:ccolour:cstyle\nterminal-features[1] ,xterm-256color:RGB\n";
        assert_eq!(rgb_configured(features, ""), Some(true));
        let overrides = "terminal-overrides[0] ,xterm-256color:Tc\n";
        assert_eq!(rgb_configured("", overrides), Some(true));
        assert_eq!(
            rgb_configured("terminal-features[0] xterm*:clipboard\n", ""),
            Some(false)
        );
        // Old tmux without either option, or no answer: unknown, no advice.
        assert_eq!(rgb_configured("", ""), None);
    }
    #[test]
    fn the_app_store_build_gets_remote_login_advice_only() {
        let text = report(&Checks {
            tailnet: Some(Tailnet {
                app_store: true,
                ..ready().tailnet.unwrap()
            }),
            ssh: false,
            ..ready()
        });
        assert!(text.contains("Remote Login"), "{text}");
        assert!(!text.contains("tailscale set --ssh"), "{text}");
    }
    #[test]
    fn tmux_without_rgb_shows_the_config_lines() {
        let text = report(&Checks {
            tmux_rgb: Some(false),
            ..ready()
        });
        assert!(text.contains("terminal-features"), "{text}");
    }
    #[test]
    fn parses_tailscale_status() {
        let json = r#"{"BackendState":"Running","Self":{"DNSName":"my-mac.tail1234.ts.net.","TailscaleIPs":["100.101.102.103","fd7a::1"]}}"#;
        let tailnet = parse_tailnet(json, false).unwrap();
        assert_eq!(tailnet.name, "my-mac.tail1234.ts.net");
        assert_eq!(tailnet.address, "100.101.102.103");
        assert!(!tailnet.app_store);
        assert!(parse_tailnet(r#"{"BackendState":"Stopped","Self":{}}"#, false).is_none());
        assert!(parse_tailnet("not json", false).is_none());
    }
    #[test]
    fn parses_mosh_server_version() {
        assert_eq!(
            parse_mosh_version("mosh-server (mosh 1.4.0) [build mosh-1.4.0]\n"),
            Some((1, 4, 0))
        );
        assert_eq!(
            parse_mosh_version("mosh-server (mosh 1.3.2)"),
            Some((1, 3, 2))
        );
        assert_eq!(parse_mosh_version("unexpected"), None);
    }
    #[tokio::test]
    async fn probe_answers_in_bounded_time_when_commands_hang() {
        use std::os::unix::fs::PermissionsExt;
        let dir = octet_testkit::TempDir::new("octet-remote-hang");
        std::fs::create_dir_all(dir.path()).unwrap();
        let hang = dir.path().join("hang");
        std::fs::write(&hang, "#!/bin/sh\nsleep 10\n").unwrap();
        std::fs::set_permissions(&hang, std::fs::Permissions::from_mode(0o755)).unwrap();
        let hang = hang.to_str().unwrap();
        let programs = Programs {
            tmux: hang,
            tailscale: &[hang, hang],
            mosh_server: hang,
        };
        let started = std::time::Instant::now();
        let checks = probe_with(&programs, true).await;
        let elapsed = started.elapsed();
        assert!(
            elapsed < std::time::Duration::from_millis(2700),
            "the interface waits {elapsed:?}"
        );
        assert!(checks.tailnet.is_none() && checks.mosh.is_none());
    }
    #[tokio::test]
    async fn run_gives_up_after_the_timeout() {
        let started = std::time::Instant::now();
        assert_eq!(run("sleep", &["10"]).await, None);
        assert!(started.elapsed() < std::time::Duration::from_secs(4));
        assert_eq!(run("echo", &["hi"]).await.as_deref(), Some("hi\n"));
        assert_eq!(run("octet-no-such-program", &[]).await, None);
    }
}
