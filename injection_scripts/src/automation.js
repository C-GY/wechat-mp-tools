/**
 * @file 一键采集全部关注的视频号作品（仅同步元数据 · 低风控版）
 *
 * 复用现有能力：
 *   - WXU.API4.finderGetFollowList  枚举「我关注的视频号」（见 docs 方案 §8）
 *   - WXU.API.finderUserPage        翻页拉取单个作者作品（与 profile.js 同步逻辑一致）
 *   - POST /__wx_channels_api/sync-feed       出口，落盘到 channels_parsed_feeds.json
 *   - GET  /__wx_channels_api/synced-feed-ids 每作者已同步作品 id，用于增量「只采新作品」
 *   - POST /__wx_channels_api/call-log        调用埋点，供「测风控概率」分析
 *
 * 不下载视频文件（仅同步元数据）。四道低风控机制：
 *   1) 增量      首页比对已知 id，命中即停，稳态下每作者只拉 1 页
 *   2) 令牌桶    任意 60s 窗口内 finder 调用 ≤ RATE_LIMIT_PER_MIN，硬压突发高频
 *   3) 熔断      连续 CIRCUIT_FAIL_THRESHOLD 次最终失败 → 整体暂停，不重试风暴
 *   4) 会话预算  单次最多处理 SESSION_AUTHOR_CAP 个作者，localStorage 轮替偏移避免饿死尾部
 */
