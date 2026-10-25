/* 页面控制层：负责与服务接口交互，并把状态同步到界面。
   绘图细节全部交给 chart.js，本文件不直接操作画布。 */
(function () {
  'use strict';

  var METRICS = [
    { key: 'view', label: '播放量', color: '#2f6feb' },
    { key: 'danmaku', label: '弹幕数', color: '#8b5cf6' },
    { key: 'reply', label: '评论数', color: '#0ea5e9' },
    { key: 'online', label: '在看人数', color: '#f59e0b' },
    { key: 'like', label: '点赞数', color: '#ef4444' },
    { key: 'coin', label: '投币数', color: '#10b981' },
    { key: 'favorite', label: '收藏数', color: '#ec4899' },
    { key: 'share', label: '分享数', color: '#64748b' }
  ];

  var RANGES = [
    { key: 'all', label: '全部', ms: null },
    { key: '5m', label: '5 分钟', ms: 5 * 60000 },
    { key: '30m', label: '30 分钟', ms: 30 * 60000 },
    { key: '2h', label: '2 小时', ms: 2 * 3600000 },
    { key: '12h', label: '12 小时', ms: 12 * 3600000 }
  ];

  var MIN_INTERVAL = 5;
  var MAX_VIDEOS = 5;
  var MAX_INTERVAL = 3600;
  var POLL_INTERVAL = 2000;
  /* 状态区倒计时每秒走一次，与采样轮询解耦 */
  var CLOCK_INTERVAL_MS = 1000;
  var HINT_MS = 3200;
  var HINT_ERROR_MS = 5200;
  /* 指标卡片迷你折线保留的点数 */
  var SPARK_POINTS = 60;
  /* generated_at 精确到秒的长度 */
  var ISO_SECONDS = 19;
  /* 单个请求的最长等待，超时按失败处理，让轮询能继续 */
  var REQUEST_TIMEOUT_MS = 15000;

  var el = {};
  var chart = null;
  var hintTimer = null;

  var state = {
    samples: [],
    lastT: 0,
    running: false,
    busy: false,
    interval: 30,
    video: null,
    startedAt: null,
    stoppedAt: null,
    failureCount: 0,
    lastError: null,
    report: null,
    settings: { auto_save: true, auto_save_dir: '', align_clock: false, import_dirs: [] },
    reportDir: '',
    cooldownUntil: 0,
    nextSampleAt: 0,
    monitors: [],
    activeMonitor: null,
    syncedBvid: null,
    syncedInterval: null,
    metric: 'view',
    range: 'all'
  };

  // ---------------------------------------------------------------- 启动

  function init() {
    [
      'statusDot', 'statusText', 'metaInterval', 'metaSamples', 'metaUptime',
      'metaFailures', 'videoList', 'listEmpty', 'listCount', 'createBtn',
      'monitorToggle', 'monitorCount', 'monitorPop',
      'sourceInput', 'intervalInput', 'configForm', 'startBtn',
      'stopBtn', 'videoCard', 'videoCover', 'videoTitle', 'videoMeta',
      'metricTabs', 'rangeTabs', 'metricList', 'reportPanel', 'reportHint',
      'chartNote', 'metaLatest', 'reportName', 'reportSamples', 'reportDuration',
      'reportTime', 'reportSavedPath', 'autoSaveToggle', 'autoSaveDir',
      'alignClockToggle', 'pickDirBtn', 'intervalHelp', 'intervalTip', 'downloadLink'
    ].forEach(function (id) { el[id] = document.getElementById(id); });

    buildTabs();
    buildMetricList();
    chart = new LineChart(document.getElementById('mainChart'));
    bindEvents();

    // 供 report-page.js 复用请求与提示能力
    window.WB = {
      getJSON: getJSON,
      postJSON: postJSON,
      showHint: showHint,
      showConfirm: showConfirm,
      showDialog: showDialog
    };

    bootstrap();
    window.setInterval(poll, POLL_INTERVAL);
    window.setInterval(renderStatus, CLOCK_INTERVAL_MS);
  }

  function buildTabs() {
    METRICS.forEach(function (metric) {
      el.metricTabs.appendChild(makeTab(metric.label, metric.key, state.metric === metric.key, function () {
        selectMetric(metric.key);
      }));
    });

    RANGES.forEach(function (range) {
      el.rangeTabs.appendChild(makeTab(range.label, range.key, state.range === range.key, function () {
        state.range = range.key;
        updateTabStates(el.rangeTabs, range.key);
        renderChart();
      }));
    });
  }

  function makeTab(label, key, active, onClick) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'tab' + (active ? ' is-active' : '');
    button.dataset.key = key;
    button.textContent = label;
    button.addEventListener('click', onClick);
    return button;
  }

  function buildMetricList() {
    METRICS.forEach(function (metric) {
      var item = document.createElement('li');
      item.className = 'metric-item';
      item.dataset.key = metric.key;

      var row = document.createElement('div');
      row.className = 'metric-row';
      var label = document.createElement('span');
      label.className = 'metric-label';
      label.textContent = metric.label;
      var delta = document.createElement('span');
      delta.className = 'metric-delta is-flat';
      delta.textContent = '—';
      row.appendChild(label);
      row.appendChild(delta);

      var value = document.createElement('div');
      value.className = 'metric-value';
      value.textContent = '—';

      var spark = document.createElement('canvas');
      spark.className = 'spark';

      item.appendChild(row);
      item.appendChild(value);
      item.appendChild(spark);
      item.addEventListener('click', function () { selectMetric(metric.key); });
      el.metricList.appendChild(item);
    });
  }

  function bindEvents() {
    el.configForm.addEventListener('submit', onStart);
    el.stopBtn.addEventListener('click', onStop);
    el.intervalInput.addEventListener('change', onIntervalChange);
    el.intervalHelp.addEventListener('click', onToggleHelp);
    el.autoSaveToggle.addEventListener('change', onAutoSaveToggle);
    el.alignClockToggle.addEventListener('change', onAlignClockToggle);
    el.pickDirBtn.addEventListener('click', onPickDirectory);
    el.downloadLink.addEventListener('click', onDownload);
    el.createBtn.addEventListener('click', onCreateMonitor);
    el.monitorToggle.addEventListener('click', onToggleMonitorPop);
    document.addEventListener('click', onDocumentClickForPop);
    document.addEventListener('click', onDocumentClick);
    window.addEventListener('resize', function () {
      if (!el.monitorPop.hidden) positionMonitorPop();
    });

    var nav = document.getElementById('mainNav');
    if (nav) {
      nav.addEventListener('click', function (event) {
        var button = event.target.closest('.nav-item');
        if (button) onNavClick(button);
      });
    }
  }

  /* 导航目前只有壳：切换高亮，非监控页给出提示，页面内容暂不切换 */
  function onNavClick(button) {
    var page = button.dataset.page;
    Array.prototype.forEach.call(button.parentNode.children, function (node) {
      node.classList.toggle('is-active', node === button);
    });

    var monitorView = document.getElementById('monitorView');
    var reportView = document.getElementById('reportView');

    if (page === 'report') {
      monitorView.hidden = true;
      reportView.hidden = false;
      if (window.ReportPage) window.ReportPage.open();
      return;
    }

    reportView.hidden = true;
    monitorView.hidden = false;
    if (page !== 'monitor') {
      showHint('「' + button.textContent + '」页面尚未实现', 'info');
    }
  }

  function onToggleMonitorPop(event) {
    event.stopPropagation();
    if (el.monitorPop.hidden) openMonitorPop();
    else closeMonitorPop();
  }

  function openMonitorPop() {
    el.monitorPop.hidden = false;
    positionMonitorPop();
    el.monitorToggle.classList.add('is-open');
    el.monitorToggle.setAttribute('aria-expanded', 'true');
  }

  /* 浮层对齐到按钮正下方；靠近屏幕边缘时自动内缩，窄屏才贴边 */
  function positionMonitorPop() {
    var rect = el.monitorToggle.getBoundingClientRect();
    var width = el.monitorPop.offsetWidth || 304;
    var left = rect.left + rect.width / 2 - width / 2;
    var maxLeft = window.innerWidth - width - 12;
    left = Math.max(12, Math.min(left, Math.max(12, maxLeft)));
    el.monitorPop.style.left = Math.round(left) + 'px';
    el.monitorPop.style.top = Math.round(rect.bottom + 8) + 'px';
  }

  function closeMonitorPop() {
    el.monitorPop.hidden = true;
    el.monitorToggle.classList.remove('is-open');
    el.monitorToggle.setAttribute('aria-expanded', 'false');
  }

  /* 点击浮层或触发按钮之外的区域时收起，事件本身照常传递给目标元素 */
  function onDocumentClickForPop(event) {
    if (el.monitorPop.hidden) return;
    if (event.target.closest('#monitorPop') || event.target.closest('#monitorToggle')) return;
    closeMonitorPop();
  }

  function onCreateMonitor() {
    postJSON('/api/monitor/create', {})
      .then(function (data) {
        invalidatePendingPoll();
        applySnapshot(data, { replace: true, syncSource: true });
        playSwitchAnimation();
        closeMonitorPop();
        showHint('已新建监控，填入链接后点「开始监控」', 'ok');
      })
      .catch(function (error) { showHint(error.message, 'error'); });
  }

  // ---------------------------------------------------------------- 交互

  function selectMetric(key) {
    state.metric = key;
    updateTabStates(el.metricTabs, key);
    renderMetrics();
    renderChart();
  }

  function updateTabStates(container, key) {
    Array.prototype.forEach.call(container.children, function (tab) {
      tab.classList.toggle('is-active', tab.dataset.key === key);
    });
  }

  function onToggleHelp(event) {
    event.preventDefault();
    event.stopPropagation();
    var willOpen = el.intervalTip.hidden;
    el.intervalTip.hidden = !willOpen;
    el.intervalHelp.classList.toggle('is-open', willOpen);
  }

  function onDocumentClick(event) {
    if (el.intervalTip.hidden) return;
    if (el.intervalTip.contains(event.target) || el.intervalHelp.contains(event.target)) return;
    el.intervalTip.hidden = true;
    el.intervalHelp.classList.remove('is-open');
  }

  function readInterval() {
    var raw = el.intervalInput.value.trim();
    if (!raw) return { error: '请填写采样周期' };
    var value = Number(raw);
    if (!isFinite(value) || Math.floor(value) !== value) return { error: '采样周期必须是整数秒' };
    if (value < MIN_INTERVAL) return { error: '采样周期不能小于 ' + MIN_INTERVAL + ' 秒' };
    if (value > MAX_INTERVAL) return { error: '采样周期不能大于 ' + MAX_INTERVAL + ' 秒' };
    return { value: value };
  }

  async function onStart(event) {
    event.preventDefault();
    var source = el.sourceInput.value.trim();
    if (!source) {
      showHint('请先填写视频链接或 BV 号', 'error');
      return;
    }

    var parsed = readInterval();
    if (parsed.error) {
      showHint(parsed.error, 'error');
      return;
    }

    // 已绑定视频且输入指向另一视频时，让用户决定是改当前监控还是另开一个
    var targetMonitor = state.activeMonitor;
    if (state.video && state.video.bvid) {
      if (source.indexOf(state.video.bvid) < 0) {
        var choice = await showDialog(
          '当前监控正在监控「' + state.video.title + '」，新链接指向其他视频。要怎么处理？',
          [
            { label: '取消', value: null },
            { label: '修改当前监控', value: 'replace' },
            { label: '新开监控', value: 'new', primary: true }
          ]
        );
        if (!choice) return;

        if (choice === 'new') {
          setBusy(true);
          try {
            var created = await postJSON('/api/monitor/create', {});
            invalidatePendingPoll();
            applySnapshot(created, { replace: true, syncSource: true });
            targetMonitor = state.activeMonitor;
          } catch (error) {
            showHint(error.message, 'error');
            setBusy(false);
            return;
          }
          setBusy(false);
        } else if (state.running) {
          var replaceOk = await showConfirm(
            '将先结束当前监控（并生成报告），再以新链接重新开始。是否继续？'
          );
          if (!replaceOk) return;
        }
      } else if (state.running) {
        var restartOk = await showConfirm(
          '当前监控正在运行，将先结束本次监控并重新开始。是否继续？'
        );
        if (!restartOk) return;
      }
    } else if (state.running) {
      var runOk = await showConfirm(
        '当前监控正在运行，将先结束本次监控，再以新链接重新开始。是否继续？'
      );
      if (!runOk) return;
    }

    setBusy(true);
    // 列表为空时先自动新建一个监控，再开始采集
    var ensure = targetMonitor
      ? Promise.resolve()
      : postJSON('/api/monitor/create', {}).then(function (created) {
          invalidatePendingPoll();
          applySnapshot(created, { replace: true });
        });

    ensure
      .then(function () {
        return postJSON('/api/monitor/start', {
          monitor_id: targetMonitor || state.activeMonitor,
          source: source,
          interval: parsed.value,
          align_clock: !!state.settings.align_clock
        });
      })
      .then(function (data) {
        invalidatePendingPoll();
        applySnapshot(data, { replace: true });
        if (data.previous_report) {
          state.report = data.previous_report;
          renderReport(data.previous_report);
          showHint('上一段监控已结束，报告可在下方下载', 'info');
        } else {
          renderReport(null);
          if (!data.sample_count && data.next_sample_at) {
            showHint(
              '整点采样已启用，将于 '
                + chartFormatTime(data.next_sample_at, 0, true)
                + ' 开始采集',
              'info'
            );
          } else if (data.align_clock) {
            showHint('监控已启动，整点采样已对齐', 'ok');
          } else {
            showHint('监控已启动，正在按 ' + data.interval + ' 秒周期采集', 'ok');
          }
        }
      })
      .catch(function (error) {
        showHint(error.message, 'error');
        if (error.retryAfter > 0) {
          state.cooldownUntil = Date.now() + error.retryAfter * 1000;
          renderStatus();
        }
      })
      .finally(function () { setBusy(false); });
  }

  /* 页面内对话框：替代浏览器原生 confirm，按钮由调用方决定 */
  function showDialog(message, actions) {
    return new Promise(function (resolve) {
      var overlay = document.getElementById('confirmOverlay');
      var messageNode = document.getElementById('confirmMessage');
      var actionsNode = document.getElementById('confirmActions');
      var settled = false;
      var buttons = [];

      messageNode.textContent = message;
      actionsNode.innerHTML = '';
      overlay.hidden = false;

      actions.forEach(function (action) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ' + (action.primary ? 'btn-primary' : 'btn-ghost');
        button.textContent = action.label;
        button.addEventListener('click', function () { finish(action.value); });
        actionsNode.appendChild(button);
        buttons.push(button);
      });
      if (buttons.length) buttons[buttons.length - 1].focus();

      function finish(value) {
        if (settled) return;
        settled = true;
        overlay.hidden = true;
        actionsNode.innerHTML = '';
        document.removeEventListener('keydown', onKeydown);
        resolve(value);
      }

      function onKeydown(event) {
        if (event.key === 'Escape') finish(null);
      }

      document.addEventListener('keydown', onKeydown);
    });
  }

  function showConfirm(message) {
    return showDialog(message, [
      { label: '取消', value: false },
      { label: '继续', value: true, primary: true }
    ]).then(function (value) { return value === true; });
  }

  function onStop() {
    setBusy(true);
    postJSON('/api/monitor/stop', { monitor_id: state.activeMonitor })
      .then(function (data) {
        invalidatePendingPoll();
        applySnapshot(data, { replace: true });
        if (data.report) {
          state.report = data.report;
          renderReport(data.report);
          if (data.report.saved_path) {
            showHint('监控已停止，报告已保存到 ' + data.report.saved_path, 'ok');
          } else if (data.report.save_error) {
            showHint('监控已停止，自动保存失败：' + data.report.save_error, 'error');
          } else {
            showHint('监控已停止，报告已生成，可在下方下载', 'ok');
          }
        } else {
          renderReport(null);
          showHint('监控已停止，本次没有采集到数据，未生成报告', 'info');
        }
      })
      .catch(function (error) { showHint(error.message, 'error'); })
      .finally(function () { setBusy(false); });
  }

  function onIntervalChange() {
    var parsed = readInterval();
    if (parsed.error) {
      showHint(parsed.error, 'error');
      return;
    }
    postJSON('/api/monitor/interval', { interval: parsed.value })
      .then(function (data) {
        state.interval = data.interval;
        state.syncedInterval = data.interval;
        el.intervalInput.value = data.interval;
        renderStatus();
        if (state.running) {
          showHint('采样周期已改为 ' + data.interval + ' 秒，下个周期起生效', 'ok');
        }
      })
      .catch(function (error) { showHint(error.message, 'error'); });
  }

  function onDownload(event) {
    if (!state.report) {
      event.preventDefault();
      showHint('当前没有可下载的报告，请先停止一次监控', 'error');
      return;
    }
    // 浏览器下载没有目标路径可回显，提示已开始下载哪一份
    showHint('已开始下载 ' + (state.report.name || '报告'), 'ok');
  }

  // ---------------------------------------------------------------- 设置

  function onAutoSaveToggle() {
    var enabled = el.autoSaveToggle.checked;
    if (!enabled) {
      saveSettings({ auto_save: false }, '已关闭自动保存');
      return;
    }
    // 目录留空表示保存到仓库目录，不再强制弹目录选择
    var directory = state.settings.auto_save_dir;
    saveSettings(
      { auto_save: true },
      directory ? '停止监控时将自动保存报告到 ' + directory : '报告将自动保存到仓库目录'
    );
  }

  function onAlignClockToggle() {
    var enabled = el.alignClockToggle.checked;
    saveSettings(
      { align_clock: enabled },
      enabled ? '已开启整点采样，首次采集会对齐到最近整点' : '已关闭整点采样'
    );
  }

  function onPickDirectory() {
    pickDirectory().then(function (path) {
      if (!path) return;
      saveSettings({ auto_save_dir: path }, '保存目录已设为 ' + path);
    }).catch(function (error) { showHint(error.message, 'error'); });
  }

  function pickDirectory() {
    if (!window.FilePicker) return Promise.resolve(null);

    el.pickDirBtn.disabled = true;
    return window.FilePicker.open({
      mode: 'dirs',
      multi: false,
      title: '选择保存目录',
      initialPath: state.settings.auto_save_dir || ''
    })
      .then(function (paths) { return paths && paths.length ? paths[0] : null; })
      .finally(function () { el.pickDirBtn.disabled = false; });
  }

  function saveSettings(patch, message) {
    return postJSON('/api/settings', patch)
      .then(function (data) {
        state.settings = data;
        renderSettings();
        if (message) showHint(message, 'ok');
      })
      .catch(function (error) { showHint(error.message, 'error'); });
  }

  // ---------------------------------------------------------------- 数据

  function bootstrap() {
    getJSON('/api/state')
      .then(function (data) {
        applySnapshot(data, { replace: true, syncSource: true });
        // 打开页面时保证至少有一个监控槽位，用户可直接填链接
        if (!state.monitors.length) {
          return postJSON('/api/monitor/create', {}).then(function (created) {
            applySnapshot(created, { replace: true, syncSource: true });
          });
        }
      })
      .catch(function (error) { showHint('无法连接本地服务：' + error.message, 'error'); });
  }

  var polling = false;
  /* 主动操作（切换/新建/启停/移除）会递增该值，让在途的轮询响应作废，
     否则旧响应可能把刚切换的监控覆盖回去 */
  var snapshotEpoch = 0;

  function invalidatePendingPoll() {
    snapshotEpoch += 1;
  }

  function poll() {
    if (polling) return;
    polling = true;
    var epoch = snapshotEpoch;

    var query = state.lastT ? '/api/state?since=' + state.lastT : '/api/state';
    getJSON(query)
      .then(function (data) {
        if (epoch !== snapshotEpoch) return;
        var switched = data.active_monitor !== state.activeMonitor;
        var needReset = switched || data.sample_count < state.samples.length ||
          (data.started_at && state.startedAt && data.started_at !== state.startedAt);
        if (needReset) {
          return getJSON('/api/state').then(function (full) {
            if (epoch !== snapshotEpoch) return;
            applySnapshot(full, { replace: true });
          });
        }
        applySnapshot(data, { replace: false });
      })
      .catch(function () {
        el.statusDot.className = 'status-dot is-error';
        el.statusText.textContent = '与本地服务断开，请检查启动窗口';
      })
      .finally(function () { polling = false; });
  }

  function applySnapshot(data, options) {
    var replace = options && options.replace;
    var switched = typeof data.active_monitor === 'string' && data.active_monitor !== state.activeMonitor;
    if (replace || switched) {
      state.samples = [];
      state.lastT = 0;
    }

    var incoming = data.samples || [];
    for (var i = 0; i < incoming.length; i += 1) {
      if (incoming[i].t > state.lastT) {
        state.samples.push(incoming[i]);
        state.lastT = incoming[i].t;
      }
    }

    if (data.sample_count === 0) {
      state.samples = [];
      state.lastT = 0;
    }

    state.running = !!data.running;
    state.interval = data.interval || state.interval;
    state.video = data.video || null;
    state.startedAt = data.started_at || null;
    state.stoppedAt = data.stopped_at || null;
    state.failureCount = data.failure_count || 0;
    state.lastError = data.last_error || null;
    state.nextSampleAt = data.next_sample_at || 0;
    state.activeMonitor = data.active_monitor || null;
    state.monitors = data.monitors || [];
    if (data.settings) state.settings = data.settings;

    // 服务端仍在冷却时，页面刷新后也要显示剩余等待时间
    if (data.cooldown_remaining > 0) {
      state.cooldownUntil = Date.now() + data.cooldown_remaining * 1000;
    }

    state.report = data.report || null;

    // 切换或新建监控时，把输入框同步为该监控绑定的视频；
    // 普通轮询不覆盖，避免打断用户正在输入的新链接
    var syncing = (options && options.syncSource) || switched;
    if (syncing && document.activeElement !== el.sourceInput) {
      el.sourceInput.value = (data.video && data.video.bvid) || '';
    }
    if (data.report_dir) state.reportDir = data.report_dir;

    renderAll();
    renderSettings();

    if (!data.running && state.report) {
      renderReport(state.report);
    }
  }

  // ---------------------------------------------------------------- 渲染

  function renderAll() {
    renderMonitorList();
    renderStatus();
    renderVideo();
    renderMetrics();
    renderChart();
  }

  function renderMonitorList() {
    var list = el.videoList;
    list.innerHTML = '';

    el.listEmpty.hidden = state.monitors.length > 0;
    el.listCount.textContent = state.monitors.length + '/' + MAX_VIDEOS;
    el.monitorCount.textContent = state.monitors.length;

    state.monitors.forEach(function (monitor) {
      var item = document.createElement('li');
      item.className = 'video-entry' + (monitor.id === state.activeMonitor ? ' is-active' : '');

      if (monitor.configured) {
        var cover = document.createElement('img');
        cover.className = 'entry-cover';
        cover.src = monitor.cover;
        cover.alt = '';
        cover.referrerPolicy = 'no-referrer';
        item.appendChild(cover);
      } else {
        var placeholder = document.createElement('span');
        placeholder.className = 'entry-cover entry-placeholder';
        placeholder.textContent = '＋';
        item.appendChild(placeholder);
      }

      var body = document.createElement('div');
      body.className = 'entry-body';

      var title = document.createElement('span');
      title.className = 'entry-title';
      title.textContent = monitor.title;
      title.title = monitor.title;

      var meta = document.createElement('span');
      meta.className = 'entry-meta' + (monitor.running ? ' is-running' : '');
      meta.textContent = monitor.configured
        ? (monitor.running ? '监控中' : '已停止') + ' · ' + monitor.sample_count + ' 点'
        : '未配置';

      body.appendChild(title);
      body.appendChild(meta);

      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'entry-remove';
      remove.textContent = '×';
      remove.setAttribute('aria-label', '移除该监控');
      remove.addEventListener('click', function (event) {
        event.stopPropagation();
        onRemoveMonitor(monitor);
      });

      item.appendChild(body);
      item.appendChild(remove);
      item.addEventListener('click', function () { onSelectMonitor(monitor); });

      list.appendChild(item);
    });
  }

  function onSelectMonitor(monitor) {
    var sameMonitor = monitor.id === state.activeMonitor;
    closeMonitorPop();

    if (sameMonitor) {
      // 已经是当前监控，只需回到监控数据页
      switchToMonitorPage();
      return;
    }
    postJSON('/api/monitor/select', { monitor_id: monitor.id })
      .then(function (data) {
        invalidatePendingPoll();
        applySnapshot(data, { replace: true, syncSource: true });
        playSwitchAnimation();
        switchToMonitorPage();
      })
      .catch(function (error) { showHint(error.message, 'error'); });
  }

  /* 从监控列表选中某个监控后，总是回到「监控数据」页 */
  function switchToMonitorPage() {
    var nav = document.getElementById('mainNav');
    if (!nav) return;
    var button = nav.querySelector('[data-page="monitor"]');
    if (button && !button.classList.contains('is-active')) onNavClick(button);
  }

  /* 切换监控时给主视图一次淡入上移，模拟浏览器换页 */
  function playSwitchAnimation() {
    [
      document.querySelector('.main-column'),
      document.querySelector('.metrics-panel'),
      document.querySelector('.status-panel')
    ].forEach(function (node) {
      if (!node) return;
      node.classList.remove('is-switching');
      void node.offsetWidth;
      node.classList.add('is-switching');
    });
  }

  function onRemoveMonitor(monitor) {
    var doRemove = function () {
      postJSON('/api/monitor/remove', { monitor_id: monitor.id })
        .then(function (data) {
          invalidatePendingPoll();
          applySnapshot(data, { replace: true });
          showHint('已移除「' + monitor.title + '」', 'info');
        })
        .catch(function (error) { showHint(error.message, 'error'); });
    };

    if (monitor.running) {
      showConfirm('「' + monitor.title + '」正在监控中，移除将停止采集并保留报告。是否继续？')
        .then(function (confirmed) { if (confirmed) doRemove(); });
      return;
    }
    doRemove();
  }

  function renderStatus() {
    var dotClass = 'status-dot';
    if (state.running) dotClass += ' is-running';
    else if (state.lastError) dotClass += ' is-error';
    el.statusDot.className = dotClass;

    if (state.running) {
      if (!state.samples.length && state.nextSampleAt > Date.now()) {
        el.statusText.textContent = '等待 '
          + chartFormatTime(state.nextSampleAt, 0, true) + ' 开始采集';
      } else {
        el.statusText.textContent = state.lastError ? '监控中（上次采样失败）' : '监控中';
      }
    } else if (state.startedAt) {
      el.statusText.textContent = '监控已停止';
    } else {
      el.statusText.textContent = '尚未开始监控';
    }

    el.metaInterval.textContent = state.interval + ' 秒';
    el.metaSamples.textContent = state.samples.length;
    el.metaFailures.textContent = state.failureCount;
    el.metaUptime.textContent = uptimeText();
    el.metaLatest.textContent = latestSampleText();

    // 运行中的主按钮为白底黑字的「重启」，未运行时为主色「开始监控」；
    // 处于限流冷却期时改为倒计时并禁用，避免反复点击延长限制
    var cooling = state.cooldownUntil > Date.now();
    if (cooling) {
      el.startBtn.textContent = '等待 ' + Math.ceil((state.cooldownUntil - Date.now()) / 1000) + ' 秒';
      el.startBtn.className = 'btn btn-ghost';
      el.startBtn.disabled = true;
    } else {
      if (state.cooldownUntil) state.cooldownUntil = 0;
      if (state.running) {
        el.startBtn.textContent = '重启';
        el.startBtn.className = 'btn btn-ghost';
      } else {
        el.startBtn.textContent = '开始监控';
        el.startBtn.className = 'btn btn-primary';
      }
      el.startBtn.disabled = state.busy;
    }
    el.stopBtn.disabled = state.busy || !state.running;

    // 仅在服务端周期发生变化时回填，避免用户清空后又被写回
    if (state.syncedInterval !== state.interval) {
      state.syncedInterval = state.interval;
      el.intervalInput.value = state.interval;
    }
  }

  function renderVideo() {
    var video = state.video;
    if (!video) {
      el.videoCard.hidden = true;
      return;
    }
    el.videoCard.hidden = false;
    if (video.cover && el.videoCover.getAttribute('src') !== video.cover) {
      el.videoCover.src = video.cover;
    }
    el.videoTitle.textContent = video.title;
    el.videoTitle.href = video.url || '#';

    // 仅在视频切换时回填链接，用户手动清空后不再覆盖
    if (state.syncedBvid !== video.bvid) {
      state.syncedBvid = video.bvid;
      el.sourceInput.value = video.url || video.bvid;
    }

    el.videoMeta.textContent = '';
    [
      ['UP主', video.owner],
      ['BV号', video.bvid],
      ['时长', formatDuration(video.duration)],
      ['发布', video.pubdate ? formatDate(video.pubdate) : '—']
    ].forEach(function (pair) {
      var span = document.createElement('span');
      span.textContent = pair[0] + ' ' + pair[1];
      el.videoMeta.appendChild(span);
    });
  }

  function renderMetrics() {
    var items = el.metricList.children;
    for (var i = 0; i < items.length; i += 1) {
      var item = items[i];
      var key = item.dataset.key;
      var color = colorOf(key);
      var values = [];
      var first = null;
      var last = null;

      for (var j = 0; j < state.samples.length; j += 1) {
        var value = state.samples[j][key];
        if (typeof value === 'number') {
          if (first === null) first = value;
          last = value;
          values.push(value);
        }
      }

      var valueNode = item.querySelector('.metric-value');
      var deltaNode = item.querySelector('.metric-delta');
      var spark = item.querySelector('.spark');

      valueNode.textContent = last === null ? '—' : formatNumber(last, true);

      if (first === null) {
        deltaNode.textContent = '—';
        deltaNode.className = 'metric-delta is-flat';
      } else {
        var delta = last - first;
        deltaNode.textContent = (delta > 0 ? '+' : '') + formatNumber(delta, true);
        deltaNode.className = 'metric-delta ' + (delta > 0 ? 'is-up' : delta < 0 ? 'is-down' : 'is-flat');
      }

      item.classList.toggle('is-active', key === state.metric);
      drawSparkline(spark, values.slice(-SPARK_POINTS), color);
    }
  }

  function renderChart() {
    var metric = metricOf(state.metric);
    var range = rangeOf(state.range);
    var latest = state.samples.length ? state.samples[state.samples.length - 1].t : Date.now();
    var cutoff = range.ms ? latest - range.ms : null;

    var points = [];
    for (var i = 0; i < state.samples.length; i += 1) {
      var sample = state.samples[i];
      if (cutoff !== null && sample.t < cutoff) continue;
      var value = sample[metric.key];
      if (typeof value === 'number') points.push({ t: sample.t, v: value });
    }

    var emptyText = state.samples.length
      ? '该时间范围内没有数据'
      : '暂无数据，开始监控后将实时绘制';
    chart.setData(points, metric.color, emptyText);
    renderChartNote(points);
  }

  /* 图表脚注只保留一条解释性说明，其余状态信息已并入状态栏 */
  function renderChartNote(points) {
    if (points.length < 3) {
      el.chartNote.hidden = true;
      return;
    }

    // 相邻点大量相等说明该指标是批量更新，台阶属于真实跳变
    var repeats = 0;
    for (var i = 1; i < points.length; i += 1) {
      if (points[i].v === points[i - 1].v) repeats += 1;
    }
    var ratio = repeats / (points.length - 1);
    if (ratio >= 0.5) {
      el.chartNote.hidden = false;
      el.chartNote.textContent = '该指标由按分钟更新，曲线中的台阶是数据源的真实采样，不是绘制误差。';
    } else {
      el.chartNote.hidden = true;
    }
  }

  function renderReport(report) {
    if (!report) {
      el.reportPanel.hidden = true;
      return;
    }

    el.reportPanel.hidden = false;
    el.reportName.textContent = report.name || '—';
    el.reportSamples.textContent = report.sample_count === null || report.sample_count === undefined
      ? '—' : report.sample_count;
    el.reportDuration.textContent = report.duration_seconds === null || report.duration_seconds === undefined
      ? '—' : formatDuration(report.duration_seconds);
    el.reportTime.textContent = report.generated_at
      ? String(report.generated_at).replace('T', ' ').slice(0, ISO_SECONDS) : '—';

    if (report.saved_path) {
      el.reportSavedPath.textContent = '已保存至 ' + report.saved_path;
    } else if (report.save_error) {
      el.reportSavedPath.textContent = '自动保存失败：' + report.save_error;
    } else {
      el.reportSavedPath.textContent = '';
    }
  }

  function renderSettings() {
    el.autoSaveToggle.checked = !!state.settings.auto_save;
    var repoPath = state.reportDir;
    el.autoSaveDir.textContent = state.settings.auto_save_dir || repoPath || '仓库目录';
    el.autoSaveDir.title = el.autoSaveDir.textContent;
    el.alignClockToggle.checked = !!state.settings.align_clock;
  }

  // ---------------------------------------------------------------- 工具

  function showHint(message, tone) {
    var node = document.getElementById('toast');
    node.textContent = message;
    node.className = 'toast toast-' + tone;
    node.hidden = false;
    window.clearTimeout(hintTimer);
    hintTimer = window.setTimeout(function () {
      node.hidden = true;
    }, tone === 'error' ? HINT_ERROR_MS : HINT_MS);
  }

  function setBusy(busy) {
    state.busy = busy;
    el.startBtn.disabled = busy;
    el.stopBtn.disabled = busy || !state.running;
  }

  function uptimeText() {
    if (!state.startedAt) return '—';
    var end = state.stoppedAt || Date.now();
    return formatDuration((end - state.startedAt) / 1000);
  }

  function latestSampleText() {
    if (!state.samples.length) return '—';
    var last = state.samples[state.samples.length - 1].t;
    return chartFormatTime(last, Date.now() - last, true);
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
    var total = Math.max(0, Math.round(seconds));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var rest = total % 60;
    if (hours > 0) return hours + ' 小时 ' + minutes + ' 分';
    if (minutes > 0) return minutes + ' 分 ' + rest + ' 秒';
    return rest + ' 秒';
  }

  function formatDate(unixSeconds) {
    var date = new Date(unixSeconds * 1000);
    var pad = function (n) { return String(n).padStart(2, '0'); };
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
  }

  function metricOf(key) {
    for (var i = 0; i < METRICS.length; i += 1) {
      if (METRICS[i].key === key) return METRICS[i];
    }
    return METRICS[0];
  }

  function colorOf(key) {
    return metricOf(key).color;
  }

  function rangeOf(key) {
    for (var i = 0; i < RANGES.length; i += 1) {
      if (RANGES[i].key === key) return RANGES[i];
    }
    return RANGES[0];
  }

  /* 本地服务偶发无响应时，fetch 可能挂很久，轮询标志会一直被占着不释放 */
  function request(url, options) {
    var controller = new AbortController();
    var timer = window.setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
    var config = Object.assign({ cache: 'no-store' }, options || {}, { signal: controller.signal });
    return fetch(url, config)
      .catch(function (error) {
        if (error && error.name === 'AbortError') {
          throw new Error('请求超时，请检查启动窗口是否还在运行');
        }
        throw error;
      })
      .then(function (response) {
        window.clearTimeout(timer);
        return response;
      }, function (error) {
        window.clearTimeout(timer);
        throw error;
      });
  }

  function getJSON(url) {
    return request(url).then(parseResponse);
  }

  function postJSON(url, body) {
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(parseResponse);
  }

  function parseResponse(response) {
    return response.json().catch(function () { return {}; }).then(function (data) {
      if (!response.ok) {
        var error = new Error(data.error || ('请求失败 HTTP ' + response.status));
        error.status = response.status;
        error.retryAfter = data.retry_after || 0;
        throw error;
      }
      return data;
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
