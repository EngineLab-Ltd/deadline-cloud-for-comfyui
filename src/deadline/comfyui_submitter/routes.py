# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
The Deadline Cloud routes on the ComfyUI server. ComfyUI's submitter panel plans a submission with
/deadline/plan, calls the Deadline Cloud client library through /deadline/cloud/<command>, and
submits with /deadline/jobs. The library runs in the Deadline Cloud CLI's own Python, through
bridge.py. Background tasks report progress and results over the websocket.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import folder_paths
from aiohttp import web
from server import PromptServer

from .get_local_assets import LocalAssets, get_local_assets, write_asset_references
from .submit import (
    ALLOW_REMOTE_ENV_VAR,
    DEFAULT_WORKFLOW_NAME,
    PICKER_KINDS,
    SubmitError,
    check_cli_version,
    create_job_bundle,
    input_folders,
    job_description,
    load_config,
    picker_command,
    read_job_template,
    remote_allowed,
    resolve_cli_python,
    resolve_conda_channels,
    resolve_deadline_cli,
    resolve_job_template,
    validate_api_workflow,
)

logger = logging.getLogger(__name__)

SUBMITTED_EVENT = "deadline.submitted"
PROGRESS_EVENT = "deadline.progress"
CONFIRM_EVENT = "deadline.confirm"
_SUBMISSION_ID = re.compile(r"[0-9a-f]{32}")

BRIDGE = Path(__file__).resolve().parent / "bridge.py"
CLOUD_COMMANDS = ("status", "login", "farms", "queues", "queue", "set-defaults")
BRIDGE_TIMEOUT = 120
# A first run of deadline --version can be slow, but it shouldn't hang.
CLI_VERSION_TIMEOUT = 60
# Logging in waits for the artist to finish in Deadline Cloud monitor.
LOGIN_TIMEOUT = 600
# A confirmation nobody answers, for example after its tab closed, is declined after this long, so
# the submission ends instead of waiting forever.
CONFIRM_TIMEOUT = 600
# The queue environment parameter that the config's conda_channels fills in.
CONDA_CHANNELS_PARAMETER = "CondaChannels"

# Keeps a reference to each background task so it isn't garbage collected while it runs.
_watchers: set[asyncio.Task] = set()

_cli_versions: dict[str, str] = {}


@dataclass
class _Submission:
    name: str
    process: asyncio.subprocess.Process
    canceled: bool = False
    # The browser tab that submitted it, which alone gets its events.
    client_id: str | None = None
    confirms: int = 0
    answered: int = 0
    # Timers that decline unanswered confirmations, canceled when the submission ends.
    timers: list[asyncio.Task] = field(default_factory=list)


_submissions: dict[str, _Submission] = {}


async def _cli_version_output(cli: str) -> str:
    if cli not in _cli_versions:
        process = await asyncio.create_subprocess_exec(
            cli,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), CLI_VERSION_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        _cli_versions[cli] = stdout.decode(errors="replace")
    return _cli_versions[cli]


async def _checked_cli(config: dict[str, Any]) -> str | web.Response:
    """Returns the Deadline Cloud CLI, once it's a version the submitter supports."""
    try:
        cli = resolve_deadline_cli(config)
    except SubmitError as e:
        return _error(400, str(e))
    try:
        check_cli_version(cli, await _cli_version_output(cli))
    except SubmitError as e:
        # Check again next time, so upgrading the CLI doesn't need a ComfyUI restart.
        _cli_versions.pop(cli, None)
        return _error(400, str(e))
    except (TimeoutError, OSError) as e:
        return _error(500, f"Couldn't run the Deadline Cloud CLI: {e}")
    return cli


def _is_loopback(remote: str | None) -> bool:
    try:
        return remote is not None and ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _client_id(body: dict[str, Any]) -> str | None:
    """The websocket client ID of the browser tab that sent a request, if it sent one."""
    client_id = body.get("clientId")
    return client_id if isinstance(client_id, str) and _CLIENT_ID.fullmatch(client_id) else None


def _send(event: str, data: dict[str, Any], client_id: str | None) -> None:
    """
    Sends an event to the browser tab that started the work. Without its ID, as from an older
    panel, every connected tab gets it.
    """
    PromptServer.instance.send_sync(event, data, client_id)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _log_value(value: object) -> str:
    """A value from a request, with its line breaks escaped so it can't forge log entries."""
    return str(value).replace("\r\n", "\\r\\n").replace("\n", "\\n").replace("\r", "\\r")


