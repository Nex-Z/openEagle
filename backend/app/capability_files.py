from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PORTABLE_SETTING_KEYS = ("mcp", "skills")
MCP_FILE_NAME = "mcp.json"
SKILLS_DIR_NAME = "skills"
DELETED_SKILLS_DIR_NAME = "deleted-skills"


def strip_file_backed_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in settings.items() if key not in PORTABLE_SETTING_KEYS}


def load_file_backed_settings(
    workspace_root: Path,
    persisted_settings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    had_persisted_settings = persisted_settings is not None
    settings = dict(persisted_settings or {})
    legacy_mcp = _object_list(settings.get("mcp"))
    legacy_skills = _object_list(settings.get("skills"))

    file_mcp = load_mcp_configs(workspace_root)
    if file_mcp is None and legacy_mcp:
        save_mcp_configs(workspace_root, legacy_mcp)
        file_mcp = load_mcp_configs(workspace_root)

    file_skills = load_skill_configs(workspace_root)
    if file_skills is None and legacy_skills:
        save_skill_configs(workspace_root, legacy_skills)
        file_skills = load_skill_configs(workspace_root)

    if file_mcp is not None:
        settings["mcp"] = file_mcp
    elif "mcp" not in settings:
        settings["mcp"] = []

    if file_skills is not None:
        settings["skills"] = file_skills
    elif "skills" not in settings:
        settings["skills"] = []

    if not had_persisted_settings and file_mcp is None and file_skills is None:
        return None
    return settings


def save_file_backed_settings(workspace_root: Path, settings: dict[str, Any]) -> dict[str, Any]:
    if isinstance(settings.get("mcp"), list):
        save_mcp_configs(workspace_root, _object_list(settings.get("mcp")))
    if isinstance(settings.get("skills"), list):
        save_skill_configs(workspace_root, _object_list(settings.get("skills")))
    return strip_file_backed_settings(settings)


def load_mcp_configs(workspace_root: Path) -> list[dict[str, Any]] | None:
    path = _mcp_file_path(workspace_root)
    if not path.exists():
        return None
    payload = _read_json(path)
    return _normalize_mcp_payload(payload)


def save_mcp_configs(workspace_root: Path, configs: list[dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "servers": [_normalize_mcp_config(item, fallback_name=f"MCP {index + 1}") for index, item in enumerate(configs)],
    }
    _write_json(_mcp_file_path(workspace_root), payload)


def load_skill_configs(workspace_root: Path) -> list[dict[str, Any]] | None:
    skills_dir = _skills_dir_path(workspace_root)
    if not skills_dir.exists():
        return None

    skills: list[dict[str, Any]] = []
    for child in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        skill = _read_skill_dir(child)
        if skill is not None:
            skills.append(skill)
    return skills


def save_skill_configs(workspace_root: Path, skills: list[dict[str, Any]]) -> None:
    skills_dir = _skills_dir_path(workspace_root)
    skills_dir.mkdir(parents=True, exist_ok=True)

    existing = _read_existing_skill_dirs(skills_dir)
    next_skills = [_normalize_skill_config(item, fallback_name=f"Skill {index + 1}") for index, item in enumerate(skills)]
    next_ids = {skill["id"] for skill in next_skills}

    for skill_id, path in existing.items():
        if skill_id not in next_ids:
            _archive_skill_dir(workspace_root, path)

    for skill in next_skills:
        target = existing.get(skill["id"])
        if target is None or not target.exists():
            target = _unique_skill_dir(skills_dir, skill["name"], skill["id"])
        target.mkdir(parents=True, exist_ok=True)
        _write_json(
            target / "skill.json",
            {
                "version": 1,
                "id": skill["id"],
                "name": skill["name"],
                "description": skill["description"],
                "enabled": skill["enabled"],
            },
        )
        _write_text(target / "SKILL.md", _ensure_trailing_newline(skill["prompt"]))


def _open_eagle_dir(workspace_root: Path) -> Path:
    return workspace_root / ".open-eagle"


def _mcp_file_path(workspace_root: Path) -> Path:
    return _open_eagle_dir(workspace_root) / MCP_FILE_NAME


def _skills_dir_path(workspace_root: Path) -> Path:
    return _open_eagle_dir(workspace_root) / SKILLS_DIR_NAME


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalize_mcp_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_normalize_mcp_config(item, fallback_name=f"MCP {index + 1}") for index, item in enumerate(_object_list(payload))]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("servers"), list):
        return [
            _normalize_mcp_config(item, fallback_name=f"MCP {index + 1}")
            for index, item in enumerate(_object_list(payload.get("servers")))
        ]
    if isinstance(payload.get("mcp"), list):
        return [
            _normalize_mcp_config(item, fallback_name=f"MCP {index + 1}")
            for index, item in enumerate(_object_list(payload.get("mcp")))
        ]
    if isinstance(payload.get("mcpServers"), dict):
        return _normalize_claude_mcp_servers(payload["mcpServers"])
    return []


