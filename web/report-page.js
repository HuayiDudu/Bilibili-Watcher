/* 查看报告页：横条（导入/统计/导出）+ 报告表格（筛选排序分页）+ 详情视图。
   文件与目录选择统一走 window.FilePicker；请求与提示复用 window.WB。 */
(function () {
  'use strict';

  /* 会话内报告与目录筛选的 key 前缀，与后端 LIVE_PREFIX 对应 */
  var LIVE_PREFIX = 'live:';
  var DIR_PREFIX = 'dir:';
  var PAGE_SIZE_DEFAULT = 20;
  /* 浏览器下载多个文件时逐个触发的间隔，太密会被浏览器拦掉 */
  var EXPORT_STAGGER_MS = 300;
  var ZOOM_IN_RATE = 0.7;
  var ZOOM_OUT_RATE = 1.4;
  var PAN_RATIO = 0.25;
  var ISO_SECONDS = 19;

  var WB = null;

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

  var state = {
    saved: [],
    live: [],
    exportDir: '',
    filterSource: 'all',
    keyword: '',
    sort: { key: null, dir: null },
    reportDir: '',
    page: 1,
    pageSize: PAGE_SIZE_DEFAULT,
    selected: new Set(),
    importDirs: [],
    activeKey: null,
    doc: null,
    metrics: ['view'],
    ready: false
  };

  var el = {};
  var mainChart = null;
  var ratioChart = null;

  // ---------------------------------------------------------------- 启动

  function init() {
    WB = window.WB;
    [
      'reportView', 'importBtn', 'reportSummary', 'exportSelectedBtn',
      'gearBtn', 'gearPop', 'pickExportDirBtn', 'exportAllBtn', 'gearDirHint',
      'manageImportDirsBtn', 'importDirsOverlay', 'importDirList',
      'addImportDirBtn', 'closeImportDirsBtn',
      'reportListView', 'filterSource', 'filterKeyword', 'sortBy', 'pageSize',
      'selectAll', 'reportTbody', 'pageInfo', 'prevPageBtn', 'nextPageBtn',
      'saveLiveBtn',
      'reportDetailView', 'backToListBtn', 'detailHint',
      'reportCover', 'reportTitle', 'reportSub', 'reportMetricTabs',
      'chartTitle', 'chartLegend',
      'zoomInBtn', 'zoomOutBtn', 'panLeftBtn', 'panRightBtn', 'resetViewBtn', 'viewHint',
      'reportChart', 'ratioA', 'ratioB', 'ratioStats', 'ratioChart'
    ].forEach(function (id) { el[id] = document.getElementById(id); });

    mainChart = new LineChart(el.reportChart);
    mainChart.setInteractive(true);
    mainChart.onViewChange = function () { syncRatioView(); renderViewHint(); };

    ratioChart = new LineChart(el.ratioChart);
    ratioChart.setInteractive(false);

    buildMetricTabs();
    buildRatioSelects();
    bindEvents();
    bindSortButtons();
    renderSortButtons();

    try {
      state.exportDir = localStorage.getItem('reportExportDir') || '';
    } catch (error) {
      state.exportDir = '';
    }

    state.ready = true;
  }

  function bindEvents() {
    el.importBtn.addEventListener('click', onImport);
    el.exportSelectedBtn.addEventListener('click', onExportSelected);
    el.gearBtn.addEventListener('click', onToggleGear);
    el.pickExportDirBtn.addEventListener('click', onPickExportDir);
    el.exportAllBtn.addEventListener('click', onExportAll);
    el.manageImportDirsBtn.addEventListener('click', onManageImportDirs);
    el.addImportDirBtn.addEventListener('click', onAddImportDir);
    el.closeImportDirsBtn.addEventListener('click', closeImportDirs);
    el.importDirsOverlay.addEventListener('click', function (event) {
      if (event.target === el.importDirsOverlay) closeImportDirs();
    });
    document.addEventListener('click', onDocumentClick);

    el.filterSource.addEventListener('change', function () {
      state.filterSource = el.filterSource.value;
      state.page = 1;
      renderTable();
    });
    el.filterKeyword.addEventListener('input', function () {
      state.keyword = el.filterKeyword.value.trim().toLowerCase();
      state.page = 1;
      renderTable();
    });
    el.saveLiveBtn.addEventListener('click', onSaveLive);
    el.pageSize.addEventListener('change', function () {
      state.pageSize = Number(el.pageSize.value) || PAGE_SIZE_DEFAULT;
      state.page = 1;
      renderTable();
    });
    el.selectAll.addEventListener('change', onSelectAll);
    el.prevPageBtn.addEventListener('click', function () { goPage(state.page - 1); });
    el.nextPageBtn.addEventListener('click', function () { goPage(state.page + 1); });

    el.backToListBtn.addEventListener('click', backToList);
    el.zoomInBtn.addEventListener('click', function () { mainChart.zoom(ZOOM_IN_RATE); });
    el.zoomOutBtn.addEventListener('click', function () { mainChart.zoom(ZOOM_OUT_RATE); });
    el.panLeftBtn.addEventListener('click', function () { mainChart.pan(-PAN_RATIO); });
    el.panRightBtn.addEventListener('click', function () { mainChart.pan(PAN_RATIO); });
    el.resetViewBtn.addEventListener('click', function () { mainChart.resetView(false, true); });
    el.ratioA.addEventListener('change', applyRatio);
    el.ratioB.addEventListener('change', applyRatio);
  }

  function onDocumentClick(event) {
    if (el.gearPop.hidden) return;
    if (event.target.closest('#gearPop') || event.target.closest('#gearBtn')) return;
    closeGear();
  }

  // ---------------------------------------------------------------- 数据

  function open() {
    if (!state.ready) return;
    el.reportView.hidden = false;
    loadReports();
  }

  function loadReports() {
    return WB.getJSON('/api/reports')
      .then(function (data) {
        state.saved = normalizeList(data.saved, false);
        state.live = normalizeList(data.live, true);
        state.importDirs = data.import_dirs || [];
        state.reportDir = data.report_dir || state.reportDir;
        renderSourceFilter();
        renderAll();
        if (data.synced) WB.showHint('已自动收录 ' + data.synced + ' 份新报告', 'ok');
      })
      .catch(function (error) { WB.showHint('读取报告列表失败：' + error.message, 'error'); });
  }

  function allReports() {
    return state.live.concat(state.saved);
  }

  function visibleReports() {
    var keyword = state.keyword;
    var filter = state.filterSource;
    var dirFilter = filter.indexOf(DIR_PREFIX) === 0
      ? filter.slice(DIR_PREFIX.length).toLowerCase() : null;

    var list = allReports().filter(function (item) {
      if (filter === 'live' && !item.isLive) return false;
      if (filter === 'saved' && item.isLive) return false;
      if (dirFilter) {
        if (item.isLive) return false;
        if ((item.origin || '').toLowerCase() !== dirFilter) return false;
      }
      if (!keyword) return true;
      var haystack = ((item.title || '') + ' ' + (item.bvid || '') + ' ' + (item.name || '')).toLowerCase();
      return haystack.indexOf(keyword) >= 0;
    });

    // 未启用任何表头排序时，默认按报告生成时间降序
    var key = state.sort.key;
    var dir = state.sort.dir === 'asc' ? 1 : -1;
    if (!key) {
      key = 'generated_at';
      dir = -1;
    }

    list.sort(function (a, b) {
      if (key === 'title') {
        return (a.title || '').localeCompare(b.title || '', 'zh-CN') * dir;
      }
      if (key === 'pubdate') {
        return ((a.pubdate || 0) - (b.pubdate || 0)) * dir;
      }
      var at = a.generated_at || '';
      var bt = b.generated_at || '';
      if (at < bt) return -1 * dir;
      if (at > bt) return 1 * dir;
      return 0;
    });

    // 会话中未保存的报告始终排在前面
    list.sort(function (a, b) {
      if (a.isLive === b.isLive) return 0;
      return a.isLive ? -1 : 1;
    });

    return list;
  }

  /* 来源下拉：全部 / 未保存 / 仓库 + 各自动导入目录 */
  function renderSourceFilter() {
    var select = el.filterSource;
    var current = state.filterSource;
    var options = [
      { value: 'all', label: '全部来源' },
      { value: 'live', label: '未保存' },
      { value: 'saved', label: '仓库' }
    ];
    state.importDirs.forEach(function (item) {
      if (item.source === 'repo') return;
      options.push({ value: 'dir:' + item.path, label: '目录：' + (item.name || item.path) });
    });

    select.textContent = '';
    options.forEach(function (option) {
      var node = document.createElement('option');
      node.value = option.value;
      node.textContent = option.label;
      select.appendChild(node);
    });

    var stillThere = options.some(function (option) { return option.value === current; });
    if (!stillThere) current = 'all';
    select.value = current;
    state.filterSource = current;
  }

  /* 表头排序按钮：单个按钮循环三种状态——关闭 → 升序 → 降序 → 关闭 */
  function renderSortButtons() {
    Array.prototype.forEach.call(document.querySelectorAll('.sort-btn'), function (btn) {
      var active = state.sort.key === btn.dataset.key ? state.sort.dir : null;
      var isAsc = active === 'asc';
      var isDesc = active === 'desc';

      btn.classList.toggle('is-asc', isAsc);
      btn.classList.toggle('is-desc', isDesc);
      btn.textContent = isAsc ? '↑' : isDesc ? '↓' : '↑↓';
      btn.title = isAsc ? '升序，点击改为降序'
        : isDesc ? '降序，点击关闭排序'
        : '点击按此列升序';
      btn.setAttribute('aria-label', btn.title);
    });
  }

  function bindSortButtons() {
    Array.prototype.forEach.call(document.querySelectorAll('.sort-btn'), function (btn) {
      btn.addEventListener('click', function (event) {
        event.stopPropagation();
        var key = btn.dataset.key;
        var active = state.sort.key === key ? state.sort.dir : null;

        if (active === null) {
          state.sort = { key: key, dir: 'asc' };
        } else if (active === 'asc') {
          state.sort = { key: key, dir: 'desc' };
        } else {
          state.sort = { key: null, dir: null };   // 第三次点击关闭排序
        }

        state.page = 1;
        renderSortButtons();
        renderTable();
      });
    });
  }

  // ---------------------------------------------------------------- 渲染

  function renderAll() {
    renderSummary();
    renderTable();
    renderGearHint();
  }

  function renderSummary() {
    el.reportSummary.textContent = '已保存 ' + state.saved.length + ' 份报告 · '
      + state.live.length + ' 份未保存';
  }

  function renderGearHint() {
    el.gearDirHint.textContent = state.exportDir
      ? '导出目录：' + state.exportDir
      : '未设置，导出时走浏览器下载';
    el.gearDirHint.title = state.exportDir || '';
  }

  function renderTable() {
    var list = visibleReports();
    var totalPages = Math.max(1, Math.ceil(list.length / state.pageSize));
    if (state.page > totalPages) state.page = totalPages;

    var start = (state.page - 1) * state.pageSize;
    var pageItems = list.slice(start, start + state.pageSize);

    el.reportTbody.textContent = '';
    pageItems.forEach(function (item) {
      el.reportTbody.appendChild(makeRow(item));
    });

    el.pageInfo.textContent = list.length
      ? '共 ' + list.length + ' 份 · 第 ' + state.page + '/' + totalPages + ' 页'
      : '共 0 份';
    el.prevPageBtn.disabled = state.page <= 1;
    el.nextPageBtn.disabled = state.page >= totalPages;

    syncSelectAll(pageItems);
    updateExportButton();
  }

  function makeRow(item) {
    var row = document.createElement('tr');
    row.dataset.key = item.key;
    if (item.key === state.activeKey) row.classList.add('is-active');

    // 标题（未保存/已移除标签 + 悬浮出现的删除按钮）
    var titleCell = document.createElement('td');
    var titleWrap = document.createElement('div');
    titleWrap.className = 'report-title-cell';
    var titleText = document.createElement('span');
    titleText.className = 'report-title-text';
    titleText.textContent = item.title || item.bvid || '未命名报告';
    titleText.title = titleText.textContent;
    titleWrap.appendChild(titleText);
    if (item.isLive) {
      titleWrap.appendChild(makeBadge('未保存', 'badge-live'));
      if (item.archived) titleWrap.appendChild(makeBadge('已移除', 'badge-archived'));
    }
    if (!item.isLive) {
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'report-row-del';
      del.textContent = '×';
      del.title = '删除该报告';
      del.addEventListener('click', function (event) {
        event.stopPropagation();
        onDelete(item);
      });
      titleWrap.appendChild(del);
    }
    titleCell.appendChild(titleWrap);

    var ownerCell = document.createElement('td');
    ownerCell.className = 'col-owner';
    ownerCell.textContent = item.owner || '—';
    ownerCell.title = ownerCell.textContent;

    var bvidCell = document.createElement('td');
    bvidCell.className = 'col-bvid';
    bvidCell.textContent = item.bvid || '—';

    var pubCell = document.createElement('td');
    pubCell.className = 'col-time';
    pubCell.textContent = formatPublish(item.pubdate);

    var generatedCell = document.createElement('td');
    generatedCell.className = 'col-time';
    generatedCell.textContent = formatStamp(item.generated_at);

    var durationCell = document.createElement('td');
    durationCell.className = 'col-num';
    durationCell.textContent = formatDuration(item.duration_seconds);

    var checkCell = document.createElement('td');
    checkCell.className = 'col-check';
    var check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = state.selected.has(item.key);
    check.setAttribute('aria-label', '选中该报告');
    check.addEventListener('click', function (event) { event.stopPropagation(); });
    check.addEventListener('change', function () {
      if (check.checked) state.selected.add(item.key);
      else state.selected.delete(item.key);
      syncSelectAll(null);
      updateExportButton();
    });
    checkCell.appendChild(check);

    row.appendChild(titleCell);
    row.appendChild(ownerCell);
    row.appendChild(bvidCell);
    row.appendChild(pubCell);
    row.appendChild(generatedCell);
    row.appendChild(durationCell);
    row.appendChild(checkCell);

    row.addEventListener('click', function () { openDetail(item); });
    return row;
  }

  function makeBadge(text, className) {
    var badge = document.createElement('span');
    badge.className = 'badge ' + className;
    badge.textContent = text;
    return badge;
  }

  function syncSelectAll(pageItems) {
    var items = pageItems || currentPageItems();
    if (!items.length) {
      el.selectAll.checked = false;
      el.selectAll.indeterminate = false;
      return;
    }
    var picked = items.filter(function (item) { return state.selected.has(item.key); }).length;
    el.selectAll.checked = picked === items.length;
    el.selectAll.indeterminate = picked > 0 && picked < items.length;
  }

  function currentPageItems() {
    var list = visibleReports();
    var start = (state.page - 1) * state.pageSize;
    return list.slice(start, start + state.pageSize);
  }

  function updateExportButton() {
    el.exportSelectedBtn.disabled = state.selected.size === 0;
  }

  function onSelectAll() {
    var items = currentPageItems();
    var checked = el.selectAll.checked;
    items.forEach(function (item) {
      if (checked) state.selected.add(item.key);
      else state.selected.delete(item.key);
    });
    renderTable();
  }

  function goPage(page) {
    var list = visibleReports();
    var totalPages = Math.max(1, Math.ceil(list.length / state.pageSize));
    if (page < 1 || page > totalPages) return;
    state.page = page;
    renderTable();
    el.reportListView.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  // ---------------------------------------------------------------- 导入导出

  function onImport() {
    var initial = '';
    try { initial = localStorage.getItem('reportImportDir') || ''; } catch (error) { initial = ''; }

    window.FilePicker.open({
      mode: 'any',
      exts: ['json'],
      title: '导入报告（可多选文件或目录）',
      initialPath: initial,
      shortcuts: state.reportDir ? [{ label: '仓库目录', path: state.reportDir }] : []
    }).then(function (paths) {
      if (!paths || !paths.length) return;
      try {
        var first = paths[0];
        localStorage.setItem('reportImportDir', first.replace(/[\\/][^\\/]*$/, '') || first);
      } catch (error) { /* 隐私模式忽略 */ }

      el.importBtn.disabled = true;
      WB.postJSON('/api/reports/import', { paths: paths })
        .then(function (data) {
          var count = (data.imported || []).length;
          var skipped = data.skipped || [];
          if (count) {
            state.saved = (data.saved || []).map(function (item) {
              item.key = item.name;
              item.isLive = false;
              return item;
            });
            renderAll();
            WB.showHint('已导入 ' + count + ' 份报告', 'ok');
          }
          if (skipped.length) {
            WB.showHint(
              '跳过 ' + skipped.length + ' 份：' + skipped[0].name + '（' + skipped[0].reason + '）',
              'error'
            );
          }
          if (!count && !skipped.length) WB.showHint('所选位置没有找到 JSON 报告', 'info');
        })
        .catch(function (error) { WB.showHint(error.message, 'error'); })
        .finally(function () { el.importBtn.disabled = false; });
    });
  }

  function collectSelection() {
    var names = [];
    var liveIds = [];
    state.selected.forEach(function (key) {
      if (key.indexOf(LIVE_PREFIX) === 0) liveIds.push(key.slice(LIVE_PREFIX.length));
      else names.push(key);
    });
    return { names: names, liveIds: liveIds, count: names.length + liveIds.length };
  }

  function onExportSelected() {
    var picked = collectSelection();
    if (!picked.count) return;
    doExport(picked);
  }

  function onExportAll() {
    closeGear();
    var all = allReports();
    if (!all.length) {
      WB.showHint('还没有任何报告可导出', 'info');
      return;
    }

    var names = state.saved.map(function (item) { return item.name; });
    var liveIds = state.live.map(function (item) { return item.monitor_id; }).filter(Boolean);
    var total = names.length + liveIds.length;

    // 每次导出前先把上次的目录填进来；没有则先让用户选择
    ensureExportDir().then(function (dir) {
      if (dir === null) return;
      WB.showConfirm(
        '将全部 ' + total + ' 份报告导出到：' + (dir || '浏览器默认下载位置') + '。是否继续？'
      ).then(function (confirmed) {
        if (!confirmed) return;
        doExport({ names: names, liveIds: liveIds, count: total });
      });
    });
  }

  /* 返回导出目录；用户取消选择时返回 null */
  function ensureExportDir() {
    if (state.exportDir) return Promise.resolve(state.exportDir);
    return pickDirectory('选择导出目录').then(function (dir) {
      return dir || null;
    });
  }

  function pickDirectory(title) {
    return window.FilePicker.open({
      mode: 'dirs',
      multi: false,
      title: title,
      initialPath: state.exportDir || '',
      shortcuts: state.reportDir ? [{ label: '仓库目录', path: state.reportDir }] : []
    }).then(function (paths) {
      if (!paths || !paths.length) return null;
      state.exportDir = paths[0];
      try { localStorage.setItem('reportExportDir', state.exportDir); } catch (error) { /* 忽略 */ }
      renderGearHint();
      return state.exportDir;
    });
  }

  function doExport(picked) {
    WB.postJSON('/api/reports/export', {
      names: picked.names,
      live_ids: picked.liveIds,
      target_dir: state.exportDir
    })
      .then(function (data) {
        if (data.mode === 'browser') {
          triggerBrowserDownloads(data.links || []);
          WB.showHint('已开始下载 ' + data.count + ' 份报告', 'ok');
        } else {
          WB.showHint('已导出至 ' + data.target_dir, 'ok');
        }
        state.selected.clear();
        renderTable();
      })
      .catch(function (error) { WB.showHint(error.message, 'error'); });
  }

  /* 未设置导出目录时，逐个触发浏览器下载（间隔一点时间，避免被合并拦截） */
  function triggerBrowserDownloads(links) {
    links.forEach(function (link, index) {
      window.setTimeout(function () {
        var anchor = document.createElement('a');
        anchor.href = link.url;
        anchor.download = link.name || '';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      }, index * EXPORT_STAGGER_MS);
    });
  }

  /* 一键保存：把会话中尚未落盘的报告写入仓库目录 */
  function onSaveLive() {
    WB.postJSON('/api/reports/save-live', {})
      .then(function (data) {
        if (!data.saved_count) {
          WB.showHint('没有未保存的报告', 'info');
          return;
        }
        state.saved = normalizeList(data.saved, false);
        state.live = normalizeList(data.live, true);
        renderAll();
        WB.showHint('已保存 ' + data.saved_count + ' 份报告到仓库', 'ok');
      })
      .catch(function (error) { WB.showHint(error.message, 'error'); });
  }

  function normalizeList(items, isLive) {
    return (items || []).map(function (item) {
      item.key = item.name;
      item.isLive = isLive;
      return item;
    });
  }

  function onPickExportDir() {
    closeGear();
    pickDirectory('选择导出目录').then(function (dir) {
      if (dir) WB.showHint('导出目录已设为 ' + dir, 'ok');
    });
  }

  // ---------------------------------------------------------------- 自动导入目录

  function onManageImportDirs() {
    closeGear();
    renderImportDirs();
    el.importDirsOverlay.hidden = false;
    document.body.classList.add('no-scroll');
  }

  function closeImportDirs() {
    el.importDirsOverlay.hidden = true;
    if (!document.querySelector('.picker-overlay:not([hidden])')) {
      document.body.classList.remove('no-scroll');
    }
    loadReports();
  }

  function renderImportDirs() {
    el.importDirList.textContent = '';

    state.importDirs.forEach(function (item) {
      var row = document.createElement('li');
      row.className = 'import-dir-item';

      var tag = document.createElement('span');
      var source = item.source;
      tag.className = 'import-dir-tag'
        + (source === 'repo' ? ' is-repo' : source === 'auto_save' ? ' is-auto' : '');
      tag.textContent = source === 'repo' ? '仓库' : source === 'auto_save' ? '自动保存' : '自定义';

      var path = document.createElement('span');
      path.className = 'import-dir-path';
      path.textContent = item.path;
      path.title = item.path;

      row.appendChild(tag);
      row.appendChild(path);

      if (!item.fixed) {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'import-dir-del';
        del.textContent = '×';
        del.title = '移除该目录';
        del.addEventListener('click', function () { removeImportDir(item.path); });
        row.appendChild(del);
      }
      el.importDirList.appendChild(row);
    });
  }

  /* 只有用户自己添加的目录参与增删；仓库与自动保存目录是自动带上的 */
  function userImportDirs() {
    return state.importDirs
      .filter(function (item) { return item.source === 'user'; })
      .map(function (item) { return item.path; });
  }

  function saveImportDirs(dirs) {
    return WB.postJSON('/api/settings', { import_dirs: dirs })
      .then(function () { return loadReports(); })
      .then(function () {
        renderImportDirs();
        WB.showHint('目录设置已保存', 'ok');
      })
      .catch(function (error) { WB.showHint(error.message, 'error'); });
  }

  function onAddImportDir() {
    window.FilePicker.open({
      mode: 'dirs',
      multi: false,
      title: '选择自动导入目录',
      shortcuts: state.reportDir ? [{ label: '仓库目录', path: state.reportDir }] : []
    }).then(function (paths) {
      if (!paths || !paths.length) return;
      var chosen = paths[0];
      var existing = state.importDirs.map(function (item) { return item.path.toLowerCase(); });
      if (existing.indexOf(chosen.toLowerCase()) >= 0) {
        WB.showHint('该目录已在列表中', 'info');
        return;
      }
      saveImportDirs(userImportDirs().concat([chosen]));
    });
  }

  function removeImportDir(path) {
    saveImportDirs(userImportDirs().filter(function (item) { return item !== path; }));
  }

  function onDelete(item) {
    WB.showConfirm('删除报告「' + item.name + '」？文件会从 reports 目录移除。').then(function (confirmed) {
      if (!confirmed) return;
      WB.postJSON('/api/reports/delete', { name: item.name })
        .then(function (data) {
          state.saved = (data.saved || []).map(function (entry) {
            entry.key = entry.name;
            entry.isLive = false;
            return entry;
          });
          state.selected.delete(item.key);
          if (state.activeKey === item.key) backToList();
          renderAll();
          WB.showHint('已删除', 'info');
        })
        .catch(function (error) { WB.showHint(error.message, 'error'); });
    });
  }

  function onToggleGear(event) {
    event.stopPropagation();
    if (el.gearPop.hidden) {
      el.gearPop.hidden = false;
      el.gearBtn.classList.add('is-open');
      el.gearBtn.setAttribute('aria-expanded', 'true');
    } else {
      closeGear();
    }
  }

  function closeGear() {
    el.gearPop.hidden = true;
    el.gearBtn.classList.remove('is-open');
    el.gearBtn.setAttribute('aria-expanded', 'false');
  }

  // ---------------------------------------------------------------- 详情

  function openDetail(item) {
    state.activeKey = item.key;
    el.reportListView.hidden = true;
    el.reportDetailView.hidden = false;
    renderTable();

    WB.getJSON('/api/reports/detail?key=' + encodeURIComponent(item.key))
      .then(function (doc) {
        state.doc = doc;
        state.metrics = ['view'];
        renderDetail();
      })
      .catch(function (error) { WB.showHint('打开报告失败：' + error.message, 'error'); });
  }

  function backToList() {
    state.activeKey = null;
    state.doc = null;
    el.reportDetailView.hidden = true;
    el.reportListView.hidden = false;
    renderTable();
  }

  function renderDetail() {
    var doc = state.doc;
    if (!doc) return;
    var video = doc.video || {};
    var monitor = doc.monitor || {};

    el.reportCover.src = video.cover || '';
    el.reportCover.hidden = !video.cover;
    el.reportTitle.textContent = video.title || '未命名视频';
    el.reportSub.textContent = [
      video.bvid || '',
      monitor.id ? '监控 ' + monitor.id : '',
      '生成于 ' + formatStamp(doc.generated_at),
      (monitor.sample_count || (doc.samples || []).length) + ' 个采样点',
      formatDuration(monitor.duration_seconds),
      monitor.interval_seconds ? monitor.interval_seconds + ' 秒周期' : ''
    ].filter(Boolean).join(' · ');

    el.detailHint.textContent = state.activeKey || '';
    buildMetricTabs();
    renderMainChart();
    applyRatio();
  }

  function buildMetricTabs() {
    el.reportMetricTabs.textContent = '';
    METRICS.forEach(function (metric) {
      var button = document.createElement('button');
      button.type = 'button';
      button.dataset.key = metric.key;
      button.className = 'metric-tab' + (state.metrics.indexOf(metric.key) >= 0 ? ' is-active' : '');
      button.textContent = metric.label;
      button.title = '点击叠加或取消该指标';
      button.addEventListener('click', function () { toggleMetric(metric.key); });
      el.reportMetricTabs.appendChild(button);
    });
  }

  /* 点选指标：最多叠加 3 条，至少保留 1 条 */
  function toggleMetric(key) {
    var index = state.metrics.indexOf(key);
    if (index >= 0) {
      if (state.metrics.length === 1) return;
      state.metrics.splice(index, 1);
    } else {
      if (state.metrics.length >= 3) state.metrics.shift();
      state.metrics.push(key);
    }
    Array.prototype.forEach.call(el.reportMetricTabs.children, function (node) {
      node.classList.toggle('is-active', state.metrics.indexOf(node.dataset.key) >= 0);
    });
    renderMainChart();
  }

  function buildRatioSelects() {
    [el.ratioA, el.ratioB].forEach(function (select, index) {
      select.textContent = '';
      METRICS.forEach(function (metric) {
        var option = document.createElement('option');
        option.value = metric.key;
        option.textContent = metric.label;
        select.appendChild(option);
      });
      select.value = index === 0 ? 'like' : 'view';
    });
  }

  function sampleTimestamp(sample) {
    if (typeof sample.t === 'number') return sample.t;
    var parsed = Date.parse(sample.time || '');
    return isFinite(parsed) ? parsed : null;
  }

  function metricPoints(key) {
    var samples = (state.doc && state.doc.samples) || [];
    var points = [];
    for (var i = 0; i < samples.length; i += 1) {
      var value = samples[i][key];
      var timestamp = sampleTimestamp(samples[i]);
      if (timestamp === null) continue;
      if (typeof value === 'number' && isFinite(value)) {
        points.push({ t: timestamp, v: value });
      }
    }
    return points;
  }

  function renderMainChart() {
    var selected = METRICS.filter(function (item) {
      return state.metrics.indexOf(item.key) >= 0;
    });

    var series = selected.map(function (item) {
      return { name: item.label, color: item.color, points: metricPoints(item.key) };
    }).filter(function (item) { return item.points.length > 0; });

    if (!series.length) {
      mainChart.setSeries([], { emptyText: '该指标没有可用数据' });
      el.chartTitle.textContent = '暂无数据';
      renderLegend([]);
      return;
    }

    mainChart.setSeries(series, { emptyText: '暂无数据' });
    el.chartTitle.textContent = series.length === 1 ? series[0].name : '指标趋势对比';

    renderLegend(series);
    renderViewHint();
    syncRatioView();
  }

  /* 图例即纵轴选择器：点哪条线，左侧刻度就切换成它的量程 */
  function renderLegend(series) {
    el.chartLegend.textContent = '';
    if (series.length < 2) return;

    series.forEach(function (item, index) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'legend-item' + (index === mainChart.activeIndex ? ' is-active' : '');

      var dot = document.createElement('span');
      dot.className = 'legend-dot';
      dot.style.background = item.color;

      var text = document.createElement('span');
      text.textContent = item.name;

      button.appendChild(dot);
      button.appendChild(text);
      button.addEventListener('click', function () {
        // 再点一次取消聚焦，纵轴随之隐藏
        var next = mainChart.activeIndex === index ? -1 : index;
        mainChart.setActiveSeries(next);
        Array.prototype.forEach.call(el.chartLegend.children, function (node, i) {
          node.classList.toggle('is-active', i === next);
        });
      });

      el.chartLegend.appendChild(button);
    });
  }


  function renderViewHint() {
    var full = mainChart.getFullRange();
    if (!full) { el.viewHint.textContent = '滚轮缩放 · 按住拖动平移 · 双击复位'; return; }
    var view = mainChart.view || full;
    el.viewHint.textContent = '显示 ' + chartFormatTime(view.min, 0, true)
      + ' ~ ' + chartFormatTime(view.max, 0, true)
      + (mainChart.view ? '（已缩放，双击复位）' : '（全量）');
  }

  function applyRatio() {
    var a = METRICS.filter(function (m) { return m.key === el.ratioA.value; })[0];
    var b = METRICS.filter(function (m) { return m.key === el.ratioB.value; })[0];
    if (!a || !b) return;

    var samples = (state.doc && state.doc.samples) || [];
    var points = [];
    var values = [];
    var skipped = 0;

    samples.forEach(function (sample) {
      var va = sample[a.key];
      var vb = sample[b.key];
      var timestamp = sampleTimestamp(sample);
      if (timestamp === null || typeof va !== 'number' || typeof vb !== 'number' || !vb) {
        skipped += 1;
        return;
      }
      var ratio = va / vb;
      if (!isFinite(ratio)) { skipped += 1; return; }
      points.push({ t: timestamp, v: ratio * 100 });
      values.push(ratio);
    });

    if (!points.length) {
      el.ratioStats.textContent = '没有可计算的点（分母为 0 或数据缺失）';
      ratioChart.setData([], '#0ea5e9', '没有可计算的点');
      return;
    }

    var sum = 0;
    var max = -Infinity;
    var min = Infinity;
    values.forEach(function (value) {
      sum += value;
      if (value > max) max = value;
      if (value < min) min = value;
    });

    el.ratioStats.textContent = a.label + ' ÷ ' + b.label + '：'
      + points.length + ' 个点'
      + (skipped ? '（跳过 ' + skipped + ' 个无有效值的点）' : '')
      + ' · 均值 ' + formatRatio(sum / values.length)
      + ' · 最大 ' + formatRatio(max)
      + ' · 最小 ' + formatRatio(min);

    ratioChart.setData(points, '#0ea5e9', '没有可计算的点', { suffix: '%' });
    syncRatioView();
  }

  function syncRatioView() {
    if (!ratioChart || !ratioChart.points.length) return;
    if (mainChart.view) ratioChart.setView(mainChart.view.min, mainChart.view.max, true);
    else ratioChart.resetView(true);
  }

  /* 百分比按 5 位有效数字输出并去掉末尾 0：
     固定 2 位小数会把 18.742% 与 18.751% 都写成 18.75%，均值/最大/最小看起来一模一样 */
  function formatRatio(value) {
    if (!isFinite(value)) return '—';
    if (value === 0) return '0%';

    var text = (value * 100).toPrecision(5);
    if (text.indexOf('.') >= 0) text = text.replace(/0+$/, '').replace(/\.$/, '');
    return text + '%';
  }

  // ---------------------------------------------------------------- 工具

  function formatStamp(value) {
    if (!value) return '—';
    return String(value).replace('T', ' ').slice(0, ISO_SECONDS);
  }

  /* 视频发布时间是秒级时间戳，外部导入的报告可能缺失该字段 */
  function formatPublish(value) {
    if (!value) return '—';
    var timestamp = typeof value === 'number' ? value * 1000 : Date.parse(value);
    if (!isFinite(timestamp)) return '—';
    var date = new Date(timestamp);
    var pad = function (n) { return String(n).padStart(2, '0'); };
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
      + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function formatDuration(seconds) {
    var total = Math.round(Number(seconds) || 0);
    if (!total) return '0 秒';
    if (total < 60) return total + ' 秒';
    var minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + ' 分钟';
    var hours = Math.floor(minutes / 60);
    return hours + ' 小时 ' + (minutes % 60) + ' 分';
  }

  window.ReportPage = { open: open, refresh: loadReports };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
