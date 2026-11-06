"use strict";

/*
 * Solana Explorer single-page app.
 * Data comes from backend.js (Solana RPC, called from the browser); pages are
 * addressed with the URL hash (#/tx/<sig>, #/account/<address>, #/block/<slot>)
 * so the site works on any static host, including GitHub Pages.
 */

const { esc, short, fmtDec, signed, lamportsToSol, fmtInt, compact, ago, utc, duration } = window.SolanaFormat;
const backend = window.SolanaBackend.createBackend(window.SOLANA_EXPLORER_CONFIG);

const state = {
  config: backend.config,
  cluster: null,
  route: { path: "/", params: new URLSearchParams() },
  navId: 0,
  timers: [],
  lazyTabs: {},
};
const app = document.getElementById("app");
const REFRESH_MS = 15000;
const ADDRESS_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

/* ---------------------------------------------------------------- routing */

/** "#/tx/<sig>?cluster=devnet&tab=logs" -> { path: "/tx/<sig>", params } */
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  return { path: path || "/", params: new URLSearchParams(query) };
}

function hashFor(path, extra = {}, cluster = state.cluster) {
  const params = new URLSearchParams();
  if (cluster && cluster !== state.config.default_cluster) params.set("cluster", cluster);
  for (const [key, value] of Object.entries(extra)) if (value !== null && value !== undefined) params.set(key, value);
  const query = params.toString();
  return `#${path}${query ? "?" + query : ""}`;
}

const link = (path) => hashFor(path);

/** ?cluster= suffix for links to other explorers (they only know the public clusters). */
const externalClusterQuery = () => (["devnet", "testnet"].includes(state.cluster) ? `?cluster=${state.cluster}` : "");

/* ---------------------------------------------------------------- helpers */

const COPY_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3h11a2 2 0 0 1 2 2v11h-2V5H8V3Zm-3 4h11a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Zm0 2v11h11V9H5Z"/></svg>';

const copyBtn = (value) => `<button class="copy" type="button" data-copy="${esc(value)}" title="Copy" aria-label="Copy">${COPY_ICON}</button>`;

function labelFor(address) {
  return state.config?.labels?.[address] || null;
}

/** Address link: shows a known label or a shortened address, with a copy button. */
function addr(address, { full = false, copy = true, text = null } = {}) {
  if (!address) return '<span class="faint">-</span>';
  const label = text || labelFor(address);
  const shown = label ? `<span class="label-tag">${esc(label)}</span>` : esc(full ? address : short(address));
  return `<a class="addr" href="${link("/account/" + address)}" title="${esc(address)}">${shown}</a>${copy ? copyBtn(address) : ""}`;
}

function txLink(signature, { full = false, copy = false } = {}) {
  return `<a class="addr" href="${link("/tx/" + signature)}" title="${esc(signature)}">${esc(full ? signature : short(signature, 8, 8))}</a>${copy ? copyBtn(signature) : ""}`;
}

const blockLink = (slot) => (slot === null || slot === undefined ? "-" : `<a href="${link("/block/" + slot)}">${fmtInt(slot)}</a>`);

const statusBadge = (ok, failText = "Failed") =>
  ok ? '<span class="badge success dot">Success</span>' : `<span class="badge fail dot">${esc(failText)}</span>`;

function avatar(seed, label) {
  let hash = 0;
  for (const c of String(seed)) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  const hue = hash % 360;
  const initials = esc((label || seed || "?").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "?");
  return `<span class="avatar" style="background:linear-gradient(135deg,hsl(${hue} 70% 55%),hsl(${(hue + 60) % 360} 70% 45%))">${initials}</span>`;
}

function tokenName(mint, tokens = {}) {
  const meta = tokens[mint];
  return labelFor(mint) || meta?.symbol || meta?.name || short(mint, 4, 4);
}

const safeImage = (url) => (typeof url === "string" && url.startsWith("https://") ? url : null);

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 2400);
}

/* -------------------------------------------------------------------- api */

function api(path, params = {}) {
  return backend.handle(path, { ...params, cluster: state.cluster });
}

/* ----------------------------------------------------------------- layout */

const card = (title, body, extra = "") =>
  `<section class="card">${title ? `<div class="card-header"><h2>${title}</h2>${extra}</div>` : ""}${body}</section>`;

const kv = (rows) =>
  `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;

function table(headers, rows, empty = "No data") {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const head = headers.map((h) => (typeof h === "string" ? `<th>${esc(h)}</th>` : `<th class="${h.cls}">${esc(h.label)}</th>`)).join("");
  return `<div class="table-wrap"><table class="table"><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

const skeletonRows = (n = 6) => Array.from({ length: n }, () => '<span class="skeleton block"></span>').join("");

