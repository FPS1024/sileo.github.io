#!/usr/bin/env python3
"""Build the Sileo APT repository from GitHub Releases.

The package files stay in the release assets of their source repositories. This
script only reads their control metadata (using HTTP Range requests), writes the
APT indexes, and generates the package list for the website.

The resulting Packages file uses absolute GitHub Release URLs in its Filename
field. Sileo supports that; standard APT does not.
"""

from __future__ import annotations

import argparse
import bz2
import datetime as dt
import fnmatch
import gzip
import hashlib
import html
import io
import json
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
USER_AGENT = "fps1024-sileo-repo-builder/1.0"
CURL = shutil.which("curl")
PREFIX_BYTES = 4096
MAX_CONTROL_ARCHIVE_BYTES = 8 * 1024 * 1024
VOLATILE_FIELDS = {"filename", "size", "md5sum", "sha1", "sha256", "sha512"}


class BuildError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def github_token() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def run_curl(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    if not CURL:
        raise BuildError("curl is required but was not found in PATH")
    command = [
        CURL,
        "-sS",
        "-L",
        "--retry",
        "3",
        "--retry-delay",
        "1",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout),
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "-H",
        f"User-Agent: {USER_AGENT}",
    ]
    command += args
    return subprocess.run(command, capture_output=True)


