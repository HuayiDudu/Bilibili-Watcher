/* 统一的文件/目录选择器。
 *
 * 所有需要“选文件或选目录”的功能都走这里，通过参数指定可选项：
 *   FilePicker.open({
 *     mode: 'any' | 'dirs' | 'files',  // 可选目标类型
 *     exts: ['json'],                  // 文件扩展名过滤，空数组表示不限
 *     title: '导入报告',
 *     initialPath: 'D:\\xxx',           // 初始目录，留空则从「此电脑」开始
 *     multi: true                       // 是否允许多选
 *   }) -> Promise<string[] | null>      // 取消返回 null
 *
 * 数据来自 GET /api/fs，同一目录下可同时勾选多个文件或目录。
 */
(function () {
  'use strict';

  var state = {
    mode: 'any',
    exts: ['json'],
    multi: true,
    path: '',
    parent: '',
    isRoot: true,
    selected: new Map(),
    resolver: null,
    busy: false
  };

  var el = {};
  var ready = false;

  function init() {
    [
      'pickerOverlay', 'pickerTitle', 'pickerCloseBtn', 'pickerCancelBtn',
      'pickerUpBtn', 'pickerRootBtn', 'pickerPathInput', 'pickerList',
      'pickerShortcuts',
      'pickerEmptyTip', 'pickerCount', 'pickerConfirmBtn'
    ].forEach(function (id) { el[id] = document.getElementById(id); });

    if (!el.pickerOverlay) return;

    el.pickerCloseBtn.addEventListener('click', function () { close(null); });
    el.pickerCancelBtn.addEventListener('click', function () { close(null); });
    el.pickerConfirmBtn.addEventListener('click', confirmSelection);

    el.pickerUpBtn.addEventListener('click', function () {
      if (state.isRoot) return;
      navigate(state.parent || '');
    });
    el.pickerRootBtn.addEventListener('click', function () { navigate(''); });

    el.pickerPathInput.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      navigate(el.pickerPathInput.value.trim());
    });
    el.pickerPathInput.addEventListener('focus', function () {
      el.pickerPathInput.select();
    });

    el.pickerOverlay.addEventListener('click', function (event) {
      if (event.target === el.pickerOverlay) close(null);
    });

    // 遮罩上的滚轮不再传给背后页面，列表区自己的滚动保留
    el.pickerOverlay.addEventListener('wheel', function (event) {
      if (!event.target.closest('.picker-list-wrap')) event.preventDefault();
    }, { passive: false });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !el.pickerOverlay.hidden) close(null);
    });

    ready = true;
  }

  function open(options) {
    if (!ready) init();
    var config = options || {};
    state.mode = config.mode === 'dirs' || config.mode === 'files' ? config.mode : 'any';
    state.exts = Array.isArray(config.exts) ? config.exts : ['json'];
    state.multi = config.multi !== false;
    state.selected.clear();

    el.pickerTitle.textContent = config.title || '选择文件或目录';
    renderShortcuts(config.shortcuts || []);
    el.pickerOverlay.hidden = false;
    document.body.classList.add('no-scroll');
    updateCount();

    return new Promise(function (resolve) {
      state.resolver = resolve;
      navigate(config.initialPath || '');
    });
  }

  function close(paths) {
    el.pickerOverlay.hidden = true;
    // 若还有其它遮罩（例如目录管理弹窗）开着，页面滚动继续保持锁定
    var stillOpen = document.querySelector('.modal-overlay:not([hidden])');
    if (!stillOpen) document.body.classList.remove('no-scroll');
    var resolve = state.resolver;
    state.resolver = null;
    if (resolve) resolve(paths);
  }

  function navigate(path) {
    if (state.busy) return;
    state.busy = true;
    el.pickerConfirmBtn.disabled = true;

    var query = '/api/fs?path=' + encodeURIComponent(path || '')
      + '&mode=' + encodeURIComponent(state.mode)
      + '&ext=' + encodeURIComponent(state.exts.join(','));

    window.WB.getJSON(query)
      .then(render)
      .catch(function (error) {
        window.WB.showHint(error.message, 'error');
        el.pickerPathInput.value = state.path;
      })
      .finally(function () {
        state.busy = false;
        updateCount();
      });
  }

  /* 调用方提供的常用位置，点击直接跳转（例如仓库目录） */
  function renderShortcuts(shortcuts) {
    var wrap = el.pickerShortcuts;
    if (!wrap) return;
    wrap.textContent = '';
    if (!shortcuts.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    shortcuts.forEach(function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'shortcut-btn';
      button.textContent = item.label;
      button.title = item.path;
      button.addEventListener('click', function () { navigate(item.path); });
      wrap.appendChild(button);
    });
  }

  function render(data) {
    state.path = data.path || '';
    state.parent = data.parent || '';
    state.isRoot = !!data.isRoot;

    el.pickerPathInput.value = state.path;
    el.pickerPathInput.placeholder = state.isRoot ? '此电脑' : '输入路径后回车跳转';
    el.pickerPathInput.title = state.isRoot ? '此电脑' : state.path;

    var rows = [];
    // 只选目录时，允许直接选中"当前所在的目录"
    if (state.mode === 'dirs' && !state.isRoot) {
      rows.push(makeRow({ name: '使用当前文件夹：' + state.path, path: state.path }, true, true));
    }
    (data.dirs || []).forEach(function (dir) { rows.push(makeRow(dir, true, false)); });
    (data.files || []).forEach(function (file) { rows.push(makeRow(file, false, false)); });

    el.pickerList.textContent = '';
    rows.forEach(function (row) { el.pickerList.appendChild(row); });
    el.pickerEmptyTip.hidden = rows.length > 0;
    el.pickerEmptyTip.textContent = state.isRoot
      ? '没有检测到可访问的磁盘'
      : '这个目录下没有子目录或目标文件';
  }

  function makeRow(item, isDir, currentDirRow) {
    var row = document.createElement('li');
    row.className = 'picker-row';

    var check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = state.selected.has(item.path);
    check.addEventListener('change', function () {
      if (check.checked) {
        if (!state.multi) {
          state.selected.clear();
          syncCheckboxes();
        }
        state.selected.set(item.path, item.name);
      } else {
        state.selected.delete(item.path);
      }
      updateCount();
    });

    // 盘符行只用于进入；只允许选文件时，目录行仅用于导航
    var selectable = true;
    if (state.isRoot || item.drive) selectable = false;
    else if (isDir && !currentDirRow && state.mode === 'files') selectable = false;

    if (!selectable) {
      check.disabled = true;
      check.title = state.isRoot ? '请进入磁盘后再选择' : '当前需要选择文件';
    }
    // 盘符本身不可选，干脆不显示勾选框，保留缩进即可
    if (state.isRoot || item.drive) check.style.visibility = 'hidden';

    var icon = document.createElement('span');
    icon.className = 'picker-icon';
    icon.textContent = isDir ? '📁' : '📄';

    var name = document.createElement('span');
    name.className = 'picker-name' + (isDir && !currentDirRow ? ' is-dir' : '');
    name.textContent = item.name;
    name.title = item.path || state.path;
    if (isDir && !currentDirRow) {
      name.addEventListener('click', function () { navigate(item.path); });
    }

    var size = document.createElement('span');
    size.className = 'picker-size';
    if (!isDir) size.textContent = formatSize(item.size);

    row.appendChild(check);
    row.appendChild(icon);
    row.appendChild(name);
    row.appendChild(size);
    return row;
  }

  /* 单选模式下取消其他勾选，需要把 DOM 状态同步回来 */
  function syncCheckboxes() {
    Array.prototype.forEach.call(el.pickerList.querySelectorAll('.picker-row'), function (row) {
      var nameNode = row.querySelector('.picker-name');
      var check = row.querySelector('input[type="checkbox"]');
      if (!nameNode || !check || check.disabled) return;
      check.checked = state.selected.has(nameNode.title);
    });
  }

  function confirmSelection() {
    var paths = Array.from(state.selected.keys());
    if (!paths.length) return;
    close(paths);
  }

  function updateCount() {
    var count = state.selected.size;
    el.pickerCount.textContent = count ? '已选 ' + count + ' 项' : '未选择';
    el.pickerConfirmBtn.disabled = count === 0;
  }

  function formatSize(bytes) {
    var size = Number(bytes) || 0;
    if (size < 1024) return size + ' B';
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
    return (size / 1024 / 1024).toFixed(1) + ' MB';
  }

  window.FilePicker = { open: open, init: init };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
