"""Replay responses through the real injected author-pagination function."""
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('injection_scripts/src/automation.js', 'utf8');
const fn = source.slice(source.indexOf('  async function refreshFavoriteAuthor'), source.indexOf('  async function runRemoteFavoritesRefresh'));
let cancelled=false, circuitOpen=false, my_username='', PAGE_JITTER_MS=0;
const esc=x=>x, setPanel=()=>{}, noteFailure=()=>{};
let reports=[];
const reportRemoteProgress=async(taskId,body)=>{reports.push(JSON.parse(JSON.stringify(body)));};
let pages=[], inputs=[], saved=[], logs=[], waits=[], failSave=false, onWait=()=>{};
const jitterSleep=async(ms)=>{waits.push(ms);onWait();};
const logCall=(api,code,message,ms,extra)=>logs.push({api,code,message,...extra});
const callWithRetry=async(_name,call)=>call();
const obj=id=>({id,objectDesc:{mediaType:4}});
const response=(ids,marker,flags={})=>({errCode:0,data:{object:ids.map(obj),lastBuffer:marker,...flags}});
const WXU={API:{finderUserPage:async(args)=>{
  inputs.push(args.lastBuffer);
  if(inputs.length>15) throw new Error('unbounded pagination');
  return pages.length>1 ? pages.shift() : pages[0];
}},request:async({body})=>{
  if(failSave) return [new Error('disk full'),null];
  saved.push(...body.feeds.map(v=>v.id));
  return [null,{saved_ids:body.feeds.map(v=>v.id),capture_task_id:body.task_id}];
}};
eval(fn);
const run=()=>refreshFavoriteAuthor({username:'author',nickname:'Fixture'},'task',1,1,{completed:0,failed:0,videos:0});
'''


def replay(script):
    result = subprocess.run(['node', '-e', HARNESS + '\n(async()=>{\n' + script +
                             '\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('flags', [
    {'hasMore': False}, {'continueFlag': 0}, {'hasMore': False, 'continueFlag': 0},
    {'hasMore': 'false', 'continueFlag': '0'},
])
@pytest.mark.parametrize('last_ids', [[], ['b']])
def test_explicit_end_accepts_retained_cursor(flags, last_ids):
    replay(f"""
      pages=[response(['a'],'opaque-cursor'),response({json.dumps(last_ids)},'opaque-cursor',{json.dumps(flags)})];
      assert.equal(await run(), {1 + len(last_ids)});
      assert.equal(inputs.length,2);
    """)


def test_repeated_cursor_retries_same_page_and_preserves_unique_saved_items():
    replay("""
      pages=[response(['a'],'opaque-secret-a'),response(['b'],'opaque-secret-a'),
             response(['b'],'opaque-secret-b'),response(['c'],'')];
      assert.equal(await run(),3);
      assert.deepEqual(inputs,['','opaque-secret-a','opaque-secret-a','opaque-secret-b']);
      assert.deepEqual([...new Set(saved)],['a','b','c']);
      assert(logs.some(l=>l.action==='retry' && l.reason==='repeated_cursor'));
      assert(!JSON.stringify(logs).includes('opaque-secret'));
    """)


def test_empty_page_retry_does_not_skip_to_unverified_cursor():
    replay("""
      pages=[response(['a'],'first'),response([],'skip-me',{hasMore:true}),response(['b'],'')];
      assert.equal(await run(),2);
      assert.deepEqual(inputs,['','first','first']);
    """)


@pytest.mark.parametrize('bad_response, reason', [
    ("response(['b'],'first')", 'repeated_cursor'),
    ("response([],'next',{hasMore:true})", 'empty_page'),
    ("response(['b'],'',{hasMore:true})", 'missing_cursor'),
    ("response(['b'],'',{hasMore:false,continueFlag:1})", 'conflicting_flags'),
    ("({errCode:0,data:{}})", 'invalid_response'),
])
def test_persistent_pagination_anomaly_fails_with_bounded_diagnostics(bad_response, reason):
    replay(f"""
      pages=[response(['a'],'first'),{bad_response}];
      let error;
      try{{await run();}}catch(e){{error=e;}}
      assert(error);
      assert.equal(inputs.length,4);
      assert.deepEqual(inputs,['','first','first','first']);
      assert.equal(error.pagination.reason,{json.dumps(reason)});
      assert.equal(error.pagination.page_number,2);
      assert.equal(error.pagination.attempt,3);
      assert(error.captured_count>=1);
      assert.equal(logs.filter(l=>l.action==='retry').length,2);
      assert.equal(logs.at(-1).action,'fail');
    """)


def test_short_pages_keep_paging_and_nonvideo_page_is_not_empty():
    replay("""
      pages=[response(['a'],'first'),{errCode:0,data:{object:[{id:'text',objectDesc:{mediaType:2}}],lastBuffer:'second'}},response(['b'],'')];
      assert.equal(await run(),2);
      assert.deepEqual(inputs,['','first','second']);
    """)


def test_cancel_during_pagination_retry_stops_before_another_request():
    replay("""
      pages=[response([],'next',{hasMore:true})];
      onWait=()=>{cancelled=true;};
      await assert.rejects(run,/采集已停止/);
      assert.equal(inputs.length,1);
    """)


def test_remote_refresh_reports_partial_count_and_pagination_evidence():
    replay("""
      let running=false, circuitFails=0, AUTHOR_JITTER_MS=0;
      const finish=()=>{}, sleep=async()=>{};
      const remote=source.slice(source.indexOf('  async function runRemoteFavoritesRefresh'),source.indexOf('  var running = false;'));
      eval(remote);
      pages=[response(['a'],'first'),response(['b'],'first')];
      await runRemoteFavoritesRefresh({task_id:'task',authors:[{username:'author'}]});
      const last=reports.at(-1);
      assert.equal(last.failed_authors,1);
      assert.equal(last.completed_authors,0);
      assert.equal(last.total_videos,2);
      assert.equal(last.author_results.author.count,2);
      assert.equal(last.author_results.author.pagination_complete,false);
      assert.equal(last.author_results.author.pagination.page_number,2);
      assert.equal(last.author_results.author.pagination.attempt,3);
    """)


def test_storage_failure_is_not_retried_as_a_pagination_anomaly():
    replay("""
      pages=[response(['a'],'first'),response(['b'],'')];
      onWait=()=>{failSave=true;};
      let error;
      try{await run();}catch(e){error=e;}
      assert.match(error.message,/disk full/);
      assert.equal(error.captured_count,1);
      assert.equal(inputs.length,2);
      assert.deepEqual(saved,['a']);
    """)
