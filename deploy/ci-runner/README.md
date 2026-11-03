# CI box runners

Five GitHub Actions self-hosted runners on the owner's Linux box, so the test jobs of
`.github/workflows/ci.yml` stop costing billed minutes. CI is the organisation's biggest
Actions cost (about 60 runs a day, 27 billed minutes each, all on `ubuntu-latest`).
When the box is off or a job loses its runner, the run is rerun on GitHub-hosted runners
by itself, so a power outage costs time, never a red merge gate.

Nothing here changes CI until the repository variable `CI_BOX_RUNNER` is `on`. Unset
(the state when this was written), every job runs on `ubuntu-latest` exactly as before.

## How a run is routed

- Box-eligible jobs: `patches`, `upstream-smoke`, `python`, `web`, `rules`. Their
  `runs-on` is `["self-hosted","ae-box"]` when the routing expression in `ci.yml` is true
  and `ubuntu-latest` otherwise. `image` (needs docker) and `leaks` always stay hosted.
- The expression is true only on the first attempt of a run (`github.run_attempt == '1'`)
  and only when the author's login (PR author, else the actor) is in the repository variable
  `CI_BOX_AUTHORS` (the owner's two GitHub logins, comma separated, no spaces), and either
  the dispatch input `runner` is `box`, or `CI_BOX_RUNNER` is `on`, the input is not
  `hosted`, and the branch is not `dependabot/*`. The logins live in a variable because the
  leak scanner's denylist blocks them in committed text. Unset, nobody qualifies, so it
  fails safe to hosted; setting `CI_BOX_AUTHORS` alone changes nothing while
  `CI_BOX_RUNNER` is unset. Anyone else's code never reaches the box, not even by a forced dispatch.
- The runner user has no docker access on purpose. Tests marked `docker`
  (`tests/deploy/conftest.py`) are deselected in the `python` job on the box and run instead
  as extra steps of the `image` job (always hosted, and already a required check, so a red
  docker test still blocks a merge). With routing off those steps are skipped.
- Job "Box watchdog" (hosted, skipped when routing is off) polls every 30 s. If the box
  jobs are queued and no job in the repository is running on an `ae-box-*` runner (the box
  looks off), its step "Cancel: box jobs never started" cancels the run after 2 minutes. If
  the box is alive but busy it waits up to 30 minutes. API errors are retried and never fail
  the job; status unknown for 2 minutes also cancels. Without it, a job queued for a dead
  box waits 24 hours.
- `.github/workflows/ci-box-fallback.yml` fires when a first-attempt CI run ends cancelled
  or failed. It reruns the run once, only when the watchdog cancelled it or a failed box job
  shows a lost-runner annotation (never for an ordinary test failure), and only while the
  run's commit is still the latest on its PR or on `main`. Attempt 2 routes to GitHub by
  itself. Its decision and reason are in the job summary either way.
- A manual dispatch gets its own concurrency group (`github.run_id`), so several dispatches
  of one commit no longer cancel each other (the drills need this).

## Kill switch

Set repository variable `CI_BOX_RUNNER` to `off`, or delete it (Settings, Secrets and
variables, Actions, Variables; or `gh variable set CI_BOX_RUNNER -R analyticendeavors/omnigent-ae --body off`).
The next run starts on GitHub. A run already queued for the box: cancel it and rerun.
For a long box outage, switch it off: while it is `on` and the box is down, every run first
waits about 2 minutes for the watchdog before the hosted rerun.

## Install

Run once from the owner's Windows PowerShell (needs `gh` signed in as an admin of the
repository on the box, and this repository cloned at `~/repos/omnigent-ae` on it). Replace
`<user>` and `<box>` with the box login and its address. The token goes through a mode-600
file, not the command line: the script reads it, deletes it, and `rm` removes it if the
script never ran.

```powershell
ssh -t <user>@<box> "umask 077 && gh api -X POST repos/analyticendeavors/omnigent-ae/actions/runners/registration-token --jq .token > ~/ci-runner-token && sudo bash ~/repos/omnigent-ae/deploy/ci-runner/install.sh ~/ci-runner-token; rm -f ~/ci-runner-token"
```

(`config.sh --token` still shows the token in the process list for a few seconds. It is a
registration-only token that expires in about an hour.)

`install.sh` creates the `ghrunner` user (no sudo, not in `docker`, not in the owner's
group) and refuses to continue if another directory under `/home` can be read or searched
by others (fix with `chmod o-rwx`). It apt-installs `shellcheck` and `webp`, downloads
actions/runner 2.338.2 (sha256 checked), registers `ae-box-1` to `ae-box-5` with labels
`self-hosted,ae-box`, writes a job-started hook, and installs `ghrunner.slice` (MemoryMax
32G, CPUQuota 1200%) and `ghrunner@.service`, enabled for 1 to 5 so they start at boot.
Each runner has its own `HOME` (`home-N`), so one job's cleanup hook never touches a
neighbour's tools; the hook empties that `~/.local/bin` and runner temp files older than a
minute. The unit also sets `ProtectHome=tmpfs` with only the `ghrunner` user's home bound back,
`ProtectSystem=full`, `PrivateTmp` and `NoNewPrivileges`, so a job sees that home,
`/usr`, `/opt` and its own `/tmp`, not the owner's home.

Run it again any time: it checks each runner's version and registration, reconfigures or
upgrades only the ones that are missing or out of date, and restarts only the units it
changed; a unit running a job, unchanged, is left alone. A rerun needs no token file unless
a runner must be registered.

Then set the allowed authors (once; a comma list of the two logins, no spaces). The drills
need this first; on its own it changes nothing while `CI_BOX_RUNNER` is unset:
`gh variable set CI_BOX_AUTHORS -R analyticendeavors/omnigent-ae --body "<login1>,<login2>"`.

