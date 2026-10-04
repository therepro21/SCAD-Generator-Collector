from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps


PREVIEW_NAME = "Folder.jpg"
ICON_NAME = ".scad-preview.ico"


def openscad_command() -> Path | None:
    found = shutil.which("openscad")
    candidates = ([Path(found)] if found else []) + [
        Path(r"C:\Program Files\OpenSCAD\openscad.exe"),
        Path(r"C:\Program Files (x86)\OpenSCAD\openscad.exe"),
    ]
    portable = os.environ.get("SCAD_PORTABLE_OPENSCAD")
    if portable:
        candidates.insert(0, Path(portable))
    return next((item for item in candidates if item.is_file()), None)


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _attributes(path: Path, add: int = 0, remove: int = 0) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if current != 0xFFFFFFFF:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), (current | add) & ~remove)
    except (AttributeError, OSError):
        pass


def _notify(folder: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005, ctypes.c_wchar_p(str(folder)), None)
    except (AttributeError, OSError):
        pass


def configure_icon(folder: Path, preview: Path) -> None:
    desktop = folder / "desktop.ini"
    current = desktop.read_text(encoding="utf-16", errors="ignore") if desktop.exists() else ""
    icon_name = ICON_NAME
    if (folder / icon_name).exists():
        icon_name = ".scad-preview-b.ico" if ".scad-preview-a.ico" in current else ".scad-preview-a.ico"
    icon = folder / icon_name
    _attributes(icon, remove=0x01 | 0x02 | 0x04)
    with Image.open(preview) as source:
        source.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS).save(
            icon, format="ICO", sizes=[(256, 256)], bitmap_format="png"
        )
    _attributes(desktop, remove=0x01 | 0x02 | 0x04)
    desktop.write_text(
        f"[.ShellClassInfo]\nIconResource={icon_name},0\nIconFile={icon_name}\nIconIndex=0\n",
        encoding="utf-16",
    )
    _attributes(folder, add=0x01)
    _attributes(desktop, add=0x02 | 0x04)
    _attributes(icon, add=0x02)
    _notify(folder)


def render_scad(source: Path, destination: Path) -> None:
    command = openscad_command()
    if command is None:
        raise RuntimeError("OpenSCAD wurde nicht gefunden")
    with tempfile.TemporaryDirectory(prefix="scad-vorschau-") as temp:
        raw = Path(temp) / "render.png"
        result = subprocess.run(
            [str(command), "--preview", "--imgsize=512,512", "--viewall", "--autocenter",
             "--colorscheme=Tomorrow", "-o", str(raw), str(source)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=45, creationflags=_flags(), cwd=str(source.parent),
        )
        if result.returncode or not raw.is_file() or raw.stat().st_size < 500:
            raise RuntimeError((result.stderr or result.stdout or "Rendern fehlgeschlagen")[-500:])
        with Image.open(raw) as opened:
            image = opened.convert("RGB")
            low, high = image.convert("L").getextrema()
            if high - low < 12:
                raise RuntimeError("OpenSCAD-Ausgabe enthält keine sichtbare Geometrie")
            image = ImageOps.contain(image, (384, 384), Image.Resampling.LANCZOS)
            image = ImageEnhance.Contrast(image).enhance(1.05)
            canvas = Image.new("RGB", (384, 384), (246, 246, 246))
            canvas.paste(image, ((384 - image.width) // 2, (384 - image.height) // 2))
        temporary = destination.with_suffix(".jpg.part")
        canvas.save(temporary, format="JPEG", quality=78, optimize=True)
        os.replace(temporary, destination)


def render_fallback(folder: Path, destination: Path) -> None:
    """Create an honest labeled icon when a source folder has no renderable geometry."""
    canvas = Image.new("RGB", (384, 384), (242, 245, 248))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((34, 38, 350, 346), radius=28, fill=(53, 91, 141), outline=(28, 55, 91), width=5)
    draw.rounded_rectangle((69, 74, 315, 238), radius=18, fill=(230, 237, 246))
    try:
        large = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 62)
        small = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 22)
    except OSError:
        large = small = ImageFont.load_default()
    draw.text((192, 155), "SCAD", font=large, fill=(53, 91, 141), anchor="mm")
    name = folder.name
    if len(name) > 28:
        name = name[:25] + "…"
    draw.text((192, 285), name, font=small, fill="white", anchor="mm")
    draw.text((192, 319), "keine Vorschaugeometrie", font=small, fill=(210, 222, 238), anchor="mm")
    temporary = destination.with_suffix(".jpg.part")
    canvas.save(temporary, format="JPEG", quality=82, optimize=True)
    os.replace(temporary, destination)


def ensure_folder_preview(folder: Path, force: bool = False) -> str:
    candidates = sorted(
        (item for item in folder.glob("*.scad") if item.is_file() and item.stat().st_size > 0),
        key=lambda item: (-item.stat().st_size, item.name.casefold()),
    )
    if not candidates:
        return "no_mesh"
    preview = folder / PREVIEW_NAME
    if not force and preview.is_file() and preview.stat().st_size > 0:
        if not (folder / "desktop.ini").is_file():
            configure_icon(folder, preview)
        return "current"
    # Try several likely entry points; library/helper files often do not render by themselves.
    for source in candidates[:8]:
        try:
            render_scad(source, preview)
            configure_icon(folder, preview)
            return "created"
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            continue
    render_fallback(folder, preview)
    configure_icon(folder, preview)
    return "fallback"


def backfill_previews(library: Path, log: Callable[[str], None], stop, force: bool = False) -> dict:
    folders = sorted({item.parent for item in library.rglob("*.scad")})
    result = {"created": 0, "current": 0, "fallback": 0, "no_mesh": 0, "unreadable": 0, "errors": 0}

    def one(folder: Path) -> str:
        if stop.is_set():
            return "stopped"
        try:
            return ensure_folder_preview(folder, force=force)
        except Exception:
            return "errors"

    # Two OpenSCAD processes keep the desktop responsive and avoid excessive RAM usage.
    with ThreadPoolExecutor(max_workers=2) as executor:
        for index, status in enumerate(executor.map(one, folders), 1):
            if status == "stopped":
                break
            result[status] += 1
            if index == 1 or index % 10 == 0 or index == len(folders):
                log(f"Vorschauen: {index}/{len(folders)} Ordner – {result['created']} neu, {result['current']} vorhanden, {result['fallback']} Ersatzsymbole")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except (AttributeError, OSError):
            pass
    return result
