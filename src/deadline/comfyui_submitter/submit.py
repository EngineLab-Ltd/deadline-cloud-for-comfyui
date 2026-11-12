# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Helpers for routes.py that don't import ComfyUI, so they can be unit tested."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TEMPLATE_ENV_VAR = "COMFYUI_DEADLINE_JOB_TEMPLATE"
CLI_ENV_VAR = "COMFYUI_DEADLINE_CLI"
# The Python the submitter panel's bridge runs with. By default, the Deadline Cloud CLI's own.
PYTHON_ENV_VAR = "COMFYUI_DEADLINE_PYTHON"
ALLOW_REMOTE_ENV_VAR = "COMFYUI_DEADLINE_ALLOW_REMOTE"
CONDA_CHANNELS_ENV_VAR = "COMFYUI_DEADLINE_CONDA_CHANNELS"
CONFIG_ENV_VAR = "COMFYUI_DEADLINE_CONFIG"

CONFIG_FILE_NAME = "deadline_cloud_submitter.json"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / CONFIG_FILE_NAME

JOB_TEMPLATE = Path(__file__).parent / "comfy_cli_job_template.yaml"
DEFAULT_WORKFLOW_NAME = "ComfyUI workflow"

# The oldest CLI the submitter supports. Older libraries don't read BOOL job parameters.
MIN_CLI_VERSION = (0, 60, 8)
# The first botocore whose Deadline Cloud API can send BOOL job parameters. The CLI accepts older
# ones, which only fail when they create the job.
MIN_BOTOCORE_VERSION = (1, 43, 105)


logger = logging.getLogger(__name__)


class SubmitError(Exception):
    """An error whose message is shown to the artist in ComfyUI."""


def resolve_job_template() -> Path:
    override = os.environ.get(TEMPLATE_ENV_VAR)
    if override:
        template = Path(override)
        if not template.is_file() or template.suffix.lower() not in (".yaml", ".yml", ".json"):
            raise SubmitError(
                f"{TEMPLATE_ENV_VAR} is set to {template}, which isn't a YAML or JSON job template."
            )
        return template
    return JOB_TEMPLATE


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)


def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SubmitError(f"Couldn't read the submitter config {path}: {e}")
    if not isinstance(config, dict):
        raise SubmitError(f"The submitter config {path} isn't a JSON object.")
    return config


def resolve_deadline_cli(config: dict[str, Any] | None = None) -> str:
    """A relative deadline_cli in the config is relative to the config file."""
    configured = os.environ.get(CLI_ENV_VAR)
    source = CLI_ENV_VAR
    if not configured and (config or {}).get("deadline_cli"):
        configured = str(config_path().parent / str((config or {})["deadline_cli"]))
        source = f"deadline_cli in {config_path()}"
    if configured:
        if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
            raise SubmitError(f"{source} is set to {configured}, which isn't an executable file.")
        return configured
    found = shutil.which("deadline")
    if not found:
        raise SubmitError(
            "The Deadline Cloud CLI wasn't found. Run install_submitter.py to set it up, or install "
            f'it into its own Python environment with pip install "deadline[gui]>=0.60.8" and put '
            f"it on the PATH or set {CLI_ENV_VAR} to its `deadline` executable."
        )
    return found


# pip writes this launcher instead of a shebang when the interpreter's path has spaces.
_EXEC_LAUNCHER = re.compile(r"^'''exec' \"?(?P<python>[^\"\n]+?)\"? \"\$0\" \"\$@\"$", re.MULTILINE)


