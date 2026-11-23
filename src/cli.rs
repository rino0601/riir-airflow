use std::env;
use std::ffi::{OsStr, OsString};

use clap::builder::OsStringValueParser;
use clap::error::ErrorKind;
use clap::{Arg, ArgAction, Command};
use serde_json::Value;

pub enum ServeMode {
    Prod,
    Dev,
}

impl ServeMode {
    pub fn default_port(&self) -> u16 {
        match self {
            Self::Prod => 80,
            Self::Dev => 8080,
        }
    }
}

pub struct Config {
    pub mode: OsString,
    pub command: Subcommand,
}

pub enum Subcommand {
    Show,
    Serve { native: bool, port: Option<u16> },
}

pub struct CliExit {
    pub code: u8,
    pub message: String,
}

impl CliExit {
    fn validation(message: impl Into<String>) -> Self {
        Self {
            code: 1,
            message: message.into(),
        }
    }
}

#[derive(Default)]
struct Environment {
    mode: Option<OsString>,
    serve: Option<OsString>,
}

impl Environment {
    fn read() -> Self {
        let mut settings = Self::default();
        // Settings matched environment keys case-insensitively, taking the last match.
        for (key, value) in env::vars_os() {
            if key.eq_ignore_ascii_case("RIIR_MODE") {
                settings.mode = Some(value);
            } else if key.eq_ignore_ascii_case("RIIR_SERVE") {
                settings.serve = Some(value);
            }
        }
        settings
    }
}

pub fn settings_mode() -> OsString {
    Environment::read().mode.unwrap_or_else(|| "dev".into())
}

fn command() -> Command {
    Command::new("riir-airflow")
        .about("riir-airflow CLI: serve/show 하위 명령을 제공한다.")
        .disable_help_subcommand(true)
        .arg(
            Arg::new("mode")
                .long("mode")
                .value_name("str")
                .allow_negative_numbers(true)
                .help("Execution mode [default: dev]")
                .value_parser(OsStringValueParser::new()),
        )
        .subcommand(
            Command::new("serve")
                .about("Start the ASGI or native HTTP server")
                .arg(
                    Arg::new("native")
                        .long("native")
                        .action(ArgAction::SetTrue)
                        .overrides_with("no-native")
                        .help("Serve /health and /show directly with Axum"),
                )
                .arg(
                    Arg::new("no-native")
                        .long("no-native")
                        .action(ArgAction::SetTrue)
                        .overrides_with("native")
                        .help("Serve through Uvicorn"),
                )
                .arg(
                    Arg::new("port")
                        .long("port")
                        .value_name("int|null")
                        .allow_negative_numbers(true)
                        .value_parser(OsStringValueParser::new())
                        .help("Override the listening port (0–65535)"),
                )
                .args_override_self(true),
        )
        .subcommand(Command::new("show").about("Print the current mode"))
        .args_override_self(true)
}

pub fn parse(args: Vec<OsString>, program: &str) -> Result<Config, CliExit> {
    let mut args = args.into_iter().peekable();
    let args = std::iter::from_fn(|| {
        let mut arg = args.next()?;
        // The previous parser accepted separated negative decimals such as -.5.
        // Use clap's attached-value syntax to preserve that spelling.
        if (arg == "--mode" || arg == "--port")
            && args
                .peek()
                .and_then(|value| value.to_str()?.strip_prefix("-."))
                .is_some_and(|digits| {
                    !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
                })
        {
            arg.push("=");
            arg.push(args.next().unwrap());
        }
        Some(arg)
    });
    let mut parser = command().bin_name(program);
    let mut matches = parser
        .try_get_matches_from_mut(std::iter::once(OsString::from(program)).chain(args))
        .map_err(|error| CliExit {
            code: if error.kind() == ErrorKind::DisplayHelp {
                0
            } else {
                2
            },
            message: error.to_string(),
        })?;

    let environment = Environment::read();
    let mode = matches.remove_one::<OsString>("mode");
    if mode.as_deref() == Some(OsStr::new("null")) {
        return Err(CliExit::validation(
            "mode: Input should be a valid string\n",
        ));
    }

    let command = match matches.remove_subcommand() {
        Some((name, _)) if name == "show" => Subcommand::Show,
        Some((_, args)) => resolve_serve(args, environment.serve.as_deref())?,
        None => {
            let help = parser.render_help();
            return Err(CliExit::validation(format!(
                "Error: CLI subcommand is required\n\n{help}"
            )));
        }
    };

    Ok(Config {
        mode: mode.or(environment.mode).unwrap_or_else(|| "dev".into()),
        command,
    })
}

