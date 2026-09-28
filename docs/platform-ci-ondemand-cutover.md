# platform-ci on-demand cutover runbook (task 816)

This runbook moves `ci.sparkswarm.com` from the always-on CI droplet (`do-1gb-runner-main-…`,
id `551995541`, `167.172.224.151`) to the dispatcher on the platform droplet (`159.65.241.127`).
After the move, CI boxes are created from a snapshot per workload and destroyed when idle. For
the design, see `ci/README.md`.

The owner runs every step. Nothing here was run during task 816 implementation.

**Precondition:** no Slopticus (or other registered repo) merge or deploy is in flight. Check
`ssh root@167.172.224.151 'tail -n 20 /srv/platform-ci/logs/worker.log; pgrep -af bin/platform'`.

Run commands from the workspace root. Never echo a token. Pipe it with `--stdin`.

## 1. DigitalOcean tokens (owner, DO control panel → API → Generate New Token → Custom Scopes)

**A. `platform-ci-dispatcher`** lives on the platform droplet only. Give it these scopes:

| Scope | Why |
|---|---|
| `droplet:create` | `POST /v2/droplets` (create the box from the snapshot) |
| `droplet:read` | `GET /v2/droplets?tag_name=…` (reconcile) |
| `droplet:delete` | `DELETE /v2/droplets/{id}` (idle teardown, extras, dead boxes) |
| `image:read` | `GET /v2/images?private=true` to find the newest `platform-ci-snap-*`, and to create from a private image (DO lists it as an associated scope of `droplet:create`) |
| `tag:read` | required by DO for the `tag_name` filter on the droplet list |
| `tag:create` | needed to tag a droplet at create time (associated scope of `droplet:create`) |
| `ssh_key:read` | attach `PLATFORM_CI_SSH_KEYS` at create (associated scope of `droplet:create`; drop it if you leave that var empty) |

It has no `droplet:update`, `image:create/delete`, `snapshot:*`, `actions:*` or `project:*`
scopes. Choose no expiry, or 1 year with a calendar reminder.

**B. `platform-ci-snapshot`** is an operator token for `bin/platform ci snapshot` only. It is
never placed on a droplet. Give it these scopes:

| Scope | Why |
|---|---|
| `droplet:read` | find the tagged box and poll `GET /v2/droplets/{id}/actions/{id}` |
| `droplet:update` + `image:create` | `POST /v2/droplets/{id}/actions {"type":"snapshot"}` (DO lists both for the snapshot action) |
| `snapshot:read` | `GET /v2/snapshots?resource_type=droplet` (prune list) |
| `snapshot:delete` | `DELETE /v2/snapshots/{id}` (prune beyond `--keep`) |
| `tag:read` | tag filter on the droplet list |

Sources: DO API scopes reference, the `droplet:create` associated scopes, and the
Droplet Actions reference (the snapshot action requires `droplet:update` and `image:create`).
DO scopes cannot be limited to a tag, so token A **can delete any droplet in the account**,
including `platform` and `platform-db`. The dispatcher only deletes droplets that are tagged
`platform-ci-ondemand` **and** named `platform-ci-ondemand-*` (see the tests). Treat token A as a
crown-jewel secret.

## 2. Spark Swarm secrets

`spark-swarm/production` is exported whole into `/root/platform-infra/.env`. Only the values the
dispatcher needs go there.

```sh
pbpaste | ./bin/platform secrets put spark-swarm production PLATFORM_CI_DO_TOKEN --stdin          # token A
openssl rand -hex 32 | ./bin/platform secrets put spark-swarm production PLATFORM_CI_RUNNER_TOKEN --stdin
openssl rand -hex 32 | ./bin/platform secrets put spark-swarm production PLATFORM_CI_ADMIN_TOKEN --stdin
ssh root@167.172.224.151 "sed -n 's/^PLATFORM_CI_WEBHOOK_SECRET=//p' /etc/platform-ci/env" \
  | ./bin/platform secrets put spark-swarm production PLATFORM_CI_WEBHOOK_SECRET --stdin
ssh root@167.172.224.151 "sed -n 's/^GH_TOKEN=//p' /etc/platform-ci/env" \
  | ./bin/platform secrets put spark-swarm production PLATFORM_CI_GH_TOKEN --stdin
ssh root@167.172.224.151 "sed -n 's/^PLATFORM_CI_REPO_MAP=//p' /etc/platform-ci/env" \
  | ./bin/platform secrets put spark-swarm production PLATFORM_CI_REPO_MAP --stdin
```

