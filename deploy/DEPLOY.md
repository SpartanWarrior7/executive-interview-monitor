# Deploying to the DigitalOcean droplet

The primary production workflow is the scheduled email digest:

```
systemd timer ──> run.py --email ──> Microsoft Graph ──> recipient inbox
```

The website is now an optional companion for ad hoc searches. When enabled, its
target is **https://interviews.example.org**, password-protected, with the
app itself bound to loopback so the reverse proxy is the only public entrance.

```
browser ──HTTPS──> proxy (TLS + auth) ──HTTP──> 127.0.0.1:8765 (serve.py)
```

The app has no login of its own. Authentication is entirely the proxy's job,
which is deliberate: HTTP Basic over TLS is well-understood and battle-tested,
and hand-rolled auth guarding live API credentials is not worth the risk.

For an email-only deployment, complete sections 2, 3, and 8 — plus 9 if the
recipient should be able to change the tracked list by replying. The DNS, web
service, proxy, and browser verification sections are optional.

---

## 1. DNS (do this first — propagation takes time)

Add an **A record** pointing at the droplet's public IPv4:

| Type | Hostname    | Value            | TTL  |
| ---- | ----------- | ---------------- | ---- |
| A    | `interviews` | *droplet IP* | 3600 |

If example.org uses DigitalOcean's nameservers, do this under
**Networking → Domains**. Otherwise add it at your registrar.

Verify before requesting a certificate — Let's Encrypt will fail if DNS hasn't
caught up:

```bash
dig +short interviews.example.org
```

## 2. Get the code onto the droplet

The repo is private, so give the droplet a **read-only deploy key**. Unlike an
account token, it grants access to this one repository and nothing else, and it
cannot push.

All of this runs **on the droplet**. Generate a named key — do not use
`/root/.ssh/id_ed25519`, which may already belong to another project here:

```bash
sudo ssh-keygen -t ed25519 -f /root/.ssh/interview-monitor-deploy -N "" -C "interview-monitor deploy key"
```

Print the public half:

```bash
sudo cat /root/.ssh/interview-monitor-deploy.pub
```

Paste that into GitHub → the repo → **Settings → Deploy keys → Add deploy key**.
Leave *Allow write access* **unchecked**.

Tell SSH which key to use for this repo, so `git pull` works later without
extra flags:

```bash
printf 'Host github-interview\n  HostName github.com\n  User git\n  IdentityFile /root/.ssh/interview-monitor-deploy\n  IdentitiesOnly yes\n' | sudo tee -a /root/.ssh/config
```

Trust GitHub's host key up front so the clone does not stop to ask:

```bash
sudo ssh-keyscan github.com | sudo tee -a /root/.ssh/known_hosts
```

Clone (note the `github-interview` alias, not `github.com`):

```bash
sudo git clone git@github-interview:YOUR-GITHUB-USER/interview-monitor.git /opt/interview-monitor
```

If a previous attempt left files there, clear it first:
`sudo rm -rf /opt/interview-monitor`.

Root owns the deploy key, so root does the pulling — but step 3 hands the tree
to the `interview` user, and git refuses to operate on a repo owned by someone
else. Tell it this one is expected, once:

```bash
sudo git config --global --add safe.directory /opt/interview-monitor
```

Without it every `git pull` fails with *"detected dubious ownership"*.

> Fallback without a deploy key: `scp -r C:\Interview-Monitor
> root@DROPLET_IP:/opt/interview-monitor` from Windows. It works, but you lose
> `git pull` updates and must delete `state/`, `.claude/` and `__pycache__`
> afterwards.

This droplet runs Ubuntu 24.04 with Python 3.12.3 — no runtime work needed.

Port 8765 must be free. This droplet already runs at least one other project,
so check before assuming:

```bash
sudo ss -lptn 'sport = :8765'
```

Empty output means it's free. If something answers, pick another port and
change it in both the systemd unit and the Caddy block.

