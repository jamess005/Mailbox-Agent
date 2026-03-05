/**
 * AIMailbox — Frontend
 *
 * Simulates an email mailbox that receives invoice attachments
 * and/or supplier queries, routes them to the backend pipeline,
 * and displays the reply as an email thread.
 *
 * Routing (determined by what is present):
 *   attachment only  → invoice_extraction
 *   body only        → supplier_query
 *   both             → combined_verification
 *
 * All sends go to: POST /api/process
 * Backend (FastAPI) handles DocLing, DB checks, LLM routing.
 */

/* ═══════════════════════════
   CONFIG
═══════════════════════════ */
const API_BASE = 'http://localhost:8000';
const MAILBOX  = 'AIMailbox@gmail.com';

/* ═══════════════════════════
   STATE
═══════════════════════════ */
const state = {
  attachment: null,      // { name, size, type, base64 }
  threads: [],           // array of thread objects
  activeThreadId: null,
};

/* ═══════════════════════════
   DOM
═══════════════════════════ */
const $ = id => document.getElementById(id);

const composeBtn    = $('composeBtn');
const composePanel  = $('composePanel');
const composeClose  = $('composeClose');
const welcomeState  = $('welcomeState');
const threadView    = $('threadView');
const inboxList     = $('inboxList');
const inboxEmpty    = $('inboxEmpty');

const cFrom         = $('cFrom');
const cSubject      = $('cSubject');
const cBody         = $('cBody');

const attachDrop    = $('attachDrop');
const attachInput   = $('attachInput');
const attachChip    = $('attachChip');
const attachChipName= $('attachChipName');
const attachRemove  = $('attachRemove');

const sendBtn       = $('sendBtn');
const sendBtnLabel  = $('sendBtnLabel');
const sendSpinner   = $('sendSpinner');
const sendArrow     = $('sendArrow');

const backBtn       = $('backBtn');
const threadSubject = $('threadSubject');
const threadMessages= $('threadMessages');

const auditLog      = $('auditLog');
const auditCount    = $('auditCount');
const auditClear    = $('auditClear');

/* ═══════════════════════════
   COMPOSE
═══════════════════════════ */
composeBtn.addEventListener('click', openCompose);
composeClose.addEventListener('click', closeCompose);

function openCompose() {
  welcomeState.classList.add('hidden');
  threadView.classList.add('hidden');
  composePanel.classList.remove('hidden');
  // Deselect inbox
  document.querySelectorAll('.inbox-thread').forEach(el => el.classList.remove('active'));
  state.activeThreadId = null;
  cFrom.focus();
}

function closeCompose() {
  composePanel.classList.add('hidden');
  if (state.threads.length === 0) welcomeState.classList.remove('hidden');
  else if (state.activeThreadId) showThread(state.activeThreadId);
  else welcomeState.classList.remove('hidden');
}

/* ═══════════════════════════
   ATTACHMENT
═══════════════════════════ */
attachDrop.addEventListener('click', () => attachInput.click());
attachDrop.addEventListener('dragover', e => { e.preventDefault(); attachDrop.style.borderColor = 'var(--amber)'; });
attachDrop.addEventListener('dragleave', () => { attachDrop.style.borderColor = ''; });
attachDrop.addEventListener('drop', e => {
  e.preventDefault();
  attachDrop.style.borderColor = '';
  if (e.dataTransfer.files[0]) loadAttachment(e.dataTransfer.files[0]);
});
attachInput.addEventListener('change', () => { if (attachInput.files[0]) loadAttachment(attachInput.files[0]); });
attachRemove.addEventListener('click', clearAttachment);

function loadAttachment(file) {
  if (file.size > 25 * 1024 * 1024) { audit('File too large (max 25 MB).', 'warn'); return; }
  const reader = new FileReader();
  reader.onload = e => {
    state.attachment = { name: file.name, size: file.size, type: file.type, base64: e.target.result.split(',')[1] };
    attachChipName.textContent = `${file.name} (${fmtBytes(file.size)})`;
    attachDrop.classList.add('hidden');
    attachChip.classList.remove('hidden');
    audit(`Attachment loaded: ${file.name}`, 'info');
  };
  reader.readAsDataURL(file);
}