fn resolve_serve(
    mut args: clap::ArgMatches,
    env_serve: Option<&OsStr>,
) -> Result<Subcommand, CliExit> {
    // Malformed JSON and non-objects left the selected command's defaults intact.
    let fields = env_serve
        .and_then(OsStr::to_str)
        .and_then(|value| serde_json::from_str::<Value>(value).ok())
        .and_then(|value| match value {
            Value::Object(fields) => Some(fields),
            _ => None,
        })
        .unwrap_or_default();
    let field = |name: &str| {
        // The last case-insensitive match wins, including when spellings differ.
        fields
            .iter()
            .rfind(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value)
    };
    let native = if args.get_flag("native") {
        true
    } else if args.get_flag("no-native") {
        false
    } else {
        field("native")
            .map(parse_native)
            .transpose()?
            .unwrap_or(false)
    };
    let port = match args.remove_one::<OsString>("port") {
        Some(value) if value == "null" => None,
        Some(value) => Some(parse_port_string(value.to_str().unwrap_or_default())?),
        None => match field("port") {
            Some(Value::Null) | None => None,
            Some(Value::String(value)) => Some(parse_port_string(value)?),
            Some(Value::Bool(value)) => Some(u16::from(*value)),
            Some(Value::Number(value)) => {
                Some(parse_port_number(value.as_f64().ok_or_else(invalid_port)?)?)
            }
            _ => return Err(invalid_port()),
        },
    };
    Ok(Subcommand::Serve { native, port })
}

fn parse_native(value: &Value) -> Result<bool, CliExit> {
    match value {
        Value::Bool(value) => return Ok(*value),
        Value::Number(value) if value.as_f64() == Some(1.0) => return Ok(true),
        Value::Number(value) if value.as_f64() == Some(0.0) => return Ok(false),
        Value::String(value) => match value.to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "on" | "yes" | "y" => return Ok(true),
            "0" | "false" | "f" | "off" | "no" | "n" => return Ok(false),
            _ => {}
        },
        _ => {}
    }
    Err(CliExit::validation(
        "serve.native: Input should be a valid boolean\n",
    ))
}

fn invalid_port() -> CliExit {
    CliExit::validation("serve.port: Input should be a valid integer\n")
}

fn parse_port_string(value: &str) -> Result<u16, CliExit> {
    let value = value.trim();
    let digits = value.strip_prefix(['+', '-']).unwrap_or(value);
    let (whole, fraction) = digits.split_once('.').unwrap_or((digits, "0"));
    // Integral strings may include digit separators and a zero decimal part.
    let valid_whole = whole
        .split('_')
        .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()));
    if !valid_whole || fraction.is_empty() || !fraction.bytes().all(|byte| byte == b'0') {
        return Err(invalid_port());
    }
    parse_port_number(value.replace('_', "").parse().map_err(|_| invalid_port())?)
}

fn parse_port_number(value: f64) -> Result<u16, CliExit> {
    if value.fract() != 0.0 {
        Err(invalid_port())
    } else if value < 0.0 {
        Err(CliExit::validation(
            "serve.port: Input should be greater than or equal to 0\n",
        ))
    } else if value > f64::from(u16::MAX) {
        Err(CliExit::validation(
            "serve.port: Input should be less than or equal to 65535\n",
        ))
    } else {
        Ok(value as u16)
    }
}
