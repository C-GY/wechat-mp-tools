# Windows Full 2026.09.17.2

程序窗口标题与浏览器标题显示为“自媒体内容采集工具 v2026.09.17.2”；左上角名称下方同时显示版本号。三个位置统一读取 `backend/config.py` 的版本常量，后续升级不需要分别维护显示文案。保留 2026.09.17.1 的视频号代理自动恢复修复。

首页和 `/index.html` 由 Flask 渲染当前运行版本，避免安装后只能通过安装文件名判断版本。侧边栏版本单独占一行，不挤压原有程序名称。

## 验证

- 3 项现有品牌与导航测试通过。
- 18 个冻结业务模块与源码一致，首页、样式及既有前端/采集脚本与发布资源一致。
- 已启动实际打包程序，在独立 APPDATA 下验证原生窗口标题、`/` 与 `/index.html` 页面标题及版本号。
- 已通过浏览器检查版本可见、侧边栏无溢出，并保存实际显示截图；未运行真实业务任务。
- 安装默认路径仍为 `%LOCALAPPDATA%\Programs\SelfMediaContentCollector`，AppId 不变，仍使用 `scripts/build_windows_installer.py` 和 `installer/windows_full_setup.iss`。

## 构建位置

F 盘空间不足，因此本次构建与验证产物保存在 `D:/CodexBuilds/videoDownloadTools/20260917-r2/`。旧安装包保留，未修改已安装程序。

- 程序目录：`dist/WeChat MP Tools/`
- 包内容核验：`package-verification.json`
- 运行验证：`version-smoke-20260917-r2/result.json`
- 版本截图：`version-smoke-20260917-r2/visible-version.png`
- 构建与编译日志：`build.log`、`installer.log`

安装包为上述目录下的 `installer/自媒体内容采集工具_Windows_x64_Full_Setup_20260917_r2.exe`。安装规则预检、实际编译和文件版本校验均成功；SHA256 为 `4c11cca0043d52114c4be2195c65adaaa27b054a00b4d1fee63cedf8731459c1`，已与同目录校验文件核对。成品记录保存在 `installer-verification.json`。
