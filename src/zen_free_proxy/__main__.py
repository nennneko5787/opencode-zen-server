from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from typing import Any

import uvicorn

from .app import APP_FACTORY, create_app
from .config import Settings, get_settings

#: Settings whose environment variable name differs from the ``ZEN_PROXY_`` + field
#: name rule. Used to hand CLI overrides to uvicorn's reload subprocess.
ENV_NAMES = {
    "host": "ZEN_PROXY_HOST",
    "port": "ZEN_PROXY_PORT",
    "zen_base_url": "ZEN_PROXY_ZEN_BASE_URL",
    "log_level": "ZEN_PROXY_LOG_LEVEL",
    "api_key": "ZEN_API_KEY",
    "allowed_client_keys": "ZEN_PROXY_ALLOWED_CLIENT_KEYS",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zen-free-proxy",
        description="OpenAI-compatible proxy for the free models on OpenCode Zen.",
        epilog="Command line options win over environment variables and .env.",
    )
    parser.add_argument("--host", metavar="ADDR", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", metavar="PORT", type=int, help="bind port (default: 8787)")
    parser.add_argument(
        "--api-key",
        metavar="KEY",
        help="Zen key sent upstream. Visible in the process list, prefer ZEN_API_KEY.",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        dest="zen_base_url",
        help="Zen base URL (default: https://opencode.ai/zen/v1)",
    )
    parser.add_argument(
        "--allowed-client-keys",
        metavar="KEY[,KEY...]",
        dest="allowed_client_keys",
        help="require one of these client keys. Pass an empty string to stay open.",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="log level (default: info)",
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev only)")
    return parser


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """Settings the caller actually asked for, as Settings constructor keywords."""
    overrides: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "api_key": args.api_key,
        "zen_base_url": args.zen_base_url,
        "log_level": args.log_level,
    }
    if args.allowed_client_keys is not None:
        overrides["allowed_client_keys"] = args.allowed_client_keys
    return {key: value for key, value in overrides.items() if value is not None}


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    overrides = overrides_from(args)
    settings = Settings(**overrides) if overrides else get_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    log = logging.getLogger("zen_free_proxy")
    if args.reload:
        # uvicorn re-imports the app in a subprocess under --reload, so an app object
        # cannot carry the overrides across. Pass them through the environment instead.
        os.environ.update(_env_for(overrides))
        uvicorn.run(
            APP_FACTORY,
            factory=True,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            reload=True,
        )
        return

    log.debug("startup overrides: %s", sorted(overrides))
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def _env_for(overrides: dict[str, Any]) -> dict[str, str]:
    env = {}
    for key, value in overrides.items():
        if key in ENV_NAMES and value is not None:
            env[ENV_NAMES[key]] = str(value)
    return env


if __name__ == "__main__":
    main()
