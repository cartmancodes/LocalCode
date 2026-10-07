//! The JSON-line transport against a scripted child: framing, limits,
//! back-pressure, stderr capture and process-group shutdown.
use octet_proc::{Process, ProcessConfig, ProcessError, ShutdownStage};
use serde_json::{Value, json};
use std::{path::PathBuf, time::Duration};
use tokio::time::timeout;

fn child_binary() -> PathBuf {
    octet_testkit::protocol_child()
}

/// The next frame, failing the test if none comes within two seconds.
async fn next_within(process: &mut Process) -> Result<Option<Value>, ProcessError> {
    timeout(Duration::from_secs(2), process.next_frame())
        .await
        .expect("no frame within two seconds")
}
fn config(mode: &str) -> ProcessConfig {
    ProcessConfig {
        executable: child_binary(),
        args: vec![mode.into()],
        cwd: None,
        max_frame_bytes: 1024,
        queue_bytes: 2048,
        stderr_bytes: 512,
        shutdown_grace: Duration::from_millis(80),
        term_grace: Duration::from_millis(150),
    }
}

#[tokio::test]
async fn echo_round_trip_and_clean_eof() {
    let mut process = Process::spawn(&config("echo")).unwrap();
    let sender = process.sender();
    sender
        .send(&json!({"op":"echo","value":"hello"}))
        .await
        .unwrap();
    assert_eq!(
        process.next_frame().await.unwrap(),
        Some(json!({"op":"echo","value":"hello"}))
    );
    sender.send(&json!({"op":"exit"})).await.unwrap();
    assert_eq!(next_within(&mut process).await.unwrap(), None);
    let report = process.shutdown().await;
    assert!(report.reaped);
}

#[tokio::test]
async fn shutdown_closes_stdin_and_reaps_cooperative_child() {
    let mut cfg = config("echo");
    // Long enough for a cooperative child to see EOF and exit on a machine
    // busy with the suite; the stage assertion is what this test is about.
    cfg.shutdown_grace = Duration::from_secs(3);
    let mut process = Process::spawn(&cfg).unwrap();
    let report = timeout(Duration::from_secs(5), process.shutdown())
        .await
        .unwrap();
    assert!(report.reaped);
    assert_eq!(report.stage, ShutdownStage::AlreadyExited);
}

#[tokio::test]
async fn split_json_line_reassembles_before_parsing() {
    let mut process = Process::spawn(&config("split")).unwrap();
    assert_eq!(
        next_within(&mut process).await.unwrap(),
        Some(json!({"part":"complete"}))
    );
    assert!(process.shutdown().await.reaped);
}

#[tokio::test]
async fn oversized_line_fails_with_explicit_limit() {
    let mut cfg = config("oversize");
    cfg.max_frame_bytes = 64;
    let mut process = Process::spawn(&cfg).unwrap();
    let error = next_within(&mut process).await.unwrap_err();
    assert!(matches!(error, ProcessError::FrameTooLarge { limit: 64 }));
    assert!(process.shutdown().await.reaped);
}

/// The `oversize` child writes one frame of 6 + 8192 + 2 bytes.
const OVERSIZE_FRAME: usize = 8200;

#[tokio::test]
async fn frame_at_the_limit_passes_and_one_more_byte_fails() {
    let mut at_limit = config("oversize");
    at_limit.max_frame_bytes = OVERSIZE_FRAME;
    at_limit.queue_bytes = 2 * OVERSIZE_FRAME;
    let mut process = Process::spawn(&at_limit).unwrap();
    let frame = next_within(&mut process)
        .await
        .unwrap()
        .expect("a frame exactly at the limit arrives");
    assert_eq!(frame["x"].as_str().map(str::len), Some(8192));
    assert!(process.shutdown().await.reaped);

    let mut over = config("oversize");
    over.max_frame_bytes = OVERSIZE_FRAME - 1;
    over.queue_bytes = 2 * OVERSIZE_FRAME;
    let mut process = Process::spawn(&over).unwrap();
    let error = next_within(&mut process).await.unwrap_err();
    assert!(matches!(error, ProcessError::FrameTooLarge { limit } if limit == OVERSIZE_FRAME - 1));
    assert!(process.shutdown().await.reaped);
}

#[tokio::test]
async fn malformed_json_is_reported() {
    let mut process = Process::spawn(&config("malformed")).unwrap();
    let error = next_within(&mut process).await.unwrap_err();
    assert!(matches!(error, ProcessError::InvalidJson(_)));
    assert_eq!(process.next_frame().await.unwrap(), None);
    assert!(process.shutdown().await.reaped);
}

#[tokio::test]
async fn partial_frame_at_eof_is_an_error() {
    let mut process = Process::spawn(&config("partial")).unwrap();
    let error = next_within(&mut process).await.unwrap_err();
    assert!(
        matches!(error, ProcessError::Io(ref io_error) if io_error.kind() == std::io::ErrorKind::UnexpectedEof)
    );
    assert!(process.shutdown().await.reaped);
}