function errorBox(error, title = "Something went wrong") {
  const notFound = error?.status === 404;
  const limited = error?.status === 429;
  return `<div class="error-box"><h2>${esc(notFound ? "Not found" : limited ? "Rate limited" : title)}</h2>
    <p>${esc(error?.message || String(error))}</p>
    ${limited ? '<p class="faint">The RPC node limits request rates. Wait a moment and reload, or add your own RPC URL.</p>' : ""}
    ${limited || error?.status === 403 ? '<p><button class="btn" type="button" data-action="custom-rpc">Add your RPC URL</button></p>' : ""}</div>`;
}

function tabs(id, items) {
  const buttons = items
    .map((t, i) => `<button class="tab${i === 0 ? " active" : ""}" type="button" data-tab="${id}:${t.key}">${esc(t.label)}${t.count !== undefined ? `<span class="count">${esc(t.count)}</span>` : ""}</button>`)
    .join("");
  const panels = items.map((t, i) => `<div class="tab-panel" data-panel="${id}:${t.key}"${i === 0 ? "" : " hidden"}>${t.body || ""}</div>`).join("");
  return `<div class="tabs" role="tablist">${buttons}</div>${panels}`;
}

function setTitle(text) {
  document.title = text ? `${text} | Solana Explorer` : "Solana Explorer";
}

/* ------------------------------------------------------------------ pages */

async function renderHome(nav) {
  document.body.classList.add("home");
  setTitle("");
  app.innerHTML = `
    <section class="hero">
      <h1>The <span>Solana</span> Blockchain Explorer</h1>
      <p>Search transactions, wallets, tokens and blocks on ${esc(state.cluster)}</p>
      <form class="search search-hero" id="hero-search" role="search">
        <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.5 3a7.5 7.5 0 0 1 5.9 12.1l4.2 4.2-1.4 1.4-4.2-4.2A7.5 7.5 0 1 1 10.5 3Zm0 2a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z"/></svg>
        <input name="q" type="search" autocomplete="off" spellcheck="false" placeholder="Search by signature, address, token or block number" aria-label="Search">
        <button type="submit">Search</button>
      </form>
    </section>
    <div class="stats" id="stats">${statCards(null, null)}</div>
    <div class="grid-2">
      ${card("Latest Blocks", `<div id="latest-blocks">${skeletonRows()}</div>`, '<span class="badge dot success">Live</span>')}
      ${card("Latest Transactions", `<div id="latest-txs">${skeletonRows()}</div>`, '<span class="muted" id="latest-txs-slot"></span>')}
    </div>`;

  let supply = null;
  api("/api/supply")
    .then((s) => (supply = s))
    .catch((error) => (supply = { error }));

  const refresh = async () => {
    if (document.hidden) return;
    const [network, blocks, txs] = await Promise.allSettled([
      api("/api/network"),
      api("/api/blocks/latest", { limit: 8 }),
      api("/api/transactions/latest", { limit: 12 }),
    ]);
    if (nav !== state.navId) return;
    if (network.status === "fulfilled") document.getElementById("stats").innerHTML = statCards(network.value, supply);
    document.getElementById("latest-blocks").innerHTML =
      blocks.status === "fulfilled" ? latestBlocksTable(blocks.value) : errorBox(blocks.reason);
    if (txs.status === "fulfilled") {
      document.getElementById("latest-txs").innerHTML = latestTxTable(txs.value);
      document.getElementById("latest-txs-slot").innerHTML = `from block ${blockLink(txs.value.slot)}`;
    } else {
      document.getElementById("latest-txs").innerHTML = errorBox(txs.reason);
    }
  };
  await refresh();
  state.timers.push(setInterval(refresh, REFRESH_MS));
}

function sparkline(samples) {
  const values = samples.map((s) => s.tps).reverse();
  if (values.length < 2) return "";
  const w = 300;
  const h = 54;
  const max = Math.max(...values) * 1.1 || 1;
  const min = Math.min(...values) * 0.8;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * w, h - ((v - min) / (max - min || 1)) * (h - 6) - 3]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="spark" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#9945ff" stop-opacity="0.35"/><stop offset="1" stop-color="#9945ff" stop-opacity="0"/></linearGradient>
    <linearGradient id="sparkline" x1="0" x2="1"><stop offset="0" stop-color="#9945ff"/><stop offset="1" stop-color="#14f195"/></linearGradient></defs>
    <path d="${line} L${w},${h} L0,${h} Z" fill="url(#spark)"/><path d="${line}" fill="none" stroke="url(#sparkline)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
}