function clearAttachment() {
  state.attachment = null;
  attachInput.value = '';
  attachChip.classList.add('hidden');
  attachDrop.classList.remove('hidden');
}

/* ═══════════════════════════
   SEND
═══════════════════════════ */
sendBtn.addEventListener('click', handleSend);

async function handleSend() {
  const from    = cFrom.value.trim();
  const subject = cSubject.value.trim();
  const body    = cBody.value.trim();
  const hasFile = !!state.attachment;
  const hasText = !!body;

  if (!from)           { audit('Please enter a FROM address.', 'warn'); cFrom.focus(); return; }
  if (!subject)        { audit('Please enter a subject.', 'warn'); cSubject.focus(); return; }
  if (!hasFile && !hasText) { audit('Please attach an invoice or write a message body.', 'warn'); return; }

  // Determine pipeline
  let pipeline, pipelineLabel;
  if (hasFile && hasText)  { pipeline = 'combined_verification'; pipelineLabel = 'COMBINED'; }
  else if (hasFile)        { pipeline = 'invoice_extraction';    pipelineLabel = 'INVOICE';  }
  else                     { pipeline = 'supplier_query';        pipelineLabel = 'QUERY';    }

  // Auto-generate subject if blank-ish
  const finalSubject = subject || (hasFile ? `Invoice — ${state.attachment.name}` : 'Supplier Query');

  // Create thread
  const threadId = `thread_${Date.now()}`;
  const now = new Date();

  const outboundMsg = {
    id:        `msg_${Date.now()}`,
    direction: 'outbound',
    from:      from,
    to:        MAILBOX,
    time:      now,
    body:      body || null,
    attachment: hasFile ? { name: state.attachment.name, size: state.attachment.size } : null,
    pipeline,
    pipelineLabel,
  };

  const thread = {
    id:       threadId,
    subject:  finalSubject,
    from:     from,
    pipeline,
    pipelineLabel,
    messages: [outboundMsg],
    status:   'pending',
    time:     now,
  };

  state.threads.unshift(thread);
  state.activeThreadId = threadId;
  addInboxRow(thread);
  showThread(threadId);
  closeCompose();

  audit(`Sent → ${pipeline} | from: ${from} | subject: ${finalSubject}`, 'info');

  // Build payload for backend
  const payload = {
    pipeline,
    timestamp:  now.toISOString(),
    email: {
      from,
      subject:  finalSubject,
      body:     body || null,
    },
    invoice: hasFile ? {
      filename:          state.attachment.name,
      mime_type:         state.attachment.type,
      size_bytes:        state.attachment.size,
      base64_data:       state.attachment.base64,
      extraction_engine: 'docling',
    } : null,
  };

  // Show processing indicator
  showProcessing(threadId);

  // Reset compose
  cFrom.value = ''; cSubject.value = ''; cBody.value = '';
  clearAttachment();

  // Call backend
  await callBackend(threadId, payload);
}

