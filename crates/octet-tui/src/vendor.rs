//! The seam between the interface and a running session: what the interface
//! sends, the prompts it queues, and the one way a turn starts.
use crate::app::App;
use octet_core::{Command, Handle, ImageAttachment, SendError};

/// What the interface asks of a session. The session's `Handle` is the real
/// one; tests use a recording stand-in.
pub(crate) trait Vendor {
    /// Queues `command` for the session.
    fn send(&self, command: Command) -> Result<(), SendError>;
    /// Cancels the running turn, and any turn sent but not yet started.
    fn interrupt(&self);
}

impl Vendor for Handle {
    fn send(&self, command: Command) -> Result<(), SendError> {
        Handle::send(self, command)
    }
    fn interrupt(&self) {
        Handle::interrupt(self);
    }
}

/// A prompt as the user sent it: what the vendor receives, what the
/// transcript shows, and the images that go with it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Prompt {
    pub(crate) wire: String,
    pub(crate) display: String,
    pub(crate) images: Vec<ImageAttachment>,
}

impl Prompt {
    /// A prompt sent and shown as typed, without images.
    pub(crate) fn plain(text: String) -> Self {
        Self {
            wire: text.clone(),
            display: text,
            images: Vec::new(),
        }
    }

    /// The command that sends it.
    pub(crate) fn into_command(self) -> Command {
        if self.wire == self.display && self.images.is_empty() {
            Command::Prompt(self.wire)
        } else {
            Command::PromptWithDisplay {
                wire: self.wire,
                display: self.display,
                images: self.images,
            }
        }
    }
}

/// Whose turn it is: the user's own prompt, or the goal runner's.
#[derive(Clone, Copy)]
pub(crate) enum By {
    User,
    Goal,
}

impl App {
    /// Sends a turn command and marks the turn started, with `status` on the
    /// status bar. Nothing changes when the send fails.
    ///
    /// # Errors
    ///
    /// The session's refusal: a prompt over the limit, or a full queue.
    pub(crate) fn begin_turn(
        &mut self,
        vendor: &dyn Vendor,
        command: Command,
        by: By,
        status: &str,
    ) -> Result<(), SendError> {
        vendor.send(command)?;
        match by {
            By::User => self.goals.user_prompt_sent(),
            By::Goal => self.goals.goal_prompt_sent(),
        }
        self.conn.start_turn();
        self.conn.status = status.into();
        Ok(())
    }
}

/// A stand-in session that records what it was sent, and can refuse.
#[cfg(test)]
#[derive(Default)]
pub(crate) struct RecordingVendor {
    pub(crate) sent: std::cell::RefCell<Vec<Command>>,
    pub(crate) interrupts: std::cell::Cell<usize>,
    /// Refuse every send, as a stopped session does.
    pub(crate) refuse: std::cell::Cell<bool>,
}

#[cfg(test)]
impl Vendor for RecordingVendor {
    fn send(&self, command: Command) -> Result<(), SendError> {
        if self.refuse.get() {
            return Err(SendError::Busy);
        }
        self.sent.borrow_mut().push(command);
        Ok(())
    }
    fn interrupt(&self) {
        self.interrupts.set(self.interrupts.get() + 1);
    }
}
