# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Finds the local files an API-format ComfyUI workflow reads (input images, videos, audio and so on,
but not custom nodes) and writes them to a job bundle's asset_references.yaml. Model files are
not part of that scan. A model the workflow loads is staged into the job's model directory and
attached from there.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from .submit import MODEL_EXTENSIONS, workflow_models, write_private_text

# Loaders for these file types look in ComfyUI's input directory.
MEDIA_EXTENSIONS = frozenset(
    {
        # Images
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".exr", ".hdr", ".avif",
        # Videos
        ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv",
        # Audio
        ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus", ".aac",
        # 3D
        ".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx",
    }
)  # fmt: skip

# A value can name its directory with a suffix: "example.png [input]".
_ANNOTATED = re.compile(r"(?P<name>.+) \[(?P<directory>input|output|temp)\]")
_MAX_VALUE_LENGTH = 1024

# folder_paths.folder_names_and_paths maps a folder type to (directories, extensions).
ModelFolders = Mapping[str, Sequence[Any]]

logger = logging.getLogger(__name__)


@dataclass
class LocalAssets:
    filenames: list[Path] = field(default_factory=list)
    directories: list[Path] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.filenames or self.directories)


def get_local_assets(
    workflow: Mapping[str, Any],
    *,
    input_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str] | None = None,
    temp_dir: str | os.PathLike[str] | None = None,
) -> LocalAssets:
    """
    Returns the existing local files that the workflow's literal input values refer to. A value
    counts when it names a media file that exists in the input directory, such as a LoadImage's
    image, or an absolute path to a media file. Model files are left out of this scan.

    Values that link to another node's output are ignored, and so are custom nodes.
    """
    resolver = _Resolver(
        input_dir=Path(input_dir),
        annotated_dirs={
            "input": Path(input_dir),
            "output": Path(output_dir) if output_dir else None,
            "temp": Path(temp_dir) if temp_dir else None,
        },
    )

    filenames: set[Path] = set()
    directories: set[Path] = set()
    for value in _literal_strings(workflow):
        try:
            resolved = resolver.resolve(value)
        except (OSError, ValueError):
            # An over-long or otherwise invalid path can't be one of the workflow's files.
            continue
        if resolved is None:
            continue
        path, is_directory = resolved
        (directories if is_directory else filenames).add(path)

    return LocalAssets(sorted(filenames), sorted(directories))


def write_asset_references(assets: LocalAssets, path: Path) -> Path:
    """
    Writes asset_references.yaml in the job bundle format. Outputs aren't listed because the job
    template's OutputDir path parameter already covers them.
    """

    def block(key: str, paths: Iterable[Path], indent: str) -> list[str]:
        paths = list(paths)
        if not paths:
            return [f"{indent}{key}: []"]
        # A JSON string is also a valid, quoted YAML string.
        return [f"{indent}{key}:"] + [
            f"{indent}- {json.dumps(str(p), ensure_ascii=False)}" for p in paths
        ]

    lines = [
        "assetReferences:",
        "  inputs:",
        *block("filenames", assets.filenames, "    "),
        *block("directories", assets.directories, "    "),
        "  outputs:",
        *block("directories", [], "    "),
        *block("referencedPaths", [], "  "),
    ]
    write_private_text(path, "\n".join(lines) + "\n")
    return path


def find_local_model(folders: ModelFolders, directory: str, name: str) -> Path | None:
    """
    The file ComfyUI would load for this folder type and name, or None. folders is
    folder_paths.folder_names_and_paths: each value is (directories, extensions). The directories
    are searched in order. name may include a subfolder.
    """
    entry = folders.get(directory)
    if not entry:
        return None
    try:
        roots = entry[0]
    except (TypeError, KeyError):
        return None
    if isinstance(roots, (str, bytes)):
        return None
    for root in roots:
        try:
            path = _Resolver._contained(Path(root), name)
        except (OSError, ValueError, TypeError):
            continue
        if path is not None and path.is_file():
            return path
    return None


def stage_local_models(
    models: Mapping[str, Path], directories: Mapping[str, str], dest: Path
) -> dict[str, Path]:
    """
    Places each model at dest/<directory>/<name> and returns the workflow name mapped to that path.
    The destination matches the model folders in the job's extra model paths config, so ComfyUI on
    the worker loads the original file name from the mapped model directory. A hard link is used
    when the destination is on the same volume; otherwise the file is copied. A file that cannot
    be placed is logged and left out.
    """
    staged: dict[str, Path] = {}
    for name, source in models.items():
        directory = directories.get(name)
        relative = _model_relative_path(directory, name)
        if relative is None:
            continue
        try:
            target = _Resolver._contained(dest, relative)
        except (OSError, ValueError):
            continue
        if target is None or not source.is_file():
            logger.warning("Not staging model %r from %s", name, source)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
        except OSError:
            logger.exception("Couldn't stage model %r from %s", name, source)
            continue
        staged[name] = target
    return dict(sorted(staged.items()))


