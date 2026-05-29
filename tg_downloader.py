import argparse
import asyncio
import json
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from tqdm import tqdm

try:
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TaskID,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
except ImportError:
    Progress = None
    TaskID = int


class ConfigError(ValueError):
    pass


def sanitize_filename(name: str) -> str:
    # Normalize to ASCII and keep filenames portable across Windows/Linux/macOS.
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "unnamed"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    if name.upper() in reserved:
        name = f"_{name}"
    return name


def normalize_caption(text: str) -> str:
    # Keep caption readable in filenames: single spaces, no newlines/tabs.
    text = re.sub(r"\s+", " ", text).strip()
    return sanitize_filename(text)


def build_filename(index: int, caption: str, fallback_base: str, ext: str, max_len: int) -> str:
    prefix = f"{index:010d} - "
    chosen = normalize_caption(caption) if caption.strip() else sanitize_filename(fallback_base)
    safe_ext = sanitize_filename(ext).replace(" ", "")
    if not safe_ext.startswith("."):
        safe_ext = f".{safe_ext}" if safe_ext else ".bin"
    # Reserve room for prefix + extension within path budget.
    room = max(1, max_len - len(prefix) - len(safe_ext))
    safe_stem = chosen[:room].rstrip(" .")
    if not safe_stem:
        safe_stem = "unnamed"
    return f"{prefix}{safe_stem}{safe_ext}"


def detect_extension(message: Any, fallback_name: str) -> str:
    if fallback_name:
        ext = _extension_from_filename(fallback_name)
        if ext:
            return ext

    media = getattr(message, "media", None)
    mime = getattr(getattr(media, "document", None), "mime_type", "") or ""
    if mime.startswith("video/"):
        return ".mp4"
    if mime.startswith("audio/"):
        return ".mp3"
    if mime.startswith("image/"):
        return ".jpg"
    if mime == "application/pdf":
        return ".pdf"
    if "wordprocessingml" in mime:
        return ".docx"
    if "spreadsheetml" in mime:
        return ".xlsx"
    if "presentationml" in mime:
        return ".pptx"
    if mime == "application/msword":
        return ".doc"
    if mime in {"application/zip", "application/x-zip-compressed"}:
        return ".zip"
    if mime in {"application/x-rar-compressed", "application/vnd.rar", "application/x-rar"}:
        return ".rar"
    if mime == "application/x-7z-compressed":
        return ".7z"
    if mime in {"application/gzip", "application/x-gzip"}:
        return ".gz"
    if mime == "application/x-tar":
        return ".tar"
    if mime == "application/x-bzip2":
        return ".bz2"
    if mime == "application/x-xz":
        return ".xz"
    if mime in {"application/zstd", "application/x-zstd"}:
        return ".zst"
    if mime.startswith("text/"):
        return ".txt"
    return ".bin"


# Compound archive extensions (check before single suffix).
ARCHIVE_COMPOUND_EXTENSIONS = (
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tbz",
    ".tar.xz",
    ".txz",
    ".tar.zst",
    ".tar.lz",
    ".tar.lzma",
    ".tar.7z",
)

# Extensions for course materials (PDF, Office, archives, notes).
ARCHIVE_EXTENSIONS = {
    ".zip",
    ".zipx",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".lz",
    ".lzma",
    ".cab",
    ".iso",
    ".img",
    ".arj",
    ".ace",
    ".apk",
    ".jar",
    ".war",
    ".deb",
    ".rpm",
    ".msi",
    ".dmg",
    ".pkg",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".txt",
    ".csv",
    ".md",
    ".epub",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
} | ARCHIVE_EXTENSIONS

DOCUMENT_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.",
    "application/zip",
    "application/x-zip",
    "application/x-rar",
    "application/vnd.rar",
    "application/x-7z",
    "application/gzip",
    "application/x-gzip",
    "application/x-tar",
    "application/x-bzip",
    "application/x-xz",
    "application/zstd",
    "application/x-compress",
    "application/x-lzip",
    "application/java-archive",
    "application/json",
    "application/rtf",
    "text/",
)

ALL_ALLOWED_EXTENSIONS = DOCUMENT_EXTENSIONS | set(ARCHIVE_COMPOUND_EXTENSIONS)


def _document_filename(message: Any) -> str:
    file_obj = getattr(message, "file", None)
    return (getattr(file_obj, "name", None) or "").lower()


