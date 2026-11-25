/* Explicit management actions; background refreshes only read local snapshots. */
"use strict";
window.CompanionNativeExtensionManagement = (() => {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const qrSource = value => /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(String(value || "")) ? value : "";
  const cameraPreviewSource = value => /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(String(value || "")) ? value : "";
  const actionNames = {scan_camera:"扫描摄像头", consent_camera:"更新摄像头确认", update_camera_policy:"保存摄像头策略", connect:"连接 VTube Studio", request_auth:"请求 VTS 授权", list_hotkeys:"读取热键",
    scan_audio:"扫描音频设备", select_audio:"保存音频输出", test_audio:"播放设备测试音", request_audio_consent:"准备本人音频授权", revoke_audio_consent:"撤销音频授权",
    update_audio_policy:"保存语音策略", save:"保存早晨照料", disable:"关闭早晨照料", stop_session:"停止本次叫醒", test:"试听一次", cancel_reminder:"取消提醒",
    xiangqi_install:"下载 Pikafish 引擎", xiangqi_check:"检测象棋引擎",
    cloudflared_install:"下载 cloudflared", cloudflared_check:"检测 cloudflared",
    tunnel_start:"启动临时公网", tunnel_stop:"停止临时公网"};
  actionNames.test_camera = "测试摄像头调用";
  const errors = {
    native_extension_inactive:"模块已暂停，请先启用模块再操作。",
    native_management_unavailable:"管理操作尚未就绪，请重新读取状态。",
    native_extension_action_failed:"操作未完成，请核对当前连接和配置。",
    vts_not_connected:"VTube Studio 尚未连接，请先连接。",
    vts_not_authenticated:"VTube Studio 尚未授权，请请求授权并在 VTS 弹窗中点允许。",
    vts_auth_timeout:"等待授权超时。请在 VTube Studio 弹窗中点允许，再重新连接核对授权结果。",
    camera_user_ineligible:"此用户没有主机摄像头使用权限，请选择管理员或主要用户。",
    camera_consent_required:"请先确认该用户的摄像头授权，再保存启用策略。",
    camera_user_not_configured:"请先确认该用户的摄像头授权，再保存使用策略。",
    native_service_not_running:"此模块尚未运行，请启用模块并等待启动完成。",
    native_service_changed:"模块已停止或重新配置，请刷新当前状态。",
    native_reality_action_failed:"操作未完成，请核对现实设备配置和当前用户授权。",
    native_reality_playback_failed:"音频未能播放，请核对输出设备、TTS 与当前用户授权。",
    native_reality_reminder_not_cancelled:"提醒未取消，请刷新提醒状态。",
    native_management_result_unknown:"操作结果暂不可确认，请刷新状态核对后再决定下一步。",
    native_vts_authorization_denied:"VTube Studio 未允许此次授权，请重新请求并在弹窗中点允许。",
    native_vts_authorization_required:"请在 VTube Studio 开启 API，并点击请求 VTS 授权。",
    native_vts_action_timeout:"操作超时，请核对 VTube Studio 连接，并在授权弹窗中点允许后重新连接。",
    native_vts_action_failed:"VTube Studio 操作未完成，请检查连接和设备状态。",
    native_game_download_disabled:"当前配置已禁止自动下载，请先在下方设置中开启允许下载或填写本地路径。",
    native_game_configured_path_active:"已配置自定义象棋引擎路径，请先清空自定义路径再执行托管下载，或直接核对本地文件。",
    native_game_xiangqi_install_failed:"Pikafish 象棋引擎下载或解压失败，请检查网络、py7zr 依赖或改填本地引擎路径。",
    native_game_xiangqi_check_failed:"Pikafish 象棋引擎自检未通过，请确认可执行文件与 pikafish.nnue 在同一目录且可执行。",
    native_game_cloudflared_install_failed:"cloudflared 下载失败，请检查网络，或改用局域网 / 固定 HTTPS 地址。",
    native_game_tunnel_failed:"临时公网通道启动失败，请确认 cloudflared 可用且网络可访问 Cloudflare。",
    native_game_server_disabled:"游戏网页服务尚未启用，请先在下方设置中启用网页服务并保存。",
    native_game_external_url_configured:"已配置固定外部访问地址，无需启动临时公网通道。",
    native_game_active_games_exist:"当前仍有进行中的对局，请等待对局结束后再停止公网通道。",
    native_game_action_failed:"游戏管理操作未完成，请刷新状态核对配置后再试。",
  };
  class Management {
    constructor(root, options) {
      this.root = root; this.options = options; this.name = options.extensionId;
      this.record = options.record; this.alive = true; this.busy = false; this.blocked = false; this.reads = 0;
      const saved = options.getState?.();
      this.cameraUser = saved?.user || ""; this.cameraDraft = saved?.draft || null;
      this.cameraOpen = saved?.open || false; this.cameraConsent = saved?.consent || false;
      this.audioOpen = saved?.audioOpen || false; this.audioDraft = saved?.audioDraft || null;
      this.cameraTestResult = saved?.cameraTestResult || null; this.cameraPreview = saved?.cameraPreview || null;
      this.click = event => this.onClick(event);
      this.change = event => this.onChange(event);
      root.addEventListener("click", this.click); root.addEventListener("change", this.change);
      this.render();
    }
    current() { return this.alive && this.root.isConnected !== false; }
    stop() {
      this.alive = false; clearTimeout(this.timer); this.timer = null;
      this.root.removeEventListener("click", this.click); this.root.removeEventListener("change", this.change);
    }
    update(record) { this.saveCameraDraft(); this.record = record; if (this.current()) this.render(); }
    setBlocked(value) { this.blocked = value; this.updateButtons(); }
    button(action, text = actionNames[action], extra = "") {
      return `<button class="button small" type="button" data-native-action="${action}" ${extra}>${escape(text)}</button>`;
    }
    notice(message, failed = false) {
      this.message = message; this.failed = failed;
      const node = this.root.querySelector("[data-native-action-notice]");
      if (node) { node.textContent = message; node.classList.toggle("is-error", failed); }
    }
    render() {
      if (!["reality", "live", "game"].includes(this.name)) { this.root.innerHTML = ""; return; }
      if (!this.record.management) {
        const heading = this.name === "game" ? "游戏引擎与公网访问管理" : "连接与设备管理";
        this.root.innerHTML = `<section class="native-management"><h4>${heading}</h4><p>管理状态尚未读取。</p><button class="button small" type="button" data-native-management-refresh>重新读取管理状态</button><p data-native-action-notice role="status" aria-live="polite"></p></section>`;
      } else this.root.innerHTML = this.name === "reality" ? this.reality() : this.name === "game" ? this.game() : this.live();
      const icons = this.root.querySelectorAll?.("[data-lucide]");
      if (icons?.length && window.lucide?.createIcons) {
        window.lucide.createIcons({ nodes: [...icons], attrs: { "aria-hidden": "true" } });
      }
      if (this.name === "reality") {
        const cameraBody = this.root.querySelector("[data-native-camera-details] .native-management-body");
        if (cameraBody) cameraBody.insertAdjacentHTML("beforeend",
          `<div class="native-management-actions">${this.button("test_camera")}</div>${this.cameraTestMarkup()}`);
      }
      this.updateButtons(); this.notice(this.message || "", this.failed);
      this.scheduleRead();
    }
    reality() {
      const data = this.record.management;
      return `<div class="native-management">${this.camera(data)}${this.audio(data)}<div class="native-management-bottom"><button class="button small" type="button" data-native-management-refresh>刷新管理状态</button><p data-native-action-notice role="status" aria-live="polite"></p></div></div>`;
    }
        camera(data) {
      const users = Array.isArray(data.users) ? data.users : [], camera = data.camera || {};
      const selected = window.CompanionProduct?.target?.();
      if (!this.cameraUser) this.cameraUser = selected?.scope_kind === "private" ? String(selected.participant_id || "") : String(users[0]?.user_id || "");
      const user = users.find(row => String(row.user_id) === this.cameraUser), policy = {...(user?.camera || {}), ...(this.cameraDraft || {})};
      const eligible = user?.camera?.eligible, consented = user?.camera?.consented === true;
      const state = !Array.isArray(data.users) ? "授权状态未读取" : !user?.camera ? "尚无摄像头授权记录"
        : eligible === false ? "当前用户不可用" : consented ? "已确认授权" : "尚未确认";
      const modes = [["off","仅明确请求"],["ask","主动前先询问"],["auto","按授权主动"]];
      const cameras = Array.isArray(camera.devices) ? camera.devices : [];
      const list = cameras.length ? `<ul class="native-management-camera-devices">${cameras.map(device => `<li>${escape(device.name || device.label || "摄像头")} <span>索引 ${escape(device.index ?? device.camera_index ?? "—")}</span></li>`).join("")}</ul>` : `<p class="native-management-muted">尚未发现摄像头，点击扫描摄像头读取设备目录。</p>`;
      return `<details data-native-camera-details ${this.cameraOpen ? "open" : ""}><summary>摄像头确认与策略 <span>${state}</span></summary><div class="native-management-body"><p class="native-management-muted">${escape(camera.boundary || "按明确任务读取摄像头单帧，画面可能发送给所选视觉模型；默认不保存原图。")}</p><label class="native-management-field">用户 QQ / ID<input type="text" data-native-camera-user list="native-camera-users-${this.options.instance}" value="${escape(this.cameraUser)}" placeholder="选择或填写要管理的用户"><datalist id="native-camera-users-${this.options.instance}">${users.map(row => `<option value="${escape(row.user_id)}">${escape(row.label || row.user_id)}</option>`).join("")}</datalist></label><p class="native-management-camera-state">${escape(user?.label || this.cameraUser || "尚未选择用户")} · ${state}</p><label class="native-management-consent"><input type="checkbox" data-native-camera-consent ${this.cameraConsent ? "checked" : ""}><span>我已了解单帧读取和视觉模型处理方式，确认给此用户授权。</span></label><div class="native-management-actions">${this.button("consent_camera", "确认摄像头授权", "data-native-consent=\"true\" data-native-requires-consent")}${this.button("consent_camera", "撤销摄像头授权", "data-native-consent=\"false\"")}</div><fieldset class="native-management-policy"><legend>此用户的使用策略</legend><label class="native-management-consent"><input type="checkbox" data-native-camera-enabled ${policy.enabled ? "checked" : ""}>允许使用摄像头</label><label class="native-management-field">主动读取方式<select data-native-camera-mode>${modes.map(([value, title]) => `<option value="${value}" ${value === (policy.proactive_mode || "off") ? "selected" : ""}>${title}</option>`).join("")}</select></label><label class="native-management-field">每天主动读取次数<input type="number" data-native-camera-daily min="-1" max="10" step="1" value="${escape(policy.proactive_max_daily ?? -1)}"><small>-1 跟随全局设置，0 关闭主动读取。</small></label>${this.button("update_camera_policy")}</fieldset><div class="native-management-actions">${this.button("scan_camera")}</div>${list}${camera.devices_error ? `<p class="native-management-muted">${escape(camera.devices_error)}</p>` : ""}</div></details>`;
    }
    cameraTestMarkup() {
      const test = this.cameraTestResult;
      if (!test) return "";
      const preview = cameraPreviewSource(this.cameraPreview?.data_url);
      const title = test.status === "error" ? "摄像头测试未完成"
        : test.status === "observation_uncertain" ? "已读取，识别结果不确定" : "摄像头测试完成";
      return `<div class="native-management-camera-test" data-native-camera-test role="status" aria-live="polite"><strong>${title}</strong><p>${escape(test.message || "")}</p>${test.summary ? `<p>视觉摘要：${escape(test.summary)}</p>` : ""}${preview ? `<img class="native-management-camera-preview" src="${escape(preview)}" alt="摄像头测试单帧预览" loading="lazy">` : ""}</div>`;
    }
    audio(data) {
      const users = Array.isArray(data.users) ? data.users : [], output = data.audio_output || {};
      const user = users.find(row => String(row.user_id) === this.cameraUser), draft = this.audioDraft || {};
      const alarm = {...(user?.alarm || {}), ...(draft.alarm || {})}, policy = {...(user?.policy || {}), ...(draft.policy || {})};
      const consented = user?.consent?.local_audio === true || user?.consent?.confirmed === true;
      const devices = Array.isArray(output.devices) ? output.devices : [], selected = draft.device_id || output.selected_device_id || "system_default";
      const number = (key, title, value, min, max) => `<label class="native-management-field">${title}<input type="number" data-native-audio-input="${key}" min="${min}" max="${max}" step="1" value="${escape(value)}"></label>`;
      const days = Array.isArray(alarm.days) ? alarm.days : [0,1,2,3,4,5,6];
      const modes = [["audio_only","仅本机语音"],["audio_and_chat","本机语音与私聊"],["chat_on_failure","语音失败时发私聊"]];
      const reminders = (Array.isArray(user?.custom_reminders) ? user.custom_reminders : []).filter(row => ["registering","scheduled","triggered","delivering"].includes(row.status));
      return `<details data-native-audio-details ${this.audioOpen ? "open" : ""}><summary>本机语音与叫醒 <span>${consented ? "本人已授权" : "等待本人授权"}</span></summary><div class="native-management-body">
        <label class="native-management-field">音频输出<select data-native-audio-device>${(!devices.some(row => row.id === selected) ? `<option value="${escape(selected)}">${escape(output.label || "跟随系统默认输出")}</option>` : "")}${devices.map(row => `<option value="${escape(row.id)}" ${row.id === selected ? "selected" : ""}>${escape(row.name || row.id)}</option>`).join("")}</select></label>
        ${number("output_volume","设备测试音量（0–100）",draft.output_volume ?? output.playback_volume ?? 35,0,100)}
        <div class="native-management-actions">${this.button("scan_audio")}${this.button("select_audio")}${this.button("test_audio")}</div>${output.error ? `<p class="native-management-muted">${escape(output.error)}</p>` : ""}
        <label class="native-management-field">语音与叫醒用户 QQ / ID<input type="text" data-native-audio-user list="native-camera-users-${this.options.instance}" value="${escape(this.cameraUser)}" placeholder="选择已有私聊的用户"></label>
        <p class="native-management-muted">${escape(user?.label || this.cameraUser || "尚未选择用户")} · ${consented ? "本人音频授权已生效" : "准备授权后，将提示文字发到本人的私聊，由本人确认。"}</p>
        <div class="native-management-actions">${this.button("request_audio_consent")}${this.button("revoke_audio_consent")}</div>
        <fieldset class="native-management-policy"><legend>主动语音</legend><label class="native-management-consent"><input type="checkbox" data-native-audio-proactive ${policy.proactive_voice_enabled ? "checked" : ""}>将主动联系同步播放到本机</label>${number("policy_volume","主动语音音量（0–100）",policy.playback_volume ?? 35,0,100)}${this.button("update_audio_policy")}</fieldset>
        <fieldset class="native-management-policy"><legend>早晨照料</legend>
        <label class="native-management-consent"><input type="checkbox" data-native-alarm-enabled ${alarm.enabled ? "checked" : ""}>在早晨试着叫你，而不是按闹钟执行</label>
        <p class="native-management-muted">它会先看你醒没醒；叫一次没回应才继续，最晚到「约定时间」就停。需要本人完成本机语音授权后才会出声。</p>
        <div class="native-management-grid">
          <label class="native-management-field">大概几点起<input type="time" data-native-alarm-time value="${escape(alarm.time || "07:30")}"></label>
          <label class="native-management-field">最晚（可选）<input type="time" data-native-alarm-deadline value="${escape(alarm.deadline || "")}"></label>
          <label class="native-management-field">怎么叫<select data-native-alarm-mode>${[["gentle","轻一点"],["normal","正常"],["persistent","执着一点"]].map(([value,title]) => `<option value="${value}" ${value === (alarm.mode || "normal") ? "selected" : ""}>${title}</option>`).join("")}</select></label>
        </div>
        <label class="native-management-field">想让你记得的事（可选）<input type="text" data-native-alarm-message maxlength="240" value="${escape(alarm.message || "")}" placeholder="例如提醒吃早餐，文案由角色生成"></label>
        <div class="native-management-actions">${this.button("save")}${this.button("disable")}${this.button("stop_session")}${this.button("test")}</div><p class="native-management-muted">${alarm.next_trigger_text ? `下次：${escape(alarm.next_trigger_text)}` : "尚未安排"}${alarm.contact_session?.status ? ` · 本次：${escape(alarm.contact_session.status)}` : ""}</p></fieldset>
        ${reminders.length ? `<fieldset class="native-management-policy"><legend>待办提醒</legend><ul class="native-management-reminders">${reminders.map(row => `<li><span>${escape(row.scheduled_text || "时间待确认")} · ${escape(row.topic || "提醒")}</span>${this.button("cancel_reminder","取消这条提醒",`data-native-reminder-id="${escape(row.id)}"`)}</li>`).join("")}</ul></fieldset>` : ""}
      </div></details>`;
    }
    live() {
      const data = this.record.management, connected = data.connected === true, authenticated = data.authenticated === true;
      const hotkeys = Array.isArray(data.hotkeys) ? data.hotkeys : [];
      const label = authenticated ? "已连接并授权" : connected ? "已连接 · 等待授权" : "尚未连接";
      const table = hotkeys.length ? `<div class="native-management-table"><table><thead><tr><th>热键</th><th>ID</th><th>表情配置</th></tr></thead><tbody>${hotkeys.map((hotkey, index) => `<tr><td>${escape(hotkey.name || "未命名热键")}</td><td><code>${escape(hotkey.hotkeyID || hotkey.hotkey_id || hotkey.id || "")}</code></td><td><button class="button small" type="button" data-native-use-hotkey="${index}">添加到表情</button></td></tr>`).join("")}</tbody></table></div>` : `<p class="native-management-muted">连接并授权后，点击读取热键查看当前模型的可用表情。</p>`;
      return `<section class="native-management"><header><h4>VTube Studio</h4><span>${label}</span></header><p class="native-management-muted">先在 VTube Studio 开启 API。请求授权后，在 VTS 弹窗中点“允许”，再读取当前模型的热键。</p>${data.error_code ? `<p class="native-management-muted">${escape(errors[data.error_code] || "连接尚未就绪，请核对设备状态。")}</p>` : ""}<div class="native-management-actions">${this.button("connect", connected ? "重新连接" : "连接 VTube Studio")}${this.button("request_auth", "请求 VTS 授权")}${this.button("list_hotkeys", "读取热键", authenticated ? "" : "data-native-requires-auth")}</div>${table}<div class="native-management-bottom"><button class="button small" type="button" data-native-management-refresh>刷新管理状态</button><p data-native-action-notice role="status" aria-live="polite"></p></div></section>`;
    }
    game() {
      const data = this.record.management || {};
      const server = data.server || {}, engine = data.xiangqi_engine || {}, tunnel = data.tunnel || {};
      const engineState = engine.available
        ? (engine.version ? `已就绪 · ${engine.version}` : "已就绪")
        : engine.configured_missing ? "自定义路径不存在"
          : engine.path ? "缺少神经网络文件" : "未安装";
      const engineSource = engine.configured ? "自定义路径" : engine.managed ? "托管安装" : "未检测到";
      const enginePath = engine.path || engine.configured_path || engine.install_root || "未配置";
      const engineInstallDisabled = engine.allow_download === false ? "data-native-disabled" : "";
      const configuredBase = String(server.public_base_url || server.external_base_url || "").trim();
      const tunnelSourceLabels = {configured:"自定义路径", managed:"托管目录", system:"系统 PATH", bundled:"插件目录"};
      const tunnelState = configuredBase ? "已配固定地址"
        : tunnel.ready && tunnel.url ? "公网通道运行中"
          : tunnel.installed ? "cloudflared 已就绪"
            : tunnel.configured_missing ? "自定义路径不存在" : "未安装 cloudflared";
      const serverLine = server.running
        ? `网页服务运行中（${server.local_base_url || `${server.host || "0.0.0.0"}:${server.port || 8765}`}）`
        : server.enabled ? "网页服务已启用，等待模块就绪" : "网页服务未启用";
      const externalLine = configuredBase ? `固定外部地址：${configuredBase}`
        : tunnel.url ? `当前公网地址：${tunnel.url}`
          : server.access_host ? `局域网访问主机：${server.access_host}` : "";
      const tunnelPath = tunnel.path || tunnel.binary_path || tunnel.configured_path || tunnel.download_dir || "未检测到";
      const tunnelSource = tunnelSourceLabels[tunnel.source] || (tunnel.installed ? "已检测到" : "未检测到");
      const tunnelInstallDisabled = tunnel.allow_download === false ? "data-native-disabled" : "";
      const tunnelStartDisabled = configuredBase || server.enabled === false || tunnel.running === true ? "data-native-disabled" : "";
      const tunnelStopDisabled = tunnel.running === true ? "" : "data-native-disabled";
      const activeCount = (Array.isArray(data.native_games) ? data.native_games.length : 0)
        + (Array.isArray(data.rooms) ? data.rooms.length : 0);
      return `<div class="native-management">
        <details open data-native-game-xiangqi><summary>中国象棋引擎（Pikafish）<span>${escape(engineState)}</span></summary><div class="native-management-body">
          <p class="native-management-muted">来源：${escape(engineSource)} · 路径：<code>${escape(enginePath)}</code>${engine.nnue_path ? " · 神经网络文件已就绪" : ""}</p>
          ${engine.guidance ? `<p class="native-management-muted">${escape(engine.guidance)}</p>` : ""}
          ${engine.error ? `<p class="native-management-login-detail">${escape(engine.error)}</p>` : ""}
          <div class="native-management-actions">${this.button("xiangqi_install", engine.managed && engine.available ? "重新下载 Pikafish 引擎" : "下载 Pikafish 引擎", engineInstallDisabled)}${this.button("xiangqi_check")}</div>
        </div></details>
        <details open data-native-game-tunnel><summary>网页房间与临时公网（cloudflared）<span>${escape(tunnelState)}</span></summary><div class="native-management-body">
          <p class="native-management-muted">${escape(serverLine)}${externalLine ? ` · ${escape(externalLine)}` : ""}</p>
          <p class="native-management-muted">cloudflared 来源：${escape(tunnelSource)} · 路径：<code>${escape(tunnelPath)}</code></p>
          ${tunnel.guidance ? `<p class="native-management-muted">${escape(tunnel.guidance)}</p>` : ""}
          ${tunnel.error ? `<p class="native-management-login-detail">${escape(tunnel.error)}</p>` : ""}
          <div class="native-management-actions">${this.button("cloudflared_install", tunnel.installed && tunnel.source === "managed" ? "重新下载 cloudflared" : "下载 cloudflared", tunnelInstallDisabled)}${this.button("cloudflared_check")}${this.button("tunnel_start", undefined, tunnelStartDisabled)}${this.button("tunnel_stop", undefined, tunnelStopDisabled)}</div>
        </div></details>
        <p class="native-management-muted">当前活动对局：${activeCount} 局</p>
        <div class="native-management-bottom"><button class="button small" type="button" data-native-management-refresh>刷新管理状态</button><p data-native-action-notice role="status" aria-live="polite"></p></div>
      </div>`;
    }
    updateButtons() {
      const consent = this.root.querySelector("[data-native-camera-consent]")?.checked === true;
      this.root.querySelectorAll("[data-native-action], [data-native-use-hotkey]").forEach(button => {
        button.disabled = this.busy || this.blocked
          || button.hasAttribute("data-native-disabled")
          || button.hasAttribute("data-native-requires-auth") && this.record.management?.authenticated !== true
          || button.hasAttribute("data-native-requires-consent") && !consent;
      });
      const refresh = this.root.querySelector("[data-native-management-refresh]");
      if (refresh) refresh.disabled = this.busy || this.blocked;
      this.root.querySelectorAll("input, select").forEach(node => { node.disabled = this.busy || this.blocked; });
      this.root.setAttribute("aria-busy", String(this.busy));
    }
    saveCameraDraft() {
      this.saveAudioDraft();
      const node = this.root.querySelector("[data-native-camera-enabled]");
      if (!node) return;
      this.cameraOpen = this.root.querySelector("[data-native-camera-details]").open;
      const draft = {enabled:node.checked, proactive_mode:this.root.querySelector("[data-native-camera-mode]").value,
        proactive_max_daily:Number(this.root.querySelector("[data-native-camera-daily]").value)};
      const stored = this.record.management?.users?.find(row => String(row.user_id) === this.cameraUser)?.camera;
      this.cameraDraft = this.differences(draft, {enabled:stored?.enabled === true,
        proactive_mode:stored?.proactive_mode || "off", proactive_max_daily:stored?.proactive_max_daily ?? -1});
      this.publishState();
    }
    differences(values, stored) {
      const result = {};
      for (const [key, value] of Object.entries(values)) {
        if (value && typeof value === "object" && !Array.isArray(value)) {
          const nested = this.differences(value, stored?.[key]);
          if (nested) result[key] = nested;
        } else if (JSON.stringify(value) !== JSON.stringify(stored?.[key])) result[key] = value;
      }
      return Object.keys(result).length ? result : null;
    }
    storedAudioValues() {
      const data = this.record.management || {}, output = data.audio_output || {};
      const user = data.users?.find(row => String(row.user_id) === this.cameraUser);
      const policy = user?.policy || {}, alarm = user?.alarm || {};
      return {device_id:output.selected_device_id || "system_default", output_volume:output.playback_volume ?? 35,
        policy:{proactive_voice_enabled:policy.proactive_voice_enabled === true, playback_volume:policy.playback_volume ?? 35},
        alarm:{enabled:alarm.enabled === true, time:alarm.time || "07:30", deadline:alarm.deadline || "",
          mode:alarm.mode || "normal", message:alarm.message || "",
          days:Array.isArray(alarm.days) ? alarm.days : [0,1,2,3,4,5,6],
          playback_volume:alarm.playback_volume ?? 35, repeat_count:alarm.repeat_count ?? 1,
          repeat_interval_seconds:alarm.repeat_interval_seconds ?? 20, snooze_minutes:alarm.snooze_minutes ?? 10,
          volume_step:alarm.volume_step ?? 8, max_volume:alarm.max_volume ?? 70, fade_in_ms:alarm.fade_in_ms ?? 800}};
    }
    audioFormValues() {
      const value = key => {
        const text = this.root.querySelector(`[data-native-audio-input="${key}"]`).value;
        return text.trim() ? Number(text) : "";
      };
      const alarm = {enabled:this.root.querySelector("[data-native-alarm-enabled]").checked,
        time:this.root.querySelector("[data-native-alarm-time]").value,
        deadline:this.root.querySelector("[data-native-alarm-deadline]").value,
        mode:this.root.querySelector("[data-native-alarm-mode]").value,
        message:this.root.querySelector("[data-native-alarm-message]").value};
      return {device_id:this.root.querySelector("[data-native-audio-device]").value, output_volume:value("output_volume"),
        policy:{proactive_voice_enabled:this.root.querySelector("[data-native-audio-proactive]").checked, playback_volume:value("policy_volume")}, alarm};
    }
    saveAudioDraft() {
      if (!this.root.querySelector("[data-native-audio-device]")) return;
      this.audioOpen = this.root.querySelector("[data-native-audio-details]").open;
      const values = this.audioFormValues();
      this.audioDraft = this.differences(values, this.storedAudioValues());
      this.publishState();
      return values;
    }
    publishState() { this.options.onState?.({user:this.cameraUser, draft:this.cameraDraft, open:this.cameraOpen, consent:this.cameraConsent, audioOpen:this.audioOpen, audioDraft:this.audioDraft, cameraTestResult:this.cameraTestResult, cameraPreview:this.cameraPreview}); }
    async read({quiet = false} = {}) {
      if (!this.current() || this.reading || this.busy && !quiet) return;
      this.reading = true;
      const operation = this.operation;
      try {
        const result = await window.CompanionProduct.request("GET", "extensions");
        if (!this.current() || operation !== this.operation) return;
        const record = result?.extensions?.[this.name];
        if (!record?.management) throw {code:"native_management_unavailable"};
        this.saveCameraDraft(); this.record.management = record.management;
        this.options.onRead?.(record); this.render();
        if (!quiet) this.notice("管理状态已刷新。");
      } catch (error) { if (this.current() && operation === this.operation && !quiet) this.notice(errors[error?.code] || "管理状态暂时读取不到，请重新刷新。", true); }
      finally { this.reading = false; }
    }
    scheduleRead() { /* 米家扫码登录轮询已随 mihome 模块移除 */ }
    async run(action, payload) {
      if (this.busy || this.blocked || !this.current()) return;
      this.operation = (this.operation || 0) + 1;
      this.saveCameraDraft(); this.busy = true; this.options.onBusy?.(true); this.updateButtons();
      const waiting = action === "request_auth" ? "请在 VTube Studio 弹窗中点“允许”，正在等待授权结果…" : `正在${actionNames[action] || "执行操作"}…`;
      this.notice(waiting);
      try {
        const timeoutMs = ["xiangqi_install", "cloudflared_install"].includes(action) ? 245000
          : action === "test_camera" ? 130000 : 65000;
        const record = await window.CompanionProduct.request("POST", "extensions/action", {extension_id:this.name, action, payload}, {timeoutMs});
        if (!this.current()) return;
        if (!record?.management) throw {code:"invalid_response"};
        this.record = record;
        if (action === "test_camera") {
          const result = record.result || {}, observation = result.observation || {};
          this.cameraTestResult = {status:result.captured === true ? result.status || "success" : "error",
            message:result.message || (result.captured === true ? "已完成摄像头单帧测试。" : "摄像头测试没有取得有效画面。"),
            summary:observation.summary || observation.purpose_answer || observation.scene_description || ""};
          const preview = record.camera_preview;
          this.cameraPreview = result.captured === true && preview?.ephemeral === true
            && String(preview.user_id) === String(payload.user_id) && cameraPreviewSource(preview.data_url) ? preview : null;
          this.publishState();
        }
        if (action === "update_camera_policy") this.cameraDraft = null;
        if (action === "consent_camera" && this.cameraDraft) delete this.cameraDraft.enabled;
        if (action === "consent_camera" || action === "update_camera_policy") { this.cameraConsent = false; this.publishState(); }
        if (this.audioDraft) {
          if (action === "select_audio") { delete this.audioDraft.device_id; delete this.audioDraft.output_volume; }
          if (action === "update_audio_policy") delete this.audioDraft.policy;
          if (action === "save") delete this.audioDraft.alarm;
          if (["disable","revoke_audio_consent"].includes(action) && this.audioDraft.alarm) delete this.audioDraft.alarm.enabled;
          if (action === "revoke_audio_consent" && this.audioDraft.policy) delete this.audioDraft.policy.proactive_voice_enabled;
          this.publishState();
        }
        this.options.onRecord?.(record);
        if (!this.current()) return;
        this.render();
        if (record.result?.confirmation_required) this.notice(record.result.prompt || "请由本人在私聊中完成音频授权确认。");
        else if (action === "request_auth" && record.management.authenticated !== true) this.notice("授权还未完成。请到 VTube Studio 弹窗中点“允许”，再重新连接核对。", true);
        else if (action === "test_camera") this.notice(this.cameraTestResult.message, this.cameraTestResult.status === "error");
        else if (action === "xiangqi_check") {
          const engine = record.management?.xiangqi_engine || {};
          this.notice(engine.available ? "Pikafish 象棋引擎自检通过，已就绪。" : (engine.guidance || "尚未检测到可用的 Pikafish 象棋引擎。"), !engine.available);
        } else if (action === "cloudflared_check") {
          const tunnel = record.management?.tunnel || {};
          this.notice(tunnel.guidance || (tunnel.installed ? "已检测到 cloudflared。" : "尚未检测到 cloudflared。"), !tunnel.installed);
        } else if (action === "xiangqi_install") this.notice("Pikafish 象棋引擎已安装并就绪。");
        else if (action === "cloudflared_install") this.notice("cloudflared 已安装到托管目录。");
        else if (action === "tunnel_start") this.notice(record.result?.url ? `临时公网已启动：${record.result.url}` : "临时公网已启动。");
        else if (action === "tunnel_stop") this.notice("临时公网已停止。");
        else this.notice(`${actionNames[action] || "操作"}已完成。`);
      } catch (error) {
        if (this.current()) {
          const message = errors[error?.code] || window.CompanionProduct.errorLabel?.(error?.code) || "操作结果暂未确认，请刷新状态核对后再试。";
          if (action === "test_camera") { this.cameraTestResult = {status:"error", message, summary:""}; this.cameraPreview = null; this.publishState(); }
          this.notice(message, true);
          await this.read({quiet:true});
        }
      } finally {
        if (this.current()) { this.busy = false; this.options.onBusy?.(false); this.updateButtons(); }
      }
    }
    payload(action, button) {
      if (["select_audio","test_audio","request_audio_consent","revoke_audio_consent","update_audio_policy","save","disable","stop_session","test","cancel_reminder"].includes(action)) {
        const draft = this.saveAudioDraft();
        if (action === "select_audio") return {device_id:draft.device_id, playback_volume:draft.output_volume};
        if (action === "test_audio") return {playback_volume:draft.output_volume};
        const user_id = this.root.querySelector("[data-native-audio-user]").value.trim();
        if (!user_id) throw new Error("先选择已有私聊的用户。");
        if (action === "update_audio_policy") return {user_id, ...draft.policy};
        if (action === "save") {
          if (!draft.alarm.time) throw new Error("请选择大概的起床时间。");
          if (draft.alarm.deadline && draft.alarm.deadline <= draft.alarm.time) throw new Error("最晚时间需要晚于起床时间。");
          return {user_id, ...draft.alarm};
        }
        if (action === "test") return {user_id, playback_volume:draft.alarm.playback_volume, message:draft.alarm.message};
        if (action === "cancel_reminder") return {user_id, reminder_id:button.dataset.nativeReminderId};
        return {user_id};
      }
      if (["consent_camera", "update_camera_policy"].includes(action)) {
        const user_id = this.root.querySelector("[data-native-camera-user]").value.trim();
        if (!user_id) throw new Error("先选择或填写要管理的用户。");
        if (action === "consent_camera") return {user_id, confirmed:button.dataset.nativeConsent === "true"};
        const daily = this.root.querySelector("[data-native-camera-daily]").value;
        const proactive_max_daily = Number(daily);
        if (!daily.trim() || !Number.isInteger(proactive_max_daily) || proactive_max_daily < -1 || proactive_max_daily > 10) throw new Error("主动读取次数应为 -1 到 10 的整数。");
        return {user_id, camera_enabled:this.root.querySelector("[data-native-camera-enabled]").checked,
          proactive_mode:this.root.querySelector("[data-native-camera-mode]").value, proactive_max_daily};
      }
      if (action === "test_camera") {
        const user_id = this.root.querySelector("[data-native-camera-user]").value.trim();
        if (!user_id) throw new Error("先选择已有 V2 私聊的用户。");
        return {user_id, purpose:"拓展页摄像头单帧测试"};
      }
      return {};
    }
    onClick(event) {
      const button = event.target.closest?.("button");
      if (!button || !this.root.contains(button) || button.disabled) return;
      if (button.hasAttribute("data-native-management-refresh")) { this.read(); return; }
      if (button.hasAttribute("data-native-use-hotkey")) {
        const hotkey = this.record.management.hotkeys[Number(button.dataset.nativeUseHotkey)];
        this.options.onUseHotkey?.(hotkey); return;
      }
      const action = button.dataset.nativeAction;
      if (action) { try { this.run(action, this.payload(action, button)); } catch (error) { this.notice(error.message, true); } }
    }
    onChange(event) {
      if (event.target.hasAttribute?.("data-native-camera-user") || event.target.hasAttribute?.("data-native-audio-user")) {
        this.cameraUser = event.target.value.trim(); this.cameraDraft = null; this.audioDraft = null; this.cameraConsent = false;
        this.cameraTestResult = null; this.cameraPreview = null;
        if (event.target.hasAttribute("data-native-audio-user")) this.audioOpen = true; else this.cameraOpen = true;
        this.publishState(); this.render();
      } else {
        if (event.target.hasAttribute?.("data-native-camera-consent")) this.cameraConsent = event.target.checked;
        this.saveCameraDraft(); this.updateButtons();
      }
    }
  }
  return {mount:(root, options) => new Management(root, options)};
})();