#[tokio::test]
async fn stderr_flood_is_drained_and_tail_is_bounded() {
    let mut cfg = config("stderr");
    cfg.stderr_bytes = 80;
    let mut process = Process::spawn(&cfg).unwrap();
    assert_eq!(
        next_within(&mut process).await.unwrap(),
        Some(json!({"ready":true}))
    );
    let report = process.shutdown().await;
    assert!(report.reaped);
    assert!(report.stderr_tail.len() <= 80);
    assert!(report.stderr_tail.ends_with(b"END-OF-STDERR\n"));
}

#[tokio::test]
async fn control_send_survives_full_stdout_queue() {
    let mut cfg = config("flood");
    cfg.queue_bytes = 128;
    cfg.max_frame_bytes = 128;
    let mut process = Process::spawn(&cfg).unwrap();
    let sender = process.sender();
    sender.send(&json!({"op":"flood"})).await.unwrap();
    // Best effort: give the flood time to fill the queue. Nothing outside the
    // transport can observe that, and a shorter wait only weakens the test.
    tokio::time::sleep(Duration::from_millis(80)).await;
    // The control send must not wait for the queue to drain; one second is
    // far below the time the flood would take to be read.
    timeout(
        Duration::from_secs(1),
        sender.send(&json!({"op":"interrupt"})),
    )
    .await
    .unwrap()
    .unwrap();
    let mut saw_ack = false;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    while tokio::time::Instant::now() < deadline {
        let frame = tokio::time::timeout_at(deadline, process.next_frame())
            .await
            .unwrap()
            .unwrap();
        if frame == Some(json!({"ack":"interrupt"})) {
            saw_ack = true;
            break;
        }
        if frame.is_none() {
            break;
        }
    }
    assert!(
        saw_ack,
        "interrupt ack must pass through a saturated data stream"
    );
    assert!(
        timeout(Duration::from_secs(2), process.shutdown())
            .await
            .unwrap()
            .reaped
    );
}

#[tokio::test]
async fn shutdown_reaps_child_while_stdout_queue_is_full() {
    let mut cfg = config("flood");
    cfg.queue_bytes = 128;
    cfg.max_frame_bytes = 128;
    let mut process = Process::spawn(&cfg).unwrap();
    process.sender().send(&json!({"op":"flood"})).await.unwrap();
    tokio::time::sleep(Duration::from_millis(80)).await;
    let report = timeout(Duration::from_secs(2), process.shutdown())
        .await
        .unwrap();
    assert!(report.reaped);
}

