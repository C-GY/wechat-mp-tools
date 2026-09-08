/** Human-maintained account labels. Selection always uses platform + author ID. */
const ChannelsCompetitorAuthorTagsPage = {
    items: [], selected: new Set(), page: 1, pageSize: 50, loading: false, editor: null,
    esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    key(item) { return JSON.stringify([item.platform, item.author_id]); },
    platform(value) { return value === 'wechat_channels' ? '微信视频号' : value; },

    render() {
        return `<section id="author-tags-page" class="author-tags-page animate-fade-in">
            <div class="author-tags-heading">
                <div><h2 class="page-title">账号标签管理</h2>
                <p class="page-description">为竞对账号添加分类与关注标签，支持多个账号一起维护。</p></div>
                <div class="author-tags-actions"><a class="btn btn-secondary" href="#channels_competitor_monitor">返回竞对监测</a>
                <button class="btn btn-secondary" id="author-tags-refresh" onclick="ChannelsCompetitorAuthorTagsPage.load()">刷新账号</button></div>
            </div>
            <div id="author-tags-stats" class="author-tags-stats" aria-live="polite"></div>
            <div class="card author-tags-card">
                <div class="author-tags-filters">
                    <label class="author-tags-search">搜索账号<input id="author-tags-search" class="form-input" type="search" placeholder="输入账号名称或作者 ID" oninput="ChannelsCompetitorAuthorTagsPage.filterChanged()"></label>
                    <label>标签状态<select id="author-tags-state" class="form-input" onchange="ChannelsCompetitorAuthorTagsPage.filterChanged()"><option value="all">全部账号</option><option value="untagged">未打标签</option><option value="tagged">已打标签</option></select></label>
                    <label>标签筛选<select id="author-tags-filter" class="form-input" onchange="ChannelsCompetitorAuthorTagsPage.filterChanged()"><option value="">全部标签</option></select></label>
                </div>
                <div class="author-tags-selection">
                    <div><strong id="author-tags-selected">已选 0 个账号</strong>
                        <button class="btn btn-sm btn-secondary" id="author-tags-select-filtered" onclick="ChannelsCompetitorAuthorTagsPage.selectFiltered()">选择全部筛选结果</button>
                        <button class="btn btn-sm btn-secondary" onclick="ChannelsCompetitorAuthorTagsPage.clearSelection()">清空勾选</button></div>
                    <div class="author-tags-actions">
                        <button class="btn btn-secondary" id="author-tags-bulk-remove" disabled onclick="ChannelsCompetitorAuthorTagsPage.openEditor('remove')">批量移除标签</button>
                        <button class="btn btn-primary" id="author-tags-bulk-add" disabled onclick="ChannelsCompetitorAuthorTagsPage.openEditor('add')">批量添加标签</button>
                    </div>
                </div>
                <p class="author-tags-hint">更改筛选或刷新会清空勾选；每次最多选择 500 个账号。添加标签时保留原有标签。</p>
                <div id="author-tags-error" class="author-tags-error" role="alert" hidden></div>
                <div class="author-tags-table-wrap">
                    <table class="author-tags-table"><thead><tr>
                        <th class="author-tags-check"><input id="author-tags-select-page" type="checkbox" aria-label="选择本页所有账号" onchange="ChannelsCompetitorAuthorTagsPage.selectPage(this.checked)"></th>
                        <th>账号</th><th>作品数</th><th>人工标签</th><th>操作</th>
                    </tr></thead><tbody id="author-tags-rows"></tbody></table>
                </div>
                <div class="author-tags-pagination"><span id="author-tags-result-count"></span><div class="author-tags-actions">
                    <button class="btn btn-sm btn-secondary" id="author-tags-prev" onclick="ChannelsCompetitorAuthorTagsPage.changePage(-1)">上一页</button>
                    <span id="author-tags-page-count"></span><button class="btn btn-sm btn-secondary" id="author-tags-next" onclick="ChannelsCompetitorAuthorTagsPage.changePage(1)">下一页</button>
                </div></div>
            </div>
        </section>`;
    },

    async init() { this.items = []; this.selected = new Set(); this.page = 1; await this.load(); },
    onShow() { if (!this.editor) this.load(); },
    allTags() { return [...new Set(this.items.flatMap(a => a.tags))].sort((a, b) => a.localeCompare(b, 'zh-CN')); },
    filtered() {
        const search = document.getElementById('author-tags-search').value.trim().toLocaleLowerCase();
        const state = document.getElementById('author-tags-state').value;
        const tag = document.getElementById('author-tags-filter').value;
        return this.items.filter(a => (!search || `${a.author_name} ${a.author_id}`.toLocaleLowerCase().includes(search))
            && (state === 'all' || (state === 'tagged' ? a.tags.length > 0 : a.tags.length === 0))
            && (!tag || a.tags.includes(tag)));
    },
    async load() {
        if (this.loading) return;
        this.loading = true; this.selected.clear(); this.draw();
        const error = document.getElementById('author-tags-error');
        error.hidden = true;
        try {
            const result = await API.competitor_monitor.getAuthors();
            this.items = result.items;
            const known = new Set(this.items.map(a => this.key(a)));
            this.selected = new Set([...this.selected].filter(key => known.has(key)));
            const filter = document.getElementById('author-tags-filter');
            const previous = filter.value;
            filter.innerHTML = '<option value="">全部标签</option>' + this.allTags().map(tag => `<option value="${this.esc(tag)}">${this.esc(tag)}</option>`).join('');
            filter.value = this.allTags().includes(previous) ? previous : '';
        } catch (err) {
            error.textContent = `读取失败：${err.message}。请检查竞对监测配置后点击“刷新账号”。`;
            error.hidden = false;
            this.items = []; this.selected.clear();
        } finally { this.loading = false; this.draw(); }
    },
    draw() {
        if (!document.getElementById('author-tags-rows')) return;
        const filtered = this.filtered();
        const pages = Math.max(1, Math.ceil(filtered.length / this.pageSize));
        this.page = Math.min(this.page, pages);
        const visible = filtered.slice((this.page - 1) * this.pageSize, this.page * this.pageSize);
        const tagged = this.items.filter(a => a.tags.length).length;
        document.getElementById('author-tags-stats').innerHTML = this.loading ? '正在读取账号…' :
            `<span>全部账号 <strong>${this.items.length}</strong></span><span>已打标签 <strong>${tagged}</strong></span><span>未打标签 <strong>${this.items.length - tagged}</strong></span>`;
        document.getElementById('author-tags-rows').innerHTML = this.loading ? '<tr><td colspan="5" class="author-tags-empty">正在加载账号…</td></tr>' : visible.length ? visible.map(a => {
            const index = this.items.indexOf(a);
            return `<tr class="${this.selected.has(this.key(a)) ? 'is-selected' : ''}">
                <td><input type="checkbox" aria-label="选择 ${this.esc(a.author_name)}" ${this.selected.has(this.key(a)) ? 'checked' : ''} onchange="ChannelsCompetitorAuthorTagsPage.toggle(${index},this.checked)"></td>
                <td><strong class="author-tags-name">${this.esc(a.author_name)}</strong><span class="author-tags-platform">${this.esc(this.platform(a.platform))}</span>
                    <span class="author-tags-id" title="${this.esc(a.author_id)}">${this.esc(a.author_id)}</span></td>
                <td>${a.video_count}</td>
                <td><div class="author-tags-chips">${a.tags.length ? a.tags.map(t => `<span class="author-tags-chip">${this.esc(t)}</span>`).join('') : '<span class="author-tags-hint">未添加标签</span>'}</div></td>
                <td><div class="author-tags-row-actions"><button class="btn btn-sm btn-secondary" onclick="ChannelsCompetitorAuthorTagsPage.openEditor('add',${index})">添加标签</button>
                    <button class="btn btn-sm btn-secondary" ${a.tags.length ? '' : 'disabled'} onclick="ChannelsCompetitorAuthorTagsPage.openEditor('remove',${index})">移除标签</button></div></td></tr>`;
        }).join('') : `<tr><td colspan="5" class="author-tags-empty">${this.items.length ? '没有符合筛选条件的账号。' : '暂无账号，请先在竞对监测系统中同步作品。'}</td></tr>`;
        document.getElementById('author-tags-selected').textContent = `已选 ${this.selected.size} 个账号`;
        for (const id of ['author-tags-bulk-add', 'author-tags-bulk-remove']) document.getElementById(id).disabled = this.loading || !this.selected.size;
        document.getElementById('author-tags-refresh').disabled = this.loading;
        document.getElementById('author-tags-select-filtered').disabled = this.loading || !filtered.length;
        const selectPage = document.getElementById('author-tags-select-page');
        const count = visible.filter(a => this.selected.has(this.key(a))).length;
        selectPage.checked = visible.length > 0 && count === visible.length;
        selectPage.indeterminate = count > 0 && count < visible.length;
        selectPage.disabled = this.loading || !visible.length;
        document.getElementById('author-tags-result-count').textContent = `筛选结果 ${filtered.length} 个账号`;
        document.getElementById('author-tags-page-count').textContent = `${this.page} / ${pages}`;
        document.getElementById('author-tags-prev').disabled = this.loading || this.page <= 1;
        document.getElementById('author-tags-next').disabled = this.loading || this.page >= pages;
    },
    filterChanged() { this.page = 1; this.selected.clear(); this.draw(); },
    clearSelection() { this.selected.clear(); this.draw(); },
    toggle(index, checked) {
        const key = this.key(this.items[index]);
        if (checked && this.selected.size >= 500) { Toast.warning('每次最多选择 500 个账号'); this.draw(); return; }
        if (checked) this.selected.add(key); else this.selected.delete(key);
        this.draw();
    },
    selectPage(checked) {
        const visible = this.filtered().slice((this.page - 1) * this.pageSize, this.page * this.pageSize);
        const next = new Set(this.selected);
        visible.forEach(a => checked ? next.add(this.key(a)) : next.delete(this.key(a)));
        if (next.size > 500) { Toast.warning('每次最多选择 500 个账号'); this.draw(); return; }
        this.selected = next; this.draw();
    },
    selectFiltered() {
        const items = this.filtered();
        if (items.length > 500) { Toast.warning('筛选结果超过 500 个账号，请缩小筛选范围'); return; }
        this.selected = new Set(items.map(a => this.key(a))); this.draw();
    },
    changePage(delta) { this.page += delta; this.draw(); },

    openEditor(operation, index = null) {
        if (this.loading || this.editor) return;
        const accounts = index === null ? this.items.filter(a => this.selected.has(this.key(a))) : [this.items[index]];
        if (!accounts.length) return;
        const removing = operation === 'remove';
        const available = removing ? [...new Set(accounts.flatMap(a => a.tags))] : this.allTags();
        this.editor = {operation, accounts, available, chosen: new Set(), saving: false};
        Modal.open({
            title: `${accounts.length > 1 ? '批量' : ''}${removing ? '移除' : '添加'}账号标签`,
            content: `<div class="author-tags-editor">
                <p>将为 <strong>${accounts.length}</strong> 个账号${removing ? '移除指定' : '添加'}标签。</p>
                <div class="author-tags-targets">${accounts.slice(0, 8).map(a => this.esc(a.author_name)).join('、')}${accounts.length > 8 ? ` 等 ${accounts.length} 个账号` : ''}</div>
                <p class="author-tags-hint">${removing ? '只移除指定标签，其余标签保留。' : '原有标签保留，重复标签会自动跳过。'}</p>
                <label for="author-tags-input">${removing ? '要移除的标签' : '新标签'}</label>
                <textarea id="author-tags-input" class="form-input" rows="3" placeholder="例如：重点关注，护肤，美妆\n多个标签用逗号、分号或换行分隔"></textarea>
                <p class="author-tags-hint">每次最多 20 个标签，每个标签最多 128 个字符。</p>
                <p>${removing ? '从所选账号的已有标签中选择' : '复用已有标签'}</p>
                <div class="author-tags-choices">${available.length ? available.map((tag, i) => `<button class="author-tags-chip author-tags-choice" aria-pressed="false" data-tag-index="${i}" onclick="ChannelsCompetitorAuthorTagsPage.chooseTag(${i},this)">${this.esc(tag)}</button>`).join('') : '<span class="author-tags-hint">暂无已有标签</span>'}</div>
                <div id="author-tags-save-error" class="author-tags-error" role="alert" hidden></div>
            </div>`,
            footer: `<button id="author-tags-cancel" class="btn btn-secondary" onclick="Modal.close()">取消</button>
                <button id="author-tags-save" class="btn btn-primary" onclick="ChannelsCompetitorAuthorTagsPage.save()">确认${removing ? '移除' : '添加'}</button>`,
            onClose: () => { this.editor = null; },
            onOpen: () => document.getElementById('author-tags-input').focus(),
        });
    },
    chooseTag(index, button) {
        if (!this.editor || this.editor.saving) return;
        const tag = this.editor.available[index];
        if (this.editor.chosen.has(tag)) this.editor.chosen.delete(tag); else this.editor.chosen.add(tag);
        button.setAttribute('aria-pressed', String(this.editor.chosen.has(tag)));
    },
    async save() {
        const editor = this.editor;
        if (!editor || editor.saving) return;
        const error = document.getElementById('author-tags-save-error');
        error.hidden = true;
        const tags = [...new Set([...editor.chosen, ...document.getElementById('author-tags-input').value.split(/[,，;；\n\r]+/).map(t => t.trim()).filter(Boolean)])];
        if (!tags.length || tags.length > 20 || tags.some(t => Array.from(t).length > 128)) {
            error.textContent = '请输入或选择 1 至 20 个标签，每个标签最多 128 个字符。'; error.hidden = false; return;
        }
        editor.saving = true;
        Modal.preventClose = true;
        Modal.dialog.querySelectorAll('button,textarea').forEach(el => el.disabled = true);
        const save = document.getElementById('author-tags-save');
        save.textContent = '正在保存…';
        try {
            const result = await API.competitor_monitor.changeAuthorTags({operation: editor.operation, tags,
                accounts: editor.accounts.map(a => ({platform: a.platform, author_id: a.author_id}))});
            Modal.preventClose = false;
            Modal.close();
            Toast.success(`${result.accounts} 个账号处理完成，${editor.operation === 'add' ? '新增' : '移除'} ${result.changed} 条标签关联`);
            await this.load();
        } catch (err) {
            error.textContent = err.message; error.hidden = false;
        } finally {
            editor.saving = false; Modal.preventClose = false;
            if (this.editor === editor) {
                Modal.dialog.querySelectorAll('button,textarea').forEach(el => el.disabled = false);
                save.textContent = `确认${editor.operation === 'add' ? '添加' : '移除'}`;
            }
        }
    },
};
