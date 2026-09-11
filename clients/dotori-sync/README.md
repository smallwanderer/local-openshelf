# Dotori Sync

Dotori Sync is a background folder-watching daemon for one-way sync into a
self-hosted Dotori server. It is the always-running counterpart to the
manual `dotori sync` command in `dotori-cli`: both are built on the exact
same sync engine (`dotori_cli.sync.run_sync`), so dotori-sync does not
reimplement manifest diffing, upload ordering, or the sync HTTP contract. It
only adds a filesystem watcher and a scheduling loop on top.

Use `dotori sync` for a one-off, inspectable dry run or apply. Use
`dotori-sync watch` to keep a folder continuously mirrored without running a
command by hand every time.

## Install

dotori-sync depends on dotori-cli's config and sync engine, so install both
into the same environment.

```bash
pipx install -e ./clients/dotori-cli
pipx inject dotori-cli -e ./clients/dotori-sync --include-apps
```

`--include-apps` exposes the `dotori-sync` console script from dotori-cli's
pipx virtual environment. For a plain virtualenv, `pip install -e
./clients/dotori-cli && pip install -e ./clients/dotori-sync` in the same
environment works the same way.

## Configure

dotori-sync reuses dotori-cli's remote/account/credential configuration; it
has no `connect` or `account` commands of its own. Register a **Folder sync
token** first, exactly as for `dotori sync`:

```bash
dotori remote add origin https://dotori.example.com
dotori account add personal --remote origin --credential sync --token-type sync
```

## Watch a folder

```bash
dotori-sync --remote origin --account personal watch ~/Documents
```

This runs an immediate sync pass, then watches `~/Documents` and re-syncs
automatically:

- shortly after a burst of local file changes goes quiet (`--debounce`,
  default 5s), and
- on a fixed fallback interval regardless of observed activity
  (`--interval`, default 300s), in case the OS misses filesystem events.

Like `dotori sync`, this is one-way (local folder to `/sync/<root-name>` on
the server) and never deletes server-only entries unless `--delete` is
passed. A transient network or server error logs a warning and is retried on
the next pass; it does not stop the watcher.

```text
dotori-sync watch PATH
  --root-name NAME   Unique server folder name under /sync (default: folder name)
  --debounce SECS    Quiet period before a burst of changes triggers a sync (default: 5)
  --interval SECS    Fallback sync interval even without activity (default: 300)
  --delete           Allow a sync pass to move server-only entries to trash
  --no-ai            Disable AI processing for new uploads
  --once             Run a single sync pass and exit, without watching
  --json             Print machine-readable JSON for --once
  -v, --verbose      Enable debug logging
```

Run with `--once` to do a single apply pass and exit, e.g. from `cron` or a
scheduled task, instead of running the daemon continuously.

Stop the daemon with Ctrl+C; it shuts the filesystem observer down cleanly
before exiting.

## Windows Explorer right-click menu

On Windows, dotori-sync can add a **Sync with Dotori** entry to the folder
right-click menu, the same way the original shelf-sync prototype did:

```bash
dotori-sync install-menu
dotori-sync uninstall-menu
```

This writes a per-user registry entry under `HKEY_CURRENT_USER`, so it needs
no administrator rights and no separate installer. Right-clicking a folder
(or the empty space inside one) and choosing **Sync with Dotori** starts
`dotori-sync watch` on that folder in a console window; the watcher keeps
running until that window is closed or interrupted, since dotori-sync
watches continuously rather than syncing once and exiting. The `--root-name`,
`--delete`, and other `watch` flags are not exposed through the menu; use
`dotori-sync watch` directly from a terminal for those.
