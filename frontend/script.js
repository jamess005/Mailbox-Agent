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
  attachments: [],       // [{ name, size, type, base64 }, …]
  threads: [],           // array of thread objects
  activeThreadId: null,
  abortControllers: {},  // threadId → AbortController
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
const attachDropText= $('attachDropText');
const attachList    = $('attachList');

const sendBtn       = $('sendBtn');
const sendBtnLabel  = $('sendBtnLabel');
const sendSpinner   = $('sendSpinner');
const sendArrow     = $('sendArrow');

const backBtn          = $('backBtn');
const threadSubject    = $('threadSubject');
const threadMessages   = $('threadMessages');
const threadDeleteBtn  = $('threadDeleteBtn');

const replyComposer      = $('replyComposer');
const replyComposerRef   = $('replyComposerRef');
const replyComposerBody  = $('replyComposerBody');
const replyComposerHint  = $('replyComposerHint');
const replyComposerSend  = $('replyComposerSend');
const replyComposerClose = $('replyComposerClose');

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
   ATTACHMENT (multi-file)
═══════════════════════════ */
const MAX_ATTACHMENTS = 10;

attachDrop.addEventListener('click', () => attachInput.click());
attachDrop.addEventListener('dragover', e => { e.preventDefault(); attachDrop.style.borderColor = 'var(--amber)'; });
attachDrop.addEventListener('dragleave', () => { attachDrop.style.borderColor = ''; });
attachDrop.addEventListener('drop', e => {
  e.preventDefault();
  attachDrop.style.borderColor = '';
  if (e.dataTransfer.files.length) addAttachments(e.dataTransfer.files);
});
attachInput.addEventListener('change', () => { if (attachInput.files.length) addAttachments(attachInput.files); });

function addAttachments(files) {
  const remaining = MAX_ATTACHMENTS - state.attachments.length;
  if (remaining <= 0) { audit(`Maximum ${MAX_ATTACHMENTS} files reached.`, 'warn'); return; }

  const toAdd = Array.from(files).slice(0, remaining);
  if (toAdd.length < files.length) {
    audit(`Only ${toAdd.length} of ${files.length} files added (limit ${MAX_ATTACHMENTS}).`, 'warn');
  }

  let pending = 0;
  let loaded = 0;
  for (const file of toAdd) {
    if (file.size > 25 * 1024 * 1024) { audit(`${file.name}: too large (max 25 MB).`, 'warn'); continue; }
    pending++;
    const reader = new FileReader();
    reader.onload = e => {
      state.attachments.push({
        name: file.name, size: file.size, type: file.type,
        base64: e.target.result.split(',')[1],
      });
      loaded++;
      if (loaded === pending) renderAttachList();
      audit(`Attachment loaded: ${file.name}`, 'info');
    };
    reader.readAsDataURL(file);
  }
  if (pending === 0) renderAttachList();
}

function removeAttachment(index) {
  state.attachments.splice(index, 1);
  renderAttachList();
}

function clearAttachments() {
  state.attachments = [];
  attachInput.value = '';
  renderAttachList();
}

function renderAttachList() {
  attachList.innerHTML = '';
  state.attachments.forEach((a, i) => {
    const chip = document.createElement('div');
    chip.className = 'attach-chip';
    chip.innerHTML = `
      <span class="attach-chip-icon">📎</span>
      <span class="attach-chip-name">${esc(a.name)} (${fmtBytes(a.size)})</span>
      <button class="attach-chip-remove">✕</button>
    `;
    chip.querySelector('.attach-chip-remove').addEventListener('click', e => {
      e.stopPropagation();
      removeAttachment(i);
    });
    attachList.appendChild(chip);
  });

  if (state.attachments.length >= MAX_ATTACHMENTS) {
    attachDrop.classList.add('hidden');
  } else {
    attachDrop.classList.remove('hidden');
    attachDropText.textContent = state.attachments.length > 0
      ? `Add more invoices (${state.attachments.length}/${MAX_ATTACHMENTS})`
      : `Attach invoices (max ${MAX_ATTACHMENTS})`;
  }
}

