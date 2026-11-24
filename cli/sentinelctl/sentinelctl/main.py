"""Main entrypoint for `sentinelctl` CLI."""

from __future__ import annotations

import typer

from sentinelctl.audit_cmd import audit_app
from sentinelctl.eval_cmd import eval_app
from sentinelctl.fault_cmd import fault_app
from sentinelctl.release_cmd import release_app

app = typer.Typer(
    name="sentinelctl",
    help="Sentinel operator CLI: fault injection, bad releases, end-to-end evals, and security audits.",
)

app.add_typer(fault_app, name="fault")
app.add_typer(release_app, name="release")
app.add_typer(eval_app, name="eval")
app.add_typer(audit_app, name="audit")

if __name__ == "__main__":
    app()