function statCards(n, supply) {
  const sk = '<span class="skeleton"></span>';
  const total = supply?.total_sol || n?.total_supply_sol;
  const circ = supply?.circulating_sol || n?.circulating_supply_sol;
  const circPct = total && circ ? (Number(circ) / Number(total)) * 100 : null;
  const progress = n ? (n.epoch_progress * 100).toFixed(1) : null;
  return `
    <div class="card stat">
      <div class="stat-label">SOL Supply</div>
      <div class="stat-value">${total ? compact(total) : supply?.error ? "-" : sk} <small>SOL</small></div>
      <div class="stat-sub">${circ ? `Circulating ${compact(circ)} (${circPct.toFixed(1)}%)` : supply?.error ? 'Not offered by this RPC &middot; <a href="#" data-action="custom-rpc">add yours</a>' : "Loading supply..."}</div>
      <div class="progress"><span style="width:${circPct ?? 0}%"></span></div>
    </div>
    <div class="card stat">
      <div class="stat-label">Epoch</div>
      <div class="stat-value">${n ? fmtInt(n.epoch) : sk} <small>${progress ? progress + "%" : ""}</small></div>
      <div class="stat-sub">${n ? `${fmtInt(n.slot_index)} / ${fmtInt(n.slots_in_epoch)} slots &middot; ~${duration(n.epoch_eta_seconds)} left` : "&nbsp;"}</div>
      <div class="progress"><span style="width:${progress ?? 0}%"></span></div>
    </div>
    <div class="card stat">
      <div class="stat-label">Slot &middot; Block height</div>
      <div class="stat-value">${n ? fmtInt(n.absolute_slot) : sk}</div>
      <div class="stat-sub">${n ? `Block height ${fmtInt(n.block_height)} &middot; ${fmtInt(n.transaction_count)} txs total` : "&nbsp;"}</div>
    </div>
    <div class="card stat has-spark">
      <div class="stat-label">TPS</div>
      <div class="stat-value">${n?.tps ? fmtInt(Math.round(n.tps)) : sk}</div>
      <div class="stat-sub">${n?.non_vote_tps ? `True TPS (non-vote) ${fmtInt(Math.round(n.non_vote_tps))}` : "&nbsp;"}${n?.node_version ? ` &middot; v${esc(n.node_version)}` : ""}</div>
      ${n ? sparkline(n.samples) : ""}
    </div>`;
}

function latestBlocksTable(blocks) {
  return table(
    ["Block", "Age", "Txs", "Leader", { label: "Reward (SOL)", cls: "right" }],
    blocks.map(
      (b) => `<tr><td>${blockLink(b.slot)}</td><td class="muted" title="${esc(utc(b.block_time))}">${ago(b.block_time)}</td>
        <td>${fmtInt(b.transaction_count)}</td><td>${addr(b.leader, { copy: false })}</td>
        <td class="right">${fmtDec(b.leader_reward_sol, 5)}</td></tr>`,
    ),
    "No recent blocks",
  );
}

function latestTxTable(data) {
  return table(
    ["Signature", "Signer", "Fee (SOL)", "Status"],
    data.transactions.map(
      (t) => `<tr><td>${txLink(t.signature)}</td><td>${addr(t.signer, { copy: false })}</td>
        <td>${lamportsToSol(t.fee_lamports)}</td><td>${statusBadge(t.success)}</td></tr>`,
    ),
    "No non-vote transactions in the latest block",
  );
}

/* ----------------------------------------------------------- transaction */

