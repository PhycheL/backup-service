#!/usr/bin/env python3
"""macOS 一次性文件夹备份；归档由系统 zip 完成。"""

import argparse
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid


class BackupError(Exception):
    """可向操作者直接报告的备份错误。"""


@dataclass(frozen=True)
class BackupItem:
    name: str
    source: Path
    destination: Path
    keep: int


def load_config(path: Path) -> BackupItem:
    try:
        with path.open(encoding="utf-8") as config:
            data = json.load(config)
    except (OSError, ValueError) as error:
        raise BackupError(f"无法读取 JSON 配置 {path}：{error}") from error
    if not isinstance(data, dict):
        raise BackupError("配置顶层必须是 JSON 对象")
    items = data.get("backups")
    if not isinstance(items, list):
        raise BackupError("backups 必须是备份项列表")
    if not items:
        raise BackupError("backups 必须包含一个备份项")
    if len(items) > 1:
        raise BackupError("本阶段尚未支持多个备份项；backups 只能包含一个备份项")
    item = items[0]
    if not isinstance(item, dict):
        raise BackupError("备份项必须是 JSON 对象")
    name = item.get("name")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or name in {".", ".."}
        or not name.isprintable()
        or any(character in name for character in "/\\")
        or len(name.encode("utf-8")) > 100
    ):
        raise BackupError("name 必须是 1–100 个 UTF-8 字节的名称，不能含路径分隔符、控制字符或首尾空白")
    keep = item.get("keep")
    if type(keep) is not int or keep < 1:
        raise BackupError(f"[{name}] keep（保留份数）必须是至少为 1 的整数，不能是布尔值")
    paths = []
    for field in ("source", "destination"):
        value = item.get(field)
        if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
            raise BackupError(f"[{name}] {field} 必须是有效的绝对目录路径")
        paths.append(Path(value))
    return BackupItem(name, paths[0], paths[1], keep)


def create_backup(name: str, source: Path, destination: Path) -> Path:
    try:
        source = source.resolve(strict=True)
        if not source.is_dir():
            raise BackupError(f"源目录不是目录：{source}")
    except (OSError, RuntimeError) as error:
        raise BackupError(f"源目录不可访问 {source}：{error}") from error
    try:
        destination = destination.resolve()
    except (OSError, RuntimeError) as error:
        raise BackupError(f"备份目录路径无效 {destination}：{error}") from error
    validate_destination(source, destination)
    validate_source_contents(source)
    destination.mkdir(parents=True, exist_ok=True)
    validate_destination(source, destination)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    archive = destination / f"backup-v1--{name}--{timestamp}--{uuid.uuid4().hex}.zip"
    environment = os.environ.copy()
    environment.pop("ZIP", None)
    environment.pop("ZIPOPT", None)
    with tempfile.TemporaryDirectory(prefix=".backup-service-", dir=destination) as work:
        temporary_archive = Path(work) / "archive.zip"
        result = subprocess.run(
            ["/usr/bin/zip", "-q", "-r", "-y", "-MM", str(temporary_archive), "./" + source.name],
            cwd=source.parent,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()
            raise BackupError(f"系统 zip 压缩失败（退出码 {result.returncode}）：{detail}")
        publish_archive(temporary_archive, archive)
    return archive


def publish_archive(temporary_archive: Path, archive: Path) -> None:
    # macOS renamex_np(RENAME_EXCL) 原子移动完整文件；目标存在时返回 EEXIST。
    # 使用系统调用而非先检查再 rename，避免竞争时覆盖，也不要求卷支持硬链接。
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename_exclusive = library.renamex_np
    rename_exclusive.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename_exclusive.restype = ctypes.c_int
    rename_excl = 0x00000004  # macOS SDK: sys/stdio.h 的 RENAME_EXCL
    if rename_exclusive(os.fsencode(temporary_archive), os.fsencode(archive), rename_excl) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, f"无法发布备份包：{os.strerror(error_number)}", str(archive))


def validate_destination(source: Path, destination: Path) -> None:
    # samefile 同时识别符号链接、大小写及 Unicode 规范化产生的目录别名。
    for ancestor in (destination, *destination.parents):
        try:
            if source.samefile(ancestor):
                raise BackupError(f"备份目录不能等于源目录或位于其内部：{destination}")
        except FileNotFoundError:
            continue


def validate_source_contents(source: Path) -> None:
    # zip 会静默跳过某些特殊对象且返回 0；这里只检查类型和目录可访问性。
    # 归档遍历和符号链接的保存仍全部由系统 zip -r -y 完成。
    directories = [source]
    while directories:
        directory = directories.pop()
        if not os.access(directory, os.R_OK | os.X_OK):
            raise BackupError(f"源目录不可读取或遍历：{directory}")
        with os.scandir(directory) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    directories.append(Path(entry.path))
                elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise BackupError(f"不支持的文件系统对象：{entry.path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="将单个源目录备份为新的 ZIP 备份包。")
    parser.add_argument("--config", required=True, type=Path, help="JSON 配置文件路径")
    args = parser.parse_args()
    label = "配置"
    try:
        item = load_config(args.config)
        label = item.name
        print(f"[{item.name}] 开始备份：{item.source}", flush=True)
        if sys.platform != "darwin":
            raise BackupError("运行环境需要 macOS 和 Python 3")
        if not Path("/usr/bin/zip").is_file() or not os.access("/usr/bin/zip", os.X_OK):
            raise BackupError("系统 zip 不可用：需要可执行的 macOS /usr/bin/zip")
        archive = create_backup(item.name, item.source, item.destination)
    except (BackupError, OSError, ValueError) as error:
        print(f"[{label}] 备份失败：{error}", file=sys.stderr)
        return 1
    print(f"[{item.name}] 备份成功：{archive}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
