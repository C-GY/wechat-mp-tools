"""Export a fixed set of bounded, redacted logs; never load application data."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import uuid
from zipfile import ZipFile, ZIP_DEFLATED

from flask import Blueprint, jsonify, request

from backend.runtime import app_dir


diagnostics_bp = Blueprint('diagnostics', __name__, url_prefix='/api/diagnostics')
_export_lock = threading.Lock()


def _redact(text):
    text = re.sub(r'(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '<REDACTED private key>', text)
    sensitive = r'authorization|(?:set[-_])?cookie|[\w-]*token|password|passwd|[\w-]*secret|access[_-]?key[\w-]*|api[_-]?key|signature|wxsid|wxuin|密码'
    text = re.sub(r'(?im)^.*(?:' + sensitive + r''')[\\"'\s]*[:=][^\r\n]*\r?$''', '<REDACTED credential-bearing line>', text)
    text = re.sub(r'''(?m)(<REDACTED credential-bearing line>\r?\n)[ \t]*["'][^\r\n]*''', r'\1<REDACTED possible continued value>', text)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=._-]+', r'\1 <REDACTED>', text)
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@', r'\1<REDACTED>@', text)
    text = re.sub(r'''(?i)(https?://[^\s?"'<>]+)\?[^\s"'<>]*''', r'\1?<REDACTED>', text)
    text = re.sub(r'(?i)([A-Z]:\\+Users\\+)[^\\/\r\n"<>]+', r'\1<USER>', text)
    return re.sub(r'S-1-5-21-\d+-\d+-\d+-\d+', '<USER-SID>', text)


def _read_tail(path, limit):
    with path.open('rb') as source:
        prefix = source.read(2)
        encoding = 'utf-16-le' if prefix == b'\xff\xfe' else 'utf-16-be' if prefix == b'\xfe\xff' else 'utf-8-sig'
        size = source.seek(0, 2)
        start = max(0, size - limit)
        if encoding.startswith('utf-16') and start % 2:
            start += 1
        source.seek(start)
        text = source.read(limit).decode(encoding, errors='replace')
    if start:
        # Never leak a truncated credential value whose key was before the tail.
        index = text.find('\n')
        text = '[Earlier content / partial first line omitted.]\n' + (text[index + 1:] if index >= 0 else '[Oversized single line omitted.]')
    return _redact(text), size


def create_diagnostics_archive(root=None):
    root = (Path(root) if root is not None else app_dir()).resolve()
    output = root / 'data' / 'diagnostics'
    # Refuse redirected destinations as well as redirected input log files.
    if not output.resolve().is_relative_to(root):
        raise ValueError('Diagnostic destination is outside the application directory')
    output.mkdir(parents=True, exist_ok=True)
    name = 'Collector-Runtime-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.zip'
    destination = output / name
    temporary = destination.with_suffix('.tmp')
    limits = {}
    for base, limit in (
        ('data/logs/desktop_memory.jsonl', 8 * 1024 * 1024),
        ('data/logs/desktop_events.jsonl', 2 * 1024 * 1024),
        ('data/proxy_runtime.log', 2 * 1024 * 1024),
        ('data/logs/channels_environment.jsonl', 2 * 1024 * 1024),
    ):
        for suffix in ('', '.1', '.2', '.3'):
            limits[base + suffix] = limit
    for relative in ('wechat_mp_tools.log', 'data/frontend_errors.log', 'data/parse_debug.log', 'data/channels_call_log.jsonl'):
        limits[relative] = 2 * 1024 * 1024
    inventory = []
    try:
        with ZipFile(temporary, 'x', ZIP_DEFLATED) as archive:
            for relative, limit in limits.items():
                path = root / relative
                item = {'file': relative, 'status': 'missing'}
                try:
                    if path.is_file():
                        if not path.resolve().is_relative_to(root) or path.is_symlink():
                            item['status'] = 'skipped_redirected_file'
                        else:
                            text, size = _read_tail(path, limit)
                            archive.writestr('logs/' + relative + '.txt', text)
                            item.update(status='collected', bytes=size, truncated=size > limit,
                                        modified=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat())
                except Exception as exc:
                    item.update(status='read_error', error_type=type(exc).__name__)
                inventory.append(item)
            # These per-session watchdog receipts contain no webhook or credentials.
            receipts = []
            for path in (root / 'data' / 'logs').glob('desktop-watchdog-*.json'):
                try:
                    receipts.append((path.stat().st_mtime, path))
                except OSError:
                    pass
            for _, path in sorted(receipts, key=lambda item: item[0], reverse=True)[:20]:
                if re.fullmatch(r'desktop-watchdog-[0-9a-f]{32}\.json', path.name) and path.resolve().is_relative_to(root):
                    try:
                        text, _ = _read_tail(path, 64 * 1024)
                        archive.writestr('watchdog/' + path.name, text)
                    except OSError:
                        pass
            archive.writestr('inventory.json', json.dumps({'captured_at': datetime.now().astimezone().isoformat(),
                                                          'files': inventory}, ensure_ascii=False, indent=2))
            archive.writestr('README.txt', '运行诊断日志包。请附上出错时间、所在页面和是否刷新过。\n'
                             'desktop_memory 记录 Windows 软件进程树及系统内存；desktop_events 记录页面加载、导航及 WebView2 故障原因。\n'
                             '同一 session 可关联内存与事件。故障事件附有最后一次已保存的内存快照，不是 JavaScript 堆快照。\n'
                             '只打包白名单日志，不含配置、Cookie 文件、数据库、浏览器 Profile、媒体或内存转储。\n'
                             '已遮盖常见凭据和 URL 参数，发送前仍请检查。缺失、失败及截断见 inventory.json；没有日志不代表没有发生异常。\n')
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


@diagnostics_bp.post('/export')
def export_diagnostics():
    if not _export_lock.acquire(blocking=False):
        return jsonify(error='诊断包正在生成，请稍后再试'), 409
    try:
        path = create_diagnostics_archive()
        with ZipFile(path) as archive:
            files = json.loads(archive.read('inventory.json'))['files']
        return jsonify(path=str(path), filename=path.name, has_desktop_diagnostics=any(
            item['status'] == 'collected' and item['file'].startswith('data/logs/desktop_') for item in files))
    except Exception as exc:
        return jsonify(error=f'生成诊断包失败（{type(exc).__name__}）'), 500
    finally:
        _export_lock.release()


@diagnostics_bp.get('/alerts')
def get_alerts():
    from backend.desktop_alerts import AlertSettings
    from backend.desktop_diagnostics import desktop_alert_status
    try:
        return jsonify(**AlertSettings().public(), watchdog_status=desktop_alert_status())
    except Exception as exc:
        return jsonify(error=f'读取告警配置失败（{type(exc).__name__}）'), 500


@diagnostics_bp.post('/alerts')
def save_alerts():
    from backend.desktop_alerts import AlertSettings
    from backend.desktop_diagnostics import desktop_alert_status
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='告警配置必须是对象'), 400
    try:
        return jsonify(**AlertSettings().save(data), watchdog_status=desktop_alert_status())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=f'保存告警配置失败（{type(exc).__name__}）'), 500


@diagnostics_bp.post('/alerts/test')
def test_alerts():
    from backend.desktop_alerts import AlertSettings, deliver_alert
    try:
        settings = AlertSettings()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify(error='告警配置必须是对象'), 400
        config = settings.normalize({**settings.load(), **data})
        result = deliver_alert(settings, config, '自媒体内容采集工具告警测试',
                               '内容：崩溃与内存预警通知通道测试', 'test', os.getpid())
        if not result.get('sent'):
            return jsonify(error='测试消息未发送，请检查机器人来源、网络和安全设置'), 400
        return jsonify(sent=True, message='告警测试消息已发送')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=f'告警测试失败（{type(exc).__name__}）'), 500