async function renderTx(nav, signature) {
  setTitle("Transaction");
  app.innerHTML = `<div class="page-title"><h1>Transaction Details</h1></div>${card("", '<div class="spinner"></div>')}`;
  let tx;
  try {
    tx = await api(`/api/tx/${encodeURIComponent(signature)}`);
  } catch (error) {
    if (nav === state.navId) app.innerHTML = `<div class="page-title"><h1>Transaction Details</h1></div>${card("", errorBox(error))}`;
    return;
  }
  if (nav !== state.navId) return;
  const tokens = tx.tokens || {};
  const otherSigners = tx.signers.filter((s) => s !== tx.accounts[0]?.pubkey);
  const overview = kv([
    ["Signature", `<span class="mono">${esc(tx.signature)}</span>${copyBtn(tx.signature)}`],
    ["Block", `${blockLink(tx.slot)}`],
    ["Timestamp", tx.block_time ? `${ago(tx.block_time)} <span class="muted">(${utc(tx.block_time)})</span>` : "-"],
    ["Result", tx.success ? statusBadge(true) : `${statusBadge(false)} <span class="mono muted">${esc(JSON.stringify(tx.error))}</span>`],
    ["Fee payer", addr(tx.accounts[0]?.pubkey, { full: true })],
    otherSigners.length ? ["Other signers", otherSigners.map((s) => addr(s, { full: true })).join(" ")] : null,
    ["Fee", `${fmtDec(tx.fee_sol)} SOL`],
    tx.compute_units !== null ? ["Compute units consumed", fmtInt(tx.compute_units)] : null,
    ["Transaction version", esc(tx.version)],
    ["Recent blockhash", `<span class="mono">${esc(tx.recent_blockhash || "-")}</span>`],
  ]);

  const transferRows = tx.transfers.map((t) => {
    const asset = t.asset === "SOL" ? "SOL" : t.mint ? `<a href="${link("/account/" + t.mint)}">${esc(tokenName(t.mint, tokens))}</a>` : "unknown token";
    const amount = t.asset !== "SOL" && t.decimals === null ? `${fmtDec(t.amount)} <span class="faint">(raw)</span>` : fmtDec(t.amount);
    return `<tr><td>${addr(t.source, { copy: false })}</td><td class="faint">&rarr;</td><td>${addr(t.destination, { copy: false })}</td>
      <td class="right">${amount}</td><td>${asset}</td></tr>`;
  });
  const transfers = card(
    tx.success ? "Transfers" : "Attempted transfers <span class=\"badge fail\">reverted</span>",
    table(["From", "", "To", { label: "Amount", cls: "right" }, "Token"], transferRows, "No SOL or token transfers decoded"),
  );

  const solRows = tx.sol_changes.map(
    (c) => `<tr><td>${addr(c.account)}</td><td class="right">${fmtDec(lamportsToSol(c.pre_lamports))}</td>
      <td class="right">${fmtDec(lamportsToSol(c.post_lamports))}</td>
      <td class="right ${c.change_lamports < 0 ? "down" : "up"}">${signed(lamportsToSol(c.change_lamports))}</td></tr>`,
  );
  const tokenRows = tx.token_changes.map(
    (c) => `<tr><td>${addr(c.owner || c.account)}</td><td><a href="${link("/account/" + c.mint)}">${esc(tokenName(c.mint, tokens))}</a></td>
      <td class="right">${fmtDec(c.pre_amount)}</td><td class="right">${fmtDec(c.post_amount)}</td>
      <td class="right ${String(c.change).startsWith("-") ? "down" : "up"}">${signed(c.change)}</td></tr>`,
  );
  const balances =
    card("SOL Balance Change", table(["Address", { label: "Before (SOL)", cls: "right" }, { label: "After (SOL)", cls: "right" }, { label: "Change (SOL)", cls: "right" }], solRows, "No SOL balance changes")) +
    (tokenRows.length
      ? card("Token Balance Change", table(["Owner", "Token", { label: "Before", cls: "right" }, { label: "After", cls: "right" }, { label: "Change", cls: "right" }], tokenRows))
      : "");

  const logs = tx.log_messages.length
    ? `<pre class="code">${tx.log_messages
        .map((line) => {
          const cls = / invoke \[/.test(line) ? "invoke" : / success$/.test(line) ? "success" : /failed|error/i.test(line) ? "failed" : "";
          return `<span class="log-line ${cls}">${esc(line)}</span>`;
        })
        .join("\n")}</pre>`
    : '<div class="empty">No log messages</div>';

  app.innerHTML = `
    <div class="page-title"><h1>Transaction Details</h1>${statusBadge(tx.success)}</div>
    ${card("Overview", overview)}
    <section class="card">${tabs("tx", [
      { key: "overview", label: "Transfers & Balances", body: `<div class="card-body">${transfers}${balances}</div>` },
      { key: "ix", label: "Instructions", count: tx.instructions.filter((i) => !i.inner).length, body: `<div class="card-body">${instructionsView(tx.instructions)}</div>` },
      { key: "logs", label: "Program Logs", count: tx.log_messages.length, body: logs },
      { key: "raw", label: "Raw", body: `<pre class="code">${esc(JSON.stringify(tx, null, 2))}</pre>` },
    ])}</section>`;
}

function infoValue(value) {
  if (value === null || value === undefined) return '<span class="faint">null</span>';
  if (typeof value === "string") return ADDRESS_RE.test(value) ? addr(value, { full: true }) : `<span class="mono">${esc(value)}</span>`;
  if (typeof value === "number" || typeof value === "boolean") return `<span class="mono">${esc(value)}</span>`;
  if (value.uiAmountString !== undefined) return `<span class="mono">${fmtDec(value.uiAmountString)}</span>`;
  return `<span class="mono">${esc(JSON.stringify(value))}</span>`;
}

