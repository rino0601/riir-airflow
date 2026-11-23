use serde_json::Value;

pub(crate) enum ServeMode {
    Prod,
    Dev,
}

impl ServeMode {
    pub(crate) fn default_port(&self) -> u16 {
        match self {
            Self::Prod => 80,
            Self::Dev => 8080,
        }
    }
}

/// Return the port for native invocations whose settings can be resolved in Rust.
pub fn native_serve_port(
    args: &[&str],
    env_mode: Option<&str>,
    env_serve: Option<&str>,
) -> Option<u16> {
    let (mode, port) = parse_native_serve_args(args, env_serve)?;
    let mode = match mode.or(env_mode).unwrap_or("dev") {
        "dev" => ServeMode::Dev,
        "prod" => ServeMode::Prod,
        _ => return None,
    };
    Some(port.unwrap_or_else(|| mode.default_port()))
}

pub(crate) fn parse_native_serve_args<'a>(
    args: &[&'a str],
    env_serve: Option<&str>,
) -> Option<(Option<&'a str>, Option<u16>)> {
    let (mode, args) = match args {
        ["--mode", mode @ ("dev" | "prod"), rest @ ..] => (Some(*mode), rest),
        args => (None, args),
    };
    let port = match (args, env_serve.is_some()) {
        (["serve", "--native"], false) | (["serve"], true) => None,
        (
            ["serve", "--native", "--port", value] | ["serve", "--port", value, "--native"],
            false,
        )
        | (["serve", "--port", value], true)
            if (1..=5).contains(&value.len())
                && value.bytes().all(|byte| byte.is_ascii_digit()) =>
        {
            Some(value.parse().ok()?)
        }
        _ => return None,
    };
    let env_port = match env_serve {
        Some(value) => native_env_port(value)?,
        None => None,
    };
    Some((mode, port.or(env_port)))
}

/// Accept only the JSON fields and types whose Settings interpretation we mirror.
fn native_env_port(value: &str) -> Option<Option<u16>> {
    let fields = serde_json::from_str::<Value>(value).ok()?;
    let fields = fields.as_object()?;
    if fields.get("native") != Some(&Value::Bool(true))
        || fields
            .keys()
            .any(|key| !matches!(key.as_str(), "native" | "port"))
    {
        return None;
    }
    match fields.get("port") {
        None | Some(Value::Null) => Some(None),
        Some(port) => Some(Some(port.as_u64()?.try_into().ok()?)),
    }
}

#[cfg(test)]
mod tests {
    use super::native_serve_port;

    #[test]
    fn resolves_default_port_using_cli_then_environment_mode() {
        assert_eq!(
            native_serve_port(&["serve", "--native"], None, None),
            Some(8080)
        );
        let settings = Some(r#"{"native":true,"port":null}"#);
        assert_eq!(
            native_serve_port(&["serve"], Some("prod"), settings),
            Some(80)
        );
        assert_eq!(
            native_serve_port(&["--mode", "dev", "serve"], Some("prod"), settings),
            Some(8080)
        );
    }
}
