//! What every vendor connection shares (`Core`), and what each vendor
//! supplies (`Protocol`). The driver loop in `driver.rs` joins the two; a new
//! vendor implements `Protocol` in its own file and adds a provider row.
use super::{emit, Config, DriverError, Event, Limits, Mode, EVENT_BYTES};
use octet_proc::Process;
use serde_json::Value;
use std::{collections::HashMap, ffi::OsString, future::Future, time::Duration};
use tokio::{
    sync::mpsc,
    time::{timeout, Instant},
};

/// A vendor approval shown to the user, denied when its deadline passes.
pub(super) struct Pending {
    pub(super) wire: Value,
    pub(super) deadline: Instant,
}

/// One vendor connection's shared state and the operations every protocol
/// uses on it.
pub(crate) struct Core {
    pub(super) config: Config,
    pub(super) limits: Limits,
    pub(super) process: Process,
    pub(super) tx: mpsc::Sender<Event>,
    // Connection
    pub(super) initialized: bool,
    pub(super) ready: bool,
    pub(super) session: String,
    pub(super) mode: Mode,
    // Turn
    pub(super) running: bool,
    pub(super) interrupt_pending: bool,
    /// Connect deadline, then the turn's silence watchdog.
    pub(super) deadline: Instant,
    pub(super) request_id: u64,
    pub(super) pending: HashMap<u64, Pending>,
    /// The protocol's approval reply, for approvals denied here.
    answer: fn(&Value, bool) -> Value,
}

impl Core {
    pub(super) fn new<P: Protocol>(
        config: Config,
        limits: Limits,
        process: Process,
        tx: mpsc::Sender<Event>,
    ) -> Self {
        Self {
            mode: config.mode,
            config,
            limits,
            process,
            tx,
            initialized: false,
            ready: false,
            session: String::new(),
            running: false,
            interrupt_pending: false,
            deadline: Instant::now() + limits.connect,
            request_id: 10,
            pending: HashMap::new(),
            answer: P::answer,
        }
    }

    pub(super) fn emit(&self, event: Event) -> Result<(), DriverError> {
        emit(&self.tx, event)
    }

    pub(super) async fn send(&self, value: Value) -> Result<(), DriverError> {
        timeout(Duration::from_secs(3), self.process.sender().send(&value))
            .await
            .map_err(|_| DriverError::from("Vendor stdin is unresponsive"))?
            .map_err(|e| DriverError::from(e.to_string()))
    }

    /// The wire reply allowing or denying a vendor approval.
    pub(super) fn answer(&self, wire: &Value, allow: bool) -> Value {
        (self.answer)(wire, allow)
    }

    /// Restarts the silence watchdog once no approval is waiting on the user.
    pub(super) fn resume_watchdog(&mut self) {
        if self.pending.is_empty() && self.running && !self.interrupt_pending {
            self.deadline = Instant::now() + self.limits.turn_idle;
        }
    }

    /// Shows a vendor approval to the user, or denies it when it is too large
    /// to show completely.
    pub(super) async fn queue_approval(
        &mut self,
        wire: Value,
        detail: String,
    ) -> Result<(), DriverError> {
        if detail.len() > EVENT_BYTES {
            self.send(self.answer(&wire, false)).await?;
            return self.emit(Event::Notice(
                "Oversized approval denied: cannot show the complete request".into(),
            ));
        }
        self.request_id += 1;
        self.emit(Event::Approval {
            id: self.request_id,
            detail,
        })?;
        let deadline = Instant::now() + self.limits.approval;
        self.pending
            .insert(self.request_id, Pending { wire, deadline });
        Ok(())
    }

    pub(super) async fn deny_all_pending(&mut self) -> Result<(), DriverError> {
        for (id, pending) in std::mem::take(&mut self.pending) {
            self.send(self.answer(&pending.wire, false)).await?;
            self.emit(Event::ApprovalClosed(id))?;
        }
        Ok(())
    }

    /// The turn ended: its approvals no longer apply.
    pub(super) fn close_all_pending(&mut self) -> Result<(), DriverError> {
        for id in std::mem::take(&mut self.pending).into_keys() {
            self.emit(Event::ApprovalClosed(id))?;
        }
        Ok(())
    }
}

/// One vendor's wire protocol. The driver owns the loop, timers and
/// approvals; a protocol turns commands into frames and frames into events.
pub(crate) trait Protocol: Default + Send {
    /// The vendor CLI's arguments.
    fn launch_args(config: &Config) -> Vec<OsString>;
    /// Starts the handshake.
    fn initialize(
        &mut self,
        core: &mut Core,
    ) -> impl Future<Output = Result<(), DriverError>> + Send;
    /// Sends the user's prompt; the turn has already started.
    fn send_prompt(
        &mut self,
        core: &mut Core,
        text: &str,
    ) -> impl Future<Output = Result<(), DriverError>> + Send;
    /// Asks the vendor to stop the running turn.
    fn interrupt(
        &mut self,
        core: &mut Core,
    ) -> impl Future<Output = Result<(), DriverError>> + Send;
    /// Switches to `target`, which differs from the current mode and is not
    /// full access; the driver has checked both.
    fn set_mode(
        &mut self,
        core: &mut Core,
        target: Mode,
    ) -> impl Future<Output = Result<(), DriverError>> + Send;
    /// Handles one frame from the vendor.
    fn on_frame(
        &mut self,
        core: &mut Core,
        frame: Value,
    ) -> impl Future<Output = Result<(), DriverError>> + Send;
    /// The reply allowing or denying the approval request `wire`.
    fn answer(wire: &Value, allow: bool) -> Value;
    /// The reply to a request Octet will not put in front of the user.
    fn stray_reply(request: &Value) -> Option<Value>;
    /// Whether `frame` shows the turn is alive, resetting its silence
    /// watchdog.
    fn is_progress(&self, _core: &Core, _frame: &Value) -> bool {
        true
    }
    /// A mode change is waiting for the vendor to confirm it.
    fn mode_change_pending(&self) -> bool {
        false
    }
    /// The protocol's own timer, if one is armed.
    fn deadline(&self) -> Option<Instant> {
        None
    }
    /// Runs when the loop wakes for a timer; checks its own deadline.
    fn on_deadline(
        &mut self,
        _core: &mut Core,
    ) -> impl Future<Output = Result<(), DriverError>> + Send {
        async { Ok(()) }
    }
    /// A turn is starting: clears per-turn protocol state.
    fn turn_started(&mut self) {}
}
