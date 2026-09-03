/** Creative Radar API configuration with the same hub workflow and dashboard. */
const ChannelsCreativeRadarPage = {
    pollTimer: null,
    scheduleTimes: [],

    render() {
        return `
            <div class="page-header animate-fade-in" style="display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap;">
                <div>
                    <h2 class="page-title">创意雷达系统</h2>
                    <p class="page-description">按创作者逐个刷新作品、补齐 OSS 链接；每轮通过 API 分批提交全部已准备好的作品，由接口处理重复数据。</p>
                </div>
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    <button class="btn btn-secondary" id="btn-creative-radar-export-config" onclick="ChannelsCreativeRadarPage.exportConfig()" title="复制已保存的完整配置，包含 API Key 和飞书密钥">📋 导出配置</button>
                    <button class="btn btn-secondary" id="btn-creative-radar-import-config" onclick="ChannelsCreativeRadarPage.importConfig()">📥 导入配置</button>
                    <button class="btn btn-primary" id="btn-creative-radar-run" onclick="ChannelsCreativeRadarPage.startRun()">▶ 立即执行</button>
                    <button class="btn btn-secondary" id="btn-creative-radar-pause" data-action="pause" onclick="ChannelsCreativeRadarPage.togglePause()" style="display:none;">⏸ 暂停</button>
                </div>
            </div>

            <div id="creative-radar-summary" class="card animate-fade-in" style="margin-top:var(--spacing-lg); padding:18px;">
                <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:center;">
                    <div>
                        <div style="font-size:.78rem; color:var(--text-muted);">当前状态</div>
                        <div id="creative-radar-run-title" style="font-size:1.05rem; font-weight:700; margin-top:4px;">正在读取...</div>
                    </div>
                    <div id="creative-radar-next-run" style="font-size:.82rem; color:var(--text-muted);">下次执行：—</div>
                </div>
                <div style="height:8px; border-radius:999px; overflow:hidden; background:rgba(0,0,0,.08); margin-top:14px;">
                    <div id="creative-radar-main-progress" style="height:100%; width:0; background:var(--primary); transition:width .25s;"></div>
                </div>
                <div id="creative-radar-run-message" style="font-size:.84rem; color:var(--text-secondary); margin-top:10px;">—</div>
                <div id="creative-radar-current-creator" style="display:none; align-items:center; gap:10px; margin-top:12px; padding:10px 12px; border-radius:9px; background:rgba(37,99,235,.08); color:var(--text-secondary);">
                    <span id="creative-radar-current-creator-position" style="font-size:.78rem; white-space:nowrap;">当前创作者</span>
                    <strong id="creative-radar-current-creator-name" style="color:var(--text-primary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">—</strong>
                </div>
                <div class="pinchuang-stats-row">
                    ${this.statCard('当前创作者', 'creative-radar-stat-creators', 'creative-radar-stat-creator-name')}
                    ${this.statCard('刷新作品', 'creative-radar-stat-refreshed')}
                    ${this.statCard('已有 OSS', 'creative-radar-stat-existing')}
                    ${this.statCard('待传 OSS', 'creative-radar-stat-new')}
                    ${this.statCard('OSS 完成', 'creative-radar-stat-uploaded')}
                    ${this.statCard('API 受理', 'creative-radar-stat-written')}
                    ${this.statCard('失败/待确认', 'creative-radar-stat-failed')}
                </div>
                <div style="font-size:.78rem; color:var(--text-muted); margin-top:8px;">每轮全量分批提交；API 受理按批次统计，不代表逐条写入结果。超时或无法确认的批次记为待确认。</div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">⚙️ API 同步配置</h3>
                    <div style="font-size:.8rem; color:var(--text-muted); margin-top:5px;">每批最多 200 条；同步超时默认 600 秒（10 分钟），可按需调整。配置保存在当前电脑用户目录，重装后不会丢失。</div>
                </div>
                <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px;">
                    ${this.input('API 地址', 'creative-radar-api-endpoint', 'url', 'http://pinguan-central-platform.fandow.com/api/external/upload')}
                    ${this.input('API Key', 'creative-radar-api-key', 'password', '请输入 API Key')}
                    <div class="form-group" style="margin:0;">
                        <label class="form-label" for="creative-radar-api-timeout">同步超时（秒）</label>
                        <input id="creative-radar-api-timeout" class="form-input" type="number" min="1" max="600" step="1" value="600">
                        <div style="font-size:.76rem; color:var(--text-muted); margin-top:5px;">每批等待响应的时间，范围 1–600 秒。</div>
                    </div>
                </div>
                <div style="display:flex; gap:8px; margin-top:16px; flex-wrap:wrap;">
                    <button class="btn btn-primary" id="btn-creative-radar-save" onclick="ChannelsCreativeRadarPage.saveConfig()">保存全部配置</button>
                    <button class="btn btn-secondary" id="btn-creative-radar-test-api" onclick="ChannelsCreativeRadarPage.testApi()">测试 API 连接</button>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">🕐 每日定时触发</h3>
                    <div style="font-size:.8rem; color:var(--text-muted); margin-top:5px;">北京时间；软件关闭时不会触发。若上一轮仍在运行，本轮将跳过并发送飞书通知。</div>
                </div>
                <label style="display:flex; align-items:center; gap:9px; cursor:pointer; font-weight:600;">
                    <input id="creative-radar-schedule-enabled" type="checkbox"> 启用定时同步
                </label>
                <div style="display:flex; gap:8px; align-items:flex-end; margin-top:14px; flex-wrap:wrap;">
                    <div class="form-group" style="margin:0;">
                        <label class="form-label" for="creative-radar-new-time">新增触发时间</label>
                        <input id="creative-radar-new-time" class="form-input" type="time" value="09:00" style="width:170px;">
                    </div>
                    <button class="btn btn-secondary" onclick="ChannelsCreativeRadarPage.addTime()">＋ 添加时间</button>
                    <div class="form-group" style="margin:0; min-width:220px;">
                        <label class="form-label" for="creative-radar-creator-interval">创作者之间的间隔（秒）</label>
                        <input id="creative-radar-creator-interval" class="form-input" type="number" min="0" max="86400" value="10">
                    </div>
                </div>
                <div id="creative-radar-time-list" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:14px;"></div>
                <div style="display:flex; gap:10px; align-items:center; margin-top:16px; flex-wrap:wrap;">
                    <button class="btn btn-primary" id="btn-creative-radar-save-schedule" onclick="ChannelsCreativeRadarPage.saveConfig()">保存定时配置</button>
                    <span style="font-size:.8rem; color:var(--text-muted);">添加、删除时间或修改间隔后，请点击保存。</span>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">🔔 飞书机器人</h3>
                    <div style="font-size:.8rem; color:var(--text-muted); margin-top:5px;">创作者失败、任务异常或计划任务未能启动时发送通知；发送失败自动重试 3 次。</div>
                </div>
                ${this.input('Webhook 地址', 'creative-radar-feishu-webhook', 'url', 'https://open.feishu.cn/open-apis/bot/v2/hook/...')}
                <div style="margin-top:14px;">${this.input('签名密钥（可选）', 'creative-radar-feishu-secret', 'password', '机器人未开启签名校验可留空')}</div>
                <div style="display:flex; gap:8px; margin-top:16px; flex-wrap:wrap;">
                    <button class="btn btn-secondary" id="btn-creative-radar-test-feishu" onclick="ChannelsCreativeRadarPage.testFeishu()">发送测试消息</button>
                    <button class="btn btn-secondary" onclick="Router.navigate('channels_oss_config')">打开 OSS 配置</button>
                    <span id="creative-radar-oss-state" style="font-size:.82rem; color:var(--text-muted); align-self:center;">OSS 状态：读取中</span>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="display:flex; justify-content:space-between; align-items:center; gap:10px; border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">📍 创作者执行明细</h3>
                    <button class="btn btn-secondary btn-sm" onclick="ChannelsCreativeRadarPage.loadStatus()">刷新</button>
                </div>
                <div style="overflow:auto;">
                    <table class="data-table" style="min-width:1100px; width:100%;">
                        <thead><tr><th>创作者</th><th>状态</th><th>刷新</th><th>已有 OSS</th><th>待传 OSS</th><th>OSS 完成</th><th title="按接口批次受理结果统计">API 受理</th><th>失败/待确认</th><th>结果</th></tr></thead>
                        <tbody id="creative-radar-creator-rows"><tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-muted);">暂无执行记录</td></tr></tbody>
                    </table>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg); margin-bottom:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">🧾 最近运行记录</h3>
                </div>
                <div id="creative-radar-history" style="display:grid; gap:9px;"><div style="color:var(--text-muted);">暂无运行记录</div></div>
            </div>
        `;
    },

    statCard(label, id, detailId = '') {
        const detail = detailId
            ? `<div id="${detailId}" style="font-size:.76rem; color:var(--text-muted); margin-top:3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="">—</div>`
            : '';
        return `<div class="stat-card"><div class="stat-content" style="min-width:0;"><div class="stat-label">${label}</div><div class="stat-value" id="${id}" style="font-size:1.25rem;">0</div>${detail}</div></div>`;
    },

    input(label, id, type, placeholder) {
        return `<div class="form-group" style="margin:0;"><label class="form-label" for="${id}">${label}</label><input id="${id}" class="form-input" type="${type}" autocomplete="off" spellcheck="false" placeholder="${placeholder}"></div>`;
    },

    async init() {
        await this.loadConfig();
        await this.loadStatus();
    },
    async onShow() {
        await this.loadConfig();
        await this.loadStatus();
    },
    destroy() {
        if (this.pollTimer) clearTimeout(this.pollTimer);
        this.pollTimer = null;
    },

    async loadConfig() {
        try {
            const config = await API.creativeRadar.getConfig();
            const api = config.api || {};
            const schedule = config.schedule || {};
            const feishu = config.feishu || {};
            this.setValue('creative-radar-api-endpoint', api.endpoint || '');
            this.setSecretPlaceholder('creative-radar-api-key', api.has_api_key, ' API Key');
            this.setValue('creative-radar-api-timeout', api.timeout_seconds ?? 600);
            this.setValue('creative-radar-creator-interval', schedule.creator_interval_seconds ?? 10);
            const enabled = document.getElementById('creative-radar-schedule-enabled');
            if (enabled) enabled.checked = !!schedule.enabled;
            this.scheduleTimes = Array.isArray(schedule.times) ? [...schedule.times] : [];
            this.renderTimes();
            this.setSecretPlaceholder('creative-radar-feishu-webhook', feishu.has_webhook, '飞书机器人 Webhook');
            this.setSecretPlaceholder('creative-radar-feishu-secret', feishu.has_secret, '飞书签名密钥');
            const oss = document.getElementById('creative-radar-oss-state');
            if (oss) {
                oss.textContent = config.oss_configured ? 'OSS 状态：✅ 已配置' : 'OSS 状态：⚠️ 未配置';
                oss.style.color = config.oss_configured ? 'var(--success)' : 'var(--warning)';
            }
        } catch (error) {
            Toast.error(`读取创意雷达配置失败：${error.message || error}`);
        }
    },

    exportConfig() {
        return HubConfigTransfer.exportConfig({
            api: API.creativeRadar, label: '创意雷达', buttonId: 'btn-creative-radar-export-config',
        });
    },

    importConfig() {
        HubConfigTransfer.importConfig({
            api: API.creativeRadar, label: '创意雷达',
            onImported: async () => { await this.loadConfig(); await this.loadStatus(); },
        });
    },

    collectConfig() {
        return {
            api: {
                endpoint: this.value('creative-radar-api-endpoint'),
                api_key: this.value('creative-radar-api-key'),
                timeout_seconds: Number(this.value('creative-radar-api-timeout')),
            },
            schedule: {
                enabled: !!document.getElementById('creative-radar-schedule-enabled')?.checked,
                times: [...this.scheduleTimes],
                creator_interval_seconds: Number(this.value('creative-radar-creator-interval') || 0),
            },
            feishu: {
                webhook_url: this.value('creative-radar-feishu-webhook'),
                secret: this.value('creative-radar-feishu-secret'),
            },
        };
    },

    async saveConfig(showToast = true) {
        const buttons = [
            [document.getElementById('btn-creative-radar-save'), '保存全部配置'],
            [document.getElementById('btn-creative-radar-save-schedule'), '保存定时配置'],
        ].filter(([button]) => !!button);
        try {
            buttons.forEach(([button]) => {
                button.disabled = true;
                button.textContent = '保存中...';
            });
            const result = await API.creativeRadar.saveConfig(this.collectConfig());
            if (showToast) Toast.success(result.message || '配置已保存');
            await this.loadConfig();
            return true;
        } catch (error) {
            Toast.error(`保存失败：${error.message || error}`);
            return false;
        } finally {
            buttons.forEach(([button, label]) => {
                button.disabled = false;
                button.textContent = label;
            });
        }
    },

    async testApi() {
        if (!await this.saveConfig(false)) return;
        const button = document.getElementById('btn-creative-radar-test-api');
        try {
            if (button) { button.disabled = true; button.textContent = '连接中...'; }
            const result = await API.creativeRadar.testApi();
            Toast.success(result.message || 'API 连接及 API Key 校验成功');
        } catch (error) {
            Toast.error(`API 测试失败：${error.message || error}`);
        } finally {
            if (button) { button.disabled = false; button.textContent = '测试 API 连接'; }
        }
    },

    async testFeishu() {
        if (!await this.saveConfig(false)) return;
        const button = document.getElementById('btn-creative-radar-test-feishu');
        try {
            if (button) { button.disabled = true; button.textContent = '发送中...'; }
            const result = await API.creativeRadar.testFeishu();
            Toast.success(result.message || '测试消息已发送');
        } catch (error) {
            Toast.error(`飞书测试失败：${error.message || error}`);
        } finally {
            if (button) { button.disabled = false; button.textContent = '发送测试消息'; }
        }
    },

    async startRun() {
        if (!await this.saveConfig(false)) return;
        const button = document.getElementById('btn-creative-radar-run');
        let started = false;
        try {
            if (button) { button.disabled = true; button.textContent = '正在创建任务...'; }
            const result = await API.creativeRadar.startRun();
            started = true;
            Toast.success(result.message || '同步任务已创建');
            await this.loadStatus();
        } catch (error) {
            Toast.error(`启动失败：${error.message || error}`);
        } finally {
            if (button) {
                button.textContent = '▶ 立即执行';
                button.disabled = started;
            }
        }
    },

    async togglePause() {
        const button = document.getElementById('btn-creative-radar-pause');
        const action = button?.dataset.action === 'resume' ? 'resume' : 'pause';
        try {
            if (button) {
                button.disabled = true;
                button.textContent = action === 'resume' ? '正在继续...' : '正在暂停...';
            }
            const result = action === 'resume'
                ? await API.creativeRadar.resumeRun()
                : await API.creativeRadar.pauseRun();
            Toast.success(result.message || (action === 'resume' ? '任务已继续执行' : '暂停请求已提交'));
            await this.loadStatus();
        } catch (error) {
            Toast.error(`${action === 'resume' ? '继续' : '暂停'}失败：${error.message || error}`);
        } finally {
            if (button) button.disabled = false;
        }
    },

    addTime() {
        const input = document.getElementById('creative-radar-new-time');
        const value = input?.value || '';
        if (!value) return Toast.warning('请选择触发时间');
        if (!this.scheduleTimes.includes(value)) this.scheduleTimes.push(value);
        this.scheduleTimes.sort();
        this.renderTimes();
    },
    removeTime(value) {
        this.scheduleTimes = this.scheduleTimes.filter(item => item !== value);
        this.renderTimes();
    },
    renderTimes() {
        const list = document.getElementById('creative-radar-time-list');
        if (!list) return;
        if (!this.scheduleTimes.length) {
            list.innerHTML = '<span style="font-size:.82rem; color:var(--text-muted);">尚未添加触发时间</span>';
            return;
        }
        list.innerHTML = this.scheduleTimes.map(value => `<span class="badge" style="display:inline-flex; gap:8px; align-items:center; padding:6px 10px; background:rgba(7,193,96,.08); color:var(--text-primary);">${this.esc(value)} <button type="button" aria-label="删除 ${this.attr(value)}" onclick="ChannelsCreativeRadarPage.removeTime('${this.attr(value)}')" style="border:0; background:none; cursor:pointer; color:var(--error); padding:0;">×</button></span>`).join('');
    },

    async loadStatus() {
        try {
            const status = await API.creativeRadar.getStatus();
            this.renderStatus(status);
            if (this.pollTimer) clearTimeout(this.pollTimer);
            this.pollTimer = setTimeout(() => this.loadStatus(), status.running ? 1000 : 5000);
        } catch (error) {
            if (this.pollTimer) clearTimeout(this.pollTimer);
            this.pollTimer = setTimeout(() => this.loadStatus(), 5000);
        }
    },

    renderStatus(status) {
        const run = status.current_run || {};
        const total = Number(run.total_creators || 0);
        const done = Number(run.completed_creators || 0) + Number(run.failed_creators || 0);
        const currentIndex = Number(run.current_creator_index || 0);
        const currentName = String(run.current_creator_name || run.current_creator_id || '').trim();
        const hasCurrentCreator = !!(status.running && currentIndex > 0 && currentName);
        const percent = total ? Math.round(done / total * 100) : (['completed', 'partial'].includes(run.status) ? 100 : 0);
        const title = document.getElementById('creative-radar-run-title');
        const message = document.getElementById('creative-radar-run-message');
        const progress = document.getElementById('creative-radar-main-progress');
        const next = document.getElementById('creative-radar-next-run');
        const button = document.getElementById('btn-creative-radar-run');
        const pauseButton = document.getElementById('btn-creative-radar-pause');
        const currentCreator = document.getElementById('creative-radar-current-creator');
        const currentCreatorPosition = document.getElementById('creative-radar-current-creator-position');
        const currentCreatorName = document.getElementById('creative-radar-current-creator-name');
        if (title) title.textContent = run.run_id ? `${this.statusLabel(run.status)} · ${this.phaseLabel(run.phase)}` : '尚未执行';
        if (message) message.textContent = run.message || '保存配置后可手动执行或等待定时触发';
        if (progress) progress.style.width = `${Math.max(0, Math.min(100, percent))}%`;
        if (next) next.textContent = status.next_scheduled_at ? `下次执行：${status.next_scheduled_at}（北京时间）` : '下次执行：未启用或未配置';
        if (button) button.disabled = !!status.running;
        const isPaused = ['pausing', 'paused'].includes(run.status);
        if (pauseButton) {
            pauseButton.style.display = status.running ? 'inline-flex' : 'none';
            pauseButton.dataset.action = isPaused ? 'resume' : 'pause';
            pauseButton.textContent = isPaused ? '▶ 继续' : '⏸ 暂停';
        }
        if (currentCreator) currentCreator.style.display = hasCurrentCreator ? 'flex' : 'none';
        if (currentCreatorPosition) currentCreatorPosition.textContent = hasCurrentCreator
            ? `${isPaused ? '已暂停于' : '正在处理'}第 ${currentIndex}/${total} 位`
            : '当前创作者';
        if (currentCreatorName) currentCreatorName.textContent = currentName || '—';

        this.text('creative-radar-stat-creators', `${hasCurrentCreator ? currentIndex : done}/${total}`);
        const creatorStatName = document.getElementById('creative-radar-stat-creator-name');
        if (creatorStatName) {
            creatorStatName.textContent = currentName || '—';
            creatorStatName.title = currentName;
        }
        this.text('creative-radar-stat-refreshed', run.refreshed_videos || 0);
        this.text('creative-radar-stat-existing', run.existing_videos || 0);
        this.text('creative-radar-stat-new', run.new_videos || 0);
        this.text('creative-radar-stat-uploaded', run.uploaded_videos || 0);
        this.text('creative-radar-stat-written', run.database_written || 0);
        this.text('creative-radar-stat-failed', run.failed_items || 0);
        this.renderCreatorRows(run.creators || [], run, status.running);
        this.renderHistory(status.history || []);
    },

    renderCreatorRows(items, run = {}, running = false) {
        const body = document.getElementById('creative-radar-creator-rows');
        if (!body) return;
        const expanded = new Set(Array.from(body.querySelectorAll('details[open]'), item => item.dataset.creatorIndex));
        const total = Number(run.total_creators || 0);
        const currentIndex = Number(run.current_creator_index || 0);
        const currentId = String(run.current_creator_id || '');
        const currentName = String(run.current_creator_name || currentId);
        const rows = items.map((item, index) => ({
            ...item,
            creator_index: Number(item.creator_index || index + 1),
            total_creators: Number(item.total_creators || total),
        }));
        const currentRow = rows.find(item => currentId && item.author_id === currentId);
        if (currentRow && currentIndex) {
            currentRow.creator_index = currentIndex;
        } else if (running && currentIndex > 0 && currentName) {
            rows.push({
                author_id: currentId,
                author_name: currentName,
                creator_index: currentIndex,
                total_creators: total,
                status: run.status === 'paused' ? 'paused' : 'running',
                message: run.message || `正在${this.phaseLabel(run.phase)}`,
                api_batches: run.phase === 'syncing_api' || run.phase === 'paused' ? run.current_api_batches : [],
            });
        }
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-muted);">暂无执行记录</td></tr>';
            return;
        }
        body.innerHTML = rows.map(item => `<tr>
            <td><strong>${this.esc(item.author_name || item.author_id)}</strong><div style="font-size:.72rem; color:var(--primary); margin-top:2px;">第 ${Number(item.creator_index || 0)}/${Number(item.total_creators || total)} 位</div><div style="font-size:.72rem; color:var(--text-muted); max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${this.attr(item.author_id || '')}">${this.esc(item.author_id || '')}</div></td>
            <td>${this.statusLabel(item.status)}</td><td>${Number(item.refreshed_videos || 0)}</td><td>${Number(item.existing_videos || 0)}</td><td>${Number(item.new_videos || 0)}</td><td>${Number(item.uploaded_videos || 0)}</td><td>${Number(item.database_written || 0)}</td><td style="color:${item.failed_items ? 'var(--error)' : 'inherit'}">${Number(item.failed_items || 0)}</td><td style="max-width:270px; font-size:.8rem; color:${item.status === 'failed' ? 'var(--error)' : 'var(--text-secondary)'};">${this.esc(item.message || '')}${this.renderBatchDetails(item.api_batches, item.creator_index, expanded.has(String(item.creator_index)))}</td>
        </tr>`).join('');
    },

    renderBatchDetails(batches, creatorIndex, expanded = false) {
        if (!Array.isArray(batches) || !batches.length) return '';
        const labels = { running:'等待响应', accepted:'已受理', partial:'部分失败', unconfirmed:'结果待确认' };
        return `<details data-creator-index="${Number(creatorIndex || 0)}" ${expanded ? 'open' : ''} style="margin-top:6px;">
            <summary style="cursor:pointer;">API 批次明细（${batches.length} 批）</summary>
            ${batches.map(batch => `<div style="margin-top:6px;">
                <strong>第 ${Number(batch.batch_index)} 批 · ${labels[batch.status] || '结果待确认'}</strong>
                <div>第 ${Number(batch.start)}–${Number(batch.end)} 条，共 ${Number(batch.item_count)} 条</div>
                <div>受理 ${Number(batch.accepted_items || 0)} · 明确失败 ${Number(batch.failed_items || 0)} · 待确认 ${Number(batch.unconfirmed_items || 0)}</div>
                <div style="overflow-wrap:anywhere;">${this.esc(batch.message || '')}</div>
            </div>`).join('')}
        </details>`;
    },

    renderHistory(items) {
        const container = document.getElementById('creative-radar-history');
        if (!container) return;
        if (!items.length) {
            container.innerHTML = '<div style="color:var(--text-muted);">暂无运行记录</div>';
            return;
        }
        container.innerHTML = items.slice(0, 20).map(item => `<div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; padding:10px 12px; background:rgba(0,0,0,.025); border-radius:8px;">
            <div><strong>${this.statusLabel(item.status)}</strong><span style="color:var(--text-muted); margin-left:8px; font-size:.78rem;">${item.trigger === 'scheduled' ? `定时 ${this.esc(item.scheduled_time || '')}` : '手动触发'}</span><div style="font-size:.78rem; color:var(--text-secondary); margin-top:4px;">${this.esc(item.message || '')}</div></div>
            <div style="font-size:.76rem; color:var(--text-muted); text-align:right;">${this.esc(item.started_at || '')}<br>批次 ${this.esc(item.sync_batch_id || '')}</div>
        </div>`).join('');
    },

    statusLabel(value) {
        return ({ queued:'等待中', running:'执行中', pausing:'正在暂停', paused:'已暂停', completed:'已完成', partial:'部分失败/待确认', failed:'失败', interrupted:'已中断' })[value] || '空闲';
    },
    phaseLabel(value) {
        return ({ queued:'任务排队', preflight:'环境检查', checking_wechat:'检查视频号', refreshing:'刷新创作者', checking_api:'整理同步数据', preparing_api:'整理同步数据', uploading_oss:'同步 OSS', syncing_api:'API 同步', creator_interval:'创作者间隔', paused:'已暂停', finished:'已结束', interrupted:'已中断' })[value] || '准备中';
    },
    value(id) { return document.getElementById(id)?.value.trim() || ''; },
    setValue(id, value) { const el = document.getElementById(id); if (el) el.value = value == null ? '' : value; },
    setSecretPlaceholder(id, saved, label) { const el = document.getElementById(id); if (el) { el.value = ''; el.placeholder = saved ? '已保存；不修改请留空' : `请输入${label}`; } },
    text(id, value) { const el = document.getElementById(id); if (el) el.textContent = value; },
    esc(value) { const div = document.createElement('div'); div.textContent = value == null ? '' : String(value); return div.innerHTML; },
    attr(value) { return this.esc(value).replace(/`/g, '&#96;').replace(/'/g, '&#39;'); },
};
