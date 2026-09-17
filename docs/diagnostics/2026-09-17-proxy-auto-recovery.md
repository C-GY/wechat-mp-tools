# 视频号长期运行后连接中断的自动恢复

用户提供的日志显示，2026-09-16 16:18:17 代理接收连接时出现 WinError 64；环境日志推算最后一次页面心跳约为当日 16:51:40。此后至 2026-09-17 09:57，444 次环境检查中 443 次已返回不可用，最后一次尚未结束，代理运行标记始终为 true。微信持续运行，恢复窗口和发送刷新按键也没有恢复心跳。

异常时间早于最后一次心跳，因此不能仅凭日志认定 WinError 64 是最初断连的直接原因。但当前代码中“线程仍活着、监听端口已失效”不会自动重建代理的缺口已在真实本机代理上复现。

## 复现与修复

回归测试先启动真实 mitmproxy，再单独关闭监听端口，保留后台线程及运行标记；模拟已经打开的视频号页面继续轮询。修改前，环境检查返回不可用，需要人工干预；修改后，自动重建代理并收到现有页面心跳，恢复过程无需打开微信或刷新窗口。窗口枚举成功和未识别到窗口两种情况均覆盖。

```powershell
venv312/Scripts/python.exe scripts/run_monitor_tests.py tests/test_proxy_lifecycle.py -k lost_listener -q
```

修复加入本机 HTTP 健康检查，要求返回当前代理实例的标记，覆盖监听丢失、能连接 TCP 却没有 HTTP 响应，以及端口被其他服务占用的情况。探测由代理本地响应，不向微信服务器发送请求，不上报或伪造采集心跳。

心跳超时后，环境检查先恢复异常代理，再等候真实页面心跳，最后才按原规则进行窗口恢复操作。代理重建串行执行，每次尝试至少间隔 60 秒；失败也受限频控制。重建失败时保留现有任务，由原恢复循环继续重试。健康代理不会仅因缺少页面心跳而被重启。

健康检查和重建结果分别写入环境诊断日志的 `proxy_health_checked`、`proxy_recovery_failed` 事件及 `proxy_runtime.log`，便于后续区分代理故障和页面连接问题。

## 验证

相关联合回归 83 项通过，包括真实代理故障恢复、同一批次续跑、已完成作者不重跑、停止及暂停语义、页面就绪检查和界面行为。剩余警告来自依赖中的既有弃用提示。

```powershell
venv312/Scripts/python.exe scripts/run_monitor_tests.py tests/test_proxy_lifecycle.py tests/test_channels_environment.py tests/test_pinchuang_recovery.py tests/test_competitor_recovery.py tests/test_competitor_resilience.py tests/test_channels_environment_ui.py tests/test_channels_pinchuang_ui.py -q --disable-warnings
```

测试通过系统代理和证书操作的替身边界隔离本机设置，通过临时目录隔离业务状态，不重跑用户的真实微信、OSS 或数据库任务。验证包含故障注入，并非连续多天的实机运行；后续仍需观察实际长期运行日志。
