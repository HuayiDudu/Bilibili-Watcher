/* chart.js 的纯函数与坐标换算测试。
   浏览器对象用最小替身注入 vm 上下文，不引入任何测试依赖。 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'chart.js'), 'utf8');

function loadChart() {
  const window = { devicePixelRatio: 1, addEventListener() {} };
  const context = vm.createContext({ window, console });
  vm.runInContext(SOURCE, context, { filename: 'chart.js' });
  return window;
}

function makeContext() {
  const calls = { moveTo: [], lineTo: [], arc: [], fillText: [], lineDash: [] };
  const ctx = {
    calls,
    setTransform() {},
    clearRect() {},
    beginPath() {},
    closePath() {},
    stroke() {},
    fill() {},
    save() {},
    restore() {},
    arcTo() {},
    /* 缩放后的绘制需要把曲线裁在绘图区内 */
    rect() {},
    clip() {},
    moveTo(x, y) {
      calls.moveTo.push([x, y]);
    },
    lineTo(x, y) {
      calls.lineTo.push([x, y]);
    },
    arc(x, y, radius) {
      calls.arc.push([x, y, radius]);
    },
    fillText(text, x, y) {
      calls.fillText.push([text, x, y]);
    },
    setLineDash(pattern) {
      calls.lineDash.push(pattern);
    },
    measureText(text) {
      return { width: text.length * 7 };
    },
    createLinearGradient() {
      return { addColorStop() {} };
    },
  };
  return ctx;
}

function makeCanvas(width, height) {
  const ctx = makeContext();
  return {
    width: 0,
    height: 0,
    ctx,
    getContext() {
      return ctx;
    },
    getBoundingClientRect() {
      return { width, height, left: 0, top: 0 };
    },
    addEventListener() {},
  };
}

function within(value, low, high) {
  return value >= low - 1e-6 && value <= high + 1e-6;
}

test('刻度覆盖数据范围且等距', () => {
  const chart = loadChart();
  const cases = [
    [100, 220, 4],
    [0, 10, 4],
    [0, 3, 4],
    [500, 500, 4],
    [0, 0, 4],
    [7, 9, 4],
    [12345, 12346, 4],
    [1e6, 3e6, 4],
  ];

  for (const [min, max, count] of cases) {
    const scale = chart.chartNiceScale(min, max, count);
    const first = scale.ticks[0];
    const last = scale.ticks[scale.ticks.length - 1];
    const step = scale.ticks[1] - first;

    assert.ok(scale.ticks.length >= 2, `刻度过少：${min}..${max}`);
    assert.ok(first <= min, `下界未覆盖：${min}..${max}`);
    assert.ok(last >= max, `上界未覆盖：${min}..${max}`);
    assert.ok(step >= 1, `计数类指标步长不应小于 1：${min}..${max}`);

    for (let i = 1; i < scale.ticks.length; i += 1) {
      const gap = scale.ticks[i] - scale.ticks[i - 1];
      assert.ok(Math.abs(gap - step) < 1e-6, `刻度间距不一致：${min}..${max}`);
    }
  }
});

test('等值数据仍给出上下留白', () => {
  const chart = loadChart();
  const scale = chart.chartNiceScale(500, 500, 4);

  assert.deepStrictEqual(Array.from(scale.ticks), [499, 500, 501]);
});

test('整数数据不会被压成重复刻度', () => {
  const chart = loadChart();

  assert.deepStrictEqual(Array.from(chart.chartNiceScale(0, 3, 4).ticks), [0, 1, 2, 3]);
  assert.deepStrictEqual(
    Array.from(chart.chartNiceScale(0, 10, 4).ticks),
    [0, 2.5, 5, 7.5, 10]
  );
});

test('大数刻度按整步长展开', () => {
  const chart = loadChart();
  const scale = chart.chartNiceScale(1e6, 3e6, 4);

  assert.strictEqual(scale.ticks.length, 5);
  assert.strictEqual(scale.ticks[1] - scale.ticks[0], 500000);
});

