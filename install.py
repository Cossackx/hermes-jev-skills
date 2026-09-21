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
import json
import os
import re
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
OBSERVER_JEVKIT = ("__init__.py", "client.py", "keystore.py", "privacy.py", "skillpick.py")


def _copytree(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def _copy_observer_jevkit(dst: Path) -> None:
    """Install only the modules needed by the bounded skill observer."""
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


# ── Hermes ───────────────────────────────────────────────────────────────────

def hermes_homes(root: Path) -> List[Path]:
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes += sorted(p for p in profiles.iterdir() if (p / "config.yaml").is_file())
    return homes


def enable_plugin(config: Path, enable: bool) -> str:
    """Add or remove `- hermes-jev` under plugins.enabled by editing only that list.

    A text edit, not a YAML round-trip: comments, ordering and every other setting survive.
    """
    text = config.read_text(encoding="utf-8")
    lines = text.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "plugins:")
    except StopIteration:
        if not enable:
            return "no plugins section"
        lines += ["plugins:", "  enabled:", f"  - {PLUGIN}"]
        start = None
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith((" ", "#"))), len(lines))
        block = lines[start + 1:end]
        item = re.compile(rf"^\s*-\s*['\"]?{re.escape(PLUGIN)}['\"]?\s*$")
        key = next((i for i, line in enumerate(block)
                    if re.match(r"^  enabled:\s*(?:\[[^\]]*\])?\s*(?:#.*)?$", line)), None)
        if key is not None:
            inline = re.match(r"^  enabled:\s*\[([^\]]*)\]\s*(#.*)?$", block[key])
            if inline:
                items = [item.strip() for item in re.findall(
                    r'"[^"]*"|\'[^\']*\'|[^,]+', inline.group(1)) if item.strip()]
                comment = f" {inline.group(2)}" if inline.group(2) else ""
                block[key] = f"  enabled:{comment}"
                block[key + 1:key + 1] = [f"  - {item}" for item in items]
        present = [i for i, line in enumerate(block) if item.match(line)]
        if enable:
            if present:
                return "already enabled"
            if key is None:
                block.insert(0, "  enabled:")
                key = 0
            block.insert(key + 1, f"  - {PLUGIN}")
        else:
            if not present:
                return "was not enabled"
            for i in reversed(present):
                del block[i]
        lines[start + 1:end] = block
    backup = config.with_name(f"{config.name}.bak-jev-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(config, backup)
    temp = config.with_name(config.name + ".jev-tmp")
    temp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp, config)
    return "enabled" if enable else "disabled"


def install_hermes(root: Path, enable: str, check: bool) -> Dict[str, object]:
    plugin_dir = root / "plugins" / PLUGIN
    homes = hermes_homes(root)
    if enable == "all":
        wanted = {"default"} | {h.name for h in homes[1:]}
    elif enable == "none":
        wanted = set()
    else:
        wanted = set(filter(None, enable.split(",")))
    report: Dict[str, object] = {"home": str(root), "profiles": len(homes) - 1, "plugin": str(plugin_dir), "enabled_in": []}
    if check:
        report["would_enable_in"] = sorted(wanted)
        return report
    _copytree(REPO / "hermes" / "plugin" / PLUGIN, plugin_dir)
    _copy_observer_jevkit(plugin_dir / "jevkit")
    skills_dir = root / "skills" / "jev"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name in SKILLS:
        _copytree(REPO / "skills" / name, skills_dir / name)
    for home in homes[1:]:
        _link(plugin_dir, home / "plugins" / PLUGIN)       # every lane scans its OWN plugins folder
        _link(skills_dir, home / "skills" / "jev")
    for home in homes:
        label = "default" if home == root else home.name
        if label in wanted and (home / "config.yaml").is_file():
            report["enabled_in"].append(f"{label}: {enable_plugin(home / 'config.yaml', True)}")  # type: ignore[union-attr]
    return report


def uninstall_hermes(root: Path) -> Dict[str, object]:
    removed = []
    for home in hermes_homes(root):
        if (home / "config.yaml").is_file():
            enable_plugin(home / "config.yaml", False)
        for path in (home / "plugins" / PLUGIN, home / "skills" / "jev"):
            if _remove(path):
                removed.append(str(path))
    return {"removed": removed}


# ── skill folders (Claude Code, Codex, generic) ──────────────────────────────

def install_skills(folder: Path, check: bool) -> Dict[str, object]:
    if not check:
        folder.mkdir(parents=True, exist_ok=True)
        for name in SKILLS:
            _copytree(REPO / "skills" / name, folder / name)
    return {"folder": str(folder), "skills": SKILLS}


def install_cli(check: bool) -> Dict[str, object]:
    target = Path.home() / ".local" / "bin" / "jev"
    if not check:
        _link(REPO / "bin" / "jev", target)
    on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
    return {"command": str(target), "on_path": on_path,
            **({} if on_path else {"hint": f"add {target.parent} to PATH, or call {REPO / 'bin' / 'jev'} directly"})}


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
        report["skills_removed"] = [str(f / n) for f in folders for n in SKILLS if _remove(f / n)]
        _remove(home / ".local" / "bin" / "jev")
    else:
        report["cli"] = install_cli(args.check)
        if (hermes / "config.yaml").is_file():
            report["hermes"] = install_hermes(hermes, args.enable, args.check)
        report["skill_folders"] = [install_skills(f, args.check) for f in folders]
        report["next"] = [
            "jev doctor",
            "jev setup-key   (only if the key is missing; the person pastes it in a private page)",
            "Hermes: start a fresh session, then /jev skills shadow",
        ]
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