Check: `ssh <user>@<box> "systemctl list-units 'ghrunner@*'"`, and
`gh api repos/analyticendeavors/omnigent-ae/actions/runners --jq '.runners[] | [.name,.status]'`
shows five `online`.

Remove: `ssh -t <user>@<box> "umask 077 && gh api -X POST repos/analyticendeavors/omnigent-ae/actions/runners/remove-token --jq .token > ~/ci-runner-token && sudo bash ~/repos/omnigent-ae/deploy/ci-runner/uninstall.sh ~/ci-runner-token; rm -f ~/ci-runner-token"`
(stops the units, deregisters the runners, removes the units; keeps the user and packages).

## Drills (Friday, in order; all with the box installed and `CI_BOX_RUNNER` still unset)

Dispatches use `-f runner=box`, which routes to the box without `CI_BOX_RUNNER` (but only for a login in `CI_BOX_AUTHORS`). Use
`--ref main` once this is merged. Watch a run with `gh run watch <id> -R analyticendeavors/omnigent-ae`;
see where its jobs ran with
`gh run view <id> -R analyticendeavors/omnigent-ae --json jobs --jq '.jobs[] | [.name,.runnerName,.conclusion]'`.

1. **Parity.** Ten box runs, three of them at once, all green.
   ```bash
   for i in 1 2 3; do gh workflow run ci.yml -R analyticendeavors/omnigent-ae --ref main -f runner=box; done
   # when those finish, seven more, one after another:
   for i in 1 2 3 4 5 6 7; do gh workflow run ci.yml -R analyticendeavors/omnigent-ae --ref main -f runner=box; sleep 600; done
   gh run list -R analyticendeavors/omnigent-ae --workflow ci.yml --limit 10 --json conclusion --jq 'map(.conclusion) | unique'
   ```
   Pass: `["success"]`, the five eligible jobs show `ae-box-N` runner names, and the
   "Docker-backed deploy tests" steps of the image job ran and passed in each.
2. **Box off.** The box is down, a run still ends green on GitHub.
   ```bash
   ssh -t <user>@<box> "sudo systemctl stop 'ghrunner@*'"
   gh workflow run ci.yml -R analyticendeavors/omnigent-ae --ref main -f runner=box
   ```
   Pass: the watchdog cancels attempt 1 after about 2 minutes, "CI box fallback" reruns it, attempt 2
   passes on `ubuntu-latest` (`gh run view <id> --json attempt,conclusion`). For a run that
   lands on GitHub at once, dispatch with `-f runner=hosted`.
3. **Race.** Same as drill 2 but read the evidence: units stopped before the jobs start.
   Pass: the box jobs sit `queued`, the watchdog job's step "Cancel: box jobs never
   started" runs after about 2 minutes and its summary says the fallback will rerun on GitHub,
   the run ends `cancelled`, then `gh run list -R analyticendeavors/omnigent-ae --workflow ci-box-fallback.yml --limit 1`
   shows a job whose summary says "Rerunning run", and attempt 2 passes.
   Start the box again afterwards: `ssh -t <user>@<box> "sudo systemctl start 'ghrunner@*'"`.
4. **Mid-job, and a real failure is not rerun.**
   ```bash
   gh workflow run ci.yml -R analyticendeavors/omnigent-ae --ref main -f runner=box
   # when "AE package lint and tests" shows in_progress:
   ssh -t <user>@<box> "sudo systemctl stop 'ghrunner@*'"
   ```
   Pass: the job fails with a lost-runner annotation, the fallback reruns the run, attempt 2
   passes. Start the units again, then prove the negative on a throwaway branch:
   ```bash
   git switch -c drill/failing-test origin/main
   printf 'def test_drill_fails():\n    assert False\n' > tests/deploy/test_drill_fail.py
   git add tests/deploy/test_drill_fail.py && git commit -m "drill: a failing test" && git push -u origin drill/failing-test
   gh workflow run ci.yml -R analyticendeavors/omnigent-ae --ref drill/failing-test -f runner=box
   ```
   Pass: the run is red, the fallback job's summary says "No rerun", and the run has one
   attempt. Delete the branch (`git push origin --delete drill/failing-test`).
5. **Reboot** (only when the owner is physically home, in case the box does not come back).
   `ssh -t <user>@<box> "sudo reboot"`, wait a few minutes, then run the
   `gh api .../actions/runners` check from Install. Pass: all five `online` with no manual step.
6. **Seven-day watch.** With `CI_BOX_AUTHORS` set, `gh variable set CI_BOX_RUNNER -R analyticendeavors/omnigent-ae --body on`.
   Each day: `gh run list -R analyticendeavors/omnigent-ae --workflow ci.yml --limit 100 --json conclusion,attempt,createdAt`
   and `gh run list -R analyticendeavors/omnigent-ae --workflow ci-box-fallback.yml --limit 20`.
   Pass after seven days: no failure caused by the box, every fallback explained by a real box
   outage, and the Actions billed minutes (Settings, Billing) well below the old 60 x 27 a day.
   Any doubt: kill switch.
7. **Release after a rerun.** A rerun started with the workflow token may not fire
   `workflow_run`, so a push to `main` whose attempt 1 was rerun might never reach Release.
   Stop the box (`sudo systemctl stop 'ghrunner@*'`), push a trivial commit to `main` with
   `CI_BOX_RUNNER` on, wait for attempt 2 to pass, and confirm the Release workflow starts.
   If it does not, run it by dispatch: `gh workflow run release.yml -R analyticendeavors/omnigent-ae --ref main`.
   The fallback's job summary says this whenever it reruns a push run.
