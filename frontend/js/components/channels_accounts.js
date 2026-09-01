/**
 * 视频号作者管理页面组件
 */
const ChannelsAccountsPage = {
    favorites: [],
    resolvedAuthor: null,
    isParsing: false,
    activeRefreshTaskId: null,
    refreshPollTimer: null,

    render() {
        return `
            <div class="page-header animate-fade-in">
                <h2 class="page-title">视频号管理</h2>
                <p class="page-description">根据视频链接解析并收藏视频号作者，查看创作者作品。</p>
            </div>



            <!-- 解析与添加区域 -->
            <div class="card animate-fade-in" style="margin-top: var(--spacing-lg); margin-bottom: var(--spacing-lg);">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; padding-bottom: var(--spacing-sm); border-bottom: 1px solid rgba(0,0,0,0.05);">
                    <h3 class="card-title" style="margin: 0;">🔍 解析并收藏作者</h3>
                    <span style="font-size: 0.85rem; color: var(--text-muted);">通过粘贴作者任意视频的链接来提取作者信息</span>
                </div>
                
                <div class="form-group" style="margin-top: var(--spacing-md);">
                    <div style="display: flex; gap: var(--spacing-sm);">
                        <input type="text" id="channels-author-url-input" class="form-input" 
                               placeholder="粘贴作者任意视频的分享链接 (如 https://weixin.qq.com/sph/xxxxx)" 
                               style="flex: 1; padding: 12px 16px; font-size: 1rem; border-radius: 12px;"
                               onkeydown="if(event.key==='Enter') ChannelsAccountsPage.parseAuthorLink()">
                        <button class="btn btn-secondary" onclick="ChannelsAccountsPage.pasteFromClipboard()" style="padding: 12px 20px; font-weight: 500;">
                            📋 粘贴
                        </button>
                        <button class="btn btn-primary" id="btn-parse-author" onclick="ChannelsAccountsPage.parseAuthorLink()" style="min-width: 120px; display: flex; align-items: center; justify-content: center; gap: 8px;">
                            <span>解析作者</span>
                        </button>
                    </div>
                    
                    <div style="margin-top: 12px; font-size: 0.85rem; color: var(--text-muted); display: flex; align-items: center; justify-content: space-between;">
                        <span>支持包含视频号链接 of 混合文本</span>
                        <a href="javascript:void(0)" onclick="ChannelsAccountsPage.toggleManualAdd()" style="color: var(--primary); text-decoration: none; font-weight: 500;">⚡ 高级：直接输入作者 ID 收藏</a>
                    </div>
                </div>

                <!-- 手动输入 ID 收藏折叠栏 -->
                <div id="manual-add-container" style="display: none; margin-top: var(--spacing-md); padding-top: var(--spacing-md); border-top: 1px dashed var(--border-color);">
                    <div style="display: flex; gap: var(--spacing-sm); align-items: center; flex-wrap: wrap;">
                        <input type="text" id="manual-username-input" class="form-input" placeholder="输入作者 ID (如 v2_xxx@finder)" style="flex: 1; min-width: 250px; font-family: monospace; font-size: 0.9rem;">
                        <input type="text" id="manual-nickname-input" class="form-input" placeholder="输入作者昵称/备注" style="width: 200px;">
                        <button class="btn btn-secondary" onclick="ChannelsAccountsPage.addManualFavorite()" style="white-space: nowrap; font-weight: 500;">直接收藏</button>
                    </div>
                    <div class="form-hint" style="margin-top: 6px; color: #ff9900; font-size: 0.8rem;">提示：直接使用 ID 收藏时，头像将采用默认占位图，且不自动拉取视频作品。</div>
                </div>

                <!-- 状态指示 -->
                <div id="author-parse-status" style="display: none; margin-top: var(--spacing-md); padding: 12px; border-radius: 8px; font-size: 0.9rem; text-align: center; background: rgba(0,0,0,0.02);">
                    <span class="spinner" style="width: 16px; height: 16px; border-width: 2px; display: inline-block; vertical-align: middle; margin-right: 8px;"></span>
                    <span id="author-parse-status-text">正在发起云端解析...</span>
                </div>

                <!-- 解析出的作者卡片 -->
                <div id="resolved-author-card" style="display: none; margin-top: var(--spacing-lg); padding: var(--spacing-md); background: rgba(7,193,96,0.03); border: 1.5px solid rgba(7,193,96,0.15); border-radius: 12px; align-items: center; justify-content: space-between; gap: var(--spacing-md); flex-wrap: wrap;">
                    <!-- 动态渲染 -->
                </div>
            </div>

            <!-- 已收藏创作者列表 -->
            <div class="card animate-fade-in">
                <div class="card-header" style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: var(--spacing-md); margin-bottom: var(--spacing-md);">
                    <h3 class="card-title" style="margin: 0; display: flex; align-items: center; gap: 8px;">
                        👥 已收藏创作者
                        <span id="favorites-count-badge" class="badge" style="background: var(--primary); color: white; font-size: 0.8rem; border-radius: 20px; padding: 2px 8px;">0</span>
                    </h3>
                    <div style="display: flex; gap: var(--spacing-sm); align-items: center; flex-wrap: wrap;">
                        <button class="btn btn-primary btn-sm" id="btn-refresh-all-favorites" onclick="ChannelsAccountsPage.startFavoritesRefresh()" style="font-size: 0.85rem; padding: 6px 12px; font-weight: 500;">
                            ⚡ 一键刷新全部收藏
                        </button>
                        <button class="btn btn-primary btn-sm" id="btn-sync-favorites-oss" onclick="ChannelsAccountsPage.syncFavoritesToOSS()" style="font-size: 0.85rem; padding: 6px 12px; font-weight: 500;">
                            ☁️ 一键同步 OSS
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="ChannelsAccountsPage.loadFavorites()" style="font-size: 0.85rem; padding: 6px 12px; font-weight: 500;">🔄 刷新列表</button>
                    </div>
                </div>

                <div id="favorites-refresh-status" style="display: none; margin-bottom: var(--spacing-md); padding: 12px 14px; border-radius: 10px; background: rgba(7,193,96,0.06); border: 1px solid rgba(7,193,96,0.16);">
                    <div style="display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 8px;">
                        <strong id="favorites-refresh-status-text" style="font-size: 0.9rem; color: var(--text-primary);">准备刷新...</strong>
                        <span id="favorites-refresh-progress-text" style="font-size: 0.8rem; color: var(--text-muted); white-space: nowrap;">0/0</span>
                    </div>
                    <div style="height: 6px; border-radius: 999px; overflow: hidden; background: rgba(0,0,0,0.08);">
                        <div id="favorites-refresh-progress-bar" style="width: 0%; height: 100%; background: var(--primary); transition: width 0.25s ease;"></div>
                    </div>
                    <div id="favorites-refresh-detail" style="margin-top: 7px; font-size: 0.78rem; color: var(--text-muted);">系统将自动检测微信环境并打开视频号，无需手动准备。</div>
                </div>

                <!-- 创作者网格 -->
                <div id="favorites-grid" class="card-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--spacing-md); margin-top: var(--spacing-lg);">
                    <!-- 动态渲染 -->
                </div>

                <!-- 空状态 -->
                <div id="favorites-empty" class="empty-state" style="display: none; padding: 60px 24px; text-align: center;">
                    <div class="empty-state-icon" style="color: var(--text-muted); margin-bottom: 16px;">
                        <svg viewBox="0 0 24 24" fill="none" width="60" height="60" stroke="currentColor" stroke-width="1.5">
                            <path d="M17 21V19C17 16.79 15.21 15 13 15H5C2.79 15 1 16.79 1 19V21"/>
                            <circle cx="9" cy="7" r="4"/>
                        </svg>
                    </div>
                    <div class="empty-state-title" style="font-size: 1.1rem; font-weight: 600; color: var(--text-primary);">暂无收藏的作者</div>
                    <div class="empty-state-desc" style="color: var(--text-muted); font-size: 0.9rem; margin-top: 4px;">在上方粘贴作者任意视频的分享链接，解析并加入收藏。</div>
                </div>
            </div>
        `;
    },

    destroy() {
        if (this.refreshPollTimer) {
            clearTimeout(this.refreshPollTimer);
            this.refreshPollTimer = null;
        }
        this.favorites = [];
        this.resolvedAuthor = null;
        this.isParsing = false;
        this.activeRefreshTaskId = null;
    },

    async onShow() {
        await this.init();
    },

    async init() {
        await this.loadFavorites();
        try {
            const savedTaskId = localStorage.getItem('channelsFavoritesRefreshTaskId');
            if (savedTaskId) {
                this.activeRefreshTaskId = savedTaskId;
                this.setFavoritesRefreshButton(true);
                this.pollFavoritesRefresh(savedTaskId);
            }
        } catch (_) {}
    },

    async pasteFromClipboard() {
        try {
            const text = await navigator.clipboard.readText();
            const input = document.getElementById('channels-author-url-input');
            if (input) {
                input.value = text.trim();
                Toast.success('已粘贴剪贴板内容');
                input.focus();
            }
        } catch (err) {
            Toast.warning('无法读取剪贴板，请手动粘贴');
        }
    },

    toggleManualAdd() {
        const container = document.getElementById('manual-add-container');
        if (container) {
            const isHidden = container.style.display === 'none';
            container.style.display = isHidden ? 'block' : 'none';
        }
    },

    async parseAuthorLink() {
        if (this.isParsing) return;

        const input = document.getElementById('channels-author-url-input');
        const shareUrl = input?.value.trim();

        if (!shareUrl) {
            Toast.warning('请输入或粘贴视频号链接');
            return;
        }

        const parseBtn = document.getElementById('btn-parse-author');
        const statusDiv = document.getElementById('author-parse-status');
        const resolvedCard = document.getElementById('resolved-author-card');

        if (parseBtn) parseBtn.disabled = true;
        if (statusDiv) statusDiv.style.display = 'block';
        if (resolvedCard) resolvedCard.style.display = 'none';

        this.isParsing = true;

        try {
            const res = await API.channels.fetchVideoProfile(shareUrl);
            const ai = res.data && res.data.authorInfo;

            if (!ai || (!ai.username && !ai.nickname)) {
                throw new Error('解析成功，但未能提取到作者信息（昵称与 ID 均为空）');
            }

            this.resolvedAuthor = {
                username: ai.username || ai.nickname,
                nickname: ai.nickname || '未命名作者',
                head_img_url: ai.headImgUrl || '',
                video_url: fi?.videoUrl || ''
            };

            // 如果有解析的视频本身，顺便把这条视频保存下来作为作者的初始视频
            const fi_data = res.data.feedInfo;
            if (fi_data && fi_data.videoUrl) {
                // 异步存入该作者的作品库，不做阻碍
                API.channels.addAuthorVideo(this.resolvedAuthor.username, {
                    id: fi_data.id || String(Date.now()),
                    description: fi_data.description || '',
                    cover_url: fi_data.coverUrl || '',
                    video_url: fi_data.videoUrl || '',
                    video_url_h264: fi_data.h264VideoInfo?.videoUrl || '',
                    video_url_h265: fi_data.h265VideoInfo?.videoUrl || '',
                    createtime: fi_data.createtime ? String(fi_data.createtime) : String(Math.floor(Date.now() / 1000)),
                    decode_key: fi_data.media?.decodeKey || fi_data.decodeKey || ''
                }).catch(e => console.error('保存初始视频失败:', e));
            }

            this.renderResolvedAuthor();
            Toast.success('成功解析出作者信息！');
        } catch (err) {
            Toast.error('解析作者失败: ' + (err.message || '网络或接口故障'));
        } finally {
            this.isParsing = false;
            if (parseBtn) parseBtn.disabled = false;
            if (statusDiv) statusDiv.style.display = 'none';
        }
    },

    renderResolvedAuthor() {
        const card = document.getElementById('resolved-author-card');
        if (!card || !this.resolvedAuthor) return;

        const defaultAvatar = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23888'><path d='M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z'/></svg>";
        const isAlreadyFav = this.favorites.some(fav => fav.username === this.resolvedAuthor.username);

        card.innerHTML = `
            <div style="display: flex; align-items: center; gap: 16px; flex: 1; min-width: 280px;">
                <img src="${this.esc(this.resolvedAuthor.head_img_url)}" alt="${this.esc(this.resolvedAuthor.nickname)}" 
                     style="width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 2px solid var(--primary);" 
                     onerror="this.src='${defaultAvatar}'">
                <div style="flex: 1; overflow: hidden;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <strong style="font-size: 1.1rem; color: var(--text-primary);">${this.esc(this.resolvedAuthor.nickname)}</strong>
                        <span class="badge badge-success" style="font-size: 0.75rem; background: var(--primary); color: white; padding: 2px 6px;">解析成功</span>
                    </div>
                    <div style="font-size: 0.8rem; color: var(--text-muted); font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 4px;" title="${this.esc(this.resolvedAuthor.username)}">
                        ID: ${this.esc(this.resolvedAuthor.username)}
                    </div>
                </div>
            </div>
            <div style="display: flex; gap: var(--spacing-sm); align-items: center; min-width: 220px; justify-content: flex-end;">
                ${isAlreadyFav ? `
                    <button class="btn btn-secondary" disabled style="padding: 10px 18px; font-weight: 500;">✓ 已在收藏列表中</button>
                ` : `
                    <button class="btn btn-primary" onclick="ChannelsAccountsPage.saveResolvedFavorite()" style="padding: 10px 20px; font-weight: 500; display: flex; align-items: center; gap: 6px;">
                        💚 收藏该作者
                    </button>
                `}
                <button class="btn btn-secondary" onclick="Router.navigate('channels_user?username=${this.esc(this.resolvedAuthor.username)}')" style="padding: 10px 16px; font-weight: 500;">
                    直接进入主页
                </button>
            </div>
        `;
        card.style.display = 'flex';
    },

    async saveResolvedFavorite() {
        if (!this.resolvedAuthor) return;
        try {
            await API.channels.addFavorite(this.resolvedAuthor);
            Toast.success(`已收藏作者: ${this.resolvedAuthor.nickname}`);
            
            // 隐藏解析卡片并清空输入框
            const card = document.getElementById('resolved-author-card');
            if (card) card.style.display = 'none';
            const input = document.getElementById('channels-author-url-input');
            if (input) input.value = '';

            this.resolvedAuthor = null;
            await this.loadFavorites();
        } catch (err) {
            Toast.error('收藏失败: ' + err.message);
        }
    },

    async addManualFavorite() {
        const usernameInput = document.getElementById('manual-username-input');
        const nicknameInput = document.getElementById('manual-nickname-input');
        const username = usernameInput?.value.trim();
        const nickname = nicknameInput?.value.trim() || '未解析创作者';

        if (!username) {
            Toast.warning('请输入作者 ID');
            return;
        }

        try {
            await API.channels.addFavorite({
                username: username,
                nickname: nickname,
                head_img_url: '',
                video_url: ''
            });

            Toast.success('手动收藏成功！');
            if (usernameInput) usernameInput.value = '';
            if (nicknameInput) nicknameInput.value = '';
            
            // 折叠回手动收藏区域
            this.toggleManualAdd();
            await this.loadFavorites();
        } catch (err) {
            Toast.error('手动收藏失败: ' + err.message);
        }
    },

    async startFavoritesRefresh() {
        if (this.favorites.length === 0) {
            Toast.warning('暂无已收藏创作者');
            return;
        }
        if (this.activeRefreshTaskId) return;

        this.setFavoritesRefreshButton(true, '⏳ 正在检测微信...');
        this.updateFavoritesRefreshStatus({
            status: 'preparing',
            total_authors: this.favorites.length,
            completed_authors: 0,
            failed_authors: 0,
            total_videos: 0,
            message: '正在检测微信环境并自动打开视频号…',
        });
        try {
            const environment = await API.channels.openWechatChannels({ showError: false });
            if (!environment.monitoring_active) {
                Toast.warning(environment.message || '视频号已打开，正在等待页面联网');
            }

            this.setFavoritesRefreshButton(true, '⏳ 正在创建刷新任务...');
            const task = await API.channels.startFavoritesRefresh();
            this.activeRefreshTaskId = task.task_id;
            try {
                localStorage.setItem('channelsFavoritesRefreshTaskId', task.task_id);
            } catch (_) {}
            this.updateFavoritesRefreshStatus(task);
            if (!task.created) {
                Toast.warning('已有刷新任务正在运行，已继续显示其进度');
            }
            this.pollFavoritesRefresh(task.task_id);
        } catch (err) {
            this.activeRefreshTaskId = null;
            try {
                localStorage.removeItem('channelsFavoritesRefreshTaskId');
            } catch (_) {}
            this.setFavoritesRefreshButton(false);
            const message = '自动刷新启动失败：' + (err.message || '未知错误');
            this.updateFavoritesRefreshStatus({
                status: 'failed',
                total_authors: this.favorites.length,
                completed_authors: 0,
                failed_authors: 0,
                total_videos: 0,
                message,
            });
            Toast.error(message);
        }
    },

    async syncFavoritesToOSS() {
        if (this.favorites.length === 0) {
            Toast.warning('暂无已收藏创作者');
            return;
        }
        const button = document.getElementById('btn-sync-favorites-oss');
        try {
            if (button) { button.disabled = true; button.textContent = '⏳ 正在创建任务...'; }
            const result = await API.oss.syncFavorites();
            Toast.success(`${result.message || 'OSS 同步任务已创建'}，共 ${Number(result.total || 0)} 个作品`);
            Router.navigate('channels_oss_progress');
        } catch (error) {
            const message = error.message || String(error);
            if (message.includes('配置')) {
                Toast.warning('请先完成 OSS 配置');
                Router.navigate('channels_oss_config');
            } else {
                Toast.error(`OSS 同步启动失败：${message}`);
            }
        } finally {
            if (button) { button.disabled = false; button.textContent = '☁️ 一键同步 OSS'; }
        }
    },

    setFavoritesRefreshButton(running, runningText = '⏳ 正在刷新收藏...') {
        const button = document.getElementById('btn-refresh-all-favorites');
        if (!button) return;
        button.disabled = running;
        button.textContent = running ? runningText : '⚡ 一键刷新全部收藏';
    },

    updateFavoritesRefreshStatus(status) {
        const container = document.getElementById('favorites-refresh-status');
        const text = document.getElementById('favorites-refresh-status-text');
        const progressText = document.getElementById('favorites-refresh-progress-text');
        const progressBar = document.getElementById('favorites-refresh-progress-bar');
        const detail = document.getElementById('favorites-refresh-detail');
        if (!container || !text || !progressText || !progressBar || !detail) return;

        const total = Number(status.total_authors || 0);
        const completed = Number(status.completed_authors || 0);
        const failed = Number(status.failed_authors || 0);
        const processed = Math.min(total, completed + failed);
        const percent = total > 0 ? Math.round((processed / total) * 100) : 0;

        container.style.display = 'block';
        container.dataset.state = status.status || '';
        text.textContent = status.message || '正在刷新收藏创作者...';
        progressText.textContent = `${processed}/${total}`;
        progressBar.style.width = `${percent}%`;
        if (status.status === 'preparing') {
            detail.textContent = '正在启动同步助手、检测微信进程并打开视频号入口。';
        } else if (status.status === 'waiting') {
            detail.textContent = '视频号已自动打开，正在等待微信页面接收刷新任务。';
        } else if (status.status === 'failed' || status.status === 'cancelled') {
            detail.textContent = '请根据上方提示处理异常后重试。';
        } else {
            detail.textContent = `已同步作品 ${Number(status.total_videos || 0)} 个${status.current_nickname ? ` · 当前：${status.current_nickname}` : ''}`;
        }

        if (status.status === 'failed' || status.status === 'cancelled') {
            container.style.background = 'rgba(245,87,108,0.06)';
            container.style.borderColor = 'rgba(245,87,108,0.2)';
        } else {
            container.style.background = 'rgba(7,193,96,0.06)';
            container.style.borderColor = 'rgba(7,193,96,0.16)';
        }
    },

    pollFavoritesRefresh(taskId) {
        if (this.refreshPollTimer) {
            clearTimeout(this.refreshPollTimer);
            this.refreshPollTimer = null;
        }

        const poll = async () => {
            if (this.activeRefreshTaskId !== taskId) return;
            try {
                const status = await API.channels.getFavoritesRefreshStatus(taskId);
                this.updateFavoritesRefreshStatus(status);

                if (['completed', 'failed', 'cancelled'].includes(status.status)) {
                    this.activeRefreshTaskId = null;
                    this.setFavoritesRefreshButton(false);
                    try {
                        localStorage.removeItem('channelsFavoritesRefreshTaskId');
                    } catch (_) {}

                    if (status.status === 'completed') {
                        await this.loadFavorites();
                        if (Number(status.failed_authors || 0) > 0) {
                            Toast.warning(status.message || '刷新完成，部分作者失败');
                        } else {
                            Toast.success(status.message || '全部收藏创作者刷新完成');
                        }
                    } else {
                        Toast.error(status.message || '收藏创作者刷新失败');
                    }
                    return;
                }
                this.refreshPollTimer = setTimeout(poll, 1500);
            } catch (err) {
                this.activeRefreshTaskId = null;
                this.setFavoritesRefreshButton(false);
                try {
                    localStorage.removeItem('channelsFavoritesRefreshTaskId');
                } catch (_) {}
                Toast.error('查询刷新进度失败: ' + (err.message || '未知错误'));
            }
        };

        poll();
    },

    async loadFavorites() {
        try {
            const favs = await API.channels.getFavorites();
            this.favorites = favs || [];
            this.renderFavoritesGrid();
        } catch (err) {
            console.error('获取收藏列表失败:', err);
            Toast.error('获取收藏列表失败');
        }
    },

    renderFavoritesGrid() {
        const grid = document.getElementById('favorites-grid');
        const empty = document.getElementById('favorites-empty');
        const badge = document.getElementById('favorites-count-badge');

        if (badge) badge.textContent = this.favorites.length;

        if (!grid || !empty) return;

        if (this.favorites.length === 0) {
            grid.style.display = 'none';
            empty.style.display = 'block';
            return;
        }

        empty.style.display = 'none';
        grid.style.display = 'grid';

        const defaultAvatar = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23888'><path d='M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z'/></svg>";

        grid.innerHTML = this.favorites.map(fav => {
            return `
                <div class="favorite-card card" 
                     style="display: flex; flex-direction: column; justify-content: space-between; padding: var(--spacing-md); cursor: pointer; transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s; position: relative; border-radius: 12px; overflow: hidden; border: 1.5px solid var(--border-color); background: var(--bg-card);"
                     onmouseenter="this.style.transform='translateY(-4px)'; this.style.borderColor='var(--primary)'; this.style.boxShadow='var(--shadow-md)';"
                     onmouseleave="this.style.transform=''; this.style.borderColor='var(--border-color)'; this.style.boxShadow='';"
                     onclick="if(event.target.closest('.action-btn')) return; Router.navigate('channels_user?username=${this.esc(fav.username)}')">
                    
                    <div style="display: flex; gap: var(--spacing-sm); align-items: center;">
                        <img src="${this.esc(fav.head_img_url)}" alt="${this.esc(fav.nickname)}" 
                             style="width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 1.5px solid rgba(0,0,0,0.05);" 
                             onerror="this.src='${defaultAvatar}'">
                        <div style="flex: 1; overflow: hidden;">
                            <h4 style="margin: 0; font-size: 1.05rem; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${this.esc(fav.nickname)}</h4>
                            <p style="margin: 4px 0 0 0; font-family: monospace; font-size: 0.75rem; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${this.esc(fav.username)}">ID: ${this.esc(fav.username)}</p>
                        </div>
                    </div>

                    <div style="display: flex; gap: var(--spacing-xs); justify-content: flex-end; margin-top: var(--spacing-md); border-top: 1px solid var(--border-color); padding-top: var(--spacing-sm);">
                        <button class="btn btn-secondary btn-sm action-btn" onclick="ChannelsAccountsPage.removeFavoriteConfirm('${this.esc(fav.username)}', '${this.esc(fav.nickname)}')" style="font-size: 0.8rem; padding: 4px 10px; border-radius: 6px; color: var(--error); border-color: rgba(245,87,108,0.2);">
                            🗑️ 删除作者
                        </button>
                        <button class="btn btn-primary btn-sm action-btn" onclick="Router.navigate('channels_user?username=${this.esc(fav.username)}')" style="font-size: 0.8rem; padding: 4px 12px; border-radius: 6px; font-weight: 500;">
                            进入主页 ➔
                        </button>
                    </div>
                </div>
            `;
        }).join('');
    },

    removeFavoriteConfirm(username, nickname) {
        Modal.open({
            title: '删除作者确认',
            content: `<p style="color: var(--text-secondary)">您确定要删除创作者“${this.esc(nickname)}”吗？<br><span style="font-size:0.8rem;color:var(--text-muted);display:block;margin-top:6px;">注意：确认后将删除该作者及其所有已同步的本地作品数据。</span></p>`,
            footer: `
                <button class="btn btn-secondary" onclick="Modal.close()">取消</button>
                <button class="btn btn-danger" id="confirm-delete-btn" style="font-weight: 500;">确认删除</button>
            `
        });
        const confirmBtn = document.getElementById('confirm-delete-btn');
        if (confirmBtn) {
            confirmBtn.onclick = async () => {
                Modal.close();
                try {
                    await API.channels.removeFavorite(username);
                    Toast.success('删除作者成功');
                    await this.loadFavorites();
                } catch (err) {
                    Toast.error('删除作者失败: ' + err.message);
                }
            };
        }
    },

    esc(s) {
        if (!s) return "";
        const div = document.createElement("div");
        div.textContent = s;
        return div.innerHTML;
    },

};
