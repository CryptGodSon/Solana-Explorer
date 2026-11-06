/*
 * Data layer: talks to Solana JSON-RPC directly from the browser and turns raw
 * responses into the plain objects the UI renders.
 *
 * The UI asks for data through `backend.handle(path, params)` with REST-like
 * paths (/api/tx/<sig>, /api/account/<address>, ...), which keeps app.js free
 * of RPC details. Everything here is unit-tested in Node (tests/backend.test.js).
 */
(function (root) {
  "use strict";

  const VOTE_PROGRAM = "Vote111111111111111111111111111111111111111";
  const SYSTEM_PROGRAM = "11111111111111111111111111111111";
  const METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s";
  const TOKEN_PROGRAM_IDS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"];
  const TOKEN_PROGRAMS = new Set(["spl-token", "spl-token-2022"]);
  // getBlock error codes meaning "there is no block for this slot on this node"
  const BLOCK_UNAVAILABLE = new Set([-32004, -32007, -32009, -32014]);
  const VERSION_NOT_SUPPORTED = -32015;
  const MULTIPLE_ACCOUNTS_LIMIT = 100;
  const MAX_HISTORY = 1000;
  const CUSTOM_RPC_KEY = "solana-explorer:custom-rpc";

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  class RpcError extends Error {
    constructor(code, message) {
      super(code !== undefined && code !== null ? `RPC error ${code}: ${message}` : message);
      this.code = code;
      this.rpcMessage = message;
    }
  }

  /* ------------------------------------------------------------ base58 */

  const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const ALPHABET_INDEX = Object.fromEntries([...ALPHABET].map((c, i) => [c, BigInt(i)]));

  function b58decode(value) {
    if (!value) throw new Error("empty string is not valid base58");
    let n = 0n;
    for (const c of value) {
      const digit = ALPHABET_INDEX[c];
      if (digit === undefined) throw new Error(`invalid base58 character ${c}`);
      n = n * 58n + digit;
    }
    const bytes = [];
    while (n > 0n) {
      bytes.unshift(Number(n & 0xffn));
      n >>= 8n;
    }
    let zeros = 0;
    while (zeros < value.length && value[zeros] === "1") zeros++;
    return Uint8Array.from([...new Array(zeros).fill(0), ...bytes]);
  }

  function b58encode(bytes) {
    let n = 0n;
    for (const b of bytes) n = (n << 8n) | BigInt(b);
    let out = "";
    while (n > 0n) {
      out = ALPHABET[Number(n % 58n)] + out;
      n /= 58n;
    }
    let zeros = 0;
    while (zeros < bytes.length && bytes[zeros] === 0) zeros++;
    return "1".repeat(zeros) + out;
  }

  function decodedLength(value) {
    // A 64-byte value never needs more than 88 characters; reject longer input
    // before doing big-integer math on it.
    if (!value || value.length > 88) return null;
    try {
      return b58decode(value).length;
    } catch {
      return null;
    }
  }

  const isValidSignature = (value) => decodedLength(String(value ?? "").trim()) === 64;
  const isValidAddress = (value) => decodedLength(String(value ?? "").trim()) === 32;

  /* ---------------------------------------------------------- numbers */

  /** raw / 10**decimals as an exact decimal string without trailing zeros. */
  function scaleAmount(raw, decimals) {
    let n = BigInt(raw);
    const negative = n < 0n;
    if (negative) n = -n;
    const digits = n.toString().padStart(decimals + 1, "0");
    const int = digits.slice(0, digits.length - decimals);
    const frac = digits.slice(digits.length - decimals).replace(/0+$/, "");
    const text = int + (frac ? "." + frac : "");
    return negative && text !== "0" ? "-" + text : text;
  }

  const lamportsToSol = (lamports) => scaleAmount(BigInt(Math.round(Number(lamports) || 0)), 9);

  const isoTime = (seconds) =>
    seconds === null || seconds === undefined ? null : new Date(seconds * 1000).toISOString().replace(/\.\d{3}Z$/, "+00:00");

  const toBigInt = (value) => BigInt(value ?? 0);
  const isPlainObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

  /* ------------------------------------------------------ transactions */

  function parseAccountKeys(raw) {
    return (raw || []).map((item) =>
      typeof item === "string"
        ? { pubkey: item, signer: false, writable: false }
        : { pubkey: item.pubkey ?? "", signer: Boolean(item.signer), writable: Boolean(item.writable) },
    );
  }

  /** Instructions in execution order: each top-level one followed by its inner ones. */
  function* iterInstructions(message, meta) {
    const inner = new Map((meta.innerInstructions || []).map((g) => [g.index, g.instructions || []]));
    const top = message.instructions || [];
    for (let i = 0; i < top.length; i++) {
      yield [String(i + 1), top[i], false];
      const children = inner.get(i) || [];
      for (let j = 0; j < children.length; j++) yield [`${i + 1}.${j + 1}`, children[j], true];
    }
  }

  function parseInstruction(position, ix, inner, programs) {
    const programId = ix.programId ?? "unknown";
    const parsed = ix.parsed;
    return {
      position,
      program_id: programId,
      program: programs[programId] || ix.program || programId,
      type: isPlainObject(parsed) ? (parsed.type ?? null) : null,
      inner,
      // For the Memo program "parsed" is the memo text itself.
      info: isPlainObject(parsed) ? (parsed.info ?? null) : (parsed ?? null),
      accounts: [...(ix.accounts || [])],
      data: ix.data ?? null,
    };
  }

  function parseTokenBalances(entries, addresses) {
    const balances = new Map();
    for (const entry of entries || []) {
      const index = entry.accountIndex;
      if (index === undefined || index === null) continue;
      const ui = entry.uiTokenAmount || {};
      balances.set(index, {
        account: index < addresses.length ? addresses[index] : `#${index}`,
        owner: entry.owner ?? null,
        mint: entry.mint ?? "",
        decimals: Number(ui.decimals ?? 0),
        raw: toBigInt(ui.amount),
      });
    }
    return balances;
  }

  function parseTransfer(ix, tokenAccounts) {
    const parsed = ix.parsed;
    if (!isPlainObject(parsed)) return null;
    const kind = parsed.type;
    const info = parsed.info || {};
    if (ix.program === "system" && (kind === "transfer" || kind === "transferWithSeed")) {
      const lamports = toBigInt(info.lamports);
      return {
        asset: "SOL",
        source: info.source ?? "",
        destination: info.destination ?? "",
        amount: scaleAmount(lamports, 9),
        raw_amount: Number(lamports),
        decimals: 9,
        mint: null,
        authority: null,
      };
    }
    if (TOKEN_PROGRAMS.has(ix.program) && (kind === "transfer" || kind === "transferChecked")) {
      const source = info.source ?? "";
      const destination = info.destination ?? "";
      let raw;
      let decimals;
      let mint;
      if (kind === "transferChecked") {
        const tokenAmount = info.tokenAmount || {};
        raw = toBigInt(tokenAmount.amount);
        decimals = tokenAmount.decimals ?? null;
        mint = info.mint ?? null;
      } else {
        // A plain "transfer" omits the mint; recover it from the token balances.
        raw = toBigInt(info.amount);
        const known = tokenAccounts.get(source) || tokenAccounts.get(destination);
        mint = known ? known.mint : null;
        decimals = known ? known.decimals : null;
      }
      return {
        asset: "token",
        source,
        destination,
        amount: scaleAmount(raw, decimals ?? 0),
        raw_amount: Number(raw),
        decimals,
        mint,
        authority: info.authority || info.multisigAuthority || null,
      };
    }
    return null;
  }

  /** Decode a getTransaction result requested with jsonParsed encoding. */
  function parseTransaction(signature, raw, programs = {}) {
    const meta = raw.meta || {};
    const message = (raw.transaction || {}).message || {};
    const accounts = parseAccountKeys(message.accountKeys);
    const addresses = accounts.map((a) => a.pubkey);
    const pre = parseTokenBalances(meta.preTokenBalances, addresses);
    const post = parseTokenBalances(meta.postTokenBalances, addresses);
    const tokenAccounts = new Map([...pre.values(), ...post.values()].map((b) => [b.account, b]));

    const instructions = [];
    const transfers = [];
    for (const [position, ix, inner] of iterInstructions(message, meta)) {
      instructions.push(parseInstruction(position, ix, inner, programs));
      const transfer = parseTransfer(ix, tokenAccounts);
      if (transfer) transfers.push(transfer);
    }

    const solChanges = [];
    const preBalances = meta.preBalances || [];
    const postBalances = meta.postBalances || [];
    for (let i = 0; i < Math.min(preBalances.length, postBalances.length); i++) {
      if (preBalances[i] !== postBalances[i]) {
        solChanges.push({
          account: i < addresses.length ? addresses[i] : `#${i}`,
          pre_lamports: preBalances[i],
          post_lamports: postBalances[i],
          change_lamports: postBalances[i] - preBalances[i],
        });
      }
    }

    const tokenChanges = [];
    const indices = [...new Set([...pre.keys(), ...post.keys()])].sort((a, b) => a - b);
    for (const index of indices) {
      const before = pre.get(index);
      const after = post.get(index);
      const ref = after || before;
      const preRaw = before ? before.raw : 0n;
      const postRaw = after ? after.raw : 0n;
      if (preRaw === postRaw) continue;
      tokenChanges.push({
        account: ref.account,
        owner: ref.owner,
        mint: ref.mint,
        decimals: ref.decimals,
        pre_amount: scaleAmount(preRaw, ref.decimals),
        post_amount: scaleAmount(postRaw, ref.decimals),
        change: scaleAmount(postRaw - preRaw, ref.decimals),
      });
    }

    const error = meta.err ?? null;
    const fee = Number(meta.fee ?? 0);
    return {
      signature,
      slot: Number(raw.slot ?? 0),
      block_time: isoTime(raw.blockTime),
      success: error === null,
      error,
      fee_lamports: fee,
      fee_sol: lamportsToSol(fee),
      compute_units: meta.computeUnitsConsumed ?? null,
      version: String(raw.version ?? "legacy"),
      recent_blockhash: message.recentBlockhash ?? null,
      accounts,
      signers: accounts.filter((a) => a.signer).map((a) => a.pubkey),
      instructions,
      transfers,
      sol_changes: solChanges,
      token_changes: tokenChanges,
      log_messages: [...(meta.logMessages || [])],
    };
  }

  /* ------------------------------------------------------ other models */

  function parseAccount(address, value) {
    if (!value) {
      return {
        address,
        exists: false,
        lamports: 0,
        balance_sol: "0",
        owner: null,
        executable: false,
        space: null,
        program: null,
        account_type: null,
        parsed: null,
      };
    }
    const data = value.data;
    let program = null;
    let accountType = null;
    let parsed = null;
    if (isPlainObject(data)) {
      program = data.program ?? null;
      if (isPlainObject(data.parsed)) {
        parsed = data.parsed;
        accountType = parsed.type ?? null;
      }
    }
    return {
      address,
      exists: true,
      lamports: Number(value.lamports ?? 0),
      balance_sol: lamportsToSol(value.lamports ?? 0),
      owner: value.owner ?? null,
      executable: Boolean(value.executable),
      space: value.space ?? null,
      program,
      account_type: accountType,
      parsed,
    };
  }

  /** Classify an account the way block explorers label it. */
  function accountKind(account) {
    if (!account.exists) return "empty";
    if (account.executable) return "program";
    if (TOKEN_PROGRAMS.has(account.program)) {
      if (account.account_type === "mint") return "token_mint";
      if (account.account_type === "account") return "token_account";
    }
    if (["vote", "stake", "nonce"].includes(account.program)) return account.program;
    if (account.owner === SYSTEM_PROGRAM) return "wallet";
    return "account";
  }

  /** Decode getBlock with "signatures" or "accounts" transaction details. */
  function parseBlock(slot, raw) {
    const transactions = (raw.transactions || []).map((t) => {
      const tx = t.transaction || {};
      const meta = t.meta || {};
      const keys = parseAccountKeys(tx.accountKeys);
      return {
        signature: (tx.signatures || [""])[0],
        success: (meta.err ?? null) === null,
        fee_lamports: Number(meta.fee ?? 0),
        signer: keys.length ? keys[0].pubkey : null,
        is_vote: keys.some((k) => k.pubkey === VOTE_PROGRAM),
      };
    });
    const count = "transactions" in raw ? transactions.length : (raw.signatures || []).length;
    const feeRewards = (raw.rewards || []).filter((r) => r.rewardType === "Fee");
    const reward = feeRewards.reduce((sum, r) => sum + Number(r.lamports ?? 0), 0);
    return {
      slot,
      blockhash: raw.blockhash ?? "",
      previous_blockhash: raw.previousBlockhash ?? null,
      parent_slot: raw.parentSlot ?? null,
      block_time: isoTime(raw.blockTime),
      block_height: raw.blockHeight ?? null,
      transaction_count: count,
      vote_transaction_count: transactions.filter((t) => t.is_vote).length,
      leader: feeRewards.length ? (feeRewards[0].pubkey ?? null) : null,
      leader_reward_lamports: reward,
      leader_reward_sol: lamportsToSol(reward),
      transactions,
    };
  }

  function parseSignature(raw) {
    return {
      signature: raw.signature ?? "",
      slot: Number(raw.slot ?? 0),
      block_time: isoTime(raw.blockTime),
      success: (raw.err ?? null) === null,
      error: raw.err ?? null,
      memo: raw.memo ?? null,
      confirmation_status: raw.confirmationStatus ?? null,
    };
  }

  function networkStats(epoch, samples, supply, version) {
    const parsed = (samples || []).map((s) => {
      const period = Number(s.samplePeriodSecs ?? 0);
      const total = Number(s.numTransactions ?? 0);
      const nonVote = s.numNonVoteTransactions ?? null;
      return {
        slot: Number(s.slot ?? 0),
        num_transactions: total,
        num_non_vote_transactions: nonVote,
        num_slots: Number(s.numSlots ?? 0),
        period_secs: period,
        tps: period ? total / period : 0,
        non_vote_tps: nonVote === null || !period ? null : nonVote / period,
      };
    });
    const slots = parsed.reduce((a, s) => a + s.num_slots, 0);
    const seconds = parsed.reduce((a, s) => a + s.period_secs, 0);
    const slotTime = slots ? seconds / slots : null;
    const slotIndex = Number(epoch.slotIndex ?? 0);
    const slotsInEpoch = Number(epoch.slotsInEpoch ?? 0);
    return {
      epoch: Number(epoch.epoch ?? 0),
      slot_index: slotIndex,
      slots_in_epoch: slotsInEpoch,
      absolute_slot: Number(epoch.absoluteSlot ?? 0),
      block_height: epoch.blockHeight ?? null,
      transaction_count: epoch.transactionCount ?? null,
      samples: parsed, // newest first
      node_version: version?.["solana-core"] ?? null,
      epoch_progress: slotsInEpoch ? slotIndex / slotsInEpoch : 0,
      average_slot_seconds: slotTime,
      epoch_eta_seconds: slotTime === null ? null : (slotsInEpoch - slotIndex) * slotTime,
      tps: parsed.length ? parsed[0].tps : null,
      non_vote_tps: parsed.length ? parsed[0].non_vote_tps : null,
      total_supply_sol: supply ? lamportsToSol(supply.total) : null,
      circulating_supply_sol: supply ? lamportsToSol(supply.circulating) : null,
    };
  }

  /* --------------------------------------------------- token metadata */

  // ed25519 field arithmetic, used to tell public keys (on the curve) from
  // program-derived addresses (off the curve).
  const P = 2n ** 255n - 19n;
  const mod = (a) => ((a % P) + P) % P;

  function modPow(base, exponent) {
    let result = 1n;
    let b = mod(base);
    let e = exponent;
    while (e > 0n) {
      if (e & 1n) result = (result * b) % P;
      b = (b * b) % P;
      e >>= 1n;
    }
    return result;
  }

  const D = mod(-121665n * modPow(121666n, P - 2n));
  const SQRT_M1 = modPow(2n, (P - 1n) / 4n);

  function isOnCurve(bytes) {
    let y = 0n;
    for (let i = 31; i >= 0; i--) y = (y << 8n) | BigInt(bytes[i]);
    y &= (1n << 255n) - 1n;
    const y2 = (y * y) % P;
    const u = mod(y2 - 1n);
    const v = mod(D * y2 + 1n);
    const x2 = (u * modPow(v, P - 2n)) % P;
    if (x2 === 0n) return true;
    let x = modPow(x2, (P + 3n) / 8n);
    if (mod(x * x - x2) !== 0n) x = (x * SQRT_M1) % P;
    return mod(x * x - x2) === 0n;
  }

  async function sha256(bytes) {
    return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  }

  function concatBytes(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let offset = 0;
    for (const p of parts) {
      out.set(p, offset);
      offset += p.length;
    }
    return out;
  }

  /** Solana's findProgramAddress: the first off-curve address, trying bumps 255..0. */
  async function findProgramAddress(seeds, programId) {
    const program = b58decode(programId);
    const marker = new TextEncoder().encode("ProgramDerivedAddress");
    for (let bump = 255; bump >= 0; bump--) {
      const digest = await sha256(concatBytes(...seeds, Uint8Array.of(bump), program, marker));
      if (!isOnCurve(digest)) return [b58encode(digest), bump];
    }
    throw new Error("unable to find a viable program address");
  }

  /** Address of the Metaplex metadata account for a mint. */
  async function metadataAddress(mint) {
    const seeds = [new TextEncoder().encode("metadata"), b58decode(METADATA_PROGRAM), b58decode(mint)];
    return (await findProgramAddress(seeds, METADATA_PROGRAM))[0];
  }

  function base64ToBytes(text) {
    return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  }

  /** [name, symbol, uri] from a Metaplex metadata account's data. */
  function parseMetaplex(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const decoder = new TextDecoder();
    let offset = 1 + 32 + 32; // key, update authority, mint
    const read = () => {
      const length = view.getUint32(offset, true);
      offset += 4;
      if (offset + length > bytes.length) throw new Error("truncated string");
      const text = decoder.decode(bytes.subarray(offset, offset + length)).replace(/\0+$/, "").trim();
      offset += length;
      return text;
    };
    return [read(), read(), read()];
  }

  function metadataFromAccount(mint, account) {
    if (!account || account.owner !== METADATA_PROGRAM || !Array.isArray(account.data)) return null;
    try {
      const [name, symbol, uri] = parseMetaplex(base64ToBytes(account.data[0]));
      return { mint, name: name || null, symbol: symbol || null, uri: uri || null, source: "metaplex" };
    } catch {
      return null;
    }
  }

  function token2022Metadata(mint, parsed) {
    for (const ext of parsed?.info?.extensions || []) {
      if (ext.extension === "tokenMetadata") {
        const s = ext.state || {};
        return { mint, name: s.name || null, symbol: s.symbol || null, uri: s.uri || null, source: "token-2022" };
      }
    }
    return null;
  }

  /** Only https URLs (ipfs:// and ar:// are mapped to public gateways). */
  function normalizeUri(uri) {
    const value = String(uri || "").trim();
    if (value.startsWith("ipfs://")) return "https://ipfs.io/ipfs/" + value.slice(7).replace(/^ipfs\//, "");
    if (value.startsWith("ar://")) return "https://arweave.net/" + value.slice(5);
    return value.startsWith("https://") ? value : null;
  }

  /** Logo, description and website from the off-chain JSON a metadata uri points to. */
  async function fetchOffchain(uri) {
    const url = normalizeUri(uri);
    if (!url) return null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(url, { signal: controller.signal, credentials: "omit", referrerPolicy: "no-referrer" });
      if (!response.ok) return null;
      const text = await response.text();
      if (text.length > 512 * 1024) return null;
      const doc = JSON.parse(text);
      if (!isPlainObject(doc)) return null;
      const website = doc.external_url || doc.extensions?.website;
      return {
        image: typeof doc.image === "string" ? normalizeUri(doc.image) : null,
        description: typeof doc.description === "string" ? doc.description.slice(0, 1000) : null,
        website: typeof website === "string" ? normalizeUri(website) : null,
      };
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /* ------------------------------------------------------------ caches */

  class Cache {
    constructor(ttlMs = Infinity, max = 512) {
      this.ttl = ttlMs;
      this.max = max;
      this.map = new Map();
    }

    get(key) {
      const entry = this.map.get(key);
      if (!entry) return undefined;
      if (Date.now() > entry.expires) {
        this.map.delete(key);
        return undefined;
      }
      return entry.value;
    }

    set(key, value) {
      this.map.delete(key);
      this.map.set(key, { value, expires: Date.now() + this.ttl });
      if (this.map.size > this.max) this.map.delete(this.map.keys().next().value);
      return value;
    }
  }

  /* --------------------------------------------------------------- rpc */

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** JSON-RPC client with retry/backoff on HTTP 429 and 5xx. */
  class Rpc {
    constructor(url, { retries = 2, backoffMs = 500 } = {}) {
      this.url = url;
      this.retries = retries;
      this.backoffMs = backoffMs;
      this.maxVersion = 0;
      this.id = 0;
    }

    async call(method, params = []) {
      for (let attempt = 0; ; attempt++) {
        let response;
        try {
          response = await fetch(this.url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jsonrpc: "2.0", id: ++this.id, method, params }),
          });
        } catch {
          if (attempt < this.retries) {
            await sleep(this.backoffMs * 2 ** attempt);
            continue;
          }
          throw new ApiError(`Could not reach the RPC node ${this.url}`, 502);
        }
        if ((response.status === 429 || response.status >= 500) && attempt < this.retries) {
          await sleep(this.backoffMs * 2 ** attempt);
          continue;
        }
        let body = null;
        try {
          body = await response.json();
        } catch {
          /* handled below */
        }
        if (body && body.error) {
          const error = isPlainObject(body.error) ? body.error : { message: String(body.error) };
          throw new RpcError(error.code, String(error.message ?? ""));
        }
        if (response.status === 429) {
          throw new ApiError("Rate limited by the RPC node. Wait a moment, or add your own RPC URL.", 429);
        }
        if (!response.ok) throw new ApiError(`HTTP ${response.status} from ${this.url}`, 502);
        if (!body || !("result" in body)) throw new ApiError(`Unexpected response from ${this.url}`, 502);
        return body.result;
      }
    }

    /**
     * Call a method that takes maxSupportedTransactionVersion. When the node says
     * the data needs a newer version, retry once with it and remember it.
     */
    async versioned(method, target, options) {
      const attempt = () => this.call(method, [target, { ...options, maxSupportedTransactionVersion: this.maxVersion }]);
      try {
        return await attempt();
      } catch (error) {
        if (error instanceof RpcError && error.code === VERSION_NOT_SUPPORTED) {
          const match = /"?maxSupportedTransactionVersion"?\s*:\s*(\d+)/.exec(error.rpcMessage);
          if (match && Number(match[1]) > this.maxVersion) {
            this.maxVersion = Number(match[1]);
            return attempt();
          }
        }
        throw error;
      }
    }
  }

  /* ---------------------------------------------------------- explorer */

  /** Cached, validated lookups against one RPC endpoint. */
  class Explorer {
    constructor(url, config, rpcOptions) {
      this.rpc = new Rpc(url, rpcOptions);
      this.config = config;
      this.transactions = new Cache(Infinity, 256);
      this.blocks = new Cache(Infinity, 128);
      this.metadata = new Cache(Infinity, 4096);
      this.offchain = new Cache(Infinity, 1024);
      this.short = new Cache(4000);
      this.long = new Cache(600000);
    }

    async cached(cache, key, fetcher) {
      const hit = cache.get(key);
      if (hit !== undefined) return hit;
      return cache.set(key, await fetcher());
    }

    async optional(cache, key, fetcher) {
      try {
        return await this.cached(cache, key, fetcher);
      } catch {
        return null;
      }
    }

    async transaction(signature) {
      if (!isValidSignature(signature)) throw new ApiError(`'${signature}' is not a valid transaction signature`, 400);
      return this.cached(this.transactions, signature, async () => {
        const raw = await this.rpc.versioned("getTransaction", signature, { encoding: "jsonParsed", commitment: "confirmed" });
        if (!raw) {
          throw new ApiError(
            `Transaction ${signature} was not found (it may be too old for this node, not yet confirmed, or on a different cluster)`,
            404,
          );
        }
        return parseTransaction(signature, raw, this.config.programs || {});
      });
    }

    async network() {
      return this.cached(this.short, "network", async () => {
        const [epoch, samples, version] = await Promise.all([
          this.rpc.call("getEpochInfo", [{ commitment: "confirmed" }]),
          this.optional(this.short, "samples", () => this.rpc.call("getRecentPerformanceSamples", [30])),
          this.optional(this.long, "version", () => this.rpc.call("getVersion")),
        ]);
        return networkStats(epoch, samples, this.long.get("supply") || null, version);
      });
    }

    async supply() {
      const value = await this.cached(this.long, "supply", async () => {
        const result = await this.rpc.call("getSupply", [{ commitment: "confirmed", excludeNonCirculatingAccountsList: true }]);
        return result?.value || {};
      });
      const sol = (key) => (value[key] !== undefined ? lamportsToSol(value[key]) : null);
      return { total_sol: sol("total"), circulating_sol: sol("circulating"), non_circulating_sol: sol("nonCirculating") };
    }

    async recentSlots(count) {
      return this.cached(this.short, `slots-${count}`, async () => {
        const tip = await this.rpc.call("getSlot", [{ commitment: "confirmed" }]);
        // Some slots are skipped by their leader, so look a bit further back.
        const slots = await this.rpc.call("getBlocks", [Math.max(0, tip - count * 3 - 10), tip, { commitment: "confirmed" }]);
        return [...(slots || [])].sort((a, b) => b - a).slice(0, count);
      });
    }

    async block(slot, details = "accounts") {
      return this.cached(this.blocks, `${slot}:${details}`, async () => {
        let raw;
        try {
          raw = await this.rpc.versioned("getBlock", slot, {
            encoding: "jsonParsed",
            transactionDetails: details,
            rewards: true,
            commitment: "confirmed",
          });
        } catch (error) {
          if (error instanceof RpcError && BLOCK_UNAVAILABLE.has(error.code)) {
            throw new ApiError(`No block is available for slot ${slot}: ${error.rpcMessage}`, 404);
          }
          throw error;
        }
        if (!raw) throw new ApiError(`No block is available for slot ${slot}`, 404);
        return parseBlock(slot, raw);
      });
    }

    async latestBlocks(limit) {
      const slots = await this.recentSlots(Math.max(1, Math.min(limit, 20)));
      const results = await Promise.allSettled(slots.map((s) => this.block(s, "signatures")));
      const blocks = results.filter((r) => r.status === "fulfilled").map((r) => r.value);
      if (!blocks.length && results.length) throw results[0].reason;
      return blocks;
    }

    async latestTransactions(limit) {
      for (const slot of await this.recentSlots(3)) {
        try {
          const block = await this.block(slot, "accounts");
          return {
            slot: block.slot,
            block_time: block.block_time,
            transactions: block.transactions.filter((t) => !t.is_vote).slice(0, limit),
          };
        } catch (error) {
          if (error.status !== 404) throw error;
        }
      }
      throw new ApiError("No recent block is available", 404);
    }

    async account(address) {
      if (!isValidAddress(address)) throw new ApiError(`'${address}' is not a valid Solana address`, 400);
      const result = await this.rpc.call("getAccountInfo", [address, { encoding: "jsonParsed", commitment: "confirmed" }]);
      return parseAccount(address, result?.value ?? null);
    }

    /** Name/symbol/uri per mint: Metaplex first, then Token-2022, then built-in labels. */
    async tokenMetadata(mints) {
      const unique = [...new Set(mints.filter(Boolean))];
      const result = {};
      const missing = [];
      for (const mint of unique) {
        const hit = this.metadata.get(mint);
        if (hit === undefined) missing.push(mint);
        else if (hit) result[mint] = hit;
      }
      if (!missing.length) return result;

      const found = {};
      for (let i = 0; i < missing.length; i += MULTIPLE_ACCOUNTS_LIMIT) {
        const chunk = missing.slice(i, i + MULTIPLE_ACCOUNTS_LIMIT);
        const pdas = await Promise.all(chunk.map(metadataAddress));
        const res = await this.rpc.call("getMultipleAccounts", [pdas, { encoding: "base64", commitment: "confirmed" }]);
        chunk.forEach((mint, j) => {
          const md = metadataFromAccount(mint, res?.value?.[j]);
          if (md) found[mint] = md;
        });
      }
      const remaining = missing.filter((m) => !found[m]);
      const builtin = this.config.tokens || {};
      for (let i = 0; i < remaining.length; i += MULTIPLE_ACCOUNTS_LIMIT) {
        const chunk = remaining.slice(i, i + MULTIPLE_ACCOUNTS_LIMIT);
        const res = await this.rpc.call("getMultipleAccounts", [chunk, { encoding: "jsonParsed", commitment: "confirmed" }]);
        chunk.forEach((mint, j) => {
          const data = res?.value?.[j]?.data;
          const md =
            token2022Metadata(mint, isPlainObject(data) ? data.parsed : null) ||
            (builtin[mint] ? { mint, name: builtin[mint], symbol: builtin[mint], uri: null, source: "builtin" } : null);
          if (md) found[mint] = md;
        });
      }
      for (const mint of missing) this.metadata.set(mint, found[mint] || null);
      return { ...result, ...found };
    }

    async tokenLabels(mints) {
      try {
        const metadata = await this.tokenMetadata(mints);
        return Object.fromEntries(Object.entries(metadata).map(([m, md]) => [m, { name: md.name, symbol: md.symbol }]));
      } catch {
        return {};
      }
    }

    async tokenInfo(account) {
      const info = account.parsed?.info || {};
      const decimals = Number(info.decimals ?? 0);
      const token = {
        mint: account.address,
        program: account.program,
        decimals,
        supply: scaleAmount(toBigInt(info.supply), decimals),
        mint_authority: info.mintAuthority ?? null,
        freeze_authority: info.freezeAuthority ?? null,
        extensions: (info.extensions || []).filter(Boolean).map((e) => e.extension ?? ""),
        name: null,
        symbol: null,
        metadata_source: null,
        uri: null,
        image: null,
        description: null,
        website: null,
      };
      let md = null;
      try {
        md = (await this.tokenMetadata([account.address]))[account.address];
      } catch {
        /* metadata is optional */
      }
      if (!md) return token;
      const offchain = md.uri ? await this.cached(this.offchain, md.uri, () => fetchOffchain(md.uri)) : null;
      return {
        ...token,
        name: md.name,
        symbol: md.symbol,
        metadata_source: md.source,
        uri: md.uri,
        image: offchain?.image ?? null,
        description: offchain?.description ?? null,
        website: offchain?.website ?? null,
      };
    }

    /** SPL Token and Token-2022 balances; non-empty and named tokens first. */
    async holdings(owner) {
      if (!isValidAddress(owner)) throw new ApiError(`'${owner}' is not a valid Solana address`, 400);
      const holdings = [];
      for (const programId of TOKEN_PROGRAM_IDS) {
        const result = await this.rpc.call("getTokenAccountsByOwner", [
          owner,
          { programId },
          { encoding: "jsonParsed", commitment: "confirmed" },
        ]);
        for (const raw of result?.value || []) {
          const data = raw.account?.data || {};
          const info = data.parsed?.info || {};
          if (!info.mint) continue;
          const decimals = Number(info.tokenAmount?.decimals ?? 0);
          holdings.push({
            token_account: raw.pubkey ?? "",
            mint: info.mint,
            amount: scaleAmount(toBigInt(info.tokenAmount?.amount), decimals),
            decimals,
            program: data.program ?? "spl-token",
            name: null,
            symbol: null,
          });
        }
      }
      const metadata = holdings.length ? await this.tokenMetadata(holdings.map((h) => h.mint)) : {};
      for (const h of holdings) {
        h.name = metadata[h.mint]?.name ?? null;
        h.symbol = metadata[h.mint]?.symbol ?? null;
      }
      const empty = (h) => Number(Number(h.amount) === 0);
      const unnamed = (h) => Number(h.symbol === null);
      return holdings.sort((a, b) => empty(a) - empty(b) || unnamed(a) - unnamed(b) || Number(b.amount) - Number(a.amount));
    }

    async signatures(address, limit, before) {
      if (!isValidAddress(address)) throw new ApiError(`'${address}' is not a valid Solana address`, 400);
      if (before && !isValidSignature(before)) throw new ApiError(`'${before}' is not a valid transaction signature`, 400);
      const options = { limit: Math.max(1, Math.min(limit, MAX_HISTORY)), commitment: "confirmed" };
      if (before) options.before = before;
      const result = await this.rpc.call("getSignaturesForAddress", [address, options]);
      return (result || []).map(parseSignature);
    }

    async holders(mint) {
      const info = await this.tokenInfo(await this.account(mint));
      const result = await this.rpc.call("getTokenLargestAccounts", [mint, { commitment: "confirmed" }]);
      const supply = Number(info.supply);
      return (result?.value || []).map((raw) => {
        const amount = scaleAmount(toBigInt(raw.amount), Number(raw.decimals ?? 0));
        return { token_account: raw.address ?? "", amount, share: supply ? Number(amount) / supply : null };
      });
    }
  }

  /* ------------------------------------------------------------ routes */

  function storage() {
    try {
      return root.localStorage || null;
    } catch {
      return null;
    }
  }

  function createBackend(config, rpcOptions) {
    const explorers = new Map();

    const backend = {
      config,

      customRpc() {
        return storage()?.getItem(CUSTOM_RPC_KEY) || null;
      },

      setCustomRpc(url) {
        const value = String(url || "").trim();
        if (value && !/^https:\/\//.test(value)) throw new Error("The RPC URL must start with https://");
        if (value) storage()?.setItem(CUSTOM_RPC_KEY, value);
        else storage()?.removeItem(CUSTOM_RPC_KEY);
        explorers.delete("custom");
      },

      endpointFor(cluster) {
        return cluster === "custom" ? backend.customRpc() : config.endpoints[cluster] || null;
      },

      explorer(cluster) {
        const url = backend.endpointFor(cluster);
        if (!url) {
          throw new ApiError(
            cluster === "custom" ? "No custom RPC URL is set. Pick \"custom\" in the cluster menu to add one." : `Unknown cluster '${cluster}'`,
            400,
          );
        }
        if (!explorers.has(cluster)) explorers.set(cluster, new Explorer(url, config, rpcOptions));
        return explorers.get(cluster);
      },

      async handle(path, params = {}) {
        const cluster = params.cluster || config.default_cluster;
        try {
          return await route(path, params, cluster);
        } catch (error) {
          throw toApiError(error, cluster);
        }
      },
    };

    function intParam(params, name, fallback, low, high) {
      if (params[name] === undefined || params[name] === null) return fallback;
      const n = Number.parseInt(params[name], 10);
      if (Number.isNaN(n)) throw new ApiError(`${name} must be an integer`, 400);
      return Math.max(low, Math.min(high, n));
    }

    async function route(path, params, cluster) {
      let m;
      if (path === "/api/search") return search(params.q);
      const ex = backend.explorer(cluster);
      if (path === "/api/network") return ex.network();
      if (path === "/api/supply") return ex.supply();
      if (path === "/api/blocks/latest") return ex.latestBlocks(intParam(params, "limit", 8, 1, 20));
      if (path === "/api/transactions/latest") return ex.latestTransactions(intParam(params, "limit", 12, 1, 50));
      if ((m = /^\/api\/tx\/([^/]+)$/.exec(path))) {
        const tx = await ex.transaction(m[1]);
        const mints = new Set([...tx.transfers.map((t) => t.mint).filter(Boolean), ...tx.token_changes.map((c) => c.mint)]);
        return { ...tx, tokens: await ex.tokenLabels([...mints].sort()) };
      }
      if ((m = /^\/api\/block\/(\d+)$/.exec(path))) return ex.block(Number(m[1]), "accounts");
      if ((m = /^\/api\/account\/([^/]+)\/tokens$/.exec(path))) return ex.holdings(m[1]);
      if ((m = /^\/api\/account\/([^/]+)\/transactions$/.exec(path))) {
        return ex.signatures(m[1], intParam(params, "limit", 25, 1, 100), params.before || null);
      }
      if ((m = /^\/api\/token\/([^/]+)\/holders$/.exec(path))) return ex.holders(m[1]);
      if ((m = /^\/api\/account\/([^/]+)$/.exec(path))) return accountPage(ex, m[1]);
      throw new ApiError("Unknown API endpoint", 404);
    }

    function search(query) {
      const term = String(query || "").trim();
      if (/^\d+$/.test(term)) return { type: "block", value: term };
      if (isValidSignature(term)) return { type: "tx", value: term };
      if (isValidAddress(term)) return { type: "account", value: term };
      throw new ApiError("Enter a transaction signature, an account or token address, or a block number", 400);
    }

    async function accountPage(ex, address) {
      const account = await ex.account(address);
      const kind = accountKind(account);
      const data = { account, kind };
      if (kind === "token_mint") data.token = await ex.tokenInfo(account);
      if (kind === "token_account") {
        const info = account.parsed?.info || {};
        const labels = info.mint ? await ex.tokenLabels([info.mint]) : {};
        data.token_account = {
          mint: info.mint ?? null,
          owner: info.owner ?? null,
          amount: info.tokenAmount?.uiAmountString ?? null,
          state: info.state ?? null,
          token: info.mint ? (labels[info.mint] ?? null) : null,
        };
      }
      return data;
    }

    function toApiError(error, cluster) {
      if (error instanceof ApiError) return error;
      if (error instanceof RpcError) {
        // Free endpoints refuse "indexed" methods (supply, token lists, holders).
        if (error.code === 403 || /personal token|request blocked|not available on|forbidden/i.test(error.rpcMessage)) {
          return new ApiError(
            `The ${cluster} RPC endpoint does not provide this data (${error.rpcMessage}). Add your own RPC URL with "custom" in the cluster menu to unlock it.`,
            403,
          );
        }
        return new ApiError(error.message, 502);
      }
      return new ApiError(String(error?.message || error), 500);
    }

    return backend;
  }

  const exported = {
    ApiError,
    RpcError,
    createBackend,
    b58decode,
    b58encode,
    isValidSignature,
    isValidAddress,
    scaleAmount,
    parseTransaction,
    parseBlock,
    parseAccount,
    parseSignature,
    accountKind,
    networkStats,
    isOnCurve,
    findProgramAddress,
    metadataAddress,
    parseMetaplex,
    metadataFromAccount,
    token2022Metadata,
    normalizeUri,
    Rpc,
  };
  root.SolanaBackend = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof globalThis !== "undefined" ? globalThis : this);
