"""Visual primitives for the darwin.agenticcloud CLI.

Brand colors (referenced semantically, not decoratively):
- bright green #00ff01 ▸ verified, success, ready
- amber #fdb515       ▸ alpha, warning, pending
- dim gray            ▸ secondary metadata
- black background    ▸ surface

Rich does not let us pick exact hex on every terminal, but it gracefully
degrades. We use bright green for the brand accents and yellow for amber.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# -------------------------------------------------------------------
# Brand
# -------------------------------------------------------------------

BRAND_GREEN = "#00ff01"
BRAND_AMBER = "#fdb515"
BRAND_DIM = "#6a6a6a"

# Block-letter banner — produced by `figlet -f ANSI Shadow DARWIN`,
# stored as a literal so we have zero runtime dependency on figlet.
BANNER = r"""██████╗  █████╗ ██████╗ ██╗    ██╗██╗███╗   ██╗
██╔══██╗██╔══██╗██╔══██╗██║    ██║██║████╗  ██║
██║  ██║███████║██████╔╝██║ █╗ ██║██║██╔██╗ ██║
██║  ██║██╔══██║██╔══██╗██║███╗██║██║██║╚██╗██║
██████╔╝██║  ██║██║  ██║╚███╔███╔╝██║██║ ╚████║
╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝ ╚══╝╚══╝ ╚═╝╚═╝  ╚═══╝"""


# -------------------------------------------------------------------
# Banner
# -------------------------------------------------------------------


def render_banner(
    version: str,
    schema: str,
    signer_key_id: str | None = None,
    substrate_id: str = "local-docker-v0",
) -> Group:
    """Branded ASCII banner with version / schema / signer footer."""
    parts: list = [Text(BANNER, style=f"bold {BRAND_GREEN}")]
    parts.append(Text(""))

    tagline = Text()
    tagline.append("  verifiable compute for AI agents", style=BRAND_DIM)
    tagline.append("               ")
    tagline.append(f"v{version}", style=f"bold {BRAND_AMBER}")
    parts.append(tagline)

    schema_line = Text()
    schema_line.append("  schema  ", style=BRAND_DIM)
    schema_line.append(schema, style=BRAND_AMBER)
    parts.append(schema_line)

    if signer_key_id:
        signer_line = Text()
        signer_line.append("  signer  ", style=BRAND_DIM)
        signer_line.append(signer_key_id, style=BRAND_GREEN)
        signer_line.append("  ·  ", style=BRAND_DIM)
        signer_line.append("substrate  ", style=BRAND_DIM)
        signer_line.append(substrate_id, style=BRAND_GREEN)
        parts.append(signer_line)

    return Group(*parts)


def print_banner(
    console: Console,
    version: str,
    schema: str,
    signer_key_id: str | None = None,
    substrate_id: str = "local-docker-v0",
) -> None:
    """Convenience: print the banner with appropriate vertical spacing."""
    console.print()
    console.print(render_banner(version, schema, signer_key_id, substrate_id))
    console.print()


# -------------------------------------------------------------------
# Matrix boot sequence
# -------------------------------------------------------------------


@dataclass(frozen=True)
class BootStep:
    label: str
    delay: float = 0.08  # seconds before next step appears


def matrix_boot(
    console: Console,
    steps: list[BootStep],
    suppress_tty_check: bool = False,
) -> None:
    """Stream lines of the form `> {label} ......... ok` one at a time.

    If not running in a TTY (CI, logs, redirected stdout), we skip the
    animation and just print each line instantly. The Karpathy/Matrix
    aesthetic should not break grep'ing logs.
    """
    is_tty = suppress_tty_check or (console.is_terminal and not console.is_jupyter)
    width = max(len(s.label) for s in steps)

    for step in steps:
        # `> step .......... ok`
        dots = "." * max(3, 56 - width - len(step.label))
        line = Text()
        line.append("> ", style=BRAND_DIM)
        line.append(step.label, style="white")
        line.append(f" {dots} ", style=BRAND_DIM)
        line.append("ok", style=f"bold {BRAND_GREEN}")
        console.print(line)
        if is_tty:
            time.sleep(step.delay)


# -------------------------------------------------------------------
# Single-line progressive ticker
# -------------------------------------------------------------------


class StepLine:
    """One line that updates in-place, then renders final state.

    Usage:
        with StepLine(console, "darwin.agenticcloud · running workload") as step:
            step.tick("budget")
            ...do work...
            step.tick("sandbox")
            ...
        # When the context exits, last state stays printed.
    """

    def __init__(self, console: Console, header: str) -> None:
        self.console = console
        self.header = header
        self.ticks: list[str] = []
        self._live: Live | None = None
        self._is_tty = console.is_terminal and not console.is_jupyter

    def __enter__(self) -> StepLine:
        if self._is_tty:
            self._live = Live(
                self._render(),
                console=self.console,
                refresh_per_second=12,
                transient=False,
            )
            self._live.__enter__()
        else:
            self.console.print(self._header_text())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.update(self._render())
            self._live.__exit__(exc_type, exc, tb)

    def tick(self, label: str) -> None:
        """Add a tick and re-render."""
        self.ticks.append(label)
        if self._live is not None:
            self._live.update(self._render())
        else:
            # non-TTY: each tick on its own line
            self.console.print(self._tick_text(label, finished=True))

    def _header_text(self) -> Text:
        t = Text()
        t.append("▶ ", style=f"bold {BRAND_GREEN}")
        t.append(self.header)
        return t

    def _tick_text(self, label: str, finished: bool) -> Text:
        t = Text()
        t.append("  ╴ ", style=BRAND_DIM)
        t.append(label)
        t.append(" ", style=BRAND_DIM)
        t.append("✓", style=f"bold {BRAND_GREEN}")
        return t

    def _render(self) -> Group:
        if not self.ticks:
            return Group(self._header_text())
        body = Text()
        for i, label in enumerate(self.ticks):
            if i > 0:
                body.append("  ", style=BRAND_DIM)
            body.append("╴ ", style=BRAND_DIM)
            body.append(label)
            body.append(" ", style=BRAND_DIM)
            body.append("✓", style=f"bold {BRAND_GREEN}")
        return Group(self._header_text(), Text("  ").append_text(body))


# -------------------------------------------------------------------
# Attestation panel
# -------------------------------------------------------------------


def render_attestation_panel(
    workload_hash: str,
    output_hash: str,
    substrate: str,
    signer_key_id: str,
    cost_usd: float,
    cost_cap_usd: float,
    verified: bool = True,
    schema_short: str = "v0.1",
    *,
    attestation_id: str | None = None,
    issued_at: float | None = None,
    public_key_b64: str | None = None,
    schema_full: str = "darwin.cloud/agenticcloud/attestation/v0.1",
    version: str = "0.1.0",
) -> Panel:
    """Branded panel showing the signed attestation contents.

    The optional keyword-only params (attestation_id, issued_at, public_key_b64,
    schema_full, version) make the panel a credible "receipt" — they're the
    fields a verifier would actually inspect.
    """
    body = Text()

    # Identity block
    if attestation_id:
        body.append("id           ", style=BRAND_DIM)
        body.append(_short_id(attestation_id), style="white")
        body.append("\n")
    if issued_at is not None:
        body.append("issued       ", style=BRAND_DIM)
        body.append(_fmt_timestamp(issued_at), style="white")
        body.append("\n")
    if attestation_id or issued_at is not None:
        body.append("\n")

    # Execution block
    body.append("workload     ", style=BRAND_DIM)
    body.append(_short_hash(workload_hash), style="white")
    body.append("\n")
    body.append("output       ", style=BRAND_DIM)
    body.append(_short_hash(output_hash), style="white")
    body.append("\n")
    body.append("substrate    ", style=BRAND_DIM)
    body.append(substrate, style=BRAND_AMBER)
    body.append("\n")
    body.append("\n")

    # Cryptography block
    body.append("signer       ", style=BRAND_DIM)
    body.append(signer_key_id, style=BRAND_GREEN)
    body.append("\n")
    if public_key_b64:
        body.append("fingerprint  ", style=BRAND_DIM)
        body.append(_fmt_fingerprint(public_key_b64), style="white")
        body.append("\n")
    body.append("cost         ", style=BRAND_DIM)
    body.append(f"${_fmt_cost(cost_usd)}", style="white")
    body.append(" / ", style=BRAND_DIM)
    body.append(f"${_fmt_cost(cost_cap_usd)}", style=BRAND_DIM)
    body.append("\n")
    body.append("\n")

    # Verification line
    if verified:
        body.append("✓ ", style=f"bold {BRAND_GREEN}")
        body.append("signature verified", style="white")
    else:
        body.append("✗ ", style=f"bold {BRAND_AMBER}")
        body.append("signature unverified", style=BRAND_AMBER)
    body.append("\n")
    body.append("schema       ", style=BRAND_DIM)
    body.append(schema_full, style=BRAND_AMBER)

    # Footer subtitle inside the panel border
    subtitle = (
        f"[dim]darwin.cloud — verifiable compute for AI agents[/dim]"
        f"   [{BRAND_AMBER}]v{version}[/{BRAND_AMBER}]"
    )

    return Panel(
        body,
        title="[bold]attestation · darwin.agenticcloud[/bold]",
        subtitle=subtitle,
        border_style=BRAND_GREEN,
        padding=(1, 2),
    )


def _short_hash(h: str) -> str:
    """sha256:7b3f4a2e8c1d5f9a -> sha256:7b3f...c91a"""
    if ":" in h:
        prefix, body = h.split(":", 1)
    else:
        prefix, body = "sha256", h
    if len(body) <= 16:
        return f"{prefix}:{body}"
    return f"{prefix}:{body[:8]}...{body[-4:]}"


def _fmt_cost(usd: float) -> str:
    """Format USD cost with enough precision to actually see sub-cent values.

    < $0.01     → 6 digits (e.g. $0.000018)
    < $1        → 4 digits (e.g. $0.0240)
    >= $1       → 2 digits (e.g. $12.50)
    """
    if usd < 0.01:
        return f"{usd:.6f}"
    if usd < 1:
        return f"{usd:.4f}"
    return f"{usd:.2f}"


def _short_id(att_id: str) -> str:
    """Make attestation IDs readable.

    UUID-shaped IDs are truncated to 12 hex chars.
    Demo/manual IDs (att_demo_...) are passed through as-is so the
    substrate name stays legible.
    """
    raw = att_id.removeprefix("att_")
    # UUID = 32 hex chars (with or without hyphens). If it looks like one,
    # shorten. Otherwise leave it alone.
    hex_only = raw.replace("-", "")
    if len(hex_only) >= 32 and all(c in "0123456789abcdef" for c in hex_only.lower()):
        return f"att_{hex_only[:12]}"
    return att_id


def _fmt_timestamp(unix_ts: float) -> str:
    """Unix timestamp → ISO 8601 UTC string."""
    return datetime.fromtimestamp(unix_ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_fingerprint(public_key_b64: str) -> str:
    """Shorten a base64-encoded public key into a canonical-looking fingerprint.

    ed25519:1de8debf02823...d0fc — first 13 + last 4 hex chars of sha256.
    """
    digest = hashlib.sha256(public_key_b64.encode("utf-8")).hexdigest()
    return f"ed25519:{digest[:13]}...{digest[-4:]}"


def signature_animation(console: Console, frames: float = 0.4) -> None:
    """Frame-by-frame signing animation, shown only on TTY.

    Renders a short '...verifying ed25519........' that gets a check mark.
    Total wall time is `frames` seconds. Pure cosmetic; signing happens in
    the actual signer module synchronously.
    """
    if not (console.is_terminal and not console.is_jupyter):
        return
    spinner_chars = ["╱", "─", "╲", "│"]  # noqa: RUF001
    n = int(frames / 0.06)
    for i in range(n):
        char = spinner_chars[i % len(spinner_chars)]
        dots = "." * min(7, i + 1)
        line = Text()
        line.append("  ▸ signing attestation  ", style="white")
        line.append(char, style=f"bold {BRAND_GREEN}")
        line.append(f" verifying ed25519 {dots}", style=BRAND_DIM)
        console.print(line, end="\r")
        time.sleep(0.06)
    # final state
    final = Text()
    final.append("  ▸ signing attestation  ", style="white")
    final.append("✓", style=f"bold {BRAND_GREEN}")
    final.append(" verifying ed25519 .......", style=BRAND_DIM)
    final.append(" ", style=BRAND_DIM)
    final.append("✓", style=f"bold {BRAND_GREEN}")
    console.print(final)


# -------------------------------------------------------------------
# Configuration receipt (used by `darwin mcp install`)
# -------------------------------------------------------------------


def render_mcp_install_receipt(
    config_path: str,
    server_name: str,
    python_interpreter: str,
    tool_names: list[str],
    sample_prompt: str = ('"use dac_run_python to compute 2+2 and show me the signer key id"'),
) -> Group:
    """The configuration receipt shown after a successful `darwin mcp install`."""
    parts: list = []

    body = Text()
    body.append("  ▸ Claude Desktop config installed\n", style="white")
    body.append("    ", style=BRAND_DIM)
    body.append(config_path, style=BRAND_AMBER)
    body.append("\n")
    body.append("  ▸ Server name        ", style="white")
    body.append(server_name, style=BRAND_GREEN)
    body.append("\n")
    body.append("  ▸ Python interpreter ", style="white")
    body.append(python_interpreter, style=BRAND_DIM)
    body.append("\n")
    body.append("  ▸ MCP tools exposed  ", style="white")
    body.append(str(len(tool_names)), style=BRAND_GREEN)
    body.append(" (", style=BRAND_DIM)
    body.append(", ".join(tool_names), style=BRAND_AMBER)
    body.append(")", style=BRAND_DIM)
    parts.append(body)

    parts.append(Text(""))
    parts.append(Text("  next ──────────────────────────────────────────────────", style=BRAND_DIM))

    nxt = Text()
    nxt.append("    1. ", style=BRAND_DIM)
    nxt.append("quit and restart Claude Desktop ", style="white")
    nxt.append("(cmd-q on macOS)", style=BRAND_DIM)
    nxt.append("\n")
    nxt.append("    2. ", style=BRAND_DIM)
    nxt.append("in any chat, ask:\n", style="white")
    nxt.append("       ")
    nxt.append(sample_prompt, style=f"italic {BRAND_AMBER}")
    parts.append(nxt)

    parts.append(Text(""))
    final = Text()
    final.append("  ✓ ", style=f"bold {BRAND_GREEN}")
    final.append("darwin substrate connected to claude.", style="white")
    parts.append(final)

    return Group(*parts)


# -------------------------------------------------------------------
# v0.2 attestation panel — polymorphic substrate block
# -------------------------------------------------------------------


def render_v02_attestation_panel(attestation: dict) -> Panel:
    """Branded panel for v0.2 attestations.

    Adds a fourth `substrate` block to the v0.1 layout, rendering the
    polymorphic substrate object: id, version, identity-signer key id
    and type, evidence schema id, TEE marker, and evidence fields.

    Visual style preserved from v0.1 — three columns, hash truncation,
    brand colors, the verification line at the bottom.
    """
    exec_result = attestation.get("execution_result", {})
    substrate = exec_result.get("substrate", {})
    evidence = substrate.get("evidence", {})

    body = Text()

    # --- Identity block ---
    body.append("id           ", style=BRAND_DIM)
    body.append(_short_id(attestation.get("attestation_id", "")), style="white")
    body.append("\n")
    body.append("issued       ", style=BRAND_DIM)
    body.append(attestation.get("issued_at", ""), style="white")
    body.append("\n")
    body.append("\n")

    # --- Execution block ---
    body.append("workload     ", style=BRAND_DIM)
    body.append(_short_hash(attestation.get("workload_spec_hash", "")), style="white")
    body.append("\n")
    body.append("output       ", style=BRAND_DIM)
    body.append(_short_hash(exec_result.get("output_hash", "")), style="white")
    body.append("\n")
    body.append("cost         ", style=BRAND_DIM)
    body.append(f"${_fmt_cost(exec_result.get('cost_usd', 0.0))}", style="white")
    body.append("\n")
    body.append("\n")

    # --- Substrate block (NEW for v0.2) ---
    body.append("substrate    ", style=BRAND_DIM)
    body.append(substrate.get("id", "?"), style=BRAND_AMBER)
    body.append("  ", style=BRAND_DIM)
    body.append(f"v{substrate.get('version', '?')}", style=BRAND_DIM)
    body.append("\n")
    body.append("schema       ", style=BRAND_DIM)
    body.append(substrate.get("evidence_schema_id", "?"), style=BRAND_AMBER)
    body.append("\n")

    # Substrate identity signer
    sig_type = substrate.get("identity_signer_type", "?")
    if sig_type == "darwin-class-key":
        sig_style = f"bold {BRAND_GREEN}"
    elif sig_type == "operator-fallback":
        sig_style = BRAND_AMBER
    else:
        sig_style = BRAND_DIM
    body.append("sub-signer   ", style=BRAND_DIM)
    body.append(substrate.get("identity_signer_key_id", "?"), style=BRAND_GREEN)
    body.append("  ", style=BRAND_DIM)
    body.append(f"[{sig_type}]", style=sig_style)
    body.append("\n")

    # TEE marker (only displayed when meaningful — true or extensions present)
    tee = substrate.get("tee_required", False)
    extensions = substrate.get("extensions", {}) or {}
    if tee:
        body.append("tee          ", style=BRAND_DIM)
        body.append("required", style=f"bold {BRAND_AMBER}")
        body.append("\n")
    if extensions:
        body.append("extensions   ", style=BRAND_DIM)
        body.append(", ".join(sorted(extensions.keys())), style=BRAND_DIM)
        body.append("\n")
    body.append("\n")

    # --- Evidence rows ---
    body.append("evidence", style=BRAND_DIM)
    body.append("\n")
    for key in sorted(evidence.keys()):
        val = evidence[key]
        body.append(f"  {key:<22}", style=BRAND_DIM)
        body.append(_fmt_evidence_value(key, val), style="white")
        body.append("\n")
    body.append("\n")

    # --- Cryptography / outer signature ---
    body.append("signer       ", style=BRAND_DIM)
    body.append(attestation.get("signer_key_id", "?"), style=BRAND_GREEN)
    body.append("\n")
    body.append("\n")

    # --- Verification line ---
    body.append("✓ ", style=f"bold {BRAND_GREEN}")
    body.append("attestation signed", style="white")
    body.append("\n")
    body.append("schema       ", style=BRAND_DIM)
    body.append(
        attestation.get("schema", "darwin.cloud/agenticcloud/attestation/v0.2"),
        style=BRAND_AMBER,
    )

    # Subtitle shows SCHEMA version (v0.2.0), not substrate adapter version.
    version_str = "0.2.0"
    subtitle = (
        f"[dim]darwin.cloud — verifiable compute for AI agents[/dim]"
        f"   [{BRAND_AMBER}]v{version_str}[/{BRAND_AMBER}]"
    )

    return Panel(
        body,
        title="[bold]attestation · darwin.agenticcloud[/bold]",
        subtitle=subtitle,
        border_style=BRAND_GREEN,
        padding=(1, 2),
    )


def _fmt_evidence_value(key: str, val) -> str:
    """Render an evidence value compactly.

    Hashes are truncated. Numbers are formatted. Strings are passed through
    with reasonable upper-bound length.
    """
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        # Hashes (hex of length 64, with or without sha256: prefix)
        if key.endswith("_hash") or (
            len(val) == 64 and all(c in "0123456789abcdef" for c in val.lower())
        ):
            return _short_hash(val)
        if len(val) > 48:
            return f"{val[:36]}…"
        return val
    if isinstance(val, int | float):
        if key.endswith("_sec"):
            return f"{val:.3f}s"
        if key.endswith("_ms"):
            return f"{val}ms"
        if key.endswith("_mb"):
            return f"{val} MB"
        return str(val)
    return str(val)


# -------------------------------------------------------------------
# Dispatcher: pick v0.1 vs v0.2 by schema
# -------------------------------------------------------------------


def render_attestation_panel_auto(attestation: dict) -> Panel:
    """Render whichever attestation panel matches the schema.

    Callers (CLI, MCP, server) use this so they don't have to track
    the schema version themselves. Returns the v0.2 panel for new
    attestations and a v0.1 panel reconstructed from the old shape
    for backward compatibility.
    """
    schema = attestation.get("schema", "")
    if "v0.2" in schema:
        return render_v02_attestation_panel(attestation)

    # Fall back to v0.1 panel construction. The old panel takes
    # positional args; we marshal them here.
    exec_result = attestation.get("execution_result", {})
    return render_attestation_panel(
        workload_hash=attestation.get("workload_spec_hash", ""),
        output_hash=exec_result.get("output_hash", ""),
        substrate=exec_result.get("substrate_id", "?"),
        signer_key_id=attestation.get("signer_key_id", "?"),
        cost_usd=exec_result.get("cost_usd", 0.0),
        cost_cap_usd=attestation.get("workload_spec", {}).get("cost_cap_usd", 0.0),
        verified=True,
        attestation_id=attestation.get("attestation_id"),
        issued_at=attestation.get("issued_at"),
        schema_full=schema,
    )