def _log_block(text: str) -> str:
    """Multi-line output for a log entry, indented so its lines can't pass for log entries."""
    return text.rstrip().replace("\r\n", "\n").replace("\n", "\n    ").replace("\r", "\\r")


# Headers that a reverse proxy adds. A proxy on this machine connects from a loopback address, so
# without them the browsers it forwards would pass for local ones.
_FORWARDING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")


async def _read_body(request: web.Request) -> dict[str, Any] | web.Response:
    """Checks that a request comes from an allowed browser and returns its JSON object body."""
    # The routes act with this machine's AWS credentials, so they accept only ComfyUI's own page
    # or a request no page sent. --enable-cors-header turns off ComfyUI's origin check, and
    # "same-site" includes other ports on this machine.
    if request.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
        return _error(403, "Deadline Cloud doesn't accept requests from other websites.")
    forwarded = any(header in request.headers for header in _FORWARDING_HEADERS)
    if (forwarded or not _is_loopback(request.remote)) and not remote_allowed():
        return _error(
            403,
            "Deadline Cloud submits from the ComfyUI server, with its credentials, so it only "
            "accepts requests from a browser on that machine, not through a proxy. Set "
            f"{ALLOW_REMOTE_ENV_VAR}=1 to allow others.",
        )
    if request.content_type != "application/json":
        return _error(415, "Expected a JSON request.")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "The request body isn't valid JSON.")
    if not isinstance(body, dict):
        return _error(400, "The request body isn't a JSON object.")
    return body


async def _read_request(
    request: web.Request,
) -> tuple[dict[str, Any], dict[str, Any], Path] | web.Response:
    """Checks a request and returns its body, its API-format workflow and the job template."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        workflow = validate_api_workflow(body.get("workflow"), body.get("ui_workflow"))
        template = resolve_job_template()
    except SubmitError as e:
        return _error(400, str(e))
    return body, workflow, template


def _workflow_name(body: dict[str, Any]) -> str:
    name = str(body.get("name") or DEFAULT_WORKFLOW_NAME)
    return name[: -len(".json")] if name.lower().endswith(".json") else name


def _filled_values() -> dict[str, Any]:
    """
    The job parameter values the submitter fills in. WorkflowFile isn't known until the bundle
    exists.
    """
    return {
        "OutputDir": folder_paths.get_output_directory(),
        "InputDir": folder_paths.get_input_directory(),
    }


def _local_assets(workflow: dict[str, Any]) -> LocalAssets:
    """The input files the workflow loads, with the folders its folder loaders read."""
    input_dir = folder_paths.get_input_directory()
    assets = get_local_assets(
        workflow,
        input_dir=input_dir,
        output_dir=folder_paths.get_output_directory(),
        temp_dir=folder_paths.get_temp_directory(),
    )
    assets.directories = sorted({*assets.directories, *input_folders(workflow, Path(input_dir))})
    return assets


def _save_asset_references(
    workflow: dict[str, Any],
    bundle_dir: Path,
    assets: LocalAssets | None = None,
) -> None:
    """Saves the workflow's local input files to asset_references.yaml next to the job template."""
    try:
        chosen = assets if assets is not None else _local_assets(workflow)
        write_asset_references(chosen, bundle_dir / "asset_references.yaml")
    except Exception:
        # The submitter still works without the references. The artist can add files in its dialog.
        logger.exception("Couldn't save asset_references.yaml")


