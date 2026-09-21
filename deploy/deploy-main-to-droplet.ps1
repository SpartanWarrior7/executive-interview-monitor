<#
.SYNOPSIS
Safely deploy the current GitHub main release of Interview Monitor to the droplet.

.DESCRIPTION
Run this from a trusted Windows development machine that can both read the
private GitHub repository and SSH to the droplet. It deliberately does not use
the working tree: every run clones GitHub main to a temporary directory, checks
that release compiles and passes the offline test suite locally, and only then
touches the droplet.

The droplet holds a git clone of this repository (see DEPLOY.md section 2), so
the release is delivered by fetching the exact validated commit there rather
than by uploading files — anything else would leave the clone permanently
dirty and break `git pull`. Before the switch, the commit is staged in /tmp and
re-validated with the droplet's own Python; after it, the web service is
restarted and health-checked, and any failure rolls the clone back to the commit
that was live before.

Not touched, by design: /etc/interview-search.env, the installed systemd units,
and everything under state/ — so the droplet's credentials, schedule and live
watchlist survive every deployment. Repo unit files that differ from the
installed copies are reported at the end rather than deployed.

.EXAMPLE
.\droplet-update.cmd
.EXAMPLE
.\droplet-update.cmd -Force
#>
[CmdletBinding()]
param(
    # Kept out of the repository: set these once per machine, e.g.
    #   setx INTERVIEW_MONITOR_SSH_TARGET "root@203.0.113.10"
    #   setx INTERVIEW_MONITOR_PUBLIC_URL "https://interviews.example.org/"
    [string]$SshTarget = $env:INTERVIEW_MONITOR_SSH_TARGET,
    [string]$RemoteAppDirectory = "/opt/interview-monitor",
    [string]$ServiceUser = "interview",
    [string]$WebServiceName = "interview-search",
    # Each name is both a oneshot service and its timer, e.g. interview-digest.
    [string[]]$ScheduledUnits = @("interview-digest", "interview-commands"),
    [string]$EnvironmentFile = "/etc/interview-search.env",
    [string]$LocalUrl = "http://127.0.0.1:8765/",
    [string]$PublicUrl = $env:INTERVIEW_MONITOR_PUBLIC_URL,
    # 401 is the healthy answer through the password-protected proxy; 200 is
    # correct for a deployment without basic auth. Pass "" to skip the check.
    [string[]]$AcceptablePublicStatus = @("401", "200"),
    [string]$BackupRoot = "/var/backups/interview-monitor",
    [switch]$SkipTests,
    [switch]$DiscardRemoteChanges,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SshTarget)) {
    throw "No droplet to deploy to. Pass -SshTarget root@<droplet-ip>, or set it once with: setx INTERVIEW_MONITOR_SSH_TARGET root@<droplet-ip>"
}
if ($null -eq $PublicUrl) { $PublicUrl = "" }

function Invoke-Native {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-RemoteScript {
    param([Parameter(Mandatory)] [string]$Script)

    # Do not pipe the script into ssh. Windows PowerShell encodes a native
    # command's stdin with [Console]::OutputEncoding and terminates it with
    # CRLF, so on a UTF-8 console bash receives a byte-order mark ahead of the
    # first line and a stray carriage return after the last one: `set -euo
    # pipefail` becomes an unknown command and a trailing `done` stops closing
    # its loop. Hand the bytes over as base64 instead, which no console setting
    # can reinterpret, and keep ssh off stdin with -n.
    $normalizedScript = ($Script -replace "`r`n", "`n").TrimEnd("`n") + "`n"
    $encodedScript = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($normalizedScript))
    & ssh -n @script:SshOptions $script:SshTarget "printf %s '$encodedScript' | base64 -d | bash -s"
    if ($LASTEXITCODE -ne 0) {
        throw "Remote deployment command failed ($LASTEXITCODE)."
    }
}

$SshOptions = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")

