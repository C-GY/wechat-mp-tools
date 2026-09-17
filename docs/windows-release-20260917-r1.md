# Windows Full 2026.09.17.1

本版本修复视频号长时间运行后，代理监听失效但仍显示“运行中”，自动任务持续等待且无法自行恢复的问题。心跳超时后检查真实本机 HTTP 响应，必要时重建代理，等待已经打开的视频号页面重新连接，再继续当前批次。恢复尝试限频并串行执行，健康代理不会被无故重启。

本次没有更改视频号注入脚本。更新时退出采集程序即可，微信可以保持运行；已加载的采集页面能够继续使用原有心跳协议。程序仍需要真实页面心跳，登录失效或微信页面本身异常不能仅靠代理恢复解决。

安装包：`dist/installer/自媒体内容采集工具_Windows_x64_Full_Setup_20260917_r1.exe`。安装规则预检与实际编译均已成功，文件版本为 `2026.9.17.1`；SHA256 为 `67d685a1a3c16b6469bf0e2feaadb30bbba77a1017cb79ddd5affd8e925b8e0d`，已与同目录 `.exe.sha256` 文件复核。本次未替用户安装或替换正在运行的版本。

## 验证记录

- 83 项代理、视频号环境、任务恢复与相关界面测试通过。
- 38 项构建、证书及代理状态界面测试通过。
- 在实际冻结 EXE 内启动真实代理，关闭其监听端口但保留后台线程，验证程序自动重建并收到模拟现有页面的 HTTP 心跳；测试中未打开、刷新或重启微信。
- 冻结 EXE 测试使用独立目录及 APPDATA，并替换系统代理、系统证书修改边界，没有执行真实采集任务。
- 校验 18 个冻结业务模块与当前源码一致，核对前端/注入资源及内置 Chromium、SQLite、WebView2 引导程序，确认发布目录未混入用户数据。
- 故障注入验证已通过，尚未进行连续多天的实机运行验证。

构建目录为 `dist/windows-20260917-full-r1/WeChat MP Tools/`。安装器统一通过 `scripts/build_windows_installer.py` 和 `installer/windows_full_setup.iss` 构建，默认路径为 `%LOCALAPPDATA%\Programs\SelfMediaContentCollector`，AppId 保持 `{E956D31A-06A0-49BB-BA0B-3AE4DF127F6A}`。

验证材料位于 `scratch/windows-20260917-r1-package-verification.json`、`scratch/packaged-recovery-20260917-r1/result.json` 和 `scratch/windows-20260917-r1-installer-verification.json`；构建日志位于 `build/windows-20260917-r1-build.log` 和 `build/windows-20260917-r1-installer.log`。

故障分析与复现方式见 [自动恢复诊断记录](diagnostics/2026-09-17-proxy-auto-recovery.md)。