/* ═══════════════════════════
   BACKEND CALL
═══════════════════════════ */
async function callBackend(threadId, payload) {
  try {
    const res = await fetch(`${API_BASE}/api/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    removeProcessing(threadId);

    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`HTTP ${res.status}: ${errText}`);
    }

    const data = await res.json();
    addReplyMessage(threadId, data);
    audit(`Reply received — pipeline: ${payload.pipeline}`, 'ok');

  } catch (err) {
    removeProcessing(threadId);
    // Show error as a reply in the thread
    addReplyMessage(threadId, {
      status:  'error',
      subject: 'Processing Error',
      body:    `Failed to process your message.\n\n${err.message}\n\nMake sure the backend server is running:\n  cd backend && uvicorn main:app --reload`,
    });
    audit(`Backend error: ${err.message}`, 'error');
  }
}

/* ═══════════════════════════
   THREAD / MESSAGE RENDERING
═══════════════════════════ */
function showThread(threadId) {
  const thread = state.threads.find(t => t.id === threadId);
  if (!thread) return;

  state.activeThreadId = threadId;

  // Mark as read in sidebar
  document.querySelectorAll('.inbox-thread').forEach(el => el.classList.remove('active'));
  const row = document.querySelector(`.inbox-thread[data-id="${threadId}"]`);
  if (row) {
    row.classList.add('active');
    row.classList.remove('unread');
    const dot = row.querySelector('.unread-dot');
    if (dot) dot.remove();
  }

  welcomeState.classList.add('hidden');
  composePanel.classList.add('hidden');
  threadView.classList.remove('hidden');

  threadSubject.textContent = thread.subject;
  threadMessages.innerHTML  = '';

  thread.messages.forEach(msg => renderMessage(msg));

  // Re-add processing card if still pending
  if (thread.status === 'pending') {
    showProcessing(threadId, false); // false = don't re-add to state
  }

  threadMessages.scrollTop = threadMessages.scrollHeight;
}

function renderMessage(msg) {
  if (msg.direction === 'outbound') {
    const card = document.createElement('div');
    card.className = 'message-card outbound';
    card.innerHTML = `
      <div class="message-head">
        <div class="message-from-wrap">
          <span class="message-from">${esc(msg.from)}</span>
          <span class="message-to">to ${esc(msg.to)}</span>
        </div>
        <div class="message-meta">
          <span class="message-time">${fmtTime(msg.time)}</span>
          <span class="message-type-badge ${msg.pipeline === 'invoice_extraction' ? 'invoice' : msg.pipeline === 'supplier_query' ? 'query' : 'combined'}">${msg.pipelineLabel}</span>
        </div>
      </div>
      ${msg.body ? `<div class="message-body">${esc(msg.body)}</div>` : ''}
      ${msg.attachment ? `<div class="message-attachment">📎 ${esc(msg.attachment.name)} <span style="color:var(--text-muted)">(${fmtBytes(msg.attachment.size)})</span></div>` : ''}
    `;
    threadMessages.appendChild(card);
  } else {
    const card = document.createElement('div');
    card.className = `message-card inbound`;
    const statusColour = msg.status === 'error' ? 'var(--red)' : 'var(--green)';
    card.innerHTML = `
      <div class="message-head">
        <div class="message-from-wrap">
          <span class="message-from" style="color:var(--amber)">${MAILBOX}</span>
          <span class="message-to">to ${esc(msg.to || 'you')}</span>
        </div>
        <div class="message-meta">
          <span class="message-time">${fmtTime(msg.time || new Date())}</span>
          <span class="message-type-badge reply">${msg.status === 'error' ? 'ERROR' : 'REPLY'}</span>
        </div>
      </div>
      <div class="message-body reply-body">${esc(msg.body || '')}</div>
    `;
    threadMessages.appendChild(card);
  }
}

function showProcessing(threadId, addToThread = true) {
  // Remove any existing
  removeProcessing(threadId);

  const thread = state.threads.find(t => t.id === threadId);
  if (addToThread && thread) thread.status = 'pending';

  const el = document.createElement('div');
  el.className     = 'processing-card';
  el.id            = `proc_${threadId}`;
  el.innerHTML = `
    <span class="processing-spin">⟳</span>
    <div>
      <div class="processing-text">Processing your message…</div>
      <div class="processing-sub">DocLing extraction + pipeline validation running</div>
    </div>
  `;
  threadMessages.appendChild(el);
  threadMessages.scrollTop = threadMessages.scrollHeight;
}

function removeProcessing(threadId) {
  const el = $(`proc_${threadId}`);
  if (el) el.remove();
}

function addReplyMessage(threadId, data) {
  const thread = state.threads.find(t => t.id === threadId);
  if (!thread) return;

  thread.status = data.status === 'error' ? 'error' : 'replied';

  const replyMsg = {
    id:        `msg_${Date.now()}`,
    direction: 'inbound',
    from:      MAILBOX,
    to:        thread.from,
    time:      new Date(),
    status:    data.status || 'ok',
    body:      data.email_body || data.body || formatReplyBody(data),
  };

  thread.messages.push(replyMsg);

  // Update inbox row
  const row = document.querySelector(`.inbox-thread[data-id="${threadId}"]`);
  if (row) {
    const badge = row.querySelector('.thread-row-badge');
    if (badge) {
      badge.textContent = 'REPLY';
      badge.className   = 'thread-row-badge reply';
    }
  }

  // If thread is visible, render
  if (state.activeThreadId === threadId) {
    renderMessage(replyMsg);
    threadMessages.scrollTop = threadMessages.scrollHeight;
  }
}

function formatReplyBody(data) {
  // Fallback formatter if backend doesn't return email_body
  if (data.status === 'error') return data.message || 'An error occurred.';
  const lines = [];
  if (data.invoice_number) lines.push(`Invoice #${data.invoice_number}`);
  if (data.supplier)       lines.push(`Supplier: ${data.supplier}`);
  if (data.message)        lines.push(data.message);
  if (data.checks)         lines.push('\nChecks:\n' + Object.entries(data.checks).map(([k,v]) => `  ${k}: ${v}`).join('\n'));
  return lines.join('\n') || JSON.stringify(data, null, 2);
}

/* ═══════════════════════════
   INBOX ROW
═══════════════════════════ */
function addInboxRow(thread) {
  inboxEmpty.classList.add('hidden');

  const row = document.createElement('div');
  row.className  = 'inbox-thread unread';
  row.dataset.id = thread.id;

  const badgeClass = thread.pipeline === 'invoice_extraction' ? 'invoice'
                   : thread.pipeline === 'supplier_query'     ? 'query' : 'combined';
  const badgeLabel = thread.pipelineLabel;

  row.innerHTML = `
    <div class="unread-dot"></div>
    <div class="thread-row-from">${esc(thread.from)}</div>
    <div class="thread-row-subject">${esc(thread.subject)}</div>
    <div class="thread-row-meta">
      <span class="thread-row-time">${fmtTime(thread.time)}</span>
      <span class="thread-row-badge ${badgeClass}">${badgeLabel}</span>
    </div>
  `;

  row.addEventListener('click', () => showThread(thread.id));

  // Prepend — newest at top
  inboxList.insertBefore(row, inboxList.firstChild);
}

backBtn.addEventListener('click', () => {
  threadView.classList.add('hidden');
  welcomeState.classList.remove('hidden');
  document.querySelectorAll('.inbox-thread').forEach(el => el.classList.remove('active'));
  state.activeThreadId = null;
});

/* ═══════════════════════════
   AUDIT LOG
═══════════════════════════ */
let auditEventCount = 0;

function audit(msg, type = 'info') {
  auditEventCount++;
  auditCount.textContent = `${auditEventCount} event${auditEventCount !== 1 ? 's' : ''}`;
  const ts    = new Date().toTimeString().slice(0, 8);
  const entry = document.createElement('span');
  entry.className   = `audit-entry type-${type}`;
  entry.textContent = `[${ts}] ${msg}`;
  auditLog.appendChild(entry);
  auditLog.scrollTop = auditLog.scrollHeight;
}

auditClear.addEventListener('click', () => {
  auditLog.innerHTML = '';
  auditEventCount = 0;
  auditCount.textContent = '0 events';
  const init = document.createElement('span');
  init.className   = 'audit-entry type-init';
  init.textContent = '▸ Log cleared.';
  auditLog.appendChild(init);
});

/* ═══════════════════════════
   UTILS
═══════════════════════════ */
function esc(s) {
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtBytes(b) {
  if (!b) return '';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

function fmtTime(d) {
  if (!d) return '';
  const dt = d instanceof Date ? d : new Date(d);
  return dt.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

/* ═══════════════════════════
   INIT
═══════════════════════════ */
audit('AIMailbox ready. Backend expected at ' + API_BASE, 'init');