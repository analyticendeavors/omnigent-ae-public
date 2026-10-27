# Deploying omnigent-ae

How to run omnigent-ae as a service on one Linux host with Docker Compose and
systemd: from a machine that has never run it to a green `/v1/ae/health`, then
the upgrade, the automatic update and the rollback. It is written for any
operator. Analytic Endeavors' own runbook for its host (its addresses, its
secrets source, its TLS fronts, its identity provider and its second user) lives
in a private repository and is not needed to follow this page.

To try it on one machine first, with no TLS, no identity provider, no systemd
and no `/secure` volume, follow the Quick start in the top-level `README.md`
(about ten minutes); this page is the service it grows into.

Every command here runs on the host unless it says otherwise. `<user>` is the
Linux account that owns the checkout and runs the host; the root steps want
`sudo`.

What is in this directory:

| File | What it is |
|---|---|
| `Dockerfile` | stage 2 of the image: the AE layer on upstream's server image |
| `docker-compose.override.yml` | the overlay on upstream's compose file: the published image, loopback `:8010`, the two env files, bind mounts under `AE_DATA_DIR` (default `/secure/omnigent`) |
| `compose.sh` | `docker compose` with the compose files and both env files filled in, plus `config/compose.local.yml` when it exists (this machine's own additions); everything else here calls it |
| `auto-update.sh` | one pass of the automatic deploy: the newest `ae-v*` tag on `main`, pulled, started, health-checked, rolled back on failure |
| `pull-secrets.sh` | one way to fill the two secrets files: Azure Key Vault to `/secure/env/omnigent-ae.env` and `/secure/env/omnigent-host.env`, atomically |
| `backup-omnigent.sh` | nightly `pg_dump` into `/secure/omnigent/backups`, newest seven kept |
| `install-host.sh` | the host's Python environment: `~/.omnigent-host/venv` with omnigent from the pinned submodule plus `packages/ae_omni_policies` |
| `systemd/ae-omnigent.service` | system unit: the compose stack, after the `/secure` mount, Docker and `tailscaled` (ordering only; a host without Tailscale needs no change) |
| `systemd/ae-omnigent-dump.service`, `.timer` | system units: the dump at 02:40 local |
| `systemd/ae-omnigent-host.service` | user unit: `omnigent host` in the foreground, restarted on failure |
| `systemd/user/ae-omnigent-auto-update.service`, `.timer` | user units: `auto-update.sh` every ten minutes |
| `systemd/user/ae-omnigent-limits.service`, `.timer` | user units: the plan limits relay every five minutes |
| `systemd/user/ae-omnigent-secrets-meta.service`, `.timer` | user units: the Key Vault expiry relay once a day (Secret expiry dates) |
| `vault-source-tags.sh` | propose, then with `--apply` set, the `ae-source` tags that give app secrets their Entra end dates (Secret expiry dates) |
| `host-relink.sh`, `systemd/user/ae-omnigent-host-relink.service` | the host's reconnect: a sign-in link and a host restart when the server rejects it (Re-login, Host relink) |
| `host-container.sh`, `host-container/` | a second user's host as a container: the images, the compose project with its egress proxy, the sign-in (Second user's host (container)) |
| `systemd/user/ae-omnigent-host-container@.service` | user unit template: one user's host container project, `up -d` and `down` |

The layout once installed, with the defaults the scripts and units ship with:

```
~/deploy/omnigent-ae/                the deploy clone, with upstream/ initialised; nobody works in it
                                     (The deploy clone, under Automatic updates)
  config/server.env                  settings (from config/server.env.example, gitignored)
  config/host.env                    settings (from config/host.env.example, gitignored)
/secure/env/omnigent-ae.env          secrets for the server
/secure/env/omnigent-host.env        secrets for the host
/secure/omnigent/data/               the container's /data: artifacts, accounts state
/secure/omnigent/postgres/           the Postgres data directory (bind mount)
/secure/omnigent/backups/            omnigent-<stamp>.sql.gz, newest seven
~/.omnigent-ae-auto-update/          auto-update state: last, log, previous, failed, pin, deferred
~/.omnigent-host/venv/               the host's Python (install-host.sh)
~/.omnigent/auth_tokens.json         the stored `omnigent login` (the host and the MCP server read it)
```

`/secure` is a path the overlay and the units name, and it must be a mount
point: `ae-omnigent.service` has `RequiresMountsFor=/secure`, and
`backup-omnigent.sh` refuses to write a dump unless `/secure` is mounted, so an
unmounted volume never collects a dump or starts an empty Postgres. An encrypted
volume is the intent (the database, the artifacts and the persisted cookie
secret sit there); for a plain directory, bind-mount it onto itself
(`sudo mount --bind /secure /secure`, persisted in fstab). Check with
`mountpoint /secure` before step 2.
`AE_SERVER_SECRETS` moves the server's secrets file elsewhere (step 1), and
`AE_DATA_DIR` in `config/server.env` moves the two bind mounts; the backups and
the host unit's secrets file keep the `/secure` paths.

## Before starting

- Docker with the compose plugin, 2.24 or newer (`docker compose version`): the
  override uses `!reset` and `!override`, the same requirement as upstream's
  https overlay.
- [`uv`](https://docs.astral.sh/uv/) for `<user>`, no sudo:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- The Claude CLI for `<user>`, signed in, before step 8: `curl -fsSL https://claude.ai/install.sh | bash`,
  then `claude auth login`. `deploy/install-host.sh` registers the AE MCP server with a
  `claude` that is already on `PATH` and only warns when there is none, and step 10's
  unit needs the CLI's directory on its `PATH`. The `CLAUDE_CODE_OAUTH_TOKEN` in the
  host's secrets file is the setup token for sessions, separate from this login.
- The checkout, with the submodule:

  ```bash
  git clone --recurse-submodules https://github.com/analyticendeavors/omnigent-ae-public.git ~/deploy/omnigent-ae
  cd ~/deploy/omnigent-ae
  ```

- A TLS front for the server's loopback port (step 7): `tailscale serve`, a
  reverse proxy, or a tunnel.
- Ports: the stack binds `127.0.0.1:8010`. If 8010 is taken, change
  `OMNIGENT_PORT` in `config/server.env` and the port your TLS front forwards to,
  nothing else.

## First install, in order

### 1. Secrets

The server and the host each read one secrets file. The files are the contract:
anything that writes the same keys, readable by root and by `<user>` (mode 640),
works. `deploy/compose.sh` reads the server's file from `AE_SERVER_SECRETS`,
default `/secure/env/omnigent-ae.env`; the host unit reads
`/secure/env/omnigent-host.env`.

| File | Keys |
|---|---|
| server (`AE_SERVER_SECRETS`) | required: `POSTGRES_PASSWORD`; with OIDC sign-in, `OMNIGENT_OIDC_CLIENT_SECRET` and `OMNIGENT_OIDC_COOKIE_SECRET`; with task-store, `AE_CF_ACCESS_CLIENT_ID` and `AE_CF_ACCESS_CLIENT_SECRET`. Optional: `AE_GITHUB_TOKEN`, `AE_GIT_TOKEN_<OWNER>` (one per GitHub account the Git repos card reads), `AE_FABRIC_WAKE_URL_<NAME>`, `AE_FABRIC_SUSPEND_KEY_<NAME>`, `AE_FABRIC_CAPACITIES_URL`, `AE_FABRIC_CAPACITIES_KEY`, `AE_DATABRICKS_CLIENT_ID`, `AE_DATABRICKS_CLIENT_SECRET`, `AE_AZURE_COST_TENANT_ID`, `AE_AZURE_COST_CLIENT_ID`, `AE_AZURE_COST_CLIENT_SECRET`, `AE_SHARED_ACCOUNTS_KEY` |
| host (`/secure/env/omnigent-host.env`) | `CLAUDE_CODE_OAUTH_TOKEN` |

Compose reads the env file with `$` interpolation, so no value may contain a
`$`. A Postgres password nobody needs to remember: `openssl rand -hex 24`.

`deploy/pull-secrets.sh` is one way to fill both: it reads the values from an
Azure Key Vault as `<user>` and places the files through sudo, atomically, so a
failed run leaves the previous files in place. Its defaults are AE's: the vault
(`AE_VAULT_NAME`), the secret names (the `KV_*` variables at the top of the
script) and the files' group (`AE_ENV_GROUP`, which must exist; use `<user>`'s
group). It fetches one named Fabric capacity and the shared capacities Logic App. It fetches, and `--check`
requires, all five server keys in the table's required column (the OIDC and
task-store pairs included) whichever features you use, and it rewrites the whole file,
so a key added by hand is lost on the next run unless the vault holds it too.
Without the OIDC or task-store features, write the file by hand, or adapt
`SERVER_KEYS` and the fetch calls in your fork. Every secret has to exist in the
vault first, the Postgres password included (a value nobody needs to remember,
stored without it touching argv:
`az keyvault secret set --vault-name <vault> --name omnigent-ae-postgres-password --file <(openssl rand -hex 24 | tr -d '\n')`).
Without `az` on the host, `uv tool install azure-cli` installs it without sudo,
and `az login --use-device-code` signs in from any browser. Run it as `<user>`:

```bash
AE_VAULT_NAME=<vault> AE_ENV_GROUP=<user> deploy/pull-secrets.sh          # sudo prompts once for the placement
AE_VAULT_NAME=<vault> AE_ENV_GROUP=<user> deploy/pull-secrets.sh --check  # both files 640, every key present, no values shown
```

Without Key Vault, write the two files any other way, then
`AE_ENV_GROUP=<user> deploy/pull-secrets.sh --check` still reports what each
holds, mode and owner included (it counts the OIDC and task-store keys as
required). `AE_SECURE_ENV_DIR` points the script at another directory than
`/secure/env`; the host unit still reads `/secure/env/omnigent-host.env`.

The Admin Center's Secrets section lists the same server keys with present or
missing, so a missing one is visible without a shell on the host. It shows what
the running server started with, so after a change to the file it catches up at
the next `deploy/compose.sh up -d`. Names only, never values. The host's
`CLAUDE_CODE_OAUTH_TOKEN` is not on it: the server never sees the host's file.

#### The GitHub token (optional)

The Your move page lists open pull requests by or waiting on the configured
login with their CI state, and the PR cards (a card per pull request a session
tracks, in the session's GitHub panel and in the Dashboard's Open PRs section)
show CI, mergeability and reviews with a Merge button. The server reads and
merges with one optional token; without it both say how to add it and
everything else works.

Permissions, fine-grained, and nothing else:

| Permission | Access | Why |
|---|---|---|
| Pull requests | Read and write | read the pull request and its reviews; merge it |
| Contents | Read and write | GitHub requires it to merge (the merge writes the base branch) |
| Checks | Read-only | the check runs behind the CI counts |
| Commit statuses | Read-only | statuses from outside Actions (a deploy preview, say) count toward CI |
| Workflows | Read and write | only to merge a pull request that changes `.github/workflows`; GitHub refuses those merges without it |
| Issues | Read and write | `complete_task` comments on and closes the parked task's linked issue; without it the task-store GitHub sync closes it within 15 minutes |
| Metadata | Read-only | added by GitHub automatically |

Repositories: every repo whose pull requests the sessions open, at least the
ones in `AE_GITHUB_REPOS`. A tracked pull request in a repo the token cannot
see shows as one error line on the Dashboard, naming the repo.

Once: create the token on GitHub (Settings, Developer settings, Fine-grained
tokens; an expiry and a reminder before it; if the organization requires
approval for fine-grained tokens, approve the request under the organization's
Settings, Personal access tokens), put it in the server's secrets
file as `AE_GITHUB_TOKEN` (with `pull-secrets.sh`, store it in the vault without
the value touching argv or shell history, for example
`read -rs TOKEN && az keyvault secret set ... --file <(printf %s "$TOKEN")`),
check that `deploy/pull-secrets.sh --check` shows `ok AE_GITHUB_TOKEN (optional)`,
then `deploy/compose.sh up -d --force-recreate omnigent` so the server reads it.

A token that expires shows as a 401 on the page, per repo, with the fix named. A
token that can read but not merge shows Merge's refusal with the missing
permissions named.

#### The Git repos card's tokens (optional)

The Admin Center's Git repos card (read only) lists open pull requests, Dependabot
alerts and branches with no pull request across several GitHub accounts. Turn it on
with `AE_GIT_SWEEP_CONFIG` (`owner/repo:path` of a Dependency Sweep config JSON)
and/or `AE_GIT_REPOS` (comma-separated `owner/repo`); `AE_GIT_VIEW_ONLY` repos are
shown but left out of the totals; `AE_GIT_SWEEP_WORKFLOW` names the sweep workflow
(default `dependency-sweep.yml`). `AE_GIT_KEEP_BRANCHES` (comma-separated
`owner/repo:branch`, exact branch name) marks branches kept on purpose: the card still
lists them but does not count them as old branches.

Each owner reads with `AE_GIT_TOKEN_<OWNER>` (owner upper-cased, every run of
non-alphanumerics as `_`: `fabrikam-labs` is `AE_GIT_TOKEN_FABRIKAM_LABS`), else
`AE_GITHUB_TOKEN`. An owner with neither shows as not connected and its repos are
not called. Use fine-grained, read-only tokens:

| Permission | Level | For |
|---|---|---|
| Metadata | Read-only | added by GitHub automatically |
| Contents | Read-only | branches, the sweep config, lockfiles |
| Pull requests | Read-only | open pull requests |
| Dependabot alerts | Read-only | the alert lists (a 403 on alerts names this) |
| Actions | Read-only | the sweep repo's last runs |

Restart the server after adding a key; a 401 on the card means the token expired.

#### The Admin Center's Fabric secrets (optional)

The Admin Center's Fabric section wakes a Fabric capacity and, with a second
secret, turns it off, through the capacity's Azure Logic App. Two optional
secrets per capacity, both in the server's secrets file:

| Variable | What it gives |
|---|---|
| `AE_FABRIC_WAKE_URL_<NAME>` | the card, its state and Wake; the value is the Logic App's trigger URL |
| `AE_FABRIC_SUSPEND_KEY_<NAME>` | Off; the Logic App suspends only with this key, so the wake link alone can never turn the capacity off |

The server finds a capacity from the variable's name; `deploy/pull-secrets.sh`
fetches one (`AETRAINING`), and another is two more fetches in it. Without either, the page
says how to add them and everything else works. After adding them, recreate the
server (`deploy/compose.sh up -d --force-recreate omnigent`). Neither value may
contain a `$` (a Logic App URL has none); `deploy/pull-secrets.sh` stops with
the secret's name if one does.

Since 2026-10-21 two more optional secrets list every Fabric capacity (three
subscriptions today) through one shared Logic App, `la-fabric-capacities`, built by
agents-and-automation's setup script, which also creates both secrets:

| Key Vault secret | Variable | What it gives |
|---|---|---|
| `fabric-capacities-url` | `AE_FABRIC_CAPACITIES_URL` | the shared Logic App's trigger URL: every capacity on the Compute card, with Pause, Start and resize |
| `fabric-capacities-key` | `AE_FABRIC_CAPACITIES_KEY` | the key it checks on every call (64 lowercase hex, sent as `x-capacity-key`) |

Both are needed; with only one, the page shows the wake URL capacities as before.
The same `$` rule applies. The price per capacity unit an hour is
`AE_FABRIC_USD_PER_CU_HOUR` in `config/server.env` (default 0.18).

#### The Admin Center's Databricks secrets (optional)

The Admin Center's Databricks section lists the SQL warehouses on one Databricks
workspace, starts and stops them, and shows spend against the account's prepaid
credit from `system.billing`. It signs in as a service principal with an OAuth
secret whose API scope is `sql` only (warehouses and statement execution). Two
optional secrets plus one setting:

| Where | Variable | What it gives |
|---|---|---|
| server secrets file | `AE_DATABRICKS_CLIENT_ID` | the service principal's application ID |
| server secrets file | `AE_DATABRICKS_CLIENT_SECRET` | its OAuth secret (scope `sql`); scopes are fixed when a secret is made, so another scope means a new secret |
| `config/server.env` | `AE_DATABRICKS_HOST` | the workspace URL; not a secret |
| `config/server.env` | `AE_AZURE_COST_SUBSCRIPTIONS` | the Azure card's subscriptions, `<guid>=<name>` pairs, comma separated; not a secret |

All three are needed; without them the section says how to add them and the rest
of the page works. After adding them, `deploy/compose.sh up -d --force-recreate
omnigent`. Neither secret may contain a `$`, for the same Compose reason as the
Fabric ones.

The spend strip also needs a grant from a metastore admin, done in Catalog
Explorer: `USE CATALOG` on the `system` catalog and `USE SCHEMA` plus `SELECT` on
`system.billing`, each to the service principal. Without it the strip says which
grant is missing. Spend is read on a warehouse, which bills until its auto stop,
so the server reads it only when the warehouse is already running or when
someone taps Refresh; it never starts a warehouse on a timer. The billing tables
lag by hours, so the strip shows the time its data runs to.

#### Shared Claude accounts

