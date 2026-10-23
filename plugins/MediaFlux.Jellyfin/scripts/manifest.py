#!/usr/bin/env python3
"""插件打包与仓库清单共用的元数据和校验入口；仅使用 Python 标准库。"""

import argparse
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

PLUGIN = Path(__file__).resolve().parents[1]
DOWNLOAD = "https://github.com/li88iioo/MediaFlux/releases/download"
FIELDS = ("guid", "name", "owner", "overview", "description", "category")


def version_key(version):
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise ValueError(f"Invalid four-part version: {version!r}")
    return tuple(map(int, version.split(".")))


def metadata(plugin):
    meta = json.loads((plugin / "meta.json").read_text(encoding="utf-8"))
    project = ET.parse(plugin / "MediaFlux.Jellyfin.csproj").getroot()
    version = project.findtext("PropertyGroup/Version", "")
    assembly_version = project.findtext("PropertyGroup/AssemblyVersion", f"{version}.0")
    if (not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or assembly_version != f"{version}.0" or meta["version"] != assembly_version):
        raise ValueError("meta/assembly version mismatch")
    version_key(meta["version"])
    framework = project.findtext("PropertyGroup/TargetFramework")
    packages = {p.get("Include"): p.get("Version") for p in project.findall("ItemGroup/PackageReference")}
    if framework != "net10.0" or any(packages.get(p) != "12.2.0" for p in ("Jellyfin.Controller", "Jellyfin.Model")):
        raise ValueError("Production baseline must remain Jellyfin 12.2.0 / net10.0")
    if meta["targetAbi"] != "12.2.0.0":
        raise ValueError("meta/API baseline mismatch")
    if str(UUID(meta["guid"])) != meta["guid"] or meta["guid"] not in (plugin / "Plugin.cs").read_text(encoding="utf-8"):
        raise ValueError("meta/plugin GUID mismatch")
    assembly = project.findtext("PropertyGroup/AssemblyName", "") + ".dll"
    if meta["assemblies"] != [assembly]:
        raise ValueError("Package must contain only the plugin assembly")
    if meta.get("imagePath") != "logo.png":
        raise ValueError("meta imagePath must refer to the packaged logo.png")
    datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00"))
    return {**meta, "assembly": assembly, "framework": framework,
            "tag": f"jellyfin-plugin-v{version}", "archive": f"MediaFlux.Jellyfin-{meta['version']}.zip"}


def package_files(plugin, meta):
    repo = plugin.parents[1]
    return {
        meta["assembly"]: plugin / "bin" / "Release" / meta["framework"] / meta["assembly"],
        "meta.json": plugin / "meta.json",
        "logo.png": plugin / "Resources" / "logo.png",
        "README.md": repo / "docs" / "jellyfin-plugin.md",
        "LICENSE": repo / "LICENSE",
    }


def package(plugin):
    meta = metadata(plugin)
    contents = {name: path.read_bytes() for name, path in package_files(plugin, meta).items()}
    if not contents["logo.png"].startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Resources/logo.png must be a PNG image")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            # 固定 ZIP 时间戳、权限与顺序，避免重跑时仅文件属性变化造成同版本冲突。
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    output = plugin / "artifacts"
    output.mkdir(exist_ok=True)
    path = output / meta["archive"]
    path.write_bytes(buffer.getvalue())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".zip.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    (output / "logo.png").write_bytes(contents["logo.png"])
    print(f"Package: {path}\nSHA256: {digest}")
    return path


def update(plugin, previous, archive, output):
    meta = metadata(plugin)
    with zipfile.ZipFile(archive) as zipped:
        if (archive.name != meta["archive"] or len(zipped.namelist()) != 5
                or set(zipped.namelist()) != set(package_files(plugin, meta)) or zipped.testzip()
                or json.loads(zipped.read("meta.json")) != json.loads((plugin / "meta.json").read_text(encoding="utf-8"))):
            raise ValueError("Invalid plugin ZIP contents or metadata")
    history = json.loads(previous.read_text(encoding="utf-8"))
    if not isinstance(history, list) or (history and (
            len(history) != 1 or not isinstance(history[0], dict)
            or history[0].get("guid") != meta["guid"]
            or not all(field in history[0] for field in FIELDS)
            or not isinstance(history[0].get("versions"), list))):
        raise ValueError("Refusing to replace an unknown manifest")
    entry = history[0] if history else {**{k: meta[k] for k in FIELDS}, "versions": []}
    versions = entry["versions"]
    seen = set()
    for item in versions:
        version_key(item["version"])
        version_key(item["targetAbi"])
        if (item["version"] in seen or not re.fullmatch(r"[a-fA-F0-9]{32}", item["checksum"])
                or not item["sourceUrl"].startswith(DOWNLOAD + "/") or not item.get("timestamp")):
            raise ValueError("Refusing to replace an unknown manifest version")
        seen.add(item["version"])
    # Jellyfin InstallationManager 使用 ZIP 的 MD5；发布资产另以 SHA256 回读验证。
    release = {"version": meta["version"], "targetAbi": meta["targetAbi"],
               "sourceUrl": f"{DOWNLOAD}/{meta['tag']}/{meta['archive']}",
               "checksum": hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest(),
               "timestamp": meta["timestamp"], "changelog": meta.get("changelog", meta["overview"])}
    for item in versions:
        if item["version"] == release["version"]:
            if (item["checksum"].lower() != release["checksum"]
                    or any(item[key] != release[key] for key in ("targetAbi", "sourceUrl"))):
                raise ValueError("Refusing different contents for an existing plugin version")
            break
    else:
        if not versions or version_key(meta["version"]) > max(version_key(v["version"]) for v in versions):
            entry.update({k: meta[k] for k in FIELDS})
            entry["imageUrl"] = f"{DOWNLOAD}/{meta['tag']}/logo.png"
        versions.append(release)
    versions.sort(key=lambda item: version_key(item["version"]), reverse=True)
    output.write_text(json.dumps([entry], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    commands.add_parser("package")
    update_parser = commands.add_parser("update")
    for name in ("previous", "archive", "output"):
        update_parser.add_argument(name, type=Path)
    args = parser.parse_args()
    if args.command == "info":
        print(json.dumps(metadata(PLUGIN), ensure_ascii=False))
    elif args.command == "package":
        package(PLUGIN)
    else:
        update(PLUGIN, args.previous, args.archive, args.output)


if __name__ == "__main__":
    main()