#[cfg(unix)]
#[tokio::test]
async fn shutdown_kills_grandchild_after_leader_exits() {
    let mut process = Process::spawn(&config("grandchild")).unwrap();
    let frame = next_within(&mut process).await.unwrap().unwrap();
    let pid = i32::try_from(frame["pid"].as_i64().unwrap()).unwrap();
    let _ = next_within(&mut process).await.unwrap();
    let report = timeout(Duration::from_secs(2), process.shutdown())
        .await
        .unwrap();
    assert!(report.reaped);
    assert!(matches!(
        report.stage,
        ShutdownStage::Term | ShutdownStage::Kill
    ));
    for _ in 0..30 {
        // SAFETY: signal 0 only checks that the process exists.
        if unsafe { libc::kill(pid, 0) } != 0 {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("grandchild {pid} remained alive after process-group shutdown");
}

#[tokio::test]
async fn repeated_shutdown_is_safe_and_sender_stays_closed() {
    let mut process = Process::spawn(&config("echo")).unwrap();
    let sender = process.sender();
    let first = process.shutdown().await;
    let second = process.shutdown().await;
    assert!(first.reaped && second.reaped);
    assert_eq!(first.stage, second.stage);
    assert!(matches!(
        sender.send(&json!({})).await,
        Err(ProcessError::StdinClosed)
    ));
}

#[tokio::test]
async fn cancelled_write_closes_pipe_without_corrupting_next_frame() {
    let mut cfg = config("sleeper");
    cfg.max_frame_bytes = 4 * 1024 * 1024;
    cfg.queue_bytes = cfg.max_frame_bytes;
    let mut process = Process::spawn(&cfg).unwrap();
    let sender = process.sender();
    let value = json!({"large": "x".repeat(2 * 1024 * 1024)});
    assert!(
        timeout(Duration::from_millis(100), sender.send(&value))
            .await
            .is_err()
    );
    assert!(matches!(
        sender.send(&json!({})).await,
        Err(ProcessError::StdinClosed)
    ));
    assert!(process.shutdown().await.reaped);
}

#[tokio::test]
async fn oversized_send_does_not_close_healthy_pipe() {
    let mut process = Process::spawn(&config("echo")).unwrap();
    let sender = process.sender();
    assert!(matches!(
        sender.send(&json!({"data":"x".repeat(2048)})).await,
        Err(ProcessError::FrameTooLarge { limit: 1024 })
    ));
    sender.send(&json!({"op":"echo"})).await.unwrap();
    assert_eq!(
        process.next_frame().await.unwrap(),
        Some(json!({"op":"echo"}))
    );
    assert!(process.shutdown().await.reaped);
}

/// Explicit diagnostic, excluded from correctness tests to avoid timing flakes.
#[tokio::test]
#[ignore = "timing benchmark; run explicitly"]
async fn transport_roundtrip_benchmark() {
    let mut process = Process::spawn(&config("echo")).unwrap();
    let sender = process.sender();
    let value = json!({"op":"echo","value":"x".repeat(256)});
    let mut samples = Vec::with_capacity(2000);
    for _ in 0..2000 {
        let start = std::time::Instant::now();
        sender.send(&value).await.unwrap();
        assert_eq!(
            next_within(&mut process).await.unwrap(),
            Some(value.clone())
        );
        samples.push(start.elapsed().as_micros());
    }
    samples.sort_unstable();
    eprintln!(
        "2000 sequential 280-byte JSON round trips: median={}us p95={}us p99={}us",
        samples[1000], samples[1900], samples[1980]
    );
    assert!(process.shutdown().await.reaped);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stderr_of_a_child_that_exits_at_once_is_complete() {
    for _ in 0..40 {
        let mut cfg = config("stderr-exit");
        cfg.stderr_bytes = 64;
        let mut process = Process::spawn(&cfg).unwrap();
        assert_eq!(next_within(&mut process).await.unwrap(), None);
        let report = process.shutdown().await;
        assert!(
            report.stderr_tail.ends_with(b"FINAL-REASON\n"),
            "{:?}",
            String::from_utf8_lossy(&report.stderr_tail)
        );
    }
}

/// Shuts down a child that wrote a long stderr and exited, without reading
/// its output first: the final line must still be in the tail.
async fn stderr_tail_after_exit(runs: usize) {
    for run in 0..runs {
        let mut cfg = config("stderr-exit");
        cfg.stderr_bytes = 64;
        // Room for the child to finish and exit by itself on a busy machine:
        // a TERM would cut its stderr short, which is not the case under test.
        cfg.shutdown_grace = Duration::from_secs(3);
        let mut process = Process::spawn(&cfg).expect("spawn the child");
        let report = process.shutdown().await;
        assert!(
            report.stderr_tail.ends_with(b"FINAL-REASON\n"),
            "run {run}: {:?}",
            String::from_utf8_lossy(&report.stderr_tail)
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stderr_is_complete_when_shutdown_does_not_drain_frames() {
    stderr_tail_after_exit(50).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "stress: about 0.1% of runs lost the tail before the fix"]
async fn stderr_stress_500_runs() {
    stderr_tail_after_exit(500).await;
}

#[tokio::test]
async fn blank_lines_are_skipped() {
    let mut process = Process::spawn(&config("blank-lines")).unwrap();
    assert_eq!(
        next_within(&mut process).await.unwrap(),
        Some(json!({"a":1}))
    );
    assert_eq!(
        next_within(&mut process).await.unwrap(),
        Some(json!({"b":2}))
    );
    assert_eq!(next_within(&mut process).await.unwrap(), None);
    assert!(process.shutdown().await.reaped);
}

#[tokio::test]
async fn invalid_json_after_valid_frames_ends_the_stream() {
    let mut process = Process::spawn(&config("valid-then-invalid")).unwrap();
    assert_eq!(
        next_within(&mut process).await.unwrap(),
        Some(json!({"a":1}))
    );
    assert_eq!(
        next_within(&mut process).await.unwrap(),
        Some(json!({"b":2}))
    );
    assert!(matches!(
        next_within(&mut process).await,
        Err(ProcessError::InvalidJson(_))
    ));
    assert_eq!(process.next_frame().await.unwrap(), None);
    assert!(process.shutdown().await.reaped);
}

#[cfg(unix)]
#[tokio::test]
async fn dropping_a_process_kills_its_group() {
    let mut process = Process::spawn(&config("grandchild")).unwrap();
    let frame = next_within(&mut process).await.unwrap().unwrap();
    let pid = i32::try_from(frame["pid"].as_i64().unwrap()).unwrap();
    drop(process);
    for _ in 0..100 {
        // SAFETY: signal 0 only checks that the process exists.
        if unsafe { libc::kill(pid, 0) } != 0 {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("grandchild {pid} survived dropping its Process");
}

#[tokio::test]
async fn a_send_after_exit_is_broken_pipe_then_closed() {
    let mut process = Process::spawn(&config("exit-at-once")).unwrap();
    assert_eq!(next_within(&mut process).await.unwrap(), None);
    let sender = process.sender();
    // The child is gone: the first write fails on the pipe and closes it.
    let mut first = sender.send(&json!({"op":"echo"})).await;
    for _ in 0..50 {
        if first.is_err() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
        first = sender.send(&json!({"op":"echo"})).await;
    }
    assert!(
        matches!(&first, Err(ProcessError::Io(e)) if e.kind() == std::io::ErrorKind::BrokenPipe),
        "{first:?}"
    );
    assert!(matches!(
        sender.send(&json!({})).await,
        Err(ProcessError::StdinClosed)
    ));
    assert!(process.shutdown().await.reaped);
}