So a second user's host can use Claude accounts that the admin manages, without
copying tokens between hosts. The admin adds them under Settings, Claude accounts
(in OmniGent's own Settings, not the Admin Center); every user's account picker then lists them, marked
Shared, and a session's runner fetches the token from the server at launch
(`ae_omni_policies/shared_accounts.py` says how it is kept and guarded). The
tokens sit encrypted in `/data/ae-shared-claude-accounts.json`
(`/secure/omnigent/data` on the host, so inside the backup), under one key in
the server's secrets file:

| Variable | What it is |
|---|---|
| `AE_SHARED_ACCOUNTS_KEY` | a Fernet key (32 url-safe base64 bytes); several, comma-separated, rotate it: the first encrypts, any of them decrypts |

Without it the section says which variable is missing and refuses to add an
account, and a launch on a shared account fails with the same message. Make the
key with a throwaway `uv` environment, without the value touching argv or shell
history, and add it to the secrets file:

```bash
uv run --no-project --with cryptography python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Then `deploy/compose.sh up -d --force-recreate omnigent`.

Losing the key loses every shared token, since the file cannot be read without
it; the accounts would then have to be added again. To rotate it, set the new
key first and the old one after a comma and recreate the container: on start the
server re-encrypts every stored token under the new key (nothing to re-add).
Once the file's next load has done that, drop the old key and recreate again.

A token the server has handed to a host stays valid until it is revoked at
claude.ai: removing a shared account under Settings, Claude accounts stops new launches but does
not revoke a copy a host already holds, and a Claude session's agent can read its
own environment. When an account is removed because it leaked, revoke its
`claude setup-token` login at claude.ai.

### 2. Directories for the data

```bash
sudo mkdir -p /secure/omnigent/data /secure/omnigent/postgres /secure/omnigent/backups
sudo chmod 700 /secure/omnigent
```

Root-owned on purpose: the containers run as root, so a directory's own mode is
where the privacy lives.

### 3. Settings

```bash
cp config/server.env.example config/server.env
cp config/host.env.example config/host.env
```

Edit both. Nothing secret goes in either file; they are gitignored anyway.

- The host's URL: replace the `http://127.0.0.1:8010` origin in
  `OMNIGENT_ACCOUNTS_BASE_URL`, `OMNIGENT_WS_ALLOWED_ORIGINS` and
  `OMNIGENT_SERVER_URL` (in `config/host.env`) with the https origin your TLS
  front serves (step 7). The host's URL must match the base URL character for
  character (step 9).
- `OMNIGENT_IMAGE_TAG`: the tag to run, `ae-vX.Y.Z.N` (the newest tag on the
  Releases page, or the tag you build in step 5).
- `AE_IMAGE`: the package that tag lives in, `ghcr.io/<owner>/<repo>`. The example
  names the public build; a fork that publishes its own (step 5) names that one.
  The compose overlay and `deploy/auto-update.sh` both read it.
- Sign-in: the example ships accounts mode (username and password, the
  admin created on the first visit, step 7), which needs no further setting and
  suits a first install. For an identity provider instead, uncomment the OIDC
  block and replace the issuer and tenant, the client id,
  `OMNIGENT_OIDC_REDIRECT_URI` (your public callback origin) and
  `OMNIGENT_OIDC_ALLOWED_DOMAINS`; the administrator then goes in the
  `/data/admins` roster file (Adding an OIDC provider, under step 7).
- The optional features (task-store, GitHub, Fabric, Databricks, the policy
  model) and what each looks like when its variables are unset are in the
  top-level `README.md`, "Configure".

The live file is copied once, so a line the example gains later is easy to miss.
The image carries the example, and the Admin Center's Deploys section lists any
uncommented key in it that the running server's environment lacks or leaves
empty (`env_drift.py`; names only, never values). Keys between
`# drift-check: pending begin` and `# drift-check: pending end` are skipped
until the step the marker names sets them.

### 4. The server's config and agents

Nothing to copy. The image carries `config/server.yaml` at
`/opt/ae/config/server.yaml` and `agents/` at `/opt/ae/agents`, and
`config/server.env` points `OMNIGENT_CONFIG` and `OMNIGENT_BUILTIN_AGENT_DIRS`
there. Reading them from the image is what lets a tag push be the whole deploy
(see "Automatic updates"). The trade: changing an agent spec or the server
config (the harness flip described in the top-level README) is a commit and a
tag, not an edit on the host.

Global policies are the exception: they live in the database and are switched,
edited and deleted on Settings > Global Policies, so a change there needs no tag
and survives every deploy. `ae_seed_policies` in `config/server.yaml` only fills
a database that holds none of its policies (`packages/ae_omni_policies/README.md`,
"How it loads").

### 5. The image

The overlay runs `${AE_IMAGE}:${OMNIGENT_IMAGE_TAG}` from `config/server.env`, the
image the operator's own `.github/workflows/image.yml` publishes on every `ae-v*`
tag (AE's box sets `AE_IMAGE=ghcr.io/analyticendeavors/omnigent-ae`). AE's package
is private and stays private: its older tags were built before the identifier
scrub, so never make it public. A fork, the public repo included, publishes its
own package (the end of this step says what to change). The first push of
`image.yml` creates that package private; its owner may make it public in the
package settings, or log the host in once with a token that has `read:packages`:

```bash
sudo docker login ghcr.io -u <github-user>
```

Or build it yourself. The image has two stages: upstream's server image built
from the patched submodule, then the AE layer (`deploy/Dockerfile`) on top.
`just image` runs both and `just image-smoke` checks the result (the pinned
version, the Parking Lot extension and its bundle, both AE packages). Compose
uses a local image of the right name before it pulls, so tag the build with the
name and the tag `config/server.env` names:

```bash
just image && just image-smoke
docker tag omnigent-ae:local ghcr.io/<owner>/<repo>:ae-vX.Y.Z.N   # AE_IMAGE and OMNIGENT_IMAGE_TAG from config/server.env
```

A self-built image is not something `auto-update.sh` can pull, so leave the
automatic update off with it. Publishing your own package needs no file edits:
set the repository variable `AE_IMAGE` to `ghcr.io/<owner>/<name>` (lower case,
under your repository's owner), enable the Image and Release workflows, and put the
same value in `config/server.env`. Without the variable both workflows build and
check but never push or tag.

### 6. Units and the first start

The two system units name the checkout by an absolute path, because systemd
has no home specifier for system units. The shipped default is
`/home/reid/deploy/omnigent-ae`; set `AE_HOME` to your clone and render the
units while copying them. The user units need no rendering but do assume the
clone is at `~/deploy/omnigent-ae` (they name `%h/deploy/omnigent-ae`), so
another path means editing them too:

```bash
AE_HOME=$HOME/deploy/omnigent-ae   # the clone on this host
for u in ae-omnigent.service ae-omnigent-dump.service; do
  sed "s|/home/reid/deploy/omnigent-ae|$AE_HOME|g" "deploy/systemd/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
done
sudo cp deploy/systemd/ae-omnigent-dump.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Bring the stack up by hand the first time, so the pull and the migrations are
on a terminal rather than in a journal:

```bash
sudo deploy/compose.sh up -d
sudo deploy/compose.sh logs -f omnigent      # ctrl-c once it prints "No admin yet" with the base URL
curl --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors --fail http://127.0.0.1:8010/health
```

`/health` is upstream's unauthenticated liveness route (the Dockerfile's own
health check). The retries matter: while migrations run the port accepts and
then resets the connection, which plain `--retry` does not count as retryable.
Then hand the stack to systemd, which is idempotent over a running stack, and
arm the dump timer:

```bash
sudo systemctl enable --now ae-omnigent.service
sudo systemctl enable --now ae-omnigent-dump.timer
systemctl list-timers ae-omnigent-dump.timer   # next elapse 02:40 local (plus up to two minutes)
```

### 7. TLS front and the admin account

The server listens on `127.0.0.1:8010` only. Put an https front on it whose
origin is the one in step 3. With Tailscale, for example:

```bash
sudo tailscale serve --bg --https 8010 http://localhost:8010
tailscale serve status
```

A reverse proxy or a tunnel works the same way. Every browser origin the
server is reached on goes in `OMNIGENT_WS_ALLOWED_ORIGINS` (exact origins,
comma-separated: scheme, host and port), or its websocket is refused. Hosts
(step 9) keep using the base URL.

With OIDC sign-in there is no Create admin form and no password: sign in on the
origin of `OMNIGENT_OIDC_REDIRECT_URI`, and the login named in the admin roster
is the admin. The roster is `/data/admins` inside the data volume (on the host,
`/secure/omnigent/data/admins` after step 2): one login per line, `#` comments,
read again whenever the file changes, so no restart. `OMNIGENT_ADMIN_LIST_PATH`
moves it. It is merged with the `admins:` list in `config/server.yaml`, which
is the alternative for an operator who rebuilds the image from a fork; a login in
either is an admin, and removing a name never demotes anyone.

In accounts mode (`OMNIGENT_AUTH_PROVIDER` unset), open the base URL and use the
Create admin form with any username and a password from a password manager. The
first account created is the admin whatever its name; `admins:` in
`config/server.yaml` only promotes a login that matches a name listed there. Type
the username at `omnigent login`'s `Username` prompt and the password only at
`Password:`, which does not echo; a password typed at the username prompt shows
in plain text.

#### Adding an OIDC provider

Upstream's OIDC sign-in works with any provider that publishes a discovery
document. In general terms:

1. Register an app (a web client) with the provider. Its redirect URI is
   `https://<public origin>/auth/callback`, and only that origin can complete a
   browser sign-in.
2. In `config/server.env`: `OMNIGENT_AUTH_ENABLED=1`,
   `OMNIGENT_AUTH_PROVIDER=oidc`, `OMNIGENT_OIDC_ISSUER` (the issuer URL),
   `OMNIGENT_OIDC_CLIENT_ID`, `OMNIGENT_OIDC_REDIRECT_URI`, and the claim and
   domain settings the example shows (`OMNIGENT_OIDC_EMAIL_CLAIM`,
   `OMNIGENT_OIDC_ALLOWED_DOMAINS`, `OMNIGENT_OIDC_SESSION_TTL_HOURS`).
3. In the server's secrets file: `OMNIGENT_OIDC_CLIENT_SECRET` (from the
   provider) and `OMNIGENT_OIDC_COOKIE_SECRET` (a random value you make;
   rotating it voids every login grant).
4. In the `/data/admins` roster file (see above), the login the provider reports
   for the administrator, one per line; no rebuild. If you build your own image,
   `admins:` in `config/server.yaml` does the same (the file ships in the image,
   step 4, so that is a commit and a tag in your fork).
5. Add the public origin to `OMNIGENT_WS_ALLOWED_ORIGINS` and recreate the
   server.

Switching an existing server from accounts mode to OIDC creates new users for
the same people; upstream's identity remap
(`omnigent debug migrate-accounts-to-oidc`, completed by patch 0036, see
`docs/UPSTREAM-SYNC.md`) moves the data across, and it is best rehearsed on a
copy of the database first.

### 8. The host's Python environment

```bash
deploy/install-host.sh
```

As `<user>`, no sudo. It builds `~/.omnigent-host/venv` with `uv` (which brings
its own Python 3.12, so no `python3-venv` is needed), installs omnigent from the
pinned submodule and `packages/ae_omni_policies`, and proves `omnigent --version`
and `python -m ae_omni_policies.mcp` resolve. Rerun it after every upstream sync.

It also registers that MCP server as `ae` in the user's Claude Code (user scope)
and Codex, once, so interactive sessions on the host get the AE tools the
scheduled agents already had: `omni_set_status` lets a session flip its own
`ae.status`, and `omni_capacity`, `park_fork` and `list_parked` work from any
session. A session started before the registration needs a restart.

### 9. One interactive login

As of the pinned upstream there is no non-interactive login (no `--token`, no
environment token, and `omnigent host --non-interactive` fails when not logged
in; recheck after an upstream sync), so this is the one
step a person types on the host, once:

```bash
~/.omnigent-host/venv/bin/omnigent login https://<host origin>
```

With OIDC sign-in it prints a `/auth/login?ticket=...` URL on the URL it was
given. When that is not the callback origin, it cannot complete the sign-in:
open the same path on the origin of `OMNIGENT_OIDC_REDIRECT_URI` instead and sign
in; the ticket lives in the server process, so the CLI's poll completes. In
accounts mode it prompts for the admin username and password instead. Either way
it stores the session in `~/.omnigent/auth_tokens.json` keyed by that exact URL
(which is why `OMNIGENT_SERVER_URL` in `config/host.env` must match character for
character) and records the URL as the `server` key in `~/.omnigent/config.yaml`.
That key is how the agents' MCP server finds the OmniGent server: the host
launches it with the MCP SDK's default environment, which carries `HOME` but not
`OMNIGENT_SERVER_URL`. Check it after logging in:
`grep '^server:' ~/.omnigent/config.yaml` prints the same URL.

The host refreshes the stored token on its own; the server's
`OMNIGENT_GRANT_MAX_LIFETIME_DAYS` (upstream's default 30; 90 in
`config/server.env.example`) is how long that lasts before this step is due
again. See "Re-login" below.

### 10. The host unit

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/ae-omnigent-host.service ~/.config/systemd/user/
systemctl --user daemon-reload
sudo loginctl enable-linger "$USER"     # once: the user manager runs without a login session
systemctl --user enable --now ae-omnigent-host.service
systemctl --user status ae-omnigent-host.service
journalctl --user -u ae-omnigent-host.service -n 30 --no-pager
```

Within a few seconds the server's Hosts view lists the host as online. If the
unit stops with exit status 78 it found no stored login: repeat step 9 and
`systemctl --user restart ae-omnigent-host`.

The unit reads `config/host.env` and the host's secrets file, and spells out its
`PATH` (the Claude CLI and `uv` live in directories a systemd user session does
not search by default). Read the unit before installing it: make sure the
directory holding the `claude` CLI is on its `PATH`, and trim any entry your host
does not have. It also sets `OMNIGENT_DISABLE_KEYRING=true`, which forces the
file credential backend (a locked desktop keyring blocks forever); keep it on a
headless host.

Unit files are not a shipping path (`scripts/next-release-tag.sh`), so a merged
change to this unit cuts no tag, `auto-update.sh` never moves the checkout for
it, and nothing copies it into `~/.config/systemd/user/`. Install it from
`origin/main` without touching the checkout (no sudo; a user unit). The restart
drops sessions running on the host, so pick a quiet moment:

```bash
git -C ~/deploy/omnigent-ae fetch origin main
unit=~/.config/systemd/user/ae-omnigent-host.service
git -C ~/deploy/omnigent-ae show origin/main:deploy/systemd/ae-omnigent-host.service > "$unit.new" \
  && test -s "$unit.new" && mv "$unit.new" "$unit"
systemctl --user daemon-reload
systemctl --user restart ae-omnigent-host.service
systemctl --user show ae-omnigent-host.service -p Environment
```

The `.new` file plus `test -s` keep a failed `git show` from truncating the
installed unit; if either fails, the old unit stays and `$unit.new` is left for
a look.

### 11. The two scheduled tasks

Rows, not config, created by name (create or patch, never a duplicate). From the
host, with the venv step 8 built and the login from step 9:

```bash
~/.omnigent-host/venv/bin/python scripts/seed-schedules.py https://<host origin>
```

From a laptop with the dev setup, `just schedules-seed https://<host origin>` is
the same call. "AE steward" (weekdays 08:00) and "AE shutdown note" (weekdays
17:30, both in the server's `AE_TASK_STORE_TZ`, UTC when unset) then appear on the Tasks page, and the host runs
them.

### 12. Verify

```bash
curl --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors --fail http://127.0.0.1:8010/health
~/.omnigent-host/venv/bin/python scripts/ae-health.py https://<host origin>
sudo systemctl start ae-omnigent-dump.service && sudo ls -la /secure/omnigent/backups
```

The second line is `GET /v1/ae/health` with the stored login (the route sits
behind the login like every `/v1/ae` route, so a bare `curl` answers 401); it
prints `ok: true`, the version, and whether task-store is configured.
`just ae-health <url>` is the same from a laptop. The third proves the dump path
end to end: an `omnigent-<stamp>.sql.gz` in the backups directory.

Done means: `/health` 200, `ae-health` ok, the host online, the two tasks
listed, one dump on disk.

## Upgrade

A release is a tag on `main` of this repo, `ae-vX.Y.Z.N` (the upstream tag plus
an AE counter). The merge is the deploy and tags are made for you: when CI passes
on `main`, `.github/workflows/release.yml` tags the tip with the next number if
anything that ships changed (`packages/`, `config/`, `agents/`,
`deploy/Dockerfile`, `patches/`, `upstream.lock`, the `upstream` pointer) and
publishes it. A docs-only or tests-only merge makes no tag; to ship one anyway,
or to re-ship, run the Release workflow on `main` (Actions, Release, Run
workflow) with `force` on. A tag is published by `.github/workflows/image.yml`,
which builds the two stages the way CI does, runs the same smoke checks, and
pushes `$AE_IMAGE:ae-vX.Y.Z.N`, the repository variable (AE's own package is
`ghcr.io/analyticendeavors/omnigent-ae`). With the auto-update
timer installed (next section) that is the whole deploy: the timer notices the
tag within ten minutes of the image being published, deploys it, checks health
and rolls back on failure, and rebuilds the host when the upstream part of the
tag moved.

Upgrading a box set up before 2026-10-21: add the `AE_IMAGE` line to
`config/server.env` before the release carrying https://github.com/analyticendeavors/omnigent-ae-public/issues/13
reaches it (AE's box: `AE_IMAGE=ghcr.io/analyticendeavors/omnigent-ae`). The
compose overlay and `deploy/auto-update.sh` require it, and a checkout that
moves past that release without it cannot start the server or roll back.

Pushing a tag by hand still works (the release workflow then sees the
commit is tagged and does nothing for it):

```bash
git tag ae-vX.Y.Z.N origin/main && git push origin ae-vX.Y.Z.N
```

Then `cat ~/.omnigent-ae-auto-update/last` on the host (or the Hosts view) shows
it landed. The manual upgrade below is for when the timer is stopped:

```bash
cd ~/deploy/omnigent-ae && git pull && git submodule update --init
sed -i 's/^OMNIGENT_IMAGE_TAG=.*/OMNIGENT_IMAGE_TAG=ae-vX.Y.Z.N/' config/server.env
sudo deploy/compose.sh pull
sudo deploy/compose.sh up -d
curl --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors --fail http://127.0.0.1:8010/health
```

`up -d` recreates only the omnigent container (Postgres keeps running).
Migrations run at startup. When the upstream pin or `packages/ae_omni_policies`
changed, also rebuild the host so it matches the server:

```bash
deploy/install-host.sh
systemctl --user restart ae-omnigent-host.service
```

Config and agents ride the image (step 4), so nothing else is copied.

A dry run of the workflow (build and smoke, no push) is `workflow_dispatch` on
any ref with `push` left off.

## Automatic updates

So a release needs nobody at the host. `deploy/auto-update.sh` runs every ten
minutes from a user timer, as `<user>`, with no sudo.

Changes to `deploy/auto-update.sh` itself ship no tag
(`scripts/next-release-tag.sh` does not count it as a shipping path), so they
reach the host with the next shipping release, or sooner with a Release run with
`force` on. The pass that fast-forwards the checkout to them is still running
the old script, so new updater logic takes effect from the pass after that one.

One pass:

Before step 0: while a deploy hold is on (The deploy hold, below), the pass
records `on-hold` and stops. Nothing is fetched, deployed or rebuilt.

0. A host rebuild that failed on an earlier pass
   (`~/.omnigent-ae-auto-update/host-pending`, step 10) is retried first, on
   every pass, including the ones that deploy nothing: before the fetch (a
   fetch failure does not block it) and before any fast-forward, and only while
   the checkout's host code (`upstream.lock`, the `upstream/` pointer,
   `packages/ae_omni_policies`) matches the running tag's, so the host is never
   built ahead of the server. When the checkout has already moved on (a pass
   that waited for an image, a rolled-back tag), the log records
   `host-deferred` with the reason, `host-pending` stays, and the pass that
   deploys the checkout's tag rebuilds. A failed retry records `host-failed`
   and the pass goes on, so a newer tag can still deploy; a pass that then
   deploys nothing fails the unit. The retry also waits while a session is
   mid-turn, the same way as step 5 below (`deferred` in `last`, the clock in
   `deferred-host`).
1. `git fetch --tags`; the target is the highest `ae-v*` tag by version sort
   (or the pinned tag, below).
2. Nothing happens when the target is the running `OMNIGENT_IMAGE_TAG`, when it
   sorts below it, or when it was rolled back before.
3. With `~/.omnigent-ae-auto-update/hold-upstream` present, a target whose
   upstream part (`X.Y.Z` in `ae-vX.Y.Z.N`) differs from the running tag's is
   held: nothing is pulled or deployed and the checkout stays put (Day to day,
   below).
4. A tag whose commit is not on `origin/main` is refused (the unit fails).
5. A busy checkout fails the pass (`busy` in the log) and nothing moves: the
   clone is not on `main`, or `git status` shows a changed tracked file or a
   moved submodule (untracked files do not count). Otherwise the checkout
   fast-forwards to `origin/main` and updates the submodule. Each git step is
   tried up to five times, two seconds apart, while another git process holds a
   lock in the checkout; the lock file is never deleted. A git step that still
   fails records git's own first error line, not a guessed cause.
   Then the pass waits while a session is mid-turn: its last relay-observed
   `live_status` is running or waiting (a turn held on an approval prompt) and
   its runner checked in within five minutes, from one `psql` count through
   `compose.sh exec` given 30 seconds (`AE_AUTO_UPDATE_BUSY_TIMEOUT`). Nothing
   moves, `last` records `deferred` with the count and when it goes ahead
   anyway, and the next pass asks again. The pass asks a second time just
   before step 8 writes the new tag, because the pull and the dump can take
   minutes. Two hours (`AE_AUTO_UPDATE_MAX_DEFER`, seconds) after the first
   wait on the running tag, the deploy goes ahead even with sessions mid-turn; a
   newer tag merged meanwhile does not restart that clock. A pause does: when 30
   minutes (`AE_AUTO_UPDATE_PAUSE_GAP`, seconds, at least 1200) passed between
   two passes (the timer was stopped or the host was down), the two hours start
   again and the journal says so. Each pass that takes the lock writes its start
   time to `~/.omnigent-ae-auto-update/last-pass`, so a run of failing passes is
   not a pause. `~/.omnigent-ae-auto-update/deferred` holds the running tag and
   when the wait began, and is cleared when a deploy lands or a pass finds
   nobody mid-turn, so a pass that fails after the go-ahead (an image not
   published yet) does not add another two hours. A count that fails, hangs or
   is not a number deploys as before, with a note in the journal. Why: a host
   restart ends every turn in flight ("The connection to the host dropped
   unexpectedly").
6. `docker pull` of the tag. Not published yet (`image.yml` still building) is
   a quiet wait for the next pass; any other pull error fails the unit.
7. When the upstream part of the tag or the upstream code (`upstream.lock`, the
   `upstream/` pointer) moved, a `pg_dump` of the running database goes to
   `~/.omnigent-ae-auto-update/dumps/` before anything else changes (Rollback,
   below): an image rollback does not undo the migrations the new version ran. A
   failed dump records `dump-failed`, deploys nothing and fails the unit; the
   next pass tries again.
8. The deploy hold is read once more, right after the second busy check: a hold
   set while the pull or the dump ran stops the pass here (`on-hold`), and the
   tag deploys after the hold. Then the old tag goes to
   `~/.omnigent-ae-auto-update/previous`, the new one into `config/server.env`,
   then `deploy/compose.sh up -d` and the `/health` check with the retries from
   step 6 of the first install.
9. Unhealthy: the old tag goes back, `up -d` again, the new tag is written to
   `~/.omnigent-ae-auto-update/failed` so it is not retried, and the unit fails.
10. Healthy and the host's code moved between the running tag and the new one
    (`upstream.lock`, the `upstream/` pointer or `packages/ae_omni_policies`):
    `deploy/install-host.sh` and a restart of `ae-omnigent-host`. Compared tag
    to tag, because a pass that waits for the image has already fast-forwarded
    the checkout. A failed rebuild or restart records `host-failed` and leaves
    the tag in `~/.omnigent-ae-auto-update/host-pending`, which step 0 retries
    at the start of every pass until it succeeds (`host-ok` in the log); a
    deploying pass also rebuilds whenever that file is present.

### The deploy hold

A deploy restarts the server and, when the host's code moved, the host, and
either one ends every turn in flight. The Admin Center's Deploys section holds
deploys for 1, 2 or 3 hours, and Resume ends the hold early. The page shows who
set the hold and until when, and the hold ends on its own. It is one file in the
server's data folder, `/data/ae-deploy-hold.json` (`AE_DEPLOY_HOLD_FILE`
overrides). It is written through `POST /v1/ae/deploy/hold` with `{"hours": N}`
and `POST /v1/ae/deploy/resume`, and read through `GET /v1/ae/deploy/hold`
(`ae_omni_policies/deploy_hold.py`). A new hold replaces the old one, counted
from the time it is set.

The pass reads the hold with
`deploy/compose.sh exec -T omnigent python -m ae_omni_policies.deploy_hold`,
which prints `none` or `held <until> <who>`. It reads through the container
because the pass runs as `<user>` and `/secure/omnigent` is root-only. It reads
twice: before step 0, and again just before step 8 writes the new tag. A hold
that cannot be read deploys as before, with a note in the journal, so a broken
hold never blocks a release. The server being down, a corrupt file, or an image
from before this change all count as unreadable. Every held pass clears
`deferred` and `deferred-host`, so sessions still mid-turn when the hold ends
get the full two hours of step 5, counted from then.

### The deploy clone

Every deploy runs from `~/deploy/omnigent-ae`, a clone that only
`auto-update.sh` moves. Work in a different checkout: a pass that fast-forwards
and runs `git submodule update` under a session that has a fork branch checked
out in `upstream/` resets it to the pinned commit, and a host whose editable
`omnigent` install points at a working checkout spawns its runners from
whatever branch `upstream/` is on, not the release. The units, the compose
overlay's `env_file` (through `compose.sh`) and `install-host.sh` all follow the
clone they run from. The Compose project name stays `omnigent` (upstream's file
pins it), so moving a deploy from one clone to another keeps the same containers
and Postgres bind mount. As `<user>`, at a quiet moment (the host restart stops
running sessions), with `<old>` the clone the deploy ran from:

```bash
systemctl --user stop ae-omnigent-auto-update.timer
git clone --recurse-submodules https://github.com/analyticendeavors/omnigent-ae-public.git ~/deploy/omnigent-ae
cd ~/deploy/omnigent-ae
# main at the running tag, so the overlay matches the running image; the next pass fast-forwards it
git reset --hard --quiet "refs/tags/$(sed -n 's/^OMNIGENT_IMAGE_TAG=//p' <old>/config/server.env)"
git submodule update --init
cp -p <old>/config/server.env <old>/config/host.env config/
deploy/install-host.sh                       # the host venv now imports from this clone
cp deploy/systemd/user/ae-omnigent-auto-update.service deploy/systemd/user/ae-omnigent-limits.service \
   deploy/systemd/ae-omnigent-host.service ~/.config/systemd/user/
systemctl --user daemon-reload
# the two system units, rendered as in step 6, then: sudo systemctl daemon-reload
deploy/compose.sh up -d                      # same project, now reading this clone's config/server.env
curl --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors --fail http://127.0.0.1:8010/health
systemctl --user restart ae-omnigent-host.service
systemctl --user start ae-omnigent-auto-update.timer
```

Then delete the old copies of the two settings files so there is one place to
edit.

### One-time install

As `<user>`, no sudo (linger is already on, step 10):

```bash
cd ~/deploy/omnigent-ae && git pull --ff-only && git submodule update --init
# The registry login for <user> (root's login does not reach a user unit), if the package is private.
docker login ghcr.io -u <github-user>
# The image config/server.env names (AE_IMAGE and OMNIGENT_IMAGE_TAG).
docker pull "$(sed -n 's/^AE_IMAGE=//p' config/server.env | tail -n 1)":"$(sed -n 's/^OMNIGENT_IMAGE_TAG=//p' config/server.env | tail -n 1)"
# The units.
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/ae-omnigent-auto-update.service deploy/systemd/user/ae-omnigent-auto-update.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ae-omnigent-auto-update.timer
systemctl --user start ae-omnigent-auto-update.service   # one pass now
cat ~/.omnigent-ae-auto-update/last                      # ... result=noop
```

`<user>` must be in the `docker` group, since the pass runs `deploy/compose.sh`
without sudo.

### Day to day

- **Status:** `cat ~/.omnigent-ae-auto-update/last` is the last pass (time,
  from, to, result: `noop`, `waiting`, `held`, `on-hold`, `deferred`, `ok`,
  `rolled-back`, `refused`, `git-failed`, `dump-failed`, `host-failed`,
  `host-ok`, `host-deferred`, ...);
  `~/.omnigent-ae-auto-update/log` keeps every pass that did something;
  `journalctl --user -u ae-omnigent-auto-update -n 50 --no-pager` has the full
  output; `systemctl --user --failed` lists it after a rollback or a refusal.
- **Hold for up to three hours:** the Admin Center's Deploys section, Hold 1 h,
  2 h or 3 h (The deploy hold, above). It ends on its own; Resume ends it early.
- **Pause longer:** `systemctl --user stop ae-omnigent-auto-update.timer` (until
  the next reboot; `disable --now` to keep it off). `start` resumes. No page
  shows this pause, so prefer the hold.
- **Pin a tag:** `echo ae-vX.Y.Z.N > ~/.omnigent-ae-auto-update/pin`. The next
  pass deploys that tag (older or newer, same health check and rollback) and
  stays there whatever is pushed. `rm ~/.omnigent-ae-auto-update/pin` unpins.
- **Hold upstream moves:** `touch ~/.omnigent-ae-auto-update/hold-upstream`.
  A tag whose upstream part differs from the running one (`ae-v0.14.0.1` while
  `ae-v0.13.0.5` runs) is then not deployed: the pass records `result=held`,
  exits 0 and leaves the checkout alone, and the tag is not marked failed. A
  tag with the same upstream part (an AE-only release) still deploys, but only
  while it is the newest tag; once a tag on the new upstream exists, it is the
  target and nothing moves. That includes fixes to this script, which reach the
  host through the fast-forward a hold skips.
  `rm ~/.omnigent-ae-auto-update/hold-upstream` lifts it; the next pass deploys
  the held tag (dump first, step 7). With both a pin and a hold, the hold wins
  for a pinned tag on another upstream.
- **Deploy now, sessions mid-turn or not:**
  `AE_AUTO_UPDATE_MAX_DEFER=0 deploy/auto-update.sh` from the deploy clone runs
  one pass that does not wait (step 5). The turns in flight end.
- **Retry a rolled-back tag:** `rm ~/.omnigent-ae-auto-update/failed`. A newer
  tag is deployed regardless.

## Plan limits relay

The composer's context ring and the Parking Lot strip show the Claude Code and
Codex plan limits from `GET /v1/ae/limits`; the host fills it. Once, as `<user>`
(`docs/PLAN-LIMITS.md` has the why and the empty states):

```
# 1. optional: per-model weekly limits, usage credits and the plan name need a
#    stored Claude login with the profile scope; the sessions keep the setup token
env -u CLAUDE_CODE_OAUTH_TOKEN claude auth login
# 2. the relay module in the host venv (the auto-update does this on a release
#    that changes packages/ae_omni_policies)
deploy/install-host.sh
# 3. the timer, every five minutes
cp deploy/systemd/user/ae-omnigent-limits.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ae-omnigent-limits.timer
~/.omnigent-host/venv/bin/python -m ae_omni_policies.limits_relay --dry-run
journalctl --user -u ae-omnigent-limits -n 20
```

## Secret expiry dates

The Admin Center's Server secrets list shows, for each key, when the vault says its
secret expires, when it last changed, and Edit and Rotate links (2026-10-23). The
server never reads Key Vault (the vault uses access policies, and a live container
should not hold a vault login for dates), so a daily user timer on the box does the
reading as reid: `python -m ae_omni_policies.secrets_relay` runs `az keyvault secret
list` (attributes only; it never calls `secret show`) and posts name, enabled, expires
and updated to `POST /v1/ae/secrets/meta` (until 2026-10-23 only for the names this
deployment maps to its keys; now for every secret, below). No value, length or hash is read or sent. The server keeps
the last post in `/data/ae-secrets-meta.json` (`AE_SECRETS_META_FILE` moves it; set
it empty to keep it in memory). A card with no reading, or one over 36 hours old, says
so and names the command below.

```
# 1. the relay module in the host venv (the auto-update does this on a release
#    that changes packages/ae_omni_policies)
deploy/install-host.sh
# 2. az logged in as this user (uv tool install azure-cli; az login --use-device-code)
# 3. the timer, once a day at 06:15 local time
cp deploy/systemd/user/ae-omnigent-secrets-meta.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ae-omnigent-secrets-meta.timer
# 4. run it now and see what it would post, then post it
~/.omnigent-host/venv/bin/python -m ae_omni_policies.secrets_relay --dry-run
systemctl --user start ae-omnigent-secrets-meta.service
journalctl --user -u ae-omnigent-secrets-meta -n 20
```

A secret shows "no expiry set" until someone sets one: in the portal on its Edit page,
or `az keyvault secret set-attributes --vault-name ae-secrets-vault --name <name>
--expires <date>`. Edit opens the secret in the Azure portal. Rotate opens the system
that issues the value (GitHub, the Databricks account console, the Entra app) and the
hint beside it names the account, because a Databricks login in the wrong tenant cost
an hour on 2026-10-23.

Since the Key Vault card (2026-10-23) the relay posts every secret of both vaults:
the box's (`AE_VAULT_NAME`, default `ae-secrets-vault`) and each extra one
(`AE_EXTRA_VAULTS`, comma-separated, default empty, so no extra vault; set it in `config/host.env`, which the relay unit loads, to add the website's vault). It also says, by key name only, whether `/secure/env/omnigent-host.env`
(`AE_HOST_ENV_FILE`) has `CLAUDE_CODE_OAUTH_TOKEN`; the file is 640 root:reid, so reid
can read it, and only a yes or no leaves the relay. A part it cannot read (an extra
vault, the host's file, the Entra dates) is named in the journal and left out, the
rest is posted, and the run exits 1 so the timer shows it.

### Expiry alerts

Since 2026-10-23 the server turns those dates into phone alarms. Right after each post
it files a task in the task store (the same one the Parking Lot uses, so only when
`AE_TASK_STORE_URL` and the Access token are set) for every secret whose effective
expiry is 30 days or fewer away, in either vault; at 7 days it moves the same task's
alarm, and once more on expiry. The alarm is `AE_SECRET_ALERT_TIME` (default 09:00) in
`AE_TASK_STORE_TZ`, and the task app pushes it to the phone. Tasks land in the list
named `AE_SECRET_ALERT_LIST` (no list when unset) with the `ae` tag when the app has
it; the note links to the Rotate and Edit pages and names the owner set on the Key
Vault card. When the expiry moves out (rotated), the server closes the task. What was
sent is kept in `/data/ae-secret-alerts.json` (`AE_SECRET_ALERTS_FILE`), so restarts
and repeat posts file nothing twice. `AE_SECRET_ALERTS=off` turns it off. A failure
(the store down, a list name that matches nothing) is logged, shown on the card, and
tried again with the next day's post. Owners set on the card live in
`/data/ae-secret-owners.json` (`AE_SECRET_OWNERS_FILE`).

### Entra end dates for app secrets

Only 2 of 103 vault secrets carry an expiry, but every Entra app client secret has a
real end date on its app. The relay reads them with `az ad app list --all` (appId,
display name and each secret's `endDateTime`; never the `hint`, which is the start of
the secret) and puts the latest one on each vault secret tagged
`ae-source=entra:<appId>`. The card then shows the earlier of the vault's date and the
app's, with "from Entra" beside it. Tags come back from `secret list`, so no value is
read. The server's own GitHub calls do the same for `AE_GITHUB_TOKEN` from GitHub's
`github-authentication-token-expiration` header ("from GitHub").

`deploy/vault-source-tags.sh` proposes the tags: a known pair (azure-cost-client-secret
is ae-omnigent-azure-cost, omnigent-oidc-client-secret is AE OmniGent, the server's
`OMNIGENT_OIDC_CLIENT_ID`), or a secret whose name, less its `-client-secret` ending,
has every word in exactly one app's display name. Already tagged secrets are left
alone, and anything unsure is left out (add it to `KNOWN_SOURCES` in
`secrets_relay.py` once its app is known). The script reads `AE_VAULT_NAME` and `AE_EXTRA_VAULTS` from the
`config/host.env` next to it and prints which file it used; that is the relay unit's file
only when run from the deployed tree (`~/deploy/omnigent-ae`), so run it there.

```
deploy/vault-source-tags.sh            # print each proposal and its az command; sets nothing
deploy/vault-source-tags.sh --apply    # set them, keeping each secret's other tags
systemctl --user start ae-omnigent-secrets-meta.service   # then post the new dates
```

Tagging does not count as a change. `set-attributes` bumps a secret's `updated`, so
the card's "changed" date and its 180-day count read `created` instead, when the
current version was made, which `secret list` reports and a tag never touches. An app
with more than one client secret shows "latest of N": the relay uses the newest end
date. A run that cannot read the Entra apps, the website's vault or the host's file
says so, posts the rest, and the server keeps its previous reading of the missing
part, marked "from <date>" on the card.
## Fast mode, per session

The model menu of a Claude session has a Fast mode switch, which types
`/fast on` or `/fast off` into that session's Claude Code terminal. On its own,
`/fast` also saves the choice as the default for every later session
(`fastMode` in `~/.claude/settings.json`), and fast mode bills usage credits. To
keep it per session, the host user's `~/.claude/settings.json` carries one more
key:

```json
"fastModePerSessionOptIn": true
```

With it every session starts with fast mode off, the switch changes only its own
session, and a session relaunched after a host restart comes back with fast mode
off.

## Rollback

With the timer running, pin the previous tag (it is in
`~/.omnigent-ae-auto-update/previous`) and let the next pass, or one started by
hand, deploy it:

```bash
cp ~/.omnigent-ae-auto-update/previous ~/.omnigent-ae-auto-update/pin
systemctl --user start ae-omnigent-auto-update.service
cat ~/.omnigent-ae-auto-update/last
```

A rollback past the release that moved the AE policies to `ae_seed_policies`
(2026-10-18) runs each of them twice: once from the older image's `policies:`
block and once from the database rows the newer image seeded. That includes the
automatic rollback after a failed `/health`, since the seed runs before the
server answers. Switch the five database copies Off on Settings > Global
Policies until the newer tag is back, then On again; switched Off they still
count as rows, so nothing is reseeded.

By hand, with the timer stopped, the previous tag is still in the registry, so
a rollback is the upgrade with the old tag:

```bash
sed -i 's/^OMNIGENT_IMAGE_TAG=.*/OMNIGENT_IMAGE_TAG=ae-vX.Y.Z.(N-1)/' config/server.env
deploy/compose.sh up -d
```

A rollback across an upstream move that added migrations needs the database
restored too: an image rollback does not undo migrations. The auto-update takes
that dump itself before every deploy that moves upstream (step 7 of "Automatic
updates"), into `~/.omnigent-ae-auto-update/dumps/` (700, the user's own, newest
three kept; `/secure/omnigent/backups` is root-only and the updater runs as
`<user>`). Each name is `omnigent-<UTC stamp>-<tag it was taken on>.sql.gz`, so
the tag to go back to is in the name. As `<user>`, no sudo, with the timer
stopped so it does not move the server again halfway:

```bash
cd ~/deploy/omnigent-ae
systemctl --user stop ae-omnigent-auto-update.timer
ls -1 ~/.omnigent-ae-auto-update/dumps/          # newest last
dump=~/.omnigent-ae-auto-update/dumps/omnigent-<stamp>-ae-vX.Y.Z.N.sql.gz
# 1. the tag the dump was taken on, and stop the server (Postgres keeps running)
sed -i 's/^OMNIGENT_IMAGE_TAG=.*/OMNIGENT_IMAGE_TAG=ae-vX.Y.Z.N/' config/server.env
deploy/compose.sh stop omnigent
# 2. an empty database in place of the migrated one
deploy/compose.sh exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
# 3. the dump into it, then the old image
gunzip -c "$dump" | deploy/compose.sh exec -T postgres sh -c 'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
deploy/compose.sh up -d
curl --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors --fail http://127.0.0.1:8010/health
# 4. keep the timer from moving it forward again, then start it
echo ae-vX.Y.Z.N > ~/.omnigent-ae-auto-update/pin
systemctl --user start ae-omnigent-auto-update.timer
```

Anything written to the server after the dump is lost by the restore. The
nightly dump in `/secure/omnigent/backups` (Backups, below) is the older
fallback; its restore line is in the header of `deploy/backup-omnigent.sh`.

## Re-login

The host's stored login refreshes itself for `OMNIGENT_GRANT_MAX_LIFETIME_DAYS`
days, counted from the moment the login was approved and checked on every
refresh (upstream's `omnigent/server/routes/device_auth.py`). Upstream's default
is 30; `config/server.env.example` sets 90, because an unattended host should not
lapse while nobody is at it: when it lapses the host unit stops with exit status
78 and stays stopped (the unit does not restart on that status, by design), and
every session and scheduled agent on that host stops with it. Repeat step 9 and
restart the unit.

The value lives in the host's own `config/server.env`, which is gitignored, so a
change to the example changes nothing on a running server. Add the line there
and recreate the container, in the deploy clone; the server reads it once at
startup:

```bash
grep -q '^OMNIGENT_GRANT_MAX_LIFETIME_DAYS=' config/server.env \
  || echo 'OMNIGENT_GRANT_MAX_LIFETIME_DAYS=90' >> config/server.env
deploy/compose.sh up -d --force-recreate omnigent
```

Because the window is measured from the approval, a login that has not lapsed
yet gets the longer window once the server restarts; a lapsed one needs step 9.
A value that is not a positive whole number falls back to 30 with a warning in
the server log. A calendar reminder a few days short of the window is still the
honest fix until upstream ships a device or token login.

### Host relink

When the server rejects the host's credential, the host keeps retrying and logs
`Connection refused (HTTP 403): the host tunnel was rejected`; a lapsed login
stops the unit instead (exit status 78, above). Either way the fix is step 9
plus a host restart, and `deploy/host-relink.sh` does both:

- The user unit `systemd/user/ae-omnigent-host-relink.service` runs
  `host-relink.sh watch`, which follows the host's journal. The host prints a
  rejection there at the first one and at every 30th, with the count ("The
  server has rejected it 30 times in a row"); at 30, or when the host unit is
  found failed (a lapsed login, or a host that never connected and gave up
  after three rejections), it runs `omnigent login`, sends the sign-in link (moved
  to the origin in `OMNIGENT_OIDC_REDIRECT_URI`) as a task-store reminder, and
  restarts `ae-omnigent-host.service` once "Logged in as" appears. Up to five
  links, each good for five minutes; a reminder that could not be sent ends its
  link at once. Then a "still disconnected" reminder, and nothing more for 15
  minutes. A unit stopped on purpose (`systemctl --user stop`) is left alone.
- The Admin Center's Box host card shows the host's state and what the helper
  last reported, and why a loop failed: links that lapsed, a sign-in whose
  restart failed, or reminders that never went out. While the host is not
  connected and no loop runs, it carries **Send me a reconnect link**, which
  runs the same loop at once, for a failed unit too. The helper takes the
  request every 15 s while the host is rejected or down, and every 10 minutes
  while it looks healthy (between those, only `systemctl --user is-active`).
  The button is hidden while the host is connected or a link is out, the
  server refuses the request while the host is online, and the helper checks
  the server's hosts table before it acts, because the restart would end every
  session on the host. Requests left before or during a loop are dropped when
  it ends, so one incident is one login and one restart.

The helper reaches the server's `/data` files and its task-store token through
`deploy/compose.sh exec -T omnigent python -m ae_omni_policies.host_relink`, as
the deploy hold does, since `/secure/omnigent` is root-only, and reads the
hosts table through `compose.sh exec -T postgres psql`, as the busy count does.
It keeps nothing secret on the host side; the login's output (a live ticket)
sits in a 600 file under `~/.local/state/ae-host-relink` only while an attempt
runs, and a HUP, INT or TERM removes it. Without task-store the reminders cannot
be sent, so the helper cannot deliver a link.

One-time install, as `<user>`:

```bash
cp ~/deploy/omnigent-ae/deploy/systemd/user/ae-omnigent-host-relink.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ae-omnigent-host-relink.service
journalctl --user -u ae-omnigent-host-relink -n 5    # "watching ae-omnigent-host.service (connected: 1)"
```

`host-relink.sh relink` runs one login loop by hand, with the same reminder and
restart.

## Second user's host (container)

For a user who reaches OmniGent from a browser only, so their agents have to run
on the server's machine, without sharing anything with the first user's host: no
files (nothing in the first user's home, no `/secure`), no logins (the first
user's Claude, GitHub, Azure and Codex credentials live in that home and the
host's secrets file), and no network reach to what the machine serves for the
first user (other local services, ssh, the server's own Postgres, the tailnet).
A separate Linux user would need sudo for every step and still share the
machine's network; a container gets all three boundaries from Docker, and
`<user>` can run it from the `docker` group.

### What runs

`deploy/host-container.sh <name>` drives one compose project per user,
`omnigent-host-<name>` (`host-container/docker-compose.yml`):

| Container | Image | Networks |
|---|---|---|
| `omnigent-host-<name>` | `omnigent-ae-host:<server tag>`: upstream's own `--target host` image built from the patched submodule (python 3.12, omnigent, node 22, the claude, codex and pi CLIs, git, tmux, gh), plus a pinned Claude Code, the Azure CLI and the non-root user `agent` (uid 61000); `host-container/Dockerfile` | `omnigent-host-<name>`, `omnigent-host-<name>-egress` |
| `omnigent-host-<name>-egress` | `omnigent-ae-host-egress:<server tag>`: Squid (`ubuntu/squid` pinned by digest) with `host-container/squid.conf` baked in | `omnigent-host-<name>-egress`, `omnigent-host-<name>-outbound` |

- **The server** is `http://omnigent:8000` on the network `omnigent-host-<name>`,
  which this machine's `config/compose.local.yml` declares (internal) and joins
  the server container to (gitignored, from `config/compose.local.yml.example`;
  `compose.sh` adds it when it exists, since 2026-10-22, so the public overlay
  names no user). Postgres is not on it, and no other user's host is. The server
  stack owns the network so the server never depends on a host project; the host
  project only attaches to it. Both networks are created with
  `com.docker.network.bridge.inhibit_ipv4`, because Docker otherwise gives an
  internal network a gateway address on the machine and a container can open
  any port the machine serves on a wildcard address through it; with no bridge
  address there is nothing to connect to. The option only applies when a network
  is created, so an `omnigent-host-<name>` network made without it must be
  removed (stop the host first) and recreated. The server joins it when its
  container is next recreated (the next release deploy, or
  `deploy/compose.sh up -d`).
- **The internet** goes through the egress proxy only. Both of the host's
  networks are internal, so it has no route anywhere else; the proxy allows HTTP
  to ports 80 and 443 and CONNECT to 443, and refuses every private, loopback,
  link-local, Docker and tailnet (100.64.0.0/10) destination by resolved
  address. `HTTPS_PROXY` and friends are set on the host and forwarded to its
  runners through `OMNIGENT_RUNNER_ENV_PASSTHROUGH`, so Claude Code, gh, az, git,
  npm and pip in a session all use it; `NO_PROXY` keeps the server connection
  direct.
- **Home** is the named volume `omnigent-host-<name>-home` at `/home/agent`:
  `~/.omnigent` (the OmniGent login), `~/.claude`, `~/.config/gh`, `~/.azure`,
  `~/workspaces`. Nothing on the machine is mounted; there is no docker socket.
- **Hardening and limits**: `no-new-privileges`, tini as PID 1, 16 GB of memory,
  8 CPUs and 4096 processes by default (`AE_HOST_MEM_LIMIT`, `AE_HOST_CPUS`,
  `AE_HOST_PIDS_LIMIT` in the environment of `host-container.sh` change them).
- **Supervision**: `restart: unless-stopped` brings both containers back after a
  reboot or a Docker restart; the user unit
  `systemd/user/ae-omnigent-host-container@.service` is the switch, the same
  oneshot shape as `ae-omnigent.service`. Its `up` builds the images when the
  server's tag has none yet. The host itself runs `host-container/entrypoint.sh`,
  which plays systemd's part: with no login it waits for one instead of exiting,
  after a permanent stop (lapsed or rejected login) it waits for a new login
  (and retries the stored one every 15 minutes), and after any other exit it
  starts again in 10 seconds.

### First start, once

Each name needs its network in `config/compose.local.yml` first (A further
user, below). Without that file, `compose.sh` warns whenever `omnigent-host-*`
networks exist, since the server would join none of them. Then, as `<user>` in the deploy clone (no sudo):

```bash
cd ~/deploy/omnigent-ae
# 1. The server joins omnigent-host-<name> when its container is recreated:
#    the next release deploy does it; to do it now, at a quiet moment
#    (it restarts the server, so every session reconnects):
deploy/compose.sh up -d
docker network inspect omnigent-host-<name> --format '{{range .Containers}}{{.Name}} {{end}}'   # omnigent-omnigent-1

# 2. Build and start (the first build takes a few minutes):
cp deploy/systemd/user/ae-omnigent-host-container@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ae-omnigent-host-container@<name>
deploy/host-container.sh <name> logs --tail 5 host   # "ae-host: no usable OmniGent login ..."
```

### Signing the host in as the user

`omnigent login` runs inside the container, as its user, against the internal
URL, with no terminal on the user's side. It needs OIDC sign-in: the script
builds the link from `OMNIGENT_OIDC_REDIRECT_URI` and stops if it is unset.

```bash
deploy/host-container.sh <name> login
```

It prints a link on the public origin, `https://<public origin>/auth/login?ticket=<ticket>`,
and then waits up to five minutes.

1. Send that link to the user; it is good for five minutes and for one sign-in.
2. The user opens it in their own browser and signs in. Whoever signs in owns
   the host, so the link goes to them and nobody else.
3. The command prints `Logged in as ...`; within ten seconds the container
   starts the host, and the user's Hosts view lists `omnigent-host-<name>` as
   online. The first user's does not: hosts are per user.

Why the link is rewritten: the CLI asks the server for a ticket at
`http://omnigent:8000` and prints `Opening browser for login:
http://omnigent:8000/auth/login?ticket=...`, which only the container can reach.
The ticket lives in the server process, so the same path on the public origin
completes it (the OIDC callback is fixed to that host anyway, as in step 9), and
the CLI's poll over the internal network collects the login. The script takes the
origin from `OMNIGENT_OIDC_REDIRECT_URI` in `config/server.env`. The stored token
is keyed by `http://omnigent:8000`, the URL the host is started with and the
`server` key in the container's `~/.omnigent/config.yaml`; the public URL is never
stored, so it does not matter that the container cannot reach it.

The same command renews a lapsed login (`OMNIGENT_GRANT_MAX_LIFETIME_DAYS`, as in
Re-login); the container picks the new login up without a restart.

Inside their sessions the user signs in to Claude Code (`/login` in a Claude
terminal), `gh auth login --web` and `az login --use-device-code` themselves; each
prints a link they open in their browser, and the result stays in their volume.
Credentials they would rather hand over go in `config/hosts/<name>.env`
(`host-container/user.env.example`), never one of the first user's.

### After a release

`auto-update.sh` moves the server only. The host container keeps the OmniGent it
was built with until it is rebuilt for the new tag, which restarts it and ends
the sessions running on it:

```bash
deploy/host-container.sh <name> build
systemctl --user restart ae-omnigent-host-container@<name>
```

### A further user

Pick a lower-case name (it names the containers, network and volume), then:

1. `config/compose.local.yml` (gitignored; start from
   `sed 's/<user>/<name>/g' config/compose.local.yml.example`, never an unedited
   copy, whose placeholder network name stops the server stack): add `host-<name>: {}` under the server's
   `networks:` and a `host-<name>` entry (`name: omnigent-host-<name>`,
   `internal: true`, and the `driver_opts` below) to the top-level `networks:`
   block, beside any user already there; then `deploy/compose.sh up -d`
   recreates the server (every session reconnects, so pick a quiet moment).
   The network needs the isolation option, or a container on it can reach any
   port the machine serves on a wildcard address (see "What runs" above):

   ```yaml
   driver_opts:
     com.docker.network.bridge.inhibit_ipv4: "true"
   ```

   It applies only when the network is created, so a network made without it
   must be removed and recreated.
2. `systemctl --user enable --now ae-omnigent-host-container@<name>`.
3. `deploy/host-container.sh <name> login`, and send that user the link.

Each user gets their own network to the server, their own proxy and their own
volume, so users cannot reach each other's hosts either.

### Known gaps

- **No bubblewrap inside the container** where the machine restricts
  unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`,
  Ubuntu's default): `bwrap` fails in a container under Docker's default
  AppArmor profile, and the host AppArmor profile `bwrap` does not help (it
  transitions every child to `unpriv_bwrap`, where the namespace is refused
  again). Claude Code native sessions do not use it and work; an agent whose
  `os_env` takes the default `linux_bwrap` sandbox, and `web_fetch`'s
  researcher, fail with a bwrap error. The fix needs root once (an AppArmor
  profile for the container that allows `userns`, and a seccomp profile that
  allows `unshare`).
- **Another user's busy sessions hold every deploy.** The auto-update's busy
  count is every session on the server, so a deploy waits for theirs too (up to
  the usual two hours).
- **An access gate in front of the public origin** (an identity-aware proxy, say)
  has to admit the new user too, or neither the sign-in link nor OmniGent works
  for them.
- The Claude session hook prints `http://omnigent:8000/c/<session>` as the
  session's address inside the terminal; the user's browser address is
  `https://<public origin>/c/<session>`.

## Logs and status

```bash
cat ~/.omnigent-ae-auto-update/last
journalctl --user -u ae-omnigent-auto-update -n 50 --no-pager
sudo systemctl status ae-omnigent.service
sudo deploy/compose.sh ps
sudo deploy/compose.sh logs -f omnigent
systemctl --user status ae-omnigent-host.service
journalctl --user -u ae-omnigent-host.service -f
journalctl -u ae-omnigent-dump.service -n 20 --no-pager
systemctl list-timers ae-omnigent-dump.timer
```

## Backups

`ae-omnigent-dump.timer` writes a consistent `pg_dump` at 02:40 local into
`/secure/omnigent/backups` and keeps seven. Copy `/secure/omnigent/backups` and
`/secure/omnigent/data` off the machine with whatever file backup you run,
scheduled after the dump. Do not back up `/secure/omnigent/postgres` itself: a
file-level copy of a running Postgres is not restorable, which is why the dump
exists. Restore: the header of `deploy/backup-omnigent.sh`.

## What still needs a person

- Everything above that says `sudo`, the TLS front, the first sign-in or the
  Create admin form, `omnigent login`, and the first tag.
- Filling the two secrets files, and the backup job that copies the dumps off the
  machine.
- The agents' MCP server finds the OmniGent server through the `server` key
  `omnigent login` writes to `~/.omnigent/config.yaml`, then the single login in
  `~/.omnigent/auth_tokens.json`; upstream launches a stdio MCP server with the
  MCP SDK's default environment (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`,
  `USER`), so `OMNIGENT_SERVER_URL` does not reach it. Otherwise it answers every
  tool call with text naming both files and the fix. After step 9, one scheduled
  run of the steward whose first tool call returns the capacity rather than that
  text proves it.
