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
import unicodedata
import uuid


class BackupError(Exception):
    """可向操作者直接报告的备份错误。"""


@dataclass(frozen=True)
class BackupItem:
    name: str
    source: Path
    destination: Path
    keep: int


def load_config(path: Path) -> list[BackupItem]:
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
    backups = []
    names: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise BackupError(f"第 {index} 个备份项必须是 JSON 对象")
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
            raise BackupError(f"第 {index} 个备份项的 name 必须是 1–100 个 UTF-8 字节的名称，不能含路径分隔符、控制字符或首尾空白")
        if name in names:
            raise BackupError(f"备份项名称重复：{name}")
        names.add(name)
        keep = item.get("keep")
        if type(keep) is not int or keep < 1:
            raise BackupError(f"[{name}] keep（保留份数）必须是至少为 1 的整数，不能是布尔值")
        paths = []
        for field in ("source", "destination"):
            value = item.get(field)
            if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
                raise BackupError(f"[{name}] {field} 必须是有效的绝对目录路径")
            try:
                os.fsencode(value)
            except UnicodeError as error:
                raise BackupError(f"[{name}] {field} 含有无法编码的路径字符") from error
            paths.append(Path(value))
        backups.append(BackupItem(name, paths[0], paths[1], keep))
    return backups


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


def directory_identity(path: Path) -> tuple[int, int, tuple[str, ...]]:
    """用已存在祖先的身份和未创建的后缀识别目录，包括路径别名。"""
    suffix = []
    ancestor = path
    while True:
        try:
            info = ancestor.stat()
            break
        except FileNotFoundError:
            if ancestor == ancestor.parent:
                raise
            suffix.append(ancestor.name)
            ancestor = ancestor.parent
    if suffix:
        # macOS SDK sys/unistd.h: _PC_CASE_SENSITIVE = 11。
        # Python 未公开该常量名称，pathconf 仍接受对应的整数。
        case_sensitive = os.pathconf(ancestor, 11)
        suffix = [unicodedata.normalize("NFD", part) for part in reversed(suffix)]
        if case_sensitive != 1:
            suffix = [part.casefold() for part in suffix]
    return info.st_dev, info.st_ino, tuple(suffix)


def validate_destination(source: Path, destination: Path) -> None:
    message = f"备份目录不能等于源目录或位于其内部：{destination}（源目录：{source}）"
    # 未创建的源目录仍参与关系核对，不能被另一项的输出目录意外创建。
    if destination == source or source in destination.parents:
        raise BackupError(message)
    try:
        source_identity = directory_identity(source)
    except OSError:
        # 源目录访问错误由其所属备份项报告，不让它阻断无关项目。
        return
    # 比较真实目录身份，未创建目录的后缀也按所在卷的大小写规则比较。
    for ancestor in (destination, *destination.parents):
        if source_identity == directory_identity(ancestor):
            raise BackupError(message)


def preflight_directories(items: list[BackupItem]) -> dict[str, str]:
    errors: dict[str, str] = {}
    sources = []
    for item in items:
        try:
            source = item.source.resolve()
        except (OSError, RuntimeError) as error:
            errors[item.name] = f"源目录不可访问 {item.source}：{error}"
            source = item.source
        sources.append((item.name, source))

    for item in items:
        if item.name in errors:
            continue
        try:
            destination = item.destination.resolve()
            for source_name, source in sources:
                try:
                    validate_destination(source, destination)
                except BackupError as error:
                    raise BackupError(f"与备份项 [{source_name}] 的目录关系无效：{error}") from error
        except (BackupError, OSError, RuntimeError) as error:
            errors[item.name] = f"备份目录检查失败 {item.destination}：{error}"
    return errors


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
    parser = argparse.ArgumentParser(description="按配置顺序将多个源目录分别备份为新的 ZIP 备份包。")
    parser.add_argument("--config", required=True, type=Path, help="JSON 配置文件路径")
    args = parser.parse_args()
    try:
        items = load_config(args.config)
    except (BackupError, OSError, ValueError) as error:
        print(f"[配置] 备份失败：{error}", file=sys.stderr)
        return 1

    directory_errors = preflight_directories(items)
    results = []
    failures = 0
    for item in items:
        print(f"[{item.name}] 开始备份：{item.source}", flush=True)
        try:
            if item.name in directory_errors:
                raise BackupError(directory_errors[item.name])
            if sys.platform != "darwin":
                raise BackupError("运行环境需要 macOS 和 Python 3")
            if not Path("/usr/bin/zip").is_file() or not os.access("/usr/bin/zip", os.X_OK):
                raise BackupError("系统 zip 不可用：需要可执行的 macOS /usr/bin/zip")
            archive = create_backup(item.name, item.source, item.destination)
        except (BackupError, OSError, ValueError) as error:
            result = f"[{item.name}] 备份失败：{error}"
            print(result, file=sys.stderr, flush=True)
            failures += 1
        else:
            result = f"[{item.name}] 备份成功：{archive}"
            print(result, flush=True)
        results.append(result)

    print("汇总：" + ("存在失败" if failures else "全部成功"), flush=True)
    for result in results:
        print(result, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
