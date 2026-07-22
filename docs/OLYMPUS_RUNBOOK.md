# Olympus — x86 run host runbook (HIVE-339)

Native x86_64 host for the multi-run SWE-bench experiment. Retires the flaky
qemu-on-Apple-Silicon path (SWE-bench images are x86-only; under emulation the
agent stack died non-deterministically — exit 137 OOM / 132 SIGILL truncating
solves). Everything below is co-located on olympus so the persistent hive lives
next to the runner on local disk.

## The box

| | |
|---|---|
| Host | `olympus` → `10.0.102.6` (LAN; **not** via Tailscale) |
| OS / arch | Ubuntu 22.04, kernel 6.8, **x86_64** |
| CPU / RAM | Intel i9-9900K, **16 threads**, 31 GiB RAM (+8 GiB swap) |
| Docker | 29.3.0, server `linux/amd64`; `nader` in the `docker` group (no sudo) |
| Disk `/` | 455 GB LVM ext4 — **docker root lives here**, keep headroom |
| Disk `/mnt/data` | 901 GB spare ext4 (root-owned; `sudo chown` a subdir for big artifacts) |

Containers run **natively** (`docker run … hello-world` reports `(amd64)`), no
qemu shim. `sudo` requires a password (not passwordless).

### SSH discipline (important)

Command-as-argument only — never a bare interactive session:

```sh
ssh olympus 'uname -m && nproc'                 # correct
ssh olympus 'bash -s' <<'EOF'                    # multi-line: heredoc + bash -s
cd ~/neohive-agent-bench && python3 -m unittest discover -s tests -p 'test_*.py'
EOF
```

`ssh olympus && cmd` is WRONG — it runs `cmd` on the Mac after an interactive login.

### Disk hygiene

`/` fills with docker images + build cache. Reclaim the safe, regenerable part first:

```sh
ssh olympus 'docker builder prune -f'     # build cache (tens of GB; touches nothing else)
ssh olympus 'docker system df'            # see images / containers / cache / volumes
```

Do **not** blanket-`docker system prune -a` — the box has other users' images
(`oncall-web`, cached `swebench/sweb.eval.x86_64.*`, `ghcr.io/neohiveai/neohive:*`)
and stopped containers that are not ours. The django SWE-bench eval images are
already pulled, so the smoke test needs no fresh multi-GB pull.

## Co-located NeoHive (dev mode — no license, dev auth)

The compounding harness targets a **local** NeoHive (`http://127.0.0.1:3577`, dev
auth, data dir on local disk) so HIVE-334 snapshots are byte-exact FS copies. On
olympus this is run from MemVec source via docker compose:

- Source: `~/MemVec` on branch **`main`** (the version `neohive_rest.py` targets —
  it has the gateway-level `GET /api/hives` + `X-HiveMind-Id` routes).
