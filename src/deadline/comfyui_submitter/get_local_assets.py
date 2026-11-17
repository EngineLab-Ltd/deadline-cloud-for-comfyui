# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Finds the local files an API-format ComfyUI workflow reads (input images, videos, audio and so on,
but not models or custom nodes) and writes them to a job bundle's asset_references.yaml.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .submit import write_private_text

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