(() => {
  if (typeof WXU === "undefined") return;
  if (window.__wx_harvest_inited__) return;
  window.__wx_harvest_inited__ = true;
  var pageId = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);

  // ---- 可调参数（测风控时单变量扫描这几个）----
  var RATE_LIMIT_PER_MIN = 20; // 令牌桶：60s 窗口内最多 finder 调用次数
  var MAX_ITEMS_PER_AUTHOR = 30; // 单作者最多采集条数（≈2 页），0 或负数 = 不限
  var SESSION_AUTHOR_CAP = 12; // 单次最多处理作者数，超出下次轮替继续
  var CIRCUIT_FAIL_THRESHOLD = 3; // 连续最终失败达到此数 → 熔断
  var PAGE_JITTER_MS = 1500; // 翻页间基准延迟
  var AUTHOR_JITTER_MS = 6000; // 作者间基准延迟

  var my_username = typeof __wx_username !== "undefined" ? __wx_username : "";
  WXU.onInit(function (data) {
    my_username = (data && data.mainFinderUsername) || my_username;
  });

  function sleep(ms) {
    return new Promise(function (r) {
      setTimeout(r, ms);
    });
  }
  // 对数正态随机延迟，模拟人类节奏（作者间/翻页间）
  function jitterSleep(baseMs) {
    return sleep(baseMs * Math.exp((Math.random() - 0.5) * 0.8));
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // ---- 令牌桶：滑动 60s 窗口限流，所有 finder 调用前必须过闸 ----
  var callTimes = [];
  async function rateGate() {
    while (true) {
      var now = Date.now();
      callTimes = callTimes.filter(function (t) {
        return now - t < 60000;
      });
      if (callTimes.length < RATE_LIMIT_PER_MIN) {
        callTimes.push(now);
        return;
      }
      // 已达上限，等到最早一次调用滑出窗口
      await sleep(60000 - (now - callTimes[0]) + 50);
    }
  }

  // ---- 熔断：连续最终失败计数；成功清零；越阈值置 circuitOpen ----
  var circuitFails = 0;
  var circuitOpen = false;
  function noteFailure() {
    circuitFails++;
    if (circuitFails >= CIRCUIT_FAIL_THRESHOLD) circuitOpen = true;
  }

  // ---- 埋点：每次 finder 调用记一行，fire-and-forget（不阻塞、不抛错）----
  // extra 可附加字段（如 author、new、capped、pages），供日志直接自证增量/上限
  function logCall(api, errCode, errMsg, ms, extra) {
    try {
      var body = { api: api, errCode: errCode, errMsg: errMsg || "", ms: ms };
      if (extra) for (var k in extra) body[k] = extra[k];
      WXU.request({
        method: "POST",
        url: "/__wx_channels_api/call-log",
        body: body,
      });
    } catch (_) {}
  }

  // 令牌桶 + 埋点 + 指数退避 重试：应对视频号接口「未就绪」瞬时拒绝（errCode -70002）
  async function callWithRetry(apiName, fn, meta) {
    var delays = [2000, 5000, 10000, 20000];
    for (var i = 0; i <= delays.length; i++) {
      await rateGate();
      var t0 = Date.now();
      try {
        var r = await fn();
        logCall(apiName, r && r.errCode, r && r.errMsg, Date.now() - t0, meta);
        if (r && r.errCode === 0) {
          circuitFails = 0; // 成功，清零熔断计数
          return r;
        }
        if (i < delays.length) {
          await sleep(delays[i]);
          continue;
        }
        return r;
      } catch (e) {
        logCall(apiName, "throw", e && e.message ? e.message : String(e), Date.now() - t0, meta);
        if (i < delays.length) {
          await sleep(delays[i]);
          continue;
        }
        throw e;
      }
    }
  }

  // 拉取全部关注作者（翻页 + 去重）
  async function fetchAllFollows(onProgress) {
    var authors = [];
    var seen = {};
    var lastBuffer = "";
    while (!circuitOpen) {
      var r = await callWithRetry("finderGetFollowList", function () {
        return WXU.API4.finderGetFollowList({ lastBuffer: lastBuffer });
      });
      if (!r || r.errCode !== 0) {
        noteFailure();
        throw new Error((r && r.errMsg) || "获取关注列表失败");
      }
      var list = (r.data && r.data.contactList) || [];
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (c.username && !seen[c.username]) {
          seen[c.username] = 1;
          authors.push({ username: c.username, nickname: c.nickname, headUrl: c.headUrl });
        }
      }
      if (onProgress) onProgress(authors.length, r.data && r.data.followCount);
      lastBuffer = (r.data && r.data.lastBuffer) || "";
      if (!r.data || r.data.continueFlag === 0 || !lastBuffer || list.length === 0) break;
      await jitterSleep(PAGE_JITTER_MS);
    }
    return authors;
  }

  // 拉取并同步单个作者的作品（增量：命中已知 id 即停）。knownSet 为该作者已同步 id 的 {id:1} 映射。
  async function harvestAuthor(author, knownSet, isCancelled, onPage) {
    var marker = "";
    var total = 0;
    var capped = false;
    var pages = 0;
    var label = author.nickname || author.username;
    var cap = MAX_ITEMS_PER_AUTHOR > 0 ? MAX_ITEMS_PER_AUTHOR : Infinity;
    while (!isCancelled() && !circuitOpen) {
      var r = await callWithRetry("finderUserPage", function () {
        return WXU.API.finderUserPage({
          username: author.username,
          finderUsername: my_username || author.username,
          lastBuffer: marker,
          needFansCount: 0,
          objectId: "0",
        });
      }, { author: label });
      if (!r || r.errCode !== 0) {
        noteFailure(); // 该作者拉取失败，跳过，不中断整体（熔断由 circuitOpen 统一接管）
        break;
      }
      pages++;
      var raw = (r.data && r.data.object) || [];
      // 视频号返回按时间倒序：逐条收集新作品，遇到第一个已知 id 即停（后面都是旧的）
      var fresh = [];
      var hitKnown = false;
      for (var i = 0; i < raw.length; i++) {
        var o = raw[i];
        if (!(o.objectDesc && o.objectDesc.mediaType === 4)) continue;
        if (knownSet && knownSet[o.id]) {
          hitKnown = true;
          break;
        }
        if (total + fresh.length >= cap) {
          capped = true; // 达单作者上限，停止（可能还有更新作品未采，需明示）
          break;
        }
        fresh.push(o);
      }
      if (fresh.length) {
        await WXU.request({
          method: "POST",
          url: "/__wx_channels_api/sync-feed",
          body: { username: author.username, feeds: fresh },
        });
        total += fresh.length;
        if (onPage) onPage(total);
      }
      if (hitKnown || capped) break; // 增量命中 或 达上限，停止翻页
      marker = (r.data && r.data.lastBuffer) || "";
      if (!marker || raw.length === 0) break; // 没有更多（以 lastBuffer 为主要翻页依据）
      await jitterSleep(PAGE_JITTER_MS);
    }
    // 作者汇总行：直接自证增量（new 少/0）与上限（capped），errCode=null 不计入成功率统计
    logCall("author-summary", null, "", 0, { author: label, "new": total, capped: capped, pages: pages });
    return { total: total, capped: capped };
  }

  // ---- 工具箱远程触发：全量刷新「已收藏创作者」----
  async function reportRemoteProgress(taskId, progress) {
    var body = progress || {};
    body.task_id = taskId;
    body.page_id = pageId;
    try {
      await WXU.request({
        method: "POST",
        url: "/__wx_channels_api/refresh-progress",
        body: body,
      });
    } catch (_) {}
  }

  // 与作者主页「同步作品」保持一致：从第一页开始翻到末页，后端按 feed id 合并更新。
  async function refreshFavoriteAuthor(author, taskId, authorIndex, authorCount, totals) {
    var marker = "";
    var authorVideos = 0;
    var pageNumber = 0;
    var hasMore = true;
    var label = author.nickname || author.username;

    while (hasMore && !cancelled && !circuitOpen) {
      pageNumber++;
      var headline =
        "正在刷新 " + authorIndex + "/" + authorCount + "：" + esc(label) +
        "<br>第 " + pageNumber + " 页 · 已同步 " + authorVideos + " 个作品";
      setPanel("running", headline);
      await reportRemoteProgress(taskId, {
        status: "running",
        completed_authors: totals.completed,
        failed_authors: totals.failed,
        total_videos: totals.videos + authorVideos,
        current_username: author.username,
        current_nickname: label,
        message: "正在刷新 " + authorIndex + "/" + authorCount + "：" + label + "（第 " + pageNumber + " 页）",
      });

      var r = await callWithRetry("finderUserPage", function () {
        return WXU.API.finderUserPage({
          username: author.username,
          finderUsername: my_username || author.username,
          lastBuffer: marker,
          needFansCount: 0,
          objectId: "0",
        });
      }, { author: label, remoteRefresh: true });

      if (!r || r.errCode !== 0) {
        noteFailure();
        throw new Error((r && r.errMsg) || "作者作品接口调用失败");
      }

      var raw = (r.data && r.data.object) || [];
      var rawVideoObjects = raw.filter(function (obj) {
        return obj.objectDesc && obj.objectDesc.mediaType === 4;
      });
      if (rawVideoObjects.length > 0) {
        await WXU.request({
          method: "POST",
          url: "/__wx_channels_api/sync-feed",
          body: { username: author.username, feeds: rawVideoObjects },
        });
        authorVideos += rawVideoObjects.length;
      }

      marker = (r.data && r.data.lastBuffer) || "";
      hasMore = !!(marker && raw.length >= 15);
      if (hasMore && !cancelled) await jitterSleep(PAGE_JITTER_MS);
    }

    return authorVideos;
  }

  async function runRemoteFavoritesRefresh(command) {
    if (running || !command || !command.task_id) return;
    running = true;
    cancelled = false;
    circuitFails = 0;
    circuitOpen = false;

    var authors = Array.isArray(command.authors) ? command.authors : [];
    var totals = { completed: 0, failed: 0, videos: 0 };
    var heartbeatTimer = setInterval(function () {
      reportRemoteProgress(command.task_id, {
        status: "running",
      });
    }, 15000);
    function stopHeartbeat() {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    }

    try {
      var waited = 0;
      setPanel("running", "正在等待视频号作者接口就绪…");
      while ((!WXU.API || typeof WXU.API.finderUserPage !== "function") && waited < 60) {
        await sleep(500);
        waited++;
      }
      if (!WXU.API || typeof WXU.API.finderUserPage !== "function") {
        throw new Error("视频号接口未就绪，请在微信里重新打开视频号页面");
      }
      if (authors.length === 0) {
        throw new Error("刷新任务中没有收藏创作者");
      }

      for (var i = 0; i < authors.length; i++) {
        if (cancelled || circuitOpen) break;
        var author = authors[i];
        try {
          var count = await refreshFavoriteAuthor(
            author, command.task_id, i + 1, authors.length, totals
          );
          totals.videos += count;
          totals.completed++;
        } catch (_) {
          totals.failed++;
        }

        await reportRemoteProgress(command.task_id, {
          status: "running",
          completed_authors: totals.completed,
          failed_authors: totals.failed,
          total_videos: totals.videos,
          current_username: author.username,
          current_nickname: author.nickname || author.username,
          message:
            "已处理 " + (totals.completed + totals.failed) + "/" + authors.length +
            "，同步作品 " + totals.videos + " 个",
        });

        if (!cancelled && !circuitOpen && i < authors.length - 1) {
          await jitterSleep(AUTHOR_JITTER_MS);
        }
      }

      if (cancelled) {
        stopHeartbeat();
        await reportRemoteProgress(command.task_id, {
          status: "cancelled",
          completed_authors: totals.completed,
          failed_authors: totals.failed,
          total_videos: totals.videos,
          message: "刷新已停止",
        });
        finish("已停止 · 已刷新 " + totals.completed + " 个作者");
      } else if (circuitOpen) {
        stopHeartbeat();
        await reportRemoteProgress(command.task_id, {
          status: "failed",
          completed_authors: totals.completed,
          failed_authors: totals.failed,
          total_videos: totals.videos,
          message: "接口连续异常，任务已暂停，请稍后重试",
        });
        finish("接口连续异常，任务已暂停");
      } else {
        var finalMessage =
          "刷新完成：成功 " + totals.completed + " 个作者，失败 " + totals.failed +
          " 个，同步作品 " + totals.videos + " 个";
        stopHeartbeat();
        await reportRemoteProgress(command.task_id, {
          status: "completed",
          completed_authors: totals.completed,
          failed_authors: totals.failed,
          total_videos: totals.videos,
          current_username: "",
          current_nickname: "",
          message: finalMessage,
        });
        finish(finalMessage);
      }
    } catch (ex) {
      var errorMessage = ex && ex.message ? ex.message : String(ex);
      stopHeartbeat();
      await reportRemoteProgress(command.task_id, {
        status: "failed",
        completed_authors: totals.completed,
        failed_authors: totals.failed,
        total_videos: totals.videos,
        message: errorMessage,
      });
      finish("刷新失败：" + errorMessage);
    } finally {
      stopHeartbeat();
      running = false;
    }
  }

  var running = false;
  var cancelled = false;

  async function harvestAll() {
    if (running) return;
    running = true;
    cancelled = false;
    circuitFails = 0;
    circuitOpen = false;

    // 等待 bundle hook 把视频号内部 API 暴露出来
    setPanel("running", "正在等待视频号接口就绪…");
    var t = 0;
    while (
      (!WXU.API4 || typeof WXU.API4.finderGetFollowList !== "function" ||
        !WXU.API || typeof WXU.API.finderUserPage !== "function") &&
      t < 60
    ) {
      await sleep(500);
      t++;
    }
    if (!WXU.API4 || typeof WXU.API4.finderGetFollowList !== "function") {
      finish("接口未就绪，请在微信里重新打开视频号页面后再试");
      running = false;
      return;
    }

    try {
      // 增量基线：每作者已同步的作品 id（命中即停）。接口失败则视为全量。
      var knownMap = {};
      try {
        var ret = await WXU.request({ method: "GET", url: "/__wx_channels_api/synced-feed-ids" });
        var map = ret && ret[1];
        if (map && typeof map === "object") {
          Object.keys(map).forEach(function (u) {
            var s = {};
            (map[u] || []).forEach(function (id) {
              s[id] = 1;
            });
            knownMap[u] = s;
          });
        }
      } catch (_) {}

      setPanel("running", "正在获取关注列表…");
      var authors = await fetchAllFollows(function (n, total) {
        setPanel("running", "正在获取关注列表… 已发现 " + n + (total ? "/" + total : "") + " 个");
      });
      if (authors.length === 0) {
        finish("没有发现关注的视频号");
        return;
      }

      // 会话预算：本次只处理 SESSION_AUTHOR_CAP 个，用 localStorage 偏移轮替，避免尾部作者被饿死
      var startIdx = 0;
      try {
        startIdx = parseInt(localStorage.getItem("wx_harvest_offset") || "0", 10) || 0;
      } catch (_) {}
      if (startIdx >= authors.length) startIdx = 0;
      var cap = Math.min(SESSION_AUTHOR_CAP, authors.length);

      var done = 0, totalVideos = 0, failed = 0, cappedAuthors = 0;
      for (var i = 0; i < cap; i++) {
        if (cancelled || circuitOpen) break;
        var a = authors[(startIdx + i) % authors.length];
        var headLine = "采集中 " + (i + 1) + "/" + cap + "：" + esc(a.nickname || a.username);
        setPanel("running", headLine + "<br>累计 " + totalVideos + " 条新作品");
        try {
          var res = await harvestAuthor(a, knownMap[a.username] || {}, function () { return cancelled; }, function (cur) {
            setPanel("running", headLine + "<br>本作者 " + cur + " 条 · 累计 " + (totalVideos + cur) + " 条");
          });
          totalVideos += res.total;
          if (res.capped) cappedAuthors++;
          done++;
        } catch (ex) {
          failed++;
        }
        if (!cancelled && !circuitOpen && i < cap - 1) await jitterSleep(AUTHOR_JITTER_MS);
      }

      // 推进轮替偏移，下次从未覆盖处继续
      try {
        localStorage.setItem("wx_harvest_offset", String((startIdx + done) % authors.length));
      } catch (_) {}

      var remaining = authors.length - cap;
      var tail =
        (remaining > 0 ? "（本次处理 " + cap + " 个，剩余 " + remaining + " 个下次继续）" : "") +
        (cappedAuthors ? "，" + cappedAuthors + " 个作者达单作者上限 " + MAX_ITEMS_PER_AUTHOR + " 条（更早作品未采）" : "") +
        (failed ? "，" + failed + " 个失败" : "");

      if (circuitOpen) {
        finish("检测到接口连续异常，已暂停 · 本次已采 " + totalVideos + " 条新作品。建议稍后在微信重开视频号页再试");
      } else if (cancelled) {
        finish("已停止 · 已采 " + totalVideos + " 条新作品 / " + done + " 个作者");
      } else {
        finish("完成 · 共采 " + totalVideos + " 条新作品，覆盖 " + done + " 个作者" + tail);
      }
    } catch (ex) {
      finish("失败：" + (ex && ex.message ? ex.message : ex));
    } finally {
      running = false;
    }
  }

  // ---- 浮窗面板（触发按钮 + 进度 + 停止三态合一）----
  var $panel = null;
  var BTN_STYLE = "border:0;border-radius:16px;padding:8px 14px;background:#07C160;color:#fff;font-size:13px;font-weight:600;cursor:pointer;";

  function ensurePanel() {
    if ($panel) return;
    $panel = document.createElement("div");
    $panel.style.cssText =
      "position:fixed;right:20px;bottom:20px;z-index:100000;background:rgba(30,30,35,0.95);" +
      "color:#fff;padding:12px 14px;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,0.35);" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;" +
      "max-width:320px;min-width:180px;line-height:1.5;";
    document.body.appendChild($panel);
  }

  function setPanel(state, msg) {
    ensurePanel();
    if (state === "idle") {
      $panel.innerHTML = '<button id="wx-harvest-btn" style="' + BTN_STYLE + '">🔄 一键采集全部关注</button>';
      $panel.querySelector("#wx-harvest-btn").onclick = harvestAll;
    } else if (state === "running") {
      $panel.innerHTML =
        '<div style="margin-bottom:8px;">' + msg + "</div>" +
        '<button id="wx-harvest-stop" style="' + BTN_STYLE + "background:#888;\">停止</button>";
      $panel.querySelector("#wx-harvest-stop").onclick = function () {
        cancelled = true;
      };
    } else {
      $panel.innerHTML =
        '<div style="margin-bottom:8px;">' + msg + "</div>" +
        '<button id="wx-harvest-again" style="' + BTN_STYLE + '">再次采集</button>';
      $panel.querySelector("#wx-harvest-again").onclick = function () {
        setPanel("idle", "");
      };
    }
  }

  function finish(msg) {
    setPanel("done", msg);
  }

  var remotePollBusy = false;
  async function pollRemoteRefreshCommand() {
    if (remotePollBusy) return;
    remotePollBusy = true;
    try {
      // Keep reporting readiness even while collecting. No desktop focus or
      // visible rendering is needed for the backend to detect this page.
      var apiReady = !!(WXU.API && typeof WXU.API.finderUserPage === "function");
      var ret = await WXU.request({
        method: "GET",
        url: "/__wx_channels_api/refresh-command?page_id=" + encodeURIComponent(pageId) +
          "&api_ready=" + (apiReady ? "1" : "0") + "&busy=" + (running ? "1" : "0"),
      });
      var command = ret && ret[1];
      // 兼容 request 包装器返回 data 或完整响应对象两种形式。
      if (command && command.code === 0 && Object.prototype.hasOwnProperty.call(command, "data")) {
        command = command.data;
      }
      if (command && command.task_id) runRemoteFavoritesRefresh(command);
    } catch (_) {
    } finally {
      remotePollBusy = false;
    }
  }

  var iv = setInterval(function () {
    if (document.body) {
      clearInterval(iv);
      setPanel("idle", "");
      pollRemoteRefreshCommand();
      setInterval(pollRemoteRefreshCommand, 2000);
    }
  }, 100);
})();