# Everything the app needs at runtime. The droplet checks out the whole tree,
# so this list is a release sanity check rather than an upload manifest: a main
# that lost one of these would start the service and then fail on first use.
$requiredFiles = @(
    "run.py"
    "serve.py"
    "interview_monitor/__init__.py"
    "interview_monitor/cli.py"
    "interview_monitor/web.py"
    "web/index.html"
    "web/app.css"
    "web/app.js"
)
$requiredDirectories = @("interview_monitor", "web", "tests")

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# Interpolate rather than calling .Trim() on the result: a git that failed
# outright returns nothing, and a method call on that would throw before the
# check below could report what is actually wrong.
$originUrl = "$(& git -C $repositoryRoot remote get-url origin)".Trim()
if ($LASTEXITCODE -ne 0 -or -not $originUrl) {
    throw "The repository at $repositoryRoot has no origin remote to clone the release from."
}

$python = (Get-Command python -ErrorAction Stop).Source
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("interview-monitor-deploy-" + [guid]::NewGuid().ToString("N"))
$temporaryRepository = Join-Path $temporaryRoot "source"

try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    Write-Host "Fetching GitHub main..."
    Invoke-Native git @("clone", "--quiet", "--depth", "1", "--branch", "main", $originUrl, $temporaryRepository)

    $commit = "$(& git -C $temporaryRepository rev-parse HEAD)".Trim().ToLowerInvariant()
    if ($commit -notmatch "^[0-9a-f]{40}$") {
        throw "GitHub main did not resolve to a valid commit SHA."
    }
    $shortCommit = $commit.Substring(0, 12)

    foreach ($file in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $temporaryRepository $file) -PathType Leaf)) {
            throw "GitHub main is missing required application file: $file"
        }
    }
    foreach ($directory in $requiredDirectories) {
        if (-not (Test-Path -LiteralPath (Join-Path $temporaryRepository $directory) -PathType Container)) {
            throw "GitHub main is missing required application directory: $directory"
        }
    }

    Write-Host "Checking release syntax ($shortCommit)..."
    Push-Location $temporaryRepository
    try {
        Invoke-Native $python @("-m", "compileall", "-q", "run.py", "serve.py", "interview_monitor", "tests")
        if (-not $SkipTests) {
            # The suite is offline and needs no credentials, so a failure here
            # is a real regression rather than a missing key on this machine.
            Write-Host "Running the offline test suite..."
            Invoke-Native $python @("-m", "unittest", "discover", "-s", "tests")
        }
    }
    finally {
        Pop-Location
    }

    $deploymentTimestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $stageDirectory = "/tmp/interview-monitor-stage-$shortCommit-$deploymentTimestamp"
    $forceValue = if ($Force) { "1" } else { "0" }
    $discardValue = if ($DiscardRemoteChanges) { "1" } else { "0" }
    $runTestsValue = if ($SkipTests) { "0" } else { "1" }
    $scheduledUnitList = $ScheduledUnits -join " "
    $publicStatusList = ($AcceptablePublicStatus | Where-Object { $_ }) -join " "

    $validationScript = (@'
set -euo pipefail
app='{APP}'
commit='{COMMIT}'
stage='{STAGE}'
run_tests='{RUN_TESTS}'
scheduled='{SCHEDULED}'

test -d "$app/.git" || { echo "NOT_A_GIT_CLONE $app - see DEPLOY.md section 2" >&2; exit 1; }

# A crawl or a mailbox check that is mid-flight has this tree open. Swapping
# the code underneath it buys nothing; the timer fires again in minutes.
for unit in $scheduled; do
    if systemctl is-active --quiet "$unit.service"; then
        echo "SCHEDULED_JOB_RUNNING $unit.service - try again in a minute" >&2
        exit 1
    fi
done

git -C "$app" fetch --quiet origin main
fetched=$(git -C "$app" rev-parse FETCH_HEAD)
if test "$fetched" != "$commit"; then
    echo "GITHUB_MAIN_MOVED validated $commit but the droplet fetched $fetched - re-run" >&2
    exit 1
fi

# Validate the exact commit with the droplet's Python before anything live
# points at it. `git archive` costs no second clone and no credentials.
rm -rf "$stage"
install -d -m 0755 "$stage"
git -C "$app" archive "$commit" | tar -x -C "$stage"
cd "$stage"
/usr/bin/python3 -m compileall -q run.py serve.py interview_monitor tests
if test "$run_tests" -eq 1; then
    /usr/bin/python3 -m unittest discover -s tests
fi
echo "STAGED_RELEASE_OK $(git -C "$app" rev-parse HEAD) -> $commit"
'@).Replace('{APP}', $RemoteAppDirectory).Replace('{COMMIT}', $commit).Replace('{STAGE}', $stageDirectory).Replace('{RUN_TESTS}', $runTestsValue).Replace('{SCHEDULED}', $scheduledUnitList)
    Write-Host "Validating $shortCommit on the droplet..."
    Invoke-RemoteScript $validationScript

    $deploymentScript = (@'
set -euo pipefail
app='{APP}'
stage='{STAGE}'
commit='{COMMIT}'
short_commit='{SHORT_COMMIT}'
force='{FORCE}'
discard='{DISCARD}'
owner='{OWNER}'
service='{SERVICE}'
scheduled='{SCHEDULED}'
env_file='{ENV_FILE}'
local_url='{LOCAL_URL}'
public_url='{PUBLIC_URL}'
public_ok='{PUBLIC_OK}'
backup_root='{BACKUP_ROOT}'

previous=$(git -C "$app" rev-parse HEAD)
if test "$previous" = "$commit" && test "$force" -ne 1; then
    rm -rf "$stage"
    echo "ALREADY_CURRENT $commit"
    exit 0
fi

# Which file is the live watchlist? Mirrors watchlist.resolve_path: the env
# variable wins, then state/executives.json, then the tracked copy. When the
# live list *is* the tracked copy, checking out main would replace somebody's
# executives with the repository template, so it is put back afterwards.
watchlist=''
if test -f "$env_file"; then
    watchlist=$(sed -n 's/^[[:space:]]*WATCHLIST_PATH=//p' "$env_file" | tail -n 1 | tr -d '\r')
fi
preserve_watchlist=0
if test -z "$watchlist"; then
    test -f "$app/state/executives.json" || preserve_watchlist=1
elif test "$watchlist" = "$app/executives.json"; then
    preserve_watchlist=1
fi

# Local edits on the droplet are somebody's hotfix until proven otherwise, and
# the checkout below would discard them silently. Two exceptions: untracked
# files (state/, reports/, __pycache__/ - none of our business), and the tracked
# executives.json when it is the live list, because then the mail handler is
# supposed to have rewritten it and it is restored rather than discarded.
dirty=$(git -C "$app" status --porcelain --untracked-files=no |
    if test "$preserve_watchlist" -eq 1; then grep -v '^.. executives\.json$'; else cat; fi || true)
if test -n "$dirty" && test "$discard" -ne 1; then
    echo "LOCAL_CHANGES_PRESENT on the droplet - commit them, or re-run with -DiscardRemoteChanges:" >&2
    printf '%s\n' "$dirty" >&2
    exit 1
fi

backup="$backup_root/release-$(date -u +%Y%m%dT%H%M%SZ)-$short_commit"
install -d -o root -g root -m 0700 "$backup_root"
install -d -o root -g root -m 0700 "$backup"
printf '%s\n' "$previous" > "$backup/previous-commit"
git -C "$app" status --porcelain > "$backup/git-status.txt" || true
test ! -f "$app/executives.json" || cp -p "$app/executives.json" "$backup/executives.json"
test ! -f "$app/state/executives.json" || cp -p "$app/state/executives.json" "$backup/state-executives.json"

rollback() {
    echo "DEPLOYMENT_FAILED_ROLLING_BACK $backup" >&2
    git -C "$app" checkout --quiet --force -B main "$previous" || true
    if test "$preserve_watchlist" -eq 1 && test -f "$backup/executives.json"; then
        cp -p "$backup/executives.json" "$app/executives.json" || true
    fi
    chown -R "$owner:$owner" "$app" || true
    if systemctl cat "$service.service" >/dev/null 2>&1; then
        systemctl restart "$service" || true
    fi
}
trap 'rollback; exit 1' ERR

# An explicit `exit` does not fire an ERR trap, so a check that decides for
# itself that the release is bad has to roll back by hand. Everything else -
# a failing command under `set -e` - is the trap's job.
fail() {
    echo "$*" >&2
    rollback
    exit 1
}

git -C "$app" checkout --quiet --force -B main "$commit"
if test "$preserve_watchlist" -eq 1 && test -f "$backup/executives.json"; then
    cp -p "$backup/executives.json" "$app/executives.json"
fi
chown -R "$owner:$owner" "$app"

# The oneshot jobs need no restart - each run starts a fresh interpreter - so
# this proves the release imports and reaches its database as the service user.
runuser -u "$owner" -- sh -c "cd '$app' && exec /usr/bin/python3 run.py --stats" > /dev/null

web_installed=0
if systemctl cat "$service.service" >/dev/null 2>&1; then
    web_installed=1
    systemctl restart "$service"
    sleep 5
    systemctl is-active --quiet "$service"
    local_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$local_url")
    test "$local_status" = 200
    if test -n "$public_url" && test -n "$public_ok"; then
        # A hostname with no DNS record says something about this droplet's
        # setup, not about the release: an email-only deployment skips section 1
        # and never creates the record, and rolling a good release back over
        # that would be absurd. Every other curl failure - refused, timed out,
        # TLS - is the public entrance being broken, and does roll back.
        curl_exit=0
        public_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$public_url") || curl_exit=$?
        if test "$curl_exit" -eq 6; then
            public_status=''
            public_note="PUBLIC_URL_UNRESOLVED $public_url has no DNS record here, so the check was skipped"
        elif test "$curl_exit" -ne 0; then
            fail "PUBLIC_URL_UNREACHABLE $public_url (curl exit $curl_exit)"
        else
            case " $public_ok " in
                *" $public_status "*) ;;
                *) fail "PUBLIC_HTTP_UNEXPECTED $public_status (wanted one of: $public_ok)" ;;
            esac
        fi
    fi
