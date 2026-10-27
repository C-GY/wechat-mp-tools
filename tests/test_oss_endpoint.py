"""Configurable endpoint, immutable upload targets, and disposable S3 checks."""
import json
from pathlib import Path
from unittest.mock import patch

from flask import Flask
import pytest
import requests

from backend import oss


@pytest.mark.parametrize('value,expected', [
    ('minio.fandow.com', 'https://minio.fandow.com'),
    (' https://MINIO.FANDOW.COM:443/ ', 'https://minio.fandow.com'),
    ('http://127.0.0.1:9000', 'http://127.0.0.1:9000'),
    ('https://[::1]:9000/', 'https://[::1]:9000'),
    ('localhost:9000', 'https://localhost:9000'),
])
def test_endpoint_normalization(value, expected):
    assert oss.normalize_oss_endpoint(value) == expected


@pytest.mark.parametrize('value', ['', '*.fandow.com', r'minio.fandow\.com', 'ftp://minio.fandow.com',
    'https://host/bucket', 'https://host?key=x', 'https://host#fragment', 'https://USER:SECRET@host',
    'https://host:65536', 'https://host:0', 'https://host:', 'https://bad_host', 'https://a\nb', 'https://%2fhost'])
def test_invalid_endpoint_does_not_overwrite_configuration(value):
    oss.save_oss_config('upload-user', 'SECRET_SENTINEL', 'bucket')
    before = oss.OSS_CONFIG_FILE.read_bytes()
    with pytest.raises(ValueError):
        oss.save_oss_config('upload-user', '', 'bucket', endpoint=value)
    assert oss.OSS_CONFIG_FILE.read_bytes() == before


def test_domain_change_reuses_saved_credentials_and_old_clients_preserve_endpoint():
    assert oss.get_oss_config()['endpoint'] == 'https://minio.fandow.com'
    oss.save_oss_config('upload-user', 'SECRET_SENTINEL', 'bucket', 'https://oss.fandow.com')
    previous = oss.OSSService.from_saved_config()
    updated = oss.save_oss_config('upload-user', '', 'bucket', 'minio.fandow.com')
    assert updated['endpoint'] == 'https://minio.fandow.com'
    assert 'SECRET_SENTINEL' not in json.dumps(updated)
    assert oss.get_oss_config(include_secret=True)['access_key_secret'] == 'SECRET_SENTINEL'
    assert previous.endpoint == 'https://oss.fandow.com'
    assert previous.bucket == 'bucket'
    previous.session.close()
    oss.save_oss_config('upload-user', '', 'bucket', 'http://custom.invalid:9000')
    oss.save_oss_config('upload-user', '', 'next-bucket')
    assert oss.get_oss_config()['endpoint'] == 'http://custom.invalid:9000'


def test_invalid_stored_endpoint_does_not_disclose_embedded_credentials():
    oss.save_oss_config('upload-user', 'SECRET_SENTINEL', 'bucket')
    data = json.loads(oss.OSS_CONFIG_FILE.read_text(encoding='utf-8'))
    data['endpoint'] = 'https://user:EMBEDDED_SENTINEL@host'
    oss.OSS_CONFIG_FILE.write_text(json.dumps(data), encoding='utf-8')
    result = oss.get_oss_config()
    assert not result['configured']
    assert result['configuration_error']
    assert 'SENTINEL' not in json.dumps(result)
    oss.save_oss_config('upload-user', '', 'bucket', 'minio.fandow.com')
    assert oss.get_oss_config()['configured']


class Response:
    def __init__(self, status=200, content=b'', text='', headers=None):
        self.status_code, self.content, self.text = status, content, text
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content


