"""Docker MCP server.

Exposes a deliberately small, non-shell interface to the Docker CLI:

============================  ========  ==========  ===========================
Tool                          Risk      Approval   Effect
============================  ========  ==========  ===========================
``docker_available``          low       no         none
``list_images``               low       no         none
``container_status``          low       no         none
``container_health``          low       no         none
``container_logs``            low       no         none
``build_image``               medium    yes        writes an image
``start_container``           medium    yes        writes and starts a container
``stop_container``            high      yes        stops a running container
============================  ========  ==========  ===========================

Safety properties, each enforced here rather than merely documented:

- **No shell, ever.** Commands are built as argument lists and run with
  ``shell=False``. There is no tool that accepts a command string, and no tool
  accepts arguments to place after the image name, so the model cannot append
  ``sh -c ...`` to a ``run`` or a ``build``.
- **Argument lists only.** A validated value can never become a flag, because a
  name starting with ``-`` is rejected before it reaches argv. Without that check
  an image called ``--privileged`` would be a privilege escalation.
- **Paths are confined.** A build context must resolve inside
  :data:`CONTEXT_ROOT`, so no path can turn the host filesystem into an image
  layer. Symlinks are resolved *before* the containment check, so a symlink out
  of the root cannot escape it.
- **Bounded capture.** Output is read incrementally and capped, and a timed-out
  command has its whole process group killed. An unbounded ``docker build`` on a
  chatty Dockerfile cannot exhaust memory or wedge the server.
- **Logs are capped.** The tail is line-limited and byte-limited before it is
  returned, with ``truncated`` set so the model is told it saw a fragment.
- **The child environment is an allowlist.** Only ``PATH``, ``HOME``, the Docker
  connection variables and ``AEGIS_DOCKER__*`` are inherited, so a Docker build
  cannot read the backend's LLM or GitHub credentials out of the environment.
- **Health comes from the image, not the model.** There is deliberately no
  ``--health-cmd`` option: that is arbitrary command execution inside a
  container, which is exactly what this server exists to avoid. A container is
  healthy only if the image defines its own ``HEALTHCHECK``.

Anticipated failures raise the SDK's :class:`ToolError`, so the caller receives an
actionable message instead of a generic "error executing tool".
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- Configuration --------------------------------------------------------
# Read once at import. The backend forwards these same variables explicitly, so a
# value that is not named in AEGIS_MCP__FORWARD_ENVIRONMENT has no effect here.

DOCKER_BINARY = os.environ.get("AEGIS_DOCKER__BINARY", "docker")

#: Builds may only use a context inside this directory. Defaults to the current
#: working directory, which is the narrowest sensible default.
CONTEXT_ROOT = Path(
    os.environ.get("AEGIS_DOCKER__CONTEXT_ROOT", os.environ.get("PWD", "."))
).expanduser().resolve()

COMMAND_TIMEOUT = _env_float("AEGIS_DOCKER__COMMAND_TIMEOUT_SECONDS", 30.0)
BUILD_TIMEOUT = _env_float("AEGIS_DOCKER__BUILD_TIMEOUT_SECONDS", 600.0)
MAX_OUTPUT_BYTES = _env_int("AEGIS_DOCKER__MAX_OUTPUT_BYTES", 64_000)
MAX_LOG_LINES = _env_int("AEGIS_DOCKER__MAX_LOG_LINES", 200)
MAX_LOG_BYTES = _env_int("AEGIS_DOCKER__MAX_LOG_BYTES", 32_000)

#: Grace period given to a container on stop, before SIGKILL.
STOP_GRACE_SECONDS = 10


server = MCPServer(
    "aegis-docker",
    instructions=(
        "Container lifecycle tools for local Docker. There is no shell tool and no "
        "way to run a command inside a container; a container runs the entrypoint "
        "its image defines. Building an image and starting a container change "
        "system state and require human approval."
    ),
)


# --- Validation -----------------------------------------------------------

#: Docker image references: an optional registry host, a path, then an optional
#: tag and digest. This is intentionally stricter than Docker's own parser.
IMAGE_REFERENCE = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9._-]*(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127})?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)

#: Container names must start alphanumeric; the remainder may also contain _ . -
CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

#: A Dockerfile path relative to the build context.
DOCKERFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

MAX_REFERENCE_LENGTH = 255


def _check_image(reference: str) -> str:
    """Validate an image reference or refuse it.

    A leading ``-`` is the important case: values reach ``argv`` as a list, so
    nothing would stop ``--privileged`` from being read as a flag by the Docker
    CLI. Rejecting it here is what makes the argv list safe.
    """
    if not reference or not reference.strip():
        raise ToolError("An image reference is required.")
    if len(reference) > MAX_REFERENCE_LENGTH:
        raise ToolError(f"Image reference exceeds {MAX_REFERENCE_LENGTH} characters.")
    if reference.startswith("-"):
        raise ToolError(
            "An image reference may not start with '-', which Docker would read as a flag."
        )
    if any(char.isspace() or ord(char) < 32 for char in reference):
        raise ToolError("An image reference may not contain whitespace or control characters.")
    if ".." in reference.split("/"):
        raise ToolError("An image reference may not contain a '..' path segment.")
    if not IMAGE_REFERENCE.match(reference):
        raise ToolError(
            f"{reference!r} is not a valid image reference. Use the form "
            "'[registry[:port]/]repository[:tag][@sha256:digest]'."
        )
    return reference


def _check_container_name(name: str) -> str:
    """Validate a container name or refuse it."""
    if not name or not name.strip():
        raise ToolError("A container name is required.")
    if name.startswith("-"):
        raise ToolError("A container name may not start with '-', which Docker reads as a flag.")
    if not CONTAINER_NAME_RE.match(name):
        raise ToolError(
            "A container name must start with a letter or digit and may then contain "
            "letters, digits, '_', '.' and '-' (128 characters maximum)."
        )
    return name


def _check_dockerfile(relative: str) -> str:
    """Validate a Dockerfile path *inside* a build context.

    A bare name only: no directories and no ``..``, so the build always reads the
    Dockerfile from the context it was given.
    """
    if not DOCKERFILE_NAME_RE.match(relative):
        raise ToolError(
            f"{relative!r} is not a valid Dockerfile name. Use a bare file name such as "
            "'Dockerfile' or 'Dockerfile.prod'."
        )
    return relative


def _check_port(spec: str) -> str:
    """Validate a ``host:container`` or ``container`` port mapping."""
    if not spec or not spec.strip():
        raise ToolError("A port mapping may not be empty.")

    parts = spec.split(":")
    if len(parts) > 2:
        raise ToolError(f"{spec!r} is not a valid port mapping; use 'host:container'.")

    for part in parts:
        if not part.isdigit():
            raise ToolError(f"{spec!r} is not a valid port mapping; ports must be integers.")
        if not 1 <= int(part) <= 65535:
            raise ToolError(f"Port {part} in {spec!r} is outside the range 1-65535.")
    return spec


def _resolve_context(relative: str) -> Path:
    """Resolve a build context and confirm it stays inside :data:`CONTEXT_ROOT`.

    This is the filesystem security boundary. ``resolve()`` collapses ``..`` and
    follows symlinks *before* the containment test, so neither a traversal
    sequence nor a symlink pointing outward can escape the root.
    """
    if not relative or not relative.strip():
        raise ToolError("A build context path is required.")

    candidate = Path(relative).expanduser()
    if not candidate.is_absolute():
        candidate = CONTEXT_ROOT / candidate

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        # RuntimeError covers symlink loops; ValueError covers embedded nulls.
        raise ToolError("The build context path could not be resolved.") from exc

    if resolved != CONTEXT_ROOT and CONTEXT_ROOT not in resolved.parents:
        raise ToolError(
            f"The build context {resolved} is outside the permitted root {CONTEXT_ROOT}. "
            "Builds may only use a directory inside it."
        )

    if not resolved.exists():
        raise ToolError(f"No build context at {resolved}.")
    if not resolved.is_dir():
        raise ToolError(f"The build context {resolved} is not a directory.")
    return resolved


def _resolve_binary() -> str:
    """Locate the Docker CLI, refusing to guess when it is ambiguous."""
    if os.sep in DOCKER_BINARY:
        path = Path(DOCKER_BINARY).expanduser()
        if not path.is_file():
            raise ToolError(f"Docker binary {DOCKER_BINARY} does not exist.")
        return str(path)

    found = shutil.which(DOCKER_BINARY)
    if not found:
        raise ToolError(
            f"The Docker CLI ({DOCKER_BINARY!r}) was not found on PATH. Install Docker, "
            "or set AEGIS_DOCKER__BINARY to its absolute path."
        )
    return found


# --- Process handling -----------------------------------------------------

#: Variables a Docker CLI legitimately needs. An allowlist, not an inheritance:
#: the backend's environment holds LLM and GitHub credentials that must never be
#: readable by a Dockerfile's build steps.
_INHERITED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CONTEXT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def _child_env() -> dict[str, str]:
    """Build the subprocess environment from an explicit allowlist."""
    env = {name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}
    env.update(
        {
            name: value
            for name, value in os.environ.items()
            if name.startswith("AEGIS_DOCKER__")
        }
    )
    return env


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of one Docker CLI invocation.

    A non-zero exit is not an exception: Docker reports ordinary failures (no such
    container, daemon down) on stderr with a message worth reading, so the result
    carries it for the caller to decide about.
    """

    exit_code: int
    success: bool
    stdout: str
    stderr: str
    truncated: bool = False


