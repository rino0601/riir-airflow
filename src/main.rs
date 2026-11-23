use std::env;
use std::ffi::OsString;
use std::io;
use std::process::{Command, ExitCode};

use riir_airflow::{native_serve_port, serve};

fn native_port(args: &[OsString]) -> Option<u16> {
    let args = args
        .iter()
        .map(|arg| arg.to_str())
        .collect::<Option<Vec<_>>>()?;
    let mut mode = None;
    let mut settings = None;
    for (key, value) in env::vars_os() {
        let Some(key) = key.to_str() else { continue };
        match key.to_ascii_lowercase().as_str() {
            "riir_show" => return None,
            "riir_mode" => mode = Some(value),
            "riir_serve" => settings = Some(value),
            _ => {}
        }
    }
    let mode = mode
        .as_ref()
        .map(|value| value.to_str())
        .unwrap_or(Some("dev"))?;
    let settings = match settings.as_ref() {
        Some(value) => Some(value.to_str()?),
        None => None,
    };
    native_serve_port(&args, Some(mode), settings)
}

fn run(args: &[OsString]) -> io::Result<ExitCode> {
    if let Some(port) = native_port(args) {
        serve(port)?;
        return Ok(ExitCode::SUCCESS);
    }

    let executable = if cfg!(windows) { "python" } else { "python3" };
    let mut python = Command::new(executable);
    python.args(["-m", "riir_airflow"]).args(args);
    let launch_error = |error: io::Error| {
        io::Error::new(
            error.kind(),
            format!("Could not launch {executable} -m riir_airflow: {error}"),
        )
    };
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(launch_error(python.exec()))
    }
    #[cfg(not(unix))]
    {
        Ok(python
            .status()
            .map_err(launch_error)?
            .code()
            .and_then(|code| u8::try_from(code).ok())
            .map_or(ExitCode::FAILURE, ExitCode::from))
    }
}

fn main() -> ExitCode {
    let args = env::args_os().skip(1).collect::<Vec<_>>();
    match run(&args) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