def _model_relative_path(directory: str | None, name: str) -> str | None:
    """`directory/name` with separators normalized. `..` is refused."""
    if not isinstance(directory, str) or not name:
        return None
    parts = (*PurePath(directory).parts, *PurePath(name).parts)
    if ".." in parts:
        return None
    return "/".join(parts) or None


def find_workflow_local_models(
    ui_workflow: Any, api_workflow: Mapping[str, Any], folders: ModelFolders
) -> tuple[dict[str, Path], dict[str, str]]:
    """
    Local files for the models the workflow loads. The names are the same list as
    workflow_models. The folder comes from the loader's properties, or from
    the subgraph definition when the file name is on a subgraph widget. Without a folder, every
    registered model folder is searched. Missing files are logged and omitted.

    Returns the local path and folder type for each file to stage as a job attachment.
    """
    if not isinstance(api_workflow, dict):
        return {}, {}
    missing = workflow_models(api_workflow)
    known = _model_directories(ui_workflow)
    found: dict[str, Path] = {}
    directories: dict[str, str] = {}
    for name in missing:
        directory = known.get(name)
        located = _search_local_model(folders, name, directory)
        if located is None:
            logger.warning("No local file for model %r in folder %r", name, directory)
            continue
        path, folder = located
        found[name] = path
        directories[name] = folder
    return found, directories


def _model_directories(ui_workflow: Any) -> dict[str, str | None]:
    """Maps a model file name written on a UI node to its ComfyUI folder, when one is known."""
    subgraphs = _subgraphs_by_id(ui_workflow)
    directories: dict[str, str | None] = {}
    nodes = ui_workflow.get("nodes") if isinstance(ui_workflow, Mapping) else None
    if not isinstance(nodes, list):
        return directories
    for node in nodes:
        # mode 2 and 4 are muted and bypassed. Their models aren't in the API workflow.
        if not isinstance(node, Mapping) or node.get("mode") in (2, 4):
            continue
        node_type = node.get("type")
        subgraph = subgraphs.get(node_type) if isinstance(node_type, str) else None
        for filename in _model_filenames(node):
            # workflow_models normalizes separators, so the lookup key has to match.
            directories.setdefault(
                filename.replace("\\", "/"), _model_directory(node, filename, subgraph)
            )
    return directories


def _is_model_filename(value: str) -> bool:
    if len(value) > _MAX_VALUE_LENGTH or "://" in value:
        return False
    if any(char in value for char in "\n\r\0"):
        return False
    return PurePath(value).suffix.lower() in MODEL_EXTENSIONS


