# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import shutil
from pathlib import Path

import pytest
import yaml

from deadline.comfyui_submitter.get_local_assets import (
    LocalAssets,
    find_local_model,
    find_workflow_local_models,
    get_local_assets,
    stage_local_models,
    write_asset_references,
)
from deadline.comfyui_submitter.submit import MODEL_EXTENSIONS


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


def test_find_local_model_searches_registered_directories(comfy, tmp_path):
    extra = tmp_path / "extra" / "vae"
    expected = touch(extra / "nested" / "ae.safetensors")
    touch(comfy["checkpoints"] / "ae.safetensors")
    folders = {
        "vae": ([str(comfy["checkpoints"]), str(extra)], {".safetensors"}),
    }
    assert find_local_model(folders, "vae", "nested/ae.safetensors") == expected
    assert find_local_model(folders, "vae", "../secret.safetensors") is None
    assert find_local_model(folders, "loras", "ae.safetensors") is None


def test_find_workflow_local_models_skips_missing_files(comfy, caplog):
    path = touch(comfy["checkpoints"] / "custom.safetensors")
    folders = {"checkpoints": ([str(comfy["checkpoints"])], {".safetensors"})}
    ui = {
        "nodes": [
            {
                "properties": {
                    "models": [
                        {"directory": "checkpoints", "name": "custom.safetensors"},
                        {"directory": "checkpoints", "name": "gone.safetensors"},
                    ]
                }
            }
        ]
    }
    api = {
        "1": {
            "inputs": {"ckpt_name": "custom.safetensors", "lora_name": "gone.safetensors"},
        }
    }
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {"custom.safetensors": path}
    assert directories == {"custom.safetensors": "checkpoints"}
    assert "gone.safetensors" in caplog.text


def test_stage_local_models_skips_a_file_that_cannot_be_placed(comfy, tmp_path, monkeypatch):
    kept = touch(comfy["checkpoints"] / "kept.safetensors")
    dropped = touch(comfy["loras"] / "dropped.safetensors")

    copy_file = shutil.copy2

    def fail_link(source, target):
        raise OSError("link")

    def fail_copy(source, target):
        if Path(source).name == "dropped.safetensors":
            raise OSError("disk full")
        copy_file(source, target)

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.os.link", fail_link)
    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.shutil.copy2", fail_copy)
    dest = tmp_path / "local_models"
    staged = stage_local_models(
        {"kept.safetensors": kept, "dropped.safetensors": dropped},
        {"kept.safetensors": "checkpoints", "dropped.safetensors": "loras"},
        dest,
    )
    assert staged == {"kept.safetensors": dest / "checkpoints" / "kept.safetensors"}
    assert not (dest / "loras" / "dropped.safetensors").exists()


def test_stage_local_models_uses_comfy_folder_layout(comfy, tmp_path):
    source = touch(comfy["checkpoints"] / "nested" / "custom.safetensors")
    secret = touch(tmp_path / "secret.safetensors")
    dest = tmp_path / "local_models"
    staged = stage_local_models(
        {"nested/custom.safetensors": source, "../../secret.safetensors": secret},
        {"nested/custom.safetensors": "checkpoints", "../../secret.safetensors": "checkpoints"},
        dest,
    )
    expected = dest / "checkpoints" / "nested" / "custom.safetensors"
    assert staged == {"nested/custom.safetensors": expected}
    assert expected.read_bytes() == source.read_bytes()
    assert expected.stat().st_ino == source.stat().st_ino
    assert not (dest / "checkpoints" / "secret.safetensors").exists()