fi

trap - ERR
rm -rf "$stage"
echo "DEPLOYED $commit"
echo "PREVIOUS $previous"
echo "BACKUP $backup"
if test "$web_installed" -eq 1; then
    echo "LOCAL_HTTP $local_status"
    test -z "${public_status:-}" || echo "PUBLIC_HTTP $public_status"
    test -z "${public_note:-}" || echo "$public_note"
else
    echo "WEB_SERVICE_NOT_INSTALLED email-only deployment, no HTTP check"
fi
test "$preserve_watchlist" -eq 0 || echo "WATCHLIST_PRESERVED $app/executives.json is the live list and was kept"

# Timers are untouched by a code deploy, but a stopped one means no digest, and
# this is the moment somebody is looking.
for unit in $scheduled; do
    if systemctl cat "$unit.timer" >/dev/null 2>&1; then
        if systemctl is-active --quiet "$unit.timer"; then
            echo "TIMER_ACTIVE $unit.timer"
        else
            echo "TIMER_INACTIVE $unit.timer - it will not fire until started" >&2
        fi
    fi
done

# The units in deploy/ are not deployed on purpose: the installed copies carry
# this droplet's schedule and paths. Drift is worth naming, not fixing here.
for unit in "$service.service" $(for u in $scheduled; do printf '%s.service %s.timer ' "$u" "$u"; done); do
    if test -f "/etc/systemd/system/$unit" && test -f "$app/deploy/$unit"; then
        cmp -s "$app/deploy/$unit" "/etc/systemd/system/$unit" ||
            echo "UNIT_DRIFT $unit - the repo copy differs from the installed one, which was left alone"
    fi
done
'@).Replace('{APP}', $RemoteAppDirectory).Replace('{STAGE}', $stageDirectory).Replace('{COMMIT}', $commit).Replace('{SHORT_COMMIT}', $shortCommit).Replace('{FORCE}', $forceValue).Replace('{DISCARD}', $discardValue).Replace('{OWNER}', $ServiceUser).Replace('{SERVICE}', $WebServiceName).Replace('{SCHEDULED}', $scheduledUnitList).Replace('{ENV_FILE}', $EnvironmentFile).Replace('{LOCAL_URL}', $LocalUrl).Replace('{PUBLIC_URL}', $PublicUrl).Replace('{PUBLIC_OK}', $publicStatusList).Replace('{BACKUP_ROOT}', $BackupRoot)
    Write-Host "Deploying $shortCommit with automatic rollback..."
    Invoke-RemoteScript $deploymentScript
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
