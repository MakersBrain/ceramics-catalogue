"""Validate, provision, or explicitly purge the selected delivery provider."""

from __future__ import annotations

import argparse
import asyncio

from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.ops.providers.factory import provisioner


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="catalogue-queue-admin", description=__doc__)
    subcommands = command.add_subparsers(dest="action", required=True)
    subcommands.add_parser("validate", help="report provider configuration drift")
    subcommands.add_parser("apply", help="create or update provider consumers")
    purge = subcommands.add_parser("purge", help="permanently delete all target messages")
    purge.add_argument(
        "--confirm-provider",
        required=True,
        help="must exactly match CATALOGUE_QUEUE_PROVIDER",
    )
    return command


async def run(options: argparse.Namespace) -> int:
    settings = Settings()
    admin = provisioner(settings)
    try:
        if options.action == "apply":
            await admin.apply()
            issues = await admin.validate()
        elif options.action == "purge":
            if options.confirm_provider != settings.queue_provider:
                raise ValueError("--confirm-provider must match CATALOGUE_QUEUE_PROVIDER")
            await admin.purge()
            issues = []
        else:
            issues = await admin.validate()
    finally:
        await admin.close()
    for issue in issues:
        print(issue)
    return 1 if issues else 0


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