test('数值缩写用于悬停提示', () => {
  const chart = loadChart();

  assert.strictEqual(chart.formatNumber(9999, false), '9,999');
  assert.strictEqual(chart.formatNumber(10000, false), '1万');
  assert.strictEqual(chart.formatNumber(15000, false), '1.5万');
  assert.strictEqual(chart.formatNumber(12345678, false), '1234.6万');
  assert.strictEqual(chart.formatNumber(1e8, false), '1亿');
  assert.strictEqual(chart.formatNumber(1.23e8, false), '1.23亿');
  assert.strictEqual(chart.formatNumber(-15000, false), '-1.5万');
});

test('精确模式下保留完整数字', () => {
  const chart = loadChart();
  assert.strictEqual(chart.formatNumber(12345678, true), '12,345,678');
});

test('缺失值显示占位符', () => {
  const chart = loadChart();

  assert.strictEqual(chart.formatNumber(null, false), '—');
  assert.strictEqual(chart.formatNumber(undefined, false), '—');
  assert.strictEqual(chart.formatNumber(NaN, false), '—');
});

test('刻度小数位由步长决定', () => {
  const chart = loadChart();

  assert.strictEqual(chart.chartDecimalsFor(5), 0);
  assert.strictEqual(chart.chartDecimalsFor(1), 0);
  assert.strictEqual(chart.chartDecimalsFor(2.5), 1);
  assert.strictEqual(chart.chartDecimalsFor(0.5), 1);
  assert.strictEqual(chart.chartDecimalsFor(0.25), 2);
  assert.strictEqual(chart.chartDecimalsFor(0.005), 3);
  assert.strictEqual(chart.chartDecimalsFor(0.0001), 4);
  assert.strictEqual(chart.chartDecimalsFor(0.30000000000000004), 1);
});

test('比值刻度不会被整数化', () => {
  const chart = loadChart();

  // 18.735% ~ 18.755% 这段区间曾因"数值≥1 就取整"全部显示成 19
  const scale = chart.chartNiceScale(18.735, 18.755, 4);
  const labels = Array.from(scale.ticks, (tick) => chart.chartFormatTick(tick, scale.step));

  assert.deepStrictEqual(labels, ['18.735', '18.740', '18.745', '18.750', '18.755']);
  assert.strictEqual(new Set(labels).size, labels.length, '刻度标签必须互不相同');
});

test('大数刻度保留千分位且不带小数', () => {
  const chart = loadChart();
  const scale = chart.chartNiceScale(7440020, 7440080, 4);

  assert.deepStrictEqual(
    Array.from(scale.ticks, (tick) => chart.chartFormatTick(tick, scale.step)),
    ['7,440,020', '7,440,040', '7,440,060', '7,440,080']
  );
});

test('极窄区间刻度不再互相重叠', () => {
  const chart = loadChart();

  // 相邻刻度只差 1e-7：曾经被统一舍入到 6 位小数，压成同一个值后完全看不见
  const scale = chart.chartNiceScale(1.3414770, 1.3414780, 4);

  assert.ok(scale.ticks.length >= 4, '应保留多个刻度');
  assert.strictEqual(
    new Set(scale.ticks).size,
    scale.ticks.length,
    '刻度值必须互不相同'
  );
});

test('时间刻度按跨度切换精度', () => {
  const chart = loadChart();
  const stamp = new Date(2024, 0, 2, 3, 4, 5).getTime();

  assert.strictEqual(chart.chartFormatTime(stamp, 10 * 60 * 1000, false), '03:04');
  assert.strictEqual(chart.chartFormatTime(stamp, 60 * 1000, false), '03:04:05');
  assert.strictEqual(chart.chartFormatTime(stamp, 60 * 60 * 1000, true), '03:04:05');
  assert.strictEqual(chart.chartFormatTime(stamp, 25 * 3600 * 1000, false), '1/2 03:04');
});

