use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

fn invoke(request: serde_json::Value) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_agrep-rs"))
        .arg("boundary-rank")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    serde_json::to_writer(child.stdin.as_mut().unwrap(), &request).unwrap();
    child.stdin.as_mut().unwrap().flush().unwrap();
    drop(child.stdin.take());
    child.wait_with_output().unwrap()
}

fn receive_response(receiver: &mpsc::Receiver<String>, child: &mut Child) -> String {
    match receiver.recv_timeout(Duration::from_secs(5)) {
        Ok(line) => line,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            panic!("boundary-rank server did not respond: {error}");
        }
    }
}

#[test]
fn one_query_batch_returns_ordered_scores_and_item_errors() {
    let output = invoke(serde_json::json!({
        "protocol": 2,
        "query": "akd",
        "stats": {"akd": [372, 32]},
        "items": [
            {"text": "peakDetect"},
            {"text": "akd"},
            {"text": "akd", "spans": [[-1, 2]]}
        ]
    }));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let response: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["protocol"], 2);
    assert_eq!(response["results"][0]["factor"], 0.12);
    assert_eq!(response["results"][0]["match_class"], "interior");
    assert_eq!(response["results"][0]["spans"], serde_json::json!([[2, 5]]));
    assert_eq!(response["results"][1]["factor"], 1.0);
    assert_eq!(response["results"][1]["match_class"], "aligned");
    assert_eq!(
        response["results"][2]["error"],
        "boundary span outside text"
    );
}

#[test]
fn protocol_mismatch_is_command_fatal() {
    let output = invoke(serde_json::json!({
        "protocol": 1,
        "query": "akd",
        "items": []
    }));
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("unsupported boundary-rank protocol"));
}

#[test]
fn compact_batch_omits_oracle_only_fields() {
    let output = invoke(serde_json::json!({
        "protocol": 2,
        "query": "akd",
        "compact": true,
        "items": [{"text": "peakDetect"}]
    }));
    assert!(output.status.success());
    let response: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let score = &response["results"][0];
    assert!(score.get("factor").is_some());
    assert!(score.get("match_class").is_some());
    assert!(score.get("qualities").is_none());
    assert!(score.get("spans").is_none());
}

#[test]
fn serve_answers_multiple_batches_without_waiting_for_eof() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_agrep-rs"))
        .args(["boundary-rank", "--serve"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut stdout = BufReader::new(stdout);
        for _ in 0..2 {
            let mut line = String::new();
            if stdout.read_line(&mut line).unwrap() == 0 {
                break;
            }
            sender.send(line).unwrap();
        }
    });

    serde_json::to_writer(
        &mut stdin,
        &serde_json::json!({
            "protocol": 2,
            "query": "akd",
            "compact": true,
            "items": [{"text": "akd"}]
        }),
    )
    .unwrap();
    stdin.write_all(b"\n").unwrap();
    stdin.flush().unwrap();

    let first: serde_json::Value =
        serde_json::from_str(&receive_response(&receiver, &mut child)).unwrap();
    assert_eq!(first["results"][0]["match_class"], "aligned");
    assert!(child.try_wait().unwrap().is_none());

    serde_json::to_writer(
        &mut stdin,
        &serde_json::json!({
            "protocol": 2,
            "query": "akd",
            "compact": true,
            "items": [{"text": "peakDetect"}]
        }),
    )
    .unwrap();
    stdin.write_all(b"\n").unwrap();
    stdin.flush().unwrap();

    let second: serde_json::Value =
        serde_json::from_str(&receive_response(&receiver, &mut child)).unwrap();
    assert_eq!(second["results"][0]["match_class"], "interior");
    drop(stdin);

    let status = child.wait().unwrap();
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    reader.join().unwrap();
    assert!(status.success(), "{stderr}");
}

#[test]
fn serve_reports_malformed_input_and_answers_the_next_request() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_agrep-rs"))
        .args(["boundary-rank", "--serve"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut stdout = BufReader::new(stdout);
        for _ in 0..2 {
            let mut line = String::new();
            if stdout.read_line(&mut line).unwrap() == 0 {
                break;
            }
            sender.send(line).unwrap();
        }
    });

    stdin.write_all(b"{not-json}\n").unwrap();
    stdin.flush().unwrap();
    let malformed: serde_json::Value =
        serde_json::from_str(&receive_response(&receiver, &mut child)).unwrap();
    assert!(!malformed["error"].as_str().unwrap().is_empty());
    assert!(child.try_wait().unwrap().is_none());

    serde_json::to_writer(
        &mut stdin,
        &serde_json::json!({
            "protocol": 2,
            "query": "akd",
            "compact": true,
            "items": [{"text": "akd"}]
        }),
    )
    .unwrap();
    stdin.write_all(b"\n").unwrap();
    stdin.flush().unwrap();
    let valid: serde_json::Value =
        serde_json::from_str(&receive_response(&receiver, &mut child)).unwrap();
    assert_eq!(valid["results"][0]["match_class"], "aligned");
    drop(stdin);

    let status = child.wait().unwrap();
    reader.join().unwrap();
    assert!(status.success());
}
