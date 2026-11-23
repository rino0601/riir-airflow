#![cfg(unix)]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

struct Server(Child);

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn wait_for<T>(mut poll: impl FnMut() -> Option<T>) -> T {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(result) = poll() {
            return result;
        }
        assert!(Instant::now() < deadline, "timed out waiting for Axum");
        thread::sleep(Duration::from_millis(20));
    }
}

fn check_native_server(
    args: &[&str],
    env_mode: &str,
    settings: Option<&str>,
    expected_port: u16,
    signal: &str,
) {
    let mut command = Command::new(env!("CARGO_BIN_EXE_riir-airflow"));
    command
        .env_clear()
        .env("PATH", "")
        .env("riir_mode", env_mode)
        .args(args)
        .stdin(Stdio::null());
    if let Some(settings) = settings {
        command.env("riir_serve", settings);
    }
    let mut server = Server(command.spawn().unwrap());
    let mut connection = wait_for(|| {
        assert_eq!(
            server.0.try_wait().unwrap(),
            None,
            "Axum exited during startup"
        );
        TcpStream::connect(("127.0.0.1", expected_port)).ok()
    });
    connection
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    connection
        .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut response = String::new();
    connection.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 200 OK\r\n"), "{response}");
    assert!(response.ends_with(r#"{"status":"healthy"}"#), "{response}");

    let status = Command::new("kill")
        .args([signal, &server.0.id().to_string()])
        .status()
        .unwrap();
    assert!(status.success());
    assert!(wait_for(|| server.0.try_wait().unwrap()).success());
}

fn free_port() -> u16 {
    TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

#[test]
fn explicit_native_serve_starts_without_python_and_handles_sigint() {
    let port = free_port();
    check_native_server(
        &["serve", "--port", &port.to_string(), "--native"],
        "prod",
        None,
        port,
        "-INT",
    );
}

#[test]
fn environment_selects_native_serve_and_cli_port_wins() {
    let port = free_port();
    check_native_server(
        &["--mode", "dev", "serve", "--port", &port.to_string()],
        "staging",
        Some(r#"{"native":true,"port":0}"#),
        port,
        "-TERM",
    );
}

#[test]
fn environment_port_is_used_without_cli_override() {
    let port = free_port();
    check_native_server(
        &["serve"],
        "prod",
        Some(&format!(r#"{{"native":true,"port":{port}}}"#)),
        port,
        "-TERM",
    );
}
