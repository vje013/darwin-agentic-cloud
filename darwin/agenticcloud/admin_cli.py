"""Operator administration commands.

Ceremony commands for bootstrapping and managing the hosted Darwin
signer service. These commands generate signing keys, archive rotated
keys, and verify keylist publication — the work the operator does
when standing up or rotating the hosted tier.

CLI surface:

    darwin admin class-keys generate <substrate_id>
        Generate a new Ed25519 class signing key.
        Prints the resulting signer_key_id and the path the PEM was
        written to. Refuses to overwrite an existing active key.

    darwin admin class-keys rotate <substrate_id>
        Archive the active key and generate a new one.
        Prints both the old (archived) and new signer_key_id values.

    darwin admin class-keys verify <substrate_id> <keylist_url>
        Fetch the live keylist and confirm the active key for
        <substrate_id> matches what we have on disk. Use after Fly
        deploy to confirm the bootstrap succeeded.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from darwin.agenticcloud.class_keys import (
    ALLOWED_SUBSTRATES,
    ClassKeyError,
    ClassKeyStore,
    generate_class_key,
    rotate_class_key,
)

admin_app = typer.Typer(
    help="Operator administration commands.",
    no_args_is_help=True,
)

class_keys_app = typer.Typer(
    help="Manage substrate-class signing keys (hosted signer).",
    no_args_is_help=True,
)

admin_app.add_typer(class_keys_app, name="class-keys")


_console = Console()
_err = Console(stderr=True)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


@class_keys_app.command("generate")
def class_keys_generate(
    substrate_id: Annotated[
        str,
        typer.Argument(help=f"Substrate id. Allowlisted: {sorted(ALLOWED_SUBSTRATES)}"),
    ],
    out_dir: Annotated[
        Path,
        typer.Option(
            "--out-dir",
            help="Directory to write the PEM file. Defaults to ./class-keys.",
        ),
    ] = Path("./class-keys"),
) -> None:
    """Generate an Ed25519 class signing key for a substrate.

    The PEM is written to {out-dir}/{substrate_id}.pem with mode 0600.
    Refuses to overwrite an existing active key — use `rotate` if
    you mean to retire an old key.

    Next steps (Fly ceremony):
        fly secrets set DARWIN_CLASS_KEY_<SUBSTRATE>=\"$(cat {pem_path})\" -a darwin-agentic-cloud
        fly deploy -a darwin-agentic-cloud
        darwin admin class-keys verify {substrate_id} \\
            https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json
    """
    if substrate_id not in ALLOWED_SUBSTRATES:
        _err.print(
            f"[red]Refusing to generate key for non-allowlisted substrate[/red] "
            f"{substrate_id!r}\n"
            f"Allowed: {sorted(ALLOWED_SUBSTRATES)}"
        )
        raise typer.Exit(code=2)

    try:
        pem_path, signer_key_id = generate_class_key(out_dir, substrate_id)
    except ClassKeyError as e:
        _err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from e

    env_var_name = "DARWIN_CLASS_KEY_" + substrate_id.upper().replace("-", "_")

    body = Text()
    body.append("substrate_id   ", style="dim")
    body.append(substrate_id, style="bold cyan")
    body.append("\n")
    body.append("signer_key_id  ", style="dim")
    body.append(signer_key_id, style="bold green")
    body.append("\n")
    body.append("pem_path       ", style="dim")
    body.append(str(pem_path), style="white")
    body.append("\n")
    body.append("env_var        ", style="dim")
    body.append(env_var_name, style="yellow")
    body.append("\n\n")
    body.append("Next steps (Fly):\n", style="dim")
    body.append("  fly secrets set ", style="white")
    body.append(env_var_name, style="yellow")
    body.append(f'="$(cat {pem_path})" \\\n', style="white")
    body.append("    -a darwin-agentic-cloud\n", style="white")
    body.append("  fly deploy -a darwin-agentic-cloud\n", style="white")
    body.append("\n")
    body.append("Then verify:\n", style="dim")
    body.append(f"  darwin admin class-keys verify {substrate_id} \\\n", style="white")
    body.append(
        "    https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json\n",
        style="white",
    )

    _console.print(
        Panel(
            body,
            title="[bold]class key generated[/bold]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


@class_keys_app.command("rotate")
def class_keys_rotate(
    substrate_id: Annotated[
        str,
        typer.Argument(help="Substrate id whose active key should be rotated."),
    ],
    keys_dir: Annotated[
        Path,
        typer.Option("--keys-dir", help="Keys directory."),
    ] = Path("./class-keys"),
) -> None:
    """Archive the active key and generate a new one.

    The archived key remains in the published keylist with status='rotated'
    so attestations signed by it before rotation still verify.
    """
    if substrate_id not in ALLOWED_SUBSTRATES:
        _err.print(f"[red]substrate not allowlisted:[/red] {substrate_id!r}")
        raise typer.Exit(code=2)

    # Read current active key id before rotation, for the report.
    store_before = ClassKeyStore(keys_dir=keys_dir)
    try:
        old_kid = store_before.get_active_signer_key_id(substrate_id)
    except ClassKeyError as e:
        _err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from e

    try:
        new_path, new_kid = rotate_class_key(keys_dir, substrate_id)
    except ClassKeyError as e:
        _err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from e

    _console.print(
        f"[bold green]rotated[/bold green] {substrate_id}\n"
        f"  old: [yellow]{old_kid}[/yellow] (archived)\n"
        f"  new: [green]{new_kid}[/green] (active)\n"
        f"  pem: {new_path}"
    )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@class_keys_app.command("verify")
def class_keys_verify(
    substrate_id: Annotated[
        str,
        typer.Argument(help="Substrate id whose active key to verify."),
    ],
    keylist_url: Annotated[
        str,
        typer.Argument(
            help="Public keylist URL, e.g. "
            "https://darwin-agentic-cloud.fly.dev/.well-known/substrate-keys.json"
        ),
    ],
    keys_dir: Annotated[
        Path,
        typer.Option("--keys-dir", help="Local keys directory."),
    ] = Path("./class-keys"),
) -> None:
    """Verify the live keylist contains the expected active key.

    Run this AFTER `fly deploy` to confirm the secret materialized into
    the running container and the server is publishing the expected key.
    """
    # Fetch keylist
    try:
        with urllib.request.urlopen(keylist_url, timeout=10.0) as resp:
            keylist_bytes = resp.read()
    except Exception as e:
        _err.print(f"[red]could not fetch keylist:[/red] {e}")
        raise typer.Exit(code=1) from e

    try:
        keylist = json.loads(keylist_bytes.decode("utf-8"))
    except Exception as e:
        _err.print(f"[red]keylist is not valid JSON:[/red] {e}")
        raise typer.Exit(code=1) from e

    # Find active key for substrate_id
    active = [
        k
        for k in keylist.get("keys", [])
        if k.get("substrate_id") == substrate_id and k.get("status") == "active"
    ]
    if not active:
        seen = [k.get("substrate_id") for k in keylist.get("keys", [])]
        _err.print(
            f"[red]no active key in live keylist[/red] for {substrate_id!r}\n  keylist has: {seen}"
        )
        raise typer.Exit(code=1)
    live_kid = active[0].get("signer_key_id")

    # Compare to local
    store = ClassKeyStore(keys_dir=keys_dir)
    try:
        local_kid = store.get_active_signer_key_id(substrate_id)
    except ClassKeyError as e:
        _err.print(
            f"[red]no local key to compare against:[/red] {e}\n  live keylist has: {live_kid}"
        )
        raise typer.Exit(code=1) from e

    if live_kid == local_kid:
        _console.print(
            f"[bold green]match[/bold green] live keylist matches local key\n"
            f"  substrate_id : {substrate_id}\n"
            f"  signer_key_id: [green]{live_kid}[/green]\n"
            f"  keylist URL  : {keylist_url}"
        )
    else:
        _err.print(
            f"[red]mismatch[/red] live keylist does NOT match local key\n"
            f"  local: [yellow]{local_kid}[/yellow]\n"
            f"  live : [red]{live_kid}[/red]"
        )
        raise typer.Exit(code=2)


__all__ = ["admin_app", "class_keys_app"]