def _capture(
    process: subprocess.Popen[bytes],
    *,
    limit: int,
    deadline: float,
) -> tuple[bytes, bytes, bool]:
    """Read both pipes until EOF, the cap or the deadline.

    Both streams are drained together with :mod:`selectors`, because reading one
    to completion first would deadlock as soon as the child filled the other.
    Output past ``limit`` is discarded rather than buffered, so a chatty build
    cannot grow this process's memory.
    """
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    sizes = {"stdout": 0, "stderr": 0}
    open_streams = 2
    truncated = False

    try:
        while open_streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError

            for key, _ in selector.select(timeout=min(remaining, 0.25)):
                # read1 returns whatever has arrived instead of blocking for a
                # full buffer, so a slow trickle still makes progress.
                chunk = key.fileobj.read1(65536)  # type: ignore[union-attr]
                name = key.data
                if not chunk:
                    selector.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                if sizes[name] < limit:
                    room = limit - sizes[name]
                    kept = chunk[:room]
                    buffers[name].extend(kept)
                    sizes[name] += len(kept)
                else:
                    # Still draining so the child does not block on a full pipe,
                    # but no longer accumulating.
                    truncated = True
    finally:
        selector.close()

    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), truncated


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the command and anything it started.

    The child leads its own session, so the process *group* can be signalled and
    Docker's own helpers do not survive as orphans.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            pass

    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _run(
    args: list[str],
    *,
    timeout: float | None = None,
    limit: int | None = None,
) -> CommandResult:
    """Run the Docker CLI with ``args`` and return its captured output.

    Never raises for a non-zero exit: Docker reports ordinary failures (no such
    container, daemon down) on stderr with a message worth reading, so those are
    returned rather than discarded.
    """
    binary = _resolve_binary()
    cap = MAX_OUTPUT_BYTES if limit is None else limit
    budget = COMMAND_TIMEOUT if timeout is None else timeout
    argv = [binary, *args]

    try:
        # shell=False with an argument list: there is no string for a shell to
        # interpret, which is what makes these tools non-shell tools.
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            env=_child_env(),
            cwd=str(CONTEXT_ROOT),
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"The Docker CLI ({binary}) could not be executed.") from exc
    except OSError as exc:
        raise ToolError(f"The Docker CLI could not be started: {exc}") from exc

    deadline = time.monotonic() + budget
    timed_out = False
    try:
        stdout, stderr, truncated = _capture(process, limit=cap, deadline=deadline)
    except TimeoutError:
        timed_out = True
        _kill_tree(process)
        # Drain whatever is buffered so the pipes do not stay open, but do not
        # wait long: the process has already been killed.
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            stdout, stderr = b"", b""
        truncated = True
    else:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()) + 5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            _kill_tree(process)

    if timed_out:
        raise ToolError(
            f"'docker {' '.join(args[:2])}' exceeded its {budget:g}s timeout and was "
            "terminated. Raise AEGIS_DOCKER__BUILD_TIMEOUT_SECONDS for a long build."
        )

    exit_code = process.returncode
    return CommandResult(
        exit_code=exit_code,
        success=exit_code == 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def _require_success(result: CommandResult, action: str) -> CommandResult:
    """Turn a non-zero exit into an actionable error."""
    if result.success:
        return result
    detail = (result.stderr or result.stdout or "").strip()
    # Docker's last stderr line is the useful one; the rest is usually progress.
    lines = [line for line in detail.splitlines() if line.strip()]
    message = lines[-1] if lines else "no diagnostic output"
    if len(message) > 300:
        message = f"{message[:297]}..."
    raise ToolError(f"Docker could not {action} (exit {result.exit_code}): {message}")


# --- Tools ----------------------------------------------------------------


@server.tool(
    name="docker_available",
    title="Check Docker availability",
    description=(
        "Report whether the Docker CLI is present, whether the daemon answers, and "
        "which server version is running. Always returns a result; it never raises "
        "for an unavailable daemon, because 'is Docker usable' is the question."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def docker_available() -> dict[str, Any]:
    """Report Docker availability. Read-only."""
    try:
        binary = _resolve_binary()
    except ToolError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "context_root": str(CONTEXT_ROOT),
        }

    result = _run(["version", "--format", "{{json .}}"], timeout=15)
    if not result.success:
        return {
            "available": False,
            "reason": "The Docker CLI is installed but the daemon did not respond.",
            "detail": (result.stderr or "").strip()[:500],
            "context_root": str(CONTEXT_ROOT),
        }

    payload: dict[str, Any] = {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        pass

    server_info = payload.get("Server") or {}
    return {
        "available": bool(server_info.get("Version")),
        "client_version": (payload.get("Client") or {}).get("Version"),
        "server_version": server_info.get("Version"),
        "os": server_info.get("Os"),
        "arch": server_info.get("Arch"),
        "context_root": str(CONTEXT_ROOT),
        "notes": [
            "Build contexts are confined to context_root; a path outside it is refused.",
            "A container is healthy only if its image defines a HEALTHCHECK.",
        ],
    }


@server.tool(
    name="list_images",
    title="List images",
    description=(
        "List locally available images with id, repository, tag, size and creation "
        "date. Most recently created first."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def list_images(
    limit: Annotated[int, Field(description="Maximum images to return.", ge=1, le=200)] = 50,
    repository: Annotated[
        str, Field(description="Optional repository prefix filter, e.g. 'myapp'.")
    ] = "",
) -> dict[str, Any]:
    """List local images. Read-only."""
    filters: list[str] = ["--format", "{{json .}}"]
    if repository:
        # Same reason images are validated: this reaches argv.
        if repository.startswith("-") or any(
            char.isspace() or ord(char) < 32 for char in repository
        ):
            raise ToolError("The repository filter may not contain whitespace or start with '-'.")
        filters.extend(["--filter", f"reference={repository}*"])

    result = _require_success(_run(["images", *filters]), "list images")

    images: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        images.append(
            {
                "id": item.get("ID"),
                "repository": item.get("Repository"),
                "tag": item.get("Tag"),
                "size": item.get("Size"),
                "created": item.get("CreatedAt"),
            }
        )
        if len(images) >= limit:
            break

    return {
        "images": images,
        "count": len(images),
        "truncated": len(images) >= limit,
        "note": "A repository shown as '<none>' is an untagged intermediate layer.",
    }


@server.tool(
    name="build_image",
    title="Build a Docker image",
    description=(
        "Build an image from a directory inside the configured context root.\n\n"
        "This executes the Dockerfile's RUN steps, so it runs code from the "
        "repository: treat the Dockerfile as untrusted input. The context may not "
        "contain the context root itself, and build arguments are not accepted, "
        "because a build argument is a reliable way to bake a secret into an image "
        "layer."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
    ),
)
def build_image(
    context_path: Annotated[
        str,
        Field(description="Build context, absolute or relative to the context root."),
    ],
    tag: Annotated[str, Field(description="Image tag to apply, e.g. 'aegis-sample:dev'.")],
    dockerfile: Annotated[
        str, Field(description="Dockerfile name inside the context.", default="Dockerfile")
    ] = "Dockerfile",
) -> dict[str, Any]:
    """Build an image. Writes an image; requires approval."""
    context = _resolve_context(context_path)
    image = _check_image(tag)
    dockerfile_name = _check_dockerfile(dockerfile)

    dockerfile_path = context / dockerfile_name
    if not dockerfile_path.is_file():
        raise ToolError(f"No {dockerfile_name} in {context}.")

    result = _require_success(
        _run(
            [
                "build",
                "--progress=plain",
                "-t",
                image,
                "-f",
                str(dockerfile_path),
                str(context),
            ],
            timeout=BUILD_TIMEOUT,
        ),
        f"build {image}",
    )

    image_id = ""
    for line in result.stdout.splitlines():
        if line.startswith("Successfully tagged ") or line.startswith("Successfully built "):
            image_id = line.rsplit(" ", 1)[-1].strip()
    if not image_id:
        image_id = _inspect_field(image, "{{.Id}}")

    return {
        "image": image,
        "image_id": image_id,
        "context": str(context),
        "dockerfile": dockerfile_name,
        "build_log_tail": _tail(result.stdout, MAX_LOG_LINES, MAX_LOG_BYTES),
        "output_truncated": result.truncated,
        "warning": (
            "The build log is the last portion only. A Dockerfile RUN step executes "
            "repository code."
        ),
    }


@server.tool(
    name="start_container",
    title="Start a container",
    description=(
        "Start a detached container from a local image, optionally publishing "
        "ports.\n\n"
        "The container runs the entrypoint its image defines. No command can be "
        "appended to this call, so there is no way to override the entrypoint and "
        "obtain arbitrary execution inside the container. The container is not "
        "removed automatically, so its logs and exit state stay inspectable."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
    ),
)
def start_container(
    image: Annotated[str, Field(description="Image reference to start.")],
    name: Annotated[str, Field(description="Name for the new container.")],
    # A plain list default, not default_factory: these functions are also called
    # directly (by tests and by any in-process caller), and default_factory would
    # leave a FieldInfo here instead of a list. It is never mutated.
    ports: Annotated[
        list[str],
        Field(description="Port mappings as 'host:container', e.g. ['8080:8000']."),
    ] = [],
) -> dict[str, Any]:
    """Start a detached container. Changes system state; requires approval."""
    image_ref = _check_image(image)
    container_name = _check_container_name(name)
    mappings = [_check_port(spec) for spec in ports]

    existing = _run(["inspect", "--format", "{{.Id}}", container_name], timeout=15)
    if existing.success:
        raise ToolError(
            f"A container named {container_name!r} already exists "
            f"({existing.stdout.strip()[:12]}). Stop and remove it, or choose another name."
        )

    args = ["run", "-d", "--name", container_name]
    for mapping in mappings:
        args.extend(["-p", mapping])
    args.append(image_ref)
    # Nothing may follow the image: that is where a command override would go.

    result = _require_success(_run(args, timeout=120), f"start a container from {image_ref}")

    return {
        "container": container_name,
        "image": image_ref,
        "container_id": result.stdout.strip()[:64],
        "ports": mappings,
        "notes": [
            "The container is detached, so this call returns as soon as it starts.",
            "Health is defined by the image's HEALTHCHECK; inspect it with "
            "container_health.",
        ],
    }


@server.tool(
    name="stop_container",
    title="Stop a container",
    description=(
        "Stop a running container, giving it a grace period before it is killed. "
        "This is the one destructive tool here: it terminates a running workload, "
        "so it is classified high risk and requires explicit human approval."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
    ),
)
def stop_container(
    name: Annotated[str, Field(description="Name of the container to stop.")],
    grace_seconds: Annotated[
        int, Field(description="Seconds to wait before SIGKILL.", ge=0, le=60)
    ] = STOP_GRACE_SECONDS,
) -> dict[str, Any]:
    """Stop a container. Destructive; requires approval."""
    container_name = _check_container_name(name)

    before = _run(["inspect", "--format", "{{.State.Status}}", container_name], timeout=15)
    if not before.success:
        raise ToolError(f"No container named {container_name!r}.")

    was_running = before.stdout.strip() == "running"
    result = _require_success(
        _run(["stop", "--time", str(grace_seconds), container_name], timeout=grace_seconds + 30),
        f"stop {container_name}",
    )

    return {
        "container": container_name,
        "was_running": was_running,
        "stopped": result.success,
        "exit_status": _inspect_field(container_name, "{{.State.Status}}"),
        "exit_code": _inspect_field(container_name, "{{.State.ExitCode}}"),
    }


@server.tool(
    name="container_status",
    title="Get container status",
    description=(
        "Report a container's status, image, creation time, exit code and restart "
        "count. Only these fields are read from the full container record, because "
        "the complete record includes environment variables that may hold secrets."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def container_status(name: Annotated[str, Field(description="Container name.")]) -> dict[str, Any]:
    """Report container status. Read-only."""
    container_name = _check_container_name(name)

    result = _run(
        [
            "inspect",
            "--format",
            "{{json .State}}\t{{.Config.Image}}\t{{.Created}}\t{{.RestartCount}}",
            container_name,
        ],
        timeout=20,
    )
    if not result.success:
        raise ToolError(
            f"No container named {container_name!r}. Start it first with start_container."
        )

    # Split the template's fields in one pass. Partitioning twice would leave the
    # last two fields fused, which put the timestamp where RestartCount belongs.
    fields = result.stdout.strip().split("\t")
    if len(fields) < 4:
        raise ToolError("Docker returned a container record that could not be parsed.")
    state_json, image, created, restart = fields[0], fields[1], fields[2], fields[3]

    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        raise ToolError("Docker returned a container record that could not be parsed.") from None

    try:
        restart_count = int(restart)
    except ValueError:
        restart_count = 0

    return {
        "container": container_name,
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "health": (state.get("Health") or {}).get("Status"),
        "restart_count": restart_count,
        "image": image or None,
        "created": created or None,
        "oom_killed": bool(state.get("OOMKilled")),
    }


@server.tool(
    name="container_health",
    title="Check container health",
    description=(
        "Report a container's health check: the health status, the most recent "
        "check's output, and how many consecutive failures have been recorded.\n\n"
        "A container whose image defines no HEALTHCHECK reports "
        "'no_healthcheck' rather than 'healthy', because the absence of a check is "
        "not evidence of health."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def container_health(name: Annotated[str, Field(description="Container name.")]) -> dict[str, Any]:
    """Report health check state. Read-only."""
    container_name = _check_container_name(name)

    result = _run(
        [
            "inspect",
            "--format",
            "{{json .State}}\t{{json .Config.Healthcheck}}",
            container_name,
        ],
        timeout=20,
    )
    if not result.success:
        raise ToolError(f"No container named {container_name!r}.")

    state_json, _, healthcheck_json = result.stdout.strip().partition("\t")
    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        raise ToolError("Docker returned a container record that could not be parsed.") from None

    running = bool(state.get("Running"))
    exit_code = state.get("ExitCode")
    health = state.get("Health")

    if not running:
        # A stopped container cannot be healthy, whatever its last check said.
        return {
            "container": container_name,
            "health": "unhealthy",
            "reason": "container_not_running",
            "detail": f"The container is {state.get('Status')!r} with exit code {exit_code}.",
            "status": state.get("Status"),
            "exit_code": exit_code,
            "failing_streak": 0,
            "healthcheck_defined": health is not None,
        }

    if not health:
        return {
            "container": container_name,
            "health": "no_healthcheck",
            "reason": "image_defines_no_healthcheck",
            "detail": (
                "The image defines no HEALTHCHECK, so the container is running but "
                "unverified. Status alone is not a health signal."
            ),
            "status": state.get("Status"),
            "exit_code": exit_code,
            "failing_streak": 0,
            "healthcheck_defined": False,
            "notes": (
                "Add a HEALTHCHECK to the image to make this verifiable. It is "
                "deliberately not configurable here, since a health command is "
                "arbitrary execution inside the container."
            ),
        }

    checks = health.get("Log") or []
    last = checks[-1] if checks else {}
    status = health.get("Status")
    healthy = status == "healthy"
    # A passing check writes nothing to stdout, so silence is the success case.
    # "starting" is also not a failure, and must not be reported as one.
    if healthy:
        detail = "The most recent health check passed."
    elif status == "starting":
        detail = "The container has not completed a health check yet."
    else:
        detail = (last.get("Output") or "").strip()[:500] or "The health check failed with no output."
    return {
        "container": container_name,
        "health": status,
        "reason": None if healthy else ("healthcheck_pending" if status == "starting" else "healthcheck_failed"),
        "detail": detail,
        "status": state.get("Status"),
        "exit_code": exit_code,
        "failing_streak": int(health.get("FailingStreak") or 0),
        "healthcheck_defined": True,
        "last_check_started": last.get("Start"),
        "last_check_exit_code": last.get("ExitCode"),
        "checks_recorded": len(checks),
    }


@server.tool(
    name="container_logs",
    title="Read container logs",
    description=(
        "Read the tail of a container's logs.\n\n"
        "The result is capped in both lines and bytes and reports whether it was "
        "truncated, so the model is told when it is reading a fragment rather than "
        "the whole log. Container stdout and stderr are reported separately, "
        "because the Docker CLI splits them into two streams and interleaving them "
        "here would invent an ordering that never happened."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def container_logs(
    name: Annotated[str, Field(description="Container name.")],
    tail: Annotated[int, Field(description="Number of trailing lines to read.", ge=1, le=5000)] = 100,
) -> dict[str, Any]:
    """Read capped container logs. Read-only."""
    container_name = _check_container_name(name)
    line_cap = min(tail, MAX_LOG_LINES)

    exists = _run(["inspect", "--format", "{{.Id}}", container_name], timeout=15)
    if not exists.success:
        raise ToolError(f"No container named {container_name!r}.")

    result = _run(
        ["logs", "--timestamps", "--tail", str(tail), container_name],
        timeout=45,
        limit=MAX_LOG_BYTES,
    )
    # `docker logs` exits non-zero for some containers even though the log read
    # succeeded, so a failure is only fatal when nothing at all was captured.
    if not result.success and not (result.stdout or result.stderr):
        raise ToolError(f"Docker could not read logs for {container_name!r}.")

    logs, log_truncated = _tail(result.stdout, line_cap, MAX_LOG_BYTES)
    errors, error_truncated = _tail(result.stderr, line_cap, MAX_LOG_BYTES)

    return {
        "container": container_name,
        "logs": logs,
        "error_logs": errors,
        "lines": logs.count("\n"),
        "error_lines": errors.count("\n"),
        "truncated": bool(result.truncated or log_truncated or error_truncated),
        "requested_tail": tail,
        "line_cap": line_cap,
        "byte_cap": MAX_LOG_BYTES,
        "notes": [
            "Logs are capped. A truncated read is a fragment, not the whole log.",
            "Logs may contain secrets; do not copy them into an external service.",
        ],
    }


# --- Helpers --------------------------------------------------------------


def _tail(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    """Return the last ``max_lines`` lines of ``text``, capped at ``max_bytes``.

    The *tail* is kept because a deployment log's cause is almost always in its
    final lines; a prefix would show startup and hide the crash.
    """
    if not text:
        return "", False

    stripped = text.rstrip("\n")
    lines = stripped.split("\n")
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[-max_lines:]

    body = "\n".join(lines)
    if len(body) > max_bytes:
        body = body[-max_bytes:]
        # Do not start mid-codepoint on the first retained line.
        newline = body.find("\n")
        if newline != -1:
            body = body[newline + 1 :]
        truncated = True
    return body, truncated


def _inspect_field(reference: str, template: str) -> str:
    """Read one formatted field from a container or image, or return ""."""
    result = _run(["inspect", "--format", template, reference], timeout=15)
    return result.stdout.strip() if result.success else ""


def main() -> int:
    """Serve over stdio, which is how Aegis launches MCP servers."""
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())