# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from pathlib import Path

import pytest
import yaml

from deadline.comfyui_submitter.get_local_assets import (
    LocalAssets,
    get_local_assets,
    write_asset_references,
)


@pytest.fixture
def comfy(tmp_path):
    """ComfyUI's input, output, and model directories."""
    root = tmp_path / "ComfyUI"
    dirs = {
        "input": root / "input",
        "output": root / "output",
        "checkpoints": root / "models" / "checkpoints",
        "loras": root / "models" / "loras",
        "diffusers": root / "models" / "diffusers",
        "custom_nodes": root / "custom_nodes",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True)
    return dirs


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def assets(workflow, comfy):
    return get_local_assets(workflow, input_dir=comfy["input"], output_dir=comfy["output"])


def node(**inputs):
    return {"class_type": "Node", "inputs": inputs}


def test_finds_input_files_and_skips_models(comfy):
    touch(comfy["checkpoints"] / "sd" / "v1-5.safetensors")
    touch(comfy["loras"] / "style.safetensors")
    image = touch(comfy["input"] / "cat.png")
    video = touch(comfy["input"] / "clips" / "run.mp4")
    workflow = {
        "1": node(ckpt_name="sd/v1-5.safetensors"),
        "2": node(lora_name="style.safetensors", strength=0.5, model=["1", 0]),
        "3": node(image="cat.png"),
        "4": node(video="clips/run.mp4"),
        "5": node(text="a cat riding a horse"),
    }
    assert assets(workflow, comfy) == LocalAssets(sorted([image, video]), [])


def test_ignores_missing_files_and_unknown_extensions(comfy):
    touch(comfy["checkpoints"] / "notes.txt")
    touch(comfy["input"] / "data.csv")
    workflow = {
        "1": node(ckpt_name="notes.txt"),
        "2": node(image="missing.png"),
        "3": node(ckpt_name="gone.ckpt"),
        "4": node(file="data.csv"),
        "5": node(image="notes.png"),
    }
    assert not assets(workflow, comfy)


def test_annotated_values(comfy):
    image = touch(comfy["input"] / "a.png")
    out = touch(comfy["output"] / "b.png")
    workflow = {"1": node(image="a.png [input]"), "2": node(image="b.png [output]")}
    assert assets(workflow, comfy).filenames == sorted([image, out])


def test_invalid_annotated_value(comfy):
    assert not assets({"1": node(image="a.png [temp]")}, comfy)


def test_skips_model_files_and_directories(comfy, tmp_path):
    touch(comfy["checkpoints"] / "a.safetensors")
    (comfy["diffusers"] / "sd-turbo").mkdir()
    absolute = touch(tmp_path / "vae" / "ae.safetensors")
    workflow = {
        "1": node(ckpt_name="a.safetensors", model_path="sd-turbo", vae=str(absolute)),
    }
    assert not assets(workflow, comfy)


def test_absolute_paths(comfy, tmp_path):
    video = touch(tmp_path / "elsewhere" / "v.mov")
    other = touch(tmp_path / "elsewhere" / "notes.txt")
    workflow = {"1": node(video=str(video), file=str(other), folder=str(tmp_path))}
    assert assets(workflow, comfy) == LocalAssets([video], [])


def test_rejects_paths_that_leave_the_directory(comfy, tmp_path):
    touch(tmp_path / "secret.png")
    workflow = {
        "1": node(image="../../secret.png"),
        "2": node(image="../ComfyUI/input/../../x.png"),
    }
    assert not assets(workflow, comfy)


def test_tolerates_odd_values(comfy):
    workflow = {
        "1": node(a="", b="x" * 5000, c="line\nbreak.png", d="nul\0.png", e=None, f=3, g=[1, 2]),
        "2": "not a node",
        "3": {"class_type": "X"},
    }
    assert not assets(workflow, comfy)


def test_deduplicates(comfy):
    image = touch(comfy["input"] / "cat.png")
    workflow = {"1": node(image="cat.png"), "2": node(image="cat.png [input]")}
    assert assets(workflow, comfy).filenames == [image]


def test_asset_references_yaml_format(tmp_path):
    assets = LocalAssets(
        [tmp_path / "a b.png", tmp_path / 'q"uote: é.ckpt'], [tmp_path / "models" / "dir"]
    )
    path = write_asset_references(assets, tmp_path / "asset_references.yaml")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "assetReferences": {
            "inputs": {
                "filenames": [str(p) for p in assets.filenames],
                "directories": [str(tmp_path / "models" / "dir")],
            },
            "outputs": {"directories": []},
            "referencedPaths": [],
        }
    }
