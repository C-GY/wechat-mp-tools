/** CompetitorMonitor hub configuration, scheduler, and durable progress dashboard. */
const ChannelsCompetitorMonitorPage = {
    pollTimer: null,
    scheduleTimes: [],

    render() {
        return `
            <div class="page-header animate-fade-in" style="display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap;">
                <div>
                    <h2 class="page-title">竞对监测系统</h2>
                    <p class="page-description">按创作者刷新作品、增量同步 OSS；同时保存视频最新数据、标签和每批次互动快照。</p>
                </div>
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    <a class="btn btn-secondary" href="#channels_competitor_author_tags">账号标签管理</a>
                    <button class="btn btn-secondary" id="btn-competitor_monitor-export-config" onclick="ChannelsCompetitorMonitorPage.exportConfig()" title="复制已保存的完整配置，包含数据库密码和飞书密钥">📋 导出配置</button>
                    <button class="btn btn-secondary" id="btn-competitor_monitor-import-config" onclick="ChannelsCompetitorMonitorPage.importConfig()">📥 导入配置</button>
                    <button class="btn btn-primary" id="btn-competitor_monitor-run" onclick="ChannelsCompetitorMonitorPage.startRun()">▶ 立即执行</button>
                    <button class="btn btn-secondary" id="btn-competitor_monitor-pause" data-action="pause" onclick="ChannelsCompetitorMonitorPage.togglePause()" style="display:none;">⏸ 暂停</button>
                </div>
            </div>

            <div id="competitor_monitor-summary" class="card animate-fade-in" style="margin-top:var(--spacing-lg); padding:18px;">
                <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:center;">
                    <div>
                        <div style="font-size:.78rem; color:var(--text-muted);">当前状态</div>
                        <div id="competitor_monitor-run-title" style="font-size:1.05rem; font-weight:700; margin-top:4px;">正在读取...</div>
                    </div>
                    <div id="competitor_monitor-next-run" style="font-size:.82rem; color:var(--text-muted);">下次执行：—</div>
                </div>
                <div style="height:8px; border-radius:999px; overflow:hidden; background:rgba(0,0,0,.08); margin-top:14px;">
                    <div id="competitor_monitor-main-progress" style="height:100%; width:0; background:var(--primary); transition:width .25s;"></div>
                </div>
                <div id="competitor_monitor-run-message" style="font-size:.84rem; color:var(--text-secondary); margin-top:10px;">—</div>
                <div id="competitor_monitor-current-creator" style="display:none; align-items:center; gap:10px; margin-top:12px; padding:10px 12px; border-radius:9px; background:rgba(37,99,235,.08); color:var(--text-secondary);">
                    <span id="competitor_monitor-current-creator-position" style="font-size:.78rem; white-space:nowrap;">当前创作者</span>
                    <strong id="competitor_monitor-current-creator-name" style="color:var(--text-primary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">—</strong>
                </div>
                <div class="competitor_monitor-stats-row">
                    ${this.statCard('当前创作者', 'competitor_monitor-stat-creators', 'competitor_monitor-stat-creator-name')}
                    ${this.statCard('刷新作品', 'competitor_monitor-stat-refreshed')}
                    ${this.statCard('数据库已有', 'competitor_monitor-stat-existing')}
                    ${this.statCard('新增作品', 'competitor_monitor-stat-new')}
                    ${this.statCard('OSS 完成', 'competitor_monitor-stat-uploaded')}
                    ${this.statCard('数据库处理', 'competitor_monitor-stat-written')}
                    ${this.statCard('失败项', 'competitor_monitor-stat-failed')}
                </div>
                <div style="font-size:.78rem; color:var(--text-muted); margin-top:8px;">每条作品的主表、标签和快照一并提交；同批次重试不重复，旧采集不会覆盖较新数据。</div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0; flex-shrink:0;">⚙️ MySQL 8 配置</h3>
                    <div style="font-size:.8rem; color:var(--text-muted); margin-top:5px;">写入 competitor_videos、competitor_video_tags、competitor_video_snapshots 三张表；配置保存在当前电脑用户目录。视频标签与标题 #话题 自动入库，已有标签保留。</div>
                </div>
                <div style="display:grid; grid-template-columns:minmax(220px,2fr) minmax(120px,1fr); gap:14px;">
                    ${this.input('数据库地址', 'competitor_monitor-db-host', 'text', '例如 127.0.0.1')}
                    ${this.input('端口', 'competitor_monitor-db-port', 'number', '3306')}
                </div>
                <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; margin-top:14px;">
                    ${this.input('账号', 'competitor_monitor-db-username', 'text', 'MySQL 用户名')}
                    ${this.input('密码', 'competitor_monitor-db-password', 'password', '请输入密码')}
                    ${this.input('数据库', 'competitor_monitor-db-database', 'text', 'competitor_monitor')}
                </div>
                <div style="display:flex; gap:8px; margin-top:16px; flex-wrap:wrap;">
                    <button class="btn btn-primary" id="btn-competitor_monitor-save" onclick="ChannelsCompetitorMonitorPage.saveConfig()">保存全部配置</button>
                    <button class="btn btn-secondary" id="btn-competitor_monitor-test-db" onclick="ChannelsCompetitorMonitorPage.testDatabase()">测试 MySQL</button>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">🕐 每日定时触发</h3>
                    <div style="font-size:.8rem; color:var(--text-muted); margin-top:5px;">北京时间；软件关闭时不会触发。若上一轮仍在运行，本轮将跳过并发送飞书通知。</div>
                </div>
                <label style="display:flex; align-items:center; gap:9px; cursor:pointer; font-weight:600;">
                    <input id="competitor_monitor-schedule-enabled" type="checkbox"> 启用定时同步
                </label>
                <div style="display:flex; gap:8px; align-items:flex-end; margin-top:14px; flex-wrap:wrap;">
                    <div class="form-group" style="margin:0;">
                        <label class="form-label" for="competitor_monitor-new-time">新增触发时间</label>
                        <input id="competitor_monitor-new-time" class="form-input" type="time" value="09:00" style="width:170px;">
                    </div>
                    <button class="btn btn-secondary" onclick="ChannelsCompetitorMonitorPage.addTime()">＋ 添加时间</button>
                    <div class="form-group" style="margin:0; min-width:220px;">
                        <label class="form-label" for="competitor_monitor-creator-interval">创作者之间的间隔（秒）</label>
                        <input id="competitor_monitor-creator-interval" class="form-input" type="number" min="0" max="86400" value="10">
                    </div>
                </div>
                <div id="competitor_monitor-time-list" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:14px;"></div>
                <div style="display:flex; gap:10px; align-items:center; margin-top:16px; flex-wrap:wrap;">
                    <button class="btn btn-primary" id="btn-competitor_monitor-save-schedule" onclick="ChannelsCompetitorMonitorPage.saveConfig()">保存定时配置</button>
                    <span style="font-size:.8rem; color:var(--text-muted);">添加、删除时间或修改间隔后，请点击保存。</span>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">🔔 飞书机器人（可选）</h3>
                    <div style="font-size:.8rem; color:var(--text-muted); margin-top:5px;">配置后，创作者失败、任务异常或计划任务未能启动时发送通知；发送失败自动重试 3 次。未配置也可同步。</div>
                </div>
                ${this.input('Webhook 地址', 'competitor_monitor-feishu-webhook', 'url', 'https://open.feishu.cn/open-apis/bot/v2/hook/...')}
                <div style="margin-top:14px;">${this.input('签名密钥（可选）', 'competitor_monitor-feishu-secret', 'password', '机器人未开启签名校验可留空')}</div>
                <div style="display:flex; gap:8px; margin-top:16px; flex-wrap:wrap;">
                    <button class="btn btn-secondary" id="btn-competitor_monitor-test-feishu" onclick="ChannelsCompetitorMonitorPage.testFeishu()">发送测试消息</button>
                    <button class="btn btn-secondary" onclick="Router.navigate('channels_oss_config')">打开 OSS 配置</button>
                    <span id="competitor_monitor-oss-state" style="font-size:.82rem; color:var(--text-muted); align-self:center;">OSS 状态：读取中</span>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg);">
                <div class="card-header" style="display:flex; justify-content:space-between; align-items:center; gap:10px; border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">📍 创作者执行明细</h3>
                    <button class="btn btn-secondary btn-sm" onclick="ChannelsCompetitorMonitorPage.loadStatus()">刷新</button>
                </div>
                <div style="overflow:auto;">
                    <table class="data-table" style="min-width:1100px; width:100%;">
                        <thead><tr><th>创作者</th><th>状态</th><th>刷新</th><th>已有</th><th>新增</th><th>OSS</th><th title="包含本批次新增快照及同批次重试">数据库处理</th><th>失败</th><th>结果</th></tr></thead>
                        <tbody id="competitor_monitor-creator-rows"><tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-muted);">暂无执行记录</td></tr></tbody>
                    </table>
                </div>
            </div>

            <div class="card animate-fade-in" style="margin-top:var(--spacing-lg); margin-bottom:var(--spacing-lg);">
                <div class="card-header" style="border-bottom:1px solid var(--border-color); padding-bottom:var(--spacing-md); margin-bottom:var(--spacing-md);">
                    <h3 class="card-title" style="margin:0;">🧾 最近运行记录</h3>
                </div>
                <div id="competitor_monitor-history" style="display:grid; gap:9px;"><div style="color:var(--text-muted);">暂无运行记录</div></div>
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
            const config = await API.competitor_monitor.getConfig();
            const db = config.database || {};
            const schedule = config.schedule || {};
            const feishu = config.feishu || {};
            this.setValue('competitor_monitor-db-host', db.host || '');
            this.setValue('competitor_monitor-db-port', db.port || 3306);
            this.setValue('competitor_monitor-db-username', db.username || '');
            this.setValue('competitor_monitor-db-database', db.database || 'competitor_monitor');
            this.setSecretPlaceholder('competitor_monitor-db-password', db.has_password, '数据库密码');
            this.setValue('competitor_monitor-creator-interval', schedule.creator_interval_seconds ?? 10);
            const enabled = document.getElementById('competitor_monitor-schedule-enabled');
            if (enabled) enabled.checked = !!schedule.enabled;
            this.scheduleTimes = Array.isArray(schedule.times) ? [...schedule.times] : [];
            this.renderTimes();
            this.setSecretPlaceholder('competitor_monitor-feishu-webhook', feishu.has_webhook, '飞书机器人 Webhook');
            this.setSecretPlaceholder('competitor_monitor-feishu-secret', feishu.has_secret, '飞书签名密钥');
            const oss = document.getElementById('competitor_monitor-oss-state');
            if (oss) {
                oss.textContent = config.oss_configured ? 'OSS 状态：✅ 已配置' : 'OSS 状态：⚠️ 未配置';
                oss.style.color = config.oss_configured ? 'var(--success)' : 'var(--warning)';
            }
        } catch (error) {
            Toast.error(`读取竞对监测配置失败：${error.message || error}`);
        }
    },

    exportConfig() {
        return HubConfigTransfer.exportConfig({
            api: API.competitor_monitor, label: '竞对监测', buttonId: 'btn-competitor_monitor-export-config',
        });
    },

    importConfig() {
        HubConfigTransfer.importConfig({
            api: API.competitor_monitor, label: '竞对监测',
            onImported: async () => { await this.loadConfig(); await this.loadStatus(); },
        });
    },

    collectConfig() {
        return {
            database: {
                host: this.value('competitor_monitor-db-host'),
                port: Number(this.value('competitor_monitor-db-port') || 3306),
                username: this.value('competitor_monitor-db-username'),
                password: this.value('competitor_monitor-db-password'),
                database: this.value('competitor_monitor-db-database') || 'competitor_monitor',
            },
            schedule: {
                enabled: !!document.getElementById('competitor_monitor-schedule-enabled')?.checked,
                times: [...this.scheduleTimes],
                creator_interval_seconds: Number(this.value('competitor_monitor-creator-interval') || 0),
            },
            feishu: {
                webhook_url: this.value('competitor_monitor-feishu-webhook'),
                secret: this.value('competitor_monitor-feishu-secret'),
            },
        };
    },

    async saveConfig(showToast = true) {
        const buttons = [
            [document.getElementById('btn-competitor_monitor-save'), '保存全部配置'],
            [document.getElementById('btn-competitor_monitor-save-schedule'), '保存定时配置'],
        ].filter(([button]) => !!button);
        try {
            buttons.forEach(([button]) => {
                button.disabled = true;
                button.textContent = '保存中...';
            });
            const result = await API.competitor_monitor.saveConfig(this.collectConfig());
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

    async testDatabase() {
        if (!await this.saveConfig(false)) return;
        const button = document.getElementById('btn-competitor_monitor-test-db');
        try {
            if (button) { button.disabled = true; button.textContent = '连接中...'; }
            const result = await API.competitor_monitor.testDatabase();
            Toast.success(`${result.message || 'MySQL 连接成功'}${result.version ? `（${result.version}）` : ''}`);
        } catch (error) {
            Toast.error(`MySQL 测试失败：${error.message || error}`);
        } finally {
            if (button) { button.disabled = false; button.textContent = '测试 MySQL'; }
        }
    },

    async testFeishu() {
        if (!await this.saveConfig(false)) return;
        const button = document.getElementById('btn-competitor_monitor-test-feishu');
        try {
            if (button) { button.disabled = true; button.textContent = '发送中...'; }
            const result = await API.competitor_monitor.testFeishu();
            Toast.success(result.message || '测试消息已发送');
        } catch (error) {
            Toast.error(`飞书测试失败：${error.message || error}`);
        } finally {
            if (button) { button.disabled = false; button.textContent = '发送测试消息'; }
        }
    },

    async startRun() {
        if (!await this.saveConfig(false)) return;
        const button = document.getElementById('btn-competitor_monitor-run');
        let started = false;
        try {
            if (button) { button.disabled = true; button.textContent = '正在创建任务...'; }
            const result = await API.competitor_monitor.startRun();
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
        const button = document.getElementById('btn-competitor_monitor-pause');
        const action = button?.dataset.action === 'resume' ? 'resume' : 'pause';
        try {
            if (button) {
                button.disabled = true;
                button.textContent = action === 'resume' ? '正在继续...' : '正在暂停...';
            }
            const result = action === 'resume'
                ? await API.competitor_monitor.resumeRun()
                : await API.competitor_monitor.pauseRun();
            Toast.success(result.message || (action === 'resume' ? '任务已继续执行' : '暂停请求已提交'));
            await this.loadStatus();
        } catch (error) {
            Toast.error(`${action === 'resume' ? '继续' : '暂停'}失败：${error.message || error}`);
        } finally {
            if (button) button.disabled = false;
        }
    },

    addTime() {
        const input = document.getElementById('competitor_monitor-new-time');
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
        const list = document.getElementById('competitor_monitor-time-list');
        if (!list) return;
        if (!this.scheduleTimes.length) {
            list.innerHTML = '<span style="font-size:.82rem; color:var(--text-muted);">尚未添加触发时间</span>';
            return;
        }
        list.innerHTML = this.scheduleTimes.map(value => `<span class="badge" style="display:inline-flex; gap:8px; align-items:center; padding:6px 10px; background:rgba(7,193,96,.08); color:var(--text-primary);">${this.esc(value)} <button type="button" aria-label="删除 ${this.attr(value)}" onclick="ChannelsCompetitorMonitorPage.removeTime('${this.attr(value)}')" style="border:0; background:none; cursor:pointer; color:var(--error); padding:0;">×</button></span>`).join('');
    },

    async loadStatus() {
        try {
            const status = await API.competitor_monitor.getStatus();
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
        const title = document.getElementById('competitor_monitor-run-title');
        const message = document.getElementById('competitor_monitor-run-message');
        const progress = document.getElementById('competitor_monitor-main-progress');
        const next = document.getElementById('competitor_monitor-next-run');
        const button = document.getElementById('btn-competitor_monitor-run');
        const pauseButton = document.getElementById('btn-competitor_monitor-pause');
        const currentCreator = document.getElementById('competitor_monitor-current-creator');
        const currentCreatorPosition = document.getElementById('competitor_monitor-current-creator-position');
        const currentCreatorName = document.getElementById('competitor_monitor-current-creator-name');
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

        this.text('competitor_monitor-stat-creators', `${hasCurrentCreator ? currentIndex : done}/${total}`);
        const creatorStatName = document.getElementById('competitor_monitor-stat-creator-name');
        if (creatorStatName) {
            creatorStatName.textContent = currentName || '—';
            creatorStatName.title = currentName;
        }
        this.text('competitor_monitor-stat-refreshed', run.refreshed_videos || 0);
        this.text('competitor_monitor-stat-existing', run.existing_videos || 0);
        this.text('competitor_monitor-stat-new', run.new_videos || 0);
        this.text('competitor_monitor-stat-uploaded', run.uploaded_videos || 0);
        this.text('competitor_monitor-stat-written', run.database_written || 0);
        this.text('competitor_monitor-stat-failed', run.failed_items || 0);
        this.renderCreatorRows(run.creators || [], run, status.running);
        this.renderHistory(status.history || []);
    },

    renderCreatorRows(items, run = {}, running = false) {
        const body = document.getElementById('competitor_monitor-creator-rows');
        if (!body) return;
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
                message: `正在${this.phaseLabel(run.phase)}`,
            });
        }
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-muted);">暂无执行记录</td></tr>';
            return;
        }
        body.innerHTML = rows.map(item => `<tr>
            <td><strong>${this.esc(item.author_name || item.author_id)}</strong><div style="font-size:.72rem; color:var(--primary); margin-top:2px;">第 ${Number(item.creator_index || 0)}/${Number(item.total_creators || total)} 位</div><div style="font-size:.72rem; color:var(--text-muted); max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${this.attr(item.author_id || '')}">${this.esc(item.author_id || '')}</div></td>
            <td>${this.statusLabel(item.status)}</td><td>${Number(item.refreshed_videos || 0)}</td><td>${Number(item.existing_videos || 0)}</td><td>${Number(item.new_videos || 0)}</td><td>${Number(item.uploaded_videos || 0)}</td><td>${Number(item.database_written || 0)}</td><td style="color:${item.failed_items ? 'var(--error)' : 'inherit'}">${Number(item.failed_items || 0)}</td><td style="max-width:270px; font-size:.8rem; color:${item.status === 'failed' ? 'var(--error)' : 'var(--text-secondary)'};">${this.esc(item.message || '')}</td>
        </tr>`).join('');
    },

    renderHistory(items) {
        const container = document.getElementById('competitor_monitor-history');
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
        return ({ queued:'等待中', running:'执行中', pausing:'正在暂停', paused:'已暂停', completed:'已完成', partial:'部分失败', failed:'失败', interrupted:'已中断' })[value] || '空闲';
    },
    phaseLabel(value) {
        return ({ queued:'任务排队', preflight:'环境检查', checking_wechat:'检查视频号', waiting_wechat:'等待自动恢复', refreshing:'刷新创作者', checking_database:'比对数据库', uploading_oss:'同步 OSS', writing_database:'数据库处理', creator_interval:'创作者间隔', paused:'已暂停', finished:'已结束', interrupted:'已中断' })[value] || '准备中';
    },
    value(id) { return document.getElementById(id)?.value.trim() || ''; },
    setValue(id, value) { const el = document.getElementById(id); if (el) el.value = value == null ? '' : value; },
    setSecretPlaceholder(id, saved, label) { const el = document.getElementById(id); if (el) { el.value = ''; el.placeholder = saved ? '已保存；不修改请留空' : `请输入${label}`; } },
    text(id, value) { const el = document.getElementById(id); if (el) el.textContent = value; },
    esc(value) { const div = document.createElement('div'); div.textContent = value == null ? '' : String(value); return div.innerHTML; },
    attr(value) { return this.esc(value).replace(/`/g, '&#96;').replace(/'/g, '&#39;'); },
};