def _extension_from_filename(filename: str) -> str:
    lower = filename.lower()
    for ext in ARCHIVE_COMPOUND_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    return Path(filename).suffix.lower()


def _is_sticker_or_animation_only(message: Any, mime: str) -> bool:
    if getattr(message, "sticker", False):
        return True
    if mime in {"application/x-tgsticker", "application/x-tgs-sticker"}:
        return True
    if getattr(message, "gif", False) and mime.startswith("video/"):
        return True
    return False


def is_supported_media(message: Any) -> bool:
    # Photos, audio, video, and documents (PDF, Office, archives, text). Skip stickers/webpages.
    media = getattr(message, "media", None)
    if media is None:
        return False
    if media.__class__.__name__ == "MessageMediaWebPage":
        return False
    if getattr(message, "photo", None) is not None:
        return True
    doc = getattr(media, "document", None)
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if _is_sticker_or_animation_only(message, mime):
        return False
    if mime.startswith("image/") or mime.startswith("audio/") or mime.startswith("video/"):
        return True
    if any(mime.startswith(prefix) for prefix in DOCUMENT_MIME_PREFIXES):
        return True
    filename = _document_filename(message)
    ext = _extension_from_filename(filename)
    if ext in ALL_ALLOWED_EXTENSIONS:
        return True
    # Generic binary uploads often use octet-stream but keep the real extension in the filename.
    if mime in {"application/octet-stream", "binary/octet-stream", ""} and ext in ALL_ALLOWED_EXTENSIONS:
        return True
    return False


def quick_header_valid(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as fh:
        sig = fh.read(16)
    known = [
        b"\xFF\xD8\xFF",  # jpg
        b"\x89PNG\r\n\x1a\n",  # png
        b"GIF87a",
        b"GIF89a",
        b"%PDF-",
        b"PK\x03\x04",  # zip/docx/apk
        b"ID3",  # mp3
        b"OggS",  # ogg
        b"\x00\x00\x00",  # may include mp4 ftyp box
    ]
    if any(sig.startswith(k) for k in known):
        return True
    # If unknown type, we still accept non-empty files instead of false failing.
    return True


class FloodWaitCoordinator:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._until = 0.0

    async def wait_if_needed(self) -> None:
        now = asyncio.get_running_loop().time()
        if now < self._until:
            await asyncio.sleep(self._until - now)

    async def set_wait(self, seconds: int) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            self._until = max(self._until, now + seconds)
            await asyncio.sleep(seconds)


@dataclass
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    output_dir: Path
    concurrency: int
    max_retries: int
    download_delay: float
    request_size_bytes: int
    manifest_flush_files: int
    manifest_flush_seconds: int
    progress_update_sec: float
    enable_multipart: bool
    part_size_bytes: int
    max_parallel_parts: int
    merge_buffer_bytes: int


@dataclass
class ProgressState:
    active: int = 0
    completed: int = 0
    failed: int = 0
    retries: int = 0
    # Global download speed tracking (bytes/s) from progress callbacks.
    bytes_seen: int = 0
    last_bytes_seen: int = 0
    last_speed_check_at: float = 0.0
    speed_bps: float = 0.0
    current_file: str = "-"
    current_pct: float = 0.0


def human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f}{units[i]}"


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def filename_for(self, msg_id: int) -> Optional[str]:
        rec = self.data.get(str(msg_id), {})
        name = rec.get("filename")
        if isinstance(name, str) and name.strip():
            return name
        return None

    def is_done(self, msg_id: int, expected_size: Optional[int], root: Path) -> bool:
        key = str(msg_id)
        rec = self.data.get(key)
        if not rec or rec.get("status") != "done":
            return False
        filename = rec.get("filename")
        if not filename:
            return False
        file_path = root / filename
        if not file_path.exists():
            return False
        if expected_size is not None and file_path.stat().st_size != expected_size:
            return False
        return quick_header_valid(file_path)

    def mark_done(self, msg_id: int, filename: str, size: Optional[int]) -> None:
        self.data[str(msg_id)] = {"status": "done", "filename": filename, "size": size}

    def reserve_filename(self, msg_id: int, filename: str) -> None:
        self.data.setdefault(str(msg_id), {})
        self.data[str(msg_id)]["filename"] = filename
        self.data[str(msg_id)]["status"] = "pending"