## 3. Service account and credentials

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin interview
```

```bash
sudo chown -R interview:interview /opt/interview-monitor
```

Copy `deploy/interview-search.env.example` to `/etc/interview-search.env`, fill
in the source credentials plus the Microsoft Graph and email settings, then
lock it down:

```bash
sudo chown root:root /etc/interview-search.env && sudo chmod 600 /etc/interview-search.env
```

systemd reads that file as root before dropping privileges, so the service user
never needs access to it.

## 4. Run it under systemd

```bash
sudo cp /opt/interview-monitor/deploy/interview-search.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now interview-search
```

Confirm it's listening on loopback only:

```bash
sudo ss -lptn 'sport = :8765'
```

You want `127.0.0.1:8765`. If it says `0.0.0.0:8765`, the app is publicly
exposed — stop it and fix the `ExecStart` line before going further.

## 5a. Proxy — Caddy (this droplet)

Caddy is **already installed and serving other sites** on this droplet, so it
needs no install and its config file must not be replaced. You are adding one
site block to a file that is already doing a job.

Look at what is there now:

```bash
sudo cat /etc/caddy/Caddyfile
```

Back it up before editing:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F)
```

Generate a password hash — never put a plaintext password in the config:

```bash
caddy hash-password --plaintext 'choose-a-strong-team-password'
```

Append the block from `deploy/caddy-interview-search.snippet` to the **end** of
`/etc/caddy/Caddyfile`, pasting the hash in place of the placeholder. If the
file begins with a global options block in braces, leave that first.

> **Edit the file in an editor — do not append the hash with `echo` or a
> heredoc.** A bcrypt hash looks like `$2a$14$…`, and the shell expands `$2`,
> `$14` and so on to empty strings, silently writing a truncated hash. You then
> get a login prompt that rejects every password. `nano` pastes it literally.

Check the config before it goes live — this catches hostname collisions and
syntax errors without disturbing the running sites:

```bash
caddy validate --config /etc/caddy/Caddyfile
```

Only if that passes:

```bash
sudo systemctl reload caddy
```

Reload is graceful: existing sites keep serving, and a bad config is rejected
rather than applied. Caddy obtains and renews the certificate automatically.

If something goes wrong, restore and reload:

