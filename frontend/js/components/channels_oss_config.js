/** Configurable S3 endpoint, credentials and storage bucket. */
const ChannelsOSSConfigPage = {
    endpoint: 'https://minio.fandow.com',
    savedAccessKeyId: '',
    hasSavedSecret: false,

    render() {
        return `
            <div class="page-header animate-fade-in">
                <h2 class="page-title">OSS 配置</h2>
                <p class="page-description">配置微信视频号作品同步使用的 OSS 存储桶和访问凭证。配置保存在当前系统用户目录，更新或重装软件后仍可继续使用。</p>
            </div>
            <div class="card animate-fade-in" style="max-width: 760px; margin-top: var(--spacing-lg);">
                <div class="card-header" style="border-bottom: 1px solid var(--border-color); padding-bottom: var(--spacing-md); margin-bottom: var(--spacing-lg);">
                    <h3 class="card-title" style="margin:0;">☁️ OSS 访问凭证</h3>
                </div>
                <div class="form-group">
                    <label class="form-label" for="oss-endpoint">OSS 服务域名</label>
                    <input id="oss-endpoint" class="form-input" type="text" maxlength="2048" autocomplete="off" spellcheck="false" value="https://minio.fandow.com" placeholder="minio.fandow.com" oninput="ChannelsOSSConfigPage.updateStoragePreview()">
                    <div class="form-hint" style="margin-top:7px;">默认 minio.fandow.com；不带协议时使用 HTTPS，可包含端口，不包含通配符或路径。</div>
                </div>
                <div class="form-group" style="margin-top: var(--spacing-md);">
                    <label class="form-label" for="oss-access-key-id">OSS_ACCESS_KEY_ID</label>
                    <input id="oss-access-key-id" class="form-input" type="text" maxlength="256" autocomplete="off" spellcheck="false" value="marketing-video-dashboard" oninput="ChannelsOSSConfigPage.updateStoragePreview()">
                </div>
                <div class="form-group" style="margin-top: var(--spacing-md);">
                    <label class="form-label" for="oss-bucket">OSS_BUCKET</label>
                    <input id="oss-bucket" class="form-input" type="text" maxlength="256" autocomplete="off" spellcheck="false" value="marketing-video-dashboard" placeholder="请输入存储桶名称" oninput="ChannelsOSSConfigPage.updateStoragePreview()">
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
                    存储地址预览（保存后生效）：<code id="oss-storage-base-url" style="overflow-wrap:anywhere;">https://minio.fandow.com/marketing-video-dashboard</code><br>
                    OSS_BUCKET 决定存储桶路径，Access Key 和 Secret 用于访问认证。对应存储桶需已存在且密钥有访问权限，软件不会自动创建。新配置用于后续上传，已有视频链接保持不变。<br>
                    OSS_ACCESS_KEY_SECRET 仅发送给本机服务并保存在当前用户配置目录，接口不会返回明文。
                    仅更换域名或存储桶时可沿用已保存的 Secret。
                </div>
                <div id="oss-config-status" style="display:none; margin-top: var(--spacing-md); padding: 10px 12px; border-radius: 8px; font-size: 0.88rem;"></div>
                <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top: var(--spacing-lg);">
                    <button class="btn btn-primary" id="btn-save-oss-config" onclick="ChannelsOSSConfigPage.save()">保存配置</button>
                    <button class="btn btn-secondary" onclick="ChannelsOSSConfigPage.load()">重新读取</button>
                    <button class="btn btn-secondary" id="btn-test-oss-connection" onclick="ChannelsOSSConfigPage.testConnection()">验证 OSS 可用性</button>
                    <button class="btn btn-danger" id="btn-clear-oss-config" onclick="ChannelsOSSConfigPage.clear()">清除配置</button>
                </div>
                <p class="form-hint" style="margin-top:12px;">验证使用当前表单及已保存的凭据，不自动保存配置；会上传一个临时小文件、验证公开读取后清理，不修改已有视频。</p>
            </div>
        `;
    },

    async init() { await this.load(); },
    async onShow() { await this.load(); },

    isValidConfigName(value) {
        return value.length <= 256 && /^[A-Za-z0-9._-]+$/.test(value) && value !== '.' && value !== '..';
    },

    normalizeEndpoint(value) {
        const input = String(value || '').trim();
        if (!input || input.length > 2048 || /[\s\\?#@]/.test(input)) throw new Error('请填写有效的 OSS 服务域名');
        const url = new URL(input.includes('://') ? input : `https://${input}`);
        const hostname = url.hostname.replace(/\.$/, '');
        if (!['http:', 'https:'].includes(url.protocol) || url.pathname !== '/' || url.search || url.hash || url.username || url.password || !hostname) throw new Error('OSS 服务域名不包含路径、账号或参数');
        if (!hostname.startsWith('[') && (hostname.length > 253 || hostname.split('.').some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)))) throw new Error('OSS 服务域名不能包含通配符或无效字符');
        url.hostname = hostname;
        return url.origin;
    },

    updateStoragePreview() {
        const accessKeyId = document.getElementById('oss-access-key-id')?.value.trim() || '';
        const bucket = document.getElementById('oss-bucket')?.value.trim() || '';
        const preview = document.getElementById('oss-storage-base-url');
        try {
            const endpoint = this.normalizeEndpoint(document.getElementById('oss-endpoint')?.value);
            if (preview) preview.textContent = this.isValidConfigName(bucket) ? `${endpoint}/${bucket}`
                : '请填写有效的 OSS_BUCKET（字母、数字、点、下划线、连字符；不能为 . 或 ..）';
        } catch (error) { if (preview) preview.textContent = '请填写有效的 OSS 服务域名（不包含路径或通配符）'; }
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
            this.endpoint = config.endpoint || 'https://minio.fandow.com';
            const endpointInput = document.getElementById('oss-endpoint');
            if (endpointInput) endpointInput.value = this.endpoint;
            this.savedAccessKeyId = config.access_key_id || 'marketing-video-dashboard';
            this.hasSavedSecret = !!config.has_secret;
            const idInput = document.getElementById('oss-access-key-id');
            const bucketInput = document.getElementById('oss-bucket');
            const secretInput = document.getElementById('oss-access-key-secret');
            const hint = document.getElementById('oss-secret-hint');
            if (idInput) idInput.value = this.savedAccessKeyId;
            if (bucketInput) bucketInput.value = config.bucket ?? this.savedAccessKeyId;
            if (secretInput) {
                secretInput.value = '';
            }
            this.updateStoragePreview();
            if (hint) hint.textContent = config.has_secret
                ? '已保存 Secret，不会在页面中回显；更换 ID 时需填写对应 Secret。'
                : '尚未配置 OSS_ACCESS_KEY_SECRET。';
            this.showStatus(
                config.configuration_error || (config.configured ? 'OSS 配置已保存，请验证实际可用性' : '请填写并保存 OSS 配置'),
                config.configuration_error ? 'error' : 'normal',
            );
        } catch (error) {
            this.showStatus(`读取失败：${error.message || error}`, 'error');
        }
    },

    collectConfig() {
        const accessKeyId = document.getElementById('oss-access-key-id')?.value.trim() || '';
        const accessKeySecret = document.getElementById('oss-access-key-secret')?.value.trim() || '';
        const bucket = document.getElementById('oss-bucket')?.value.trim() || '';
        if (!accessKeyId) { Toast.warning('请填写 OSS_ACCESS_KEY_ID'); return null; }
        if (!this.isValidConfigName(accessKeyId)) { Toast.warning('OSS_ACCESS_KEY_ID 只能包含字母、数字、点、下划线和连字符，且不能为 . 或 ..'); return null; }
        if (!this.isValidConfigName(bucket)) { Toast.warning('请填写有效的 OSS_BUCKET（字母、数字、点、下划线、连字符；不能为 . 或 ..）'); return null; }
        if (this.hasSavedSecret && accessKeyId !== this.savedAccessKeyId && !accessKeySecret) {
            Toast.warning('ID 已变更，请填写对应的 OSS_ACCESS_KEY_SECRET');
            return null;
        }
        try {
            const endpoint = this.normalizeEndpoint(document.getElementById('oss-endpoint')?.value);
            return {access_key_id: accessKeyId, access_key_secret: accessKeySecret, bucket, endpoint};
        } catch (error) { Toast.warning('请填写有效的 OSS 服务域名（不包含路径或通配符）'); return null; }
    },

    async save() {
        const button = document.getElementById('btn-save-oss-config');
        const config = this.collectConfig();
        if (!config) return;
        try {
            if (button) { button.disabled = true; button.textContent = '保存中...'; }
            const result = await API.oss.saveConfig(config.access_key_id, config.access_key_secret, config.bucket, config.endpoint);
            Toast.success(result.message || 'OSS 配置已保存');
            await this.load();
        } catch (error) {
            this.showStatus(`保存失败：${error.message || error}`, 'error');
        } finally {
            if (button) { button.disabled = false; button.textContent = '保存配置'; }
        }
    },

    async testConnection() {
        const config = this.collectConfig();
        if (!config) return;
        const button = document.getElementById('btn-test-oss-connection');
        try {
            if (button) { button.disabled = true; button.textContent = '验证中...'; }
            this.showStatus('正在验证上传、公开读取和临时文件清理…', 'normal');
            const result = await API.oss.testConnection(config);
            this.showStatus(result.message || 'OSS 验证通过', 'normal');
            Toast.success(result.message || 'OSS 验证通过');
        } catch (error) { this.showStatus(`验证失败：${error.message || error}`, 'error'); }
        finally { if (button) { button.disabled = false; button.textContent = '验证 OSS 可用性'; } }
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
