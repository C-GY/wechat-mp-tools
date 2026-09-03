/** OSS credentials and the storage bucket derived from the configured ID. */
const ChannelsOSSConfigPage = {
    endpoint: 'https://oss.fandow.com',
    savedAccessKeyId: '',
    hasSavedSecret: false,

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
                    <input id="oss-access-key-id" class="form-input" type="text" maxlength="256" autocomplete="off" spellcheck="false" value="marketing-video-dashboard" oninput="ChannelsOSSConfigPage.updateStoragePreview()">
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
                    存储地址预览（保存后生效）：<code id="oss-storage-base-url" style="overflow-wrap:anywhere;">https://oss.fandow.com/marketing-video-dashboard</code><br>
                    使用 OSS_ACCESS_KEY_ID 作为存储桶路径；对应存储桶需已存在且密钥有访问权限，软件不会自动创建。新配置用于后续上传，已有视频链接保持不变。<br>
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

    isValidAccessKeyId(value) {
        return value.length <= 256 && /^[A-Za-z0-9._-]+$/.test(value) && value !== '.' && value !== '..';
    },

    updateStoragePreview() {
        const accessKeyId = document.getElementById('oss-access-key-id')?.value.trim() || '';
        const preview = document.getElementById('oss-storage-base-url');
        if (preview) preview.textContent = this.isValidAccessKeyId(accessKeyId)
            ? `${this.endpoint}/${accessKeyId}`
            : '请填写有效的 OSS_ACCESS_KEY_ID（字母、数字、点、下划线、连字符；不能为 . 或 ..）';
        const secretInput = document.getElementById('oss-access-key-secret');
        if (secretInput) secretInput.placeholder = this.hasSavedSecret
            ? (accessKeyId === this.savedAccessKeyId ? '已保存；如不修改请留空' : 'ID 已变更，请填写对应的 Secret')
            : '请输入 OSS_ACCESS_KEY_SECRET';
    },

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
            this.endpoint = config.endpoint || 'https://oss.fandow.com';
            this.savedAccessKeyId = config.access_key_id || 'marketing-video-dashboard';
            this.hasSavedSecret = !!config.has_secret;
            const idInput = document.getElementById('oss-access-key-id');
            const secretInput = document.getElementById('oss-access-key-secret');
            const hint = document.getElementById('oss-secret-hint');
            if (idInput) idInput.value = this.savedAccessKeyId;
            if (secretInput) {
                secretInput.value = '';
            }
            this.updateStoragePreview();
            if (hint) hint.textContent = config.has_secret
                ? '已保存 Secret，不会在页面中回显；更换 ID 时需填写对应 Secret。'
                : '尚未配置 OSS_ACCESS_KEY_SECRET。';
            this.showStatus(
                config.configuration_error || (config.configured ? 'OSS 配置已就绪' : '请填写并保存 OSS 配置'),
                config.configuration_error ? 'error' : 'normal',
            );
        } catch (error) {
            this.showStatus(`读取失败：${error.message || error}`, 'error');
        }
    },

    async save() {
        const button = document.getElementById('btn-save-oss-config');
        const accessKeyId = document.getElementById('oss-access-key-id')?.value.trim() || '';
        const accessKeySecret = document.getElementById('oss-access-key-secret')?.value.trim() || '';
        if (!accessKeyId) return Toast.warning('请填写 OSS_ACCESS_KEY_ID');
        if (!this.isValidAccessKeyId(accessKeyId)) return Toast.warning('OSS_ACCESS_KEY_ID 只能包含字母、数字、点、下划线和连字符，且不能为 . 或 ..');
        if (this.hasSavedSecret && accessKeyId !== this.savedAccessKeyId && !accessKeySecret) {
            return Toast.warning('ID 已变更，请填写对应的 OSS_ACCESS_KEY_SECRET');
        }
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