def _start(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _watchers.add(task)
    task.add_done_callback(_watchers.discard)


def _plan(body: dict[str, Any], workflow: dict[str, Any], template: Path) -> dict[str, Any]:
    """Works out the panel's plan. It reads files, so it runs in a thread. Raises SubmitError."""
    conda_channels = resolve_conda_channels(load_config())
    job_template = read_job_template(template)
    assets = _local_assets(workflow)
    return {
        "name": _workflow_name(body),
        "template": template.name,
        "parameters": job_template.get("parameterDefinitions", []),
        "values": _filled_values(),
        "queueValues": ({CONDA_CHANNELS_PARAMETER: conda_channels} if conda_channels else {}),
        "attachments": {
            "filenames": [str(path) for path in assets.filenames],
            "directories": [str(path) for path in assets.directories],
        },
        "warnings": [],
    }


# One plan runs at a time, and one replaced while it waits doesn't run, so slow storage can't pile
# up work.
_plan_lock = asyncio.Lock()
_plan_requests = 0


@PromptServer.instance.routes.post("/deadline/plan")
async def plan_submission(request: web.Request) -> web.Response:
    """Returns what the panel shows for the workflow."""
    global _plan_requests
    checked = await _read_request(request)
    if isinstance(checked, web.Response):
        return checked
    body, workflow, template = checked
    _plan_requests += 1
    this_request = _plan_requests
    async with _plan_lock:
        if this_request != _plan_requests:
            return _error(409, "A newer plan replaced this one.")
        try:
            # Checking files can be slow on network storage, which mustn't hold up ComfyUI's server.
            plan = await asyncio.to_thread(_plan, body, workflow, template)
        except SubmitError as e:
            return _error(400, str(e))
    return web.json_response(plan)


async def _bridge_process(command: str, cli: str | None = None) -> asyncio.subprocess.Process:
    """
    Starts bridge.py with the Python of the Deadline Cloud CLI, or of the configured one when cli
    isn't given. Raises SubmitError.
    """
    python = resolve_cli_python(cli or resolve_deadline_cli(load_config()))
    try:
        return await asyncio.create_subprocess_exec(
            # -I keeps this extension's modules and PYTHONPATH out of the CLI's environment.
            python,
            "-I",
            str(BRIDGE),
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise SubmitError(f"Couldn't run the Deadline Cloud CLI's Python {python}: {e}")


def _parse_event(line: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and isinstance(event.get("event"), str) else None


def _bridge_failure(events: list[dict[str, Any]], stderr: bytes, returncode: int | None) -> str:
    for event in events:
        if event["event"] == "error":
            return str(event.get("message"))
    errors = stderr.decode(errors="replace").strip()
    logger.error("The Deadline Cloud bridge exited with code %s:\n%s", returncode, errors)
    last_line = next((line for line in reversed(errors.splitlines()) if line.strip()), "")
    return f"The Deadline Cloud library failed. {last_line.strip()}".strip()


async def _run_bridge(command: str, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Runs one bridge command and returns its result. Raises SubmitError."""
    process = await _bridge_process(command)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate((json.dumps(request) + "\n").encode()), timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise SubmitError(f"The Deadline Cloud library took longer than {timeout:g} seconds.")
    events = [event for event in map(_parse_event, stdout.splitlines()) if event]
    for event in events:
        if event["event"] == "result" and isinstance(event.get("data"), dict):
            return event["data"]
    raise SubmitError(_bridge_failure(events, stderr, process.returncode))


@PromptServer.instance.routes.post("/deadline/cloud/{command}")
async def call_deadline_cloud(request: web.Request) -> web.Response:
    """Runs a Deadline Cloud library call for the panel."""
    command = request.match_info["command"]
    if command not in CLOUD_COMMANDS:
        return _error(404, f"There's no Deadline Cloud command {command!r}.")
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        result = await _run_bridge(
            command, body, LOGIN_TIMEOUT if command == "login" else BRIDGE_TIMEOUT
        )
    except SubmitError as e:
        return _error(502, str(e))
    return web.json_response(result)


def _string_map(value: Any, what: str) -> dict[str, Any]:
    """Checks a {name: value} object of parameter values or job settings. Raises SubmitError."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(item, (str, int, float, bool)) or item is None for item in value.values()
    ):
        raise SubmitError(f"{what} must be an object of strings and numbers.")
    return {str(key): item for key, item in value.items() if item is not None}


def _attachments(value: Any) -> LocalAssets | None:
    """The input files and folders the artist chose to attach. Raises SubmitError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SubmitError("attachments must be an object.")
    paths = {}
    for key in ("filenames", "directories"):
        listed = value.get(key, [])
        if not isinstance(listed, list) or not all(isinstance(path, str) for path in listed):
            raise SubmitError(f"attachments.{key} must be a list of paths.")
        paths[key] = sorted({Path(path) for path in listed if path.strip()})
    return LocalAssets(paths["filenames"], paths["directories"])


@dataclass
class _Bundle:
    """A job bundle that's ready to submit, with its job parameter values."""

    bundle_dir: Path
    workflow_path: Path
    values: dict[str, Any]


async def _build_bundle(body: dict[str, Any], workflow: dict[str, Any], template: Path) -> _Bundle:
    """Creates the job bundle from the panel's values. Raises SubmitError."""
    parameters = _string_map(body.get("parameters"), "parameters")
    assets = _attachments(body.get("attachments"))
    description = job_description(body.get("description"))

    defined = {
        parameter.get("name")
        for parameter in read_job_template(template).get("parameterDefinitions", [])
    }
    bundle_dir, workflow_path = create_job_bundle(
        template,
        workflow,
        _workflow_name(body),
        Path(folder_paths.get_user_directory()) / "deadline_submissions",
        description=description,
    )
    await asyncio.to_thread(_save_asset_references, workflow, bundle_dir, assets)

    # The artist's values win over the ones the submitter fills in, except the workflow file. The
    # library rejects parameters that neither the job template nor the queue defines.
    filled = {key: value for key, value in _filled_values().items() if key in defined}
    values = {**filled, **parameters, "WorkflowFile": str(workflow_path)}
    return _Bundle(bundle_dir, workflow_path, values)


@PromptServer.instance.routes.post("/deadline/jobs")
async def submit_job(request: web.Request) -> web.Response:
    """
    Creates the job bundle and submits it in the background. The panel follows its progress over the
    websocket.
    """
    checked = await _read_request(request)
    if isinstance(checked, web.Response):
        return checked
    body, workflow, template = checked
    name = _workflow_name(body)
    farm_id, queue_id = body.get("farmId"), body.get("queueId")
    if not (isinstance(farm_id, str) and farm_id and isinstance(queue_id, str) and queue_id):
        return _error(400, "Choose a farm and a queue.")
    try:
        config = load_config()
        settings = _string_map(body.get("settings"), "settings")
    except SubmitError as e:
        return _error(400, str(e))
    cli = await _checked_cli(config)
    if isinstance(cli, web.Response):
        return cli
    try:
        bundle = await _build_bundle(body, workflow, template)
    except SubmitError as e:
        return _error(400, str(e))

    settings.setdefault("name", f"ComfyUI - {name}")
    submission = {
        "bundleDir": str(bundle.bundle_dir),
        "farmId": farm_id,
        "queueId": queue_id,
        "parameters": {key: str(value) for key, value in bundle.values.items()},
        "settings": settings,
    }
    storage_profile = body.get("storageProfileId")
    if isinstance(storage_profile, str):
        submission["storageProfileId"] = storage_profile
    try:
        process = await _bridge_process("submit", cli)
    except SubmitError as e:
        return _error(500, str(e))
    # A bridge that already exited is reported by _watch_submission, like any other failure.
    await _write_line(process, submission)

    # The panel sends its own ID, so it can match events sent before this response reaches it.
    requested_id = body.get("submissionId")
    if (
        isinstance(requested_id, str)
        and _SUBMISSION_ID.fullmatch(requested_id)
        and requested_id not in _submissions
    ):
        submission_id = requested_id
    else:
        submission_id = uuid.uuid4().hex
    _submissions[submission_id] = _Submission(
        str(settings["name"]), process, client_id=_client_id(body)
    )
    logger.info(
        "Submitting %s to Deadline Cloud from %s",
        _log_value(name),
        _log_value(bundle.bundle_dir),
    )
    _start(_watch_submission(submission_id))
    return web.json_response(
        {
            "submissionId": submission_id,
            "job_bundle": str(bundle.bundle_dir),
            "workflow_file": str(bundle.workflow_path),
            "warnings": [],
        },
        status=202,
    )


async def _watch_submission(submission_id: str) -> None:
    """Relays a submission's progress to the panel and reports how it ended."""
    submission = _submissions[submission_id]
    process = submission.process
    assert process.stdout is not None and process.stderr is not None
    base = {"submissionId": submission_id, "name": submission.name}
    events = []
    result = None
    # Read stderr while reading stdout. A child that fills the stderr pipe blocks before it closes
    # stdout, so reading stderr only after stdout ends would wait forever.
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        async for line in process.stdout:
            event = _parse_event(line)
            if event is None:
                continue
            events.append(event)
            kind = event["event"]
            data = {key: value for key, value in event.items() if key != "event"}
            if kind in ("progress", "message"):
                _send(PROGRESS_EVENT, {**base, **data, "kind": kind}, submission.client_id)
            elif kind == "confirm":
                submission.confirms += 1
                _send(CONFIRM_EVENT, {**base, **data}, submission.client_id)
                submission.timers.append(
                    asyncio.create_task(
                        _decline_unanswered(submission_id, submission, submission.confirms)
                    )
                )
            elif kind == "result":
                result = event.get("data")
        stderr = await stderr_task
        await process.wait()
    finally:
        stderr_task.cancel()
        for timer in submission.timers:
            timer.cancel()
        _submissions.pop(submission_id, None)

    # A job the bridge created is reported even when Cancel came too late to stop it.
    if isinstance(result, dict) and result.get("jobId"):
        logger.info(
            "Submitted %s to Deadline Cloud as %s",
            _log_value(submission.name),
            _log_value(result["jobId"]),
        )
        _send(SUBMITTED_EVENT, {**base, **result, "status": "SUBMITTED"}, submission.client_id)
    elif submission.canceled:
        _send(SUBMITTED_EVENT, {**base, "status": "CANCELED"}, submission.client_id)
    else:
        error = _bridge_failure(events, stderr, process.returncode)
        logger.error(
            "Couldn't submit %s to Deadline Cloud: %s",
            _log_value(submission.name),
            _log_block(str(error)),
        )
        _send(SUBMITTED_EVENT, {**base, "status": "FAILED", "error": error}, submission.client_id)


async def _write_line(process: asyncio.subprocess.Process, data: dict[str, Any]) -> bool:
    """Sends the bridge a JSON line. Returns False when it has already exited."""
    assert process.stdin is not None
    try:
        process.stdin.write((json.dumps(data) + "\n").encode())
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        return False
    return True


async def _decline_unanswered(submission_id: str, submission: _Submission, number: int) -> None:
    """Declines the bridge's confirmation `number` if it's still unanswered after the timeout."""
    await asyncio.sleep(CONFIRM_TIMEOUT)
    if _submissions.get(submission_id) is not submission or submission.answered >= number:
        return
    logger.warning(
        "Nobody answered the confirmation of %s within %g seconds, so it isn't submitted.",
        _log_value(submission.name),
        CONFIRM_TIMEOUT,
    )
    submission.answered = number
    await _write_line(submission.process, {"accept": False})


def _running_submission(body: dict[str, Any]) -> _Submission | web.Response:
    submission = _submissions.get(str(body.get("submissionId")))
    if submission is None:
        return _error(404, "That submission isn't running.")
    return submission


@PromptServer.instance.routes.post("/deadline/jobs/confirm")
async def confirm_submission(request: web.Request) -> web.Response:
    """Answers a submission's confirmation."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    submission = _running_submission(body)
    if isinstance(submission, web.Response):
        return submission
    submission.answered = submission.confirms
    if not await _write_line(submission.process, {"accept": body.get("accept") is True}):
        return _error(404, "That submission isn't running.")
    return web.json_response({})


@PromptServer.instance.routes.post("/deadline/jobs/cancel")
async def cancel_submission(request: web.Request) -> web.Response:
    """Stops a submission. A job it already created stays."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    submission = _running_submission(body)
    if isinstance(submission, web.Response):
        return submission
    submission.canceled = True
    try:
        submission.process.kill()
    except ProcessLookupError:
        # It already exited. _watch_submission reports how it ended.
        pass
    return web.json_response({})


_picker_lock = asyncio.Lock()


@PromptServer.instance.routes.post("/deadline/choose")
async def choose_paths(request: web.Request) -> web.Response:
    """Opens a native file or folder dialog on the ComfyUI machine and returns the chosen paths."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    kind = body.get("kind")
    if kind not in PICKER_KINDS:
        return _error(400, f"kind must be one of {', '.join(PICKER_KINDS)}.")
    command = picker_command(kind)
    if command is None:
        return _error(
            501,
            "There's no file dialog on the ComfyUI machine. On Linux, install zenity.",
        )
    if _picker_lock.locked():
        return _error(409, "A file dialog is already open on the ComfyUI machine.")
    async with _picker_lock:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
        except OSError as e:
            return _error(500, f"Couldn't open the file dialog: {e}")
    # Canceling the dialog exits with an error and prints nothing.
    paths = [line.strip() for line in stdout.decode(errors="replace").splitlines()]
    if process.returncode != 0:
        paths = []
    return web.json_response({"paths": [str(Path(path)) for path in paths if path]})
