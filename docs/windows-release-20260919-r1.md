# Windows Full 2026.09.19.1

修复采集代理监听失效但后台线程仍存活时，浏览器持续出现 `ERR_PROXY_CONNECTION_FAILED`、视频号任务长期重试的问题。

开启同步助手后，由独立后台线程每 5 秒检查本地代理的真实 HTTP 响应。Windows 接收连接异常和代理线程意外退出立即唤醒巡检；代理失效时先关闭系统代理，随后自动重建。冷却期或重建失败期间保持系统代理关闭，自动继续尝试；新代理通过 HTTP 验证后才重新接管流量。主动停止同步助手会取消巡检和后续重建。

此机制不依赖采集任务、前端页面或微信窗口操作。恢复采集仍需收到真实页面心跳，保留原有批次与暂停语义。本次未更改采集注入脚本。

## 验证

- 135 项代理、环境检测、任务恢复、界面和构建相关回归通过。
- 实际冻结 EXE 完成 100 次连续故障注入，全部自动恢复；包括 Windows accept 异常、端口丢失和线程退出。每次恢复均收到真实 HTTP 页面心跳，无需环境检测或桌面操作。
- 普通网页 HTTP 与 HTTPS CONNECT 转发在恢复后通过验证；事件循环卡住时先解除系统代理，再等待工作线程退出。
- 旧工作线程全部退出，仅保留一个巡检线程；100 次压力测试中句柄数量稳定。测试未修改真实系统代理和系统证书，未运行真实采集、OSS 或数据库任务。
- 18 个冻结模块与当前源码一致，资源文件一致，发行目录不含用户数据。
- 尚未完成 24 小时实机持续运行验证。加速故障注入不等于多日稳定性验证，不承诺操作系统不会再次发生瞬时连接重置。

## 交付位置

构建目录：`D:/CodexBuilds/videoDownloadTools/20260919-r1/`。

- 程序：`dist/WeChat MP Tools/`
- 安装包：`installer/自媒体内容采集工具_Windows_x64_Full_Setup_20260919_r1.exe`
- 内容核验：`package-verification.json`
- 冻结程序验证：`frozen-watchdog/result.json`
- 编译日志：`build-final.log`、`installer.log`

安装器统一使用 `scripts/build_windows_installer.py` 与 `installer/windows_full_setup.iss`，默认目录仍为 `%LOCALAPPDATA%\Programs\SelfMediaContentCollector`，AppId 不变。升级需要退出旧采集程序一次，新版启动后的故障处理自动执行。

安装规则预检与实际编译均成功，安装器文件版本为 `2026.9.19.1`，SHA256 为 `c37e70a107866e0714e0798f482b8dcbc05d5132fe5f5f573eb2835926f007cf`，已与 `.exe.sha256` 文件复核。交付校验记录为构建目录中的 `installer-verification.json`。未自动安装到用户电脑，未提交或推送 Git。

完整原因与验证边界见 [代理独立巡检诊断记录](diagnostics/2026-09-19-proxy-watchdog.md)。