/* ═══════════════════════════
   SEND
═══════════════════════════ */
sendBtn.addEventListener('click', handleSend);

async function handleSend() {
  const from    = cFrom.value.trim();
  const subject = cSubject.value.trim();
  const body    = cBody.value.trim();
  const hasFile = state.attachments.length > 0;
  const hasMultipleFiles = state.attachments.length > 1;
  const hasText = !!body;

  if (!from)           { audit('Please enter a FROM address.', 'warn'); cFrom.focus(); return; }
  if (!subject)        { audit('Please enter a subject.', 'warn'); cSubject.focus(); return; }
  if (!hasFile && !hasText) { audit('Please attach an invoice or write a message body.', 'warn'); return; }

  let pipeline, pipelineLabel;
  if (hasMultipleFiles)      { pipeline = 'invoice_batch';          pipelineLabel = 'BATCH';    }
  else if (hasFile && hasText) { pipeline = 'combined_verification'; pipelineLabel = 'COMBINED'; }
  else if (hasFile)          { pipeline = 'invoice_extraction';    pipelineLabel = 'INVOICE';  }
  else                       { pipeline = 'supplier_query';        pipelineLabel = 'QUERY';    }

  const finalSubject = subject || (hasFile ? `Invoice — ${state.attachments[0].name}` : 'Supplier Query');

  const threadId = `thread_${Date.now()}`;
  const now = new Date();

  const attachmentsList = hasFile
    ? state.attachments.map(a => ({ name: a.name, size: a.size }))
    : null;

  const outboundMsg = {
    id:          `msg_${Date.now()}`,
    direction:   'outbound',
    from,
    to:          MAILBOX,
    time:        now,
    body:        body || null,
    attachments: attachmentsList,
    pipeline,
    pipelineLabel,
  };

  const thread = {
    id:       threadId,
    subject:  finalSubject,
    from,
    pipeline,
    pipelineLabel,
    messages: [outboundMsg],
    status:   'pending',
    time:     now,
    pendingInvoiceNumber: null,
    pendingBatchKey:      null,
  };

  state.threads.unshift(thread);
  state.activeThreadId = threadId;
  addInboxRow(thread);
  showThread(threadId);
  closeCompose();

  audit(`Sent → ${pipeline} | from: ${from} | subject: ${finalSubject}`, 'info');

  showProcessing(threadId);

  cFrom.value = ''; cSubject.value = ''; cBody.value = '';

  if (hasMultipleFiles) {
    // ── Batch: send all invoices in one request ──
    const batchPayload = {
      pipeline:  'invoice_batch',
      timestamp: now.toISOString(),
      email:     { from, subject: finalSubject, body: null },
      invoices:  state.attachments.map(a => ({
        filename:          a.name,
        mime_type:         a.type,
        size_bytes:        a.size,
        base64_data:       a.base64,
        extraction_engine: 'docling',
      })),
    };
    clearAttachments();

    await callBackend(threadId, batchPayload);

    // If there was also body text, fire a query after the batch resolves
    if (hasText) {
      const stillExists = state.threads.find(t => t.id === threadId);
      if (stillExists) {
        showProcessing(threadId, false);
        const queryPayload = {
          pipeline:  'supplier_query',
          timestamp: new Date().toISOString(),
          email:     { from, subject: finalSubject, body },
        };
        await callBackend(threadId, queryPayload);
      }
    }
  } else if (hasFile && hasText) {
    // ── Single file + text: combined flow (invoice first, then query) ──
    const invPayload = {
      pipeline:  'invoice_extraction',
      timestamp: now.toISOString(),
      email:     { from, subject: finalSubject, body: null },
      invoice:   {
        filename:          state.attachments[0].name,
        mime_type:         state.attachments[0].type,
        size_bytes:        state.attachments[0].size,
        base64_data:       state.attachments[0].base64,
        extraction_engine: 'docling',
      },
    };
    clearAttachments();

    await callBackend(threadId, invPayload);

    const stillExists = state.threads.find(t => t.id === threadId);
    if (stillExists) {
      showProcessing(threadId, false);
      const queryPayload = {
        pipeline:  'supplier_query',
        timestamp: new Date().toISOString(),
        email:     { from, subject: finalSubject, body },
      };
      await callBackend(threadId, queryPayload);
    }
  } else {
    // ── Single file or query only ──
    const payload = {
      pipeline,
      timestamp: now.toISOString(),
      email:     { from, subject: finalSubject, body: body || null },
      invoice: hasFile ? {
        filename:          state.attachments[0].name,
        mime_type:         state.attachments[0].type,
        size_bytes:        state.attachments[0].size,
        base64_data:       state.attachments[0].base64,
        extraction_engine: 'docling',
      } : null,
    };
    clearAttachments();
    await callBackend(threadId, payload);
  }
}

