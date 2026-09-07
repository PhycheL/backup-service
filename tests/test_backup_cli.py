"""通过真实命令行和 macOS 解压工具验收备份行为。"""

from contextlib import closing
import errno
import json
import os
from pathlib import Path
import resource
import re
import select
import socket
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from typing import Callable, Optional, TextIO


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
        self.config.write_text(json.dumps(config), encoding="utf-8")

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

    def create_excess_history(self) -> dict[Path, bytes]:
        self.write_config({"backups": [{**self.item, "keep": 10}]})
        for _ in range(3):
            result = self.run_backup()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write_config({"backups": [self.item]})
        return {path: path.read_bytes() for path in self.destination.iterdir()}

    def start_backup(self, config: Path, *, stderr: Optional[TextIO] = None) -> subprocess.Popen[str]:
        environment = os.environ.copy()
        environment.update(HOME=str(self.root), TMPDIR=str(self.source))
        process = subprocess.Popen(
            [sys.executable, str(ENTRY_POINT), "--config", str(config)],
            cwd=self.source,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if stderr is None else stderr,
            text=True,
            errors="replace",
            start_new_session=True,
        )

        def stop_processes() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=10)

        self.addCleanup(stop_processes)
        return process

    def open_config_writer(self, process: subprocess.Popen[str], fifo: Path) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), "首个实例在读取配置前退出")
            try:
                # 写端成功打开表明真实 CLI 已打开读端，随后保持无 EOF 状态。
                return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                os.sched_yield()
        self.fail("首个实例未打开配置管道")

    def test_overlapping_configurations_are_rejected_without_backup_or_cleanup(self) -> None:
        previous = self.create_excess_history()
        original_config = self.config.read_text(encoding="utf-8")
        fifo = self.root / "first-config.fifo"
        os.mkfifo(fifo)
        first = self.start_backup(fifo)
        with os.fdopen(self.open_config_writer(first, fifo), "w") as writer:
            second = self.run_backup()
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertIn("备份正在运行", second.stderr)
            self.assertNotIn("开始备份", second.stdout)
            self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, previous)
            self.assertIsNone(first.poll())
            unrelated_destination = self.root / "unrelated-backups"
            self.write_config({"backups": [{**self.item, "destination": str(unrelated_destination)}]})
            second = self.run_backup()
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertIn("备份正在运行", second.stderr)
            self.assertFalse(unrelated_destination.exists())
            self.config.write_text("{broken", encoding="utf-8")
            second = self.run_backup()
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertIn("备份正在运行", second.stderr)
            self.assertNotIn("配置", second.stderr)
            self.config.write_text(original_config, encoding="utf-8")
            writer.write(original_config)

        stdout, stderr = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        self.assertIn("汇总：全部成功", stdout)
        self.assertEqual(len(list(self.destination.iterdir())), 1)
        again = self.run_backup()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_configuration_error_and_termination_allow_restart_without_manual_cleanup(self) -> None:
        for termination in [None, signal.SIGTERM, signal.SIGKILL]:
            with self.subTest(termination=termination):
                fifo = self.root / f"config-{termination}.fifo"
                os.mkfifo(fifo)
                first = self.start_backup(fifo)
                with os.fdopen(self.open_config_writer(first, fifo), "w") as writer:
                    second = self.run_backup()
                    self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
                    self.assertIn("备份正在运行", second.stderr)
                    if termination is None:
                        writer.write("{broken")
                    else:
                        first.send_signal(termination)
                stdout, stderr = first.communicate(timeout=10)
                if termination is None:
                    self.assertEqual(first.returncode, 1, stdout + stderr)
                    self.assertIn("配置", stderr)
                else:
                    self.assertEqual(first.returncode, -termination)
                again = self.run_backup()
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_exclusion_covers_summary_after_all_items_and_cleanup(self) -> None:
        previous = self.create_excess_history()
        other_source = self.root / "other-source"
        other_source.mkdir()
        (other_source / "content.txt").write_text("other content", encoding="utf-8")
        config = self.root / "summary-config.json"
        config.write_text(json.dumps({"backups": [
            self.item,
            {**self.item, "name": "other", "source": str(other_source)},
            # 超长无效路径的错误结果使汇总超过管道容量，未读完时进程不能退出。
            {**self.item, "name": "invalid", "source": "/" + "x" * (256 * 1024)},
        ]}), encoding="utf-8")
        with (self.root / "errors.txt").open("w") as errors:
            first = self.start_backup(config, stderr=errors)
            assert first.stdout is not None
            deadline = time.monotonic() + 20
            output = b""
            while "\n汇总：".encode() not in output:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "未观察到最终汇总")
                self.assertTrue(select.select([first.stdout], [], [], remaining)[0], "未观察到最终汇总")
                chunk = os.read(first.stdout.fileno(), 65536)
                self.assertTrue(chunk, "首个实例在汇总前退出")
                output += chunk
            first.send_signal(signal.SIGSTOP)
            _, status = os.waitpid(first.pid, os.WUNTRACED)
            self.assertTrue(os.WIFSTOPPED(status))
            archives = {path: path.read_bytes() for path in self.destination.iterdir()}
            self.assertEqual(len(archives), 2)
            self.assertFalse(previous.keys() & archives.keys())
            second = self.run_backup()
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertIn("备份正在运行", second.stderr)
            self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, archives)
            first.send_signal(signal.SIGCONT)
            first.communicate(timeout=20)
            self.assertEqual(first.returncode, 1)
        again = self.run_backup()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def stop_zip_child(self, process: subprocess.Popen[str]) -> int:
        deadline = time.monotonic() + 10
        child = None
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), "首个实例在暂停 zip 前退出")
            listing = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,stat=,comm="],
                capture_output=True, text=True, check=True, timeout=5,
            )
            for line in listing.stdout.splitlines():
                fields = line.split(None, 3)
                if len(fields) != 4:
                    continue
                pid, parent, status, command = fields
                if int(parent) != process.pid or command != "/usr/bin/zip":
                    continue
                if child is None:
                    child = int(pid)
                    os.kill(child, signal.SIGSTOP)
                elif int(pid) == child and "T" in status:
                    return child
        self.fail("未观察到已暂停的真实 zip 子进程")

    def test_orphaned_zip_holds_exclusion_until_it_exits(self) -> None:
        large_source = self.root / "large-source"
        large_source.mkdir()
        # 留出外部进程控制的机会；正确性取决于 ps 确认的停止状态，而非等待时长。
        with (large_source / "large.bin").open("wb") as payload:
            payload.truncate(256 * 1024 * 1024)
        config = self.root / "large-config.json"
        config.write_text(json.dumps({"backups": [{
            **self.item, "source": str(large_source),
            "destination": str(self.root / "first-backups"),
        }]}), encoding="utf-8")
        for termination, child_signal in [(signal.SIGKILL, signal.SIGCONT), (signal.SIGTERM, signal.SIGKILL)]:
            with self.subTest(termination=termination, child_signal=child_signal):
                previous = {path: path.read_bytes() for path in self.destination.glob("*")}
                first = self.start_backup(config)
                child = self.stop_zip_child(first)
                with closing(select.kqueue()) as events:
                    events.control([select.kevent(
                        child, filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )], 0, 0)
                    second = self.run_backup()
                    self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
                    self.assertIn("备份正在运行", second.stderr)
                    first.send_signal(termination)
                    self.assertEqual(first.wait(timeout=10), -termination)

                    second = self.run_backup()
                    self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
                    self.assertIn("备份正在运行", second.stderr)
                    self.assertEqual({path: path.read_bytes() for path in self.destination.glob("*")}, previous)

                    os.kill(child, child_signal)
                    self.assertTrue(events.control(None, 1, 20), "zip 未结束")
                again = self.run_backup()
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

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

    def multiple_items(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for index, name in enumerate(["z-first", "middle", "a-last"]):
            source = self.root / name
            source.mkdir()
            (source / "content.txt").write_text(name, encoding="utf-8")
            items.append({**self.item, "name": name, "source": str(source), "keep": index + 1})
        return items

    def test_multiple_backups_run_in_order_and_keep_independent_recent_archives(self) -> None:
        items = self.multiple_items()
        self.write_config({"backups": items})
        previous: dict[Path, bytes] = {}
        histories: dict[str, list[Path]] = {str(item["name"]): [] for item in items}
        for _ in range(4):
            result = self.run_backup()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            events = [line for line in result.stdout.split("汇总", 1)[0].splitlines() if "开始备份" in line or "备份成功" in line]
            self.assertEqual(len(events), 6, result.stdout)
            for index, name in enumerate(["z-first", "middle", "a-last"]):
                self.assertIn(f"[{name}] 开始备份", events[index * 2])
                self.assertIn(f"[{name}] 备份成功", events[index * 2 + 1])
            summary = result.stdout.split("汇总", 1)[1]
            self.assertIn("全部成功", summary)
            for name in ["z-first", "middle", "a-last"]:
                self.assertIn(f"[{name}]", summary)
            new_paths = set(self.destination.iterdir()) - previous.keys()
            self.assertEqual(len(new_paths), 3)
            for name in ["z-first", "middle", "a-last"]:
                archive = next(path for path in new_paths if f"--{name}--" in path.name)
                self.assertIn(str(archive), summary)
                with zipfile.ZipFile(archive) as backup:
                    self.assertEqual(backup.namelist(), [f"{name}/", f"{name}/content.txt"])
                    self.assertEqual(backup.read(f"{name}/content.txt"), name.encode())
                histories[name].append(archive)
            expected = set(histories["z-first"][-1:] + histories["middle"][-2:] + histories["a-last"][-3:])
            self.assertEqual(set(self.destination.iterdir()), expected)
            for path in expected & previous.keys():
                self.assertEqual(path.read_bytes(), previous[path])
            previous = {path: path.read_bytes() for path in self.destination.iterdir()}

    def test_invalid_configuration_fails_before_creating_a_backup(self) -> None:
        cases: list[tuple[object, str]] = [
            ([], "JSON 对象"),
            ({}, "backups"),
            ({"backups": {}}, "backups"),
            ({"backups": []}, "一个备份项"),
            ({"backups": [self.item, self.item]}, "名称重复"),
            ({"backups": [{**self.item, "name": "café"}, {**self.item, "name": "cafe\u0301"}]}, "名称重复"),
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

    def test_retention_uses_archive_time_and_preserves_unrelated_entries_after_rename(self) -> None:
        self.write_config({"backups": [{**self.item, "keep": 10}]})
        history: list[Path] = []
        for index in range(3):
            (self.source / "version.txt").write_text(f"version {index}")
            self.assertEqual(self.run_backup().returncode, 0)
            archive = (set(self.destination.iterdir()) - set(history)).pop()
            history.append(archive)
        # 文件修改时间与创建顺序相反，保留仍应依据包名中的备份时间。
        for index, archive in enumerate(history):
            os.utime(archive, (1000 - index, 1000 - index))
        self.write_config({"backups": [{**self.item, "keep": 2}]})
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(history[0].exists())
        self.assertFalse(history[1].exists())
        self.assertTrue(history[2].exists())
        self.assertEqual(len(list(self.destination.iterdir())), 2)
        old_archives = {path: path.read_bytes() for path in self.destination.iterdir()}

        name = "中文--documents[1]"
        prefix = f"backup-v1--{name}--"
        suffix = "--" + "a" * 32 + ".zip"
        unrelated_names = [
            "notes.txt", "unknown.zip", "backup-v2--" + name + "--20200101T000000.000000Z" + suffix,
            prefix + "20201301T000000.000000Z" + suffix,
            prefix + "20200101T000000.000000Z--unknown.zip",
            prefix + "20200101T000000.000000Z" + suffix + ".partial",
            prefix + "20200101T000000.000000Z" + suffix + "\n",
            "backup-v1--" + name + "-other--20200101T000000.000000Z" + suffix,
        ]
        for filename in unrelated_names:
            (self.destination / filename).write_bytes(b"unrelated content")
        directory = self.destination / (prefix + "20200102T000000.000000Z" + suffix)
        directory.mkdir()
        (directory / "keep.txt").write_bytes(b"directory content")
        temporary = self.destination / ".backup-service-unfinished"
        temporary.mkdir()
        (temporary / "archive.zip").write_bytes(b"unfinished")
        link = self.destination / (prefix + "20200103T000000.000000Z" + suffix)
        link.symlink_to(history[2])
        dangling = self.destination / (prefix + "20200104T000000.000000Z" + suffix)
        dangling.symlink_to("missing")
        protected = set(self.destination.iterdir())
        self.write_config({"backups": [{**self.item, "name": name}]})
        first_new = None
        for _ in range(2):
            result = self.run_backup()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            new_paths = set(self.destination.iterdir()) - protected
            self.assertEqual(len(new_paths), 1)
            if first_new is not None:
                self.assertFalse(first_new.exists())
            first_new = new_paths.pop()
            self.assertEqual(set(self.destination.iterdir()), protected | {first_new})
            for path, content in old_archives.items():
                self.assertEqual(path.read_bytes(), content)
            for filename in unrelated_names:
                self.assertEqual((self.destination / filename).read_bytes(), b"unrelated content")
            self.assertEqual((directory / "keep.txt").read_bytes(), b"directory content")
            self.assertEqual((temporary / "archive.zip").read_bytes(), b"unfinished")
            self.assertEqual(os.readlink(link), str(history[2]))
            self.assertEqual(os.readlink(dangling), "missing")

    def test_cleanup_failure_keeps_new_archive_and_continues_later_items(self) -> None:
        items = self.multiple_items()
        self.write_config({"backups": [{**item, "keep": 10} for item in items]})
        history = []
        for _ in range(3):
            self.assertEqual(self.run_backup().returncode, 0)
            history.append(next(path for path in self.destination.glob("*--middle--*.zip") if path not in history))
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        self.write_config({"backups": [{**item, "keep": 1} for item in items]})
        # 允许压缩与发布，只禁止删除第二旧的 middle 包，观察淘汰顺序。
        profile = f'(version 1)(allow default)(deny file-write-unlink (literal {json.dumps(str(history[1]), ensure_ascii=False)}))'

        result = self.run_backup(prefix=("/usr/bin/sandbox-exec", "-p", profile))

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[middle] 新包已成功", result.stderr)
        self.assertIn("旧包清理失败", result.stderr)
        self.assertIn(str(history[1]), result.stderr)
        self.assertNotIn("[middle] 备份失败", result.stdout + result.stderr)
        self.assertNotIn("[middle] 备份成功", result.stdout)
        summary = result.stdout.split("汇总", 1)[1]
        self.assertIn("存在失败", summary)
        self.assertIn("[middle] 新包已成功", summary)
        self.assertIn("旧包清理失败", summary)
        self.assertIn("[z-first] 备份成功", summary)
        self.assertIn("[a-last] 备份成功", summary)
        self.assertFalse(history[0].exists())
        for path in history[1:]:
            self.assertEqual(path.read_bytes(), previous[path])
        new_paths = set(self.destination.iterdir()) - previous.keys()
        self.assertEqual(len(new_paths), 3)
        for name in ["z-first", "middle", "a-last"]:
            archive = next(path for path in new_paths if f"--{name}--" in path.name)
            self.assertIn(str(archive), summary)
            with zipfile.ZipFile(archive) as backup:
                self.assertEqual(backup.read(f"{name}/content.txt"), name.encode())
        self.assertEqual(set(self.destination.iterdir()), new_paths | set(history[1:]))
        # 故障解除后再次运行即可完成保留处理。
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list(self.destination.iterdir())), 3)

    def test_current_archive_is_kept_even_when_history_has_a_future_timestamp(self) -> None:
        previous = self.create_excess_history()
        oldest = sorted(previous)[0]
        prefix, _, suffix = oldest.name.rsplit("--", 2)
        future = oldest.with_name(prefix + "--29990101T000000.000000Z--" + suffix)
        oldest.rename(future)
        future_content = future.read_bytes()
        self.write_config({"backups": [{**self.item, "keep": 2}]})

        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(future.read_bytes(), future_content)
        current = (set(self.destination.iterdir()) - {future}).pop()
        self.assertNotIn(current, previous)
        self.assertIn(str(current), result.stdout)
        self.assertEqual(set(self.destination.iterdir()), {future, current})
        self.write_config({"backups": [self.item]})
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(future.exists())
        self.assertFalse(current.exists())
        self.assertEqual(len(list(self.destination.iterdir())), 1)

    def test_invalid_later_item_prevents_every_backup(self) -> None:
        for invalid, reason in [
            (self.item, "名称重复"),
            ("wrong", "备份项"),
            ({**self.item, "name": "second", "keep": True}, "keep"),
            ({**self.item, "name": "second", "source": "relative"}, "source"),
            ({**self.item, "name": "second", "destination": None}, "destination"),
            ({**self.item, "name": "second", "source": "/tmp/\ud800"}, "source"),
        ]:
            with self.subTest(invalid=invalid):
                self.write_config({"backups": [self.item, invalid]})
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)
                self.assertNotIn("开始备份", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_cross_item_destinations_are_rejected_before_any_archive_is_written(self) -> None:
        items = self.multiple_items()
        other_source = self.root / "middle"
        alias = self.root / "middle-alias"
        alias.symlink_to(other_source, target_is_directory=True)
        cases = [other_source, other_source / "new/backups", alias, alias / "new/backups"]
        for index, destination in enumerate(cases):
            with self.subTest(destination=destination):
                items[0]["destination"] = str(destination)
                safe_destination = self.root / f"safe-{index}"
                items[1]["destination"] = str(safe_destination)
                self.write_config({"backups": items})
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[z-first]", result.stderr)
                self.assertIn("备份目录不能等于源目录或位于其内部", result.stderr)
                self.assertIn("middle", result.stderr)
                self.assertEqual(list(other_source.iterdir()), [other_source / "content.txt"])
                archive = next(safe_destination.glob("*.zip"))
                with zipfile.ZipFile(archive) as backup:
                    self.assertEqual(set(backup.namelist()), {"middle/", "middle/content.txt"})
                summary = result.stdout.split("汇总", 1)[1]
                self.assertIn("[z-first] 备份失败", summary)
                self.assertIn("[middle] 备份成功", summary)
                self.assertIn("[a-last] 备份成功", summary)

    def assert_middle_failure_preserves_history_and_continues(
        self, result: subprocess.CompletedProcess[str], previous: dict[Path, bytes]
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[middle] 备份失败", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        summary = result.stdout.split("汇总", 1)[1]
        self.assertIn("[z-first] 备份成功", summary)
        self.assertIn("[middle] 备份失败", summary)
        self.assertIn("[a-last] 备份成功", summary)
        for path, content in previous.items():
            if "--middle--" in path.name:
                self.assertEqual(path.read_bytes(), content)
        new_paths = set(self.destination.iterdir()) - previous.keys()
        self.assertEqual(len(new_paths), 2, new_paths)
        for name in ["z-first", "a-last"]:
            archive = next(path for path in new_paths if f"--{name}--" in path.name)
            with zipfile.ZipFile(archive) as backup:
                self.assertEqual(backup.read(f"{name}/content.txt"), name.encode())

    def test_middle_directory_errors_do_not_stop_later_items(self) -> None:
        items = self.multiple_items()
        self.write_config({"backups": items})
        self.assertEqual(self.run_backup().returncode, 0)
        plain_file = self.root / "plain-file"
        plain_file.write_bytes(b"untouched")
        loop = self.root / "loop"
        loop.symlink_to(loop)
        for field, path in [
            ("source", self.root / "missing"),
            ("source", plain_file),
            ("destination", plain_file),
            ("source", loop),
            ("destination", loop),
        ]:
            with self.subTest(field=field, path=path):
                previous = {path: path.read_bytes() for path in self.destination.iterdir()}
                self.write_config({"backups": [items[0], {**items[1], field: str(path)}, items[2]]})
                result = self.run_backup()
                self.assert_middle_failure_preserves_history_and_continues(result, previous)
                self.assertIn(str(path), result.stderr)
                self.assertEqual(plain_file.read_bytes(), b"untouched")

    @unittest.skipIf(os.geteuid() == 0, "权限失败场景需要非 root 用户")
    def test_middle_permission_errors_do_not_stop_later_items(self) -> None:
        items = self.multiple_items()
        parent = self.root / "private-parent"
        parent.mkdir()
        source = parent / "middle"
        (self.root / "middle").rename(source)
        items[1]["source"] = str(source)
        destination = self.root / "private-destination"
        destination.mkdir()
        self.write_config({"backups": items})
        self.assertEqual(self.run_backup().returncode, 0)
        for inaccessible in [parent, source, source / "content.txt", destination]:
            with self.subTest(inaccessible=inaccessible):
                middle = items[1].copy()
                if inaccessible == destination:
                    middle["destination"] = str(destination)
                self.write_config({"backups": [items[0], middle, items[2]]})
                previous = {path: path.read_bytes() for path in self.destination.iterdir()}
                mode = inaccessible.stat().st_mode
                try:
                    inaccessible.chmod(0)
                    result = self.run_backup()
                finally:
                    inaccessible.chmod(mode)
                self.assert_middle_failure_preserves_history_and_continues(result, previous)
                if inaccessible == source / "content.txt":
                    self.assertIn("zip 压缩失败", result.stderr)

    def test_middle_archive_write_failure_does_not_stop_later_items(self) -> None:
        items = self.multiple_items()
        self.write_config({"backups": items})
        self.assertEqual(self.run_backup().returncode, 0)
        previous = {path: path.read_bytes() for path in self.destination.iterdir()}
        (self.root / "middle/content.txt").write_bytes(os.urandom(128 * 1024))

        def limit_file_size() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))

        result = self.run_backup(preexec_fn=limit_file_size)

        self.assert_middle_failure_preserves_history_and_continues(result, previous)
        self.assertIn("zip 压缩失败", result.stderr)

    def test_missing_source_still_prevents_other_items_writing_inside_it(self) -> None:
        items = self.multiple_items()
        missing = self.root / "missing"
        alias = self.root / "missing-alias"
        alias.symlink_to(missing, target_is_directory=True)
        items[0]["destination"] = str(alias / "backups")
        items[1]["source"] = str(missing)
        self.write_config({"backups": items})

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())
        summary = result.stdout.split("汇总", 1)[1]
        self.assertIn("[z-first] 备份失败", summary)
        self.assertIn("[middle] 备份失败", summary)
        self.assertIn("[a-last] 备份成功", summary)

    def test_missing_source_is_protected_through_case_and_unicode_aliases(self) -> None:
        items = self.multiple_items()
        parent = self.root / "Parent-é"
        parent.mkdir()
        aliases = [self.root / "parent-é", self.root / "Parent-e\u0301"]
        if not all(alias.exists() for alias in aliases):
            self.skipTest("临时目录所在卷需要支持大小写和 Unicode 规范化别名")
        for index, (source, destination) in enumerate([
            (parent / "missing", aliases[0] / "missing/archives"),
            (parent / "missing", aliases[1] / "missing/archives"),
            (parent / "Missing", parent / "missing/archives"),
            (parent / "é", parent / "e\u0301/archives"),
        ]):
            with self.subTest(source=source, destination=destination):
                safe_destination = self.root / f"safe-alias-{index}"
                self.write_config({"backups": [
                    {**items[0], "destination": str(destination)},
                    {**items[1], "source": str(source)},
                    {**items[2], "destination": str(safe_destination)},
                ]})
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(list(parent.iterdir()), [])
                summary = result.stdout.split("汇总", 1)[1]
                self.assertIn("[z-first] 备份失败", summary)
                self.assertIn("[middle] 备份失败", summary)
                self.assertIn("[a-last] 备份成功", summary)
                self.assertEqual(len(list(safe_destination.glob("*.zip"))), 1)

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
            self.write_config({"backups": [{**self.item, "name": name, "keep": 10}]})
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
        previous = self.create_excess_history()
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
        previous = self.create_excess_history()
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
        previous = self.create_excess_history()
        # 允许写临时产物，但禁止创建正式名称，模拟发布时权限或文件系统错误。
        pattern = "^" + re.escape(str(self.destination)) + "/backup-v1--"
        profile = f'(version 1)(allow default)(deny file-write* (regex {json.dumps(pattern, ensure_ascii=False)}))'

        result = self.run_backup(prefix=("/usr/bin/sandbox-exec", "-p", profile))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("成功", result.stdout)
        self.assertEqual({path: path.read_bytes() for path in self.destination.iterdir()}, previous)


if __name__ == "__main__":
    unittest.main()
