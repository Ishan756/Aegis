# Deploying to EC2

Aegis deploys `examples/sample_app` to a configured EC2 instance using Docker
over SSH: the same PLAN → EXECUTE → VERIFY workflow as a local deployment, with
the Docker daemon reached on the instance instead of on this machine.

- Endpoint: `POST /api/deployment/ec2` (JSON, or `?format=markdown` for a
  human-readable `text/plain` response)
- Target: `AEGIS_EC2__*` configuration — never a request field
- History: every run is recorded with its target, plan, execution and
  verification, like any other deployment

## How it works

```
POST /api/deployment/ec2
        │
        ▼
  pre-flight ─── configuration and key file checked BEFORE any history row exists
        │
        ▼
  connect ────── ssh uname -sr          (argument list; no shell, ever)
        │
        ▼
  prepare ────── ssh docker version     (present? running? usable?)
        │         refusal without approve=true stops the run as a refused step
        ▼
  scoped server ─ docker-ec2 started with DOCKER_HOST=ssh://user@host
        │
        ▼
  PLAN ───────── tasks qualified docker-ec2.* ; policy unchanged
        ▼
  EXECUTE ────── build, run, health — refused without approve=true, audited
        ▼
  VERIFY ────── 7 checks through the instance's address, plus:
        │         loopback probe from inside the instance (ssh curl)
        │         CloudWatch CPU datapoint (aws.cloudwatch_get_metrics)
        ▼
  END ────────── history row carries the target
```

Three transports, each used for what it is good at:

| Transport | Responsibility |
| --------- | -------------- |
| SSH (`app/services/ssh.py`) | reachability, Docker readiness, loopback probe |
| Scoped Docker MCP (`docker-ec2`) | build, run, inspect — all Docker work |
| AWS MCP (`aws`) | CloudWatch CPU evidence |

The scoped server is opened per run and closed afterwards. The persistent local
`docker` server is never repointed: `docker-ec2.build_image` and
`docker.build_image` are different calls to different daemons, and the policy
sees both by qualified name.

## Prerequisites (the manual part)

Nothing in this list is done by Aegis without saying so, and two of the three
cannot be done from here at all.

1. **Key file.** Put `aegis-dev-key.pem` somewhere only you can read and point
   `AEGIS_EC2__SSH_KEY_FILE` at it. It is never committed, never logged and
   never sent to a tool — only copied into a per-run `0700` directory.

   ```bash
   chmod 600 ~/.ssh/aegis-dev-key.pem
   ```

2. **Security group.** Inbound TCP/22 from this machine, and inbound TCP/8080
   (or your `AEGIS_EC2__HOST_PORT`) from wherever health probes run. If the
   inside probe answers 200 and the public probe does not, the verification
   response says exactly this.

3. **Docker on the instance.** Either install it yourself:

   ```bash
   ssh -i ~/.ssh/aegis-dev-key.pem ec2-user@<host> \
     'sudo dnf install -y docker && sudo systemctl enable --now docker'
   ```

   or let the run do it with `approve: true` (see below).

Then flip the switch:

```bash
AEGIS_EC2__ENABLED=true
```

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `AEGIS_EC2__ENABLED` | `false` | Master switch. Off means the endpoint refuses. |
| `AEGIS_EC2__INSTANCE_ID` | — | Instance ID, recorded on every run. |
| `AEGIS_EC2__REGION` | `AEGIS_AWS__REGION` | Region for CloudWatch evidence. |
| `AEGIS_EC2__HOST` | — | Address for SSH, Docker-over-SSH and probes. |
| `AEGIS_EC2__SSH_USER` | `ec2-user` | Login user. |
| `AEGIS_EC2__SSH_KEY_FILE` | — | Private key path on this machine. |
| `AEGIS_EC2__SSH_TIMEOUT_SECONDS` | `30` | Per-command SSH budget. |
| `AEGIS_EC2__HOST_PORT` | `8080` | Published host port (probe target). |
| `AEGIS_EC2__CONTAINER_PORT` | `8000` | Container port. |
| `AEGIS_EC2__HEALTH_PATH` | `/healthz` | Health endpoint path. |
| `AEGIS_EC2__INSTALL_DOCKER` | `true` | Permit an approval-gated install. |

