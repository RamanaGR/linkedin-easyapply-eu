"""CLI entrypoint for LinkedIn Easy Apply EU bot."""

from __future__ import annotations

import argparse
import sys

from src.bot import EasyApplyBot
from src.config import load_config, load_env
from src.logger import setup_logger
from src.utils import ensure_dirs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EU LinkedIn Easy Apply bot (local Ollama)")
    p.add_argument("--check", action="store_true", help="Health-check config + Ollama and exit")
    p.add_argument("--dry-run", action="store_true", help="Force dry_run (no submit)")
    p.add_argument("--live", action="store_true", help="Force dry_run=false (submit applications)")
    p.add_argument("--max-apps", type=int, default=None, help="Override max applications this run")
    p.add_argument(
        "--position",
        action="append",
        default=None,
        help="Limit to one or more role keywords (repeatable)",
    )
    p.add_argument(
        "--tech",
        action="append",
        default=None,
        help="Limit to one or more tech-stack keywords (repeatable)",
    )
    p.add_argument(
        "--roles-only",
        action="store_true",
        help="Search only positions (disable tech_keywords)",
    )
    p.add_argument(
        "--tech-only",
        action="store_true",
        help="Search only tech_keywords (disable positions)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = setup_logger()
    ensure_dirs()

    try:
        config = load_config()
        env = load_env()
    except Exception as exc:
        logger.error("%s", exc)
        return 1

    if args.dry_run:
        config.session.dry_run = True
    if args.live:
        config.session.dry_run = False
    if args.max_apps is not None:
        config.session.max_applications_per_run = args.max_apps
    if args.position:
        config.positions = args.position
        config.search.use_positions = True
    if args.tech:
        config.tech_keywords = args.tech
        config.search.use_tech_keywords = True
    if args.roles_only:
        config.search.use_positions = True
        config.search.use_tech_keywords = False
    if args.tech_only:
        config.search.use_positions = False
        config.search.use_tech_keywords = True

    bot = EasyApplyBot(config, env)

    if args.check:
        ok = bot.check_health()
        return 0 if ok else 2

    if not bot.check_health():
        logger.error("Health check failed. Fix issues or run with available models.")
        return 2

    started = False
    try:
        bot.start_browser()
        started = True
        if not bot.login():
            logger.error("Login failed")
            return 3
        # Human step is only inside login() (mobile approve after Sign in).
        # No captcha pause during search/apply.
        bot.run()
    except KeyboardInterrupt:
        logger.warning("Interrupted — writing summary")
    except OSError as exc:
        logger.error(
            "Browser failed to start: %s\n"
            "On Apple Silicon this is often a wrong-arch chromedriver. "
            "Delete ~/Library/Application Support/undetected_chromedriver/ "
            "and retry; the bot auto-downloads mac-arm64 ChromeDriver.",
            exc,
        )
        return 5
    except Exception as exc:
        name = type(exc).__name__
        if name in ("InvalidSessionIdException", "NoSuchWindowException"):
            logger.error("Browser window closed or session lost — stopping run.")
            return 6
        raise
    finally:
        if started:
            try:
                path = bot.reporter.write_summary_md()
                bot.reporter.print_console_summary()
                logger.info("Summary written to %s", path)
            except Exception as exc:
                logger.warning("Could not write summary: %s", exc)
        bot.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
