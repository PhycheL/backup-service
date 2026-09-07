"""通过真实命令行和 macOS 解压工具验收备份行为。"""

import json
import os
from pathlib import Path
import resource
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from typing import Callable, Optional


ENTRY_POINT = Path(__file__).resolve().parents[1] / "backup.py"


@unittest.skipUnless(sys.platform == "darwin", "验收平台为 macOS")
class BackupCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="backup-cli-test-")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name).resolve()
        self.source = self.root / "-资料 源"
        self.source.mkdir()
        self.destination = self.root / "备份 目录" / "archives"
        self.config = self.root / "config.json"
        self.item = {
            "name": "documents",
            "source": str(self.source),
            "destination": str(self.destination),
            "keep": 1,
        }
        self.write_config({"backups": [self.item]})

    def write_config(self, config: object) -> None:
        self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def run_backup(
        self,
        *,
        environment: Optional[dict[str, str]] = None,
        preexec_fn: Optional[Callable[[], None]] = None,
        prefix: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*prefix, sys.executable, str(ENTRY_POINT), "--config", str(self.config)],
            cwd=self.root,
            env=environment,
            preexec_fn=preexec_fn,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def restore(self, archive: Path, name: str = "restored") -> Path:
        destination = self.root / name
        destination.mkdir()
        environment = os.environ.copy()
        environment.pop("UNZIP", None)
        environment.pop("UNZIPOPT", None)
        result = subprocess.run(
            ["/usr/bin/unzip", "-q", "-^", str(archive), "-d", str(destination)],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return destination / self.source.name

    def test_complete_directory_can_be_restored(self) -> None:
        (self.source / "多层" / "子目录").mkdir(parents=True)
        (self.source / "空目录").mkdir()
        (self.source / ".隐藏目录").mkdir()
        files = {
            "中文 空格.txt": "备份内容\n".encode(),
            "多层/子目录/binary.bin": b"\x00\xff\x10\x80\x00",
            "empty.txt": b"",
            ".hidden": b"hidden file",
            ".隐藏目录/inside.txt": b"nested hidden file",
        }
        for name, content in files.items():
            (self.source / name).write_bytes(content)

        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("开始", result.stdout)
        self.assertIn("成功", result.stdout)
        archives = list(self.destination.glob("*.zip"))
        self.assertEqual(len(archives), 1)
        self.assertIn(str(archives[0]), result.stdout)
        restored = self.restore(archives[0])
        for name, content in files.items():
            self.assertEqual((restored / name).read_bytes(), content)
        self.assertTrue((restored / "空目录").is_dir())
        self.assertEqual(
            {str(path.relative_to(restored)) for path in restored.rglob("*")},
            set(files) | {"多层", "多层/子目录", "空目录", ".隐藏目录"},
        )

    def test_invalid_configuration_fails_before_creating_a_backup(self) -> None:
        cases: list[tuple[object, str]] = [
            ([], "JSON 对象"),
            ({}, "backups"),
            ({"backups": {}}, "backups"),
            ({"backups": []}, "一个备份项"),
            ({"backups": [self.item, self.item]}, "尚未支持多个备份项"),
            ({"backups": ["wrong"]}, "备份项"),
        ]
        for field in self.item:
            item = self.item.copy()
            del item[field]
            cases.append(({"backups": [item]}, field))
        for keep in [0, -1, 1.5, True, False, "2", None]:
            cases.append(({"backups": [{**self.item, "keep": keep}]}, "keep"))
        for name in ["", " ", ".", "..", "../escape", "a/b", "a\\b", "x\n", "a" * 101, 42]:
            cases.append(({"backups": [{**self.item, "name": name}]}, "name"))
        for field in ["source", "destination"]:
            for path in ["relative/path", "", 7, None, "/tmp/\x00"]:
                cases.append(({"backups": [{**self.item, field: path}]}, field))
        for config, reason in cases:
            with self.subTest(config=config):
                self.write_config(config)
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_links_are_restored_without_traversing_their_targets(self) -> None:
        (self.source / "file.txt").write_bytes(b"inside")
        (self.source / "folder").mkdir()
        (self.source / "folder" / "child").write_bytes(b"child")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "external-secret").write_bytes(b"must not enter archive")
        links = {
            "relative": "./file.txt",
            "absolute": str(outside / "external-secret"),
            "directory": "folder",
            "external-directory": str(outside),
            "断开 链接": "missing/中文 target",
            "cycle": ".",
            "self-loop": "self-loop",
            "folder/parent": "..",
        }
        for name, target in links.items():
            (self.source / name).symlink_to(target)

        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.restore(next(self.destination.glob("*.zip")))
        self.assertEqual((restored / "file.txt").read_bytes(), b"inside")
        self.assertEqual((restored / "folder/child").read_bytes(), b"child")
        for name, target in links.items():
            self.assertTrue((restored / name).is_symlink(), name)
            self.assertEqual(os.readlink(restored / name), target)
        self.assertEqual(
            {str(path.relative_to(restored)) for path in restored.rglob("*")},
            set(links) | {"file.txt", "folder", "folder/child"},
        )

    def test_rapid_runs_keep_every_existing_backup_and_identify_the_item(self) -> None:
        self.destination.mkdir(parents=True)
        (self.destination / "unrelated.txt").write_bytes(b"keep unrelated file")
        (self.destination / "other.zip").write_bytes(b"not our archive")
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        for index, name in enumerate(["documents", "documents", "中文 名称--documents"]):
            self.write_config({"backups": [{**self.item, "name": name}]})
            content = f"version {index}".encode()
            (self.source / "version.txt").write_bytes(content)

            result = self.run_backup()

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for path, old_content in previous.items():
                self.assertEqual(path.read_bytes(), old_content)
            new_paths = set(self.destination.iterdir()) - previous.keys()
            self.assertEqual(len(new_paths), 1)
            archive = new_paths.pop()
            self.assertRegex(
                archive.name,
                rf"^backup-v1--{name}--\d{{8}}T\d{{6}}\.\d{{6}}Z--[0-9a-f]{{32}}\.zip$",
            )
            restored = self.restore(archive, f"restored-{index}")
            self.assertEqual((restored / "version.txt").read_bytes(), content)
            previous[archive] = archive.read_bytes()

    def test_destination_inside_source_is_rejected_including_aliases(self) -> None:
        source_alias = self.root / "source-alias"
        source_alias.symlink_to(self.source, target_is_directory=True)
        backup_alias = self.root / "backup-alias"
        backup_alias.symlink_to(self.source, target_is_directory=True)
        cases = [
            (self.source, self.source),
            (self.source, self.source / "new" / "backups"),
            (source_alias, self.source / "alias-backups"),
            (self.source, backup_alias),
            (self.source, backup_alias / "new" / "backups"),
            (self.source, self.source / "unused" / ".." / "backups"),
        ]
        for source, destination in cases:
            with self.subTest(source=source, destination=destination):
                self.write_config({"backups": [{**self.item, "source": str(source), "destination": str(destination)}]})
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("备份目录不能等于源目录或位于其内部", result.stderr)
                self.assertEqual(list(self.source.iterdir()), [])

    def test_case_alias_of_source_cannot_contain_the_destination(self) -> None:
        source = self.root / "Case Source"
        source.mkdir()
        alias = self.root / "case source"
        if not alias.exists():
            self.skipTest("临时目录所在文件系统区分大小写")
        self.write_config({"backups": [{**self.item, "source": str(source), "destination": str(alias / "backups")}]})

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("备份目录不能等于源目录或位于其内部", result.stderr)
        self.assertEqual(list(source.iterdir()), [])

    def test_special_objects_fail_without_losing_existing_backups(self) -> None:
        (self.source / "file.txt").write_bytes(b"original")
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        nested = self.source / "nested"
        nested.mkdir()
        for kind in ["fifo", "socket"]:
            with self.subTest(kind=kind):
                special = nested / kind
                connection = socket.socket(socket.AF_UNIX)
                try:
                    if kind == "fifo":
                        os.mkfifo(special)
                    else:
                        # macOS 的 Unix socket 路径最多约 104 字节，用相对路径绑定。
                        working_directory = Path.cwd()
                        try:
                            os.chdir(nested)
                            connection.bind(kind)
                        finally:
                            os.chdir(working_directory)
                    result = self.run_backup()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("不支持的文件系统对象", result.stderr)
                    self.assertIn(kind, result.stderr)
                    self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, previous)
                finally:
                    connection.close()
                    special.unlink(missing_ok=True)

    def test_unreadable_source_content_preserves_existing_backups(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("权限失败场景需要非 root 用户")
        file = self.source / "private.txt"
        file.write_bytes(b"previous content")
        child = self.source / "private-directory"
        child.mkdir()
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        for inaccessible in [self.source, child, file]:
            with self.subTest(path=inaccessible):
                original_mode = inaccessible.stat().st_mode
                try:
                    inaccessible.chmod(0)
                    result = self.run_backup()
                finally:
                    inaccessible.chmod(original_mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("documents", result.stderr)
                self.assertIn("失败", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, previous)

    def test_write_failure_removes_partial_output_and_preserves_existing_backups(self) -> None:
        file = self.source / "data.bin"
        file.write_bytes(b"original")
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        file.write_bytes(os.urandom(128 * 1024))

        def limit_file_size() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))

        result = self.run_backup(preexec_fn=limit_file_size)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("zip 压缩失败", result.stderr)
        self.assertNotIn("成功", result.stdout)
        self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, previous)

    def test_missing_system_zip_reports_the_runtime_requirement(self) -> None:
        profile = '(version 1)(allow default)(deny file-read* process-exec (literal "/usr/bin/zip"))'

        result = self.run_backup(prefix=("/usr/bin/sandbox-exec", "-p", profile))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("系统 zip 不可用", result.stderr)
        self.assertIn("/usr/bin/zip", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_invalid_json_or_missing_config_has_a_clear_error(self) -> None:
        for content in [b"{broken", b"\xff", None]:
            with self.subTest(content=content):
                if content is None:
                    self.config.unlink()
                else:
                    self.config.write_bytes(content)
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("无法读取 JSON 配置", result.stderr)
                self.assertIn(str(self.config), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_missing_source_and_non_directory_paths_fail(self) -> None:
        file = self.root / "plain-file"
        file.write_bytes(b"untouched")
        for field, path in [("source", self.root / "missing"), ("source", file), ("destination", file)]:
            with self.subTest(field=field, path=path):
                self.write_config({"backups": [{**self.item, field: str(path)}]})
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("失败", result.stderr)
                self.assertIn(str(path), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(file.read_bytes(), b"untouched")
                self.assertFalse(self.destination.exists())

    def test_unwritable_destination_preserves_its_contents(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("权限失败场景需要非 root 用户")
        self.destination.mkdir(parents=True)
        existing = self.destination / "existing.zip"
        existing.write_bytes(b"untouched")
        try:
            self.destination.chmod(0o500)
            result = self.run_backup()
        finally:
            self.destination.chmod(0o700)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败", result.stderr)
        self.assertEqual(list(self.destination.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"untouched")

    def test_zip_environment_cannot_exclude_source_content(self) -> None:
        (self.source / "must-keep.txt").write_bytes(b"complete backup")
        environment = {**os.environ, "ZIP": "-j", "ZIPOPT": "-x *.txt"}

        result = self.run_backup(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.restore(next(self.destination.glob("*.zip")))
        self.assertEqual((restored / "must-keep.txt").read_bytes(), b"complete backup")

    def test_empty_source_directory_is_preserved(self) -> None:
        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.restore(next(self.destination.glob("*.zip")))
        self.assertTrue(restored.is_dir())
        self.assertEqual(list(restored.iterdir()), [])

    def test_control_characters_in_filenames_are_preserved_on_restore(self) -> None:
        (self.source / "newline\n.txt").write_bytes(b"with newline")
        (self.source / "newline.txt").write_bytes(b"without newline")

        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.restore(next(self.destination.glob("*.zip")))
        self.assertEqual((restored / "newline\n.txt").read_bytes(), b"with newline")
        self.assertEqual((restored / "newline.txt").read_bytes(), b"without newline")

    def test_publication_failure_preserves_old_archives_and_cleans_temporary_output(self) -> None:
        (self.source / "file.txt").write_bytes(b"original")
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        # 允许写临时产物，但禁止创建正式名称，模拟发布时权限或文件系统错误。
        pattern = "^" + re.escape(str(self.destination)) + "/backup-v1--"
        profile = f'(version 1)(allow default)(deny file-write* (regex {json.dumps(pattern, ensure_ascii=False)}))'

        result = self.run_backup(prefix=("/usr/bin/sandbox-exec", "-p", profile))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("成功", result.stdout)
        self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, previous)


if __name__ == "__main__":
    unittest.main()