function instructionCard(ix, children, innerCount = 0) {
  let body;
  if (ix.info && typeof ix.info === "object" && !Array.isArray(ix.info)) {
    body = kv(Object.entries(ix.info).map(([k, v]) => [k, infoValue(v)]));
  } else if (typeof ix.info === "string") {
    body = kv([["Data", `<span class="mono">${esc(ix.info)}</span>`]]);
  } else {
    body = kv([
      ["Program", addr(ix.program_id, { full: true })],
      ...ix.accounts.map((a, i) => [`Account #${i + 1}`, addr(a, { full: true })]),
      ["Instruction data", `<span class="mono">${esc(ix.data || "-")}</span>`],
    ]);
  }
  const name = labelFor(ix.program_id) || ix.program;
  return `<div class="ix${ix.inner ? " inner" : ""}">
    <div class="ix-head" data-action="toggle-ix"><span class="pos">#${esc(ix.position)}</span>
      <span class="name">${esc(ADDRESS_RE.test(name) ? short(name) : name)}</span>${ix.type ? `<span class="type">${esc(ix.type)}</span>` : ""}
      ${innerCount ? `<span class="badge">+${innerCount} inner</span>` : ""}<span class="chev">&rsaquo;</span></div>
    <div class="ix-body">${body}${children}</div></div>`;
}

function instructionsView(instructions) {
  const top = instructions.filter((i) => !i.inner);
  if (!top.length) return '<div class="empty">No instructions</div>';
  return top
    .map((ix) => {
      const inner = instructions.filter((i) => i.inner && i.position.split(".")[0] === ix.position);
      return instructionCard(ix, inner.map((child) => instructionCard(child, "")).join(""), inner.length);
    })
    .join("");
}

/* --------------------------------------------------------------- account */

const KIND_LABELS = {
  wallet: "Wallet",
  token_mint: "Token",
  token_account: "Token Account",
  program: "Program",
  vote: "Vote Account",
  stake: "Stake Account",
  nonce: "Nonce Account",
  account: "Data Account",
  empty: "Empty Account",
};

async function renderAccount(nav, address) {
  setTitle("Account");
  app.innerHTML = `<div class="page-title"><h1>Account</h1></div>${card("", '<div class="spinner"></div>')}`;
  let data;
  try {
    data = await api(`/api/account/${encodeURIComponent(address)}`);
  } catch (error) {
    if (nav === state.navId) app.innerHTML = `<div class="page-title"><h1>Account</h1></div>${card("", errorBox(error))}`;
    return;
  }
  if (nav !== state.navId) return;
  const { account, kind, token } = data;
  const label = labelFor(address);
  const badge = `<span class="badge kind">${esc(KIND_LABELS[kind] || kind)}</span>`;

  let title;
  if (token) {
    const image = safeImage(token.image);
    const logo = image ? `<img class="token-logo" src="${esc(image)}" alt="" referrerpolicy="no-referrer" loading="lazy">` : avatar(address, token.symbol || token.name);
    title = `${logo}<h1>${esc(token.name || "Unknown token")}</h1>${token.symbol ? `<span class="muted">(${esc(token.symbol)})</span>` : ""}${badge}`;
    setTitle(token.symbol || token.name || "Token");
  } else {
    title = `<h1>${esc(label || KIND_LABELS[kind] || "Account")}</h1>${badge}`;
  }

  const overviewRows = [
    ["Address", `<span class="mono">${esc(address)}</span>${copyBtn(address)}`],
    ["SOL balance", `${fmtDec(account.balance_sol)} SOL`],
    ["Owner program", addr(account.owner, { full: !labelFor(account.owner) })],
    ["Executable", account.executable ? "Yes" : "No"],
    account.space !== null ? ["Data size", `${fmtInt(account.space)} bytes`] : null,
  ];
  if (data.token_account) {
    const ta = data.token_account;
    const symbol = ta.token?.symbol || ta.token?.name || labelFor(ta.mint) || short(ta.mint);
    overviewRows.push(
      ["Token", `<a href="${link("/account/" + ta.mint)}">${esc(symbol)}</a> <span class="faint mono">${esc(short(ta.mint))}</span>`],
      ["Token balance", `${fmtDec(ta.amount)} ${esc(symbol)}`],
      ["Token owner", addr(ta.owner, { full: true })],
      ["State", esc(ta.state || "-")],
    );
  }

  let tokenCard = "";
  if (token) {
    tokenCard = card(
      "Token Overview",
      kv([
        ["Supply", `${fmtDec(token.supply)} ${esc(token.symbol || "")}`],
        ["Decimals", fmtInt(token.decimals)],
        ["Mint authority", token.mint_authority ? addr(token.mint_authority, { full: true }) : '<span class="muted">Disabled (fixed supply)</span>'],
        ["Freeze authority", token.freeze_authority ? addr(token.freeze_authority, { full: true }) : '<span class="muted">Disabled</span>'],
        ["Token program", token.program === "spl-token-2022" ? "Token-2022" : "SPL Token"],
        token.extensions?.length ? ["Extensions", token.extensions.map((e) => `<span class="badge">${esc(e)}</span>`).join(" ")] : null,
        token.website && safeImage(token.website) ? ["Website", `<a href="${esc(token.website)}" target="_blank" rel="noopener noreferrer">${esc(token.website)}</a>`] : null,
        token.description ? ["Description", `<span>${esc(token.description)}</span>`] : null,
        token.metadata_source ? ["Metadata source", esc(token.metadata_source)] : null,
      ]),
    );
  }

  const tabItems = [{ key: "txs", label: "Transactions", body: `<div id="acct-txs">${skeletonRows(8)}</div>` }];
  if (kind === "token_mint") tabItems.push({ key: "holders", label: "Top Holders", body: '<div id="acct-holders"></div>' });
  if (kind === "wallet" || kind === "account" || kind === "empty") tabItems.push({ key: "tokens", label: "Token Holdings", body: '<div id="acct-tokens"></div>' });
  tabItems.push({ key: "raw", label: "Raw", body: `<pre class="code">${esc(JSON.stringify(data, null, 2))}</pre>` });

  app.innerHTML = `
    <div class="page-title">${title}</div>
    <div class="grid-2" style="margin-top:0">${card("Overview", kv(overviewRows))}${tokenCard || card("More info", kv([
      ["Account type", esc(KIND_LABELS[kind] || kind)],
      ["Parser", esc(account.program || "-")],
      ["Explorer links", `<a href="https://solscan.io/account/${esc(address)}${externalClusterQuery()}" target="_blank" rel="noopener noreferrer">Solscan</a> &middot; <a href="https://explorer.solana.com/address/${esc(address)}${externalClusterQuery()}" target="_blank" rel="noopener noreferrer">Solana Explorer</a>`],
    ]))}</div>
    <section class="card" style="margin-top:20px">${tabs("acct", tabItems)}</section>`;

  loadAccountTransactions(nav, address, null);
  state.lazyTabs = {
    "acct:holders": () => loadHolders(nav, address, token),
    "acct:tokens": () => loadTokens(nav, address),
  };
}

