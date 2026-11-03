# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-10

Complete rewrite. The previous script called a REST endpoint
(`/v1/transactions/<id>`) that Solana RPC nodes do not provide, so it could not
fetch any transaction.

### Added

- Installable Python package (`pip install`) with `solana-explorer` and `solana-explorer-gui` commands.
- JSON-RPC client with timeouts, retries and exponential backoff on HTTP 429/5xx.
- Transaction decoding: status, fee, compute units, signers, SOL and SPL-token transfers (including inner instructions), SOL and token balance changes, and the instruction tree.
- `account` and `history` commands for address lookups.
- Batch lookups from arguments, files or stdin, plus `--json` output.
- Tkinter GUI that runs requests on a background thread.
- Cluster selection (mainnet-beta, devnet, testnet, custom URL, `SOLANA_RPC_URL`).
- Base58 validation of signatures and addresses.
- In-memory LRU cache for fetched transactions.
- Offline pytest suite and GitHub Actions CI (ruff, pytest on Linux, Windows and macOS).

### Removed

- The non-working `SolanaExplorer.py` script and its duplicate `Code` file.