```bash
sudo cp /etc/caddy/Caddyfile.bak.$(date +%F) /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

## 5b. Proxy — nginx (only if nginx serves the main site instead)

Do **not** install Caddy alongside nginx; both want port 443.

```bash
sudo apt install -y apache2-utils && sudo htpasswd -c /etc/nginx/.htpasswd-interviews team
```

Copy `deploy/nginx-interview-search.conf.example` to
`/etc/nginx/sites-available/interview-search`, then:

```bash
sudo ln -s /etc/nginx/sites-available/interview-search /etc/nginx/sites-enabled/ && sudo nginx -t && sudo systemctl reload nginx
```

Then issue the certificate (this rewrites the vhost for TLS and adds a
redirect):

```bash
sudo certbot --nginx -d interviews.example.org
```

## 6. Firewall

Caddy already serves 80/443 on this droplet, so those are open and nothing
needs changing. The rule that matters is the one you must *not* add: **never
open 8765.** The app is reachable only through the proxy, which is what makes
the password meaningful.

Confirm 8765 is not exposed from outside:

```bash
sudo ufw status
```

If you use DigitalOcean's cloud firewall, check there too — it applies before
the droplet's own rules.

## 7. Verify

```bash
curl -sI https://interviews.example.org | head -1
```

Expect `401 Unauthorized` — that means auth is working. With credentials:

```bash
curl -su team:PASSWORD https://interviews.example.org/api/platforms
```

All five platforms should report `"available": true`. If Spotify or YouTube say
false, the credentials didn't reach the service — check
`/etc/interview-search.env` and `sudo systemctl restart interview-search`.

---

## 8. The scheduled email digest (primary)

The digest is the primary production interface. It shares the same code,
credentials file, and seen-database with the optional search UI. Installing it
requires one service unit and one timer.

Fill in `EMAIL_FROM`, `EMAIL_TO`, and the `GRAPH_*` lines in
`/etc/interview-search.env` first. SMTP remains an optional fallback:

```bash
sudo nano /etc/interview-search.env
```

For least privilege, grant the app Exchange Online's **Application Mail.Send**
role through Application RBAC, scoped to the `EMAIL_FROM` mailbox. Remove any
organization-wide Graph `Mail.Send` application grant from Entra after the
scoped assignment tests successfully; the two authorization systems are
additive, and an unscoped Entra grant defeats the mailbox restriction.

Install and enable the timer:

```bash
sudo cp /opt/interview-monitor/deploy/interview-digest.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now interview-digest.timer
```

**Baseline before the first real run**, or the first digest reports every
interview already on the internet:

```bash
sudo -u interview /usr/bin/python3 /opt/interview-monitor/run.py --seed
```

Then test delivery immediately rather than waiting for the schedule:

```bash
sudo systemctl start interview-digest.service && sudo journalctl -u interview-digest -n 30 --no-pager
```

Check when it will next fire:

```bash
systemctl list-timers interview-digest.timer --no-pager
```

The schedule is `*-*-* 07:30:00 America/New_York` — every day, including
weekends — set in the `[Timer]` section of the unit. To change it, edit the unit
in the repo, deploy it, and `systemctl daemon-reload`; editing only the copy in
`/etc/systemd/system/` works until the next `cp` from `deploy/` silently reverts
it. Check any new expression before trusting it:

```bash
systemd-analyze calendar '*-*-* 07:30:00 America/New_York' --iterations=3
```

`Mon..Fri 07:30 America/New_York` is the weekdays-only form, if the weekend
emails turn out to be noise.

---

## 9. Changing the tracked list by replying to the digest

This lets someone add or remove executives by replying to the digest, with no
server access and no JSON editing. It is optional: skip this section and the
digest simply never mentions the feature.

### The watchlist has to move out of git first

`executives.json` is tracked in git, and deploys are `git pull` as root. If the
mail handler wrote that file, every future pull would conflict. So the live list
lives in `state/`, which is gitignored and already the only writable path in the
units:

```bash
sudo -u interview cp /opt/interview-monitor/executives.json /opt/interview-monitor/state/executives.json
```

Then set `WATCHLIST_PATH=/opt/interview-monitor/state/executives.json` in
`/etc/interview-search.env`. From then on the checked-in `executives.json` is
just the starting template — editing it changes nothing on the server.

### Grant the app permission to read the mailbox

Reading needs `Mail.ReadWrite` — `Mail.Read` is not enough, because marking a
message as handled is a write. Grant it the same way section 8 grants
`Mail.Send`: through **Exchange Online Application RBAC**, scoped to this one
mailbox. Nothing is added in Entra. An Entra application permission would apply
to every mailbox in the tenant, and because the two authorities are additive, an
unscoped grant there silently defeats any scoping done here.

Requires the **Organization Management** Exchange role group. Connect first with
`Connect-ExchangeOnline`.

Register the app as an Exchange service principal, if section 8 has not already:

```powershell
New-ServicePrincipal -AppId <application-id> -ObjectId <enterprise-app-object-id> -DisplayName "Interview Monitor"
```

> Take both IDs from **Entra ID → Enterprise applications**, *not* App
> registrations — that page shows a different Object ID and the assignment will
> point at nothing.

Define the scope. Confirm the filter matches exactly one mailbox before you
grant anything against it:

```powershell
New-ManagementScope -Name "Interview Monitor mailbox" -RecipientRestrictionFilter "Alias -eq 'alerts'"
```

```powershell
Get-Recipient -RecipientPreviewFilter "Alias -eq 'alerts'" | Format-Table Name,PrimarySmtpAddress
```

Assign the two roles the app actually uses:

```powershell
New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.ReadWrite" -CustomResourceScope "Interview Monitor mailbox"
```

```powershell
New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.Send" -CustomResourceScope "Interview Monitor mailbox"
```

Two assignments rather than the single `Application Mail Full Access` role, which
is the union of both: the effective permissions are identical, but this way the
reading half can be revoked without stopping the digest.

Verify — the target mailbox must be `InScope True`, and any other mailbox must
come back `False`:

```powershell
Test-ServicePrincipalAuthorization -Identity <application-id> -Resource alerts | Format-Table
```

```powershell
Test-ServicePrincipalAuthorization -Identity <application-id> -Resource <someone-else> | Format-Table
```

> Permission changes take 30 minutes to 2 hours to leave Exchange's cache, so a
> fresh grant may not work immediately. `Test-ServicePrincipalAuthorization`
> bypasses that cache — trust it over a failing `--check-mail`, and wait rather
> than granting something broader.

### Configure and install

Add to `/etc/interview-search.env` (see `interview-search.env.example`):

```
COMMAND_MAILBOX=alerts@example.org
COMMAND_SENDERS=you@example.com
WATCHLIST_PATH=/opt/interview-monitor/state/executives.json
```

`COMMAND_SENDERS` is the allowlist of people who may give instructions. On top
of it, mail must pass DMARC according to Exchange's own `Authentication-Results`
header — a shared mailbox accepts internet mail, and `From:` is trivially
forged. Mail failing either check is marked read and dropped with a line in the
journal, and is never replied to.

One optional variable is not in that block: `COMMAND_FOLDER` chooses which mail
folder is read, and defaults to `inbox`. Set it only if an Outlook rule on the
shared mailbox files digest replies into a subfolder — the reader looks in one
folder and nowhere else, so a rule that moves replies out of the inbox otherwise
makes every instruction disappear with no error anywhere.

Dry-run it first, against a real unread test message in the mailbox:

```bash
sudo -u interview /usr/bin/python3 /opt/interview-monitor/run.py --check-mail --dry-run --verbose
```

That proves the token, the read permission, and — the part that actually decides
whether this works — that Graph returns `internetMessageHeaders` containing
`dmarc=pass`. If authorization fails there, fix it before installing the timer;
without those headers every message is refused by design.

```bash
sudo cp /opt/interview-monitor/deploy/interview-commands.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now interview-commands.timer
```

```bash
sudo systemctl start interview-commands.service && sudo journalctl -u interview-commands -n 40 --no-pager
```

The check runs every 15 minutes (`OnCalendar=*:0/15`). Confirm with
`systemctl list-timers interview-commands.timer --no-pager`.

### What the user does

They reply to the digest, one instruction per line. The digest itself teaches
the three commands in its footer, so nothing has to be explained in advance:

```
add Tim Cook, Apple CEO
remove Jensen Huang
list
```

All of these are understood, which is the point — there is no syntax to learn
and no reason to trim the quoted digest underneath:

```
add Tim Cook, Apple CEO           name, plus optional company and title
add Tim Cook to tracker
remove Jensen Huang
remove Jensen Huang from tracker
Can you remove Jensen Huang
take Jensen Huang off the list
list
help
```

The subject line is read as an instruction as well, but only when it is not a
`RE:`/`FW:` reply subject — otherwise every reply to *"Interview digest: 2 new
interviews"* would be parsed as one.

Company and title are optional. Where two tracked people share a name, a bare
`remove Michael Brown` is refused and both are named rather than one guessed at;
`remove Michael Brown, Initech` picks one.

**The tracker holds people, not companies.** `add Acme company to tracker` is
refused, with a reply asking for a named person. Nothing here crawls a company —
the company on an entry is only a disambiguator for matching a person's name in
a headline — so there is no companies list to add `Acme` to, and pretending
otherwise would track nobody while looking like it worked. The instruction that
does what they meant is `add Jane Doe, Acme CEO`.

They get a reply in the same thread listing what changed and everyone currently
tracked, so a misread instruction is visible at once. Bad edits are recoverable:
each write leaves the previous file as `state/executives.json.bak`, and a
candidate that would not load is refused before it can replace anything.

### "I replied and nothing happened"

Rejected mail is never answered — answering a forged `From:` would make this
mailbox a backscatter source — so silence is the expected symptom of a
rejection, not a sign the job is down. Work down this list:

1. **Is the sender address in `COMMAND_SENDERS`?** It defaults to `EMAIL_TO`, so
   a reply sent from someone's second address is refused.
2. **Did the reply pass DMARC?** Exchange's `Authentication-Results` header must
   show `dmarc=pass`, or `spf=pass` and `dkim=pass` together. Missing headers are
   rejected on purpose: this is a shared mailbox that accepts internet mail, and
   `From:` is forgeable.
3. **Is the message still unread?** Anything a human opens in the shared mailbox
   before the timer fires is skipped.
4. **Is it in the folder `COMMAND_FOLDER` names?** Default `inbox`. Check for an
   Outlook rule that moved it.
5. **Read the journal** — it is the only place a rejection is reported:

   ```bash
   sudo journalctl -u interview-commands -n 40 --no-pager
   ```

6. **Allow ~16 minutes.** The timer is `OnCalendar=*:0/15`, plus up to 60s of
   randomised delay. Nothing in normal operation takes hours: the 30-minute-to-
   2-hour wait above is Exchange's RBAC grant cache, which applies once when a
   permission is first granted and never to a reply.

---

## 10. Updating the droplet from Windows

Once the droplet is running, updates are one command from the repository root on
a machine that can read the repo and SSH to the droplet as root. The droplet's
address and public URL are kept out of the repository; set them once per machine:

```bash
setx INTERVIEW_MONITOR_SSH_TARGET "root@<droplet-ip>"
setx INTERVIEW_MONITOR_PUBLIC_URL "https://interviews.example.org/"
```

Then, from a new terminal:

```bash
.\droplet-update.cmd
```

It deploys **GitHub main**, never your working tree — an uncommitted local
experiment cannot reach the droplet even by accident. In order, it:

1. clones main to a temporary directory, byte-compiles it, and runs the 300-odd
   offline tests **locally**, so a broken release never reaches the server;
2. checks no digest or mailbox job is mid-run, fetches that exact commit on the
   droplet, and refuses to continue if main moved in the meantime;
3. stages the commit in `/tmp` and re-compiles and re-tests it there with the
   droplet's own Python;
4. backs up the current commit SHA and both copies of the watchlist to
   `/var/backups/interview-monitor/release-<stamp>-<sha>/`;
5. checks the clone out at the commit, re-applies `interview` ownership, proves
   the release imports and reaches its database as the service user, restarts
   `interview-search`, and checks `127.0.0.1:8765` and the public URL — the
   latter is skipped automatically when the hostname has no DNS record, since
   an email-only deployment never creates one;
6. **rolls the clone back to the previous commit and restarts the service** if
   any of step 5 fails.

Deliberately untouched: `/etc/interview-search.env`, the installed systemd
units, and everything under `state/` — so credentials, the schedule, the
seen-database and the live watchlist all survive a deployment. A repo unit file
that differs from the installed copy is reported as `UNIT_DRIFT` rather than
deployed; install it yourself with the `cp` from section 4 or 8 when you want it.

| Flag | Use |
| --- | --- |
| *(none)* | Deploy main if the droplet is not already on it |
| `-Force` | Redeploy and restart even when the commit is already live |
| `-DiscardRemoteChanges` | Proceed when the droplet has edited tracked files, discarding them |
| `-SkipTests` | Compile only, skipping the test suite on both ends |
| `-PublicUrl ""` | Never check the public URL at all (it already skips itself when the hostname does not resolve) |
| `-SshTarget`, `-RemoteAppDirectory`, `-ServiceUser` | Point it at a different droplet or layout |

What the output means:

| Line | Meaning |
| --- | --- |
| `ALREADY_CURRENT <sha>` | Nothing to do; `-Force` restarts anyway |
| `DEPLOYED <sha>` / `BACKUP <path>` | Success, and where the rollback material is |
| `DEPLOYMENT_FAILED_ROLLING_BACK` | The release was pulled; the droplet is back on the old commit |
| `LOCAL_CHANGES_PRESENT` | Someone edited files on the droplet — see `-DiscardRemoteChanges` |
| `SCHEDULED_JOB_RUNNING` | A crawl or mailbox check is running; re-run in a minute |
| `GITHUB_MAIN_MOVED` | Somebody pushed mid-deploy; re-run to validate the new tip |
| `PUBLIC_URL_UNRESOLVED` | No DNS record for the site here; the release deployed and the check was skipped |
| `PUBLIC_URL_UNREACHABLE` | The hostname resolves but nothing answered — rolled back |
| `PUBLIC_HTTP_UNEXPECTED` | The proxy answered with something other than 401/200 — rolled back |
| `WATCHLIST_PRESERVED` | The live list is the tracked `executives.json`, and was kept |
| `UNIT_DRIFT <unit>` | The repo's copy of that unit differs from the installed one |
| `TIMER_INACTIVE <unit>` | The digest or command timer is not running — no email will arrive |

The automatic rollback covers a failed restart or health check, not a release
that starts cleanly and misbehaves later. To undo one of those by hand, take the
SHA from the backup directory and put the clone back on it:

```bash
sudo git -C /opt/interview-monitor checkout --force -B main $(cat /var/backups/interview-monitor/release-<stamp>-<sha>/previous-commit) && sudo chown -R interview:interview /opt/interview-monitor && sudo systemctl restart interview-search
```

## Operating it

| Task | Command |
| --- | --- |
| Logs | `sudo journalctl -u interview-search -f` |
| Restart | `sudo systemctl restart interview-search` |
| Deploy an update | `.\droplet-update.cmd` from Windows — see section 10 |
| Deploy by hand | `cd /opt/interview-monitor && sudo git pull && sudo chown -R interview:interview . && sudo systemctl restart interview-search` |
| Change the password | Re-run the hash step, reload Caddy |

After a manual `git pull`, re-apply ownership if new files appeared:
`sudo chown -R interview:interview /opt/interview-monitor`. The updater in
section 10 does that itself.

## Things to know

- **The YouTube quota is now shared by the whole team.** 100 units per search
  against 10,000/day means roughly 100 searches per day across everyone. Heavy
  use will exhaust it; it cannot be topped up. If that bites, drop `youtube`
  from the default toggles and let people opt in.
- **Basic auth is one shared password.** There's no per-user identity and no
  audit trail of who searched what. That's usually fine for a small team; if
  you need real accounts, that's an authentication layer worth doing properly
  rather than extending this.
- **This is a prototype server.** Python's `http.server` is fine behind a proxy
  for a handful of colleagues. It is single-process with a thread per request —
  if this becomes something the whole firm leans on, it wants a real WSGI/ASGI
  server in front.
- **The live watchlist is outside git.** Once `WATCHLIST_PATH` points into
  `state/`, `git pull` no longer touches the list and the tracked
  `executives.json` is only a template. Back up `state/` if the list matters —
  it is not in the repository. `state/executives.json.bak` holds the version
  before the last email-driven change.
- **Anyone on the allowlist can change what the firm monitors.** The email
  channel is as trustworthy as `COMMAND_SENDERS` plus DMARC. It cannot change
  recipients, settings, or anything outside the watchlist, so the worst case is
  a wrong list, not disclosure — but keep the allowlist short.
- **The scheduled monitor is separate.** This deploys only the search UI. If you
  want the daily "new interviews" report running on the droplet instead of your
  laptop, that's a second systemd service plus a timer — ask and I'll add it.