def resolve_cli_python(cli: str) -> str:
    """
    Returns the Python the Deadline Cloud CLI runs with, from its shebang or pip launcher, or
    otherwise the python next to it.
    """
    configured = os.environ.get(PYTHON_ENV_VAR)
    if configured:
        if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
            raise SubmitError(
                f"{PYTHON_ENV_VAR} is set to {configured}, which isn't an executable file."
            )
        return configured
    candidates = []
    try:
        with open(cli, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
    except OSError:
        head = ""
    launcher = _EXEC_LAUNCHER.search(head)
    if launcher:
        candidates.append(launcher["python"])
    elif head.startswith("#!"):
        candidates.append(head[2:].splitlines()[0].strip())
    directory = Path(cli).parent
    candidates += [str(directory / name) for name in ("python", "python3", "python.exe")]
    for candidate in candidates:
        if os.path.basename(candidate).lower().startswith("python") and os.path.isfile(candidate):
            return candidate
    raise SubmitError(
        f"Couldn't find the Python that runs the Deadline Cloud CLI {cli}. Set {PYTHON_ENV_VAR} "
        "to it."
    )


PICKER_KINDS = ("files", "folder")
_PICKER_PROMPTS = {
    "files": "Choose files to attach to the Deadline Cloud job",
    "folder": "Choose a folder to attach to the Deadline Cloud job",
}


def picker_command(kind: str, platform: str = sys.platform) -> list[str] | None:
    """
    Returns the command for a native file or folder dialog, which prints one absolute path per line,
    or None when there isn't one. The browser only chooses the kind.
    """
    prompt = _PICKER_PROMPTS[kind]
    if platform == "darwin":
        if kind == "folder":
            return ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")']
        return [
            "osascript",
            "-e",
            f'set chosen to choose file with prompt "{prompt}" with multiple selections allowed',
            "-e",
            'set output to ""',
            "-e",
            "repeat with item_ in chosen",
            "-e",
            "set output to output & POSIX path of item_ & linefeed",
            "-e",
            "end repeat",
            "-e",
            "return output",
        ]
    if platform == "win32":
        # PowerShell writes in the console code page unless told otherwise, and the server reads
        # the paths as UTF-8, so paths with accents or dashes would come back garbled.
        utf8 = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        if kind == "folder":
            script = (
                utf8 + "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description='{prompt}';"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"
            )
        else:
            script = (
                utf8 + "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                f"$d.Title='{prompt}';$d.Multiselect=$true;"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write(($d.FileNames -join \"`n\"))}"
            )
        return ["powershell", "-NoProfile", "-STA", "-Command", script]
    if shutil.which("zenity"):
        command = ["zenity", "--file-selection", f"--title={prompt}"]
        return command + (["--directory"] if kind == "folder" else ["--multiple", "--separator=\n"])
    return None


def resolve_conda_channels(config: dict[str, Any] | None = None) -> str | None:
    channels = os.environ.get(CONDA_CHANNELS_ENV_VAR) or (config or {}).get("conda_channels")
    if not channels:
        return None
    return " ".join(str(channels).split()) or None


def parse_cli_version(output: str) -> tuple[int, ...] | None:
    """Parses `deadline --version` output, for example "deadline, version 0.60.10"."""
    match = re.search(r"version (\d+)\.(\d+)\.(\d+)", output)
    return tuple(int(part) for part in match.groups()) if match else None


def check_cli_version(cli: str, version_output: str) -> None:
    version = parse_cli_version(version_output)
    # Let an unrecognized version through rather than block submissions on a format change.
    if version is not None and version < MIN_CLI_VERSION:
        minimum = ".".join(map(str, MIN_CLI_VERSION))
        raise SubmitError(
            f"The Deadline Cloud CLI at {cli} is version {'.'.join(map(str, version))}, which can't "
            f"open the submitter with the workflow filled in. Upgrade it to {minimum} or later with "
            f'pip install --upgrade "deadline[gui]>={minimum}".'
        )


def remote_allowed() -> bool:
    return os.environ.get(ALLOW_REMOTE_ENV_VAR, "").lower() in ("1", "true", "yes")


def _describe_missing_node(node_id: str, ui_workflow: Any) -> str:
    # A node inside a subgraph has an API ID like "12:137"; its UI ID is the last part.
    ui_id = node_id.rsplit(":", 1)[-1]
    for node in _workflow_nodes(ui_workflow):
        if str(node.get("id")) != ui_id:
            continue
        description = f"{node_id} ({node.get('type')}"
        properties = node.get("properties")
        pack = properties.get("cnr_id") if isinstance(properties, dict) else None
        if isinstance(pack, str):
            description += f", from the {pack} node pack"
        return description + ")"
    return node_id


# Job bundles hold the artist's workflow, prompts and file paths, so only the user that runs
# ComfyUI can open them. On Windows, files inherit the ACLs of the user directory instead.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def make_private_dir(path: Path) -> None:
    """Creates a directory, if it's missing, and limits it to its owner."""
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    # mkdir's mode goes through the umask, and an existing directory keeps its own.
    os.chmod(path, PRIVATE_DIR_MODE)


def _private_opener(path: str, flags: int) -> int:
    """Opens a new file with only its owner's permissions."""
    return os.open(path, flags, PRIVATE_FILE_MODE)


def write_private_text(path: Path, text: str) -> None:
    """Writes a file that only its owner can read, whatever the umask."""
    with open(path, "w", encoding="utf-8", opener=_private_opener) as file:
        file.write(text)
    # An existing file keeps its mode when it's opened.
    os.chmod(path, PRIVATE_FILE_MODE)


def validate_api_workflow(workflow: Any, ui_workflow: Any = None) -> dict[str, Any]:
    if not isinstance(workflow, dict) or not workflow:
        raise SubmitError("The workflow is empty.")
    if "nodes" in workflow and "links" in workflow:
        raise SubmitError("The workflow is in UI format. The submitter needs the API format.")
    # ComfyUI leaves out class_type for a node type it has no definition for, so the node can't
    # be turned into the API format.
    missing = [
        _describe_missing_node(str(node_id), ui_workflow)
        for node_id, node in workflow.items()
        if not isinstance(node, dict) or "class_type" not in node
    ]
    if missing:
        raise SubmitError(
            f"This ComfyUI can't convert node {', '.join(missing)} because its node type isn't "
            "installed. Install the missing node packs and restart ComfyUI. Manager > Install "
            "Missing Custom Nodes finds packs from the Comfy Registry. A node with no pack named "
            "here isn't from the registry, so get its code from whoever made the workflow."
        )
    # ComfyUI always writes these as a string and an object, so anything else didn't come from it.
    for node_id, node in workflow.items():
        if not isinstance(node["class_type"], str) or not node["class_type"]:
            raise SubmitError(f"Node {_quoted(node_id)} has no node type in the workflow.")
        if not isinstance(node.get("inputs", {}), dict):
            raise SubmitError(f"Node {_quoted(node_id)} has inputs that aren't an object.")
    return workflow


def _quoted(value: Any) -> str:
    """A value from a request, shortened and on one line, to quote in an error message."""
    text = re.sub(r"\s+", " ", str(value))
    return text if len(text) <= 40 else text[:39] + "…"


# Core nodes that load every image in a folder of the input directory as a list.
FOLDER_LOADERS = ("LoadImageDataSetFromFolder",)
# Nodes whose folder input names a folder of the input directory, which is sent with the job.
FOLDER_INPUT_NODES = (*FOLDER_LOADERS, "LoadImageTextDataSetFromFolder")


def input_path(input_dir: Path | str, name: Any) -> Path | None:
    """
    The file or folder in ComfyUI's input directory that a node input names, such as "fox.png",
    "fox.png [input]" or "dataset/cats", or None if it names a path outside it.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    if name.endswith(" [input]"):
        name = name[: -len(" [input]")]
    elif re.search(r" \[(output|temp)\]$", name):
        return None
    # ComfyUI checks paths this way too, without resolving symbolic links.
    base = Path(os.path.abspath(input_dir))
    path = Path(os.path.abspath(base / name))
    return path if path != base and path.is_relative_to(base) else None


def input_folders(workflow: dict[str, Any], input_dir: Path) -> list[Path]:
    """
    Returns the folders in ComfyUI's input directory that the workflow's folder loaders read.
    """
    folders = set()
    for node in workflow.values():
        if node.get("class_type") not in FOLDER_INPUT_NODES:
            continue
        path = input_path(input_dir, node.get("inputs", {}).get("folder"))
        if path is not None and path.is_dir():
            folders.add(path)
    return sorted(folders)


def _dump_template(job_template: dict[str, Any]) -> str:
    """Writes a job template as YAML, with multiline strings, such as scripts, as literal blocks."""
    import yaml

    class TemplateDumper(yaml.SafeDumper):
        pass

    TemplateDumper.add_representer(
        str,
        lambda dumper, value: dumper.represent_scalar(
            "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
        ),
    )
    return yaml.dump(job_template, Dumper=TemplateDumper, sort_keys=False, width=100)


def _workflow_nodes(value: Any):
    """Yields every node of a UI-format workflow, including the nodes inside subgraphs."""
    if not isinstance(value, dict):
        return
    for node in value.get("nodes") or []:
        if isinstance(node, dict):
            yield node
    definitions = value.get("definitions")
    if isinstance(definitions, dict):
        for subgraph in definitions.get("subgraphs") or []:
            yield from _workflow_nodes(subgraph)


def safe_name(name: str) -> str:
    stem = name[: -len(".json")] if name.lower().endswith(".json") else name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem[:100] or "workflow"


def get_safe_path(user_input: str, base_dir: Path | str) -> Path:
    """
    user_input resolved against base_dir. Raises ValueError when the resolved path would fall
    outside base_dir, for example through ".." segments or a symlink, or when it is base_dir
    itself. realpath resolves both ".." and symlinks, and the boundary check compares against
    base_dir plus a path separator so a sibling such as "base_dir_evil" doesn't pass for a
    child of base_dir.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, user_input))
    if not target.startswith(base + os.sep):
        raise ValueError("Access Denied: Path injection attempt detected.")
    return Path(target)


