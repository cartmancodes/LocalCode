//! Offline demo engine: no vendor process.
use super::*;

pub(super) fn demo_set_mode(
    tx: &mpsc::Sender<Event>,
    mode: &mut Mode,
    target: Mode,
) -> Result<(), DriverError> {
    if target == Mode::FullAccess || *mode == Mode::FullAccess {
        emit(
            tx,
            Event::Notice("Full access is changed by reconnecting; use /mode".into()),
        )?;
    } else {
        *mode = target;
    }
    emit(tx, Event::ModeChanged(*mode))
}
pub(super) async fn demo(
    mut mode: Mode,
    mut commands: mpsc::Receiver<Command>,
    mut cancel: watch::Receiver<u64>,
    mut stop: watch::Receiver<bool>,
    tx: &mpsc::Sender<Event>,
) -> Result<(), DriverError> {
    emit(
        tx,
        Event::Ready {
            session: "demo · offline".into(),
        },
    )?;
    emit(tx, Event::ModeChanged(mode))?;
    loop {
        tokio::select! {
            _=stop.changed()=>break,
            _=cancel.changed()=>{},
            command=commands.recv()=>match command{
                Some(command @ (Command::Prompt(_) | Command::PromptWithDisplay { .. }))=>{
                    let (text,display)=prompt_parts(command);
                    emit(tx,Event::User(display.clone()))?;
                    emit(tx,Event::Started)?;
                    let reply=if text.trim()=="/approval-demo" && mode!=Mode::Ask && mode!=Mode::AcceptEdits {
                        emit(tx,Event::Notice(format!("Allowed without a dialog by {} mode (demo only)",mode.label())))?;
                        "Approved automatically. In a live session, the vendor's own reviewer decides.".to_owned()
                    } else if text.trim()=="/approval-demo" {
                        emit(tx,Event::Approval{id:1,detail:"Demo only — no command will execute.\n\nWrite a greeting to hello.txt?".into()})?;
                        let expiry=tokio::time::sleep(Duration::from_secs(120));
                        tokio::pin!(expiry);
                        // A mode switch while the dialog is open applies and keeps waiting.
                        let allowed=loop{tokio::select!{_=stop.changed()=>return Ok(()),_=cancel.changed()=>break false,_=&mut expiry=>break false,c=commands.recv()=>match c{
                            Some(Command::SetMode(target))=>demo_set_mode(tx,&mut mode,target)?,
                            c=>break matches!(c,Some(Command::Answer{id:1,allow:true})),
                        }}};
                        emit(tx,Event::ApprovalClosed(1))?;
                        if allowed{"Approved. In a live session, the vendor would now continue.".to_owned()}else{"Denied. No action was performed.".to_owned()}
                    }else{format!("This is an offline demo. Your prompt was:\n\n{display}\n\nThe editor, streaming transcript, approval dialog, history, and cancellation are live. Start with --engine codex or --engine claude to work with a model.\n\nTry /approval-demo to preview a permission request.")};
                    let mut interrupted=false;
                    for word in reply.split_inclusive(' '){tokio::select!{_=stop.changed()=>return Ok(()),_=cancel.changed()=>{interrupted=true;break;},_=tokio::time::sleep(Duration::from_millis(18))=>{emit(tx,Event::Text(word.into()))?;}}}
                    emit(tx,Event::Finished{outcome:if interrupted{Outcome::Interrupted}else{Outcome::Completed}})?;
                },
                Some(Command::SetMode(target))=>demo_set_mode(tx,&mut mode,target)?,
                None=>break,
                _=>{}
            }
        }
    }
    Ok(())
}