async function loadAccountTransactions(nav, address, before) {
  const container = document.getElementById("acct-txs");
  if (!container) return;
  let rows;
  try {
    rows = await api(`/api/account/${encodeURIComponent(address)}/transactions`, before ? { before, limit: 25 } : { limit: 25 });
  } catch (error) {
    if (nav === state.navId) container.innerHTML = errorBox(error);
    return;
  }
  if (nav !== state.navId) return;
  const html = rows.map(
    (s) => `<tr><td>${txLink(s.signature)}</td><td>${blockLink(s.slot)}</td>
      <td class="muted" title="${esc(utc(s.block_time))}">${ago(s.block_time)}</td><td>${statusBadge(s.success)}</td>
      <td class="wrap muted">${esc(s.memo || "")}</td></tr>`,
  );
  const tbody = container.querySelector("tbody");
  if (before && tbody) {
    tbody.insertAdjacentHTML("beforeend", html.join(""));
  } else {
    container.innerHTML = table(["Signature", "Block", "Age", "Result", "Memo"], html, "No transactions found");
  }
  container.querySelector(".table-footer")?.remove();
  if (rows.length === 25) {
    const last = rows[rows.length - 1].signature;
    container.insertAdjacentHTML("beforeend", `<div class="table-footer"><button class="btn" type="button" data-action="more-txs" data-address="${esc(address)}" data-before="${esc(last)}">Load more</button></div>`);
  }
}

async function loadTokens(nav, address) {
  const container = document.getElementById("acct-tokens");
  container.innerHTML = skeletonRows(6);
  let holdings;
  try {
    holdings = await api(`/api/account/${encodeURIComponent(address)}/tokens`);
  } catch (error) {
    if (nav === state.navId) container.innerHTML = errorBox(error);
    return;
  }
  if (nav !== state.navId) return;
  container.innerHTML = table(
    ["Token", { label: "Amount", cls: "right" }, "Token account"],
    holdings.map((h) => {
      const name = h.name || labelFor(h.mint) || "Unknown token";
      return `<tr><td><a class="token-cell" href="${link("/account/" + h.mint)}">${avatar(h.mint, h.symbol || h.name)}
        <span class="names"><span>${esc(name)}</span><span class="faint">${esc(h.symbol || short(h.mint, 4, 4))}</span></span></a></td>
        <td class="right mono">${fmtDec(h.amount)}</td><td>${addr(h.token_account)}</td></tr>`;
    }),
    "This account holds no SPL tokens",
  );
}

