/* Pure formatting helpers shared by the UI (and unit-tested in Node). */
(function (root) {
  "use strict";

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function short(value, head = 6, tail = 6) {
    const s = String(value ?? "");
    return s.length > head + tail + 3 ? `${s.slice(0, head)}...${s.slice(-tail)}` : s;
  }

  /** Thousands separators for a decimal string, without losing precision. */
  function fmtDec(value, maxFrac = null) {
    if (value === null || value === undefined || value === "") return "-";
    let s = String(value);
    const negative = s.startsWith("-");
    if (negative || s.startsWith("+")) s = s.slice(1);
    let [int, frac = ""] = s.split(".");
    if (maxFrac !== null && frac.length > maxFrac) frac = frac.slice(0, maxFrac).replace(/0+$/, "");
    int = int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (negative ? "-" : "") + int + (frac ? "." + frac : "");
  }

  function signed(value) {
    const s = String(value);
    return s.startsWith("-") ? fmtDec(s) : "+" + fmtDec(s);
  }

  function lamportsToSol(lamports) {
    const n = BigInt(Math.round(Number(lamports) || 0));
    const negative = n < 0n;
    const abs = negative ? -n : n;
    const int = abs / 1000000000n;
    const frac = (abs % 1000000000n).toString().padStart(9, "0").replace(/0+$/, "");
    return (negative ? "-" : "") + int.toString() + (frac ? "." + frac : "");
  }

  const fmtInt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString("en-US"));

  function compact(n) {
    if (n === null || n === undefined) return "-";
    return Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(Number(n));
  }

  function ago(iso, now = Date.now()) {
    if (!iso) return "-";
    const s = Math.max(0, (now - Date.parse(iso)) / 1000);
    if (s < 60) return `${Math.floor(s)}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  const utc = (iso) => (iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "-");

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "-";
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }

  const exported = { esc, short, fmtDec, signed, lamportsToSol, fmtInt, compact, ago, utc, duration };
  root.SolanaFormat = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof globalThis !== "undefined" ? globalThis : this);
