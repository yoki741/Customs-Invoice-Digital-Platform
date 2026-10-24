import { createUniver, LocaleType } from '@univerjs/presets';
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core';
import zhCN from '@univerjs/preset-sheets-core/locales/zh-CN';
import '@univerjs/preset-sheets-core/lib/index.css';

const state = {
  univer: null,
  univerAPI: null,
  editable: new Map(), // sheetId -> Set("r,c")
  fieldIndex: new Map(), // "sheetId:r,c" -> a1 coord
  nameToId: new Map(),
  idToName: new Map(),
  onChange: null,
  guardReady: false,
  suppress: new Set(), // 程序化写入的 "sheetId:r,c"，不回声到表单
};

function parseA1(a1) {
  const m = /^([A-Za-z]+)(\d+)$/.exec(String(a1).trim());
  if (!m) return null;
  const letters = m[1].toUpperCase();
  let col = 0;
  for (const ch of letters) col = col * 26 + (ch.charCodeAt(0) - 64);
  return { row: parseInt(m[2], 10) - 1, col: col - 1 };
}

function toA1(row, col) {
  let n = col + 1;
  let letters = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor((n - 1) / 26);
  }
  return `${letters}${row + 1}`;
}

function buildIndex(snapshot) {
  state.editable.clear();
  state.fieldIndex.clear();
  state.nameToId.clear();
  state.idToName.clear();
  state.suppress.clear();
  Object.values(snapshot.sheets || {}).forEach((s) => {
    state.nameToId.set(s.name, s.id);
    state.idToName.set(s.id, s.name);
  });
}

function markEditable(editableByName) {
  state.editable.clear();
  state.fieldIndex.clear();
  Object.entries(editableByName || {}).forEach(([sheetName, coords]) => {
    const sheetId = state.nameToId.get(sheetName);
    if (!sheetId) return;
    const set = state.editable.get(sheetId) || new Set();
    coords.forEach((a1) => {
      const p = parseA1(a1);
      if (!p) return;
      const key = `${p.row},${p.col}`;
      set.add(key);
      state.fieldIndex.set(`${sheetId}:${key}`, a1.toUpperCase());
    });
    state.editable.set(sheetId, set);
  });
}

function inEditable(sheetId, range) {
  const set = state.editable.get(sheetId);
  if (!set || !range) return true; // 未配置白名单时不拦截
  const r0 = range.startRow ?? range.row;
  const c0 = range.startColumn ?? range.column;
  const r1 = range.endRow ?? r0;
  const c1 = range.endColumn ?? c0;
  for (let r = r0; r <= r1; r += 1) {
    for (let c = c0; c <= c1; c += 1) {
      if (!set.has(`${r},${c}`)) return false;
    }
  }
  return true;
}

function ensureGuard() {
  if (state.guardReady || !state.univerAPI) return;
  state.guardReady = true;
  const api = state.univerAPI;

  const commandRanges = (params) => {
    if (params.range) return [params.range];
    if (Array.isArray(params.ranges) && params.ranges.length) return params.ranges;
    return [];
  };

  const sheetIdOf = (params) =>
    (params.range && params.range.sheetId) ||
    params.subUnitId ||
    params.sheetId ||
    api.getActiveWorkbook?.()?.getActiveSheet?.()?.getSheetId?.() ||
    '';

  api.addEvent(api.Event.BeforeCommandExecute, (event) => {
    const id = String(event.id || '');
    const isSet = id.includes('set-range-values');
    const isClear = id.includes('clear-selection');
    if (!isSet && !isClear) return;
    const params = event.params || {};
    const ranges = commandRanges(params);
    if (!ranges.length) return;
    const sid = sheetIdOf(params);
    if (!ranges.every((r) => inEditable(sid, r))) event.cancel = true;
  });

  api.addEvent(api.Event.CommandExecuted, (event) => {
    const id = String(event.id || '');
    const isSet = id.includes('set-range-values');
    const isClear = id.includes('clear-selection');
    if (!isSet && !isClear) return;
    if (typeof state.onChange !== 'function') return;
    const params = event.params || {};
    const ranges = commandRanges(params);
    if (!ranges.length) return;
    const wb = api.getActiveWorkbook();
    if (!wb) return;
    ranges.forEach((range) => {
      const sheetId = range.sheetId || params.subUnitId || params.sheetId ||
        wb.getActiveSheet?.()?.getSheetId?.();
      const rkey = `${sheetId}:${range.startRow},${range.startColumn}`;
      if (state.suppress.has(rkey)) {
        state.suppress.delete(rkey);
        return;
      }
      const coord = state.fieldIndex.get(rkey);
      if (!coord) return;
      try {
        const sheet = wb.getSheetBySheetId(sheetId);
        const value = sheet.getRange(coord).getValue();
        state.onChange(state.idToName.get(sheetId), coord, value);
      } catch (err) {
        console.warn('读取单元格失败', err);
      }
    });
  });
}

const InvoiceGrid = {
  /** 载入快照并渲染到容器 */
  load(containerId, snapshot, editableByName, onChange) {
    buildIndex(snapshot);
    markEditable(editableByName);
    state.onChange = onChange || null;

    if (state.univerAPI) {
      state.univerAPI.createWorkbook(snapshot);
      return true;
    }

    const created = createUniver({
      locale: LocaleType.ZH_CN,
      locales: { [LocaleType.ZH_CN]: zhCN },
      presets: [UniverSheetsCorePreset({ container: containerId })],
    });
    state.univer = created.univer;
    state.univerAPI = created.univerAPI;
    ensureGuard();
    state.univerAPI.createWorkbook(snapshot);
    return true;
  },

  getCellValue(sheetName, a1) {
    const wb = state.univerAPI && state.univerAPI.getActiveWorkbook();
    if (!wb) return null;
    const sheet = wb.getSheetBySheetId(state.nameToId.get(sheetName));
    if (!sheet) return null;
    try {
      return sheet.getRange(a1).getValue();
    } catch (e) {
      return null;
    }
  },

  setCellValue(sheetName, a1, value) {
    const wb = state.univerAPI && state.univerAPI.getActiveWorkbook();
    if (!wb) return false;
    const sheetId = state.nameToId.get(sheetName);
    const sheet = wb.getSheetBySheetId(sheetId);
    if (!sheet) return false;
    const pos = parseA1(a1);
    const key = pos ? `${sheetId}:${pos.row},${pos.col}` : '';
    try {
      const range = sheet.getRange(a1);
      if (key) state.suppress.add(key);
      if (value === '' || value === null || value === undefined) {
        if (typeof range.clearContent === 'function') range.clearContent();
        else range.clear();
      } else {
        range.setValue(value);
      }
      return true;
    } catch (e) {
      if (key) state.suppress.delete(key);
      console.warn('写入单元格失败', e);
      return false;
    }
  },

  /** 激活指定工作表 */
  showSheet(sheetName) {
    const wb = state.univerAPI && state.univerAPI.getActiveWorkbook();
    if (!wb) return;
    const id = state.nameToId.get(sheetName);
    if (id) wb.setActiveSheet?.(wb.getSheetBySheetId(id));
  },
};

window.InvoiceGrid = InvoiceGrid;
InvoiceGrid.internals = () => state;
export default InvoiceGrid;