async function loadHolders(nav, mint, token) {
  const container = document.getElementById("acct-holders");
  container.innerHTML = skeletonRows(6);
  let holders;
  try {
    holders = await api(`/api/token/${encodeURIComponent(mint)}/holders`);
  } catch (error) {
    if (nav === state.navId) container.innerHTML = errorBox(error);
    return;
  }
  if (nav !== state.navId) return;
  container.innerHTML = table(
    ["#", "Token account", { label: "Amount", cls: "right" }, "Share"],
    holders.map((h, i) => {
      const pct = h.share !== null ? (h.share * 100).toFixed(2) : null;
      return `<tr><td class="muted">${i + 1}</td><td>${addr(h.token_account, { full: true })}</td>
        <td class="right mono">${fmtDec(h.amount)} ${esc(token?.symbol || "")}</td>
        <td style="min-width:140px">${pct !== null ? `${pct}%<div class="progress"><span style="width:${Math.min(100, pct)}%"></span></div>` : "-"}</td></tr>`;
    }),
    "No holders found",
  );
}

/* ------------------------------------------------------------------ block */

async function renderBlock(nav, slot) {
  setTitle(`Block ${slot}`);
  app.innerHTML = `<div class="page-title"><h1>Block #${fmtInt(slot)}</h1></div>${card("", '<div class="spinner"></div>')}`;
  let block;
  try {
    block = await api(`/api/block/${slot}`);
  } catch (error) {
    if (nav === state.navId) app.innerHTML = `<div class="page-title"><h1>Block #${fmtInt(slot)}</h1></div>${card("", errorBox(error))}`;
    return;
  }
  if (nav !== state.navId) return;
  const nonVote = block.transaction_count - block.vote_transaction_count;
  app.innerHTML = `
    <div class="page-title"><h1>Block #${fmtInt(block.slot)}</h1>
      <a class="btn btn-sm" href="${link("/block/" + (block.parent_slot ?? block.slot - 1))}">&larr; Previous</a>
      <a class="btn btn-sm" href="${link("/block/" + (block.slot + 1))}">Next &rarr;</a></div>
    ${card("Overview", kv([
      ["Slot", `${fmtInt(block.slot)}${copyBtn(block.slot)}`],
      ["Timestamp", block.block_time ? `${ago(block.block_time)} <span class="muted">(${utc(block.block_time)})</span>` : "-"],
      ["Block height", fmtInt(block.block_height)],
      ["Blockhash", `<span class="mono">${esc(block.blockhash)}</span>${copyBtn(block.blockhash)}`],
      ["Parent slot", blockLink(block.parent_slot)],
      ["Previous blockhash", `<span class="mono">${esc(block.previous_blockhash || "-")}</span>`],
      ["Leader", addr(block.leader, { full: true })],
      ["Leader reward", `${fmtDec(block.leader_reward_sol)} SOL`],
      ["Transactions", `${fmtInt(block.transaction_count)} <span class="muted">(${fmtInt(nonVote)} non-vote, ${fmtInt(block.vote_transaction_count)} vote)</span>`],
    ]))}
    ${card("Transactions", '<div id="block-txs"></div>', '<label class="toggle"><input type="checkbox" id="show-votes"> Show vote transactions</label>')}`;

  let showVotes = false;
  let visible = 50;
  const draw = () => {
    const list = block.transactions.filter((t) => showVotes || !t.is_vote);
    const rows = list.slice(0, visible).map(
      (t) => `<tr><td>${txLink(t.signature)}</td><td>${addr(t.signer, { copy: false })}</td>
        <td>${lamportsToSol(t.fee_lamports)}</td><td>${t.is_vote ? '<span class="badge">Vote</span>' : ""}</td><td>${statusBadge(t.success)}</td></tr>`,
    );
    const more = list.length > visible ? `<div class="table-footer"><button class="btn" type="button" id="more-block-txs">Show more (${fmtInt(list.length - visible)} left)</button></div>` : "";
    document.getElementById("block-txs").innerHTML = table(["Signature", "Signer", "Fee (SOL)", "Type", "Result"], rows, "No transactions") + more;
    document.getElementById("more-block-txs")?.addEventListener("click", () => {
      visible += 100;
      draw();
    });
  };
  document.getElementById("show-votes").addEventListener("change", (e) => {
    showVotes = e.target.checked;
    visible = 50;
    draw();
  });
  draw();
}

/* ----------------------------------------------------------------- router */

function activateTab(tab) {
  const [group] = tab.dataset.tab.split(":");
  document.querySelectorAll(`[data-tab^="${group}:"]`).forEach((b) => b.classList.toggle("active", b === tab));
  document.querySelectorAll(`[data-panel^="${group}:"]`).forEach((p) => (p.hidden = p.dataset.panel !== tab.dataset.tab));
  const lazy = state.lazyTabs[tab.dataset.tab];
  if (lazy) {
    delete state.lazyTabs[tab.dataset.tab];
    lazy();
  }
}

/** Open the tab named in the URL (e.g. #/tx/...?tab=logs), so tabs can be linked to. */
function activateTabFromRoute() {
  const key = state.route.params.get("tab");
  if (!key) return;
  const tab = [...document.querySelectorAll("[data-tab]")].find((b) => b.dataset.tab.split(":")[1] === key);
  if (tab) activateTab(tab);
}