def _normalize_claude_mcp_servers(servers: dict[str, Any]) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for name, raw in sorted(servers.items()):
        if not isinstance(raw, dict):
            continue
        config = dict(raw)
        config.setdefault("name", name)
        config.setdefault("id", f"mcp-{_slug(name) or len(configs) + 1}")
        if not config.get("endpoint"):
            config["endpoint"] = _endpoint_from_claude_server(config)
        if not config.get("transport"):
            config["transport"] = _transport_from_claude_server(config)
        configs.append(_normalize_mcp_config(config, fallback_name=name))
    return configs


def _endpoint_from_claude_server(server: dict[str, Any]) -> str:
    command = _string_or_none(server.get("command"))
    args = server.get("args")
    if command:
        parts = [command]
        if isinstance(args, list):
            parts.extend(str(arg) for arg in args)
        return _join_command_line(parts)
    return _string_or_none(server.get("url")) or ""


def _transport_from_claude_server(server: dict[str, Any]) -> str:
    if _string_or_none(server.get("command")):
        return "stdio"
    transport = _string_or_none(server.get("type")) or _string_or_none(server.get("transport"))
    if transport:
        return transport
    url = _string_or_none(server.get("url")) or ""
    return "sse" if url.endswith("/sse") else "streamable-http"


def _normalize_mcp_config(raw: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    name = _string_or_none(raw.get("name")) or fallback_name
    config_id = _string_or_none(raw.get("id")) or f"mcp-{_slug(name) or 'server'}"
    transport = _string_or_none(raw.get("transport")) or "stdio"
    endpoint = _string_or_none(raw.get("endpoint")) or _string_or_none(raw.get("url")) or ""
    description = _string_or_none(raw.get("description")) or ""
    enabled = bool(raw.get("enabled", not bool(raw.get("disabled", False))))
    return {
        "id": config_id,
        "name": name,
        "transport": transport,
        "endpoint": endpoint,
        "description": description,
        "enabled": enabled,
    }


def _read_skill_dir(path: Path) -> dict[str, Any] | None:
    metadata = _read_json(path / "skill.json")
    if not isinstance(metadata, dict):
        metadata = {}
    skill_path = path / "SKILL.md"
    prompt = skill_path.read_text(encoding="utf-8") if skill_path.exists() else _string_or_none(metadata.get("prompt")) or ""
    if not metadata and not prompt:
        return None

    front_matter = _front_matter(prompt)
    name = _string_or_none(metadata.get("name")) or front_matter.get("name") or path.name
    return {
        "id": _string_or_none(metadata.get("id")) or path.name,
        "name": name,
        "description": _string_or_none(metadata.get("description")) or front_matter.get("description") or "",
        "prompt": prompt,
        "enabled": bool(metadata.get("enabled", True)),
    }


def _normalize_skill_config(raw: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    name = _string_or_none(raw.get("name")) or fallback_name
    return {
        "id": _string_or_none(raw.get("id")) or f"skill-{_slug(name) or 'item'}",
        "name": name,
        "description": _string_or_none(raw.get("description")) or "",
        "prompt": _string_or_none(raw.get("prompt")) or "",
        "enabled": bool(raw.get("enabled", True)),
    }


def _read_existing_skill_dirs(skills_dir: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for child in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        skill = _read_skill_dir(child)
        if skill is None:
            continue
        result[skill["id"]] = child
    return result


def _archive_skill_dir(workspace_root: Path, skill_dir: Path) -> None:
    if not skill_dir.exists():
        return
    archive_root = _open_eagle_dir(workspace_root) / DELETED_SKILLS_DIR_NAME
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = archive_root / f"{skill_dir.name}-{stamp}"
    counter = 1
    while target.exists():
        counter += 1
        target = archive_root / f"{skill_dir.name}-{stamp}-{counter}"
    shutil.move(str(skill_dir), str(target))


def _unique_skill_dir(skills_dir: Path, name: str, skill_id: str) -> Path:
    base = _slug(name) or _slug(skill_id) or "skill"
    candidate = skills_dir / base
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = skills_dir / f"{base}-{counter}"
    return candidate


def _front_matter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"name", "description"}:
            fields[key] = value.strip().strip('"')
    return {}


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", value).strip("-").lower()
    return slug[:80]


def _join_command_line(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _ensure_trailing_newline(value: str) -> str:
    return value if value.endswith("\n") else value + "\n"