Also required, once, for any build:

```bash
AEGIS_REPOSITORY_ROOT=..            # resolves against backend/ as the README runs it
AEGIS_DOCKER__CONTEXT_ROOT=..      # build contexts must stay inside this root
```

The scoped server receives `AEGIS_DOCKER__CONTEXT_ROOT` and
`AEGIS_DOCKER__PROBE_HOSTS=<host>` explicitly, so the remote build boundary and
the one probe host it may reach do not depend on the environment it inherited.

## Usage

```bash
# Dry run: reach the instance, check Docker, produce the plan. Calls no tool.
curl -s 'localhost:8000/api/deployment/ec2?format=markdown' \
  -H 'content-type: application/json' \
  -d '{"repository_path":"examples/sample_app","dry_run":true}'

# Real run: build and start need approve=true.
curl -s 'localhost:8000/api/deployment/ec2?format=markdown' \
  -H 'content-type: application/json' \
  -d '{"repository_path":"examples/sample_app","approve":true}'
```

`image`, `container_name`, `ports` and `health_path` default from
`AEGIS_EC2__*` and can be overridden per request. The target cannot.

### What approval covers

| Action | Gate |
| ------ | ---- |
| `docker-ec2.build_image`, `docker-ec2.start_container` | MCP policy: refused without `approve: true` |
| Install / start Docker on the instance, add user to `docker` group | The request's `approve` flag directly — these are SSH commands, so they never see the MCP policy |
| `docker-ec2.stop_container` (destructive) | MCP policy: refused without explicit approval |
| Everything read-only (status, logs, probes, CloudWatch) | No approval needed |

A refusal is a *result*: the run ends with a `refused` step and a stop reason
that says what needed approving. Nothing is skipped silently.

## Reading the result

`succeeded` is true only when verification returned SUCCESS. Everything else —
dry run, refused preparation, stopped execution, failed or warning verification
— is false, and the reasons are in the fields next to it:

| Field | What it tells you |
| ----- | ----------------- |
| `steps` | connect and prepare outcomes, with durations |
| `plan` / `plan_explanation` | the task list, all qualified `docker-ec2.*` |
| `execution` | the audit trail: every action, approval and error |
| `verification` | the seven checks, evidence and verdict |
| `notes` | caveats, including the security-group diagnostic when it applies |
| `stop_reason` | why the run stopped, when it did |

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `EC2 deployment is not configured` (500) | `AEGIS_EC2__ENABLED` is false or a required value is missing. |
| `The configured SSH key file does not exist` (422) | Wrong `AEGIS_EC2__SSH_KEY_FILE`. |
| `the target was unreachable` | Port 22 closed, wrong host/user/key, or the instance is down. Nothing was attempted; the history row records it. |
| `Docker is not installed on the target` + refused | Re-run with `"approve": true`, or install Docker manually. |
| Verification fails but `notes` mentions the instance answered itself | Open the container port in the security group. |
| Build fails with "outside the permitted root" | `AEGIS_DOCKER__CONTEXT_ROOT` does not contain the repository path. |

## Known limitations

- **One configured target per deployment.** The instance is configuration, not
  input; multi-target fan-out is not built.
- **No bastion / ProxyJump.** `SSHSession` connects directly; a private
  instance behind a jump host is not reachable yet.
- **Failure investigation still reads the local daemon.** The investigation
  agent probes `docker.*`; for an EC2 failure its container evidence is
  unavailable and the incident report says so rather than guessing.
- **Self-healing is off by default** and, when on, runs its fixes through the
  scoped server like any other deployment.
