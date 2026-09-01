/** Unified OSS upload progress page. */
const ChannelsOSSProgressPage = {
    pollTimer: null,

    render() {
        return `
            <div class="page-header animate-fade-in">
                <h2 class="page-title">OSS 上传进度</h2>
                <p class="page-description">所有微信视频号 OSS 上传任务都会集中显示在这里。</p>
            </div>
            <div class="oss-stats-row animate-fade-in">
                ${this.statCard('任务总数', 'oss-stat-total')}
                ${this.statCard('等待中', 'oss-stat-pending')}
                ${this.statCard('下载中', 'oss-stat-downloading')}
                ${this.statCard('上传中', 'oss-stat-uploading')}
                ${this.statCard('已完成', 'oss-stat-completed')}
                ${this.statCard('失败', 'oss-stat-failed')}
            </div>
            <div class="card animate-fade-in" style="margin-top: var(--spacing-lg);">
                <div class="card-header" style="display:flex; justify-content:space-between; align-items:center; gap:12px; border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <div>
                        <h3 class="card-title" style="margin:0;">☁️ 上传任务</h3>
                        <div id="oss-queue-running" style="font-size:0.8rem; color:var(--text-muted); margin-top:5px;">正在读取...</div>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button class="btn btn-secondary btn-sm" onclick="ChannelsOSSProgressPage.load()">🔄 刷新</button>
                        <button class="btn btn-secondary btn-sm" onclick="ChannelsOSSProgressPage.clearFinished()">清除已结束记录</button>
                    </div>
                </div>
                <div style="overflow:auto;">
                    <table class="data-table" style="min-width:1050px; width:100%;">
                        <thead><tr>
                            <th>创作者 / 作品</th><th style="width:120px;">状态</th>
                            <th style="width:240px;">进度</th><th>OSS 视频链接</th>
                            <th>错误信息</th><th style="width:155px;">更新时间</th>
                        </tr></thead>
                        <tbody id="oss-upload-table-body">
                            <tr><td colspan="6" style="text-align:center; padding:36px; color:var(--text-muted);">正在读取上传记录...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    },

    statCard(label, id) {
        return `<div class="stat-card"><div class="stat-content"><div class="stat-label">${label}</div><div class="stat-value" id="${id}">0</div></div></div>`;
    },

    async init() { await this.load(); },
    async onShow() { await this.load(); },
    destroy() { if (this.pollTimer) clearTimeout(this.pollTimer); this.pollTimer = null; },

    async load() {
        try {
            const result = await API.oss.getUploads();
            this.renderStats(result.stats || {});
            this.renderRows(result.items || []);
            const running = document.getElementById('oss-queue-running');
            if (running) running.textContent = result.running
                ? '同步任务运行中，页面每秒自动刷新'
                : '当前没有正在运行的同步任务';
            if (this.pollTimer) clearTimeout(this.pollTimer);
            this.pollTimer = setTimeout(() => this.load(), result.running ? 1000 : 5000);
        } catch (error) {
            const body = document.getElementById('oss-upload-table-body');
            if (body) body.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--error); padding:32px;">读取失败：${this.esc(error.message || error)}</td></tr>`;
        }
    },

    renderStats(stats) {
        ['total', 'pending', 'downloading', 'uploading', 'completed', 'failed'].forEach(key => {
            const element = document.getElementById(`oss-stat-${key}`);
            if (element) element.textContent = Number(stats[key] || 0);
        });
    },

    renderRows(items) {
        const body = document.getElementById('oss-upload-table-body');
        if (!body) return;
        if (!items.length) {
            body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:40px; color:var(--text-muted);">暂无 OSS 上传记录</td></tr>';
            return;
        }
        body.innerHTML = items.map(item => {
            const state = this.status(item.status);
            const percent = Math.max(0, Math.min(100, Number(item.progress || 0)));
            const size = item.total_bytes
                ? `${this.bytes(item.uploaded_bytes)} / ${this.bytes(item.total_bytes)}`
                : `${percent.toFixed(0)}%`;
            const link = item.oss_url
                ? `<a href="${this.attr(item.oss_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--primary);">打开 OSS 视频</a>`
                : '<span style="color:var(--text-muted);">—</span>';
            return `<tr>
                <td><div style="font-weight:600;">${this.esc(item.author || '未知创作者')}</div><div style="font-size:0.8rem; color:var(--text-muted); margin-top:4px; max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${this.attr(item.title || '')}">${this.esc(item.title || item.video_id || '')}</div></td>
                <td><span class="badge" style="color:${state.color};">${state.text}</span></td>
                <td><div style="height:7px; background:rgba(0,0,0,0.08); border-radius:999px; overflow:hidden;"><div style="height:100%; width:${percent}%; background:${state.color}; transition:width .2s;"></div></div><div style="font-size:.76rem; color:var(--text-muted); margin-top:5px;">${size}</div></td>
                <td>${link}</td><td style="max-width:260px; color:var(--error); font-size:.8rem;">${this.esc(item.error || '')}</td>
                <td style="font-size:.8rem; color:var(--text-muted);">${this.esc(item.updated_at || '')}</td>
            </tr>`;
        }).join('');
    },

    status(status) {
        return ({
            pending: { text: '等待中', color: 'var(--text-muted)' },
            downloading: { text: '下载中', color: 'var(--warning)' },
            uploading: { text: '上传中', color: 'var(--primary)' },
            completed: { text: '已完成', color: 'var(--success)' },
            skipped: { text: '已同步', color: 'var(--success)' },
            failed: { text: '失败', color: 'var(--error)' },
        })[status] || { text: status || '未知', color: 'var(--text-muted)' };
    },

    async clearFinished() {
        try { await API.oss.clearFinishedUploads(); await this.load(); }
        catch (error) { Toast.error(`清除失败：${error.message || error}`); }
    },

    bytes(value) {
        const number = Number(value || 0);
        if (number < 1024) return `${number} B`;
        if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
        return `${(number / 1024 / 1024).toFixed(1)} MB`;
    },
    esc(value) { const div = document.createElement('div'); div.textContent = value == null ? '' : String(value); return div.innerHTML; },
    attr(value) { return this.esc(value).replace(/`/g, '&#96;'); },
};