Store token B outside the exported environment:

```sh
pbpaste | ./bin/platform secrets put spark-swarm ci-operator PLATFORM_CI_SNAPSHOT_DO_TOKEN --stdin   # token B
```

`PLATFORM_CI_SSH_KEYS` is optional. The snapshot already carries root's `authorized_keys`. If it
is set, use DO key ids (for example `46869945`). With no key set, DO may email a root password
for each new box.

## 3. Merge the PR

Merging deploys nothing. platform-infra is not registered with platform-ci, and the droplet only
changes on `prod sync infra`.

## 4. Start the dispatcher (no traffic yet)

```sh
git -C repos/platform-infra pull --ff-only
git -C repos/platform-infra diff --stat         # Rich's uncommitted compose OOM work shows here; it is what prod runs
ssh root@159.65.241.127 'cat /root/platform-infra/docker-compose.yml' | diff - repos/platform-infra/docker-compose.yml   # expect only the new platform-ci-dispatcher service + volume
ssh root@159.65.241.127 'cat /root/platform-infra/caddy/Caddyfile' | diff - repos/platform-infra/caddy/Caddyfile       # expect only the new ci.sparkswarm.com block
./bin/platform prod secrets apply spark-swarm production --yes
./bin/platform prod sync infra --yes
ssh root@159.65.241.127 'cd /root/platform-infra && docker compose up -d platform-ci-dispatcher'
./bin/platform prod logs platform-ci-dispatcher --tail 50
```

In the log, expect `platform-ci dispatcher on 0.0.0.0:8766; repos=[…]`, and no `FATAL`
or `cannot list CI droplets` lines. Do **not** reload Caddy yet.

## 5. Build the runner image from the always-on box (push mode keeps serving)

```sh
ssh root@167.172.224.151 'cd /srv/platform-ci/workspace/repos/platform-infra && git fetch -q origin && git checkout -q main && git pull -q --ff-only origin main'
{ echo "PLATFORM_CI_DISPATCHER_URL=https://ci.sparkswarm.com"; \
  printf 'PLATFORM_CI_RUNNER_TOKEN='; curl -fsS "https://sparkswarm.com/api/v1/secrets/resolve/PLATFORM_CI_RUNNER_TOKEN?project=spark-swarm&environment=production" -H "X-API-Key: $SPARK_SWARM_API_KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])'; } \
  | ssh root@167.172.224.151 'cat >> /etc/platform-ci/env && chmod 600 /etc/platform-ci/env'
ssh root@167.172.224.151 'PLATFORM_CI_MODE=runner bash /srv/platform-ci/workspace/repos/platform-infra/ci/provision.sh'
```

After this, the runner is enabled for the next boot, and `platform-ci` and `caddy` are disabled
for the next boot. Both keep running now. Wait until no job is running (see the precondition),
then run:

```sh
python3.13 ./bin/platform ci snapshot --droplet-id 551995541 --dry-run
python3.13 ./bin/platform ci snapshot --droplet-id 551995541 --yes     # live snapshot, ~19 GB; record the duration
```

## 6. Switch traffic

1. In Cloudflare (sparkswarm.com zone), change the A record for `ci.sparkswarm.com` from
   `167.172.224.151` to `159.65.241.127`, DNS only (not proxied). Wait until
   `dig +short ci.sparkswarm.com @1.1.1.1` returns the new IP.
2. Reload Caddy to pick up the block and issue the certificate:
   `ssh root@159.65.241.127 'docker compose -f /root/platform-infra/docker-compose.yml exec caddy caddy reload --config /etc/caddy/Caddyfile'`