class S3Session:
    def __init__(self, *, invalid_key=False, public_read=True, cleanup=True, uncertain_put=False):
        self.objects, self.calls = {}, []
        self.invalid_key, self.public_read, self.cleanup, self.uncertain_put = invalid_key, public_read, cleanup, uncertain_put
        self.closed = False

    def put(self, url, data, **kwargs):
        self.calls.append(('PUT', url, kwargs))
        if self.invalid_key:
            return Response(403, text='<Error><Code>InvalidAccessKeyId</Code><Secret>SECRET_SENTINEL</Secret></Error>')
        self.objects[url] = data
        if self.uncertain_put:
            raise requests.ReadTimeout('SECRET_SENTINEL')
        return Response()

    def head(self, url, **kwargs):
        self.calls.append(('HEAD', url, kwargs))
        return Response(headers={'Content-Length': str(len(self.objects[url]))})

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        if not self.public_read:
            return Response(403, text='<Error><Code>AccessDenied</Code></Error>')
        return Response(content=self.objects[url])

    def delete(self, url, **kwargs):
        self.calls.append(('DELETE', url, kwargs))
        if not self.cleanup:
            return Response(403, text='<Error><Code>AccessDenied</Code></Error>')
        self.objects.pop(url, None)
        return Response(204)

    def close(self):
        self.closed = True


def test_disposable_verification_uses_same_domain_and_cleans_only_its_test_file():
    session = S3Session()
    service = oss.OSSService('user', 'secret', session=session, bucket='bucket', endpoint='minio.fandow.com')
    result = service.verify_connection()
    assert result['verified'] and result['cleanup'] and result['read'] and result['upload']
    assert [method for method, _, _ in session.calls] == ['PUT', 'HEAD', 'GET', 'DELETE']
    assert len({url for _, url, _ in session.calls}) == 1
    assert session.calls[0][1].startswith('https://minio.fandow.com/bucket/wechat_channel/.connection-check/')
    assert all(not options['allow_redirects'] for _, _, options in session.calls)
    assert 'headers' not in session.calls[2][2]  # Public reads match stored video link requirements.
    assert not session.objects


@pytest.mark.parametrize('options,expected', [
    ({'invalid_key': True}, 'InvalidAccessKeyId'), ({'public_read': False}, 'AccessDenied'),
    ({'uncertain_put': True}, 'ReadTimeout'), ({'cleanup': False}, '清理未确认'),
])
def test_verification_reports_real_failure_and_cleans_possible_uploads(options, expected):
    session = S3Session(**options)
    service = oss.OSSService('user', 'SECRET_SENTINEL', session=session, bucket='bucket')
    with pytest.raises(RuntimeError, match=expected) as failure:
        service.verify_connection()
    assert 'SECRET_SENTINEL' not in str(failure.value)
    if options.get('invalid_key'):
        assert [call[0] for call in session.calls] == ['PUT']
    elif options.get('cleanup', True):
        assert session.calls[-1][0] == 'DELETE'
        assert not session.objects


def test_api_verifies_unsaved_domain_with_saved_secret_without_changing_config():
    oss.save_oss_config('user', 'SECRET_SENTINEL', 'bucket', 'https://oss.fandow.com')
    original = oss.OSS_CONFIG_FILE.read_bytes()
    app = Flask(__name__)
    app.register_blueprint(oss.oss_bp)
    session = S3Session()
    with patch.object(oss.requests, 'Session', return_value=session):
        response = app.test_client().post('/api/oss/test-connection', json={
            'access_key_id': 'user', 'access_key_secret': '', 'bucket': 'bucket', 'endpoint': 'minio.fandow.com'})
    assert response.status_code == 200
    assert response.json['endpoint'] == 'https://minio.fandow.com'
    assert 'SECRET_SENTINEL' not in response.get_data(as_text=True)
    assert session.closed
    assert oss.OSS_CONFIG_FILE.read_bytes() == original


def test_historical_receipts_keep_original_domain_and_cannot_be_reused_for_new_domain():
    old_url = 'https://oss.fandow.com/bucket/wechat_channel/video-1.mp4'
    receipt = oss._legacy_upload_receipt('video-1', old_url)
    assert receipt['endpoint'] == 'https://oss.fandow.com'
    assert receipt['url'] == old_url
    oss.upload_receipts().save(receipt['endpoint'], oss.DEFAULT_OSS_OBJECT_PREFIX, 'video-1', receipt)
    assert oss.upload_receipts().get('https://oss.fandow.com', 'bucket', oss.DEFAULT_OSS_OBJECT_PREFIX, 'video-1')['url'] == old_url
    assert oss.upload_receipts().get('https://minio.fandow.com', 'bucket', oss.DEFAULT_OSS_OBJECT_PREFIX, 'video-1') is None