/* ═══════════════════════════
   BACKEND CALL
═══════════════════════════ */
async function callBackend(threadId, payload, attempt = 1, _controller = null) {
  const MAX_ATTEMPTS = 12;
  const RETRY_MS     = 5000;

  // Each call gets its own AbortController so concurrent requests
  // (e.g. query in-flight while approval is sent) don't cancel each other.
  const controller = _controller || new AbortController();
  if (!state.abortControllers[threadId]) state.abortControllers[threadId] = new Set();
  if (!_controller) state.abortControllers[threadId].add(controller);
  const signal = controller.signal;

  try {
    const res = await fetch(`${API_BASE}/api/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });

    if (res.status === 503) {
      if (attempt >= MAX_ATTEMPTS) {
        removeProcessing(threadId);
        addReplyMessage(threadId, {
          status: 'error',
          body:   'Backend failed to become ready within 60 s. Please restart the server.',
        });
        audit('Backend did not become ready in time.', 'error');
        return;
      }
      const retryAfter = parseInt(res.headers.get('Retry-After') || '5', 10) * 1000;
      audit(`Server still loading… retrying in ${retryAfter / 1000} s (attempt ${attempt}/${MAX_ATTEMPTS})`, 'warn');
      setTimeout(() => callBackend(threadId, payload, attempt + 1, controller), retryAfter);
      return;
    }

    if (res.status === 429) {
      removeProcessing(threadId);
      addReplyMessage(threadId, {
        status: 'error',
        body:   'Rate limit reached. Please wait a moment before sending another request.',
      });
      audit('Rate limited by server (429).', 'warn');
      return;
    }

    removeProcessing(threadId);

    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`HTTP ${res.status}: ${errText}`);
    }

    const data = await res.json();
    addReplyMessage(threadId, data);
    audit(`Reply received — pipeline: ${payload.pipeline}`, 'ok');

  } catch (err) {
    if (err.name === 'AbortError') {
      removeProcessing(threadId);
      audit('Backend call aborted (thread deleted).', 'warn');
      return;
    }
    removeProcessing(threadId);
    addReplyMessage(threadId, {
      status:  'error',
      subject: 'Processing Error',
      body:    `Failed to process your message.\n\n${err.message}\n\nMake sure the backend server is running:\n  cd backend && uvicorn main:app --reload`,
    });
    audit(`Backend error: ${err.message}`, 'error');
  } finally {
    const set = state.abortControllers[threadId];
    if (set instanceof Set) {
      set.delete(controller);
      if (set.size === 0) delete state.abortControllers[threadId];
    } else {
      delete state.abortControllers[threadId];
    }
  }
}

/* ═══════════════════════════
   THREAD / MESSAGE RENDERING
═══════════════════════════ */
function showThread(threadId) {
  const thread = state.threads.find(t => t.id === threadId);
  if (!thread) return;

  state.activeThreadId = threadId;

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

  thread.messages.forEach(msg => renderMessage(msg, thread));

  if (thread.status === 'pending') {
    showProcessing(threadId, false);
  }

  closeReplyComposer();

  threadMessages.scrollTop = threadMessages.scrollHeight;
}

function renderMessage(msg, thread) {
  const card = document.createElement('div');

  if (msg.direction === 'outbound') {
    card.className = 'message-card outbound';
    card.innerHTML = `
      <div class="message-head">
        <div class="message-from-wrap">
          <span class="message-from">${esc(msg.from)}</span>
          <span class="message-to">to ${esc(msg.to)}</span>
        </div>
        <div class="message-meta">
          <span class="message-time">${fmtTime(msg.time)}</span>
          <span class="message-type-badge ${
            msg.pipeline === 'invoice_extraction' ? 'invoice'
          : msg.pipeline === 'invoice_batch'      ? 'combined'
          : msg.pipeline === 'supplier_query'     ? 'query'
          : msg.pipeline === 'invoice_approval' && msg.pipelineLabel === 'ACCEPT'  ? 'combined'
          : msg.pipeline === 'invoice_approval' && msg.pipelineLabel === 'DECLINE' ? 'decline'
          : msg.pipeline === 'batch_approval'   && msg.pipelineLabel === 'ACCEPT'  ? 'combined'
          : msg.pipeline === 'batch_approval'   && msg.pipelineLabel === 'DECLINE' ? 'decline'
          : 'combined'}">${msg.pipelineLabel}</span>
        </div>
      </div>
      ${msg.body ? `<div class="message-body">${esc(msg.body)}</div>` : ''}
      ${msg.attachments ? msg.attachments.map(a => `<div class="message-attachment">📎 ${esc(a.name)} <span style="color:var(--text-muted)">(${fmtBytes(a.size)})</span></div>`).join('') : msg.attachment ? `<div class="message-attachment">📎 ${esc(msg.attachment.name)} <span style="color:var(--text-muted)">(${fmtBytes(msg.attachment.size)})</span></div>` : ''}
      <div class="message-actions">
        <button class="msg-action-btn reply-action" data-action="reply">↩ Reply</button>
        <button class="msg-action-btn forward-action" data-action="forward">↗ Forward</button>
        <button class="msg-action-btn delete-action" data-action="delete">✕ Delete</button>
      </div>
    `;
  } else {
    const isPending = msg.status === 'pending_approval';
    const isError   = msg.status === 'error';
    const badgeLabel = isError ? 'ERROR' : isPending ? 'REVIEW' : 'REPLY';
    const badgeClass = isPending ? 'pending' : 'reply';
    card.className = 'message-card inbound';
    card.innerHTML = `
      <div class="message-head">
        <div class="message-from-wrap">
          <span class="message-from" style="color:var(--amber)">${MAILBOX}</span>
          <span class="message-to">to ${esc(msg.to || 'you')}</span>
        </div>
        <div class="message-meta">
          <span class="message-time">${fmtTime(msg.time || new Date())}</span>
          <span class="message-type-badge ${badgeClass}">${badgeLabel}</span>
        </div>
      </div>
      <div class="message-body reply-body">${esc(msg.body || '')}</div>
      <div class="message-actions">
        <button class="msg-action-btn reply-action" data-action="reply">↩ Reply</button>
        <button class="msg-action-btn forward-action" data-action="forward">↗ Forward</button>
        <button class="msg-action-btn delete-action" data-action="delete">✕ Delete</button>
      </div>
    `;
  }

  // Wire action buttons
  card.querySelector('[data-action="reply"]').addEventListener('click', () => {
    openReplyComposer(thread);
  });
  card.querySelector('[data-action="forward"]').addEventListener('click', () => {
    audit('Forward is not available in this simulation.', 'warn');
  });
  card.querySelector('[data-action="delete"]').addEventListener('click', () => {
    const idx = thread.messages.findIndex(m => m.id === msg.id);
    if (idx !== -1) {
      thread.messages.splice(idx, 1);
      card.remove();
      audit('Message deleted from thread.', 'edit');
      if (thread.messages.length === 0) deleteThread(thread.id);
    }
  });

  threadMessages.appendChild(card);
}

/* ═══════════════════════════
   REPLY COMPOSER (in-thread)
═══════════════════════════ */
function openReplyComposer(thread) {
  if (!thread) return;
  replyComposer.classList.remove('hidden');
  replyComposerRef.textContent = `Re: ${thread.subject}`;
  replyComposerBody.value = '';

  if (thread.pendingInvoiceNumber) {
    replyComposerHint.textContent = `Invoice ${thread.pendingInvoiceNumber} awaiting review — reply "accept" or "decline"`;
    replyComposerHint.classList.add('visible');
  } else if (thread.pendingBatchKey) {
    replyComposerHint.textContent = `Batch of invoices awaiting review — reply "accept" or "decline"`;
    replyComposerHint.classList.add('visible');
  } else {
    replyComposerHint.textContent = '';
    replyComposerHint.classList.remove('visible');
  }

  replyComposerBody.focus();
  setTimeout(() => {
    replyComposer.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, 50);
}

function closeReplyComposer() {
  replyComposer.classList.add('hidden');
  replyComposerBody.value = '';
  replyComposerHint.textContent = '';
  replyComposerHint.classList.remove('visible');
}

replyComposerClose.addEventListener('click', closeReplyComposer);
replyComposerSend.addEventListener('click', handleReplySend);
replyComposerBody.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) handleReplySend();
});

function handleReplySend() {
  const thread = state.threads.find(t => t.id === state.activeThreadId);
  if (!thread) return;

  const body = replyComposerBody.value.trim();
  if (!body) { audit('Reply body is empty.', 'warn'); replyComposerBody.focus(); return; }

  closeReplyComposer();

  const normalised = body.toLowerCase().replace(/[^a-z]/g, '');
  const isAccept  = (thread.pendingInvoiceNumber || thread.pendingBatchKey) && normalised === 'accept';
  const isDecline = (thread.pendingInvoiceNumber || thread.pendingBatchKey) && normalised === 'decline';

  if (isAccept || isDecline) {
    const decision = isAccept ? 'accept' : 'decline';

    if (thread.pendingInvoiceNumber) {
      // ── Single invoice approval ──
      const invoiceNumber = thread.pendingInvoiceNumber;
      thread.pendingInvoiceNumber = null;

      const outMsg = {
        id:            `msg_${Date.now()}`,
        direction:     'outbound',
        from:          thread.from,
        to:            MAILBOX,
        time:          new Date(),
        body,
        attachments:   null,
        pipeline:      'invoice_approval',
        pipelineLabel: decision.toUpperCase(),
      };
      thread.messages.push(outMsg);
      renderMessage(outMsg, thread);
      showProcessing(thread.id);

      audit(`Reply: ${decision.toUpperCase()} for invoice ${invoiceNumber}`,
            decision === 'accept' ? 'ok' : 'warn');

      callBackend(thread.id, {
        pipeline:       'invoice_approval',
        timestamp:      new Date().toISOString(),
        invoice_number: invoiceNumber,
        decision,
        email: { from: thread.from, subject: `Re: ${thread.subject}`, body },
      });
    } else {
      // ── Batch approval ──
      const batchKey = thread.pendingBatchKey;
      thread.pendingBatchKey = null;

      const outMsg = {
        id:            `msg_${Date.now()}`,
        direction:     'outbound',
        from:          thread.from,
        to:            MAILBOX,
        time:          new Date(),
        body,
        attachments:   null,
        pipeline:      'batch_approval',
        pipelineLabel: decision.toUpperCase(),
      };
      thread.messages.push(outMsg);
      renderMessage(outMsg, thread);
      showProcessing(thread.id);

      audit(`Reply: ${decision.toUpperCase()} for invoice batch`,
            decision === 'accept' ? 'ok' : 'warn');

      callBackend(thread.id, {
        pipeline:  'batch_approval',
        timestamp: new Date().toISOString(),
        batch_key: batchKey,
        decision,
        email: { from: thread.from, subject: `Re: ${thread.subject}`, body },
      });
    }
  } else {
    // Generic reply — route as a new query to backend
    const outMsg = {
      id:            `msg_${Date.now()}`,
      direction:     'outbound',
      from:          thread.from,
      to:            MAILBOX,
      time:          new Date(),
      body,
      attachment:    null,
      pipeline:      'supplier_query',
      pipelineLabel: 'QUERY',
    };
    thread.messages.push(outMsg);
    renderMessage(outMsg, thread);

    if (thread.pendingInvoiceNumber || thread.pendingBatchKey) {
      // Still pending approval — remind, don't route to backend
      const ref = thread.pendingInvoiceNumber
        ? `invoice ${thread.pendingInvoiceNumber}`
        : 'the invoice batch';
      const hintMsg = {
        id:        `msg_${Date.now() + 1}`,
        direction: 'inbound',
        from:      MAILBOX,
        to:        thread.from,
        time:      new Date(),
        status:    'pending_approval',
        body:      `Your reply was noted, but ${ref} is still awaiting your decision.\n\nPlease reply with "accept" or "decline" to proceed.`,
      };
      thread.messages.push(hintMsg);
      renderMessage(hintMsg, thread);
      audit('Reply noted — awaiting accept/decline decision.', 'info');
    } else {
      // Send to backend as supplier_query
      showProcessing(thread.id);
      audit(`Reply → supplier_query from: ${thread.from}`, 'info');

      callBackend(thread.id, {
        pipeline:  'supplier_query',
        timestamp: new Date().toISOString(),
        email: { from: thread.from, subject: `Re: ${thread.subject}`, body },
      });
    }
  }

  // Update inbox row snippet
  const row = document.querySelector(`.inbox-thread[data-id="${thread.id}"]`);
  if (row) {
    const snippet = row.querySelector('.thread-row-snippet');
    if (snippet) snippet.textContent = body.substring(0, 60).replace(/\n/g, ' ');
    const count = row.querySelector('.thread-row-count');
    if (count) count.textContent = thread.messages.length;
  }

  threadMessages.scrollTop = threadMessages.scrollHeight;
}

/* ═══════════════════════════
   PROCESSING INDICATOR
═══════════════════════════ */
function showProcessing(threadId, addToThread = true) {
  removeProcessing(threadId);
  const thread = state.threads.find(t => t.id === threadId);
  if (addToThread && thread) thread.status = 'pending';

  const el = document.createElement('div');
  el.className = 'processing-card';
  el.id        = `proc_${threadId}`;
  el.innerHTML = `
    <span class="processing-spin">⟳</span>
    <div>
      <div class="processing-text">Processing your message…</div>
      <div class="processing-sub">Pipeline running — this may take a moment</div>
    </div>
  `;
  threadMessages.appendChild(el);
  threadMessages.scrollTop = threadMessages.scrollHeight;
}

function removeProcessing(threadId) {
  const el = $(`proc_${threadId}`);
  if (el) el.remove();
}

function cancelProcessing(threadId) {
  const set = state.abortControllers[threadId];
  if (set instanceof Set) {
    for (const c of set) c.abort();
  } else if (set) {
    set.abort();
  }
  delete state.abortControllers[threadId];
  removeProcessing(threadId);
  const thread = state.threads.find(t => t.id === threadId);
  if (thread) thread.status = 'idle';
  audit('Request cancelled.', 'warn');
}

/* ═══════════════════════════
   REPLY MESSAGE (from backend)
═══════════════════════════ */
function addReplyMessage(threadId, data) {
  const thread = state.threads.find(t => t.id === threadId);
  if (!thread) return;

  const isPendingApproval = data.status === 'pending_approval';

  // Only update thread status if this is an error, an approval prompt, or
  // the thread is NOT already waiting for an accept/decline decision.
  if (data.status === 'error') {
    thread.status = 'error';
  } else if (isPendingApproval) {
    thread.status = 'pending_approval';
    thread.pendingInvoiceNumber = data.invoice_number || null;
    thread.pendingBatchKey      = data.batch_key      || null;
  } else if (thread.status !== 'pending_approval') {
    thread.status = 'replied';
  }
  // Note: pendingInvoiceNumber is only cleared by handleReplySend when
  // the user explicitly accepts or declines.

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
      if (isPendingApproval) {
        badge.textContent = 'REVIEW';
        badge.className   = 'thread-row-badge pending';
      } else if (thread.status !== 'pending_approval') {
        badge.textContent = 'REPLY';
        badge.className   = 'thread-row-badge reply';
      }
    }
    const snippet = row.querySelector('.thread-row-snippet');
    if (snippet) {
      const preview = (replyMsg.body || '').substring(0, 60).replace(/\n/g, ' ');
      snippet.textContent = preview || thread.subject;
    }
    const count = row.querySelector('.thread-row-count');
    if (count) count.textContent = thread.messages.length;
  }

  // Render if active thread
  if (state.activeThreadId === threadId) {
    renderMessage(replyMsg, thread);
    threadMessages.scrollTop = threadMessages.scrollHeight;
  }
}

function formatReplyBody(data) {
  if (data.status === 'error') return data.message || 'An error occurred.';
  const lines = [];
  if (data.invoice_number) lines.push(`Invoice ${data.invoice_number}`);
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
                   : thread.pipeline === 'supplier_query'     ? 'query'
                   : thread.pipeline === 'invoice_batch'      ? 'combined' : 'combined';
  const badgeLabel = thread.pipelineLabel;

  const snippet = (thread.messages[0] && thread.messages[0].body)
    ? thread.messages[0].body.substring(0, 60).replace(/\n/g, ' ')
    : thread.subject;

  row.innerHTML = `
    <div class="unread-dot"></div>
    <div class="thread-row-from">${esc(thread.from)}</div>
    <div class="thread-row-subject">${esc(thread.subject)}</div>
    <div class="thread-row-snippet">${esc(snippet)}</div>
    <div class="thread-row-meta">
      <span class="thread-row-time">${fmtTime(thread.time)}</span>
      <span class="thread-row-count">${thread.messages.length}</span>
      <span class="thread-row-badge ${badgeClass}">${badgeLabel}</span>
      <span class="thread-row-delete" title="Delete thread">DELETE</span>
    </div>
  `;

  row.querySelector('.thread-row-delete').addEventListener('click', e => {
    e.stopPropagation();
    deleteThread(thread.id);
  });

  row.addEventListener('click', () => showThread(thread.id));

  inboxList.insertBefore(row, inboxList.firstChild);
}

/* ═══════════════════════════
   THREAD NAV
═══════════════════════════ */
backBtn.addEventListener('click', () => {
  threadView.classList.add('hidden');
  closeReplyComposer();
  welcomeState.classList.remove('hidden');
  document.querySelectorAll('.inbox-thread').forEach(el => el.classList.remove('active'));
  state.activeThreadId = null;
});

threadDeleteBtn.addEventListener('click', () => {
  if (state.activeThreadId) deleteThread(state.activeThreadId);
});

function deleteThread(threadId) {
  // Abort any in-flight backend calls for this thread
  const set = state.abortControllers[threadId];
  if (set instanceof Set) {
    for (const c of set) c.abort();
  } else if (set) {
    set.abort();
  }
  delete state.abortControllers[threadId];

  const idx = state.threads.findIndex(t => t.id === threadId);
  if (idx !== -1) state.threads.splice(idx, 1);

  const row = document.querySelector(`.inbox-thread[data-id="${threadId}"]`);
  if (row) row.remove();

  if (state.threads.length === 0) inboxEmpty.classList.remove('hidden');

  if (state.activeThreadId === threadId) {
    state.activeThreadId = null;
    threadView.classList.add('hidden');
    closeReplyComposer();
    welcomeState.classList.remove('hidden');
    document.querySelectorAll('.inbox-thread').forEach(el => el.classList.remove('active'));
  }

  audit('Thread deleted.', 'edit');
}

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