3. Check that `curl -fsS https://ci.sparkswarm.com/healthz` prints `ok`, and that
   `python3.13 ./bin/platform ci status` prints `box: none` and `queued: 0`.
4. In each registered repo, go to Settings → Webhooks → Recent deliveries and redeliver the
   last `ping`. Expect `200 pong`. Redeliver any push or PR that failed during the DNS window.
5. Stop push mode on the old box. Do not destroy it:
   `ssh root@167.172.224.151 'systemctl stop platform-ci caddy'`

## 7. Verify end to end (acceptance 1, 2, 4)

1. **PR check:** push a trivial commit to an open PR on a registered repo. Watch
   `ci status`: the job is queued, then the box is `booting`, then `ready`, then the job is
   running, then `done`. The PR gets `platform-ci: queued`, then `running make <target>`, then
   pass or fail. **Cold start** is the time from the `queued` log line to the `leased … attempt
   1` line in `prod logs platform-ci-dispatcher`. Record it.
2. **Superseding:** push twice to that PR within a minute, while the box is busy or booting. The
   older SHA shows `success: superseded: PR #N head moved to …; not run`.
3. **Deploy:** merge a low-risk change to a deploy-on-push repo (richmiles-xyz is the smallest).
   Expect `deployed`, and `./bin/platform prod release-status richmiles-xyz` shows the new SHA.
4. **Idle teardown:** 10 minutes after the last job, run `doctl compute droplet list --tag-name
   platform-ci-ondemand`. It should be empty, and `ci status` should show `box: none`.
5. **Restart:** queue a PR check and restart the dispatcher mid-job
   (`docker compose restart platform-ci-dispatcher`). The job completes once. `ci status` shows
   one `done` row, and DO shows at most one tagged droplet.
6. Record cold start, box lifetime per job and cost on task 816. Cost is lifetime ×
   $0.0714/h for `s-4vcpu-8gb`, plus snapshot storage at $0.06/GB-month.

## 8. Decommission the always-on box (owner confirms first)

The step 5 snapshot already preserves the box. Keep it until a refreshed snapshot replaces it.
After the owner confirms:

```sh
doctl compute droplet delete 551995541
```

Then close task 784 with a reference to the 816 evidence.

## Rollback (any time before step 8)

```sh
# DNS: ci.sparkswarm.com A → 167.172.224.151
ssh root@167.172.224.151 'systemctl enable --now caddy platform-ci && systemctl disable platform-ci-runner'
ssh root@159.65.241.127 'cd /root/platform-infra && docker compose stop platform-ci-dispatcher'
doctl compute droplet list --tag-name platform-ci-ondemand   # delete any leftover on-demand box by id
```

Redeliver any webhooks that failed while traffic moved.

## Proposed CLAUDE.md change (workspace root, platform-ci section)

Replace the `platform-ci droplet (167.172.224.151)` section with the following:

> ## platform-ci (on-demand)
>
> GitHub-Actions-free CI/CD. `ci.sparkswarm.com` → Caddy on the platform droplet →
> `platform-ci-dispatcher` (compose service; SQLite queue on a volume). With work queued, the
> dispatcher creates a CI droplet (`s-4vcpu-8gb`, nyc3, tag `platform-ci-ondemand`) from the newest
> `platform-ci-snap-*` snapshot. The box's runner pulls jobs and runs `bin/platform check|build
> --rollout`. After 10 idle minutes the dispatcher destroys the box. Superseded PR checks and
> older queued deploys are marked `superseded` (a success status), never failed. See
> `repos/platform-infra/ci/README.md`.
>
> ```bash
> python3.13 ./bin/platform ci status                 # queue + box
> python3.13 ./bin/platform ci hold 60                # keep a box up (e.g. to refresh it)
> python3.13 ./bin/platform ci snapshot --yes         # refresh the box image (token: spark-swarm/ci-operator)
> ./bin/platform prod logs platform-ci-dispatcher --tail 100
> ```
>
> The dispatcher's DO token (`PLATFORM_CI_DO_TOKEN`, spark-swarm/production) can delete any
> droplet. Never reuse it elsewhere.