- `docker compose up --build -d` builds the compose **`build` target** (devDeps +
  `tsx`, source-mounted). Because the source mount keeps `build-info.ts` with
  `IS_DEV_BUILD = true`, `NEOHIVE_LICENSE_SKIP=true` bypasses the Keygen gate and
  `AUTH0_ENABLED=false` gives dev auth — **no license, no PAT**. (The *production*
  image bakes `IS_DEV_BUILD=false`, so it would need a real license — don't use it here.)
- Port is bound to **loopback** (`127.0.0.1:3577:3577`); a no-auth dev instance
  must never be LAN-exposed.

`~/MemVec/.env` (dev config):

```
MEMVEC_ENV=development
PORT=3577
MEMVEC_STORAGE=local
NEOHIVE_LICENSE_SKIP=true
AUTH0_ENABLED=false
MEMVEC_TLS=false
```

Start / verify:

```sh
# build needs the private @logilica npm token (see Prerequisites)
ssh olympus 'cd ~/MemVec && LOGILICA_PKG_TOKEN=$(cat ~/.memvec_pkg_token) \
  nohup docker compose up --build -d >build.log 2>&1 </dev/null & disown'
ssh olympus 'curl -sS http://127.0.0.1:3577/health'          # {"status":"ok",...}
ssh olympus 'cd ~/neohive-agent-bench && NEOHIVE_BASE=http://127.0.0.1:3577 \
  python3 neohive_rest.py projects'                          # dev auth, no PAT
```

Notes:
- The container shows **`(unhealthy)`** — cosmetic: the compose healthcheck probes
  `https://…/health` but dev serves plain `http`.
- The compose `build` target does **not** include the Rust embedder, so
  `memory_store` / `memory_recall` / indexing need the embedder wired in
  (list_hives does not). Build the x86 binary once and bind-mount it:
  ```sh
  ssh olympus 'cd ~/MemVec && LOGILICA_PKG_TOKEN=$(cat ~/.memvec_pkg_token) \
    docker build --target rust-embedder-builder -f server/Dockerfile -t nh-embedder .'
  # then docker cp /build/target/release/neohive-embedder out and mount it into the
  # dev container at /app/neohive-embedder (MEMVEC_EMBEDDER_PATH), and restart.
  ```

## Runner (neohive-agent-bench)

- Lives at `~/neohive-agent-bench`, kept at current `main`. **olympus has no GitHub
  credentials for this repo**, so update it with a git bundle pushed from the Mac:
  ```sh
  # on the Mac:
  git -C ~/Logilica/neohive-agent-bench fetch origin
  git -C ~/Logilica/neohive-agent-bench bundle create /tmp/nab.bundle origin/main
  scp /tmp/nab.bundle olympus:~/nab.bundle
  # on olympus:
  ssh olympus 'cd ~/neohive-agent-bench && git fetch ~/nab.bundle refs/remotes/origin/main && git reset --hard FETCH_HEAD'
  ```
- 62 unit tests (stdlib only, no venv). Run from the repo root **without `-t`**
  (the tests import repo-root modules like `from cl_metrics`):
  ```sh
  ssh olympus 'cd ~/neohive-agent-bench && python3 -m unittest discover -s tests -p "test_*.py"'
  # -> Ran 62 tests ... OK
  ```
- The SWE-bench smoke / full run also needs a venv with `swebench` + `datasets`
  and the pinned opencode + node binaries (`./fetch_opencode.sh`, `./fetch_node.sh`).

## Detached-run pattern (never hold a run on a live SSH pipe)

**Rule:** launch the run *on olympus*, detached, logging to a file; disconnect;
poll. Do **not** orchestrate the rounds loop step-by-step over SSH from the Mac,
and do not keep a long run alive only for as long as an SSH pipe stays open.

### Launch — nohup

```sh
ssh olympus 'mkdir -p ~/runs; cd ~/neohive-agent-bench && \
  OPENROUTER_API_KEY=$(cat ~/.openrouter_key) \
  NEOHIVE_HOST_BASE=http://127.0.0.1:3577 \
  NEOHIVE_BASE=http://host.docker.internal:3577 \
  NEOHIVE_PROJECT=<career_project_id> \
  nohup python3 run_rounds.py --repo django/django --timeline django_timeline.json \
    --mode treated --model z-ai/glm-5.2 --round-size 2 \
    > ~/runs/django-treated.log 2>&1 </dev/null & disown; \
  echo "launched PID $!"'
```

### Launch — tmux (attachable later)

```sh
ssh olympus 'tmux new-session -d -s bench "cd ~/neohive-agent-bench && \
  OPENROUTER_API_KEY=$(cat ~/.openrouter_key) python3 run_rounds.py … \
  2>&1 | tee ~/runs/django-treated.log"'
```

### Monitor — from the Mac, non-interactively

```sh
ssh olympus 'tail -n 40 ~/runs/django-treated.log'        # snapshot
ssh olympus 'tail -f  ~/runs/django-treated.log'          # follow (Ctrl-C detaches; run keeps going)
ssh olympus 'pgrep -af run_rounds.py || echo "run finished"'   # status poll
ssh olympus 'tmux capture-pane -pt bench | tail -40'      # tmux snapshot without attaching
ssh olympus 'docker ps --format "{{.Names}} {{.Status}}"' # per-instance eval containers
```

`tail -f` is a read-only follow and is fine to Ctrl-C at any time — the run is
owned by nohup/tmux on olympus, not by the SSH session. This same pattern is how
the NeoHive image itself is (re)built on the box: `nohup docker compose up --build
-d >build.log 2>&1 & disown`, then poll `build.log`.

## Prerequisites still to provision (credentials only)

Both are secrets the operator holds; place them on olympus (mode 600):

1. **`~/.memvec_pkg_token`** — the `@logilica` GitHub-Packages read token (it's the
   `//npm.pkg.github.com/:_authToken=` line in the Mac's `~/.npmrc`). Required to
   `pnpm install` MemVec `main` (which depends on the private `@logilica/logging`).
   Without it the dev-image build 401s.
2. **`~/.openrouter_key`** — the OpenRouter API key. Required for the agent models
   (`moonshotai/kimi-k2.7`, `z-ai/glm-5.2`, `minimax/minimax-m3`,
   `anthropic/claude-sonnet-5`) and the fixed GLM-4.6 rewriter/reflect helper, i.e.
   for both the per-slug completion checks and any opencode run.
