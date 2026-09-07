# 文件夹备份

在 macOS 上一次运行，按配置顺序将多个完整源目录分别保存为新的 ZIP 备份包，汇总结果后退出。Python 负责配置和流程，系统 `/usr/bin/zip` 负责整体归档，无需第三方 Python 包。

当前已交付单项归档、[Issue #3：多备份项顺序执行与失败汇总](https://github.com/PhycheL/backup-service/issues/3)、[Issue #4：按备份项保留最近的备份包](https://github.com/PhycheL/backup-service/issues/4) 和 [Issue #5：拒绝重复启动并自动释放运行锁](https://github.com/PhycheL/backup-service/issues/5)。各项新包发布成功后按自己的 `keep` 清理超额旧包，单项失败后继续处理后续项；同一台机器同一时间只允许一个实例运行。

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
    },
    {
      "name": "projects",
      "source": "/Users/backup/Projects",
      "destination": "/Volumes/Backup/archives",
      "keep": 5
    }
  ]
}
```

- `backups`：至少包含一个备份项的有序列表。严格按列表顺序执行，一项结束后才开始下一项，不同时压缩多项。
- `name`：在整份配置中唯一且稳定的备份项名称，可含中文、空格和连字符。长度为 1–100 个 UTF-8 字节，不能含 `/`、`\`、控制字符或首尾空白，也不能是 `.`、`..`。改变名称视为新的备份项归属，原名称的历史包不自动清理；Unicode 规范等价的写法视为同一名称。请保持名称稳定，并避免在不同配置中为无关备份复用同一名称及备份目录。
- `source`：源目录，必须存在。若配置路径通过符号链接指向目录，会备份实际目录，包内顶层名称采用解析后的源目录名称；源目录内部的符号链接仍保存链接本身。
- `destination`：备份目录，不存在时自动创建，多项可共用。不能等于任一已配置源目录或位于其内部；符号链接、大小写及 Unicode 规范化等路径别名按实际目录关系检查。首次备份前核对完整配置中的目录关系，包括执行顺序靠后或不存在的源目录。
- `keep`：每项必填的保留份数，至少为 1 的整数，不接受布尔值或字符串。新包发布并成功清理后，该项最多保留此数量，包含本次新包；`1` 表示仅保留本次新包。各项独立计数，示例中的 `10` 不是全局默认值。

运行一次：

```sh
python3 backup.py --config /绝对路径/config.json
```

`--config` 也可使用相对于当前工作目录的路径。终端会输出每项的开始、结果和成功备份包的完整位置，最后按配置顺序汇总全部项目。全部成功退出码为 `0`，任一项失败为 `1`。即时错误输出在标准错误流中，包含原因和备份项名称；最终汇总在标准输出中重列各项结果。

无法解析 JSON、字段结构不合法、名称重复或保留份数不合法，会在执行任何备份前报错退出。可归属到某项的目录关系、目录访问、压缩或发布问题，作为该项失败处理，其他合法项目仍按顺序执行。新备份生成失败时不删除该项任何历史包，即使旧包数量已经超过 `keep`。例如中间项生成失败时：

```text
[documents] 开始备份：/Users/backup/Documents
[documents] 备份成功：/Volumes/Backup/archives/backup-v1--documents--….zip
[missing] 开始备份：/Users/backup/Missing
[missing] 备份失败：源目录不可访问 /Users/backup/Missing：…
[projects] 开始备份：/Users/backup/Projects
[projects] 备份成功：/Volumes/Backup/archives/backup-v1--projects--….zip
汇总：存在失败
[documents] 备份成功：/Volumes/Backup/archives/backup-v1--documents--….zip
[missing] 备份失败：源目录不可访问 /Users/backup/Missing：…
[projects] 备份成功：/Volumes/Backup/archives/backup-v1--projects--….zip
```

上例省略了备份包的时间戳、随机标识、清理完成提示及系统错误详情，整次运行退出码为 `1`。共用备份目录时，包名中的 `name` 区分各项归属；成功项只清理自己的超额旧包，不占用或改变其他项的保留份数。

备份包名称格式为：

```text
backup-v1--<name>--<UTC时间戳 YYYYMMDDTHHMMSS.ffffffZ>--<32位随机标识>.zip
```

前缀标识本程序的命名版本，名称标识备份项归属，时间戳和随机标识区分每次运行。解析时从右侧识别时间戳和随机标识，名称本身允许包含 `--`。此完整命名格式用于识别受管包，请勿将无关文件命名为此格式。清理只处理当前名称、合法时间戳和 32 位小写十六进制随机标识的正式普通文件，不处理其他名称或版本的包、归属不明的 ZIP、目录、符号链接及临时产物，也不递归扫描备份目录。

只有本项完整新 ZIP 已成功发布，才按包名中的 UTC 时间戳从旧到新删除超额历史包；不依据文件修改时间。同一时间戳的历史包按完整文件名排序。本次新包始终保留，即使系统时间回拨使某个历史包的时间戳更晚，也不会为了满足份数删除本次新包。

如果扫描旧包或删除旧包失败，该项报告“新包已成功：<完整路径>；旧包清理失败：<原因>”，保留新包并继续后续项，最终汇总为“存在失败”、退出码为 `1`。这与“备份失败”表示的新包生成失败不同；只有旧包清理全部完成才报告该项“备份成功”。清理可能已经删除了部分最旧的包，剩余数量可能超额；根据错误检查备份目录权限或文件系统状态后重新运行，下一次新包发布成功会再次尝试清理。

压缩过程使用备份目录中的私有临时子目录。系统 zip 完整写入并成功关闭后，程序通过 macOS `renamex_np(RENAME_EXCL)` 原子发布正式 ZIP；已有同名文件不会被覆盖，也不依赖硬链接。读取、压缩或发布失败会记录该项失败，尽力清理本次临时产物，继续后续项，最终非零退出。如果卷不支持排他重命名，发布会明确失败。强制杀进程或断电可能留下 `.backup-service-*` 临时目录，它们不属于正式备份；确认没有程序运行后可人工移除。

## 重复启动与异常结束

同一台 macOS 已有本程序运行时，新实例立即向标准错误输出 `备份正在运行`，退出码为 `1`，不会排队、创建 ZIP 或清理旧包，也不会中断首个实例。即使使用不同配置、工作目录、用户或 `HOME` / `TMPDIR`，仍共享同一把运行锁。互斥从命令行参数和配置处理之前开始，覆盖全部备份项、旧包清理及最终汇总，直到进程结束。

正常完成、配置或备份错误退出、收到终止信号或被强制结束后，系统自动释放运行锁，随后可直接重新运行原命令。若只结束 Python 父进程时 `/usr/bin/zip` 仍存活，zip 会继续持有这把锁，此时重试仍提示 `备份正在运行`；待该压缩进程结束后即可重试。父进程已结束的这次压缩不会再发布正式备份包，也不会触发旧包清理；可能遗留的临时产物按上文处理，不妨碍再次启动。

锁使用固定本机文件 `/private/tmp/phychel-backup-service.lock` 上的非阻塞 `flock`。文件可以长期存在，文件存在本身不代表备份正在运行；无需、也不要删除或替换它来解锁，否则会破坏正在运行实例之间的互斥。该文件由程序以所有用户可读的权限自动创建。锁文件不可访问或不是普通文件时，程序明确报告 `无法取得运行锁` 并以 `1` 退出，不开始备份。本功能不提供跨机器互斥或等待队列。

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

测试通过真实 CLI、独立读取 ZIP 及系统 unzip 验证恢复，覆盖多项顺序执行、共用备份目录、独立保留份数、按备份时间淘汰、更名后保留原包、无关条目保持、历史包超额时生成失败不清理，以及清理失败保留新包并继续后续项。另覆盖全局配置拒绝、跨项目录关系、最终汇总、单项归档、连续运行、路径别名、各类符号链接、FIFO/Unix socket、权限错误、写入失败和正式发布失败。测试数据均放在隔离临时目录；CLI 仍使用真实的本机共享运行锁，运行验收时应保证没有实际备份或另一份验收套件同时运行。

并发验收通过配置 FIFO 的打开握手、汇总输出管道的背压、`ps` 确认的 `SIGSTOP` 状态和 `kqueue` 退出事件协调真实子进程，不依靠固定等待时长判定重叠。覆盖不同配置和环境的拒绝、无新包或旧包删除、正常及错误退出后重启、`SIGTERM` / `SIGKILL` 后恢复，以及父进程退出后存活 zip 的持锁与释放。工具不可用、发布失败和旧包删除失败场景使用 macOS `/usr/bin/sandbox-exec`；测试环境需允许创建 Unix socket、查询和控制测试子进程，以及启动该隔离子进程。root 用户会跳过依赖普通用户权限的测试。

开发时可选安装 mypy，执行 `mypy --strict backup.py tests`；它仅用于开发检查，不是运行依赖。本次验证使用 macOS 26.6.2、Python 3.14.6、系统 zip 3.0 / unzip 6.00，并使用系统 Python 3.9.6 验证了目录和链接恢复，以及新增的 4 项并发验收测试。

## 恢复范围

恢复范围为普通文件内容、目录结构和符号链接。遇到 FIFO、Unix socket 等不支持的特殊文件系统对象会报告失败。文件类型检查不跟随链接，实际归档统一交给系统 zip；环境中的 `ZIP`、`ZIPOPT` 不会影响归档范围。

不额外保存 Finder 标签、资源叉或完整文件系统元数据。不暂停源目录写入、不检测源文件变化，不承诺同一时间点的一致性。架构依据见 [ZIP 恢复范围](docs/adr/0001-zip-file-backups.md) 和 [系统工具整体归档](docs/adr/0002-system-zip-archiving.md)。
