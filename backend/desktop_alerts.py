"""Desktop alert settings/policy. Reads bot credentials without loading hubs."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
import uuid

from backend.profile_paths import persistent_config_dir


SOURCES = {
    'pinchuang': ('历史通知配置 1', 'pinchuang_hub_config.json'),
    'guangce': ('历史通知配置 2', 'guangce_hub_config.json'),
    'competitor': ('竞对监测', 'competitor_monitor_config.json'),
    'radar': ('历史通知配置 3', 'creative_radar_config.json'),
}
DEFAULTS = dict(enabled=True, source='auto', sustain_seconds=60, cooldown_seconds=600,
                renderer_private_mib=1536, application_private_mib=2048, system_commit_percent=90)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    try:
        with Path(path).open('rb') as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('Configuration exceeds size limit')
        result = json.loads(data.decode('utf-8-sig'))
        if not isinstance(result, dict):
            raise ValueError('Configuration must be an object')
        return result
    except FileNotFoundError:
        return {}


class AlertSettings:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else persistent_config_dir()
        self.path = self.directory / 'desktop_alerts.json'

    @staticmethod
    def normalize(data):
        result = {key: data.get(key, value) for key, value in DEFAULTS.items()}
        if type(result['enabled']) is not bool or result['source'] not in ('auto', *SOURCES):
            raise ValueError('告警开关或机器人来源无效')
        for name, low, high in (('sustain_seconds', 15, 3600), ('cooldown_seconds', 60, 86400),
                                ('renderer_private_mib', 128, 131072), ('application_private_mib', 128, 131072),
                                ('system_commit_percent', 50, 99)):
            if type(result[name]) is not int or not low <= result[name] <= high:
                raise ValueError(f'{name} 必须为 {low}～{high} 之间的整数')
        return result

    def load(self):
        return self.normalize(read_json(self.path))

    def target(self, config):
        candidates = []
        keys = SOURCES if config['source'] == 'auto' else [config['source']]
        for key in keys:
            label, filename = SOURCES[key]
            raw = read_json(self.directory / filename).get('feishu', {})
            if not isinstance(raw, dict):
                continue
            url = str(raw.get('webhook_url') or '').strip()
            if url:
                candidates.append(dict(label=label, url=url, secret=str(raw.get('secret') or '').strip()))
        if not candidates:
            raise ValueError('未找到已配置的飞书机器人，请先在业务模块配置')
        if len({c['url'] for c in candidates}) != 1 or len({c['secret'] for c in candidates if c['secret']}) > 1:
            raise ValueError('检测到多个不同机器人或签名配置，请选择告警使用的模块')
        return dict(url=candidates[0]['url'], secret=next((c['secret'] for c in candidates if c['secret']), ''),
                    label='、'.join(c['label'] for c in candidates))

    def public(self):
        config = self.load()
        # Retired modules never run; reuse an existing notification channel only.
        legacy_sources = []
        for key, (label, filename) in SOURCES.items():
            if key == 'competitor':
                continue
            try:
                raw = read_json(self.directory / filename).get('feishu')
                if isinstance(raw, dict) and str(raw.get('webhook_url') or '').strip():
                    legacy_sources.append({'source': key, 'label': label})
            except (OSError, ValueError):
                pass
        try:
            target = self.target(config)
            return {**config, 'configured': True, 'target_label': target['label'], 'configuration_error': '', 'legacy_sources': legacy_sources}
        except ValueError as exc:
            return {**config, 'configured': False, 'target_label': '', 'configuration_error': str(exc), 'legacy_sources': legacy_sources}

    def save(self, data):
        config = self.normalize({**self.load(), **data})
        if config['enabled']:
            self.target(config)
        atomic_json(self.path, config)
        return self.public()


def deliver_alert(settings, config, title, details, session, parent_pid):
    """Only the watchdog or an explicit UI test calls this; never a UI event thread."""
    from backend.feishu_notifier import FeishuNotifier
    import requests
    try:
        target = settings.target(config)
    except ValueError:
        return {'sent': False, 'skipped': True, 'reason': 'robot_not_configured_or_ambiguous'}
    machine = re.sub(r'[^\w.-]', '_', os.environ.get('COMPUTERNAME', 'Windows'))[:64]
    message = (f'应用：自媒体内容采集工具\n机器：{machine}\n时间：{datetime.now().astimezone().isoformat(timespec="seconds")}\n'
               f'通知配置：{target["label"]}\n进程：{parent_pid}\n会话：{session[:8]}\n{details}\n'
               '排查：系统设置 → 运行诊断日志 → 导出诊断日志')
    # A dead app can leave the Windows proxy pointing at its dead local listener.
    # Bypass that proxy only for this alert session; never change system settings.
    with requests.Session() as http:
        http.trust_env = False
        result = FeishuNotifier(target['url'], target['secret'], session=http).send(title, message)
    return {'sent': bool(result.get('sent')), 'attempts': result.get('attempts', 0)}


class AlertPolicy:
    def __init__(self, parent_pid, send, clock=time.monotonic):
        self.parent_pid, self.send, self.clock = parent_pid, send, clock
        self.since = {}
        self.next_allowed = {}
        self.last_memory = None
        self.last_config = None

    def emit(self, key, title, detail, config):
        now = self.clock()
        if now < self.next_allowed.get(key, 0):
            return
        result = self.send(title, detail, config)
        self.next_allowed[key] = now + (config['cooldown_seconds'] if result.get('sent') else 60)

    def consume(self, record, config):
        if config != self.last_config:
            self.since.clear()
            self.last_config = dict(config)
        if record.get('event') == 'memory_sample':
            self.last_memory = record
        if not config['enabled']:
            self.since.clear()
            return
        kind = record.get('event')
        if kind == 'memory_sample':
            self._memory(record, config)
        elif kind == 'webview_process_failed':
            failure = record.get('ProcessFailedKind')
            reason = record.get('Reason', 'Unknown')
            reason = reason if isinstance(reason, str) and re.fullmatch(r'[A-Za-z0-9_]{1,64}', reason) else 'Unknown'
            if failure == 'RenderProcessUnresponsive':
                self.emit('page_unresponsive', '自媒体内容采集工具页面无响应', f'事件：页面无响应\n原因：{reason}', config)
            elif failure in ('RenderProcessExited', 'FrameRenderProcessExited', 'BrowserProcessExited'):
                code = record.get('ExitCode')
                detail = f'事件：{failure}\n原因：{reason}' + (f'\n退出码：{code}' if type(code) is int else '')
                self.emit('page_crash', '自媒体内容采集工具页面崩溃', detail, config)
        elif kind == 'browser_process_exited' and record.get('exit_kind') != 'Normal':
            self.emit('page_crash', '自媒体内容采集工具浏览器异常退出', '事件：内嵌浏览器异常退出', config)
        elif kind == 'application_startup_failed':
            self.emit('application_error', '自媒体内容采集工具启动异常', '事件：应用启动失败，请查看启动日志', config)

    def _memory(self, record, config):
        bad = {}
        system = record.get('system') or {}
        commit, limit = system.get('committed_bytes'), system.get('commit_limit_bytes')
        if isinstance(commit, int) and isinstance(limit, int) and limit > 0 and commit / limit * 100 >= config['system_commit_percent']:
            bad['system'] = f'系统已提交内存：{commit / limit * 100:.1f}%（阈值 {config["system_commit_percent"]}%）'
        for process in record.get('processes', []):
            private = process.get('private_bytes')
            if not isinstance(private, int):
                continue
            is_app = process.get('pid') == self.parent_pid
            is_renderer = process.get('role') == 'Renderer'
            threshold = config['application_private_mib'] if is_app else config['renderer_private_mib'] if is_renderer else None
            if threshold is not None and private >= threshold * 1024 * 1024:
                key = (process.get('pid'), process.get('created_filetime'))
                bad[key] = f'{"主程序" if is_app else "网页渲染进程"} PID {process["pid"]} 私有内存：{private / 1024**2:.0f} MiB（阈值 {threshold} MiB）'
        now = self.clock()
        self.since = {key: self.since.get(key, now) for key in bad}
        persistent = [bad[key] for key, since in self.since.items() if now - since >= config['sustain_seconds']]
        if persistent:
            self.emit('memory_risk', '自媒体内容采集工具内存风险预警',
                      '事件：内存持续高于阈值\n' + '\n'.join(persistent[:8]) + '\n说明：这是风险预警，不表示一定会崩溃。', config)

    def exited(self, exit_code, normal, config):
        if config['enabled'] and not normal:
            self.emit('application_exit', '自媒体内容采集工具异常退出', f'事件：主进程已异常退出\n退出码：{exit_code}', config)
