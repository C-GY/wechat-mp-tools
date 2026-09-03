/** Shared JSON clipboard backups for the two synchronization hubs. */
const HubConfigTransfer = {
    async copyText(text) {
        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(text);
                return;
            }
        } catch (_) {
            // Desktop webviews and HTTP pages may need the selection fallback.
        }
        const previousFocus = document.activeElement;
        const input = document.createElement('textarea');
        input.value = text;
        input.readOnly = true;
        input.style.cssText = 'position:fixed; left:-9999px; top:0;';
        document.body.appendChild(input);
        try {
            input.focus();
            input.select();
            if (!document.execCommand('copy')) throw new Error('无法自动复制到剪贴板');
        } finally {
            input.remove();
            if (previousFocus?.isConnected) previousFocus.focus();
        }
    },

    async exportConfig({ api, label, buttonId }) {
        const button = document.getElementById(buttonId);
        if (button?.disabled) return;
        const idleText = button?.textContent;
        if (button) { button.disabled = true; button.textContent = '正在导出...'; }
        try {
            const backup = await api.exportConfig();
            const text = JSON.stringify(backup, null, 2);
            try {
                await this.copyText(text);
                Toast.success(`${label}配置 JSON 已复制到剪贴板`);
            } catch (_) {
                this.showCopyDialog(label, text);
            }
        } catch (error) {
            Toast.error(`导出配置失败：${error.message || error}`);
        } finally {
            if (button) { button.disabled = false; button.textContent = idleText; }
        }
    },

    showCopyDialog(label, text) {
        Modal.open({
            title: `${label} · 导出配置`,
            content: `
                <p id="hub-config-copy-message" role="status" style="color:var(--text-secondary);">自动复制未成功，可点击“复制 JSON”重试，或选中内容后按 Ctrl+C（macOS 为 ⌘C）。</p>
                <textarea id="hub-config-export-json" class="form-input" aria-label="导出的配置 JSON" rows="12" readonly spellcheck="false" style="width:100%; box-sizing:border-box; font-family:monospace; resize:vertical;"></textarea>
            `,
            footer: '<button class="btn btn-secondary" onclick="Modal.close()">关闭</button><button class="btn btn-primary" id="hub-config-copy">复制 JSON</button>',
            onClose: () => { document.getElementById('hub-config-export-json').value = ''; },
        });
        const input = document.getElementById('hub-config-export-json');
        input.value = text;
        input.focus();
        input.select();
        const button = document.getElementById('hub-config-copy');
        button.onclick = async () => {
            button.disabled = true;
            try {
                await this.copyText(input.value);
                Toast.success(`${label}配置 JSON 已复制到剪贴板`);
                Modal.close();
            } catch (_) {
                input.focus();
                input.select();
                document.getElementById('hub-config-copy-message').textContent = '请按 Ctrl+C（macOS 为 ⌘C）复制已选中的 JSON。';
            } finally {
                button.disabled = false;
            }
        };
    },

    importConfig({ api, label, onImported }) {
        Modal.open({
            title: `${label} · 导入配置`,
            content: `
                <p style="color:var(--text-secondary);">粘贴本模块导出的完整 JSON。导入并保存后，将替换当前模块的连接、定时和飞书配置。</p>
                <label class="form-label" for="hub-config-import-json">配置 JSON</label>
                <textarea id="hub-config-import-json" class="form-input" rows="12" placeholder="在此粘贴配置 JSON" autocomplete="off" spellcheck="false" style="width:100%; box-sizing:border-box; font-family:monospace; resize:vertical;"></textarea>
                <div id="hub-config-import-error" role="alert" style="color:var(--error); margin-top:10px; white-space:pre-wrap;"></div>
            `,
            footer: '<button class="btn btn-secondary" onclick="Modal.close()">取消</button><button class="btn btn-primary" id="hub-config-import-submit">导入并保存</button>',
            onClose: () => { document.getElementById('hub-config-import-json').value = ''; },
        });
        const input = document.getElementById('hub-config-import-json');
        const errorBox = document.getElementById('hub-config-import-error');
        const button = document.getElementById('hub-config-import-submit');
        input.focus();
        button.onclick = async () => {
            if (button.disabled) return;
            errorBox.textContent = '';
            const text = input.value.trim();
            if (!text) {
                errorBox.textContent = '请先粘贴配置 JSON';
                input.focus();
                return;
            }
            let payload;
            try {
                payload = JSON.parse(text);
            } catch (_) {
                errorBox.textContent = 'JSON 格式无效，请检查是否完整复制了配置内容';
                return;
            }
            const buttons = [...Modal.dialog.querySelectorAll('button')];
            buttons.forEach(item => { item.disabled = true; });
            input.readOnly = true;
            Modal.preventClose = true;
            button.textContent = '正在导入...';
            try {
                const result = await api.importConfig(payload);
                Modal.preventClose = false;
                Modal.close();
                Toast.success(result.message || `${label}配置已导入并保存`);
                await onImported();
            } catch (error) {
                errorBox.textContent = error.message || '导入失败，请重试';
            } finally {
                Modal.preventClose = false;
                buttons.forEach(item => { item.disabled = false; });
                input.readOnly = false;
                button.textContent = '导入并保存';
            }
        };
    },
};