def read_job_template(template: Path) -> dict[str, Any]:
    """Reads a YAML or JSON job template."""
    import yaml

    try:
        text = template.read_text(encoding="utf-8")
        base = json.loads(text) if template.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise SubmitError(f"Couldn't read the job template {template}: {e}")
    if not isinstance(base, dict):
        raise SubmitError(f"The job template {template} isn't a YAML or JSON object.")
    return base


MAX_JOB_DESCRIPTION = 2048


def job_description(value: Any) -> str | None:
    """Checks the panel's job description. Empty keeps the job template's. Raises SubmitError."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SubmitError("description must be a string.")
    if len(value) > MAX_JOB_DESCRIPTION:
        raise SubmitError(f"The job description can't be over {MAX_JOB_DESCRIPTION} characters.")
    return value


def create_job_bundle(
    template: Path,
    workflow: dict[str, Any],
    name: str,
    dest_dir: Path,
    now: datetime | None = None,
    description: str | None = None,
) -> tuple[Path, Path]:
    """
    Creates <dest_dir>/<timestamp>_<name>/ with the job template and the API-format workflow, and
    returns the bundle directory and the workflow file. They have to outlive the submitter dialog
    and the job attachments upload. The template is copied as it is, unless the artist changed its
    description.
    """
    suffix = ".json" if template.suffix.lower() == ".json" else ".yaml"
    text = None
    if description:
        job_template = read_job_template(template)
        job_template["description"] = description
        text = (
            json.dumps(job_template, indent=2)
            if suffix == ".json"
            else _dump_template(job_template)
        )
    now = now or datetime.now(UTC)
    stem = safe_name(name)
    leaf = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{stem}"
    # The name comes from the request. safe_name strips traversal, and get_safe_path checks again
    # that the bundle and workflow file land inside dest_dir.
    try:
        bundle_dir = get_safe_path(leaf, dest_dir)
    except ValueError as e:
        raise SubmitError(str(e)) from e
    # Everything in the bundle is only reachable through these directories, whatever its own
    # permissions.
    make_private_dir(dest_dir)
    bundle_dir.mkdir(mode=PRIVATE_DIR_MODE)
    os.chmod(bundle_dir, PRIVATE_DIR_MODE)
    if text is None:
        text = template.read_text(encoding="utf-8")
    write_private_text(bundle_dir / f"template{suffix}", text)
    try:
        workflow_path = get_safe_path(f"{stem}_api.json", bundle_dir)
    except ValueError as e:
        raise SubmitError(str(e)) from e
    write_private_text(workflow_path, json.dumps(workflow, indent=2))
    return bundle_dir, workflow_path
