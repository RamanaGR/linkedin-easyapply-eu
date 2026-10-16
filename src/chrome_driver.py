"""Ensure an Apple Silicon–compatible ChromeDriver for undetected-chromedriver."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx

from src.utils import project_path

logger = logging.getLogger("easyapply")

UC_CACHE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "undetected_chromedriver"
    / "undetected_chromedriver"
)
CFT_META = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
CFT_MILESTONE = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "latest-versions-per-milestone-with-downloads.json"
)


def chrome_major_version() -> int | None:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        return None
    try:
        out = subprocess.check_output([str(chrome), "--version"], text=True).strip()
        for p in out.split():
            if p and p[0].isdigit():
                return int(p.split(".")[0])
    except Exception as exc:
        logger.warning("Could not read Chrome version: %s", exc)
    return None


def _file_arch(path: Path) -> str:
    try:
        return subprocess.check_output(["file", str(path)], text=True)
    except Exception:
        return ""


def purge_bad_uc_cache() -> None:
    """Remove x86_64-only UC cache on arm64 Macs (Errno 86 Bad CPU type)."""
    if platform.machine() != "arm64" or not UC_CACHE.exists():
        return
    info = _file_arch(UC_CACHE)
    if "x86_64" in info and "arm64" not in info:
        logger.warning("Removing wrong-arch chromedriver cache: %s", UC_CACHE)
        UC_CACHE.unlink(missing_ok=True)


def _mac_arm64_url_from_downloads(downloads: dict) -> str | None:
    for item in downloads.get("chromedriver", []):
        if "mac-arm64" in item.get("url", ""):
            return item["url"]
    return None


def resolve_arm64_chromedriver_url() -> tuple[str | None, str | None]:
    major = chrome_major_version()
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        if major is not None:
            r = client.get(CFT_MILESTONE)
            r.raise_for_status()
            mile = r.json().get("milestones", {}).get(str(major), {})
            if mile:
                url = _mac_arm64_url_from_downloads(mile.get("downloads", {}))
                if url:
                    return mile.get("version"), url
        r = client.get(CFT_META)
        r.raise_for_status()
        stable = r.json()["channels"]["Stable"]
        url = _mac_arm64_url_from_downloads(stable.get("downloads", {}))
        return stable.get("version"), url


def ensure_arm64_chromedriver() -> Path | None:
    """Download Chrome for Testing mac-arm64 chromedriver matching installed Chrome."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None

    drivers_dir = project_path(".drivers")
    drivers_dir.mkdir(parents=True, exist_ok=True)
    target = drivers_dir / "chromedriver"

    if target.exists() and "arm64" in _file_arch(target):
        # Ensure previously downloaded binaries are runnable on macOS
        try:
            subprocess.run(
                [str(target), "--version"],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            _trust_binary(target)
        return target
    if target.exists():
        target.unlink(missing_ok=True)

    try:
        version, url = resolve_arm64_chromedriver_url()
    except Exception as exc:
        logger.warning("Could not resolve chromedriver URL: %s", exc)
        return None
    if not url:
        logger.error("No mac-arm64 chromedriver URL found")
        return None

    zip_path = drivers_dir / "chromedriver-mac-arm64.zip"
    logger.info("Downloading arm64 chromedriver (%s)...", version)
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            zip_path.write_bytes(r.content)
        with zipfile.ZipFile(zip_path) as zf:
            member = next(
                (
                    n
                    for n in zf.namelist()
                    if n.endswith("/chromedriver") or n == "chromedriver"
                ),
                None,
            )
            if not member:
                raise RuntimeError(f"chromedriver not in zip: {zf.namelist()[:8]}")
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        target.chmod(0o755)
        zip_path.unlink(missing_ok=True)
        _trust_binary(target)
        logger.info("Installed arm64 chromedriver → %s", target)
        return target
    except Exception as exc:
        logger.error("arm64 chromedriver download failed: %s", exc)
        return None


def _trust_binary(path: Path) -> None:
    """Clear quarantine and ad-hoc codesign so macOS does not SIGKILL the driver."""
    try:
        subprocess.run(["xattr", "-cr", str(path)], check=False, capture_output=True)
    except Exception:
        pass
    try:
        subprocess.run(
            ["codesign", "-s", "-", "--force", "--deep", str(path)],
            check=False,
            capture_output=True,
        )
    except Exception as exc:
        logger.warning("codesign failed (may still work): %s", exc)
