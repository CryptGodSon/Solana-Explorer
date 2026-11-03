"""Command-line interface: ``solana-explorer tx|account|history|gui``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ._version import __version__
from .constants import CLUSTERS, RPC_URL_ENV_VAR
from .errors import InvalidInputError, SolanaExplorerError
from .explorer import MAX_HISTORY_LIMIT, SolanaExplorer
from .formatting import format_account, format_signatures, format_transaction
from .validation import split_identifiers

ExplorerFactory = Callable[[str | None, float], SolanaExplorer]

EPILOG = f"""\
examples:
  solana-explorer tx <SIGNATURE>
  solana-explorer tx <SIG1> <SIG2> --json
  solana-explorer tx --file signatures.txt --cluster devnet
  solana-explorer account <ADDRESS>
  solana-explorer history <ADDRESS> --limit 25
  solana-explorer gui

The RPC endpoint defaults to ${RPC_URL_ENV_VAR} or, if unset, public mainnet-beta.
"""


def _default_factory(endpoint: str | None, timeout: float) -> SolanaExplorer:
    return SolanaExplorer(endpoint=endpoint, timeout=timeout)


def _positive_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_HISTORY_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_HISTORY_LIMIT}")
    return number


def build_parser() -> argparse.ArgumentParser:
    network = argparse.ArgumentParser(add_help=False)
    group = network.add_argument_group("network options")
    target = group.add_mutually_exclusive_group()
    target.add_argument(
        "-c",
        "--cluster",
        metavar="NAME",
        help=f"public cluster: {', '.join(CLUSTERS)} (default: mainnet-beta)",
    )
    target.add_argument("-u", "--rpc-url", metavar="URL", help="custom RPC endpoint URL")
    group.add_argument(
        "--timeout", type=float, default=15.0, metavar="SEC", help="request timeout (default: 15)"
    )

    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="print machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="solana-explorer",
        description="Explore Solana transactions and accounts from the terminal.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    tx = commands.add_parser(
        "tx", parents=[network, output], help="show details of one or more transactions"
    )
    tx.add_argument("signatures", nargs="*", metavar="SIGNATURE", help="transaction signature(s)")
    tx.add_argument(
        "-f",
        "--file",
        metavar="PATH",
        help="read signatures from a file ('-' for stdin), separated by newlines or commas",
    )
    tx.add_argument("--logs", action="store_true", help="include program log messages")

    account = commands.add_parser(
        "account", parents=[network, output], help="show balance and owner of an account"
    )
    account.add_argument("address", help="account address (public key)")

    history = commands.add_parser(
        "history", parents=[network, output], help="list recent transactions of an account"
    )
    history.add_argument("address", help="account address (public key)")
    history.add_argument(
        "-n", "--limit", type=_positive_int, default=10, help="number of signatures (default: 10)"
    )
    history.add_argument("--before", metavar="SIGNATURE", help="start searching before this one")

    commands.add_parser("gui", parents=[network], help="open the graphical interface")
    return parser


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_signatures(args: argparse.Namespace) -> list[str]:
    sources: list[str] = list(args.signatures)
    if args.file:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        sources.append(text)
    return split_identifiers(sources)


def _cmd_tx(args: argparse.Namespace, explorer: SolanaExplorer) -> int:
    signatures = _read_signatures(args)
    if not signatures:
        raise InvalidInputError("provide at least one signature (or --file)")

    failures = 0
    json_results: list[dict[str, Any]] = []
    for index, result in enumerate(explorer.iter_transactions(signatures)):
        if result.transaction is None:
            failures += 1
        if args.json:
            json_results.append(
                result.transaction.to_dict()
                if result.transaction is not None
                else {"signature": result.signature, "error": str(result.error)}
            )
            continue
        if result.transaction is None:
            print(f"error: {result.error}", file=sys.stderr)
            continue
        if index:
            print("\n" + "-" * 72 + "\n")
        print(format_transaction(result.transaction, explorer.endpoint, show_logs=args.logs))
        sys.stdout.flush()

    if args.json:
        _print_json(json_results[0] if len(json_results) == 1 else json_results)
    return 1 if failures else 0


def _cmd_account(args: argparse.Namespace, explorer: SolanaExplorer) -> int:
    account = explorer.get_account(args.address)
    if args.json:
        _print_json(account.to_dict())
    else:
        print(format_account(account, explorer.endpoint))
    return 0


def _cmd_history(args: argparse.Namespace, explorer: SolanaExplorer) -> int:
    signatures = explorer.get_signatures(args.address, limit=args.limit, before=args.before)
    if args.json:
        _print_json([sig.to_dict() for sig in signatures])
    else:
        print(format_signatures(args.address, signatures))
    return 0


COMMANDS = {"tx": _cmd_tx, "account": _cmd_account, "history": _cmd_history}


def main(
    argv: Sequence[str] | None = None, *, explorer_factory: ExplorerFactory | None = None
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    endpoint = args.rpc_url or args.cluster

    if args.command == "gui":
        from .gui import run

        run(endpoint)
        return 0

    try:
        explorer = (explorer_factory or _default_factory)(endpoint, args.timeout)
        return COMMANDS[args.command](args, explorer)
    except InvalidInputError as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    except (SolanaExplorerError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
