# Dotori CLI

Dotori CLI is an independent HTTP client for a self-hosted Dotori server. It
does not import Django modules or access the server database.

## Install for development

```bash
pipx install -e ./clients/dotori-cli
```

## Configure a remote and account

Open **Settings → Tokens** in the Dotori SPA, select **Read-only token** or
**Read/write token**, and issue it before registering the account. Read-only is
appropriate for browsing, search, RAG, and MCP; uploads require read/write. The full token is shown only
once. A server URL is stored once as a named `remote`; `origin` is the
conventional default remote name.

```bash
dotori remote add origin https://dotori.example.com
dotori account add personal --remote origin
dotori account use personal --remote origin

dotori status
dotori list
dotori upload report.pdf
dotori search "contract termination"
dotori ask "Summarize the main risks"
```

Omitting `--token` prompts without echoing the secret. Use `--remote`,
`--account`, and `--credential` to override the active context for one command.

```bash
dotori --remote origin --account personal --credential default search "policy"
```

`dotori connect URL --remote origin --account personal` remains as a shortcut
that adds the remote when needed and saves the account credential. The old
`--profile NAME` option and `profiles` command remain compatibility paths.

Public context is stored in `config.json` as `remote → account → workspace`,
with named credential metadata under each account. Raw tokens remain in the
separate `credentials.json` file, restricted to the current user where POSIX
permissions are supported. `workspace` is currently `null` because workspace
management is not yet a server feature. `DOTORI_CLI_TOKEN` can override the
selected stored token for automation.

## Folder sync

Select **Folder sync token** in **Settings → Tokens** and register it as the
`sync` credential of the same remote account. Newly issued read-only and
read/write tokens do not carry the `sync` scope.

```bash
dotori account add personal --remote origin --credential sync --token-type sync
dotori --remote origin --account personal sync ~/Documents
dotori --remote origin --account personal sync ~/Documents --apply
dotori --remote origin --account personal sync ~/Documents --apply --delete
```

`dotori sync` is one-way from the local folder to `/sync/<root-name>` on the
server. It is a dry run by default. `--apply` is required for uploads and
updates, and server-only entries are moved to trash only when `--delete` is
also present. Symbolic links are skipped. Use `--root-name NAME` when multiple
local roots have the same basename; each name must be unique for that server
account.

## Existing 0.2 configuration

When `config.json` does not exist, the CLI converts the existing
`profiles.json` and `credentials.json` into schema version 2. Normalized server
URLs become remotes, the active server becomes `origin`, and each old profile
becomes an account alias with a credential. The source `profiles.json` is kept
as a rollback copy, and the old token layout is saved as the
permission-restricted `credentials.v1.json` backup before `credentials.json` is
converted.
