#!/usr/bin/env python3
"""Install Hermes Jev Skills for whichever agents live on this machine.

    python3 install.py                 # detect Hermes / Claude Code / Codex and install for each
    python3 install.py --check         # show what would happen, change nothing
    python3 install.py --uninstall

It never asks for, reads or prints an API key. Connecting the key is a separate,
private step: `jev setup-key`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parent
PLUGIN = "hermes-jev"
SKILLS = sorted(p.name for p in (REPO / "skills").iterdir() if (p / "SKILL.md").is_file())
OBSERVER_JEVKIT = ("__init__.py", "client.py", "keystore.py", "privacy.py", "skillpick.py",
                   "rerank.py", "compact.py", "choose.py", "route.py", "catalog.py", "launch.py")
INSTALL_MARKER = ".hermes-jev-install.json"


def _copytree(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def _copy_observer_jevkit(dst: Path) -> None:
    """Install the modules needed by the explicit, profile-gated integration."""
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in OBSERVER_JEVKIT:
        shutil.copy2(REPO / "jevkit" / name, dst / name)


def _link(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    _remove(link)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return
    except OSError:
        if os.name != "nt":
            raise
    if target.is_dir():
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise OSError(result.stderr.strip() or result.stdout.strip() or "failed to create directory junction")
    else:
        try:
            os.link(target, link)
        except OSError:
            shutil.copy2(target, link)


def _remove(path: Path) -> bool:
    if path.is_symlink():
        path.unlink()
        return True
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if os.name == "nt" and reparse and attributes & reparse:
        try:
            path.rmdir()
        except NotADirectoryError:
            path.unlink()
        return True
    if stat.S_ISREG(info.st_mode):
        path.unlink()
        return True
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
        return True
    return False


def _leading_indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _child_indent(parent: str, lines: List[str]) -> str:
    """Return the established child indentation, or a conservative default."""
    indents = [_leading_indent(line) for line in lines
               if line.strip() and not line.lstrip().startswith("#") and len(_leading_indent(line)) > len(parent)]
    if indents:
        return min(indents, key=len)
    return parent + ("\t" if "\t" in parent else "  ")


# ── Hermes ───────────────────────────────────────────────────────────────────

def hermes_homes(root: Path) -> List[Path]:
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes += sorted(p for p in profiles.iterdir() if (p / "config.yaml").is_file())
    return homes


def _strip_yaml_comment(value: str) -> str:
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char in "'\"":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _yaml_scalar(value: str) -> str:
    value = _strip_yaml_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else value[1:-1]
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def _yaml_flow_items(value: str) -> List[str]:
    body = value.strip()[1:-1]
    items = []
    start = 0
    quote = None
    escaped = False
    for index, char in enumerate(body):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char in "'\"":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "," and quote is None:
            items.append(body[start:index])
            start = index + 1
    items.append(body[start:])
    return [item for item in (_yaml_scalar(item) for item in items) if item]


def _configured_external_dirs(config: Path) -> List[Path]:
    """Read direct ``skills.external_dirs`` scalar/list forms without PyYAML."""
    if not config.is_file():
        return []
    lines = config.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if re.match(r"^skills:\s*(?:#.*)?$", line))
    except StopIteration:
        return []
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i].lstrip().startswith("#")
                and not lines[i].startswith((" ", "\t"))), len(lines))
    block = lines[start + 1:end]
    content = [(i, line, len(_leading_indent(line))) for i, line in enumerate(block)
               if line.strip() and not line.lstrip().startswith("#")]
    if not content:
        return []
    direct_indent = min(indent for _, _, indent in content)
    entry = next(((i, line) for i, line, indent in content
                  if indent == direct_indent and re.match(r"^[ \t]+external_dirs:", line)), None)
    if entry is None:
        return []
    key, line = entry
    raw = _strip_yaml_comment(line.split(":", 1)[1]).strip()
    if raw.startswith("[") and raw.endswith("]"):
        values = _yaml_flow_items(raw)
    elif raw:
        values = [_yaml_scalar(raw)]
    else:
        values = []
        for child in block[key + 1:]:
            if child.strip() and not child.lstrip().startswith("#") \
                    and len(_leading_indent(child)) <= direct_indent:
                break
            match = re.match(r"^[ \t]+-[ \t]+(.+?)\s*$", child)
            if match:
                value = _yaml_scalar(match.group(1))
                if value:
                    values.append(value)
    paths = []
    for value in values:
        expanded = Path(os.path.expandvars(value)).expanduser()
        paths.append(expanded if expanded.is_absolute() else config.parent / expanded)
    return paths


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path.resolve(strict=False))))


def _within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([_normalized(path), _normalized(parent)]) == _normalized(parent)
    except ValueError:
        return False


def _skill_name(skill_file: Path) -> str:
    try:
        head = skill_file.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return ""
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\r\n#]+)", head)
    return match.group(1).strip() if match else skill_file.parent.name


def _external_skill_names(config: Path, local_path: Path,
                          planned_roots: Dict[Path, List[str]] | None = None) -> set[str]:
    configured = _configured_external_dirs(config)
    planned = {_normalized(path): names for path, names in (planned_roots or {}).items()}
    names = set()
    for root in configured:
        if _normalized(root) in planned and not _within(root, local_path):
            names.update(planned[_normalized(root)])
        if not root.exists():
            continue
        candidates = [root / "SKILL.md"] if (root / "SKILL.md").is_file() else []
        for directory, _, files in os.walk(root, followlinks=False):
            if "SKILL.md" in files:
                candidate = Path(directory) / "SKILL.md"
                if candidate not in candidates:
                    candidates.append(candidate)
        for skill_file in candidates:
            # Never count the local projection as its own external replacement.
            if not _within(skill_file, local_path):
                names.add(_skill_name(skill_file))
    return names


def _all_skills_external(config: Path, local_path: Path,
                         planned_roots: Dict[Path, List[str]] | None = None) -> bool:
    return set(SKILLS).issubset(_external_skill_names(config, local_path, planned_roots))


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(os.name == "nt" and reparse and attributes & reparse)


def _tree_manifest(root: Path) -> Dict[str, bytes]:
    manifest = {}

    def visit(directory: Path) -> None:
        for entry in os.scandir(directory):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if entry.name in {"__pycache__", ".DS_Store", INSTALL_MARKER} \
                    or entry.name.endswith(".pyc"):
                continue
            if _is_link_or_reparse(path):
                manifest[relative] = b"link"
            elif entry.is_dir(follow_symlinks=False):
                manifest[relative] = b"dir"
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                manifest[relative] = b"file\0" + path.read_bytes()
            else:
                manifest[relative] = b"other"

    if root.is_dir():
        visit(root)
    return manifest


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, value in sorted(_tree_manifest(root).items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value)
        digest.update(b"\0")
    return digest.hexdigest()


def _write_install_marker(path: Path, kind: str, name: str = "") -> None:
    marker = {
        "installer": PLUGIN,
        "kind": kind,
        "name": name,
        "digest": _tree_digest(path),
    }
    (path / INSTALL_MARKER).write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")


def _marker_matches(path: Path, kind: str, name: str = "") -> bool:
    try:
        marker = json.loads((path / INSTALL_MARKER).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return marker.get("installer") == PLUGIN \
        and marker.get("kind") == kind \
        and marker.get("name", "") == name \
        and marker.get("digest") == _tree_digest(path)


def _real_target(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.realpath(str(path))))


def _is_managed_skill_bundle(path: Path) -> bool:
    if _is_link_or_reparse(path) or not path.is_dir():
        return False
    if _marker_matches(path, "bundle"):
        return True
    entries = {child.name for child in path.iterdir()
               if child.name not in {".DS_Store", INSTALL_MARKER}}
    if entries != set(SKILLS):
        return False
    return all(_tree_manifest(path / name) == _tree_manifest(REPO / "skills" / name)
               for name in SKILLS)


def _is_managed_skill_projection(path: Path, expected_targets: List[Path]) -> bool:
    if _is_link_or_reparse(path):
        return _real_target(path) in {_real_target(target) for target in expected_targets}
    return _is_managed_skill_bundle(path)


def _remove_managed_skill_projection(path: Path, expected_targets: List[Path]) -> bool:
    return _remove(path) if _is_managed_skill_projection(path, expected_targets) else False


def _is_managed_skill_copy(path: Path, name: str) -> bool:
    if _is_link_or_reparse(path) or not path.is_dir():
        return False
    return _marker_matches(path, "skill", name) \
        or _tree_manifest(path) == _tree_manifest(REPO / "skills" / name)


def _expected_projection_targets(root: Path, plugin_dir: Path, home: Path) -> List[Path]:
    targets = [plugin_dir / "skills"]
    legacy = root / "skills" / "jev"
    if home != root and _is_managed_skill_bundle(legacy):
        targets.append(legacy)
    return targets


def enable_plugin(config: Path, enable: bool) -> str:
    """Add or remove `- hermes-jev` under plugins.enabled by editing only that list.

    A text edit, not a YAML round-trip: comments, ordering and every other setting survive.
    """
    with config.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if re.match(r"^[ \t]*plugins:\s*(?:#.*)?$", line))
    except StopIteration:
        if not enable:
            return "no plugins section"
        lines += ["plugins:", "  enabled:", f"    - {PLUGIN}"]
        start = None
    if start is not None:
        plugins_indent = _leading_indent(lines[start])
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() and not lines[i].lstrip().startswith("#")
                    and len(_leading_indent(lines[i])) <= len(plugins_indent)), len(lines))
        block = lines[start + 1:end]
        item = re.compile(rf"^[ \t]*-[ \t]*['\"]?{re.escape(PLUGIN)}['\"]?[ \t]*(?:#.*)?$")
        child_indent = _child_indent(plugins_indent, block)
        key = next((i for i, line in enumerate(block)
                    if _leading_indent(line) == child_indent
                    and re.match(r"^[ \t]+enabled:\s*(?:\[[^\]]*\])?\s*(?:#.*)?$", line)), None)
        if key is not None:
            key_indent = _leading_indent(block[key])
            inline = re.match(r"^[ \t]+enabled:\s*\[([^\]]*)\](\s*(?:#.*)?)$", block[key])
            if inline:
                items = [item.strip() for item in re.findall(
                    r'"[^"]*"|\'[^\']*\'|[^,]+', inline.group(1)) if item.strip()]
                block[key] = f"{key_indent}enabled:{inline.group(2)}"
                list_indent = _child_indent(key_indent, block[key + 1:])
                block[key + 1:key + 1] = [f"{list_indent}- {item}" for item in items]
        present = [i for i, line in enumerate(block) if item.match(line)]
        if enable:
            if present:
                return "already enabled"
            if key is None:
                key_indent = child_indent
                block.insert(0, f"{key_indent}enabled:")
                key = 0
            else:
                key_indent = _leading_indent(block[key])
            list_indent = next((_leading_indent(line) for line in block[key + 1:]
                                if re.match(r"^[ \t]+-[ \t]+", line)
                                and len(_leading_indent(line)) > len(key_indent)),
                               _child_indent(key_indent, block[key + 1:]))
            block.insert(key + 1, f"{list_indent}- {PLUGIN}")
        else:
            if not present:
                return "was not enabled"
            for i in reversed(present):
                del block[i]
        lines[start + 1:end] = block
    backup = config.with_name(f"{config.name}.bak-jev-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(config, backup)
    temp = config.with_name(config.name + ".jev-tmp")
    rendered = newline.join(lines) + (newline if trailing_newline else "")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(rendered)
    os.replace(temp, config)
    return "enabled" if enable else "disabled"


def install_hermes(root: Path, enable: str, check: bool,
                   planned_skill_roots: Dict[Path, List[str]] | None = None) -> Dict[str, object]:
    plugin_dir = root / "plugins" / PLUGIN
    homes = hermes_homes(root)
    if enable == "all":
        wanted = {"default"} | {h.name for h in homes[1:]}
    elif enable == "none":
        wanted = set()
    else:
        wanted = set(filter(None, enable.split(",")))
    states = []
    for home in homes:
        label = "default" if home == root else home.name
        skill_path = home / "skills" / "jev"
        expected_targets = _expected_projection_targets(root, plugin_dir, home)
        external = _all_skills_external(
            home / "config.yaml", skill_path, planned_skill_roots
        )
        conflict = os.path.lexists(skill_path) and not _is_managed_skill_projection(
            skill_path, expected_targets
        )
        states.append((home, label, skill_path, expected_targets, external, conflict))
    report: Dict[str, object] = {
        "home": str(root),
        "profiles": len(homes) - 1,
        "plugin": str(plugin_dir),
        "enabled_in": [],
        "skills_external_in": [label for _, label, _, _, external, _ in states if external],
        "skills_linked_in": [label for _, label, _, _, external, conflict in states
                             if not external and not conflict],
        "skills_conflicts_in": [label for _, label, _, _, _, conflict in states if conflict],
    }
    if check:
        report["would_enable_in"] = sorted(wanted)
        return report
    _copytree(REPO / "hermes" / "plugin" / PLUGIN, plugin_dir)
    _copy_observer_jevkit(plugin_dir / "jevkit")
    skills_dir = plugin_dir / "skills"
    skills_dir.mkdir()
    for name in SKILLS:
        _copytree(REPO / "skills" / name, skills_dir / name)
    _write_install_marker(skills_dir, "bundle")
    for home in homes[1:]:
        _link(plugin_dir, home / "plugins" / PLUGIN)       # every lane scans its OWN plugins folder
    # Profiles first: legacy profile junctions may target the default real bundle.
    for home, label, skill_path, expected_targets, external, conflict in sorted(
            states, key=lambda state: state[0] == root):
        if not conflict:
            _remove_managed_skill_projection(skill_path, expected_targets)
            if not external:
                _link(skills_dir, skill_path)
        if label in wanted and (home / "config.yaml").is_file():
            report["enabled_in"].append(f"{label}: {enable_plugin(home / 'config.yaml', True)}")  # type: ignore[union-attr]
    return report


def uninstall_hermes(root: Path) -> Dict[str, object]:
    removed = []
    preserved = []
    homes = hermes_homes(root)
    plugin_dir = root / "plugins" / PLUGIN
    states = [
        (home, home / "skills" / "jev", _expected_projection_targets(root, plugin_dir, home))
        for home in homes
    ]
    # Remove profile projections while legacy/default targets still exist.
    for home, skill_path, expected_targets in sorted(
            states, key=lambda state: state[0] == root):
        if _remove_managed_skill_projection(skill_path, expected_targets):
            removed.append(str(skill_path))
        elif os.path.lexists(skill_path):
            preserved.append(str(skill_path))
    for home in sorted(homes, key=lambda candidate: candidate == root):
        if (home / "config.yaml").is_file():
            enable_plugin(home / "config.yaml", False)
        plugin_path = home / "plugins" / PLUGIN
        if _remove(plugin_path):
            removed.append(str(plugin_path))
    return {"removed": removed, "preserved_unmanaged": preserved}


# ── skill folders (Claude Code, Codex, generic) ──────────────────────────────

def install_skills(folder: Path, check: bool) -> Dict[str, object]:
    installed = []
    conflicts = []
    for name in SKILLS:
        target = folder / name
        if os.path.lexists(target) and not _is_managed_skill_copy(target, name):
            conflicts.append(name)
        else:
            installed.append(name)
    if not check:
        folder.mkdir(parents=True, exist_ok=True)
        for name in installed:
            _copytree(REPO / "skills" / name, folder / name)
            _write_install_marker(folder / name, "skill", name)
    return {"folder": str(folder), "skills": SKILLS,
            "installed": installed, "conflicts": conflicts}


def uninstall_skills(folder: Path) -> Dict[str, List[str]]:
    removed = []
    preserved = []
    for name in SKILLS:
        target = folder / name
        if _is_managed_skill_copy(target, name) and _remove(target):
            removed.append(str(target))
        elif os.path.lexists(target):
            preserved.append(str(target))
    return {"removed": removed, "preserved_unmanaged": preserved}


def install_cli(check: bool) -> Dict[str, object]:
    target = Path.home() / ".local" / "bin" / "jev"
    if not check:
        _write_cli_launcher(target)
    on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
    return {"command": str(target), "on_path": on_path,
            **({} if on_path else {"hint": f"add {target.parent} to PATH, or call {REPO / 'bin' / 'jev'} directly"})}


def _write_cli_launcher(target: Path, repo: Path = REPO) -> None:
    """Install a launcher that retains the checkout location through hardlink fallback."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove(target)
    source = str(repo.resolve())
    launcher = (
        "#!/bin/sh\n"
        "# Installed by Hermes Jev Skills. The retained checkout supplies jevkit.\n"
        f"repo={shlex.quote(source)}\n"
        "if [ ! -f \"$repo/jevkit/__main__.py\" ]; then\n"
        "  printf '%s\\n' \"jev: source checkout is missing: $repo\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "PYTHONPATH=\"$repo${PYTHONPATH:+:$PYTHONPATH}\" exec python3 -m jevkit \"$@\"\n"
    )
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(launcher)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    parser.add_argument("--enable", default="all", help="Hermes profiles to enable the plugin in: all, none, or a,b,c")
    parser.add_argument("--skills-dir", action="append", default=[], help="extra skill folder to install into")
    args = parser.parse_args()

    home = Path.home()
    hermes = Path(args.hermes_home).expanduser()
    folders = [Path(p).expanduser() for p in args.skills_dir]
    folders += [p for p in (home / ".claude" / "skills", home / ".codex" / "skills", home / ".agents" / "skills") if p.parent.is_dir()]

    report: Dict[str, object] = {"repo": str(REPO), "mode": "uninstall" if args.uninstall else "check" if args.check else "install"}
    if args.uninstall:
        if hermes.is_dir():
            report["hermes"] = uninstall_hermes(hermes)
        skill_reports = [uninstall_skills(folder) for folder in folders]
        report["skills_removed"] = [path for item in skill_reports for path in item["removed"]]
        report["skills_preserved_unmanaged"] = [
            path for item in skill_reports for path in item["preserved_unmanaged"]
        ]
        _remove(home / ".local" / "bin" / "jev")
    else:
        report["cli"] = install_cli(args.check)
        # Populate shared roots first so Hermes can avoid a second discoverable
        # copy when one of those roots is configured in skills.external_dirs.
        report["skill_folders"] = [install_skills(f, args.check) for f in folders]
        # Dry runs count only copies that will actually be installed; conflicts
        # remain on disk and cannot supply a hypothetical SKILL.md. Real runs
        # use readback exclusively, not the installer's projection.
        planned = {
            Path(item["folder"]): item["installed"] for item in report["skill_folders"]
        } if args.check else None
        if (hermes / "config.yaml").is_file():
            report["hermes"] = install_hermes(
                hermes, args.enable, args.check, planned_skill_roots=planned
            )
        report["next"] = [
            "jev doctor",
            "jev setup-key   (only if the key is missing; the person pastes it in a private page)",
            "Hermes: start a fresh session, then /jev skills shadow",
        ]
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