def api_get(path: str) -> object:
    args = [
        "-f",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
    ]
    token = github_token()
    if token:
        args += ["-H", f"Authorization: Bearer {token}"]
    args.append(API + path)

    result = run_curl(args, timeout=120)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BuildError(f"GitHub API request failed for {path}: {detail[:400]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"GitHub API returned invalid JSON for {path}") from exc


def curl_range(url: str, start: int, end: int) -> bytes | None:
    """Return exactly the requested byte range, or None if unsupported."""
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        args = [
            "-f",
            "--max-filesize",
            str(end - start + 1),
            "-H",
            f"Range: bytes={start}-{end}",
            "-o",
            str(temp_path),
            "-w",
            "%{http_code}",
            url,
        ]
        result = run_curl(args, timeout=120)
        if result.returncode != 0:
            return None
        if result.stdout.decode("ascii", "replace").strip() != "206":
            return None
        data = temp_path.read_bytes()
        if len(data) != end - start + 1:
            return None
        return data
    finally:
        temp_path.unlink(missing_ok=True)


def curl_download(url: str, destination: Path) -> tuple[int, str]:
    args = [
        "-f",
        "-o",
        str(destination),
        url,
    ]
    result = run_curl(args, timeout=900)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BuildError(f"download failed for {url}: {detail[:400]}")

    digest = hashlib.sha256()
    size = 0
    with destination.open("rb") as handle:
        while chunk := handle.read(1024 * 256):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def parse_ar(data: bytes) -> list[dict[str, object]]:
    if not data.startswith(b"!<arch>\n"):
        raise BuildError("not an ar archive")

    members: list[dict[str, object]] = []
    position = 8
    while position + 60 <= len(data):
        header = data[position : position + 60]
        if header[58:60] != b"`\n":
            break
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        size_text = header[48:58].decode("ascii", "replace").strip()
        if not size_text.isdigit():
            break
        size = int(size_text)
        members.append(
            {
                "name": name,
                "start": position + 60,
                "size": size,
            }
        )
        position = position + 60 + size + (size % 2)

    if not members:
        raise BuildError("no ar members found")
    return members


def decompress_control_member(name: str, blob: bytes) -> bytes:
    lowered = name.lower()
    if lowered.endswith(".tar.gz"):
        return gzip.decompress(blob)
    if lowered.endswith(".tar.bz2"):
        return bz2.decompress(blob)
    if lowered.endswith(".tar.xz") or lowered.endswith(".tar.lzma"):
        return lzma.decompress(blob)
    if lowered.endswith(".tar.zst"):
        zstd = shutil.which("zstd")
        if not zstd:
            raise BuildError("zstd is required to read control.tar.zst")
        result = subprocess.run(
            [zstd, "-dc"],
            input=blob,
            capture_output=True,
        )
        if result.returncode != 0:
            raise BuildError(
                f"zstd failed to decompress control archive: "
                f"{result.stderr[:200]!r}"
            )
        return result.stdout
    if lowered.endswith(".tar"):
        return blob
    raise BuildError(f"unsupported control archive: {name}")


def control_from_tar(tar_bytes: bytes) -> str:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.split("/")[-1] == "control":
                fileobj = archive.extractfile(member)
                if fileobj is not None:
                    return fileobj.read().decode("utf-8", "replace")
    raise BuildError("control file not found in control archive")


def control_from_archive(name: str, blob: bytes) -> str:
    return control_from_tar(decompress_control_member(name, blob))


def control_from_url(url: str) -> str | None:
    prefix = curl_range(url, 0, PREFIX_BYTES - 1)
    if prefix is None:
        return None

    try:
        members = parse_ar(prefix)
    except BuildError:
        return None

    member = next(
        (
            item
            for item in members
            if str(item["name"]).lower().startswith("control.tar")
        ),
        None,
    )
    if member is None:
        return None

    size = int(member["size"])
    if size > MAX_CONTROL_ARCHIVE_BYTES:
        return None

    start = int(member["start"])
    end = start + size
    if end <= len(prefix):
        blob = prefix[start:end]
    else:
        blob = curl_range(url, start, end - 1)
        if blob is None:
            return None

    try:
        control = control_from_archive(str(member["name"]), blob)
    except BuildError:
        return None
    return control or None


def control_from_file(path: Path) -> str:
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb:
        result = subprocess.run(
            [dpkg_deb, "-f", str(path)],
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.decode("utf-8", "replace")

    data = path.read_bytes()
    members = parse_ar(data)
    member = next(
        (
            item
            for item in members
            if str(item["name"]).lower().startswith("control.tar")
        ),
        None,
    )
    if member is None:
        raise BuildError(f"no control archive in {path.name}")
    start = int(member["start"])
    size = int(member["size"])
    return control_from_archive(str(member["name"]), data[start : start + size])


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and current_key is not None:
            fields[current_key] += "\n" + line[1:]
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip().lower()
        fields[current_key] = value.strip()

    return fields


def strip_volatile_fields(text: str) -> str:
    lines: list[str] = []
    dropping = False

    for line in text.splitlines():
        if line[:1] in (" ", "\t"):
            if not dropping:
                lines.append(line)
            continue
        key = line.split(":", 1)[0].strip().lower() if ":" in line else ""
        if key in VOLATILE_FIELDS:
            dropping = True
            continue
        dropping = False
        lines.append(line)

    return "\n".join(lines).strip("\n")


def version_is_newer(candidate: str, current: str) -> bool:
    dpkg = shutil.which("dpkg")
    if dpkg:
        result = subprocess.run(
            [dpkg, "--compare-versions", candidate, "gt", current],
            capture_output=True,
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
    return candidate > current


def resolve_asset(
    asset: dict[str, object],
    release: dict[str, object],
    repo: str,
    config: dict[str, object],
    target_architectures: set[str],
) -> dict[str, object] | None:
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith("https://"):
        raise BuildError(f"{repo}: invalid download URL for {name}")

    api_size = asset.get("size")
    try:
        api_size = int(api_size) if api_size is not None else None
    except (TypeError, ValueError):
        api_size = None

    digest = str(asset.get("digest") or "")
    sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
    if sha256 is not None and (
        len(sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in sha256)
    ):
        sha256 = None

    control_text = control_from_url(url)
    if control_text is None or sha256 is None or api_size is None:
        log(f"  {name}: downloading full asset for control or checksum")
        with tempfile.TemporaryDirectory(prefix="sileo-deb-") as temp_dir:
            downloaded_path = Path(temp_dir) / "package.deb"
            downloaded_size, downloaded_sha = curl_download(url, downloaded_path)

            if sha256 is not None and downloaded_sha.lower() != sha256.lower():
                raise BuildError(
                    f"{repo}: sha256 mismatch for {name} "
                    f"(expected {sha256}, got {downloaded_sha})"
                )
            if api_size is not None and downloaded_size != api_size:
                raise BuildError(
                    f"{repo}: size mismatch for {name} "
                    f"(expected {api_size}, got {downloaded_size})"
                )

            api_size = downloaded_size
            sha256 = downloaded_sha
            control_text = control_from_file(downloaded_path)

    if control_text is None or not control_text.strip():
        raise BuildError(f"{repo}: empty control data for {name}")

    fields = parse_control(control_text)
    package = fields.get("package", "").strip()
    version = fields.get("version", "").strip()
    architecture = fields.get("architecture", "").strip()

    if not package or not version or not architecture:
        raise BuildError(
            f"{repo}: {name} is missing Package, Version, or Architecture"
        )

    include_all = bool(config.get("include_arch_all", True))
    if architecture not in target_architectures and not (
        include_all and architecture == "all"
    ):
        log(f"  {name}: skipping architecture {architecture}")
        return None

    description = fields.get("description", "").strip()
    short_description = (
        description.splitlines()[0].strip() if description else ""
    )

    return {
        "package": package,
        "name": fields.get("name", "").strip() or package,
        "version": version,
        "architecture": architecture,
        "description": description,
        "short_description": short_description,
        "section": fields.get("section", ""),
        "priority": fields.get("priority", ""),
        "depends": fields.get("depends", ""),
        "maintainer": fields.get("maintainer", ""),
        "author": fields.get("author", ""),
        "homepage": fields.get("homepage", ""),
        "size": int(api_size or 0),
        "sha256": str(sha256),
        "filename": url,
        "release_url": str(release.get("html_url") or ""),
        "source_repo": repo,
        "updated_at": str(
            release.get("published_at")
            or asset.get("updated_at")
            or ""
        ),
        "control": control_text.strip() + "\n",
    }


def packages_from_repo(
    repo_config: dict[str, object],
    config: dict[str, object],
) -> list[dict[str, object]]:
    repo = str(repo_config["repo"])
    pattern = str(repo_config.get("asset", "*.deb"))
    raw_excludes = repo_config.get("exclude_assets", [])
    if isinstance(raw_excludes, str):
        excludes = [raw_excludes]
    else:
        excludes = [str(item) for item in raw_excludes]
    limit = int(config.get("release_scan_limit", 8))
    limit = max(1, min(limit, 100))

    releases = api_get(f"/repos/{repo}/releases?per_page={limit}")
    if not isinstance(releases, list):
        raise BuildError(f"{repo}: unexpected GitHub API response")

    candidates = [release for release in releases if not release.get("draft")]
    if not config.get("include_prereleases", False):
        candidates = [
            release for release in candidates if not release.get("prerelease")
        ]
    candidates.sort(
        key=lambda release: str(
            release.get("published_at") or release.get("created_at") or ""
        ),
        reverse=True,
    )

    if not candidates:
        log(f"{repo}: no published releases found")
        return []

    target_architectures = set(config["architectures"])

    for release in candidates[:limit]:
        assets = []
        for asset in release.get("assets", []):
            asset_name = str(asset.get("name") or "")
            lowered = asset_name.lower()
            if not fnmatch.fnmatch(lowered, pattern.lower()):
                continue
            if any(
                fnmatch.fnmatch(lowered, exclude.lower())
                for exclude in excludes
            ):
                log(f"  {asset_name}: excluded by repository configuration")
                continue
            assets.append(asset)

        if not assets:
            continue

        found: list[dict[str, object]] = []
        for asset in assets:
            package = resolve_asset(
                asset,
                release,
                repo,
                config,
                target_architectures,
            )
            if package is not None:
                found.append(package)

        if found:
            log(
                f"{repo}: using release {release.get('tag_name')} "
                f"({len(found)} package(s))"
            )
            return found

        log(
            f"{repo}: release {release.get('tag_name')} has no matching "
            "architecture, checking older releases"
        )

    log(f"{repo}: no matching .deb found in the scanned releases")
    return []


def deduplicate(
    packages: list[dict[str, object]],
) -> list[dict[str, object]]:
    chosen: dict[tuple[str, str], dict[str, object]] = {}

    for package in packages:
        key = (
            str(package["package"]),
            str(package["architecture"]),
        )
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = package
            continue

        if version_is_newer(
            str(package["version"]),
            str(previous["version"]),
        ):
            log(
                f"dedupe: {key[0]} {package['version']} replaces "
                f"{previous['version']}"
            )
            chosen[key] = package
        else:
            log(
                f"dedupe: keeping {key[0]} {previous['version']} over "
                f"{package['version']}"
            )

    return sorted(
        chosen.values(),
        key=lambda package: (
            str(package["package"]).lower(),
            str(package["architecture"]),
        ),
    )


def build_packages_text(packages: list[dict[str, object]]) -> bytes:
    stanzas: list[str] = []

    for package in packages:
        control = strip_volatile_fields(str(package["control"])).rstrip("\n")
        stanzas.append(
            control
            + "\n"
            + f"Filename: {package['filename']}\n"
            + f"Size: {package['size']}\n"
            + f"SHA256: {package['sha256']}\n"
        )

    text = "\n".join(stanzas)
    return text.encode("utf-8") if text else b""


def gzip_bytes(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(
        fileobj=buffer,
        mode="wb",
        compresslevel=9,
        mtime=0,
    ) as handle:
        handle.write(data)
    return buffer.getvalue()


def build_release(
    config: dict[str, object],
    files: list[tuple[str, bytes]],
) -> str:
    architectures = " ".join(str(item) for item in config["architectures"])
    lines = [
        f"Origin: {config['origin']}",
        f"Label: {config['label']}",
        f"Suite: {config['suite']}",
        f"Version: {config['version']}",
        f"Codename: {config['codename']}",
        f"Architectures: {architectures}",
        "Components: main",
        f"Description: {config['description']}",
    ]

    for algorithm, field in (("md5", "MD5Sum"), ("sha256", "SHA256")):
        lines.append(f"{field}:")
        for name, data in files:
            digest = hashlib.new(algorithm, data).hexdigest()
            lines.append(f" {digest} {len(data)} {name}")

    return "\n".join(lines) + "\n"


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def render_summary(
    packages: list[dict[str, object]],
    generated_at: str,
) -> str:
    count = len(packages)
    if count == 0:
        return "No packages were found. Check <code>config/repos.json</code>."

    noun = "package" if count == 1 else "packages"
    return (
        f"{count} {noun} &middot; index generated "
        f"{html.escape(generated_at)} &middot; downloads are served directly "
        "from GitHub Releases."
    )


def render_rows(packages: list[dict[str, object]]) -> str:
    if not packages:
        return (
            '<tr><td colspan="5">No packages were found. Check the source '
            "repository configuration.</td></tr>"
        )

    rows: list[str] = []

    for package in packages:
        display_name = html.escape(str(package["name"]))
        package_id = html.escape(str(package["package"]))

        name_cell = f"<strong>{display_name}</strong>"
        if str(package["name"]).lower() != str(package["package"]).lower():
            name_cell += f"<br><code>{package_id}</code>"

        short_description = str(package.get("short_description") or "")
        if short_description:
            name_cell += (
                f'<div class="package-desc">'
                f"{html.escape(short_description)}</div>"
            )

        source_repo = str(package.get("source_repo") or "")
        if source_repo:
            source_url = f"https://github.com/{source_repo}"
            name_cell += (
                f'<div class="package-source">from '
                f'<a href="{html.escape(source_url, quote=True)}">'
                f"{html.escape(source_repo)}</a></div>"
            )

        version_cell = (
            html.escape(str(package["version"]))
            + f'<div class="package-arch">'
            + html.escape(str(package["architecture"]))
            + "</div>"
        )

        updated = str(package.get("updated_at") or "")[:10] or "—"
        release_url = str(package.get("release_url") or "")
        if release_url:
            updated_cell = (
                f'<a href="{html.escape(release_url, quote=True)}">'
                f"{html.escape(updated)}</a>"
            )
        else:
            updated_cell = html.escape(updated)

        download_url = html.escape(str(package["filename"]), quote=True)

        rows.append(
            "<tr>\n"
            f'    <td data-label="Package">{name_cell}</td>\n'
            f'    <td data-label="Version">{version_cell}</td>\n'
            f'    <td data-label="Size">{format_size(int(package["size"]))}</td>\n'
            f'    <td data-label="Updated">{updated_cell}</td>\n'
            f'    <td data-label="Download">'
            f'<a href="{download_url}">Download .deb</a></td>\n'
            "</tr>"
        )

    return "\n".join(rows)


def write_packages_json(
    out_dir: Path,
    config: dict[str, object],
    packages: list[dict[str, object]],
    generated_at_iso: str,
    packages_data: bytes,
) -> None:
    entries = [
        {
            "package": package["package"],
            "name": package["name"],
            "version": package["version"],
            "architecture": package["architecture"],
            "description": package["description"],
            "short_description": package["short_description"],
            "section": package["section"],
            "priority": package["priority"],
            "depends": package["depends"],
            "maintainer": package["maintainer"],
            "author": package["author"],
            "homepage": package["homepage"],
            "size": package["size"],
            "sha256": package["sha256"],
            "filename": package["filename"],
            "release_url": package["release_url"],
            "source_repo": package["source_repo"],
            "updated_at": package["updated_at"],
        }
        for package in packages
    ]

    hash_payload = packages_data + b"\n" + json.dumps(
        entries,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    content_hash = hashlib.sha256(hash_payload).hexdigest()

    document = {
        "generated_at": generated_at_iso,
        "content_hash": content_hash,
        "repository": {
            "origin": config["origin"],
            "label": config["label"],
            "suite": config["suite"],
            "codename": config["codename"],
            "architectures": config["architectures"],
        },
        "packages": entries,
    }

    (out_dir / "packages.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def validate_output_dir(out_dir: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        out_dir == ROOT
        or out_dir == temp_root
        or out_dir == Path("/")
        or (ROOT not in out_dir.parents and temp_root not in out_dir.parents)
    ):
        raise BuildError(f"refusing to clean unsafe output directory: {out_dir}")


def write_outputs(
    out_dir: Path,
    template_path: Path,
    config: dict[str, object],
    packages: list[dict[str, object]],
) -> None:
    validate_output_dir(out_dir)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    packages_data = build_packages_text(packages)
    index_files: list[tuple[str, bytes]] = [
        ("Packages", packages_data),
        ("Packages.gz", gzip_bytes(packages_data)),
        ("Packages.bz2", bz2.compress(packages_data, compresslevel=9)),
        (
            "Packages.xz",
            lzma.compress(
                packages_data,
                format=lzma.FORMAT_XZ,
                preset=9,
            ),
        ),
    ]

    for name, data in index_files:
        (out_dir / name).write_bytes(data)

    (out_dir / "Release").write_text(
        build_release(config, index_files),
        encoding="utf-8",
    )

    template = template_path.read_text(encoding="utf-8")
    for marker in ("<!-- PACKAGES_SUMMARY -->", "<!-- PACKAGES_ROWS -->"):
        if marker not in template:
            raise BuildError(f"missing {marker} in {template_path}")

    now = dt.datetime.now(dt.timezone.utc)
    generated_at_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    generated_at_display = now.strftime("%Y-%m-%d %H:%M UTC")

    rendered = template.replace(
        "<!-- PACKAGES_SUMMARY -->",
        render_summary(packages, generated_at_display),
    )
    rendered = rendered.replace(
        "<!-- PACKAGES_ROWS -->",
        render_rows(packages),
    )
    (out_dir / "index.html").write_text(rendered, encoding="utf-8")

    for name in ("styles.css", ".nojekyll"):
        source = ROOT / name
        if not source.exists():
            raise BuildError(f"missing required file: {name}")
        shutil.copy2(source, out_dir / name)

    for name in ("CydiaIcon.png", "CydiaIcon@2x.png", "CydiaIcon@3x.png"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, out_dir / name)

    write_packages_json(
        out_dir,
        config,
        packages,
        generated_at_iso,
        packages_data,
    )


def load_config(path: Path) -> dict[str, object]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildError(f"config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(config, dict):
        raise BuildError("config must be a JSON object")

    required = (
        "origin",
        "label",
        "suite",
        "codename",
        "version",
        "description",
        "architectures",
        "repositories",
    )
    for key in required:
        if key not in config:
            raise BuildError(f"config is missing required field: {key}")

    architectures = config["architectures"]
    if (
        not isinstance(architectures, list)
        or not architectures
        or not all(isinstance(item, str) and item for item in architectures)
    ):
        raise BuildError("config.architectures must be a non-empty list of strings")

    raw_repositories = config["repositories"]
    if not isinstance(raw_repositories, list) or not raw_repositories:
        raise BuildError("config.repositories must be a non-empty list")

    repositories: list[dict[str, object]] = []
    for item in raw_repositories:
        if isinstance(item, str):
            item = {"repo": item}
        if not isinstance(item, dict):
            raise BuildError("each repository entry must be a string or object")
        repo = item.get("repo")
        if not isinstance(repo, str) or "/" not in repo:
            raise BuildError(f"invalid repository entry: {item!r}")
        asset = item.get("asset", "*.deb")
        if not isinstance(asset, str) or not asset:
            raise BuildError(f"invalid asset pattern for {repo}")

        raw_excludes = item.get("exclude_assets", [])
        if isinstance(raw_excludes, str):
            excludes = [raw_excludes]
        elif isinstance(raw_excludes, list) and all(
            isinstance(entry, str) and entry for entry in raw_excludes
        ):
            excludes = raw_excludes
        else:
            raise BuildError(f"invalid exclude_assets for {repo}")

        repositories.append(
            {
                "repo": repo,
                "asset": asset,
                "exclude_assets": excludes,
            }
        )

    config["repositories"] = repositories
    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the Sileo repository from GitHub Releases.",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config/repos.json"),
        help="repository configuration (default: config/repos.json)",
    )
    parser.add_argument(
        "--template",
        default=str(ROOT / "index.html"),
        help="HTML template (default: index.html)",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "public"),
        help="output directory (default: public)",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    out_dir = Path(args.out).resolve()
    template_path = Path(args.template).resolve()
    validate_output_dir(out_dir)

    if not github_token():
        log(
            "warning: GH_TOKEN/GITHUB_TOKEN is not set; "
            "GitHub API rate limits will be lower."
        )

    packages: list[dict[str, object]] = []
    for repo_config in config["repositories"]:
        packages.extend(packages_from_repo(repo_config, config))

    packages = deduplicate(packages)
    if not packages:
        raise BuildError(
            "no packages found; refusing to publish an empty repository"
        )

    write_outputs(out_dir, template_path, config, packages)

    log(f"built {len(packages)} package(s):")
    for package in packages:
        log(
            f"  {package['package']} {package['version']} "
            f"{package['architecture']} -> {package['filename']}"
        )
    log(f"wrote {out_dir}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
