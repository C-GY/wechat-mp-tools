自媒体内容采集工具：消失/删除排查

1. 将 ZIP 完整解压到任意文件夹。
2. 双击 collect_install_diagnostics.cmd；无需安装 Python。
3. 等待报告自动打开。桌面会生成 Collector-Diagnostics-时间-编号.zip。
4. 把这个 ZIP 或 report.txt 发给排查人员，并注明事发时间和安装包版本。

如安装在自定义目录，可以将安装文件夹拖到 CMD 文件上。
若报告中 Defender 事件显示拒绝访问，可在相同 Windows 账户下，右键 CMD
选择“以管理员身份运行”；未取得权限也会保留其余可读取的报告。

仅读取证：不修改杀毒设置、注册表或代理，不恢复/删除软件，不联网上传。
只读取指定软件日志片段，不收集配置、Cookie、业务数据库或私钥文件。
报告会对常见凭据、用户目录名和用户 SID 做脱敏，发送前仍请自行检查。
事件查询限最近 21 天，单类最多 3000 条（重启事件最多 100 条），等待上限
30 秒；日志只取最多 128 KiB 的片段。未找到事件不代表没有发生删除。
Defender 检测记录不能单独证明是误报，不会自动给程序添加排除项。

高级用法（Windows PowerShell）：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\collect_install_diagnostics.ps1 -Days 30 -InstallDir "D:\你的安装目录" -OutputDir "D:\报告"

ExecutionPolicy Bypass 仅对本次 PowerShell 进程生效，不永久修改执行策略。
