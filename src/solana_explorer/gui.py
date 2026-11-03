"""Tkinter desktop interface.

Network calls run on a worker thread; results are handed back to the Tk main
loop through a queue so the window never freezes.
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING

from .constants import CLUSTERS, DEFAULT_CLUSTER
from .errors import SolanaExplorerError
from .explorer import SolanaExplorer
from .formatting import format_account, format_signatures, format_transaction
from .rpc import cluster_name, resolve_endpoint
from .validation import is_valid_address, is_valid_signature, split_identifiers

if TYPE_CHECKING:
    import tkinter as tk

HISTORY_LIMIT = 10
POLL_INTERVAL_MS = 100
SEPARATOR = "\n" + "-" * 80 + "\n\n"


def describe(explorer: SolanaExplorer, query: str) -> str:
    """Resolve one user query (signature or address) to a printable report."""
    try:
        if is_valid_signature(query):
            return format_transaction(explorer.get_transaction(query), explorer.endpoint)
        if is_valid_address(query):
            account = explorer.get_account(query)
            history = explorer.get_signatures(query, limit=HISTORY_LIMIT)
            return (
                format_account(account, explorer.endpoint)
                + "\n\n"
                + format_signatures(query, history)
            )
        return f"error: {query!r} is neither a transaction signature nor an address"
    except SolanaExplorerError as exc:
        return f"error: {exc}"


class ExplorerApp:
    def __init__(self, root: tk.Tk, endpoint: str | None = None) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.root = root
        self._explorers: dict[str, SolanaExplorer] = {}
        self._results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._busy = False

        root.title("Solana Explorer")
        root.geometry("980x680")
        root.minsize(640, 420)

        frame = ttk.Frame(root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(frame, text="Cluster / RPC URL:").grid(row=0, column=0, sticky="w")
        resolved = resolve_endpoint(endpoint)
        self.cluster = tk.StringVar(value=cluster_name(resolved) or resolved)
        ttk.Combobox(frame, textvariable=self.cluster, values=list(CLUSTERS)).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

        ttk.Label(
            frame,
            text="Transaction signatures or account addresses (one per line or comma-separated):",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.query = tk.Text(frame, height=4, wrap="word", undo=True)
        self.query.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.query.bind("<Control-Return>", self._on_shortcut)
        self.query.focus_set()

        self.output = scrolledtext.ScrolledText(frame, wrap="none", font=("Courier New", 10))
        self.output.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        self.output.configure(state="disabled")

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.explore_button = ttk.Button(
            buttons, text="Explore  (Ctrl+Enter)", command=self.explore
        )
        self.explore_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Clear", command=self.clear).pack(side=tk.LEFT, padx=(8, 0))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(buttons, textvariable=self.status).pack(side=tk.RIGHT)

    def _on_shortcut(self, _event: object) -> str:
        self.explore()
        return "break"

    def _explorer(self) -> SolanaExplorer:
        endpoint = resolve_endpoint(self.cluster.get() or DEFAULT_CLUSTER)
        if endpoint not in self._explorers:
            self._explorers[endpoint] = SolanaExplorer(endpoint=endpoint)
        return self._explorers[endpoint]

    def explore(self) -> None:
        from tkinter import messagebox

        if self._busy:
            return
        queries = split_identifiers(self.query.get("1.0", "end"))
        if not queries:
            messagebox.showinfo("Solana Explorer", "Enter a transaction signature or an address.")
            return
        try:
            explorer = self._explorer()
        except SolanaExplorerError as exc:
            messagebox.showerror("Solana Explorer", str(exc))
            return

        self._set_busy(True, f"Fetching {len(queries)} item(s) from {explorer.endpoint}...")
        self._write("", replace=True)
        threading.Thread(target=self._worker, args=(explorer, queries), daemon=True).start()
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _worker(self, explorer: SolanaExplorer, queries: list[str]) -> None:
        for index, query in enumerate(queries):
            prefix = SEPARATOR if index else ""
            self._results.put(("result", prefix + describe(explorer, query) + "\n"))
        self._results.put(("done", f"Done - {len(queries)} item(s)"))

    def _poll(self) -> None:
        while True:
            try:
                kind, payload = self._results.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self._set_busy(False, payload)
                return
            self._write(payload)
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _write(self, text: str, *, replace: bool = False) -> None:
        self.output.configure(state="normal")
        if replace:
            self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.explore_button.configure(state="disabled" if busy else "normal")
        self.status.set(message)

    def clear(self) -> None:
        self.query.delete("1.0", "end")
        self._write("", replace=True)
        self.status.set("Ready")


def run(endpoint: str | None = None) -> None:
    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - depends on the Python build
        raise SystemExit(
            "tkinter is not available in this Python installation. "
            "Install it (e.g. `sudo apt install python3-tk`) or use the CLI."
        ) from exc
    root = tk.Tk()
    ExplorerApp(root, endpoint)
    root.mainloop()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
