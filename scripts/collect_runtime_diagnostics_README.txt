自媒体内容采集工具：一键收集运行异常 / Out of Memory 诊断

使用方法
1. 将工具 ZIP 全部解压，CMD 和 PS1 保持在同一文件夹，不要直接在压缩包内运行。
2. 最好保留“此页存在问题 / Out of Memory”的错误窗口，先不要刷新、关闭或重启。
3. 双击 collect_runtime_diagnostics.cmd，无需安装 Python，通常不需要管理员权限。
4. 等待完成，默认在桌面生成 Collector-Runtime-日期时间-随机编号.zip 和同名文件夹。
   完成后会打开资源管理器并选中 ZIP；桌面不可写时使用当前用户 TEMP 文件夹。
5. 将该诊断 ZIP 发回；不要把整个软件 data 文件夹发回。
6. 同时告诉排查人员：出错时间、正在操作的页面/任务、是否连续两周没退出、
   近期是否有更新、采集时是否仍报错，以及重启后能否恢复。

如果找不到安装目录
程序运行时会优先从进程识别安装目录，也会检查安装记录和默认安装位置。
找不到时会弹出文件夹选择窗口，请选包含 WeChat MP Tools.exe 的文件夹。
也可以把该安装文件夹拖到 collect_runtime_diagnostics.cmd 上再运行。
若提示 Collected logs: 0，仍应保留该包，再指定正确安装目录补采一次。
程序已退出也能采集，但无法还原退出前的内存现场。应注明采集发生在重启之后。

刷新后恢复、需要捕获下次故障时：持续记录模式
1. 软件恢复正常后，双击 watch_runtime_memory.cmd，提前开始记录。
2. 保留这个命令窗口，可以最小化；关闭它就会停止记录，不会注册开机启动或后台服务。
3. 默认约每 15 秒采样一次，记录软件/子进程内存、系统内存，以及网页进程编号/启动时间。
4. 再次报错时，先不要刷新，回到记录窗口按回车，工具将停止记录并自动打包日志和内存历史。
   若已经刷新，仍请按回车结束并发回，之前已保存的记录仍在。
5. 按回车后的日志打包仍需要一些时间，看到 Send this ZIP 再关闭窗口。
持续记录文件每个约 8 MiB，保留当前和 3 份旧文件（总计约 32 MiB）；最早记录会滚动替换，
另存一份开始时的基线和最后采样。不会为记录占用无限磁盘空间或在内存中累积全部历史。
若直接关闭窗口、断电或采集失败，已写入的数据仍留在窗口显示的桌面报告文件夹；
这时可能没有 ZIP，可手动压缩该文件夹发回。记录器只在手动开启期间运行。
采样不保证捕获两次采样之间的瞬时内存峰值；不含 JavaScript 堆快照，不能单凭曲线确定代码根因。
网页进程更换也可能来自刷新或导航，进程编号变化不等于崩溃。

收集内容
- 软件及其 WebView2/工作子进程的内存、启动时间、进程关系、句柄和线程数；三次采样。
- Windows 物理内存、虚拟内存、已提交内存/上限、分页文件和磁盘剩余空间。
- 软件和正在运行的相关可执行文件版本、PE 架构；其他高内存进程只保留名称与用量。
- 最近 7 天的软件/WebView2 崩溃事件及系统内存耗尽事件。
- 以下日志的尾部，单文件最多读取 2 MiB，合计读取预算 32 MiB：
  软件安装目录\wechat_mp_tools.log
  软件安装目录\data\frontend_errors.log
  软件安装目录\data\proxy_runtime.log（含 .1 至 .3）
  软件安装目录\data\logs\channels_environment.jsonl（含 .1 至 .3）
  软件安装目录\data\parse_debug.log
  软件安装目录\data\channels_call_log.jsonl
这些日志有些只在特定功能使用或出现异常时产生，缺少某项不一定是异常。
wechat_mp_tools.log 主要记录启动异常；frontend_errors.log 是视频号注入页面日志，
两者都不保证记录主窗口此次崩溃。此时系统事件和内存现场尤其重要。

隐私与边界
只读本机证据并创建报告，不联网、不上传、不访问数据库、不启动或重启软件，
不清缓存、不更改代理、注册表或分页文件。不复制登录配置、Cookie、浏览器 Profile、
数据库/业务数据、媒体、私钥或内存转储，不保存进程完整命令行。
自动遮盖常见密码、Token、Cookie、URL 参数和用户目录名；无法保证识别所有敏感文本，
发送前请检查解压后的报告。事件可能受权限/保留策略影响，性能计数器也可能不可用。
存在超大单行时可能省略该行；完整文件大小、截断与读取失败会记录在 log-inventory.json。
诊断 ZIP 生成成功仅表示证据收集完成，不表示已经找到或修复原因。
ExecutionPolicy Bypass 仅作用于本次 PowerShell 进程，不永久修改系统执行策略。

高级用法（Windows PowerShell 5.1 或更高版本）
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\collect_runtime_diagnostics.ps1 -InstallDir "D:\软件安装目录" -Days 14 -OutputDir "D:\诊断输出"
-NonInteractive 可关闭文件夹选择和完成后的资源管理器窗口，适合命令行运行。
持续记录 30 分钟后自动打包：在上面命令后增加 -Monitor -MonitorMinutes 30。
-SampleIntervalSeconds 可调整记录间隔（最低 5 秒）；默认不指定 -MonitorMinutes 时，按回车结束。
使用 -Monitor -NonInteractive 时必须指定正数 -MonitorMinutes，避免无人交互时无限运行。