test('折线各点落在绘图区内', () => {
  const chart = loadChart();
  const canvas = makeCanvas(600, 300);
  const line = new chart.LineChart(canvas);

  line.setData(
    [
      { t: 0, v: 0 },
      { t: 60000, v: 100 },
      { t: 120000, v: 50 },
    ],
    '#2f6feb',
    '暂无数据'
  );

  const geometry = line._geometry;
  assert.ok(geometry, '渲染后应记录绘图区');
  assert.ok(geometry.left >= 64, '左侧留白不应小于默认值');
  assert.ok(geometry.width > 0);

  const points = canvas.ctx.calls.lineTo;
  assert.ok(points.length > 0);
  for (const [x, y] of points) {
    assert.ok(
      within(x, geometry.left, geometry.left + geometry.width),
      `横坐标越界：${x}`
    );
    assert.ok(within(y, geometry.top, geometry.top + geometry.height), `纵坐标越界：${y}`);
  }
});

test('单点数据居中绘制', () => {
  const chart = loadChart();
  const canvas = makeCanvas(600, 300);
  const line = new chart.LineChart(canvas);

  line.setData([{ t: 0, v: 5 }], '#2f6feb', '暂无数据');

  const geometry = line._geometry;
  const marks = canvas.ctx.calls.arc;
  assert.strictEqual(marks.length, 1);
  assert.ok(Math.abs(marks[0][0] - (geometry.left + geometry.width / 2)) < 1e-6);

  const labels = canvas.ctx.calls.fillText;
  const lastLabel = labels[labels.length - 1];
  assert.ok(Math.abs(lastLabel[1] - (geometry.left + geometry.width / 2)) < 1e-6);
});

test('空数据显示占位文本', () => {
  const chart = loadChart();
  const canvas = makeCanvas(600, 300);
  const line = new chart.LineChart(canvas);

  line.setData([], undefined, '暂无数据');

  assert.strictEqual(line._geometry, null);
  const labels = canvas.ctx.calls.fillText;
  assert.strictEqual(labels[labels.length - 1][0], '暂无数据');
});

test('悬停时绘制虚线并显示精确数值', () => {
  const chart = loadChart();
  const canvas = makeCanvas(600, 300);
  const line = new chart.LineChart(canvas);
  line.setData(
    [
      { t: 0, v: 1000 },
      { t: 60000, v: 2000 },
    ],
    '#2f6feb',
    '暂无数据'
  );

  line.hoverIndex = 1;
  line.render();

  assert.ok(canvas.ctx.calls.lineDash.length > 0, '悬停应画竖直参考线');
  const texts = canvas.ctx.calls.fillText.map((item) => item[0]);
  assert.ok(
    texts.some((text) => text.includes('2,000')),
    '提示框应显示精确数值'
  );
});

test('聚焦某条线后同样能读出该点数值', () => {
  const chart = loadChart();
  const canvas = makeCanvas(600, 300);
  const line = new chart.LineChart(canvas);

  line.setSeries([
    { name: '播放量', color: '#2f6feb', points: [{ t: 0, v: 1000000 }, { t: 60000, v: 2000000 }] },
    { name: '点赞数', color: '#0ea5e9', points: [{ t: 0, v: 3000 }, { t: 60000, v: 4000 }] },
  ]);

  // 叠加且未聚焦时纵轴不存在，不应给出读数（X 轴时间刻度仍在）
  canvas.ctx.calls.fillText.length = 0;
  line.hoverIndex = 0;
  line.render();
  assert.ok(
    !canvas.ctx.calls.fillText.some((item) => String(item[0]).includes('3,000')),
    '未聚焦时不应给出读数'
  );

  // 聚焦到点赞数，读数应取这条线的值
  line.setActiveSeries(1);
  canvas.ctx.calls.fillText.length = 0;
  line.hoverIndex = 0;
  line.render();

  const texts = canvas.ctx.calls.fillText.map((item) => item[0]);
  assert.ok(texts.some((text) => text.includes('3,000')), '应显示聚焦线的数值');
  assert.ok(canvas.ctx.calls.lineDash.length > 0, '应画竖直参考线');
});

test('迷你折线少于两点时不绘制', () => {
  const chart = loadChart();
  const canvas = makeCanvas(80, 24);

  chart.drawSparkline(canvas, [5], '#2f6feb');

  assert.strictEqual(canvas.ctx.calls.lineTo.length, 0);
});