def _model_filenames(node: Mapping[str, Any]) -> list[str]:
    """Model file names written on a node, ignoring download URLs."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(value: Any, key: str | None = None) -> None:
        if key == "url":
            return
        if isinstance(value, str):
            filename = value.strip()
            if filename not in seen and _is_model_filename(filename):
                seen.add(filename)
                found.append(filename)
        elif isinstance(value, Mapping):
            for child_key, child in value.items():
                walk(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def _node_models(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return []
    models = properties.get("models")
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict)]


def _subgraphs(ui_workflow: Any) -> list[Mapping[str, Any]]:
    if not isinstance(ui_workflow, Mapping):
        return []
    definitions = ui_workflow.get("definitions")
    if not isinstance(definitions, Mapping):
        return []
    subgraphs = definitions.get("subgraphs")
    if not isinstance(subgraphs, list):
        return []
    return [subgraph for subgraph in subgraphs if isinstance(subgraph, Mapping)]


def _subgraphs_by_id(ui_workflow: Any) -> dict[str, Mapping[str, Any]]:
    return {
        subgraph_id: subgraph
        for subgraph in _subgraphs(ui_workflow)
        if isinstance(subgraph_id := subgraph.get("id"), str)
    }


def _widget_inputs(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inputs = node.get("inputs")
    if not isinstance(inputs, list):
        return []
    return [
        item
        for item in inputs
        if isinstance(item, Mapping) and isinstance(item.get("widget"), dict)
    ]


def _widget_name(item: Mapping[str, Any]) -> str | None:
    name = item["widget"].get("name")
    if isinstance(name, str):
        return name
    fallback = item.get("name")
    return fallback if isinstance(fallback, str) else None


def _widget_names_for_filename(node: Mapping[str, Any], filename: str) -> list[str]:
    names: list[str] = []
    named = node.get("widgets_values_named")
    if isinstance(named, Mapping):
        names.extend(
            key for key, value in named.items() if isinstance(key, str) and value == filename
        )
    widgets = node.get("widgets_values")
    if isinstance(widgets, list):
        for index, item in enumerate(_widget_inputs(node)):
            label = _widget_name(item)
            if label and label not in names and index < len(widgets) and widgets[index] == filename:
                names.append(label)
    return names


def _directory_from_models(models: Iterable[dict[str, Any]], filename: str) -> str | None:
    for model in models:
        directory = model.get("directory")
        if model.get("name") == filename and isinstance(directory, str):
            return directory
    return None


def _linked_directory(
    node: Mapping[str, Any], filename: str, subgraph: Mapping[str, Any] | None
) -> str | None:
    """Folder type of the definition loader a subgraph widget feeds this file name into."""
    if subgraph is None:
        return None
    widget_names = set(_widget_names_for_filename(node, filename))
    if not widget_names:
        return None
    link_ids: list[Any] = []
    inputs = subgraph.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, Mapping) or item.get("name") not in widget_names:
                continue
            ids = item.get("linkIds")
            if isinstance(ids, list):
                link_ids.extend(ids)
    if not link_ids:
        return None
    nodes = subgraph.get("nodes")
    if not isinstance(nodes, list):
        return None
    for inner in nodes:
        if not isinstance(inner, Mapping):
            continue
        inner_inputs = inner.get("inputs")
        if not isinstance(inner_inputs, list):
            continue
        if not any(
            isinstance(item, Mapping) and item.get("link") in link_ids for item in inner_inputs
        ):
            continue
        for model in _node_models(inner):
            directory = model.get("directory")
            if isinstance(directory, str):
                return directory
    return None


def _model_directory(
    node: Mapping[str, Any], filename: str, subgraph: Mapping[str, Any] | None
) -> str | None:
    return _directory_from_models(_node_models(node), filename) or _linked_directory(
        node, filename, subgraph
    )


# Folder names ComfyUI still accepts from older workflows, as its folder_paths.map_legacy does.
_LEGACY_FOLDERS = {"clip": "text_encoders", "unet": "diffusion_models"}


def _search_local_model(
    folders: ModelFolders, name: str, directory: str | None
) -> tuple[Path, str] | None:
    """
    The local file and the folder it was found in, or None when it is missing. The folder the
    workflow names is searched first. It can be a legacy name, or a guess from a subgraph, so the
    other folders are searched when it doesn't have the file.
    """
    if directory and directory not in folders:
        directory = _LEGACY_FOLDERS.get(directory, directory)
    for folder_name in [directory] if directory else []:
        path = find_local_model(folders, folder_name, name)
        if path is not None:
            return path, folder_name
    for folder_name in folders:
        path = find_local_model(folders, folder_name, name)
        if path is not None:
            return path, folder_name
    return None


def _literal_strings(workflow: Mapping[str, Any]) -> Iterable[str]:
    """Yields every string input value. A link to another node is a list, so it's skipped."""
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, Mapping) else None
        if not isinstance(inputs, Mapping):
            continue
        for value in inputs.values():
            if isinstance(value, str):
                yield value


class _Resolver:
    def __init__(
        self,
        input_dir: Path,
        annotated_dirs: Mapping[str, Path | None],
    ):
        self._input_dir = input_dir
        self._annotated_dirs = annotated_dirs

    def resolve(self, value: str) -> tuple[Path, bool] | None:
        """Returns the (path, is_directory) that a value refers to, or None."""

        def accept(path: Path | None, is_directory: bool) -> tuple[Path, bool] | None:
            if path is None:
                return None
            return path, is_directory

        if len(value) > _MAX_VALUE_LENGTH or "\n" in value or "\0" in value:
            return None

        annotated = _ANNOTATED.fullmatch(value)
        if annotated:
            base = self._annotated_dirs[annotated["directory"]]
            path = self._contained(base, annotated["name"]) if base else None
            return accept(path if path is not None and path.is_file() else None, False)

        name = value.strip()
        if Path(name).is_absolute():
            path = Path(os.path.normpath(name))
            if path.suffix.lower() in MEDIA_EXTENSIONS and path.is_file():
                return accept(path, False)
            return None

        if Path(name).suffix.lower() in MEDIA_EXTENSIONS:
            path = self._contained(self._input_dir, name)
            if path is not None and path.is_file():
                return accept(path, False)
        return None

    @staticmethod
    def _contained(directory: Path, name: str) -> Path | None:
        """Joins name onto directory, refusing names that climb out of it (for example `../x`)."""
        path = Path(os.path.normpath(directory / name))
        base = Path(os.path.normpath(directory))
        return path if base in path.parents else None
