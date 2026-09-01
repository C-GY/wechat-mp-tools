/**
 * 自媒体内容采集工具 — 全局应用管理器
 */
const App = {
    ffmpegAvailable: true,
    isDouyinLoggedIn: false,
    douyinAccountInfo: null,

    async init() {
        console.log('App initializing...');

        // 初始化基础组件
        Toast.init();
        Modal.init();

        // 固定侧边导航顺序，并移除历史 DOM 中遗留的分隔节点。
        this.orderNavigationGroups();

        // 首次加载认证状态（非阻塞，不影响页面首屏渲染速度）
        this.checkAuthStatus();

        // 检查 ffmpeg 可用状态
        try {
            await this.checkFFmpegStatus();
        } catch (e) {
            console.error('Failed to run initial checkFFmpegStatus:', e);
        }

        // 初始化路由
        Router.init();

        // 初始化侧边栏手风琴折叠菜单
        this.initSidebarAccordion();

        // 启动定时状态检查 (每 30 秒检查一次登录态)
        setInterval(() => this.checkAuthStatus(), 30000);

    },

    orderNavigationGroups() {
        const nav = document.querySelector('.sidebar-nav');
        if (!nav) return;
        nav.querySelectorAll(':scope > .nav-divider').forEach(divider => divider.remove());
        const order = [
            'wechat_channels', 'douyin', 'kuaishou', 'xiaohongshu',
            'bilibili', 'wechat', 'common',
        ];
        order.forEach((group, index) => {
            const title = nav.querySelector(`:scope > .nav-group-title[data-group="${group}"]`);
            const items = document.getElementById(`items-${group}`);
            if (!title || !items) return;
            if (index > 0) {
                const divider = document.createElement('div');
                divider.className = 'nav-divider';
                nav.appendChild(divider);
            }
            nav.appendChild(title);
            nav.appendChild(items);
        });
    },

    async checkAuthStatus() {
        try {
            // 微信登录状态
            const wechatData = await API.auth.status();
            
            // 抖音登录状态
            const dyRes = await fetch('/api/douyin-auth/status');
            const dyText = await dyRes.text();
            let dyData = {};
            try {
                dyData = JSON.parse(dyText);
            } catch (e) {
                dyData = { logged_in: false };
            }
            
            // 小红书登录状态
            let xhsData = { logged_in: false };
            try {
                xhsData = await API.xhs.auth.status();
            } catch (e) {
                // ignore
            }

            // B站登录状态
            let biliData = { logged_in: false };
            try {
                biliData = await API.bili.auth.status();
            } catch (e) {
                // ignore
            }
            
            const prevLoggedIn = this.isDouyinLoggedIn;
            this.isDouyinLoggedIn = !!dyData.logged_in;
            this.douyinAccountInfo = dyData.account_info || null;
            
            this.updateLoginStatus(
                wechatData.logged_in, 
                wechatData.expired || wechatData.may_expired,
                this.isDouyinLoggedIn,
                !!xhsData.logged_in,
                !!biliData.logged_in
            );

            if (this.isDouyinLoggedIn && !prevLoggedIn) {
                const promptEl = document.getElementById('dy-login-prompt-page');
                if (promptEl && promptEl.style.display === 'block') {
                    promptEl.style.display = 'none';
                    Router.refreshCurrent();
                }
            }

            // 账号池踢出事件轮询（全局提示）
            try {
                const evData = await API.accountPool.events();
                if (evData.events && evData.events.length > 0) {
                    for (const ev of evData.events) {
                        Toast.warning(`账号【${ev.nickname || '未知'}】${ev.reason}，已被移出账号池`);
                    }
                }
            } catch (e) { /* silent */ }
        } catch (err) {
            console.error('Failed to fetch auth status:', err);
            this.updateLoginStatus(false, false, false);
        }
    },

    async checkFFmpegStatus() {
        try {
            const data = await API.transcode.checkFFmpeg();
            const transcodeNav = document.getElementById('nav-transcode');
            if (transcodeNav) transcodeNav.style.display = 'flex';
            this.ffmpegAvailable = !!(data && data.available);
        } catch (err) {
            console.error('Failed to check ffmpeg status:', err);
            const transcodeNav = document.getElementById('nav-transcode');
            if (transcodeNav) transcodeNav.style.display = 'flex';
            this.ffmpegAvailable = false;
        }
    },

    updateLoginStatus(loggedIn, mayExpired = false, dyLoggedIn = false, xhsLoggedIn = false, biliLoggedIn = false) {
        const wechatDot = document.getElementById('login-status-dot');
        const wechatIndicator = document.getElementById('status-indicator');
        const wechatText = document.getElementById('status-text');

        const dyIndicator = document.getElementById('dy-status-indicator');
        const dyText = document.getElementById('dy-status-text');

        const xhsIndicator = document.getElementById('xhs-status-indicator');
        const xhsText = document.getElementById('xhs-status-text');

        const biliIndicator = document.getElementById('bili-status-indicator');
        const biliText = document.getElementById('bili-status-text');

        // 更新微信状态显示
        if (loggedIn) {
            if (mayExpired) {
                if (wechatIndicator) wechatIndicator.className = 'status-dot expired';
                if (wechatText) wechatText.textContent = '登录过期';
                if (wechatDot) wechatDot.style.backgroundColor = 'var(--warning)';
            } else {
                if (wechatIndicator) wechatIndicator.className = 'status-dot online';
                if (wechatText) wechatText.textContent = '已登录';
                if (wechatDot) wechatDot.style.backgroundColor = 'var(--success)';
            }
        } else {
            if (wechatIndicator) wechatIndicator.className = 'status-dot offline';
            if (wechatText) wechatText.textContent = '未登录';
            if (wechatDot) wechatDot.style.backgroundColor = 'transparent';
        }

        // 更新抖音状态显示
        if (dyLoggedIn) {
            if (dyIndicator) dyIndicator.className = 'status-dot online';
            if (dyText) dyText.textContent = 'Cookie 已配置';
        } else {
            if (dyIndicator) dyIndicator.className = 'status-dot warning';
            if (dyText) dyText.textContent = '需要登录 Cookie';
        }

        // 更新小红书状态显示
        if (xhsLoggedIn) {
            if (xhsIndicator) xhsIndicator.className = 'status-dot online';
            if (xhsText) xhsText.textContent = 'Cookie 已配置';
        } else {
            if (xhsIndicator) xhsIndicator.className = 'status-dot warning';
            if (xhsText) xhsText.textContent = '需要登录 Cookie';
        }

        // 更新B站状态显示
        if (biliLoggedIn) {
            if (biliIndicator) biliIndicator.className = 'status-dot online';
            if (biliText) biliText.textContent = 'Cookie 已配置';
        } else {
            if (biliIndicator) biliIndicator.className = 'status-dot warning';
            if (biliText) biliText.textContent = '需要登录 Cookie';
        }
    },

    initSidebarAccordion() {
        const headers = document.querySelectorAll('.nav-group-title');
        headers.forEach(header => {
            header.addEventListener('click', () => {
                const group = header.getAttribute('data-group');
                if (group) {
                    this.toggleNavGroup(group);
                }
            });
        });
    },

    toggleNavGroup(targetGroup) {
        const groups = ['wechat', 'wechat_channels', 'douyin', 'xiaohongshu', 'kuaishou', 'bilibili', 'common'];
        groups.forEach(g => {
            const itemsEl = document.getElementById(`items-${g}`);
            const titleEl = document.querySelector(`.nav-group-title[data-group="${g}"]`);
            const arrowEl = titleEl ? titleEl.querySelector('.group-arrow') : null;
            
            if (g === targetGroup) {
                if (itemsEl) {
                    const isCollapsed = itemsEl.classList.toggle('collapsed');
                    if (arrowEl) {
                        arrowEl.textContent = isCollapsed ? '▶' : '▼';
                    }
                }
            } else {
                if (itemsEl) {
                    itemsEl.classList.add('collapsed');
                    if (arrowEl) {
                        arrowEl.textContent = '▶';
                    }
                }
            }
        });
    },

    async downloadChannelsVideo(videoUrl, description, createtime, decryptKey, onSuccessCallback) {
        if (!videoUrl) {
            Toast.error('下载链接无效');
            return;
        }

        let taskId = null;
        let isDownloading = true;

        Toast.info('已开始视频下载任务，请稍候...');

        Modal.open({
            title: '📥 正在下载视频到本地',
            preventClose: true,
            content: `
                <div style="padding: 10px 0;">
                    <p style="font-size: 0.95rem; color: var(--text-secondary); margin-bottom: 20px; line-height: 1.5; word-break: break-all;">
                        视频: <strong style="color: var(--text-primary); font-size: 1rem;">${this.esc(description || '视频号视频')}</strong>
                    </p>
                    <div style="background: #eee; border-radius: 8px; height: 16px; overflow: hidden; margin-bottom: 12px; position: relative;">
                        <div id="single-download-progress-bar" style="background: var(--primary); height: 100%; width: 0%; transition: width 0.3s; border-radius: 8px;"></div>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 0.9rem; margin-bottom: var(--spacing-md);">
                        <span id="single-download-progress-text" style="font-weight: 500; color: var(--text-primary);">正在连接视频服务器...</span>
                        <span id="single-download-progress-percent" style="font-weight: 600; color: var(--primary);">0%</span>
                    </div>
                </div>
            `,
            footer: `
                <button class="btn btn-secondary" id="btn-single-download-cancel" style="background: #ff3b30; color: white; border-color: rgba(255,59,48,0.2); font-weight: 500;">终止下载</button>
            `,
            onClose: () => {
                isDownloading = false;
                if (taskId) {
                    API.channels.cancelDownload(taskId).catch(e => console.error("Cancel failed:", e));
                }
            }
        });

        try {
            const startRes = await API.channels.downloadAsync(videoUrl, description, createtime, decryptKey);
            if (!startRes.success || !startRes.task_id) {
                throw new Error(startRes.error || '无法启动下载任务');
            }
            taskId = startRes.task_id;
        } catch (err) {
            isDownloading = false;
            Modal.close();
            Toast.error('启动下载失败: ' + err.message);
            return;
        }

        const cancelBtn = document.getElementById('btn-single-download-cancel');
        if (cancelBtn) {
            cancelBtn.onclick = async () => {
                cancelBtn.disabled = true;
                cancelBtn.textContent = '正在取消...';
                try {
                    await API.channels.cancelDownload(taskId);
                } catch (e) {
                    console.error(e);
                }
            };
        }

        const pollInterval = setInterval(async () => {
            if (!isDownloading) {
                clearInterval(pollInterval);
                return;
            }

            try {
                const res = await API.channels.getDownloadStatus(taskId);
                if (res.status === 'downloading') {
                    const pct = res.progress || 0;
                    const pb = document.getElementById('single-download-progress-bar');
                    const pt = document.getElementById('single-download-progress-text');
                    const pp = document.getElementById('single-download-progress-percent');
                    if (pb) pb.style.width = `${pct}%`;
                    if (pp) pp.textContent = `${pct}%`;
                    if (pt) pt.textContent = `已下载 ${pct}%`;
                } else if (res.status === 'success') {
                    clearInterval(pollInterval);
                    isDownloading = false;
                    Modal.close();
                    Toast.success('视频下载成功！');
                    if (onSuccessCallback) {
                        onSuccessCallback(res.result);
                    }
                } else if (res.status === 'failed') {
                    clearInterval(pollInterval);
                    isDownloading = false;
                    Modal.close();
                    Toast.error('下载失败: ' + (res.error || '未知错误'));
                } else if (res.status === 'cancelled') {
                    clearInterval(pollInterval);
                    isDownloading = false;
                    Modal.close();
                    Toast.info('下载已取消');
                }
            } catch (err) {
                console.error("Polling error:", err);
            }
        }, 1000);
    },

    async ensureFFmpeg(onSuccess) {
        try {
            const check = await API.transcode.checkFFmpeg();
            if (check && check.available) {
                if (onSuccess) onSuccess();
                return;
            }
        } catch (e) {
            console.error('Check FFmpeg failed', e);
        }

        Modal.open({
            title: '⚠️ 缺少 FFmpeg 运行环境',
            preventClose: false,
            content: `
                <div style="padding: 10px 0;">
                    <p style="font-size: 0.92rem; color: var(--text-secondary); line-height: 1.5; margin-bottom: var(--spacing-md);">
                        系统未检测到 FFmpeg 环境。B站视频下载、音频合并和视频转码需要本地安装有 FFmpeg 才能正常运行。
                    </p>
                    <p style="font-size: 0.92rem; color: var(--text-secondary); line-height: 1.5; margin-bottom: 20px;">
                        为了保持软件打包体积轻量化，我们不默认捆绑 FFmpeg。您可以点击下方按钮<strong>一键下载并配置静态 FFmpeg 环境</strong>（大小约 15MB）。
                    </p>
                    <div id="ffmpeg-dl-progress-container" style="display: none; margin-bottom: 12px;">
                        <div style="font-size: 0.88rem; margin-bottom: 6px; color: var(--text-primary); font-weight: 500;" id="ffmpeg-dl-status-text">正在初始化下载...</div>
                        <div style="background: var(--border-color); border-radius: 6px; height: 12px; overflow: hidden; position: relative;">
                            <div id="ffmpeg-dl-bar" style="background: var(--primary); height: 100%; width: 0%; transition: width 0.3s; border-radius: 6px;"></div>
                        </div>
                    </div>
                </div>
            `,
            footer: `
                <button class="btn btn-secondary" id="btn-ffmpeg-dl-cancel" onclick="Modal.close()">暂不配置</button>
                <button class="btn btn-primary" id="btn-ffmpeg-dl-start">一键下载配置</button>
            `,
            onOpen: () => {
                const btnStart = document.getElementById('btn-ffmpeg-dl-start');
                const btnCancel = document.getElementById('btn-ffmpeg-dl-cancel');
                if (btnStart) {
                    btnStart.onclick = async () => {
                        btnStart.disabled = true;
                        btnCancel.disabled = true;
                        document.getElementById('ffmpeg-dl-progress-container').style.display = 'block';
                        
                        try {
                            await API.transcode.downloadFFmpeg();
                            this.pollFFmpegDownloadProgress(onSuccess);
                        } catch (err) {
                            Toast.error('启动下载失败: ' + err.message);
                            btnStart.disabled = false;
                            btnCancel.disabled = false;
                        }
                    };
                }
            }
        });
    },

    pollFFmpegDownloadProgress(onSuccess) {
        const text = document.getElementById('ffmpeg-dl-status-text');
        const bar = document.getElementById('ffmpeg-dl-bar');
        const btnStart = document.getElementById('btn-ffmpeg-dl-start');
        const btnCancel = document.getElementById('btn-ffmpeg-dl-cancel');
        
        let pollId = setInterval(async () => {
            try {
                const st = await API.transcode.downloadFFmpegStatus();
                
                if (st.status === 'downloading') {
                    if (text) text.textContent = `正在下载 FFmpeg / FFprobe ... ${st.progress}%`;
                    if (bar) bar.style.width = `${st.progress}%`;
                } else if (st.status === 'unzipping') {
                    if (text) text.textContent = `正在解压缩并配置环境...`;
                    if (bar) bar.style.width = `95%`;
                } else if (st.status === 'completed') {
                    clearInterval(pollId);
                    if (text) text.textContent = `配置成功！即将自动刷新...`;
                    if (bar) bar.style.width = `100%`;
                    Toast.success('FFmpeg 环境下载并配置成功！');
                    await this.checkFFmpegStatus();
                    setTimeout(() => {
                        Modal.close();
                        window.location.reload();
                    }, 1200);
                } else if (st.status === 'failed') {
                    clearInterval(pollId);
                    if (text) text.textContent = `配置失败: ${st.error || '未知错误'}`;
                    Toast.error('配置 FFmpeg 失败: ' + (st.error || '网络超时'));
                    if (btnStart) btnStart.disabled = false;
                    if (btnCancel) btnCancel.disabled = false;
                }
            } catch (err) {
                console.error('Poll FFmpeg progress failed', err);
            }
        }, 1000);
    },

    showDisclaimer() {
        Modal.open({
            title: '免责声明',
            content: `
                <div style="font-size: 0.9rem; line-height: 1.6; color: var(--text-secondary);">
                    <p style="margin-bottom: 12px; font-weight: 500; color: var(--text-primary);">在使用本工具前，请仔细阅读以下免责声明：</p>
                    <p style="margin-top: 8px; margin-bottom: 8px; text-indent: -1.2em; padding-left: 1.2em;">1. 本项目所有功能仅用于个人学习、研究与本地备份，请勿用于任何商业用途或非法牟利。</p>
                    <p style="margin-top: 8px; margin-bottom: 8px; text-indent: -1.2em; padding-left: 1.2em;">2. 使用本工具下载资源时需遵守平台的用户服务协议及相关法律法规。用户因滥用本工具造成的账号风控、限制或法律纠纷，由用户本人承担，与本项目作者无关。</p>
                    <p style="margin-top: 8px; margin-bottom: 8px; text-indent: -1.2em; padding-left: 1.2em;">3. 本项目为开源软件，不提供任何形式的担保或售后承诺。</p>
                </div>
            `,
            footer: '<button class="btn btn-primary" onclick="Modal.close()" style="width: 100%;">我已阅知</button>'
        });
    },

    esc(s) {
        if (!s) return "";
        const div = document.createElement("div");
        div.textContent = s;
        return div.innerHTML;
    }
};

// 页面加载完成后启动应用
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});
