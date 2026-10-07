//! Provider-neutral permission modes. Each vendor maps them in its own file.
use super::{DriverError, Engine, Event, emit};
use tokio::sync::mpsc;

/// Provider-neutral permission mode. The vendor mapping lives only in the
/// functions below; `Auto` delegates to each vendor's own reviewer and
/// Octet never answers a vendor approval by itself.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Mode {
    #[default]
    /// Ask before edits and commands.
    Ask,
    /// Edits proceed; other actions ask.
    AcceptEdits,
    /// The vendor's own reviewer decides each approval.
    Auto,
    /// No checks at all; only reached by reconnecting.
    FullAccess,
}
impl Mode {
    /// Every mode, from strictest to most open.
    pub const ALL: [Mode; 4] = [Mode::Ask, Mode::AcceptEdits, Mode::Auto, Mode::FullAccess];
    /// The mode named by its label.
    ///
    /// ```
    /// use octet_engine::live::Mode;
    ///
    /// assert_eq!(Mode::parse("accept-edits"), Some(Mode::AcceptEdits));
    /// assert_eq!(Mode::parse("bypassPermissions"), None);
    /// ```
    pub fn parse(value: &str) -> Option<Mode> {
        Self::ALL.into_iter().find(|mode| mode.label() == value)
    }
    /// The name users type and the header shows.
    pub fn label(self) -> &'static str {
        match self {
            Mode::Ask => "ask",
            Mode::AcceptEdits => "accept-edits",
            Mode::Auto => "auto",
            Mode::FullAccess => "full-access",
        }
    }
    /// Shift+Tab order. Full access is never reached by cycling.
    #[must_use]
    pub fn cycle(self) -> Mode {
        match self {
            Mode::Ask => Mode::AcceptEdits,
            Mode::AcceptEdits => Mode::Auto,
            Mode::Auto => Mode::Ask,
            Mode::FullAccess => Mode::FullAccess,
        }
    }
    /// What this mode means for `engine`, for `/mode`.
    pub fn describe(self, engine: Engine) -> &'static str {
        engine.provider().modes[self.index()]
    }
    /// The position in `ALL`, which is also the order of a provider's
    /// `modes`.
    fn index(self) -> usize {
        match self {
            Mode::Ask => 0,
            Mode::AcceptEdits => 1,
            Mode::Auto => 2,
            Mode::FullAccess => 3,
        }
    }
}
/// Adopt the mode the vendor confirmed. An unmapped report keeps the requested
/// mode and says what the vendor actually uses; Octet never guesses.
pub(super) fn confirm_mode(
    tx: &mpsc::Sender<Event>,
    vendor: &str,
    requested: Mode,
    reported: Option<(Option<Mode>, String)>,
) -> Result<Mode, DriverError> {
    match reported {
        None => Ok(requested),
        Some((Some(actual), _)) if actual == requested => Ok(requested),
        Some((Some(actual), _)) => {
            emit(
                tx,
                Event::Notice(format!(
                    "{vendor} reports {}; you asked for {}",
                    actual.label(),
                    requested.label()
                )),
            )?;
            Ok(actual)
        }
        Some((None, raw)) => {
            let notice = format!(
                "{vendor} reports a permission setting Octet does not map ({raw}); showing the requested {}",
                requested.label()
            );
            emit(tx, Event::Notice(notice))?;
            Ok(requested)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mode_labels_parse_and_cycle() {
        for mode in Mode::ALL {
            assert_eq!(Mode::parse(mode.label()), Some(mode));
        }
        assert_eq!(Mode::default(), Mode::Ask);
        assert_eq!(Mode::parse("bypassPermissions"), None);
        assert_eq!(Mode::parse(""), None);
        assert_eq!(Mode::Ask.cycle(), Mode::AcceptEdits);
        assert_eq!(Mode::AcceptEdits.cycle(), Mode::Auto);
        assert_eq!(Mode::Auto.cycle(), Mode::Ask);
        assert_eq!(Mode::FullAccess.cycle(), Mode::FullAccess);
    }
    #[test]
    fn every_mode_is_described_for_every_engine() {
        for engine in Engine::ALL.iter().copied() {
            for mode in Mode::ALL {
                assert!(!mode.describe(engine).is_empty());
            }
        }
    }
}
