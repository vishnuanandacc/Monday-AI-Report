from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "config.yml"
DEFAULT_ENV_PATH = ROOT_DIR / ".env"

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


class ConfigError(ValueError):
    """Raised when local configuration cannot be read."""


@dataclass(frozen=True)
class AppSettings:
    config_path: Path
    timezone: str = "America/Indiana/Indianapolis"
    monday_board_id: str = ""
    groups: dict[str, str] = field(default_factory=dict)
    group_ids: dict[str, str] = field(default_factory=dict)
    column_ids: dict[str, str] = field(default_factory=dict)
    reporting: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    ai: dict[str, Any] = field(default_factory=dict)

    def with_board_id(self, board_id: str | None) -> "AppSettings":
        if not board_id:
            return self
        return AppSettings(
            config_path=self.config_path,
            timezone=self.timezone,
            monday_board_id=board_id,
            groups=dict(self.groups),
            group_ids=dict(self.group_ids),
            column_ids=dict(self.column_ids),
            reporting=dict(self.reporting),
            rules=dict(self.rules),
            ai=dict(self.ai),
        )

    def live_inspection_status(self, environ: Mapping[str, str] | None = None) -> dict[str, bool]:
        env = load_runtime_environment(environ)
        return {
            "MONDAY_BOARD_ID": bool(self.monday_board_id),
            "MONDAY_API_TOKEN_or_MONDAY_MCP_AUTH": bool(get_monday_token(env)),
        }

    def missing_live_inspection_requirements(
        self, environ: Mapping[str, str] | None = None
    ) -> list[str]:
        return [name for name, present in self.live_inspection_status(environ).items() if not present]


def load_settings(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppSettings:
    env = load_runtime_environment(environ)
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    raw_config: dict[str, Any] = {}

    if path.exists():
        raw_config = parse_simple_yaml(path.read_text(encoding="utf-8"))
    elif config_path:
        raise ConfigError(f"Config file not found: {path}")

    config = resolve_env_placeholders(raw_config, env)

    groups = {
        "backlog": "Backlog",
        "on_deck": "On Deck",
        "in_progress": "In Progress",
        "blocked": "Blocked",
        "pending_approval": "Pending Approval",
        "complete": "Complete",
    }
    groups.update(_dict_value(config, "groups"))
    group_ids = _dict_value(config, "group_ids")
    column_ids = _dict_value(config, "column_ids")

    reporting = {
        "snapshot_day": "monday",
        "snapshot_time": "08:00",
        "report_day": "friday",
        "report_time": "15:00",
    }
    reporting.update(_dict_value(config, "reporting"))

    rules = {
        "pending_approval_is_delivered": True,
        "complete_is_delivered": True,
        "include_backlog": False,
    }
    rules.update(_dict_value(config, "rules"))

    ai = {
        "model": "",
        "max_comments_per_item": 20,
        "max_characters_per_item": 12000,
    }
    ai.update(_dict_value(config, "ai"))

    timezone = str(
        env.get("REPORT_TIMEZONE")
        or _nested_value(config, "application", "timezone")
        or "America/Indiana/Indianapolis"
    )

    board_id = str(_nested_value(config, "monday", "board_id") or env.get("MONDAY_BOARD_ID", ""))

    return AppSettings(
        config_path=path,
        timezone=timezone,
        monday_board_id=board_id,
        groups=groups,
        group_ids={key: str(value or "") for key, value in group_ids.items()},
        column_ids={key: str(value or "") for key, value in column_ids.items()},
        reporting=reporting,
        rules=rules,
        ai=ai,
    )


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small subset of YAML used by config/config.yml."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("\t"):
            raise ConfigError(f"Tabs are not supported in config YAML, line {line_number}")

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        key, separator, raw_value = stripped.partition(":")
        if not separator:
            raise ConfigError(f"Expected key/value pair in config YAML, line {line_number}")

        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise ConfigError(f"Invalid indentation in config YAML, line {line_number}")

        parent = stack[-1][1]
        key = key.strip()
        value = raw_value.strip()
        if not key:
            raise ConfigError(f"Missing key in config YAML, line {line_number}")

        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value)

    return root


def resolve_env_placeholders(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_env_placeholders(child, environ) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve_env_placeholders(child, environ) for child in value]
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda match: environ.get(match.group(1), match.group(2) or ""),
            value,
        )
    return value


def load_runtime_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return process env merged over local .env values.

    Tests can pass environ to avoid reading the developer's real .env file.
    """
    if environ is not None:
        return dict(environ)

    merged = parse_dotenv(DEFAULT_ENV_PATH)
    merged.update(os.environ)
    return merged


def parse_dotenv(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    return parse_dotenv_text(env_path.read_text(encoding="utf-8"), source=str(env_path))


def parse_dotenv_text(text: str, source: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, raw_value = line.partition("=")
        if not separator:
            raise ConfigError(f"Expected KEY=value in {source}, line {line_number}")
        key = key.strip()
        if not key:
            raise ConfigError(f"Missing environment variable name in {source}, line {line_number}")
        values[key] = _coerce_dotenv_value(raw_value.strip())
    return values


def get_monday_token(environ: Mapping[str, str] | None = None) -> str:
    env = load_runtime_environment(environ)
    token = (env.get("MONDAY_API_TOKEN") or "").strip()
    if token:
        return _normalize_token(token)

    mcp_auth = (env.get("MONDAY_MCP_AUTH") or "").strip()
    if not mcp_auth:
        return ""

    if mcp_auth.startswith("{"):
        try:
            parsed = json.loads(mcp_auth)
        except json.JSONDecodeError:
            return _normalize_token(mcp_auth)
        for key in ("token", "api_token", "apiToken", "access_token"):
            if parsed.get(key):
                return _normalize_token(str(parsed[key]))

    return _normalize_token(mcp_auth)


def get_openai_api_key(environ: Mapping[str, str] | None = None) -> str:
    env = load_runtime_environment(environ)
    return (env.get("OPENAI_API_KEY") or "").strip().strip('"').strip("'")


def _normalize_token(token: str) -> str:
    token = token.strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


def _coerce_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _coerce_scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _nested_value(config: Mapping[str, Any], *keys: str) -> Any:
    current: Any = config
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _dict_value(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return dict(value) if isinstance(value, Mapping) else {}
