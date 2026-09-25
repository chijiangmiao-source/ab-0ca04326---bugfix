/* 值班长补丁补传页面。
 * 原则：屏幕上的 "权威全文/修订" 只来自真实接口；localStorage 仅保存
 * 本终端的基准修订与最近确认结果，刷新后不把本地草案当作已批准内容。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const LS_KEY = "interlock-client-v1";

const state = {
  doc: { text: "", revision: null },
  draft: null, // { base_revision, base_text }
};

function loadLocal() {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || "null");
  } catch (_) {
    return null;
  }
}

function saveLocal(patch) {
  localStorage.setItem(LS_KEY, JSON.stringify(patch));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) {
    const err = new Error((data && data.message) || `HTTP ${res.status}`);
    err.code = data && data.error;
    err.body = data;
    throw err;
  }
  return data;
}

function renderDoc() {
  $("server-rev").textContent = String(state.doc.revision);
  $("doc").value = state.doc.text;
}

function renderLocalCard() {
  const local = loadLocal();
  const card = $("local-card");
  if (!local) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const conf = local.last_confirmed;
  $("local-info").innerHTML =
    `本终端保存的基准修订：<strong>${local.base_revision}</strong>` +
    (conf
      ? `；最近确认结果：补丁 <code>${conf.id}</code> → 修订 <strong>${conf.revision}</strong>` +
        `（${conf.idempotent ? "重传未新增修订" : "新确认"}）。` +
        `刷新后权威状态仍以服务端接口为准，本地旧草案不会被当作已批准内容。`
      : `；尚无已确认补丁。`);
}

async function refreshDocument() {
  $("health").textContent = "拉取中…";
  try {
    state.doc = await api("GET", "/api/document");
    renderDoc();
    $("health").textContent = "已连接";
    if (state.draft) $("draft-base").textContent = String(state.draft.base_revision);
  } catch (err) {
    $("health").textContent = "接口不可用";
    throw err;
  }
}

async function createDraft() {
  const d = await api("POST", "/api/drafts");
  state.draft = { base_revision: d.base_revision, base_text: d.base_text };
  saveLocal({ base_revision: d.base_revision,
              last_confirmed: (loadLocal() || {}).last_confirmed || null });
  $("draft-card").classList.remove("hidden");
  $("draft-base").textContent = String(d.base_revision);
  $("draft-doc").value = d.base_text;
  $("result-card").classList.add("hidden");
  $("error-card").classList.add("hidden");
  const local = loadLocal() || {};
  if (!$("patch-id").value) {
            $("patch-id").value =
        `terminal-${(local.base_revision ?? 0)}-${Date.now().toString(36)}`;
  }
  renderLocalCard();
}

function selectedKind() {
  return document.querySelector('input[name="kind"]:checked').value;
}

function syncKindFields() {
  const kind = selectedKind();
  $("insert-fields").classList.toggle("hidden", kind !== "insert");
  $("delete-fields").classList.toggle("hidden", kind !== "delete");
}

function fillPositionFromSelection() {
  const ta = $("draft-doc");
  const kind = selectedKind();
  if (kind === "insert") {
    $("ins-pos").value = ta.selectionStart;
  } else if (ta.selectionStart !== ta.selectionEnd) {
    $("del-lo").value = ta.selectionStart;
    $("del-hi").value = ta.selectionEnd;
  } else {
    $("del-lo").value = ta.selectionStart;
  }
}

function piecesToString(pieces) {
  return pieces.map((p) => {
    if (p[0] === "ins") {
      return `ins(pos=${p[1]}, text=${JSON.stringify(p[2])}, id=${JSON.stringify(p[3])})`;
    }
    return `del([${p[1]}, ${p[2]}), id=${JSON.stringify(p[3])})`;
  }).join("\n");
}

async function submitPatch() {
  if (!state.draft) {
    alert("请先建立草案");
    return;
  }
  const id = $("patch-id").value.trim();
  if (!id) { alert("补丁标识 id 必填"); return; }
  const req = { id, base_revision: state.draft.base_revision };
  if (selectedKind() === "insert") {
    req.kind = "insert";
    req.payload = { pos: Number($("ins-pos").value), text: $("ins-text").value };
  } else {
    req.kind = "delete";
    req.payload = {
      lo: Number($("del-lo").value),
      hi: $("del-hi").value === "" ? undefined : Number($("del-hi").value),
      length: $("del-length").value === "" ? undefined : Number($("del-length").value),
    };
  }

  try {
    const r = await api("POST", "/api/patches", req);
    // 页面立即显示服务端确认的全文、连续修订和实际落点
    const confirmedRev = r.revision;
    const confirmedText = r.text;
    state.doc = { text: r.current_text || r.text,
                  revision: r.current_revision || r.revision };
    renderDoc();
    $("result-card").classList.remove("hidden");
    $("error-card").classList.add("hidden");
    $("res-rev").textContent =
      `#${confirmedRev}（基于草案修订 #${r.base_revision}，` +
      `提交${r.idempotent ? "为重传，未新增修订" : "后成为最新修订"}）`;
    $("res-idem").classList.toggle("hidden", !r.idempotent);
    $("res-pieces").textContent = piecesToString(r.pieces) + "（空表示被并发补丁完全覆盖）";
    $("res-text").textContent = confirmedText;
    saveLocal({ base_revision: state.doc.revision,
                last_confirmed: { id: r.id, revision: confirmedRev,
                                  idempotent: !!r.idempotent } });
    // 确认后旧草案作废；如需继续编辑须重新建立草案
    state.draft = null;
    $("draft-card").classList.add("hidden");
    renderLocalCard();
  } catch (err) {
    // 被拒绝：显示拒绝原因，并以接口返回的权威全文/修订恢复页面
    $("error-card").classList.remove("hidden");
    $("result-card").classList.add("hidden");
    $("err-msg").textContent = `${err.code || "error"}：${err.message}`;
    if (err.body && typeof err.body.revision === "number") {
      state.doc = { text: err.body.text, revision: err.body.revision };
      renderDoc();
      $("err-text").textContent = err.body.text;
    } else {
      await refreshDocument();
      $("err-text").textContent = state.doc.text;
    }
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  syncKindFields();
  document.querySelectorAll('input[name="kind"]').forEach((el) =>
    el.addEventListener("change", syncKindFields));
  $("refresh").addEventListener("click", refreshDocument);
  $("new-draft").addEventListener("click", () => createDraft().catch(
    (e) => alert("建立草案失败：" + e.message)));
  $("submit").addEventListener("click", () => submitPatch().catch(
    (e) => alert("提交失败：" + e.message)));
  $("discard").addEventListener("click", () => {
    state.draft = null;
    $("draft-card").classList.add("hidden");
  });
  $("draft-doc").addEventListener("mouseup", fillPositionFromSelection);
  $("draft-doc").addEventListener("keyup", fillPositionFromSelection);

  renderLocalCard();
  await refreshDocument();
});
