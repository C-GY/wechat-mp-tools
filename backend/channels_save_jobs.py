"""Bounded page writes with durable receipts; HTTP requests never wait for disk."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time

from backend.channels_refresh import capture_write, validate_capture, record_capture
from backend.channels_storage import feed_store
from backend.sync_errors import redact_error

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ChannelsPageSave')
_slots = threading.BoundedSemaphore(8)
_lock = threading.RLock()
_jobs = {}


def _store():
    from backend.channels import CHANNELS_FEEDS_FILE
    return feed_store(CHANNELS_FEEDS_FILE)


def _identity(payload):
    values = tuple(str(payload.get(key) or '') for key in ('task_id','username','page_id'))
    if not all(values) or any(len(v)>1024 for v in values):
        raise ValueError('采集保存请求缺少有效的任务、作者或页面标识')
    return values


def submit_page(payload):
    task, author, page = key = _identity(payload)
    lease = payload.get('lease_token')
    validate_capture(task,author,lease)
    feeds = payload.get('feeds')
    if not isinstance(feeds,list) or len(feeds)>1000:
        raise ValueError('采集页数据无效')
    digest = hashlib.sha256(json.dumps(feeds,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    store = _store()
    # Migration normally finishes before collection. Do not run it in this request.
    if store.status()['status'] != 'ready':
        store.prepare_background()
        return {'status':'preparing','message':store.status()['message']}
    old = store.receipt(*key)
    if old:
        # merge's receipt-only transaction validates the digest without rewriting rows.
        with capture_write(task,author,lease):
            receipt = store.merge(author,[],page={'task_id':task,'page_id':page,'digest':digest})
            record_capture(task,author,receipt['saved_ids'],lease)
        return {'status':'saved',**receipt}
    with _lock:
        job = _jobs.get(key)
        if job and not job['future'].done():
            if job['digest'] != digest:
                raise ValueError('同一采集页的内容不一致')
            return {'status':'pending'}
        if not _slots.acquire(blocking=False):
            return {'status':'busy','message':'保存队列繁忙，正在等待'}
        def write():
            from backend.mitm_proxy import save_synced_feeds
            from backend import mitm_proxy
            from backend.proxy_logging import runtime_logger
            started = time.monotonic()
            try:
                receipt = save_synced_feeds(author,feeds,task_id=task,lease_token=lease,
                                           page={'task_id':task,'page_id':page,'digest':digest})
                runtime_logger(mitm_proxy.DATA_DIR).info('page_saved task=%s page=%s saved=%d elapsed_ms=%.1f',
                                                       task,page,len(receipt['saved_ids']),(time.monotonic()-started)*1000)
                return receipt
            except Exception as exc:
                runtime_logger(mitm_proxy.DATA_DIR).warning('page_save_failed task=%s page=%s error=%s',task,page,redact_error(exc))
                raise
            finally:
                _slots.release()
        try:
            future = _executor.submit(write)
        except Exception:
            _slots.release()
            raise
        _jobs[key] = {'digest':digest,'future':future}
        for old_key in list(_jobs):
            if len(_jobs)<=256:
                break
            if _jobs[old_key]['future'].done() and old_key != key:
                del _jobs[old_key]
    return {'status':'pending'}


def page_status(payload):
    task,author,page = key = _identity(payload)
    validate_capture(task,author,payload.get('lease_token'))
    receipt = _store().receipt(*key)
    if receipt:
        return {'status':'saved',**receipt}
    with _lock:
        job = _jobs.get(key)
    if not job:
        return {'status':'missing'}
    if not job['future'].done():
        return {'status':'pending'}
    try:
        return {'status':'saved',**job['future'].result()}
    except Exception as exc:
        return {'status':'failed','message':redact_error(exc)}


def save_checkpoint(payload):
    task,author,page = _identity(payload)
    number, marker = payload.get('page_number'), payload.get('marker','')
    if type(number) is not int or not 1<=number<=1000000 or not isinstance(marker,str) or len(marker)>65536:
        raise ValueError('分页进度无效')
    value = {'page_number':number,'marker':marker,'complete':payload.get('complete') is True}
    with capture_write(task,author,payload.get('lease_token')):
        _store().checkpoint(task,author,page,value)
    return {'status':'saved'}


def resume_capture(payload):
    task,author = str(payload.get('task_id') or ''), str(payload.get('username') or '')
    with capture_write(task,author,payload.get('lease_token')):
        result = _store().resume(task,author)
        record_capture(task,author,result['saved_ids'],payload.get('lease_token'))
    return result


def reset_cursor(payload):
    task,author = str(payload.get('task_id') or ''), str(payload.get('username') or '')
    with capture_write(task,author,payload.get('lease_token')):
        _store().reset_cursor(task,author)
    return {'status':'reset'}
