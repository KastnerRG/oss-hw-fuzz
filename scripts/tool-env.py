#!/usr/bin/env python3
"""Discover host EDA tools through generic, ordered mount roots and PATH entries."""

import argparse
import json
import os
from pathlib import Path
import re
import sys


# Installation roots are independent of vendors; only executable/home conventions
# belong here. Additional tools can always use an explicit TOOL_PATHS entry.
TOOLS = {
    "vivado": (("vivado",), ("VIVADO_HOME", "XILINX_VIVADO")),
    "vcs": (("vcs",), ("VCS_HOME",)),
    "verdi": (("verdi",), ("VERDI_HOME",)),
    "testmax": (("tmax", "tmax64"), ("TESTMAX_HOME", "SYNOPSYS_TMAX")),
    "genus": (("genus",), ("GENUS_HOME",)),
    "xcelium": (("xrun",), ("XCELIUM_HOME",)),
    "imc": (("imc",), ("IMC_HOME",)),
}
PRUNE = {name.casefold() for name in {
    "doc", "docs", "examples", "lib", "lib64", "libexec", "include", "share",
    "data", "installData", "iscape_logs", "test", "tests", "tutorials", "tps",
    "third_party", "thirdparty", "jre", "jre64", "downloads", "node_modules",
    # Public launchers select architecture-specific payloads themselves.
    "amd64", "aarch64", "arm64", "x86_64", "ia32", "linux64", "lnx86",
    "i386", "i686", "win32", "win64", "nt64",
}}


def split_paths(value):
    return list(dict.fromkeys(Path(item) for item in value.split(":") if item))


def executable(path):
    return path.is_file() and os.access(path, os.X_OK)


def bin_directories(root):
    """Bound the search and avoid traversing tool libraries, sources, or caches."""
    pending, seen = [(root, 0)], set()
    commands = [name for names, _ in TOOLS.values() for name in names]
    # A root may itself be an unusual executable directory.
    if root.name not in {"bin", "bin64"}:
        yield root
    while pending:
        directory, depth = pending.pop()
        try:
            stat = directory.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            if directory.name in {"bin", "bin64"}:
                yield directory
                continue
            if depth <= 4:
                # scandir uses directory-entry types; Path.is_dir() on every
                # file is prohibitively expensive on shared tool filesystems.
                with os.scandir(directory) as entries:
                    children = []
                    for entry in entries:
                        if entry.name.startswith(".") or entry.name.casefold() in PRUNE:
                            continue
                        try:
                            if entry.is_dir():
                                children.append(entry.path)
                        except OSError:
                            continue
                public_bins = [Path(path) for path in children if Path(path).name in {"bin", "bin64"}]
                for path in children:
                    if Path(path).name in {"tools", "tools.lnx86"}:
                        candidate = Path(path) / "bin"
                        if candidate.is_dir():
                            public_bins.append(candidate)
                # A versioned installation with a known public launcher does
                # not need a recursive search through its implementation files.
                if (any(re.search(r"\d", part) for part in directory.relative_to(root).parts)
                        and any(executable(path / command) for path in public_bins for command in commands)):
                    yield from public_bins
                    continue
                if depth == 4:
                    yield from public_bins
                else:
                    pending.extend((Path(path), depth + 1) for path in sorted(children, reverse=True))
        except (OSError, RuntimeError):
            continue


def version_key(directory, root):
    parts = directory.relative_to(root).parts
    version = tuple(int(number) for part in parts
                    if part not in {"bin", "bin64", "tools", "tools.lnx86", "linux64", "64bit",
                                    "amd64", "x86_64", "x86", "ia32", "lnx86"}
                    for number in re.findall(r"\d+", part))
    return version, -len(parts), str(directory)


def tool_home(directory):
    home = directory.parent if directory.name in {"bin", "bin64"} else directory
    return home.parent if home.name in {"tools", "tools.lnx86"} else home


def discover(roots, extra_paths, base_path):
    for directory in extra_paths:
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError(f"TOOL_PATHS directory is unavailable in the image/mounts: {directory}")
    candidates = [sorted(bin_directories(root), key=lambda path: version_key(path, root), reverse=True)
                  for root in roots if root.is_dir()]
    selected_paths = list(extra_paths)
    for names, _ in TOOLS.values():
        groups = [extra_paths, *candidates]
        selected = next((directory for group in groups for directory in group
                         if any(executable(directory / name) for name in names)), None)
        if selected is not None and selected not in selected_paths:
            selected_paths.append(selected)

    # Preserve the image's Java/compiler priority and reflect actual PATH lookup
    # when a selected vendor bin directory contains more than one known tool.
    search_paths = list(dict.fromkeys([*split_paths(base_path), *selected_paths]))
    environment = {
        "TOOL_ROOTS": ":".join(map(str, roots)),
        "TOOL_PATHS": ":".join(map(str, extra_paths)),
        "PATH": ":".join(map(str, search_paths)),
    }
    found = {}
    for name, (commands, homes) in TOOLS.items():
        program = next((directory / command for directory in search_paths for command in commands
                        if executable(directory / command)), None)
        if program is not None:
            home = tool_home(program.parent)
            found[name] = {"executable": str(program), "home": str(home)}
            environment.update({key: str(home) for key in homes})
    if "testmax" in found:
        environment["TMAX_64BIT"] = "1"
    return {"roots": list(map(str, roots)), "extra_paths": list(map(str, extra_paths)),
            "tools": found, "missing_tools": [name for name in TOOLS if name not in found],
            "environment": environment}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "env"), default="json")
    args = parser.parse_args()
    result = discover(split_paths(os.environ.get("TOOL_ROOTS", "")),
                      split_paths(os.environ.get("TOOL_PATHS", "")), os.environ["PATH"])
    if args.format == "env":
        for key, value in result["environment"].items():
            sys.stdout.write(f"{key}={value}\0")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
