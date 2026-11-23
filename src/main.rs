use std::env;
use std::io;
use std::process::{Command, ExitCode};

use riir_airflow::cli::{Config, INVALID_MODE, ServeMode, Subcommand};
use riir_airflow::{cli, serve};

fn run(config: Config) -> io::Result<ExitCode> {
    let (command, port) = match config.command {
        Subcommand::Show => ("show", None),
        Subcommand::Serve { native, port } => {
            let Some(mode) = ServeMode::parse(&config.mode) else {
                println!("{INVALID_MODE}");
                return Ok(ExitCode::SUCCESS);
            };
            if native {
                serve(port.unwrap_or_else(|| mode.default_port()))?;
                return Ok(ExitCode::SUCCESS);
            }
            ("serve", port)
        }
    };

    let executable = if cfg!(windows) { "python" } else { "python3" };
    let mut python = Command::new(executable);
    python
        .args([
            "-c",
            "from riir_airflow.__main__ import _run_from_cargo; _run_from_cargo()",
        ])
        .arg(config.mode)
        .arg(command);
    if let Some(port) = port {
        python.arg(port.to_string());
    }
    let launch_error = |error: io::Error| {
        io::Error::new(
            error.kind(),
            format!("Could not launch the riir-airflow Python stage with {executable}: {error}"),
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
    let config = match cli::parse(env::args_os().skip(1).collect(), "riir-airflow") {
        Ok(config) => config,
        Err(exit) => {
            if exit.code == 0 {
                print!("{}", exit.message);
            } else {
                eprint!("{}", exit.message);
            }
            return ExitCode::from(exit.code);
        }
    };
    match run(config) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
