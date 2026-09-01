/** OSS credentials for the fixed WeChat Channels storage target. */
const ChannelsOSSConfigPage = {
    render() {
        return `
            <div class="page-header animate-fade-in">
                <h2 class="page-title">OSS 配置</h2>
                <p class="page-description">配置微信视频号作品同步使用的 OSS 凭证。配置保存在当前系统用户目录，更新或重装软件后仍可继续使用。</p>
            </div>
            <div class="card animate-fade-in" style="max-width: 760px; margin-top: var(--spacing-lg);">
                <div class="card-header" style="border-bottom: 1px solid var(--border-color); padding-bottom: var(--spacing-md); margin-bottom: var(--spacing-lg);">
                    <h3 class="card-title" style="margin:0;">☁️ OSS 访问凭证</h3>
                </div>
                <div class="form-group">
                    <label class="form-label" for="oss-access-key-id">OSS_ACCESS_KEY_ID</label>
                    <input id="oss-access-key-id" class="form-input" type="text" maxlength="256" autocomplete="off" spellcheck="false" value="marketing-video-dashboard">
                </div>
                <div class="form-group" style="margin-top: var(--spacing-md);">
                    <label class="form-label" for="oss-access-key-secret">OSS_ACCESS_KEY_SECRET</label>
                    <div style="display:flex; gap:8px;">
                        <input id="oss-access-key-secret" class="form-input" type="password" maxlength="1024" autocomplete="new-password" spellcheck="false" placeholder="请输入 OSS_ACCESS_KEY_SECRET" style="flex:1;">
                        <button class="btn btn-secondary" id="btn-toggle-oss-secret" type="button" onclick="ChannelsOSSConfigPage.toggleSecret()">显示</button>
                    </div>
                    <div id="oss-secret-hint" class="form-hint" style="margin-top:7px;">保存后不会在页面中回显 Secret。</div>
                </div>
                <div style="margin-top: var(--spacing-lg); padding: 12px 14px; border-radius: 10px; background: rgba(7,193,96,0.06); color: var(--text-secondary); font-size: 0.85rem; line-height: 1.7;">
                    固定存储地址：<code>https://oss.fandow.com/marketing-video-dashboard</code><br>
                    OSS_ACCESS_KEY_SECRET 仅发送给本机服务并保存在当前用户配置目录，接口不会返回明文。
                </div>
                <div id="oss-config-status" style="display:none; margin-top: var(--spacing-md); padding: 10px 12px; border-radius: 8px; font-size: 0.88rem;"></div>
                <div style="display:flex; gap:10px; margin-top: var(--spacing-lg);">
                    <button class="btn btn-primary" id="btn-save-oss-config" onclick="ChannelsOSSConfigPage.save()">保存配置</button>
                    <button class="btn btn-secondary" onclick="ChannelsOSSConfigPage.load()">重新读取</button>
                    <button class="btn btn-danger" id="btn-clear-oss-config" onclick="ChannelsOSSConfigPage.clear()">清除配置</button>
                </div>
            </div>
        `;
    },

    async init() { await this.load(); },
    async onShow() { await this.load(); },

    toggleSecret() {
        const input = document.getElementById('oss-access-key-secret');
        const button = document.getElementById('btn-toggle-oss-secret');
        if (!input) return;
        input.type = input.type === 'password' ? 'text' : 'password';
        if (button) button.textContent = input.type === 'password' ? '显示' : '隐藏';
    },

    showStatus(message, state = 'normal') {
        const element = document.getElementById('oss-config-status');
        if (!element) return;
        element.style.display = 'block';
        element.textContent = message;
        element.style.color = state === 'error' ? 'var(--error)' : 'var(--text-primary)';
        element.style.background = state === 'error' ? 'rgba(250,81,81,0.08)' : 'rgba(7,193,96,0.08)';
    },

    async load() {
        try {
            const config = await API.oss.getConfig();
            const idInput = document.getElementById('oss-access-key-id');
            const secretInput = document.getElementById('oss-access-key-secret');
            const hint = document.getElementById('oss-secret-hint');
            if (idInput) idInput.value = config.access_key_id || 'marketing-video-dashboard';
            if (secretInput) {
                secretInput.value = '';
                secretInput.placeholder = config.has_secret
                    ? '已保存；如不修改请留空'
                    : '请输入 OSS_ACCESS_KEY_SECRET';
            }
            if (hint) hint.textContent = config.configured
                ? '✅ 已保存 OSS 配置；Secret 不会在页面中回显。'
                : '尚未配置 OSS_ACCESS_KEY_SECRET。';
            this.showStatus(config.configured ? 'OSS 配置已就绪' : '请填写并保存 OSS 配置');
        } catch (error) {
            this.showStatus(`读取失败：${error.message || error}`, 'error');
        }
    },

    async save() {
        const button = document.getElementById('btn-save-oss-config');
        const accessKeyId = document.getElementById('oss-access-key-id')?.value.trim() || '';
        const accessKeySecret = document.getElementById('oss-access-key-secret')?.value.trim() || '';
        if (!accessKeyId) return Toast.warning('请填写 OSS_ACCESS_KEY_ID');
        try {
            if (button) { button.disabled = true; button.textContent = '保存中...'; }
            const result = await API.oss.saveConfig(accessKeyId, accessKeySecret);
            Toast.success(result.message || 'OSS 配置已保存');
            await this.load();
        } catch (error) {
            this.showStatus(`保存失败：${error.message || error}`, 'error');
        } finally {
            if (button) { button.disabled = false; button.textContent = '保存配置'; }
        }
    },

    async clear() {
        try {
            const result = await API.oss.clearConfig();
            Toast.success(result.message || 'OSS 配置已清除');
            await this.load();
        } catch (error) {
            this.showStatus(`清除失败：${error.message || error}`, 'error');
        }
    },
};
