# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Runs the comfy-cli job template's task script with a stand-in `comfy` command. The script runs
comfy-cli twice: once to print the workflow in API format, and once to run it, which the stand-in
saves for the test to read. Also runs the part of the server environment's Enter script that
writes extra_model_paths.yaml.
"""

import http.server
import json
import os
import shlex
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from deadline.comfyui_submitter import submit

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The stand-in comfy is a POSIX script")

TEMPLATE = yaml.safe_load(submit.JOB_TEMPLATE.read_text())


def run_task(
    tmp_path: Path,
    workflow: dict,
    *,
    mode: str = "",
    workflow_timeout: int = 3600,
    port: int = 8188,
) -> tuple[int, str, dict]:
    """
    Runs the task script and returns its exit code, its output and the workflow it ran. The
    stand-in comfy prints JSON with non-ASCII text unescaped, as comfy-cli does, and logs its
    arguments to calls.jsonl. mode changes what it does: "no-envelope" prints no envelope, and
    "hang" never finishes the run.
    """
    for name in ("bin", "out", "run"):
        (tmp_path / name).mkdir(exist_ok=True)
    workflow_file = tmp_path / "workflow_api.json"
    workflow_file.write_text(json.dumps(workflow))
    ran = tmp_path / "ran.json"
    stand_in = tmp_path / "comfy.py"
    stand_in.write_text(textwrap.dedent(f"""\
        import json, sys, time
        sys.stdout.reconfigure(encoding="utf-8")
        args = sys.argv
        with open({str(tmp_path / "calls.jsonl")!r}, "a", encoding="utf-8") as log:
            log.write(json.dumps(args[1:]) + "\\n")
        workflow = json.load(open(args[args.index("--workflow") + 1], encoding="utf-8"))
        def emit(event):
            print(json.dumps(event, ensure_ascii=False))
        if {mode!r} == "no-envelope":
            print("not json")
        elif "--print-prompt" in args:
            emit({{"type": "prompt_preview", "prompt": workflow}})
            emit({{"type": "envelope", "ok": True, "data": {{"prompt": workflow}}}})
        elif {mode!r} == "hang":
            time.sleep(60)
        else:
            json.dump(workflow, open({str(ran)!r}, "w", encoding="utf-8"), ensure_ascii=False)
            open({str(tmp_path / "out" / "image.png")!r}, "w").write("x")
            emit({{"type": "envelope", "ok": True, "data": {{"prompt": workflow}}}})
        """))
    # A shell wrapper rather than a shebang, which breaks when the interpreter path has spaces.
    comfy = tmp_path / "bin" / "comfy"
    comfy.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stand_in))} "$@"\n'
    )
    comfy.chmod(0o755)
    script = (
        TEMPLATE["steps"][0]["script"]["embeddedFiles"][0]["data"]
        .replace("{{Param.OutputDir}}", str(tmp_path / "out"))
        .replace("{{Param.WorkflowFile}}", str(workflow_file))
        .replace("{{Param.WorkflowTimeout}}", str(workflow_timeout))
    )
    (tmp_path / "run.py").write_text(script, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "COMFYUI_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, str(tmp_path / "run.py")],
        cwd=tmp_path / "run",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    workflow_ran = json.loads(ran.read_text(encoding="utf-8")) if ran.exists() else {}
    ran.unlink(missing_ok=True)
    return result.returncode, result.stdout + result.stderr, workflow_ran


def comfy_calls(tmp_path: Path) -> list[list[str]]:
    """The arguments of each comfy command the task script ran."""
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def enter_script(
    tmp_path: Path,
    end: str,
    model_directory: Path | None = None,
    extra: str = "",
    custom_nodes_directory: Path | None = None,
    attach_nodes: bool = True,
) -> str:
    """The start of the server environment's Enter script, with the job's values filled in."""
    [environment] = TEMPLATE["jobEnvironments"]
    [enter] = [f for f in environment["script"]["embeddedFiles"] if f["name"] == "Enter"]
    data = enter["data"]
    return (
        data[data.index("model_folders() {") : data.index(end)]
        .replace("{{Session.WorkingDirectory}}", str(tmp_path))
        .replace("{{Param.ModelDirectory}}", str(model_directory or ""))
        .replace("{{Param.ExtraModelPathsConfig}}", extra)
        .replace("{{Param.CustomNodesDirectory}}", str(custom_nodes_directory or ""))
        .replace("{{Param.AttachCustomNodes}}", "true" if attach_nodes else "false")
    )


def write_model_paths(
    tmp_path: Path,
    model_directory: Path | None = None,
    conda_nodes: Path | None = None,
    custom_nodes_directory: Path | None = None,
    attach_nodes: bool = True,
) -> Any:
    """
    Runs the part of the Enter script that writes extra_model_paths.yaml, and returns the config
    it wrote.
    """
    script = enter_script(
        tmp_path,
        "# ComfyUI can't read an empty",
        model_directory,
        custom_nodes_directory=custom_nodes_directory,
        attach_nodes=attach_nodes,
    )
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        env={
            "PATH": os.environ["PATH"],
            **({"COMFYUI_CUSTOM_NODES_DIR": str(conda_nodes)} if conda_nodes else {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load((tmp_path / "extra_model_paths.yaml").read_text())


def test_model_paths_map_every_attached_model_folder(tmp_path):
    """Folders outside the common ones, such as style_models, are mapped too."""
    attached = tmp_path / "attached models"
    for folder in (attached / "loras", attached / "style_models"):
        folder.mkdir(parents=True)
    # Not a model folder type name, so it's left out rather than written as a YAML key.
    (attached / "bad: name").mkdir()
    assert write_model_paths(tmp_path, attached) == {
        "model_directory": {
            "base_path": str(attached),
            "loras": "loras/",
            "style_models": "style_models/",
        },
    }


def test_model_paths_are_empty_without_models_or_conda_custom_nodes(tmp_path):
    """The file stays empty, and the Enter script then doesn't pass it to ComfyUI."""
    assert write_model_paths(tmp_path) is None
    assert write_model_paths(tmp_path, tmp_path / "missing") is None


