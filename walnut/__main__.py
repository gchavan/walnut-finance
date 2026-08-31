"""Entry point for the Walnut CLI: serve, sync, claim."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiohttp import web

from .config import Config, load_dotenv
from .db import connect, init_db


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not verbose:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def run_serve(cfg: Config) -> None:
    from . import categories as cats
    from . import db as dbmod
    from .server import build_app

    conn = connect(cfg.db_path)
    init_db(conn)
    cats.ensure_seeded(conn)
    dbmod.recompute_spending(conn)
    cats.apply_to_uncategorized(conn)
    log = logging.getLogger("walnut")
    runner = web.AppRunner(build_app(cfg, conn))
    await runner.setup()
    site = web.TCPSite(runner, cfg.web_host, cfg.web_port)
    await site.start()
    shown = "localhost" if cfg.web_host in ("0.0.0.0", "::") else cfg.web_host
    log.info("Walnut at http://%s:%d", shown, cfg.web_port)
    if cfg.live_source == "simplefin" and cfg.simplefin_ready:
        log.info("Live source: SimpleFIN")
    else:
        log.warning(
            "SIMPLEFIN_ACCESS_URL missing. UI will show setup. "
            "Create a setup token at https://bridge.simplefin.org/simplefin/create "
            "and paste it in Walnut."
        )
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        conn.close()


def run_sync(cfg: Config) -> int:
    from . import db
    from . import recurring as rec
    from . import simplefin

    from . import categories as cats

    conn = connect(cfg.db_path)
    init_db(conn)
    cats.ensure_seeded(conn)
    if not cfg.simplefin_ready:
        print(
            "SimpleFIN is not configured. Create a setup token at "
            "https://bridge.simplefin.org/simplefin/create and paste it in "
            "the Walnut UI (or run: walnut claim <setup-token>).",
            file=sys.stderr,
        )
        conn.close()
        return 2
    result = asyncio.run(simplefin.sync_all(cfg, conn))
    rec.refresh(conn)
    db.recompute_spending(conn)
    cats.apply_to_uncategorized(conn)
    cov = result["coverage"]
    src = result.get("source") or cfg.live_source
    print(
        f"sync {src} {'ok' if result['ok'] else 'finished with errors'}: "
        f"+{result['n_added']} ~{result['n_modified']} -{result['n_removed']}"
    )
    if cov.get("min_date"):
        print(
            f"held range: {cov['min_date']} → {cov['max_date']} "
            f"({cov['n']} transactions)"
        )
    else:
        print("no transactions stored yet")
    if cov.get("partial"):
        reason = cov.get("partial_reason") or "limited history window"
        print(f"coverage is PARTIAL — {reason}", file=sys.stderr)
    for err in result.get("errors") or []:
        print(f"  error: {err}", file=sys.stderr)
    for e in result.get("errlist") or []:
        msg = e.get("msg") if isinstance(e, dict) else e
        if msg:
            print(f"  simplefin: {msg}", file=sys.stderr)
    conn.close()
    return 0 if result["ok"] else 1


def run_claim(cfg: Config, token: str) -> int:
    from . import db
    from . import recurring as rec
    from . import simplefin

    blob = (token or "").strip()
    if not blob:
        print(
            "Usage: walnut claim <setup-token>\n"
            "Prefer the Walnut UI: open http://localhost:8766 and paste the token there. "
            "This command prints nothing secret.",
            file=sys.stderr,
        )
        return 2
    conn = connect(cfg.db_path)
    init_db(conn)
    try:
        access_url = asyncio.run(simplefin.claim_setup_token(blob))
        simplefin.store_access_url(cfg, access_url)
        result = asyncio.run(simplefin.sync_all(cfg, conn))
        rec.refresh(conn)
    except simplefin.SimplefinError as exc:
        print(str(exc), file=sys.stderr)
        conn.close()
        return 1
    print(
        "SimpleFIN connected. Access URL stored in .env. "
        f"sync {'ok' if result.get('ok') else 'finished with errors'}: "
        f"+{result.get('n_added', 0)} ~{result.get('n_modified', 0)} "
        f"-{result.get('n_removed', 0)}"
    )
    conn.close()
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="walnut", description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "sync", "claim"],
        help="serve: dashboard on 127.0.0.1:8766 (default). "
             "sync: pull from SimpleFIN when configured. "
             "claim: exchange a SimpleFIN setup token (prefer the UI).",
    )
    parser.add_argument(
        "setup_token",
        nargs="?",
        default=None,
        help="SimpleFIN setup token for `walnut claim`. Prefer the UI.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    setup_logging(args.verbose)
    cfg = Config()
    cfg.ensure_dirs()

    if args.command == "sync":
        return run_sync(cfg)
    if args.command == "claim":
        return run_claim(cfg, args.setup_token or "")

    try:
        asyncio.run(run_serve(cfg))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