class TelegramBulkDownloader:
    def __init__(self, client: TelegramClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.flood = FloodWaitCoordinator()

    async def _download_single_stream(
        self,
        file_input: Any,
        temp_path: Path,
        expected_size: Optional[int],
        on_progress: Any,
    ) -> None:
        with temp_path.open("wb") as out_fh:
            current = 0
            async for chunk in self.client.iter_download(
                file_input,
                request_size=self.settings.request_size_bytes,
            ):
                if not chunk:
                    continue
                out_fh.write(chunk)
                current += len(chunk)
                on_progress(current, int(expected_size or 0))

    async def _download_multipart(
        self,
        file_input: Any,
        temp_path: Path,
        expected_size: int,
        on_progress: Any,
    ) -> None:
        part_size = self.settings.part_size_bytes
        part_count = max(1, (expected_size + part_size - 1) // part_size)
        parts_dir = temp_path.parent / f"{temp_path.name}.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(self.settings.max_parallel_parts)
        downloaded_total = 0
        total_lock = asyncio.Lock()

        async def part_worker(part_index: int) -> None:
            nonlocal downloaded_total
            async with sem:
                start = part_index * part_size
                end = min(expected_size, start + part_size)
                target_len = end - start
                part_path = parts_dir / f"part{part_index:05d}.tmp"

                existing = part_path.stat().st_size if part_path.exists() else 0
                if existing > target_len:
                    part_path.unlink(missing_ok=True)
                    existing = 0

                mode = "ab" if existing > 0 else "wb"
                offset = start + existing
                remaining = target_len - existing

                async with total_lock:
                    downloaded_total += existing
                    on_progress(downloaded_total, expected_size)

                if remaining <= 0:
                    return

                with part_path.open(mode) as part_fh:
                    async for chunk in self.client.iter_download(
                        file_input,
                        offset=offset,
                        request_size=self.settings.request_size_bytes,
                        limit=remaining,
                    ):
                        if not chunk:
                            continue
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        part_fh.write(chunk)
                        wrote = len(chunk)
                        remaining -= wrote
                        offset += wrote
                        async with total_lock:
                            downloaded_total += wrote
                            on_progress(downloaded_total, expected_size)
                        if remaining <= 0:
                            break

                if part_path.stat().st_size != target_len:
                    raise IOError(f"part size mismatch for {part_path.name}")

        await asyncio.gather(*(part_worker(i) for i in range(part_count)))

        with temp_path.open("wb") as out_fh:
            for i in range(part_count):
                part_path = parts_dir / f"part{i:05d}.tmp"
                with part_path.open("rb") as in_fh:
                    while True:
                        buf = in_fh.read(self.settings.merge_buffer_bytes)
                        if not buf:
                            break
                        out_fh.write(buf)

        shutil.rmtree(parts_dir, ignore_errors=True)

    def _cleanup_temp_artifacts(self, temp_path: Path) -> None:
        temp_path.unlink(missing_ok=True)
        parts_dir = temp_path.parent / f"{temp_path.name}.parts"
        if parts_dir.exists():
            shutil.rmtree(parts_dir, ignore_errors=True)

    def _normalize_existing_manifest_filenames(self, root: Path, manifest: Manifest) -> None:
        changed = False
        max_filename_len = max(40, 240 - len(str(root)) - 1)
        for msg_id, rec in manifest.data.items():
            if rec.get("status") != "done":
                continue
            old_name = rec.get("filename")
            if not isinstance(old_name, str) or not old_name:
                continue
            old_path = root / old_name
            stem = Path(old_name).stem
            ext = Path(old_name).suffix or ".bin"
            # Keep numbering prefix if present.
            m = re.match(r"^(\d{10})\s-\s(.*)$", stem)
            if m:
                idx = int(m.group(1))
                caption = m.group(2)
            else:
                idx = int(msg_id) if str(msg_id).isdigit() else 0
                caption = stem
            new_name = build_filename(
                index=idx,
                caption=caption,
                fallback_base=caption or "unnamed",
                ext=ext,
                max_len=max_filename_len,
            )
            needs_change = (new_name != old_name) or (len(str(old_path)) > 240)
            if not needs_change:
                continue
            new_path = root / new_name
            if old_path.exists() and old_path != new_path:
                if new_path.exists():
                    suffix = sanitize_filename(msg_id)
                    new_name = build_filename(
                        index=idx,
                        caption=f"{caption} {suffix}",
                        fallback_base=caption or "unnamed",
                        ext=ext,
                        max_len=max_filename_len,
                    )
                    new_path = root / new_name
                old_path.rename(new_path)
            rec["filename"] = new_name
            changed = True
        if changed:
            manifest.save()

    async def resolve_entity(self, channel: str) -> Any:
        # Supports public username, t.me links, and private invite links.
        ch = channel.strip()
        if "joinchat/" in ch or "t.me/+" in ch or ch.startswith("+"):
            invite_hash = ch.split("/")[-1].lstrip("+")
            try:
                await self.client(ImportChatInviteRequest(invite_hash))
            except RPCError:
                pass
            return await self.client.get_entity(ch)

        if ch.startswith("https://t.me/"):
            username = ch.rstrip("/").split("/")[-1]
            ch = username

        try:
            await self.client(JoinChannelRequest(ch))
        except RPCError:
            pass
        return await self.client.get_entity(ch)

    async def download_channel(self, channel: str) -> None:
        entity = await self.resolve_entity(channel)
        channel_name = sanitize_filename(getattr(entity, "title", str(channel)))
        root = self.settings.output_dir / channel_name
        root.mkdir(parents=True, exist_ok=True)

        manifest = Manifest(root / "manifest.json")
        self._normalize_existing_manifest_filenames(root, manifest)

        messages = []
        async for msg in self.client.iter_messages(entity, reverse=True):
            if msg and msg.media and is_supported_media(msg):
                messages.append(msg)

        total = len(messages)
        if total == 0:
            print("No media messages found.")
            return

        sem = asyncio.Semaphore(self.settings.concurrency)
        use_rich = Progress is not None
        state = ProgressState(last_speed_check_at=time.monotonic())
        pending_manifest_writes = 0
        last_manifest_save_at = time.monotonic()

        progress_tqdm = None
        progress_rich = None
        overall_task: Optional[TaskID] = None
        file_tasks: Dict[int, TaskID] = {}
        if use_rich:
            progress_rich = Progress(
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(bar_width=None),
                TaskProgressColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )
            overall_task = progress_rich.add_task("Files A:0 D:0 F:0 R:0", total=total, completed=0)
        else:
            progress_tqdm = tqdm(total=total, desc="Files", unit="file", dynamic_ncols=True)

        def render_status() -> None:
            if use_rich and progress_rich is not None and overall_task is not None:
                progress_rich.update(
                    overall_task,
                    description=f"Files A:{state.active} D:{state.completed} F:{state.failed} R:{state.retries}",
                    completed=state.completed + state.failed,
                )
                return
            if progress_tqdm is None:
                return
            progress_tqdm.set_postfix(
                active=state.active,
                done=state.completed,
                failed=state.failed,
                retries=state.retries,
                speed=f"{human_bytes(state.speed_bps)}/s",
                file=f"{state.current_pct:5.1f}%",
                name=state.current_file[:28],
            )

        def maybe_flush_manifest(force: bool = False) -> None:
            nonlocal pending_manifest_writes, last_manifest_save_at
            now = time.monotonic()
            timed_out = (now - last_manifest_save_at) >= self.settings.manifest_flush_seconds
            file_batch_full = pending_manifest_writes >= self.settings.manifest_flush_files
            if force or timed_out or file_batch_full:
                manifest.save()
                pending_manifest_writes = 0
                last_manifest_save_at = now

        async def worker(index: int, msg: Any) -> None:
            nonlocal pending_manifest_writes
            async with sem:
                state.active += 1
                render_status()
                await self.flood.wait_if_needed()
                expected_size = getattr(getattr(msg, "file", None), "size", None)
                base_name = getattr(getattr(msg, "file", None), "name", "") or "file"
                ext = detect_extension(msg, base_name)
                caption = getattr(msg, "message", "") or ""
                fallback_base = sanitize_filename(Path(base_name).stem or "file")
                max_filename_len = max(40, 240 - len(str(root)) - 1)
                existing_name = manifest.filename_for(msg.id)
                if existing_name and len(str(root / existing_name)) <= 240:
                    filename = existing_name
                else:
                    filename = build_filename(
                        index=index,
                        caption=caption,
                        fallback_base=fallback_base,
                        ext=ext,
                        max_len=max_filename_len,
                    )
                final_path = root / filename
                temp_path = root / f"{filename}.tmp"
                if use_rich and progress_rich is not None:
                    file_tasks[msg.id] = progress_rich.add_task(
                        f"{filename[:50]}",
                        total=float(expected_size) if expected_size else 0.0,
                        completed=0.0,
                    )

                if manifest.is_done(msg.id, expected_size, root):
                    state.active -= 1
                    state.completed += 1
                    render_status()
                    if use_rich and progress_rich is not None and msg.id in file_tasks:
                        progress_rich.remove_task(file_tasks[msg.id])
                        del file_tasks[msg.id]
                    if progress_tqdm is not None:
                        progress_tqdm.update(1)
                    return
                manifest.reserve_filename(msg.id, filename)

                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

                attempt = 0
                while True:
                    try:
                        await asyncio.sleep(self.settings.download_delay)

                        last_current = 0

                        def on_progress(current: int, total_bytes: int) -> None:
                            nonlocal last_current
                            delta = max(0, current - last_current)
                            last_current = current
                            state.bytes_seen += delta
                            state.current_file = filename
                            state.current_pct = (current / total_bytes * 100.0) if total_bytes else 0.0
                            now = time.monotonic()
                            dt = now - state.last_speed_check_at
                            if dt >= self.settings.progress_update_sec:
                                db = state.bytes_seen - state.last_bytes_seen
                                state.speed_bps = db / dt if dt > 0 else 0.0
                                state.last_speed_check_at = now
                                state.last_bytes_seen = state.bytes_seen
                                render_status()
                            if use_rich and progress_rich is not None and msg.id in file_tasks:
                                progress_rich.update(
                                    file_tasks[msg.id],
                                    completed=float(current),
                                    total=float(total_bytes) if total_bytes else 0.0,
                                    description=f"{filename[:50]} ({state.current_pct:5.1f}%)",
                                )

                        file_input = getattr(msg, "document", None) or getattr(msg, "photo", None)
                        if file_input is None:
                            raise IOError("unsupported media payload for iter_download")

                        can_multipart = (
                            self.settings.enable_multipart
                            and expected_size is not None
                            and expected_size >= (self.settings.part_size_bytes * 2)
                        )
                        if can_multipart:
                            await self._download_multipart(
                                file_input=file_input,
                                temp_path=temp_path,
                                expected_size=int(expected_size),
                                on_progress=on_progress,
                            )
                        else:
                            await self._download_single_stream(
                                file_input=file_input,
                                temp_path=temp_path,
                                expected_size=expected_size,
                                on_progress=on_progress,
                            )

                        if not temp_path.exists():
                            raise IOError("download did not produce a file")
                        if expected_size is not None and temp_path.stat().st_size != expected_size:
                            raise IOError("size mismatch after download")
                        if not quick_header_valid(temp_path):
                            raise IOError("header verification failed")

                        temp_path.replace(final_path)
                        manifest.mark_done(msg.id, filename, expected_size)
                        pending_manifest_writes += 1
                        maybe_flush_manifest()
                        state.active -= 1
                        state.completed += 1
                        state.current_file = filename
                        state.current_pct = 100.0
                        render_status()
                        if use_rich and progress_rich is not None and msg.id in file_tasks:
                            progress_rich.remove_task(file_tasks[msg.id])
                            del file_tasks[msg.id]
                        if progress_tqdm is not None:
                            progress_tqdm.update(1)
                        return
                    except FloodWaitError as e:
                        print(f"\nFloodWait: pausing {e.seconds}s globally")
                        await self.flood.set_wait(int(e.seconds))
                    except (RPCError, IOError, OSError, TypeError) as e:
                        attempt += 1
                        state.retries += 1
                        render_status()
                        self._cleanup_temp_artifacts(temp_path)
                        if isinstance(e, TypeError):
                            # Usually non-downloadable media shape from Telegram API.
                            attempt = self.settings.max_retries + 1
                        if attempt > self.settings.max_retries:
                            print(f"\nFailed msg {msg.id}: {e}")
                            state.active -= 1
                            state.failed += 1
                            render_status()
                            if use_rich and progress_rich is not None and msg.id in file_tasks:
                                progress_rich.remove_task(file_tasks[msg.id])
                                del file_tasks[msg.id]
                            if progress_tqdm is not None:
                                progress_tqdm.update(1)
                            return
                        backoff = min(60, 2 ** attempt)
                        print(f"\nRetry msg {msg.id} attempt {attempt}/{self.settings.max_retries} in {backoff}s")
                        await asyncio.sleep(backoff)

        if use_rich and progress_rich is not None:
            with progress_rich:
                await asyncio.gather(*(worker(i, m) for i, m in enumerate(messages, start=1)))
        else:
            await asyncio.gather(*(worker(i, m) for i, m in enumerate(messages, start=1)))
            if progress_tqdm is not None:
                progress_tqdm.close()
        maybe_flush_manifest(force=True)
        print(f"Completed channel: {channel_name}")


def load_settings() -> Settings:
    load_dotenv()
    api_id_raw = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    missing = []
    if not api_id_raw:
        missing.append("TG_API_ID")
    if not api_hash:
        missing.append("TG_API_HASH")
    if missing:
        missing_csv = ", ".join(missing)
        raise ConfigError(
            f"Missing required env var(s): {missing_csv}\n"
            "Create a .env file in project root (you can copy .env.example), then set:\n"
            "TG_API_ID=<your telegram api id>\n"
            "TG_API_HASH=<your telegram api hash>"
        )

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ConfigError("TG_API_ID must be an integer.") from exc

    session_name = os.getenv("TG_SESSION_NAME", "tg_downloader")
    output_dir = Path(os.getenv("TG_OUTPUT_DIR", "downloads"))
    max_global_downloads_raw = os.getenv("TG_MAX_GLOBAL_DOWNLOADS", "").strip()
    concurrency = int(max_global_downloads_raw or os.getenv("TG_CONCURRENCY", "1"))
    max_retries = int(os.getenv("TG_MAX_RETRIES", "5"))
    download_delay = float(os.getenv("TG_DOWNLOAD_DELAY", "0"))
    request_size_bytes = int(os.getenv("TG_REQUEST_SIZE_BYTES", str(2 * 1024 * 1024)))
    manifest_flush_files = int(os.getenv("TG_MANIFEST_FLUSH_FILES", "20"))
    manifest_flush_seconds = int(os.getenv("TG_MANIFEST_FLUSH_SECONDS", "30"))
    progress_update_sec = float(os.getenv("TG_PROGRESS_UPDATE_SEC", "1.5"))
    enable_multipart = os.getenv("TG_ENABLE_MULTIPART", "true").strip().lower() in {"1", "true", "yes", "on"}
    part_size_bytes = int(float(os.getenv("TG_PART_SIZE_MB", "8")) * 1024 * 1024)
    max_parallel_parts = int(os.getenv("TG_MAX_PARALLEL_PARTS", "4"))
    merge_buffer_bytes = int(float(os.getenv("TG_MERGE_BUFFER_MB", "16")) * 1024 * 1024)
    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        output_dir=output_dir,
        concurrency=max(1, concurrency),
        max_retries=max(0, max_retries),
        download_delay=max(0.0, download_delay),
        request_size_bytes=max(64 * 1024, request_size_bytes),
        manifest_flush_files=max(1, manifest_flush_files),
        manifest_flush_seconds=max(5, manifest_flush_seconds),
        progress_update_sec=max(0.5, progress_update_sec),
        enable_multipart=enable_multipart,
        part_size_bytes=max(1024 * 1024, part_size_bytes),
        max_parallel_parts=max(1, max_parallel_parts),
        merge_buffer_bytes=max(1024 * 1024, merge_buffer_bytes),
    )


async def run(channels: list[str]) -> None:
    settings = load_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        settings.session_name,
        settings.api_id,
        settings.api_hash,
        connection=ConnectionTcpAbridged,
    )

    await client.connect()
    if not await client.is_user_authorized():
        print("First run login required.")
        phone = input("Enter phone number (with country code): ").strip()
        await client.send_code_request(phone)
        code = input("Enter OTP code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except Exception:
            pwd = input("Two-step password: ").strip()
            await client.sign_in(password=pwd)

    downloader = TelegramBulkDownloader(client, settings)
    for ch in channels:
        await downloader.download_channel(ch)

    await client.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram bulk media downloader")
    parser.add_argument("channels", nargs="+", help="Channel username/link/invite")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(run(args.channels))
    except ConfigError as e:
        print(f"Configuration error:\n{e}")
