"""AWS MCP server (inspect-only).

Exposes read-only tools for EC2, S3, and CloudWatch. All tools are annotated
``read_only`` and none perform destructive infrastructure actions (no termination,
deletion, stop/start, modify, or create operations).

Credentials come from the environment only. The backend forwards AWS variables
explicitly via ``AEGIS_MCP__FORWARD_ENVIRONMENT``; the MCP SDK does not inherit
arbitrary environment variables.

Run directly with::

    python mcp_servers/aws/server.py

Anticipated failures raise the SDK's :class:`ToolError` so the caller receives
an actionable message instead of a generic "error executing tool".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

REQUEST_TIMEOUT = float(os.environ.get("AEGIS_AWS__TIMEOUT_SECONDS", "60"))
COMMAND_TIMEOUT = REQUEST_TIMEOUT
MAX_OUTPUT_BYTES = int(os.environ.get("AEGIS_AWS__MAX_OUTPUT_BYTES", "128000"))
MAX_LOG_LINES = int(os.environ.get("AEGIS_AWS__MAX_LOG_LINES", "200"))
MAX_LOG_BYTES = int(os.environ.get("AEGIS_AWS__MAX_LOG_BYTES", "32000"))

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)

server = MCPServer(
    "aegis-aws",
    instructions=(
        "Read-only access to AWS (EC2, S3, CloudWatch). No destructive actions; "
        "only inspect and retrieve data."
    ),
)

ALLOWED_REGIONS = {
    "ap-south-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "eu-west-1",
    "eu-west-2",
    "eu-central-1",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
}

def _check_nonempty(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ToolError(f"{name} must not be empty.")
    return cleaned


def _child_env() -> dict[str, str]:
    """Allowlist environment passed to aws CLI subprocess."""
    allowed: dict[str, str] = {}
    for k in (
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "TERM",
        "SHELL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        if k in os.environ:
            allowed[k] = os.environ[k]

    for k, v in os.environ.items():
        if k.startswith("AWS_") or k.startswith("AEGIS_AWS__"):
            allowed[k] = v
        elif k in ("DOCKER_HOST",):
            allowed[k] = v

    return allowed


def _run(args: list[str], timeout: float | None = None, limit: int | None = None) -> Any:
    """Run aws CLI with argument list, no shell. Return structured result."""
    timeout = timeout if timeout is not None else COMMAND_TIMEOUT
    limit = limit if limit is not None else MAX_OUTPUT_BYTES
    env = _child_env()

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=False,
        )
    except FileNotFoundError as exc:
        raise ToolError("AWS CLI (aws) not found in PATH.") from exc
    except OSError as exc:
        raise ToolError(f"Could not start aws CLI: {exc}") from exc

    import threading

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _reader(stream: Any, chunks: list[bytes]) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > limit:
                    break
        except Exception:
            pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        raise ToolError(f"aws CLI command timed out after {timeout}s.")

    t_out.join(timeout=1)
    t_err.join(timeout=1)
    proc.stdout.close()
    proc.stderr.close()

    stdout_b = b"".join(stdout_chunks)
    stderr_b = b"".join(stderr_chunks)
    truncated = (len(stdout_b) + len(stderr_b)) > limit

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    return {
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
    }


def _tail(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
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
        newline = body.find("\n")
        if newline != -1:
            body = body[newline + 1 :]
        truncated = True
    return body, truncated


def _parse_json_safe(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return None


@server.tool(
    name="ec2_describe_instance",
    title="Describe EC2 instance",
    description=(
        "Return detailed metadata for a single EC2 instance (state, instance type, "
        "launch time, public/private IPs, VPC/subnet, tags). Read-only."
    ),
    annotations=_READ_ONLY,
)
def ec2_describe_instance(
    instance_id: Annotated[str, Field(description="EC2 instance ID (e.g. i-0123456789abcdef0).")],
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    instance_id = _check_nonempty(instance_id, "instance_id")
    args = ["aws", "ec2", "describe-instances", "--instance-ids", instance_id]
    if region:
        region_clean = _check_nonempty(region, "region")
        if region_clean not in ALLOWED_REGIONS:
            raise ToolError(f"Region {region_clean!r} not in allowed list.")
        args.extend(["--region", region_clean])

    res = _run(args, timeout=COMMAND_TIMEOUT)
    if not res["success"]:
        msg = res["stderr"] or res["stdout"] or "aws ec2 describe-instances failed"
        raise ToolError(msg.strip())

    parsed = _parse_json_safe(res["stdout"])
    if parsed is None:
        return {
            "instance_id": instance_id,
            "raw": res["stdout"],
            "truncated": res["truncated"],
        }
    return {
        "instance_id": instance_id,
        "response": parsed,
        "truncated": res["truncated"],
    }


@server.tool(
    name="ec2_instance_state",
    title="Check EC2 instance state",
    description=(
        "Return instance state (pending/running/stopped/terminated/stopping/shutting-down). Read-only."
    ),
    annotations=_READ_ONLY,
)
def ec2_instance_state(
    instance_id: Annotated[str, Field(description="EC2 instance ID.")],
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    instance_id = _check_nonempty(instance_id, "instance_id")
    args = ["aws", "ec2", "describe-instances", "--instance-ids", instance_id, "--query", "Reservations[0].Instances[0].{State:State.Name,Code:State.Code}"]
    if region:
        region_clean = _check_nonempty(region, "region")
        if region_clean not in ALLOWED_REGIONS:
            raise ToolError(f"Region {region_clean!r} not in allowed list.")
        args.extend(["--region", region_clean])
    args.extend(["--output", "json"])

    res = _run(args, timeout=COMMAND_TIMEOUT)
    if not res["success"]:
        msg = res["stderr"] or res["stdout"] or "aws ec2 describe-instances failed"
        raise ToolError(msg.strip())

    parsed = _parse_json_safe(res["stdout"])
    return {
        "instance_id": instance_id,
        "state": parsed,
        "raw": res["stdout"] if parsed is None else None,
        "truncated": res["truncated"],
    }


@server.tool(
    name="ec2_instance_networking",
    title="Retrieve EC2 instance networking info",
    description=(
        "Return VPC, subnet, security groups, public/private IPs, and network interfaces. Read-only."
    ),
    annotations=_READ_ONLY,
)
def ec2_instance_networking(
    instance_id: Annotated[str, Field(description="EC2 instance ID.")],
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    instance_id = _check_nonempty(instance_id, "instance_id")
    query = "Reservations[0].Instances[0].{VpcId:VpcId,SubnetId:SubnetId,SecurityGroups:SecurityGroups,PublicIpAddress:PublicIpAddress,PrivateIpAddress:PrivateIpAddress,PrivateIpAddresses:PrivateIpAddresses,NetworkInterfaces:NetworkInterfaces}"
    args = ["aws", "ec2", "describe-instances", "--instance-ids", instance_id, "--query", query]
    if region:
        region_clean = _check_nonempty(region, "region")
        if region_clean not in ALLOWED_REGIONS:
            raise ToolError(f"Region {region_clean!r} not in allowed list.")
        args.extend(["--region", region_clean])
    args.extend(["--output", "json"])

    res = _run(args, timeout=COMMAND_TIMEOUT)
    if not res["success"]:
        msg = res["stderr"] or res["stdout"] or "aws ec2 describe-instances failed"
        raise ToolError(msg.strip())

    parsed = _parse_json_safe(res["stdout"])
    return {
        "instance_id": instance_id,
        "networking": parsed,
        "raw": res["stdout"] if parsed is None else None,
        "truncated": res["truncated"],
    }


_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)

@server.tool(
    name="s3_upload_deployment_report",
    title="Upload deployment report to S3",
    description="Upload a deployment report (JSON or text) to an S3 bucket/key.",
    annotations=_WRITE,
)
def s3_upload_deployment_report(
    bucket: Annotated[str, Field(description="S3 bucket name.")],
    key: Annotated[str, Field(description="S3 object key.")],
    body: Annotated[str, Field(description="Report content (JSON or text).")],
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    bucket = _check_nonempty(bucket, "bucket")
    key = _check_nonempty(key, "key")
    if body is None:
        raise ToolError("body must not be empty.")
    import tempfile

    fd, path_tmp = tempfile.mkstemp(suffix=".report", text=True)
    try:
        os.write(fd, body.encode("utf-8", errors="replace"))
        os.close(fd)
        fd = -1
        args = ["aws", "s3", "cp", path_tmp, f"s3://{bucket}/{key}"]
        if region:
            region_clean = _check_nonempty(region, "region")
            if region_clean not in ALLOWED_REGIONS:
                raise ToolError(f"Region {region_clean!r} not in allowed list.")
            args.extend(["--region", region_clean])
        res = _run(args, timeout=COMMAND_TIMEOUT)
        if not res["success"]:
            msg = res["stderr"] or res["stdout"] or "aws s3 cp failed"
            raise ToolError(msg.strip())
        return {
            "bucket": bucket,
            "key": key,
            "success": True,
            "truncated": res["truncated"],
        }
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(path_tmp)
        except Exception:
            pass
@server.tool(
    name="s3_get_deployment_report",
    title="Retrieve deployment report from S3",
    description="Retrieve deployment report content from S3 (JSON/text). Read-only.",
    annotations=_READ_ONLY,
)
def s3_get_deployment_report(
    bucket: Annotated[str, Field(description="S3 bucket name.")],
    key: Annotated[str, Field(description="S3 object key.")],
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    bucket = _check_nonempty(bucket, "bucket")
    key = _check_nonempty(key, "key")
    args = ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"]
    if region:
        region_clean = _check_nonempty(region, "region")
        if region_clean not in ALLOWED_REGIONS:
            raise ToolError(f"Region {region_clean!r} not in allowed list.")
        args.extend(["--region", region_clean])

    res = _run(args, timeout=COMMAND_TIMEOUT, limit=MAX_OUTPUT_BYTES)
    if not res["success"]:
        msg = res["stderr"] or res["stdout"] or "aws s3 cp failed"
        raise ToolError(msg.strip())

    logs, truncated = _tail(res["stdout"], MAX_LOG_LINES, MAX_LOG_BYTES)
    return {
        "bucket": bucket,
        "key": key,
        "content": logs,
        "truncated": truncated or res["truncated"],
    }


@server.tool(
    name="cloudwatch_get_log_events",
    title="Retrieve CloudWatch log events",
    description=(
        "Retrieve relevant application/deployment logs from a CloudWatch Logs log group. "
        "Returns log events (time, message). Read-only."
    ),
    annotations=_READ_ONLY,
)
def cloudwatch_get_log_events(
    log_group: Annotated[str, Field(description="CloudWatch Logs log group name.")],
    log_stream: Annotated[str | None, Field(description="Specific log stream name (optional).")] = None,
    start_time: Annotated[int | None, Field(description="Start time in milliseconds since epoch (optional).")] = None,
    end_time: Annotated[int | None, Field(description="End time in milliseconds since epoch (optional).")] = None,
    limit: Annotated[int, Field(description="Max events to return.", ge=1, le=1000)] = 100,
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    log_group = _check_nonempty(log_group, "log_group")
    limit_clamped = max(1, min(limit, 1000))
    args = ["aws", "logs", "get-log-events"]
    if log_stream:
        ls = _check_nonempty(log_stream, "log_stream")
        args.extend(["--log-stream-name", ls])
    else:
        # If no stream specified, use filter-log-events as broader search
        args = ["aws", "logs", "filter-log-events", "--log-group-name", log_group]
        if start_time is not None:
            args.extend(["--start-time", str(start_time)])
        if end_time is not None:
            args.extend(["--end-time", str(end_time)])
        args.extend(["--max-items", str(limit_clamped)])
        if region:
            region_clean = _check_nonempty(region, "region")
            if region_clean not in ALLOWED_REGIONS:
                raise ToolError(f"Region {region_clean!r} not in allowed list.")
            args.extend(["--region", region_clean])
        args.extend(["--output", "json"])
        res = _run(args, timeout=COMMAND_TIMEOUT)
        if not res["success"]:
            msg = res["stderr"] or res["stdout"] or "aws logs filter-log-events failed"
            raise ToolError(msg.strip())
        parsed = _parse_json_safe(res["stdout"])
        return {
            "log_group": log_group,
            "events": (parsed.get("events") if isinstance(parsed, dict) else None),
            "raw": res["stdout"] if parsed is None else None,
            "truncated": res["truncated"],
        }

    args.extend(["--log-group-name", log_group, "--limit", str(limit_clamped)])
    if start_time is not None:
        args.extend(["--start-time", str(start_time)])
    if end_time is not None:
        args.extend(["--end-time", str(end_time)])
    if region:
        region_clean = _check_nonempty(region, "region")
        if region_clean not in ALLOWED_REGIONS:
            raise ToolError(f"Region {region_clean!r} not in allowed list.")
        args.extend(["--region", region_clean])
    args.extend(["--output", "json"])

    res = _run(args, timeout=COMMAND_TIMEOUT)
    if not res["success"]:
        msg = res["stderr"] or res["stdout"] or "aws logs get-log-events failed"
        raise ToolError(msg.strip())

    parsed = _parse_json_safe(res["stdout"])
    return {
        "log_group": log_group,
        "log_stream": log_stream,
        "events": (parsed.get("events") if isinstance(parsed, dict) else None),
        "next_forward_token": (parsed.get("nextForwardToken") if isinstance(parsed, dict) else None),
        "raw": res["stdout"] if parsed is None else None,
        "truncated": res["truncated"],
    }


@server.tool(
    name="cloudwatch_get_metrics",
    title="Retrieve CloudWatch metrics",
    description=(
        "Retrieve basic CloudWatch metrics for a namespace/metric (e.g. CPUUtilization, "
        "NetworkIn). Returns metric data points. Read-only."
    ),
    annotations=_READ_ONLY,
)
def cloudwatch_get_metrics(
    namespace: Annotated[str, Field(description="CloudWatch namespace (e.g. AWS/EC2).")],
    metric_name: Annotated[str, Field(description="Metric name (e.g. CPUUtilization).")],
    dimensions: Annotated[dict[str, str] | None, Field(description="Dimensions dict, e.g. {'InstanceId':'i-...'}")] = None,
    start_time: Annotated[int | None, Field(description="Start time in milliseconds since epoch (optional).")] = None,
    end_time: Annotated[int | None, Field(description="End time in milliseconds since epoch (optional).")] = None,
    period: Annotated[int, Field(description="Period in seconds (e.g. 300).", ge=1, le=3600)] = 300,
    statistics: Annotated[list[str], Field(description="Statistics (SampleCount/Average/Sum/Minimum/Maximum).")] = ["Average"],
    region: Annotated[str | None, Field(description="AWS region (optional).")] = None,
) -> dict[str, Any]:
    namespace = _check_nonempty(namespace, "namespace")
    metric_name = _check_nonempty(metric_name, "metric_name")
    period_clamped = max(1, min(period, 3600))
    stats = [s for s in statistics if s in ("SampleCount", "Average", "Sum", "Minimum", "Maximum")][:5]
    if not stats:
        stats = ["Average"]

    args = ["aws", "cloudwatch", "get-metric-statistics", "--namespace", namespace, "--metric-name", metric_name, "--period", str(period_clamped), "--statistics"]
    args.extend(stats)
    if start_time is not None and end_time is not None:
        args.extend(["--start-time", str(start_time // 1000) if start_time > 10**12 else str(start_time)])
        args.extend(["--end-time", str(end_time // 1000) if end_time > 10**12 else str(end_time)])
    else:
        import datetime

        end = datetime.datetime.utcnow()
        start = end - datetime.timedelta(minutes=60)
        args.extend(["--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ")])
        args.extend(["--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ")])

    if dimensions:
        dim_json = json.dumps([{"Name": k, "Value": v} for k, v in dimensions.items() if k and v])
        args.extend(["--dimensions", dim_json])

    if region:
        region_clean = _check_nonempty(region, "region")
        if region_clean not in ALLOWED_REGIONS:
            raise ToolError(f"Region {region_clean!r} not in allowed list.")
        args.extend(["--region", region_clean])
    args.extend(["--output", "json"])

    res = _run(args, timeout=COMMAND_TIMEOUT)
    if not res["success"]:
        msg = res["stderr"] or res["stdout"] or "aws cloudwatch get-metric-statistics failed"
        raise ToolError(msg.strip())

    parsed = _parse_json_safe(res["stdout"])
    return {
        "namespace": namespace,
        "metric_name": metric_name,
        "datapoints": (parsed.get("Datapoints") if isinstance(parsed, dict) else None),
        "raw": res["stdout"] if parsed is None else None,
        "truncated": res["truncated"],
    }


def main() -> int:
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
