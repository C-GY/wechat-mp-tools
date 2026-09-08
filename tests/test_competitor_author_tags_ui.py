"""Real browser -> Flask endpoints -> MySQL, with all fixture changes rolled back."""

import os
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import pymysql
import pytest
from flask import Flask
from playwright.sync_api import sync_playwright

from backend import competitor_monitor as monitor
from backend.competitor_author_tags import CompetitorAuthorTagStore
from test_competitor_monitor_mysql import database, make_row  # Shared rollback-only fixture.


pytestmark = pytest.mark.skipif(os.environ.get('COMPETITOR_MYSQL_INTEGRATION') != '1', reason='opt-in real database browser flow')
ROOT = Path(__file__).resolve().parents[1]


def test_account_selection_bulk_labels_errors_and_reload(database):
    adapter, _ = database
    prefix = 'ui-tags-' + uuid.uuid4().hex
    identities = [{'platform': 'wechat_channels', 'author_id': prefix + str(i)} for i in range(3)]
    for i, account in enumerate(identities):
        for n in range(2):
            row = make_row(account['author_id'] + '-' + str(n), 'ui-tags', datetime(2026, 9, 8, 12, n))
            row.update(author_id=account['author_id'], author_name=f'测试账号{i}' if i else '<img src=x onerror="window.__injected=true">')
            adapter.write_snapshots([row])
    store = CompetitorAuthorTagStore(adapter.connect)
    store.change_tags({'operation': 'add', 'accounts': identities[:1], 'tags': ['原有标签']})
    app = Flask(__name__)
    app.register_blueprint(monitor.competitor_monitor_bp)
    client = app.test_client()
    posts, errors = [], []

    with patch.object(monitor, '_author_tag_store', return_value=store), sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1360, 'height': 980})
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                req = route.request
                path = urlsplit(req.url).path
                if path == '/':
                    route.fulfill(content_type='text/html', body='<body class="wechat-theme"><main id="app" style="padding:24px"></main><div class="modal-overlay" id="modal-overlay"><div class="modal-dialog" id="modal-dialog"></div></div></body>')
                else:
                    if req.method == 'POST':
                        posts.append(req.post_data_json)
                    response = client.open(path, method=req.method, data=req.post_data, content_type='application/json')
                    route.fulfill(status=response.status_code, content_type='application/json', body=response.data)

            page.route('https://author-tags.test/**', handle)
            page.goto('https://author-tags.test/')
            page.add_style_tag(path=str(ROOT / 'frontend/css/style.css'))
            for file in ['utils/modal.js', 'api.js', 'components/channels_competitor_author_tags.js']:
                page.add_script_tag(path=str(ROOT / 'frontend/js' / file))
            page.evaluate('''async () => {
                window.__messages = [];
                window.Toast = Object.fromEntries(['success','warning','error'].map(type => [type, message => window.__messages.push(message)]));
                Modal.init();
                const page = ChannelsCompetitorAuthorTagsPage;
                page.pageSize = 2;
                document.getElementById('app').innerHTML = page.render();
                await page.init();
            }''')
            page.locator('#author-tags-search').fill(prefix)
            assert page.locator('#author-tags-rows tr').count() == 2
            assert page.locator('#author-tags-rows img').count() == 0
            assert page.evaluate('window.__injected') is None
            page.locator('#author-tags-select-page').check()
            page.locator('#author-tags-next').click()
            page.locator('#author-tags-rows input[type=checkbox]').check()
            assert page.locator('#author-tags-selected').inner_text() == '已选 3 个账号'
            page.locator('#author-tags-bulk-add').click()
            page.locator('#author-tags-save').click()
            assert page.locator('#author-tags-save-error').is_visible()
            assert not posts
            page.locator('#author-tags-input').fill('浏览器重点，浏览器护肤\n浏览器重点')
            page.locator('#author-tags-save').click()
            page.wait_for_function("!Modal.overlay.classList.contains('active') && !ChannelsCompetitorAuthorTagsPage.loading")
            assert len(posts) == 1
            assert set(a['author_id'] for a in posts[0]['accounts']) == {a['author_id'] for a in identities}
            assert len(posts[0]['tags']) == 2
            assert page.locator('#author-tags-selected').inner_text() == '已选 0 个账号'
            rows = {a['author_id']: a for a in store.list_accounts() if a['author_id'].startswith(prefix)}
            assert set(rows[identities[0]['author_id']]['tags']) == {'原有标签', '浏览器重点', '浏览器护肤'}
            assert set(rows[identities[1]['author_id']]['tags']) == {'浏览器重点', '浏览器护肤'}
            assert all(a['video_count'] == 2 for a in rows.values())

            # Pick one account only; other accounts keep the same tag.
            page.locator('#author-tags-search').fill(identities[0]['author_id'])
            page.get_by_role('button', name='移除标签', exact=True).click()
            page.get_by_role('button', name='浏览器重点', exact=True).click()
            page.locator('#author-tags-save').click()
            page.wait_for_function("!Modal.overlay.classList.contains('active') && !ChannelsCompetitorAuthorTagsPage.loading")
            rows = {a['author_id']: a for a in store.list_accounts() if a['author_id'].startswith(prefix)}
            assert '浏览器重点' not in rows[identities[0]['author_id']]['tags']
            assert '浏览器重点' in rows[identities[1]['author_id']]['tags']

            # A database error keeps the editor and input available for a retry.
            page.get_by_role('button', name='添加标签', exact=True).click()
            page.locator('#author-tags-input').fill('重试标签')
            with patch.object(store, 'change_tags', side_effect=pymysql.OperationalError(2003, 'fixture unavailable')):
                page.locator('#author-tags-save').click()
                page.wait_for_function("!document.getElementById('author-tags-save-error').hidden")
                assert page.locator('#author-tags-input').input_value() == '重试标签'
                assert page.locator('#author-tags-save').is_enabled()
            page.locator('#author-tags-save').click()
            page.wait_for_function("!Modal.overlay.classList.contains('active') && !ChannelsCompetitorAuthorTagsPage.loading")

            # Filter-wide selection spans pages; changing filters drops the selection.
            page.locator('#author-tags-search').fill(prefix)
            page.locator('#author-tags-select-filtered').click()
            assert page.locator('#author-tags-selected').inner_text() == '已选 3 个账号'
            page.locator('#author-tags-state').select_option('untagged')
            assert page.locator('#author-tags-selected').inner_text() == '已选 0 个账号'
            assert page.locator('#author-tags-bulk-add').is_disabled()
            assert '没有符合筛选条件' in page.locator('#author-tags-rows').inner_text()
            page.locator('#author-tags-state').select_option('all')
            page.locator('#author-tags-filter').select_option('浏览器重点')
            assert page.locator('#author-tags-result-count').inner_text() == '筛选结果 2 个账号'
            page.locator('#author-tags-select-filtered').click()
            page.locator('#author-tags-bulk-remove').click()
            page.get_by_role('button', name='浏览器重点', exact=True).click()
            page.locator('#author-tags-save').click()
            page.wait_for_function("!Modal.overlay.classList.contains('active') && !ChannelsCompetitorAuthorTagsPage.loading")
            assert all('浏览器重点' not in a['tags'] for a in store.list_accounts() if a['author_id'].startswith(prefix))

            with patch.object(store, 'list_accounts', side_effect=pymysql.OperationalError(2003, 'fixture unavailable')):
                page.locator('#author-tags-refresh').click()
                page.wait_for_function("!ChannelsCompetitorAuthorTagsPage.loading")
                assert page.locator('#author-tags-error').is_visible()
                assert page.locator('#author-tags-bulk-add').is_disabled()
            page.locator('#author-tags-refresh').click()
            page.wait_for_function("!ChannelsCompetitorAuthorTagsPage.loading")
            assert page.locator('#author-tags-error').is_hidden()
            assert not errors
        finally:
            browser.close()
