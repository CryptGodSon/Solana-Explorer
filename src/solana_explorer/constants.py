"""Well-known Solana constants and human-readable labels."""

from __future__ import annotations

LAMPORTS_PER_SOL = 1_000_000_000
SOL_DECIMALS = 9

CLUSTERS: dict[str, str] = {
    "mainnet-beta": "https://api.mainnet-beta.solana.com",
    "devnet": "https://api.devnet.solana.com",
    "testnet": "https://api.testnet.solana.com",
}
CLUSTER_ALIASES: dict[str, str] = {"mainnet": "mainnet-beta"}
DEFAULT_CLUSTER = "mainnet-beta"
RPC_URL_ENV_VAR = "SOLANA_RPC_URL"

KNOWN_PROGRAMS: dict[str, str] = {
    "11111111111111111111111111111111": "System Program",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token Program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022 Program",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token Account Program",
    "ComputeBudget111111111111111111111111111111": "Compute Budget Program",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo Program",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo": "Memo Program (v1)",
    "Vote111111111111111111111111111111111111111": "Vote Program",
    "Stake11111111111111111111111111111111111111": "Stake Program",
    "BPFLoaderUpgradeab1e11111111111111111111111": "BPF Upgradeable Loader",
    "AddressLookupTab1e1111111111111111111111111": "Address Lookup Table Program",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s": "Metaplex Token Metadata",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter Aggregator v6",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpools",
}

KNOWN_TOKENS: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "wSOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYb": "USDT",
}
