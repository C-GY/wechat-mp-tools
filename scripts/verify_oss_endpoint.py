"""Verify a configured S3 endpoint with one disposable object, without app startup."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='https://minio.fandow.com')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    # Import only OSS and isolate its queue files; never start Flask or schedulers.
    from backend import config
    import requests
    report = {'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
              'verified': False, 'existing_credentials_reused': True}
    with tempfile.TemporaryDirectory(prefix='collector-oss-check-') as temporary:
        config.DATA_DIR = Path(temporary) / 'data'
        config.OUTPUT_DIR = config.DATA_DIR / 'articles'
        from backend import oss
        service = None
        try:
            endpoint = oss.normalize_oss_endpoint(args.endpoint)
            report['endpoint'] = endpoint
            host = oss.urlsplit(endpoint).hostname
            report['addresses'] = sorted({value[4][0] for value in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
            with requests.Session() as http:
                http.trust_env = False
                with http.get(endpoint + '/minio/health/live', timeout=(5, 10), allow_redirects=False) as response:
                    report['health_status'] = response.status_code
                saved = oss.get_oss_config(include_secret=True)
                service = oss.OSSService(saved['access_key_id'], saved['access_key_secret'], session=http,
                                         bucket=saved['bucket'], endpoint=endpoint)
                report.update(service.verify_connection())
        except (ValueError, RuntimeError) as exc:
            report['error'] = str(exc)
            if hasattr(exc, 'status_code'):
                report['status_code'] = exc.status_code
                report['s3_error_code'] = exc.s3_error_code
        except Exception as exc:
            report['error_type'] = type(exc).__name__
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
