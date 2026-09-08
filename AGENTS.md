# Windows 安装包

构建 Windows 安装包、修改安装器或交付升级包前，阅读 [BUILD.md 的固定安装规则](BUILD.md#windows-installer-policy)，并使用 `scripts/build_windows_installer.py`。

用户要求每次安装默认使用 `%LOCALAPPDATA%\Programs\SelfMediaContentCollector`。统一使用 `installer/windows_full_setup.iss`，保持其 AppId 稳定；安装规则校验和实际编译均成功后才能交付安装包。