function renderNotFound() {
  setTitle("Not found");
  app.innerHTML = card("", errorBox({ status: 404, message: "This page does not exist." }));
}

const routes = [
  [/^\/$/, renderHome],
  [/^\/tx\/([^/]+)$/, renderTx],
  [/^\/(?:account|address|token)\/([^/]+)$/, renderAccount],
  [/^\/block\/(\d+)$/, renderBlock],
];

async function router() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  state.lazyTabs = {};
  const nav = ++state.navId;
  state.route = parseHash();
  const requested = state.route.params.get("cluster");
  state.cluster = state.config.clusters.includes(requested) ? requested : state.config.default_cluster;
  document.getElementById("cluster-select").value = state.cluster;
  updateRpcLabel();
  document.body.classList.remove("home");
  window.scrollTo(0, 0);
  for (const [pattern, render] of routes) {
    const match = state.route.path.match(pattern);
    if (match) {
      await render(nav, ...match.slice(1).map(decodeURIComponent));
      if (nav === state.navId) activateTabFromRoute();
      return;
    }
  }
  renderNotFound();
}

function navigate(hash) {
  if (location.hash === hash) router();
  else location.hash = hash;
}

async function search(term) {
  term = term.trim();
  if (!term) return;
  try {
    const result = await api("/api/search", { q: term });
    navigate(link(`/${result.type}/${result.value}`));
  } catch (error) {
    toast(error.message);
  }
}

/* ------------------------------------------------------------ custom RPC */

function updateRpcLabel() {
  const endpoint = backend.endpointFor(state.cluster);
  let host = "not set";
  try {
    host = endpoint ? new URL(endpoint).host : host;
  } catch {
    /* keep "not set" */
  }
  document.getElementById("rpc-host").textContent = host;
}

/** Ask for an RPC URL; returns true when one was saved. */
function promptCustomRpc() {
  const url = window.prompt(
    "Paste your Solana RPC URL (https://...).\n" +
      "It is stored only in this browser. Free endpoints with full data are available from Helius, QuickNode, Alchemy and others.\n" +
      "Leave empty to remove it.",
    backend.customRpc() || "",
  );
  if (url === null) return false;
  try {
    backend.setCustomRpc(url);
  } catch (error) {
    toast(error.message);
    return false;
  }
  if (!url.trim()) {
    toast("Custom RPC removed");
    navigate(hashFor("/", {}, state.config.default_cluster));
    return false;
  }
  toast("Custom RPC saved");
  return true;
}

function useCustomRpc() {
  if (promptCustomRpc()) navigate(hashFor(state.route.path === "/" ? "/" : state.route.path, {}, "custom"));
}

/* ----------------------------------------------------------------- events */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("theme", theme);
  } catch {
    /* storage unavailable */
  }
}

document.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    event.preventDefault();
    navigator.clipboard?.writeText(copy.dataset.copy).then(() => toast("Copied to clipboard"));
    return;
  }
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    activateTab(tab);
    // replaceState does not fire "hashchange", so the page is not re-rendered.
    history.replaceState(null, "", hashFor(state.route.path, { tab: tab.dataset.tab.split(":")[1] }));
    state.route = parseHash();
    return;
  }
  const action = event.target.closest("[data-action]");
  if (!action) return;
  if (action.dataset.action === "toggle-ix") {
    action.parentElement.classList.toggle("open");
  } else if (action.dataset.action === "more-txs") {
    action.disabled = true;
    action.textContent = "Loading...";
    loadAccountTransactions(state.navId, action.dataset.address, action.dataset.before);
  } else if (action.dataset.action === "custom-rpc") {
    event.preventDefault();
    useCustomRpc();
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[role=search]");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector("input[name=q]");
  search(input.value);
  input.blur();
});

window.addEventListener("hashchange", router);

function init() {
  let saved = null;
  try {
    saved = localStorage.getItem("theme");
  } catch {
    /* storage unavailable */
  }
  applyTheme(saved || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  document.getElementById("theme-toggle").addEventListener("click", () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"),
  );

  document.getElementById("app-version").textContent = "v" + state.config.version;
  const select = document.getElementById("cluster-select");
  select.innerHTML = state.config.clusters
    .map((c) => `<option value="${esc(c)}">${esc(c === "custom" ? "Custom RPC..." : c)}</option>`)
    .join("");
  select.addEventListener("change", () => {
    const chosen = select.value;
    if (chosen === "custom" && !backend.customRpc() && !promptCustomRpc()) {
      select.value = state.cluster;
      return;
    }
    navigate(hashFor("/", {}, chosen));
  });
  router();
}

init();
