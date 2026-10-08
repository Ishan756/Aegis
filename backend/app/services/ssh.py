"""SSH transport to a remote deployment target.

Used for the two things no MCP tool can do from here: checking and preparing
Docker on the target, and probing the target's own loopback when the public port
is closed. Image builds and container lifecycle go through the Docker MCP over
``DOCKER_HOST=ssh://`` instead -- this module never runs a build or a container.

Three properties matter:

- **No local shell, ever.** Commands are argument lists launched with
  ``create_subprocess_exec``. The remote login shell is unavoidable over SSH, so
  each argument is quoted with :func:`shlex.join` exactly once, and every element
  is a fixed verb or a value that came from configuration or was already
  validated -- never a raw request field.
- **The key never leaves the operator's disk unguarded.** The private key is
  copied into a fresh ``0700`` temporary directory as ``0600`` and the copy is
  deleted with the session. OpenSSH refuses a group-readable key, and copying
  into a private directory works on filesystems (such as ``/mnt/c`` under WSL)
  where permission bits cannot be repaired in place.
- **Batch mode, always.** ``BatchMode=yes`` means a missing key or a wrong host
  fails immediately instead of hanging a deployment on a password prompt nobody
  will ever answer.

The same temporary directory doubles as ``HOME`` for a child process that needs
the identical SSH configuration -- notably the Docker MCP server, whose CLI
invokes ``ssh`` itself to reach a remote daemon. Pointing both at one config is
what keeps "the container was built over SSH" and "the container was verified
over SSH" talking to the same host with the same key.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Cap on captured output per stream. A chatty install cannot exhaust memory.
MAX_OUTPUT_BYTES = 64_000

#: Hostnames and IPv4 addresses only. Rejecting anything else means a crafted
#: value can never become an option (``-o...``) or a different user's host.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")

#: POSIX-ish login names: ``ec2-user``, ``ubuntu``, ``admin``.
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


@dataclass(slots=True)
class SSHResult:
    """One completed remote command."""

    argv: tuple[str, ...]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def command_text(self) -> str:
        """The remote command, as it was sent. Safe to log: no secrets reach argv."""
        return shlex.join(self.argv)


def _tail(text: str, limit: int) -> str:
    """Keep the end of a blob: the last lines are the ones that explain a failure."""
    if len(text) <= limit:
        return text
    return text[-limit:]


class SSHSession:
    """A short-lived SSH session bound to exactly one target.

    Build with ``async with SSHSession(...) as session`` so the temporary
    directory is removed even when the deployment raises.
    """

    def __init__(
        self,
        *,
        host: str,
        user: str,
        key_file: Path,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not _HOST_RE.match(host):
            raise ValidationError(
                "The configured EC2 host must be a plain hostname or IPv4 address.",
                details={"host": host},
            )
        if not _USER_RE.match(user):
            raise ValidationError(
                "The configured SSH user is not a valid login name.",
                details={"user": user},
            )
        self._host = host
        self._user = user
        self._key_file = key_file.expanduser()
        self._timeout = timeout_seconds
        self._home: Path | None = None
        self._config: Path | None = None

    # -- Lifecycle --------------------------------------------------------

    @property
    def home(self) -> str:
        """The temporary HOME holding ``.ssh/config``. Requires :meth:`open`."""
        if self._home is None:
            raise RuntimeError("SSHSession is not open.")
        return str(self._home)

    @property
    def host(self) -> str:
        return self._host

    @property
    def user(self) -> str:
        return self._user

    def open(self) -> SSHSession:
        """Create the private directory and write the SSH configuration."""
        if self._home is not None:
            return self

        if not self._key_file.is_file():
            raise ValidationError(
                "The configured SSH key file does not exist. Point "
                "AEGIS_EC2__SSH_KEY_FILE at the private key for the target instance.",
                details={"key_file": str(self._key_file)},
            )

        home = Path(tempfile.mkdtemp(prefix="aegis-ssh-"))
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(mode=0o700)

        # A copy with fixed 0600 beats a reference to the original: OpenSSH
        # rejects a key anyone else can read, and on a mounted Windows
        # filesystem the original's mode often cannot be corrected at all.
        key_copy = ssh_dir / "id_aegis"
        shutil.copyfile(self._key_file, key_copy)
        key_copy.chmod(0o600)

        # Host keys persist in the operator's own known_hosts when it exists, so
        # a changed host key is caught on the second deployment instead of being
        # accepted fresh every time. Inside a container there is no such file,
        # and per-session trust-on-first-use is the honest fallback.
        persistent = Path.home() / ".ssh"
        known_hosts = persistent / "known_hosts" if persistent.is_dir() else ssh_dir / "known_hosts"

        config = ssh_dir / "config"
        config.write_text(
            "\n".join(
                [
                    f"Host {self._host}",
                    f"  HostName {self._host}",
                    f"  User {self._user}",
                    f"  IdentityFile {key_copy}",
                    "  IdentitiesOnly yes",
                    "  BatchMode yes",
                    "  PasswordAuthentication no",
                    "  KbdInteractiveAuthentication no",
                    "  StrictHostKeyChecking accept-new",
                    f"  UserKnownHostsFile {known_hosts}",
                    f"  ConnectTimeout {max(1, int(self._timeout))}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)

        self._home = home
        self._config = config
        return self

    def close(self) -> None:
        """Remove the temporary directory, key copy included."""
        if self._home is None:
            return
        shutil.rmtree(self._home, ignore_errors=True)
        self._home = None
        self._config = None

    async def __aenter__(self) -> SSHSession:
        return self.open()

    async def __aexit__(self, *_: object) -> None:
        self.close()

    # -- Execution --------------------------------------------------------

    async def run(
        self,
        *argv: str,
        timeout: float | None = None,
    ) -> SSHResult:
        """Run one command on the target. Never raises for a command that fails.

        A non-zero exit, a timeout and an unreachable host are all results: the
        caller decides what they mean, because "docker is not installed" and
        "the network is down" demand different responses.
        """
        if not argv:
            raise ValidationError("An SSH command requires at least one argument.")
        if self._config is None:
            raise RuntimeError("SSHSession is not open.")

        budget = timeout if timeout is not None else self._timeout
        remote = shlex.join(argv)
        command = ("ssh", "-F", str(self._config), self._host, remote)
        started = time.monotonic()

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # `ssh` itself could not be launched (not installed, not on PATH).
            # That is a result the caller must see as a failed command, not an
            # exception that would strand the deployment's history as in-progress.
            return SSHResult(
                argv=argv,
                exit_code=None,
                stderr=f"{type(exc).__name__}: {exc}",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), budget)
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()

        elapsed = round(time.monotonic() - started, 3)
        result = SSHResult(
            argv=argv,
            exit_code=None if timed_out else process.returncode,
            stdout=_tail(stdout_bytes.decode("utf-8", errors="replace"), MAX_OUTPUT_BYTES),
            stderr=_tail(stderr_bytes.decode("utf-8", errors="replace"), MAX_OUTPUT_BYTES),
            timed_out=timed_out,
            duration_seconds=elapsed,
        )

        # The command text only: argv carries no credentials, and logging it is
        # what makes a failed deployment reconstructable.
        logger.info(
            "ssh command finished",
            extra={
                "host": self._host,
                "command": result.command_text,
                "exit_code": result.exit_code,
                "timed_out": timed_out,
                "duration_seconds": elapsed,
            },
        )
        return result

    async def http_status(self, url: str, timeout: float = 10.0) -> int | None:
        """The HTTP status ``url`` answers with *from inside the target*.

        This is the fallback for a port the security group does not expose: it
        distinguishes "the application is broken" from "the firewall hides it".
        ``None`` means the probe could not be made at all -- ``curl`` missing,
        connection refused, a timeout -- and the caller must report that rather
        than infer a status.
        """
        if not url.startswith(("http://", "https://")):
            raise ValidationError("A probe URL must start with http:// or https://")
        result = await self.run(
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            str(int(timeout)),
            url,
            timeout=timeout + 5.0,
        )
        text = result.stdout.strip()
        if result.success and text.isdigit():
            return int(text)
        return None


__all__ = ["MAX_OUTPUT_BYTES", "SSHResult", "SSHSession"]
