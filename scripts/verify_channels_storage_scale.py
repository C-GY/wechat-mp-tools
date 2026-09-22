"""Isolated SQLite migration/HTTP capture benchmark with synthetic legacy data."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mib',type=int,default=438)
    args=parser.parse_args()
    root=args.output.resolve()
    root.mkdir(parents=True,exist_ok=True)
    source=root/'feeds.json'
    if source.exists():
        parser.error('Use a new output directory to preserve prior benchmark evidence')
    count=0
    with source.open('w',encoding='utf-8') as out:
        out.write('{"large-author":[')
        while out.tell()<args.mib*1024*1024:
            item={'id':str(count),'description':'Fixture '+str(count),
                  'rpa_payload':{'padding':'x'*16000,'likeCount':count},'oss_video_url':'https://fixture.invalid/retained'}
            out.write((',' if count else '')+json.dumps(item,separators=(',',':')))
            count+=1
        out.write(']}')
    before=source.stat()
    from backend import config
    with patch.object(config,'DATA_DIR',root/'data'),patch.object(config,'OUTPUT_DIR',root/'output'):
        from backend import oss
        with patch.object(oss,'persistent_config_dir',return_value=root/'profile'):
            from backend import channels,mitm_proxy,channels_refresh
    from backend.channels_storage import FeedStore
    channels.CHANNELS_FEEDS_FILE=source
    channels.CHANNELS_FAVORITES_FILE=root/'favorites.json'
    mitm_proxy.DATA_DIR=root/'data'
    proxy=mitm_proxy.ProxyManager()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0)); proxy.port=reserved.getsockname()[1]
    timings={}
    health=[]
    stop=threading.Event()
    switch=Mock()
    def check_health():
        while not stop.wait(.2):
            started=time.perf_counter()
            health.append({'ok':proxy.is_healthy(timeout=1),'ms':round((time.perf_counter()-started)*1000,2)})
    with patch.object(mitm_proxy,'ensure_ca_certificates',lambda:None), \
         patch.object(mitm_proxy,'install_system_cert',lambda *_:True), \
         patch.object(mitm_proxy,'prepare_mitm_confdir',lambda:root/'mitm'), \
         patch.object(mitm_proxy,'set_system_proxy',switch), \
         patch.object(mitm_proxy,'_set_no_proxy',Mock()),patch.object(mitm_proxy,'_restore_no_proxy',Mock()):
        proxy.start()
        thread=proxy.thread
        switch.reset_mock()
        monitor=threading.Thread(target=check_health)
        monitor.start()
        try:
            from backend.channels_storage import feed_store
            store=feed_store(source)
            started=time.perf_counter(); store.ensure_ready(); timings['migration_seconds']=round(time.perf_counter()-started,3)
            assert len(store.author('large-author'))==count
            samples=[]
            channels_refresh.reset_refresh_task()
            task,_=channels_refresh.start_refresh_task([{'username':'new-author'}],require_receipt=True)
            channels_refresh.claim_refresh_command()
            for page in range(20):
                body=json.dumps({'username':'new-author','task_id':task['task_id'],'feeds':[
                    {'id':f'{page}-{i}','contact':{'nickname':'Fixture'},'objectDesc':{'mediaType':4,'media':[]}} for i in range(15)]})
                conn=http.client.HTTPConnection('127.0.0.1',proxy.port,timeout=5)
                started=time.perf_counter()
                try:
                    conn.request('POST','http://channels.weixin.qq.com/__wx_channels_api/sync-feed',body,{'Content-Type':'application/json'})
                    reply=json.loads(conn.getresponse().read())
                    assert len(reply['data']['saved_ids'])==15
                finally:
                    conn.close()
                samples.append(round((time.perf_counter()-started)*1000,3))
            timings['page_save_ms']=samples
            assert channels_refresh.get_refresh_status()['persisted_counts']['new-author']==300
            assert proxy.thread is thread and not any(c.args[0] is False for c in switch.call_args_list)
            assert (source.stat().st_size,source.stat().st_mtime_ns)==(before.st_size,before.st_mtime_ns)
            backup=root/'backup.sqlite3'
            started=time.perf_counter(); store.backup(backup); timings['backup_seconds']=round(time.perf_counter()-started,3)
            exported=root/'export.json'
            started=time.perf_counter(); store.export_json(exported); timings['export_seconds']=round(time.perf_counter()-started,3)
            # A second store imports the exported snapshot, checking the complete stream.
            restored=FeedStore(exported,root/'restored.sqlite3')
            restored.ensure_ready()
            assert len(restored.author('new-author'))==300
            assert len(restored.author('large-author'))==count
        finally:
            stop.set(); monitor.join(3); proxy.stop(); channels_refresh.reset_refresh_task()
    result={'source_bytes':before.st_size,'legacy_records':count,'added_records':300,'timings':timings,
            'health_samples':len(health),'health_failures':sum(not h['ok'] for h in health),
            'max_health_ms':max((h['ms'] for h in health),default=0),'database_bytes':store.path.stat().st_size}
    (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
    assert result['health_failures']==0


if __name__=='__main__':
    main()
