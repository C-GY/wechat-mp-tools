"""Run the frozen release's storage and HTTP save protocol in an isolated profile."""
import argparse
import builtins
import json
import multiprocessing as mp
import multiprocessing.spawn as spawn
import os
from pathlib import Path
import shutil
from unittest.mock import patch


CHILD = r'''
import http.client, json, socket, sys, threading, time, traceback
from pathlib import Path
root = Path(sys.executable).parent
report = {'ok':False, 'frozen':bool(getattr(sys,'frozen',False))}
proxy = None
try:
    from backend import config, channels, channels_refresh, channels_save_jobs, mitm_proxy
    from backend.channels_storage import feed_store, FeedStore
    assert sys.frozen and config.APP_VERSION == '2026.09.22.1'
    assert Path(channels_save_jobs.__file__).is_relative_to(Path(sys._MEIPASS))
    source = channels.CHANNELS_FEEDS_FILE
    source.parent.mkdir(parents=True, exist_ok=True)
    second = (root/'first.json').exists()
    if not second:
        source.write_text(json.dumps({'legacy':[{'id':'old','rpa_payload':{'保留':'历史'},'oss_video_url':'verified'}]}),encoding='utf-8')
    original = source.read_bytes()
    store = feed_store(source)
    store.ensure_ready()
    assert store.path.is_relative_to(root/'profile')
    assert store.author('legacy')[0]['rpa_payload'] == {'保留':'历史'}
    resume = store.resume('frozen-task','author')
    task,_ = channels_refresh.start_refresh_task([{'username':'author'}],require_receipt=True,
        task_id='frozen-task',persisted_ids={'author':resume['saved_ids']})
    lease = channels_refresh.claim_refresh_command('frozen-page')['lease_token']
    mitm_proxy.ensure_ca_certificates = lambda: None
    mitm_proxy.install_system_cert = lambda *_: True
    mitm_proxy.prepare_mitm_confdir = lambda: root/'mitm'
    mitm_proxy._set_no_proxy = lambda: None
    mitm_proxy._restore_no_proxy = lambda: None
    switches=[]
    mitm_proxy.set_system_proxy = lambda enabled, **_: switches.append(enabled)
    proxy = mitm_proxy.ProxyManager()
    proxy.watchdog_interval=.05
    proxy.watchdog_probe_timeout=.2
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0)); proxy.port=reservation.getsockname()[1]
    proxy.start()
    thread=proxy.thread
    switches.clear()
    def call(path, body=None):
        conn=http.client.HTTPConnection('127.0.0.1',proxy.port,timeout=2)
        try:
            conn.request('POST' if body is not None else 'GET','http://channels.weixin.qq.com/__wx_channels_api/'+path,
                         None if body is None else json.dumps(body),{'Content-Type':'application/json'})
            result=json.loads(conn.getresponse().read())
            assert result.get('code') == 0, result
            return result['data']
        finally:
            conn.close()
    from urllib.parse import urlencode
    identity={'username':'author','task_id':'frozen-task','lease_token':lease}
    if not second:
        entered, release = threading.Event(),threading.Event()
        original_merge=store._merge
        def slow(db,author,items):
            entered.set()
            assert release.wait(4)
            return original_merge(db,author,items)
        store._merge=slow
        body={**identity,'page_id':'page-1','feeds':[{'id':'one','objectDesc':{'mediaType':4,'media':[]}}]}
        try:
            assert call('sync-feed',body)['status']=='pending'
            assert entered.wait(2)
            for _ in range(5):
                assert proxy.is_healthy(timeout=.4)
                time.sleep(.05)
            assert proxy.thread is thread and False not in switches
        finally:
            release.set()
        query='sync-feed-status?'+urlencode({**identity,'page_id':'page-1'})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            receipt=call(query)
            if receipt['status']=='saved': break
            time.sleep(.02)
        assert receipt['saved_ids']==['one']
        store._merge=original_merge
        proxy.stop(); proxy.start()
        assert call(query)['saved_ids']==['one']
        assert call('sync-feed',body)['status']=='saved'
        call('refresh-checkpoint',{**identity,'page_id':'page-1','page_number':2,'marker':'next','complete':False})
        assert call('refresh-resume?'+urlencode(identity))['marker']=='next'
    else:
        assert resume['marker']=='next' and resume['saved_ids']==['one']
        receipt=call('sync-feed-status?'+urlencode({**identity,'page_id':'page-1'}))
        assert receipt['saved_ids']==['one']
        body={**identity,'page_id':'page-2','feeds':[{'id':'two','objectDesc':{'mediaType':4,'media':[]}}]}
        call('sync-feed',body)
        query='sync-feed-status?'+urlencode({**identity,'page_id':'page-2'})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            receipt=call(query)
            if receipt['status']=='saved':break
            time.sleep(.02)
        assert receipt['saved_ids']==['two']
        call('refresh-checkpoint',{**identity,'page_id':'page-2','page_number':2,'marker':'','complete':True})
        store.patch_existing('author','one',{'oss_video_url':'saved-oss'})
        backup=root/'snapshot.sqlite3'; store.backup(backup)
        assert len(FeedStore(root/'absent.json',backup).author('author'))==2
        exported=root/'complete.json'; store.export_json(exported)
        exported_data=json.loads(exported.read_text(encoding='utf-8'))
        assert exported_data['author'][0]['oss_video_url']=='saved-oss'
        assert len(exported_data['author'])==2
        assert channels_refresh.get_refresh_status()['persisted_counts']['author']==2
        # A slow startup migration must not delay serving the window or status.
        from backend import oss
        from backend.rss_scheduler import rss_scheduler
        from backend.pinchuang import pinchuang_hub
        from backend.guangce import guangce_hub
        from backend.competitor_monitor import competitor_monitor_hub
        from backend.creative_radar import creative_radar_hub
        for scheduler in (rss_scheduler,pinchuang_hub,guangce_hub,competitor_monitor_hub,creative_radar_hub):
            scheduler.start=lambda:None
        entered,release=threading.Event(),threading.Event()
        def wait_migration():
            entered.set()
            assert release.wait(10)
        oss.upload_manager.migrate_upload_receipts=wait_migration
        try:
            started=time.monotonic()
            import app
            assert entered.wait(2)
            assert time.monotonic()-started < 5
            client=app.app.test_client()
            assert client.get('/').status_code==200
            assert client.get('/api/channels/storage/status').status_code==200
            report['startup_remains_responsive_during_migration']=True
        finally:
            release.set()
    assert source.read_bytes()==original
    report.update(ok=True,version=config.APP_VERSION,stage='restart' if second else 'first',
                  real_http=True,real_system_proxy_or_certificate_changes=False,
                  source_imports=False,local_database=str(store.path))
except BaseException:
    report['error']=traceback.format_exc()
finally:
    if proxy:
        proxy.stop()
    (root/('restart.json' if (root/'first.json').exists() else 'first.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve()
    if root.exists():
        parser.error('Use a new directory; prior verification evidence is preserved')
    # Test files are created beside the read-only hardlinked release, never in it.
    shutil.copytree(args.bundle,root,copy_function=os.link)
    exe=root/'WeChat MP Tools.exe'
    original_prepare=spawn.get_preparation_data
    def prepare(name):
        data=original_prepare(name)
        data.pop('init_main_from_path',None); data.pop('init_main_from_name',None)
        data['sys_path']=[str(root/'_internal/base_library.zip'),str(root/'_internal')]
        data['dir']=str(root)
        return data
    def command_line(**kwargs):
        return [str(exe),'--multiprocessing-fork',*[f'{key}={value!r}' for key,value in kwargs.items()]]
    previous=spawn.get_executable()
    mp.set_executable(str(exe))
    reports=[]
    try:
        for name in ('first','restart'):
            process=mp.get_context('spawn').Process(target=builtins.exec,args=(CHILD,{}))
            try:
                with patch.dict(os.environ,{'APPDATA':str(root/'profile')}), \
                     patch.object(spawn,'get_preparation_data',prepare), \
                     patch.object(spawn,'get_command_line',command_line):
                    process.start()
                process.join(45)
                assert not process.is_alive(), 'Frozen verification timed out'
                report=json.loads((root/(name+'.json')).read_text(encoding='utf-8'))
                reports.append(report)
                assert report['ok'],report.get('error')
            finally:
                if process.is_alive():
                    process.terminate(); process.join(5)
    finally:
        mp.set_executable(previous)
    print(json.dumps(reports,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
