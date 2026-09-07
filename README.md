# 文件夹备份

在 macOS 上一次运行，将一个完整源目录保存为新的 ZIP 备份包，然后退出。Python 负责配置和流程，系统 `/usr/bin/zip` 负责整体归档，无需第三方 Python 包。

当前交付 [Issue #2](https://github.com/PhycheL/backup-service/issues/2)：只接受一个备份项；`keep` 会校验，但尚不清理历史备份。多项执行、历史保留和单实例互斥由后续任务实现。

## 运行前提

- 目标机器为 macOS 10.12 或更新版本，安装 Python 3.9 或更新版本。部署到另一台机器时仍需单独确认运行环境。
- `/usr/bin/zip` 可执行，支持 `-r`（递归）、`-y`（保存链接本身）和 `-MM`（匹配或读取错误时失败）。macOS 自带的 Info-ZIP 3.0 已验证。
- 源目录及子目录可读取和遍历，普通文件可读取；备份目录可写，空间足够容纳一个新备份包。
- 恢复时使用支持符号链接的 `/usr/bin/unzip`；macOS 自带的 UnZip 6.00 已验证。

在目标机器检查：

```sh
python3 --version
/usr/bin/zip -v
/usr/bin/unzip -v
```

程序启动时也会检查平台和 `/usr/bin/zip` 的可用性。

## 配置和运行

在项目目录复制示例：

```sh
cp config.example.json config.json
```

编辑 `config.json`，将路径改为目标 macOS 上的真实绝对目录路径：

```json
{
  "backups": [
    {
      "name": "documents",
      "source": "/Users/backup/Documents",
      "destination": "/Volumes/Backup/archives",
      "keep": 10
    }
  ]
}
```

- `backups`：必须是仅有一个备份项的列表；多项配置会直接报错，不会执行第一项后报告全部成功。
- `name`：稳定的备份项名称，可含中文、空格和连字符。长度为 1–100 个 UTF-8 字节，不能含 `/`、`\`、控制字符或首尾空白，也不能是 `.`、`..`。改变名称视为新的备份项归属。
- `source`：源目录，必须存在。若配置路径通过符号链接指向目录，会备份实际目录，包内顶层名称采用解析后的源目录名称；源目录内部的符号链接仍保存链接本身。
- `destination`：备份目录，不存在时自动创建。不能等于源目录或位于其内部；符号链接、大小写等路径别名按实际目录关系检查。
- `keep`：至少为 1 的整数，不接受布尔值或字符串。本阶段即使超过此值也保留所有历史包。

运行一次：

```sh
python3 backup.py --config /绝对路径/config.json
```

`--config` 也可使用相对于当前工作目录的路径。终端会输出备份项的开始、结果和成功备份包的完整位置；成功退出码为 `0`，失败为非零。错误输出在标准错误流中，包含原因和已识别的备份项名称。

备份包名称格式为：

```text
backup-v1--<name>--<UTC时间戳 YYYYMMDDTHHMMSS.ffffffZ>--<32位随机标识>.zip
```

前缀标识本程序的命名版本，名称标识备份项归属，时间戳和随机标识区分每次运行。解析时从右侧识别时间戳和随机标识，名称本身允许包含 `--`。本版本不会删除或覆盖已有备份包及其他文件。

压缩过程使用备份目录中的私有临时子目录。系统 zip 完整写入并成功关闭后，程序通过 macOS `renamex_np(RENAME_EXCL)` 原子发布正式 ZIP；已有同名文件不会被覆盖，也不依赖硬链接。读取、压缩或发布失败会非零退出，尽力清理本次临时产物。如果卷不支持排他重命名，发布会明确失败。强制杀进程或断电可能留下 `.backup-service-*` 临时目录，它们不属于正式备份；确认没有程序运行后可人工移除。

## 在 macOS 上恢复

用终端报告的真实备份包位置替换下方 `archive`。解压到新的空目录，保留包中的源目录这一层：

```sh
archive='/Volumes/Backup/archives/实际备份包名称.zip'
restore="$(mktemp -d /tmp/backup-restore.XXXXXX)"
env -u UNZIP -u UNZIPOPT /usr/bin/unzip -q '-^' "$archive" -d "$restore"
printf '恢复目录：%s\n' "$restore"
```

该命令已通过真实 macOS 解压验证：普通文件、二进制内容、空文件、多层目录、空目录、隐藏内容、中文和空格名称均可恢复；`-^` 保留文件名中的换行等控制字符，避免默认过滤造成名称冲突。相对、绝对、目录、断开及循环符号链接恢复为链接，保留原始目标字符串。绝对链接仍指向原有绝对路径，断开链接可能依旧断开，程序不会将链接目标的外部内容额外打包。

检查恢复结果时，应读取文件并与预期内容比较，检查目录结构，同时用 `test -L` 检查链接类型、`readlink` 检查原始目标字符串；仅看到同名条目不足以确认恢复正确。例如，实际源目录名为 `Documents` 且包含 `notes.txt`、`empty-directory` 和目标为 `./notes.txt` 的 `notes-link` 时：

```sh
cmp /Users/backup/Documents/notes.txt "$restore/Documents/notes.txt"
test -d "$restore/Documents/empty-directory"
test -L "$restore/Documents/notes-link"
test "$(readlink "$restore/Documents/notes-link")" = './notes.txt'
```

`cmp` 示例只适用于原文件自备份后未变化的情况。下面的自动测试使用固定预期内容验证恢复，无需接触真实备份。

## 验证

在 macOS 上以普通用户运行完整验收套件：

```sh
python3 -m unittest discover -s tests -v
```

测试通过真实 CLI 和独立的系统 unzip 验证恢复，覆盖配置错误、连续运行、路径别名、各类符号链接、FIFO/Unix socket、权限错误、写入失败和正式发布失败。全部文件均放在隔离临时目录。工具不可用和发布失败场景使用 macOS `/usr/bin/sandbox-exec`；测试环境需允许创建 Unix socket 和启动该隔离子进程。root 用户会跳过两个依赖普通用户权限的测试。

开发时可选安装 mypy，执行 `mypy --strict backup.py tests`；它仅用于开发检查，不是运行依赖。本次验证使用 macOS 26.6.2、Python 3.14.6、系统 zip 3.0 / unzip 6.00，并使用系统 Python 3.9.6 验证了目录和链接恢复。

## 恢复范围

恢复范围为普通文件内容、目录结构和符号链接。遇到 FIFO、Unix socket 等不支持的特殊文件系统对象会报告失败。文件类型检查不跟随链接，实际归档统一交给系统 zip；环境中的 `ZIP`、`ZIPOPT` 不会影响归档范围。

不额外保存 Finder 标签、资源叉或完整文件系统元数据。不暂停源目录写入、不检测源文件变化，不承诺同一时间点的一致性。架构依据见 [ZIP 恢复范围](docs/adr/0001-zip-file-backups.md) 和 [系统工具整体归档](docs/adr/0002-system-zip-archiving.md)。