def test_model_paths_add_the_conda_environments_custom_nodes(tmp_path):
    conda_nodes = tmp_path / "conda custom_nodes"
    conda_nodes.mkdir()
    assert write_model_paths(tmp_path, conda_nodes=conda_nodes) == {
        "conda_custom_nodes": {"custom_nodes": str(conda_nodes)}
    }


def test_model_paths_add_the_attached_custom_nodes(tmp_path):
    attached = tmp_path / "attached nodes"
    (attached / "studio-nodes").mkdir(parents=True)
    assert write_model_paths(tmp_path, custom_nodes_directory=attached) == {
        "attached_custom_nodes": {"custom_nodes": str(attached)}
    }


def test_model_paths_leave_out_attached_custom_nodes_when_attaching_is_off(tmp_path):
    """The workers then provide every custom node, for example from a conda package."""
    attached = tmp_path / "attached nodes"
    (attached / "studio-nodes").mkdir(parents=True)
    assert write_model_paths(tmp_path, custom_nodes_directory=attached, attach_nodes=False) is None


def model_path_args(
    tmp_path: Path,
    extra: str = "",
    conda_nodes: Path | None = None,
    model_directory: Path | None = None,
) -> list[str]:
    """The extra model paths arguments the Enter script passes to ComfyUI."""
    script = enter_script(tmp_path, "# Keep ComfyUI's data out", model_directory, extra)
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + script
            + '\necho ARGS; printf "%s\\n" ${MODEL_PATH_ARGS[@]+"${MODEL_PATH_ARGS[@]}"}',
        ],
        env={
            "PATH": os.environ["PATH"],
            **({"COMFYUI_CUSTOM_NODES_DIR": str(conda_nodes)} if conda_nodes else {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split("ARGS\n", 1)[1].split()


def test_an_empty_extra_model_paths_file_isnt_passed_to_comfyui(tmp_path):
    """ComfyUI can't read an empty extra_model_paths.yaml."""
    assert model_path_args(tmp_path) == []
    studio = "/mnt/studio/extra_model_paths.yaml"
    assert model_path_args(tmp_path, studio) == ["--extra-model-paths-config", studio]


def test_conda_custom_nodes_and_the_studio_config_are_both_passed(tmp_path):
    conda_nodes = tmp_path / "conda_nodes"
    conda_nodes.mkdir()
    studio = "/mnt/studio/extra_model_paths.yaml"
    assert model_path_args(tmp_path, studio, conda_nodes) == [
        "--extra-model-paths-config",
        str(tmp_path / "extra_model_paths.yaml"),
        studio,
    ]


def test_attached_models_and_the_studio_config_are_both_passed(tmp_path):
    attached = tmp_path / "models"
    (attached / "loras").mkdir(parents=True)
    studio = "/mnt/studio/extra_model_paths.yaml"
    assert model_path_args(tmp_path, studio, model_directory=attached) == [
        "--extra-model-paths-config",
        str(tmp_path / "extra_model_paths.yaml"),
        studio,
    ]


# Line and paragraph separators that str.splitlines() also splits on, and other non-ASCII text.
UNUSUAL_TEXT = "final fade.   \x85\x0b\x0c\x1c — café, naïve 🦊"


def _prompt(text=UNUSUAL_TEXT, prefix="fox — café 🦊"):
    return {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": text}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix}},
    }


def test_prompts_with_line_separators_and_other_unicode_run(tmp_path):
    """comfy-cli prints them unescaped, inside one JSON line that splitlines() would break up."""
    code, output, ran = run_task(tmp_path, _prompt())
    assert code == 0, output
    assert ran["6"]["inputs"]["text"] == UNUSUAL_TEXT
    assert ran["9"]["inputs"]["filename_prefix"] == "fox — café 🦊"
    [preview, run] = comfy_calls(tmp_path)
    assert "--print-prompt" in preview and "--wait" in run


def test_output_without_an_envelope_says_so(tmp_path):
    code, output, _ = run_task(tmp_path, _prompt("fox"), mode="no-envelope")
    assert code == 1
    assert "openjd_fail: comfy run printed no result (exit code 0)" in output


def test_the_run_waits_as_long_as_the_workflow_timeout(tmp_path):
    """comfy run's default 120 second wait for progress is shorter than loading a large model."""
    code, output, _ = run_task(tmp_path, _prompt("fox"), workflow_timeout=5400)
    assert code == 0, output
    [preview, run] = comfy_calls(tmp_path)
    assert "--timeout" not in preview
    assert run[run.index("--timeout") + 1] == "5400"


def test_a_workflow_past_its_timeout_is_interrupted(tmp_path):
    interrupts = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            interrupts.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, output, _ = run_task(
            tmp_path, _prompt("fox"), mode="hang", workflow_timeout=2, port=server.server_port
        )
    finally:
        server.shutdown()
    assert code == 1
    assert "didn't finish within the Workflow Timeout of 2 seconds" in output
    assert interrupts == ["/interrupt"]
