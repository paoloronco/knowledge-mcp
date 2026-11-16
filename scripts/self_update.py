#!/usr/bin/env python3
"""One-command, data-preserving self-update for Knowledge MCP containers.

Run this on the Docker host. It inspects the running app (and, when present,
the host companion), pulls the newest image for the same tag, and recreates the
containers with an identical configuration. Named volumes and bind mounts are
reused, so settings, documents, the model cache, agent credentials and Qdrant
indexes survive. If the new app does not become healthy, the previous image is
restored automatically.

The application container deliberately has no Docker socket. This script uses
the Docker CLI on the host instead, so no extra privileges are granted to the
long-running app.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

DEFAULT_APP = "knowledge-mcp"
HELPER_SUFFIX = "-host-agent"


class UpdateError(RuntimeError):
    pass


def log(message):
    print("[self-update] " + message, flush=True)


def docker(args, check=True, timeout=900):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise UpdateError(
            f"docker {' '.join(args[:3])} failed (exit {result.returncode}): "
            f"{detail[:1000]}"
        )
    return result


def inspect_container(name):
    result = docker(["container", "inspect", name], check=False, timeout=30)
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except ValueError as exc:
        raise UpdateError(f"Could not read docker inspect output: {exc}") from exc
    return entries[0] if entries else None


def image_id(reference):
    result = docker(
        ["image", "inspect", "--format", "{{.Id}}", reference],
        check=False,
        timeout=30,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def state_of(name):
    result = docker(
        ["inspect", "--format", "{{json .State}}", name], check=False, timeout=30
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip())
    except ValueError:
        return None


def build_run_args(info, image, name, network_override=None):
    """Reconstruct an equivalent `docker run` command from `docker inspect`."""
    config = info.get("Config") or {}
    host = info.get("HostConfig") or {}
    args = ["run", "-d", "--name", name]

    restart = (host.get("RestartPolicy") or {}).get("Name") or ""
    if restart and restart != "no":
        args += ["--restart", restart]

    network = network_override or host.get("NetworkMode") or ""
    if network and network not in ("default", "bridge"):
        args += ["--network", network]
    shared_network = network.startswith("container:") or network == "host"

    for item in config.get("Env") or []:
        args += ["-e", item]

    for label, value in (config.get("Labels") or {}).items():
        args += ["-l", f"{label}={value}"]

    hostname = config.get("Hostname") or ""
    container_id = (info.get("Id") or "").replace("sha256:", "")
    if hostname and not shared_network and hostname != container_id[:12]:
        args += ["--hostname", hostname]
    if config.get("User"):
        args += ["--user", config["User"]]
    if config.get("WorkingDir"):
        args += ["-w", config["WorkingDir"]]
    if config.get("StopSignal"):
        args += ["--stop-signal", config["StopSignal"]]
    if config.get("StopTimeout"):
        args += ["--stop-timeout", str(config["StopTimeout"])]
    if config.get("Tty"):
        args += ["-t"]
    if config.get("OpenStdin"):
        args += ["-i"]

    for cap in host.get("CapDrop") or []:
        args += ["--cap-drop", cap]
    for cap in host.get("CapAdd") or []:
        args += ["--cap-add", cap]
    for option in host.get("SecurityOpt") or []:
        args += ["--security-opt", option]
    if host.get("ReadonlyRootfs"):
        args += ["--read-only"]
    if host.get("Privileged"):
        args += ["--privileged"]
    if host.get("PidMode") and host["PidMode"] not in ("", "private"):
        args += ["--pid", host["PidMode"]]
    if host.get("IpcMode") and host["IpcMode"] not in ("", "private"):
        args += ["--ipc", host["IpcMode"]]
    for device in host.get("Devices") or []:
        source = device.get("PathOnHost") or ""
        target = device.get("PathInContainer") or ""
        perms = device.get("CgroupPermissions") or "rwm"
        if source and target:
            args += ["--device", f"{source}:{target}:{perms}"]

    requests = host.get("DeviceRequests") or []
    if any(
        request.get("Driver") == "nvidia" or "gpu" in str(request.get("Capabilities"))
        for request in requests
    ):
        args += ["--gpus", "all"]

    binds = host.get("Binds") or []
    if binds:
        for bind in binds:
            args += ["-v", bind]
    else:
        for mount in info.get("Mounts") or []:
            if mount.get("Type") == "tmpfs":
                continue
            destination = mount.get("Destination")
            source = mount.get("Name") or mount.get("Source") or ""
            if not destination or not source:
                continue
            options = []
            mode = mount.get("Mode") or ""
            if mode and mode != "rw":
                options.extend(part for part in mode.split(",") if part)
            if not mount.get("RW", True) and "ro" not in options:
                options.append("ro")
            volume_options = mount.get("VolumeOptions") or {}
            if volume_options.get("NoCopy"):
                options.append("nocopy")
            spec = f"{source}:{destination}"
            if options:
                spec += ":" + ",".join(dict.fromkeys(options))
            args += ["-v", spec]

    for destination, options in (host.get("Tmpfs") or {}).items():
        args += ["--tmpfs", destination + (":" + options if options else "")]

    if not shared_network:
        for port_spec, bindings in (host.get("PortBindings") or {}).items():
            container_port, _, protocol = port_spec.partition("/")
            for binding in bindings or []:
                host_ip = binding.get("HostIp") or ""
                host_port = binding.get("HostPort") or ""
                if host_ip in ("", "0.0.0.0", "::"):
                    spec = (host_port + ":" if host_port else "") + container_port
                else:
                    spec = host_ip + ":" + (host_port + ":" if host_port else "") + container_port
                if protocol == "udp":
                    spec += "/udp"
                args += ["-p", spec]

        for server in host.get("Dns") or []:
            args += ["--dns", server]
        for entry in host.get("ExtraHosts") or []:
            args += ["--add-host", entry]

    log_config = host.get("LogConfig") or {}
    driver = log_config.get("Type") or ""
    if driver and driver != "json-file":
        args += ["--log-driver", driver]
        for key, value in (log_config.get("Config") or {}).items():
            args += ["--log-opt", f"{key}={value}"]

    cmd = list(config.get("Cmd") or [])
    entrypoint = config.get("Entrypoint") or []
    if entrypoint:
        args += ["--entrypoint", entrypoint[0]]
        cmd = list(entrypoint[1:]) + cmd

    args.append(image)
    args += cmd
    return args


def detect_compose_files(app_info):
    labels = (app_info.get("Config") or {}).get("Labels") or {}
    if not labels.get("com.docker.compose.project"):
        return []
    candidates = ["compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"]
    found = [name for name in candidates if os.path.isfile(name)]
    if not found:
        return []
    image = (app_info.get("Config") or {}).get("Image") or ""
    if image.rsplit(":", 1)[-1] == "cuda" and os.path.isfile("compose.gpu.yaml"):
        found.append("compose.gpu.yaml")
    return found


def compose_args(files, project):
    result = ["compose"]
    for name in files:
        result += ["-f", name]
    if project:
        result += ["-p", project]
    return result


def compose_update(files, project, dry_run):
    base = compose_args(files, project)
    commands = [
        ["docker", *base, "pull"],
        ["docker", *base, "up", "-d"],
    ]
    for command in commands:
        log("Would run: " + shlex.join(command) if dry_run else "Running: " + shlex.join(command))
        if not dry_run:
            docker(command[1:], timeout=1800)
    if not dry_run:
        log("Compose stack updated. Volumes were preserved.")
    return 0


def wait_for(name, timeout, require_health=True):
    deadline = time.time() + timeout
    last = "starting"
    while time.time() < deadline:
        state = state_of(name)
        if state and not state.get("Running"):
            return False, "container exited"
        if state:
            health = state.get("Health") or {}
            status = health.get("Status")
            if not require_health:
                return True, "running"
            if status == "healthy":
                return True, "healthy"
            if status:
                last = status
            else:
                return True, "running"
        time.sleep(2)
    return False, last


def rollback(plans, created, old_commands):
    log("Rolling back to the previously running image…")
    for name in reversed(created):
        docker(["rm", "-f", name], check=False, timeout=60)
    for plan, command in zip(plans, old_commands):
        log("Restoring " + plan["name"])
        result = docker(command, check=False, timeout=300)
        if result.returncode != 0:
            log("WARNING: could not restore {}: {}".format(plan["name"], (result.stderr or "").strip()[:400]))


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Update the Knowledge MCP app (and companion) without losing data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--app", default=os.getenv("APP_CONTAINER", DEFAULT_APP), help="App container name")
    parser.add_argument("--helper", default="", help="Companion container name; empty auto-detects")
    parser.add_argument("--no-helper", action="store_true", help="Do not update the companion container")
    parser.add_argument("--tag", default="", help="Override the app image tag to pull")
    parser.add_argument("--helper-tag", default="", help="Override the companion image tag to pull")
    parser.add_argument("--mode", choices=("auto", "run", "compose"), default="auto", help="Update strategy")
    parser.add_argument("--compose-file", action="append", default=[], help="Compose file (repeatable)")
    parser.add_argument("--stop-timeout", type=int, default=30, help="Seconds to wait for a graceful stop")
    parser.add_argument("--timeout", type=int, default=240, help="Seconds to wait for health after restart")
    parser.add_argument("--force", action="store_true", help="Recreate even when the image is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without changing anything")
    return parser.parse_args(argv)


def run(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    docker(["version", "--format", "{{.Server.Version}}"], timeout=30)

    app_info = inspect_container(args.app)
    if app_info is None:
        raise UpdateError(
            f"Container {args.app!r} was not found. Start the app with the README docker run command "
            "or run this script from a Docker Compose checkout."
        )
    helper_name = "" if args.no_helper else (args.helper or args.app + HELPER_SUFFIX)
    helper_info = inspect_container(helper_name) if helper_name else None
    log(
        "Found app {}{}".format(args.app, " and companion " + helper_name if helper_info else " (no companion found)")
    )

    labels = (app_info.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    compose_files = args.compose_file or detect_compose_files(app_info)
    if args.mode in ("auto", "compose") and project and compose_files:
        log(f"Detected Compose project {project!r}; using docker compose.")
        return compose_update(compose_files, project, args.dry_run)
    if args.mode == "compose":
        raise UpdateError("No Compose project files found; pass --compose-file or use --mode run")
    if project:
        log("WARNING: Compose labels found but no Compose files here; recreating with docker run.")

    app_image = args.tag or app_info["Config"]["Image"]
    helper_image = (args.helper_tag or helper_info["Config"]["Image"]) if helper_info else None

    plans = [
        {
            "name": args.app,
            "info": app_info,
            "new_image": app_image,
            "old_image": app_info.get("Image") or app_info["Config"]["Image"],
            "network_override": None,
        }
    ]
    if helper_info:
        plans.append(
            {
                "name": helper_name,
                "info": helper_info,
                "new_image": helper_image,
                "old_image": helper_info.get("Image") or helper_info["Config"]["Image"],
                "network_override": "container:" + args.app,
            }
        )

    new_commands = [
        build_run_args(p["info"], p["new_image"], p["name"], p["network_override"])
        for p in plans
    ]
    old_commands = [
        build_run_args(p["info"], p["old_image"], p["name"], p["network_override"])
        for p in plans
    ]

    if args.dry_run:
        log("Dry run: no changes will be made.")
        for p in plans:
            log("Would pull " + p["new_image"])
        for command in new_commands:
            log("Would run: " + shlex.join(["docker", *command]))
        log("Rollback would restore the previous image IDs.")
        return 0

    old_ids = {p["name"]: image_id(p["old_image"]) or p["old_image"] for p in plans}
    for p in plans:
        log("Pulling " + p["new_image"])
        docker(["pull", p["new_image"]], timeout=1800)
    changed = [
        p["name"]
        for p in plans
        if (image_id(p["new_image"]) or p["new_image"]) != old_ids[p["name"]]
    ]
    if not changed and not args.force:
        log("Images are already up to date; no restart needed. Use --force to recreate anyway.")
        return 0
    if changed:
        log("New image available for: " + ", ".join(changed) + ".")
    else:
        log("Forced recreate requested.")

    for p in reversed(plans):
        log("Stopping " + p["name"])
        docker(["stop", "-t", str(args.stop_timeout), p["name"]], check=False, timeout=args.stop_timeout + 30)
        docker(["rm", "-f", p["name"]], check=False, timeout=60)

    created = []
    try:
        for p, command in zip(plans, new_commands):
            log("Starting " + p["name"] + " with the updated image")
            docker(command, timeout=300)
            created.append(p["name"])
        ok, reason = wait_for(args.app, args.timeout, require_health=True)
        if not ok:
            raise UpdateError(f"app container did not become healthy ({reason})")
        log("App is healthy.")
        if helper_info:
            ok, reason = wait_for(helper_name, 60, require_health=False)
            if not ok:
                raise UpdateError(f"companion container did not start ({reason})")
            log("Companion is running.")
    except Exception as exc:
        rollback(plans, created, old_commands)
        raise UpdateError(f"Update failed and was rolled back: {exc}") from exc

    log("Update complete. Settings, documents, credentials and indexes were preserved.")
    return 0


def main():
    try:
        return run()
    except (UpdateError, subprocess.SubprocessError, OSError) as exc:
        log("ERROR: " + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