def test_find_workflow_local_models_reads_extensions_under_nodes(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    folders = {"checkpoints": ([str(checkpoints)], set[str]())}
    names = [f"m{extension}" for extension in MODEL_EXTENSIONS]
    for name in names:
        touch(checkpoints / name)
    touch(checkpoints / "photo.png")
    ui = {
        "nodes": [
            {
                "type": "CheckpointLoaderSimple",
                "properties": {
                    "models": [{"directory": "checkpoints", "name": name} for name in names]
                },
                "widgets_values": [*names, "photo.png"],
            }
        ]
    }
    api = {"1": {"inputs": {name: name for name in [*names, "photo.png"]}}}
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {name: checkpoints / name for name in names}
    assert directories == {name: "checkpoints" for name in names}


def test_find_workflow_local_models_reads_properties_on_a_node_inside_a_subgraph(tmp_path):
    """A subgraph node's properties name the folder. A URL there doesn't stop it being staged."""
    checkpoint = touch(tmp_path / "checkpoints" / "sd.safetensors")
    kept = touch(tmp_path / "loras" / "style.safetensors")
    folders = {
        "checkpoints": ([str(checkpoint.parent)], {".safetensors"}),
        "loras": ([str(kept.parent)], {".safetensors"}),
    }
    ui = {
        "nodes": [
            {
                "type": "sg",
                "properties": {"cnr_id": "comfy-core", "ver": "0.3.64"},
                "inputs": [
                    {"name": "ckpt_name", "widget": {"name": "ckpt_name"}},
                    {"name": "lora_name", "widget": {"name": "lora_name"}},
                ],
                "widgets_values_named": {
                    "ckpt_name": "sd.safetensors",
                    "lora_name": "style.safetensors",
                },
            }
        ],
        "definitions": {
            "subgraphs": [
                {
                    "id": "sg",
                    "inputs": [
                        {"name": "ckpt_name", "linkIds": [1]},
                        {"name": "lora_name", "linkIds": [2]},
                    ],
                    "nodes": [
                        {
                            "type": "CheckpointLoaderSimple",
                            "inputs": [
                                {
                                    "name": "ckpt_name",
                                    "widget": {"name": "ckpt_name"},
                                    "link": 1,
                                }
                            ],
                            "properties": {
                                "models": [
                                    {
                                        "directory": "checkpoints",
                                        "name": "sd.safetensors",
                                        "url": "https://x.test/sd.safetensors",
                                    }
                                ]
                            },
                        },
                        {
                            "type": "LoraLoader",
                            "inputs": [
                                {"name": "lora_name", "widget": {"name": "lora_name"}, "link": 2}
                            ],
                            "properties": {
                                "models": [{"directory": "loras", "name": "style.safetensors"}]
                            },
                        },
                    ],
                }
            ]
        },
    }
    api = {
        "1": {"inputs": {"ckpt_name": "sd.safetensors"}},
        "2": {"inputs": {"lora_name": "style.safetensors"}},
    }
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {"sd.safetensors": checkpoint, "style.safetensors": kept}
    assert directories == {"sd.safetensors": "checkpoints", "style.safetensors": "loras"}
    dest = tmp_path / "local_models"
    staged = stage_local_models(found, directories, dest)
    assert staged == {
        "sd.safetensors": dest / "checkpoints" / "sd.safetensors",
        "style.safetensors": dest / "loras" / "style.safetensors",
    }


def test_find_workflow_local_models_searches_registered_folders_without_a_directory(tmp_path):
    path = touch(tmp_path / "loras" / "style.pt")
    other = touch(tmp_path / "checkpoints" / "other.ckpt")
    folders = {
        "checkpoints": ([str(tmp_path / "checkpoints")], set[str]()),
        "loras": ([str(path.parent)], set[str]()),
    }
    # other.ckpt is only an API input. models_without_links still counts it, so it is attached.
    ui = {"nodes": [{"widgets_values": ["style.pt", "notes.txt"]}]}
    api = {"1": {"inputs": {"lora_name": "style.pt", "ckpt_name": "other.ckpt"}}}
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {"other.ckpt": other, "style.pt": path}
    assert directories == {"other.ckpt": "checkpoints", "style.pt": "loras"}


@pytest.mark.parametrize(
    ("hint", "folder"), [("clip", "text_encoders"), ("unet", "diffusion_models")]
)
def test_find_workflow_local_models_maps_legacy_folder_names(tmp_path, hint, folder):
    """ComfyUI loads clip and unet models from text_encoders and diffusion_models."""
    path = touch(tmp_path / folder / "m.safetensors")
    folders = {folder: ([str(path.parent)], set[str]())}
    ui = {"nodes": [{"properties": {"models": [{"directory": hint, "name": "m.safetensors"}]}}]}
    api = {"1": {"inputs": {"clip_name": "m.safetensors"}}}
    assert find_workflow_local_models(ui, api, folders) == (
        {"m.safetensors": path},
        {"m.safetensors": folder},
    )


def test_find_workflow_local_models_searches_every_folder_when_the_hint_misses(tmp_path):
    """A wrong folder hint, such as one guessed from a subgraph, doesn't hide a local model."""
    path = touch(tmp_path / "vae" / "m.safetensors")
    folders = {
        "checkpoints": ([str(tmp_path / "checkpoints")], set[str]()),
        "vae": ([str(path.parent)], set[str]()),
    }
    ui = {
        "nodes": [
            {"properties": {"models": [{"directory": "checkpoints", "name": "m.safetensors"}]}}
        ]
    }
    api = {"1": {"inputs": {"vae_name": "m.safetensors"}}}
    assert find_workflow_local_models(ui, api, folders) == (
        {"m.safetensors": path},
        {"m.safetensors": "vae"},
    )


def test_stage_local_models_warns_when_it_copies_a_model(comfy, tmp_path, monkeypatch, caplog):
    """A copy, such as of a model on another drive, takes time and space, so it's logged."""
    source = touch(comfy["checkpoints"] / "m.safetensors")

    def fail_link(source, target):
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.os.link", fail_link)
    dest = tmp_path / "local_models"
    staged = stage_local_models({"m.safetensors": source}, {"m.safetensors": "checkpoints"}, dest)
    assert staged == {"m.safetensors": dest / "checkpoints" / "m.safetensors"}
    assert "Copying model 'm.safetensors' into the job bundle" in caplog.text
    assert "Invalid cross-device link" in caplog.text
