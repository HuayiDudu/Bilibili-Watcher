/* 轻量折线图：自绘 Canvas，无第三方依赖。
   职责单一：把 [{t, v}] 画成带坐标轴、面积填充与悬停提示的折线。 */
(function (global) {
  'use strict';

  var PAD = { top: 18, right: 20, bottom: 28, left: 64 };
  var FONT = '13px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
  /* 缩放步长：放大用 1/ZOOM_STEP，缩小用 ZOOM_STEP */
  var ZOOM_STEP = 1.25;
  var ANIM_MS = 160;
  /* Y 轴上下留白比例；数据占比低于 FIT_RATIO 时换更细刻度 */
  var Y_MARGIN_RATIO = 0.15;
  var FIT_RATIO = 0.55;
  /* 判断视图是否已回到全量的容差 */
  var FULL_VIEW_EPSILON = 0.999;

  function niceScale(min, max, tickCount) {
    if (min === max) {
      // 值不变时仍要给纵向留白，但比例要小，否则曲线会被压成一条贴边的直线；
      // 比值这类小数指标不能按整数留白，否则范围会被撑到 0~1
      var scale = Math.abs(min);
      var pad = scale >= 1 ? Math.max(1, scale * 0.002) : scale > 0 ? scale * 0.02 : 0.5;
      min -= pad;
      max += pad;
    }
    var rawStep = (max - min) / Math.max(1, tickCount);
    var magnitude = Math.pow(10, Math.floor(Math.log(rawStep) / Math.LN10));
    var normalized = rawStep / magnitude;
    var step;
    if (normalized <= 1) step = 1;
    else if (normalized <= 2) step = 2;
    else if (normalized <= 2.5) step = 2.5;
    else if (normalized <= 5) step = 5;
    else step = 10;
    step *= magnitude;

    // 计数类指标是整数，步长小于 1 会让刻度取整后重复；
    // 但跨度本身不足 1 的是小数指标（如比值），必须保留细步长
    if (step < 1 && max - min >= 1) step = 1;

    var niceMin = Math.floor(min / step) * step;
    var niceMax = Math.ceil(max / step) * step;

    // 只抹掉浮点累加的误差，不能统一舍入到固定位数：
    // 比值这类窄区间相邻刻度只差 1e-7，按 6 位舍入会被压成同一个值、叠在一起看不见
    var precision = Math.min(15, decimalsFor(step) + 2);
    var ticks = [];
    for (var v = niceMin; v <= niceMax + step * 0.5; v += step) {
      ticks.push(Number(v.toFixed(precision)));
    }
    return { min: niceMin, max: niceMax, ticks: ticks, step: step };
  }

  /* 先按留白算范围，若取整后数据只占一半高度，就用更细的刻度再算一次 */
  function fitScale(low, high, dataLow, dataHigh) {
    var scale = niceScale(low, high, 4);
    var total = scale.max - scale.min;
    if (total > 0 && (dataHigh - dataLow) / total < FIT_RATIO) {
      var finer = niceScale(low, high, 8);
      if (finer.max - finer.min < total) scale = finer;
    }
    return scale;
  }

  /* 刻度步长需要的小数位数：0.005 要 3 位、2.5 要 1 位、5 要 0 位。
     先按有效数字归一化，消掉 0.30000000000004 这类浮点噪声。 */
  function decimalsFor(step) {
    if (!isFinite(step) || step <= 0) return 0;

    var text = String(Number(step.toPrecision(6)));
    var exponent = 0;
    var hit = text.indexOf('e');
    if (hit >= 0) {
      exponent = parseInt(text.slice(hit + 1), 10) || 0;
      text = text.slice(0, hit);
    }

    var dot = text.indexOf('.');
    var frac = dot >= 0 ? text.length - dot - 1 : 0;
    return Math.max(0, Math.min(8, frac - exponent));
  }

  /* 刻度标签的小数位由步长决定，与数值大小无关：
     比值按百分比显示后落在 18.7 附近，若按"≥1 就取整"会把所有刻度画成同一个 19。 */
  function formatTick(value, step) {
    var digits = decimalsFor(step);
    var text = value.toFixed(digits);
    return digits > 0
      ? Number(text).toLocaleString('zh-CN', {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits
        })
      : Number(text).toLocaleString('zh-CN');
  }

  function trimZeros(text) {
    return text.replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');
  }

  function formatNumber(value, precise) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    var abs = Math.abs(value);
    if (precise || abs < 10000) {
      // 比值这类小数不能用取整，否则全部显示为 0
      if (abs > 0 && abs < 1) return Number(value.toPrecision(4)).toString();
      return Math.round(value).toLocaleString('zh-CN');
    }
    if (abs >= 1e8) return trimZeros((value / 1e8).toFixed(2)) + '亿';
    return trimZeros((value / 1e4).toFixed(abs >= 1e6 ? 1 : 2)) + '万';
  }

  function formatTime(ts, span, withSeconds) {
    var d = new Date(ts);
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (span > 20 * 3600 * 1000) {
      return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
    }
    if (withSeconds || span < 5 * 60 * 1000) return hm + ':' + pad(d.getSeconds());
    return hm;
  }

  function rgba(hex, alpha) {
    var value = hex.replace('#', '');
    if (value.length === 3) {
      value = value[0] + value[0] + value[1] + value[1] + value[2] + value[2];
    }
    var num = parseInt(value, 16);
    return 'rgba(' + ((num >> 16) & 255) + ',' + ((num >> 8) & 255) + ',' + (num & 255) + ',' + alpha + ')';
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /* 缩放后允许的最小时间跨度（毫秒），避免退化成无意义的空白 */
  var MIN_VIEW_SPAN = 1000;

  function LineChart(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.points = [];
    this.series = [];
    this.valueSuffix = '';
    this.activeIndex = -1;
    this.color = '#2f6feb';
    this.hoverIndex = null;
    this.emptyText = '暂无数据';
    this._geometry = null;
    // view 为 null 表示展示全量时间范围
    this.view = null;
    this.interactive = false;
    this.onViewChange = null;
    this._drag = null;
    this._bindEvents();
    this._observeResize();
  }

  LineChart.prototype.setData = function (points, color, emptyText, options) {
    this.setSeries(
      [{ name: '', color: color || this.color, points: points || [] }],
      { emptyText: emptyText, suffix: options && options.suffix }
    );
  };

  /* 多条折线叠加：series 为 [{name, color, points}]，共用一条真实数值的纵轴。 */
  LineChart.prototype.setSeries = function (series, options) {
    var list = (series || []).filter(function (item) {
      return item && item.points && item.points.length;
    });
    if (options && options.emptyText) this.emptyText = options.emptyText;
    this.valueSuffix = (options && options.suffix) || '';
    this.series = list;
    this.points = list.length ? list[0].points : [];
    // 单条线本身就是解释对象；多条线默认不聚焦，因而没有纵轴
    this.activeIndex = list.length > 1 ? -1 : 0;
    if (list.length && list[0].color) this.color = list[0].color;
    this.hoverIndex = null;
    this.view = null;
    this.render();
  };

  /* 高频重绘（拖拽、缩放、悬停）合并到下一帧，避免一次 mousemove 画一次 */
  LineChart.prototype.requestRender = function () {
    var self = this;
    if (typeof global.requestAnimationFrame !== 'function') {
      this.render();
      return;
    }
    if (this._rafPending) return;
    this._rafPending = true;
    global.requestAnimationFrame(function () {
      self._rafPending = false;
      self.render();
    });
  };

  LineChart.prototype.setInteractive = function (enabled) {
    this.interactive = !!enabled;
    this.canvas.style.cursor = this.interactive ? 'grab' : 'default';
  };

  LineChart.prototype.getFullRange = function () {
    if (!this.points.length) return null;
    return { min: this.points[0].t, max: this.points[this.points.length - 1].t };
  };

  /* 设定可视窗口；跨度不小于 1 秒，且始终被限制在完整范围内。
     animate 为 true 时用 easeOutCubic 过渡，缩放/按钮操作不会瞬间跳变。 */
  LineChart.prototype.setView = function (min, max, silent, animate) {
    var full = this.getFullRange();
    if (!full) return;

    var total = Math.max(1, full.max - full.min);
    var span = Math.max(MIN_VIEW_SPAN, Math.min(total, max - min));
    var target = null;
    if (span < total) {
      var start = Math.max(full.min, Math.min(min, full.max - span));
      target = { min: start, max: start + span };
    }

    if (animate && typeof global.requestAnimationFrame === 'function') {
      this._animateTo(target || { min: full.min, max: full.max });
    } else {
      this.view = target;
      this.requestRender();
    }

    if (!silent && this.onViewChange) this.onViewChange(this.view, full);
  };

  /* 视图过渡：从当前窗口缓动到目标窗口，拖动时不调用以免脱离手感 */
  LineChart.prototype._animateTo = function (target) {
    var self = this;
    var from = this.view || this.getFullRange();
    if (!from) return;

    var token = (this._animToken || 0) + 1;
    this._animToken = token;
    var began = (global.performance && global.performance.now) ? global.performance.now() : Date.now();
    var duration = ANIM_MS;

    var step = function () {
      if (token !== self._animToken) return;   // 已被新的操作打断
      var now = (global.performance && global.performance.now) ? global.performance.now() : Date.now();
      var ratio = Math.min(1, (now - began) / duration);
      var eased = 1 - Math.pow(1 - ratio, 3);

      if (ratio < 1) {
        self.view = {
          min: from.min + (target.min - from.min) * eased,
          max: from.max + (target.max - from.max) * eased
        };
        self.requestRender();
        global.requestAnimationFrame(step);
        return;
      }

      // 停在完整范围时把 view 清空：全量是"没有视窗"的状态，
      // 提示文案与滚轮是否需要拦截都靠这个约定判断
      var full = self.getFullRange();
      var total = full ? full.max - full.min : 0;
      var atFull = !full || total <= 0 || (target.max - target.min) >= total * FULL_VIEW_EPSILON;
      self.view = atFull ? null : { min: target.min, max: target.max };
      self.requestRender();
      if (self.onViewChange) self.onViewChange(self.view, full);
    };

    global.requestAnimationFrame(step);
  };

  LineChart.prototype.resetView = function (silent, animate) {
    if (animate && typeof global.requestAnimationFrame === 'function') {
      var full = this.getFullRange();
      if (full) this._animateTo({ min: full.min, max: full.max });
      else { this.view = null; this.requestRender(); }
    } else {
      this.view = null;
      this.requestRender();
    }
    if (!silent && this.onViewChange) this.onViewChange(null, this.getFullRange());
  };

  /* 按钮用的公开入口：以绘图区中心为锚点缩放、按窗口宽度比例平移 */
  LineChart.prototype.zoom = function (factor) {
    var rect = this.canvas.getBoundingClientRect();
    this._zoomAt(factor, rect.left + rect.width / 2, true);
  };

  LineChart.prototype.pan = function (ratio) {
    this._panByRatio(ratio, true);
  };

  LineChart.prototype._currentView = function () {
    var full = this.getFullRange();
    if (!full) return null;
    return this.view || { min: full.min, max: full.max };
  };

  /* 当前窗口是否等于完整范围（含动画停在端点、view 为空两种情形） */
  LineChart.prototype._isFullView = function () {
    var full = this.getFullRange();
    if (!full) return true;
    var view = this.view;
    if (!view) return true;
    var total = full.max - full.min;
    if (total <= 0) return true;
    return view.max - view.min >= total * FULL_VIEW_EPSILON;
  };

  LineChart.prototype._zoomAt = function (factor, clientX, animate) {
    var view = this._currentView();
    if (!view) return;

    var span = view.max - view.min;
    var ratio = 0.5;
    if (this._geometry) {
      var rect = this.canvas.getBoundingClientRect();
      var offset = clientX - rect.left - this._geometry.left;
      ratio = Math.max(0, Math.min(1, offset / this._geometry.width));
    }
    var anchor = view.min + ratio * span;
    var nextSpan = span * factor;
    this.setView(anchor - ratio * nextSpan, anchor + (1 - ratio) * nextSpan, false, !!animate);
  };

  LineChart.prototype._panByRatio = function (ratio, animate) {
    var view = this._currentView();
    if (!view) return;
    var shift = (view.max - view.min) * ratio;
    this.setView(view.min + shift, view.max + shift, false, !!animate);
  };

  LineChart.prototype._observeResize = function () {
    var self = this;
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(function () { self.render(); }).observe(this.canvas);
    } else {
      global.addEventListener('resize', function () { self.render(); });
    }
  };

  /* 鼠标所在位置对应的可见点下标，按时间反查而非按比例换算 */
  LineChart.prototype._indexAt = function (offsetX) {
    var geometry = this._geometry;
    if (!geometry || !geometry.visible.length) return null;

    var ratio = (offsetX - geometry.left) / geometry.width;
    ratio = Math.max(0, Math.min(1, ratio));
    var targetT = geometry.tStart + ratio * geometry.tSpan;
    var points = geometry.visible;

    var low = 0;
    var high = points.length - 1;
    while (low < high) {
      var mid = (low + high) >> 1;
      if (points[mid].t < targetT) low = mid + 1;
      else high = mid;
    }
    if (low > 0 && Math.abs(points[low - 1].t - targetT) < Math.abs(points[low].t - targetT)) {
      low -= 1;
    }
    return low;
  };

  LineChart.prototype._bindEvents = function () {
    var self = this;

    this.canvas.addEventListener('mousemove', function (event) {
      if (self._drag) return;
      var geometry = self._geometry;
      if (!geometry || !self.points.length) return;

      var rect = self.canvas.getBoundingClientRect();
      var x = event.clientX - rect.left;
      if (x < geometry.left || x > geometry.left + geometry.width) {
        if (self.hoverIndex !== null) { self.hoverIndex = null; self.requestRender(); }
        return;
      }
      var index = self._indexAt(x);
      if (index !== null && index !== self.hoverIndex) {
        self.hoverIndex = index;
        self.requestRender();
      }
    });

    this.canvas.addEventListener('mouseleave', function () {
      if (self.hoverIndex !== null) { self.hoverIndex = null; self.requestRender(); }
    });

    /* 滚轮缩放不会吞掉整页滚动：窗口已经全量时，向下滚（继续缩小）没有意义，
       把事件交还页面；向上滚仍可放大。按住 Ctrl/⌘ 时始终缩放。 */
    this.canvas.addEventListener('wheel', function (event) {
      if (!self.interactive || !self.points.length) return;

      var zoomIn = event.deltaY < 0;
      var forced = event.ctrlKey || event.metaKey;
      if (!zoomIn && !forced && self._isFullView()) return;

      event.preventDefault();
      self._zoomAt(zoomIn ? 1 / ZOOM_STEP : ZOOM_STEP, event.clientX, true);
    }, { passive: false });

    this.canvas.addEventListener('mousedown', function (event) {
      if (!self.interactive || !self.points.length || event.button !== 0) return;
      var view = self._currentView();
      if (!view) return;
      self._drag = { startX: event.clientX, origin: view };
      self.canvas.style.cursor = 'grabbing';
      event.preventDefault();
    });

    global.addEventListener('mousemove', function (event) {
      if (!self._drag || !self._geometry) return;
      var dx = event.clientX - self._drag.startX;
      var span = self._drag.origin.max - self._drag.origin.min;
      var shift = -(dx / self._geometry.width) * span;
      self.setView(self._drag.origin.min + shift, self._drag.origin.max + shift);
    });

    global.addEventListener('mouseup', function () {
      if (!self._drag) return;
      self._drag = null;
      self.canvas.style.cursor = self.interactive ? 'grab' : 'default';
    });

    this.canvas.addEventListener('dblclick', function () {
      if (!self.interactive) return;
      self.resetView(false, true);
    });
  };

  /* 取窗口内的点，向两侧各多带一个，保证线段在窗口边缘连续 */
  function sliceVisible(points, tStart, tEnd) {
    var startIdx = -1;
    var endIdx = -1;
    for (var i = 0; i < points.length; i += 1) {
      if (startIdx < 0 && points[i].t >= tStart) startIdx = i;
      if (points[i].t <= tEnd) endIdx = i;
    }
    if (startIdx < 0) startIdx = points.length;
    return points.slice(
      Math.max(0, startIdx - 1),
      Math.min(points.length - 1, endIdx + 1) + 1
    );
  }

  LineChart.prototype.render = function () {
    var canvas = this.canvas;
    var ctx = this.ctx;
    var rect = canvas.getBoundingClientRect();
    var width = Math.max(1, Math.floor(rect.width));
    var height = Math.max(1, Math.floor(rect.height));
    var dpr = global.devicePixelRatio || 1;

    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    var plotTop = PAD.top;
    var plotHeight = height - PAD.top - PAD.bottom;
    if (plotHeight < 24) return;

    var points = this.points;
    if (!points.length) {
      this._geometry = null;
      ctx.fillStyle = '#a4adbd';
      ctx.font = FONT;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(this.emptyText, width / 2, height / 2);
      return;
    }

    // 视图窗口：view 为空表示展示全量
    var fullMin = points[0].t;
    var fullMax = points[points.length - 1].t;
    var view = this.view || { min: fullMin, max: fullMax };
    var tStart = view.min;
    var tEnd = view.max;
    if (tEnd <= tStart) {
      tStart = fullMin;
      tEnd = fullMax;
    }
    var tSpan = Math.max(1, tEnd - tStart);

    // 绘制时向两侧各多取一个点，保证线段在窗口边缘连续
    var visible = sliceVisible(points, tStart, tEnd);

    // 每条序列各算一套刻度：叠加时各用各的纵轴，量级互不影响
    var seriesList = this.series.length ? this.series : [{ points: points }];
    var scales = seriesList.map(function (item) {
      var low = Infinity;
      var high = -Infinity;
      item.points.forEach(function (point) {
        if (point.v < low) low = point.v;
        if (point.v > high) high = point.v;
      });
      // 上下各留 15%：非 0 的数据不会贴底，也不会顶到上沿
      var dataLow = low;
      var dataHigh = high;
      if (high > low) {
        var margin = (high - low) * Y_MARGIN_RATIO;
        low = (low >= 0 && low - margin < 0) ? 0 : low - margin;
        high = high + margin;
      }
      return fitScale(low, high, dataLow, dataHigh);
    });

    // 多条线未聚焦时不画纵轴：各线按自己的量程撑满，只比形状
    var activeIndex = Math.min(Math.max(-1, this.activeIndex), scales.length - 1);
    if (this.series.length <= 1) activeIndex = 0;
    this.activeIndex = activeIndex;

    var axisScale = scales[activeIndex >= 0 ? activeIndex : 0];
    var showAxis = activeIndex >= 0;

    // 悬停读数只针对当前被解释的那条线：单指标就是它本身，聚焦时是聚焦的那条
    var hoverSeries = seriesList[activeIndex >= 0 ? activeIndex : 0];
    var hoverVisible = sliceVisible(hoverSeries.points, tStart, tEnd);

    ctx.font = FONT;
    var labelTexts = [];
    var maxLabelWidth = 0;
    var suffix = this.valueSuffix || '';

    if (showAxis) {
      // 左侧留白按最宽刻度标签自适应，避免长数字被裁掉或互相重叠
      for (var k = 0; k < axisScale.ticks.length; k += 1) {
        var text = formatTick(axisScale.ticks[k], axisScale.step) + suffix;
        labelTexts.push(text);
        var measured = ctx.measureText(text).width;
        if (measured > maxLabelWidth) maxLabelWidth = measured;
      }
    }
    var plotLeft = showAxis ? Math.max(PAD.left, Math.ceil(maxLabelWidth) + 22) : 26;
    var plotWidth = width - plotLeft - PAD.right;
    if (plotWidth < 24) return;

    var xOf = function (t) {
      return tEnd === tStart ? plotLeft + plotWidth / 2 : plotLeft + ((t - tStart) / tSpan) * plotWidth;
    };
    var yOf = function (v) {
      var ratio = (v - axisScale.min) / (axisScale.max - axisScale.min);
      return plotTop + plotHeight - ratio * plotHeight;
    };

    this._geometry = {
      left: plotLeft,
      width: plotWidth,
      top: plotTop,
      height: plotHeight,
      tStart: tStart,
      tSpan: tSpan,
      visible: hoverVisible
    };

    // 横向网格与 Y 轴刻度
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var g = 0; showAxis && g < axisScale.ticks.length; g += 1) {
      var tick = axisScale.ticks[g];
      var gy = yOf(tick);
      if (gy < plotTop - 1 || gy > plotTop + plotHeight + 1) continue;
      ctx.beginPath();
      ctx.moveTo(plotLeft, gy);
      ctx.lineTo(plotLeft + plotWidth, gy);
      ctx.strokeStyle = g === 0 ? '#e3e7ef' : '#eff1f6';
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = '#98a2b3';
      ctx.fillText(labelTexts[g], plotLeft - 10, gy);
    }

    // X 轴时间刻度
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#98a2b3';
    if (visible.length <= 1) {
      // 只有一个采样点时没有时间跨度，重复画同一刻度没有意义
      ctx.textAlign = 'center';
      ctx.fillText(formatTime(tStart, 0, true), plotLeft + plotWidth / 2, plotTop + plotHeight + 9);
    } else {
      var divisions = width < 520 ? 3 : 5;
      for (var d = 0; d <= divisions; d += 1) {
        var t = tStart + (tSpan * d) / divisions;
        var tx = plotLeft + (plotWidth * d) / divisions;
        ctx.textAlign = d === 0 ? 'left' : d === divisions ? 'right' : 'center';
        ctx.fillText(formatTime(t, tSpan, false), tx, plotTop + plotHeight + 9);
      }
    }

    // 曲线绘制（裁剪到绘图区内，缩放后线段不会溢出到轴标签上）
    ctx.save();
    ctx.beginPath();
    ctx.rect(plotLeft, plotTop, plotWidth, plotHeight);
    ctx.clip();

    if (this.series.length > 1) {
      this._drawSeries(ctx, scales, plotTop, plotHeight, xOf, tStart, tEnd, tSpan);
    } else {
      var gradient = ctx.createLinearGradient(0, plotTop, 0, plotTop + plotHeight);
      gradient.addColorStop(0, rgba(this.color, 0.20));
      gradient.addColorStop(1, rgba(this.color, 0.01));
      ctx.beginPath();
      ctx.moveTo(xOf(visible[0].t), plotTop + plotHeight);
      for (var a = 0; a < visible.length; a += 1) {
        ctx.lineTo(xOf(visible[a].t), yOf(visible[a].v));
      }
      ctx.lineTo(xOf(visible[visible.length - 1].t), plotTop + plotHeight);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();

      ctx.beginPath();
      for (var b = 0; b < visible.length; b += 1) {
        var px = xOf(visible[b].t);
        var py = yOf(visible[b].v);
        if (b === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.strokeStyle = this.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();

      // 单点时补一个圆点，否则曲线不可见
      if (visible.length === 1) {
        ctx.beginPath();
        ctx.arc(xOf(visible[0].t), yOf(visible[0].v), 4, 0, Math.PI * 2);
        ctx.fillStyle = this.color;
        ctx.fill();
      }

      // 末端高亮点
      if (visible.length > 1 && this.hoverIndex === null) {
        var lastPoint = visible[visible.length - 1];
        ctx.beginPath();
        ctx.arc(xOf(lastPoint.t), yOf(lastPoint.v), 3.5, 0, Math.PI * 2);
        ctx.fillStyle = this.color;
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    ctx.restore();

    // 只有存在统一刻度时才谈得上读数：单指标始终可用，叠加时聚焦某条才有
    if (showAxis && this.hoverIndex !== null && hoverVisible.length) {
      this._drawHover(
        ctx, hoverVisible, hoverSeries.color || this.color,
        xOf, yOf, width, plotTop, plotHeight, tSpan
      );
    }
  };

  /* 多条折线叠加：统一按当前激活指标的刻度绘制。
     切换图例即换坐标系，刻度与所有可见线的读数始终一致。 */
  LineChart.prototype._drawSeries = function (ctx, scales, plotTop, plotHeight, xOf, tStart, tEnd, tSpan) {
    var activeIndex = this.activeIndex;
    var focusing = activeIndex >= 0;

    this.series.forEach(function (s, index) {
      var scale = scales[index];
      var span = (scale.max - scale.min) || 1;
      var yOf = function (v) {
        return plotTop + plotHeight - ((v - scale.min) / span) * plotHeight;
      };
      var isActive = !focusing || index === activeIndex;

      ctx.beginPath();
      var started = false;
      s.points.forEach(function (p) {
        if (p.t < tStart - tSpan || p.t > tEnd + tSpan) return;
        var px = xOf(p.t);
        var py = yOf(p.v);
        if (!started) { ctx.moveTo(px, py); started = true; }
        else ctx.lineTo(px, py);
      });

      ctx.strokeStyle = s.color;
      ctx.lineWidth = isActive ? 2.2 : 1.6;
      ctx.globalAlpha = isActive ? 1 : 0.55;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();
      ctx.globalAlpha = 1;
    });
  };

  /* 切换纵轴归属：图例点击时调用 */
  LineChart.prototype.setActiveSeries = function (index) {
    if (index < -1 || index >= this.series.length) return;
    this.activeIndex = index;
    this.hoverIndex = null;
    this.requestRender();
  };

  LineChart.prototype._drawHover = function (ctx, points, color, xOf, yOf, width, plotTop, plotHeight, tSpan) {
    var index = Math.max(0, Math.min(points.length - 1, this.hoverIndex));
    var point = points[index];
    var x = xOf(point.t);
    var y = yOf(point.v);

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, plotTop);
    ctx.lineTo(x, plotTop + plotHeight);
    ctx.strokeStyle = '#c4cddd';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.arc(x, y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.stroke();

    var text = formatTime(point.t, tSpan, true) + '   '
      + formatNumber(point.v, true) + (this.valueSuffix || '');
    ctx.font = FONT;
    var textWidth = ctx.measureText(text).width;
    var boxW = textWidth + 18;
    var boxH = 26;
    var boxX = x + 12;
    if (boxX + boxW > width - 4) boxX = x - boxW - 12;
    var boxY = Math.max(4, y - boxH - 10);

    ctx.save();
    roundRect(ctx, boxX, boxY, boxW, boxH, 6);
    ctx.fillStyle = 'rgba(23, 29, 41, 0.92)';
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, boxX + 9, boxY + boxH / 2 + 0.5);
    ctx.restore();
  };

  function drawSparkline(canvas, values, color) {
    var ctx = canvas.getContext('2d');
    var rect = canvas.getBoundingClientRect();
    var width = Math.max(1, Math.floor(rect.width));
    var height = Math.max(1, Math.floor(rect.height));
    var dpr = global.devicePixelRatio || 1;

    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!values || values.length < 2) return;

    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var span = max - min;
    if (span <= 0) span = Math.max(1, Math.abs(max) * 0.01);

    var stepX = (width - 2) / (values.length - 1);
    var yOf = function (v) { return height - 2 - ((v - min) / span) * (height - 4); };

    ctx.beginPath();
    for (var i = 0; i < values.length; i += 1) {
      var x = 1 + stepX * i;
      var y = yOf(values[i]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();

    var lastX = 1 + stepX * (values.length - 1);
    ctx.beginPath();
    ctx.arc(lastX, yOf(values[values.length - 1]), 2, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  global.LineChart = LineChart;
  global.drawSparkline = drawSparkline;
  global.formatNumber = formatNumber;
  global.chartFormatTime = formatTime;
  /* 纯函数单独暴露，便于直接验证刻度与取值规则，不参与渲染 */
  global.chartNiceScale = niceScale;
  global.chartFormatTick = formatTick;
  global.chartDecimalsFor = decimalsFor;
})(window);
