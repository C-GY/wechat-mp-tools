# Windows Full 2026.09.14.1

已完成当前源码的 Windows x64 完整版构建，包含竞对监测故障修复、内置 Chromium 和 WebView2 引导安装程序。

- 安装包：`dist/installer/自媒体内容采集工具_Windows_x64_Full_Setup_20260914_r1.exe`
- 大小：198,558,648 字节（189.36 MiB）。
- SHA256：`ca9003d23333e5b67617158ac25330dc056080b1eaaa6c7251a31997d92d0628`，同目录提供 `.exe.sha256` 文件。
- 默认安装目录：`%LOCALAPPDATA%\Programs\SelfMediaContentCollector`。
- AppId 保持 `{E956D31A-06A0-49BB-BA0B-3AE4DF127F6A}`，`UsePreviousAppDir=no`。

## 构建与验证

使用 Python 3.12 环境 `venv312`、PyInstaller 6.22.2 和 `wechat_mp_tools.spec` 重新构建业务代码，输出到 `dist/windows-20260914-full-r1/WeChat MP Tools/`。安装器使用 `installer/windows_full_setup.iss`，通过 `scripts/build_windows_installer.py --version 2026.09.14.1 --source-dir "dist/windows-20260914-full-r1/WeChat MP Tools"` 完成规则预检和实际编译，未覆盖历史安装包。

验证结果：

- 安装规则、安装器编译、文件版本和 SHA256 全部通过。
- 14 个相关冻结模块的代码对象与当前源码一致，四个修改过的前端/注入文件与源码逐字节一致。
- SQLite 运行时、Chromium 和 WebView2 引导程序均包含在包中；未包含实际用户的任务、采集数据及凭据配置文件。
- 使用独立 APPDATA 和测试数据启动实际打包程序，Windows 桌面窗口、后端接口、配置保存正常。
- 冻结程序迁移并保存 105 条测试失败记录，末页成功返回 5 条，摘要只保留 3 条预览，完整明细没有丢失。
- 包内 Chromium 151.0.7922.34 成功启动并渲染测试页面。
- 本轮相关回归和构建测试共 69 项通过。首次构建工作流测试受 Windows 默认 GBK 解码影响，使用 Python UTF-8 模式重跑该组后 30 项全部通过。

构建日志为 `build/windows-20260914-r1-build.log` 和 `build/windows-20260914-r1-installer.log`。成品验证记录保存在 `scratch/windows-20260914-r1-package-verification.json`、`scratch/windows-20260914-r1-smoke-final/result.json` 和 `scratch/windows-20260914-r1-installer-verification.json`。

## 使用范围

安装前退出旧程序，更新后重新打开微信视频号页面，以加载新采集脚本。固定默认目录不会自动迁移其他旧目录下的 `data/`。

本包包含已完成的故障处理与失败明细分页功能；页面按具体错误原因汇总尚未加入。历史被截掉的失败明细无法恢复。此次仅构建并验证成品，没有替用户安装，也没有重跑真实微信/OSS 失败任务。
