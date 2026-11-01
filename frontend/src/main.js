import { UserManager, WebStorageStateStore } from "oidc-client-ts";
import "./style.css";

// ---------- Configuration ----------

// Values come from frontend/.env.local, which Terraform writes (see infrastructure/outputs.tf).
const APP_URL = window.location.origin; // http://localhost:3000
const COGNITO_AUTHORITY = import.meta.env.VITE_COGNITO_AUTHORITY;
const COGNITO_CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID;
const COGNITO_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN;
const API_URL = import.meta.env.VITE_API_URL;

const missingConfig = Object.entries({
  VITE_API_URL: API_URL,
  VITE_COGNITO_AUTHORITY: COGNITO_AUTHORITY,
  VITE_COGNITO_CLIENT_ID: COGNITO_CLIENT_ID,
  VITE_COGNITO_DOMAIN: COGNITO_DOMAIN
}).filter(([, value]) => !value).map(([key]) => key);

if (missingConfig.length) {
  document.querySelector("#app").textContent =
    `Missing configuration: ${missingConfig.join(", ")}. ` +
    "Run terraform apply in infrastructure/ (it writes frontend/.env.local) and restart npm run dev.";
  throw new Error("Missing frontend configuration");
}

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
const AUTO_REFRESH_MS = 3000;
const MAX_AUTO_REFRESH = 40; // about 2 minutes

const auth = new UserManager({
  authority: COGNITO_AUTHORITY,
  client_id: COGNITO_CLIENT_ID,
  redirect_uri: `${APP_URL}/callback`,
  response_type: "code",
  scope: "openid email",
  userStore: new WebStorageStateStore({ store: window.sessionStorage })
});

// How each backend status is shown to the user.
const STATUS = {
  created: { label: "No file", tone: "idle" },
  pending_upload: { label: "Upload not finished", tone: "idle" },
  uploaded: { label: "Queued", tone: "busy" },
  processing: { label: "Reading text", tone: "busy" },
  ready: { label: "Ready", tone: "ok" },
  failed: { label: "Couldn't read", tone: "bad" },
  rejected: { label: "Rejected", tone: "bad" }
};
const BUSY = ["uploaded", "processing"];
const HAS_FILE = ["uploaded", "processing", "ready", "failed"];

const STOPWORDS = new Set(
  ("the and for are was were with that this from what when where which who why how does did " +
   "can could would should about into your you our their there then than have has had not " +
   "document pdf please tell explain summarize summary").split(" ")
);

// ---------- Page skeleton ----------

document.querySelector("#app").innerHTML = `
  <header class="topbar">
    <span class="brand">Document Assistant</span>
    <div class="account" id="account" hidden>
      <span id="who"></span>
      <button class="btn btn-quiet btn-small" id="signOut" type="button">Sign out</button>
    </div>
  </header>

  <section class="welcome" id="signedOut" hidden>
    <h1>Ask your PDFs where things are.</h1>
    <p>Upload a PDF and its text is extracted automatically. Ask a question and you get the
       passages that answer it, with the page they come from.</p>
    <button class="btn btn-primary" id="signIn" type="button">Sign in</button>
    <div class="welcome-sample" aria-hidden="true">
      Objects in Amazon S3 are stored in <mark>buckets</mark>, and each object is identified by a unique key.
      <small>Page 4 of “Introduction to AWS”</small>
    </div>
  </section>

  <main class="workspace" id="workspace" hidden>
    <aside class="library">
      <section class="upload" aria-labelledby="uploadHeading">
        <h2 id="uploadHeading" hidden>Add a PDF</h2>
        <input type="file" id="fileInput" accept="application/pdf" hidden />
        <label class="drop-target" for="fileInput" id="dropTarget">
          <strong>Add a PDF</strong>
          <span>Drop it here or choose a file. Up to 10 MB.</span>
        </label>

        <form class="upload-form" id="uploadForm" hidden>
          <p class="file-chosen" id="fileChosen"></p>
          <label class="field">Title
            <input id="uploadTitle" required maxlength="200" />
          </label>
          <label class="field">Category
            <input id="uploadCategory" required maxlength="100" value="General" />
          </label>
          <div class="upload-actions">
            <button class="btn btn-primary" id="uploadButton" type="submit">Upload</button>
            <button class="btn btn-quiet" id="uploadCancel" type="button">Cancel</button>
          </div>
          <p class="upload-progress" id="uploadProgress" role="status"></p>
        </form>
      </section>

      <section aria-labelledby="libraryHeading">
        <div class="library-head">
          <h2 id="libraryHeading">Your documents</h2>
          <button class="btn btn-quiet btn-small" id="refresh" type="button">Refresh</button>
        </div>
        <ul class="doc-list" id="docList"></ul>
      </section>
    </aside>

    <section class="reader" id="reader" aria-live="polite"></section>
  </main>

  <div class="toast" id="toast" role="status" hidden></div>
`;

const $ = (selector) => document.querySelector(selector);

const state = {
  docs: [],
  aiEnabled: false,
  selectedId: null,
  tab: "ask",
  renderedKey: "",
  texts: new Map(), // document_id -> text response
  pendingFile: null,
  refreshTimer: null,
  refreshCount: 0
};

// ---------- Small helpers ----------

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

let toastTimer = null;
function toast(message, tone = "info") {
  const box = $("#toast");
  box.textContent = message;
  box.dataset.tone = tone;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), tone === "bad" ? 6000 : 3500);
}

function searchTerms(text) {
  const words = (text.toLowerCase().match(/[\p{L}\p{N}]+/gu) || [])
    .filter((w) => w.length > 2 && !STOPWORDS.has(w));
  return [...new Set(words)];
}

// Returns a fragment where every occurrence of the terms is wrapped in <mark>.
function highlighted(text, terms) {
  const fragment = document.createDocumentFragment();
  if (!terms.length) {
    fragment.append(text);
    return fragment;
  }
  // "buckets" also marks "bucket" and "bucketing": match the word stem plus any ending.
  const stems = terms.map((t) => (t.length > 4 && t.endsWith("s") ? t.slice(0, -1) : t));
  const escaped = stems.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`((?:${escaped.join("|")})\\p{L}*)`, "giu");
  text.split(pattern).forEach((part, index) => {
    if (!part) return;
    // split() with one capture group puts the matches at odd positions
    fragment.append(index % 2 === 1 ? el("mark", { text: part }) : part);
  });
  return fragment;
}

function statusBadge(status) {
  const info = STATUS[status] || { label: status, tone: "idle" };
  return el("span", { class: "badge", "data-tone": info.tone, text: info.label });
}

function formatDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function formatSize(bytes) {
  if (!bytes) return "";
  return bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// ---------- API ----------

async function apiRequest(method, query = "", body = null) {
  const user = await auth.getUser();
  if (!user || user.expired) {
    showSignedOut();
    throw new Error("Your session has expired. Sign in again.");
  }

  const headers = { Authorization: `Bearer ${user.access_token}` };
  if (body) headers["Content-Type"] = "application/json";

  const response = await fetch(`${API_URL}${query}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = data.code ? ` (${data.code})` : "";
    throw new Error(`${data.error || `Request failed with HTTP ${response.status}`}${code}`);
  }
  return data;
}

const docQuery = (id, action) =>
  `?document_id=${encodeURIComponent(id)}${action ? `&action=${action}` : ""}`;

// ---------- Sign in / out ----------

function showSignedOut() {
  $("#signedOut").hidden = false;
  $("#workspace").hidden = true;
  $("#account").hidden = true;
  clearTimeout(state.refreshTimer);
}

function showWorkspace(user) {
  $("#signedOut").hidden = true;
  $("#workspace").hidden = false;
  $("#account").hidden = false;
  $("#who").textContent = user.profile?.email || "Signed in";
}

$("#signIn").addEventListener("click", () =>
  auth.signinRedirect().catch((err) => toast(err.message, "bad"))
);

$("#signOut").addEventListener("click", async () => {
  await auth.removeUser();
  const logoutUri = encodeURIComponent(`${APP_URL}/`);
  window.location.href =
    `${COGNITO_DOMAIN}/logout?client_id=${COGNITO_CLIENT_ID}&logout_uri=${logoutUri}`;
});

// ---------- Document list ----------

async function loadDocuments({ auto = false } = {}) {
  clearTimeout(state.refreshTimer);
  if (!auto) state.refreshCount = 0;

  try {
    const data = await apiRequest("GET");
    state.docs = data.documents;
    state.aiEnabled = Boolean(data.ai_enabled);
  } catch (err) {
    toast(err.message, "bad");
    return;
  }

  if (state.selectedId && !state.docs.some((d) => d.document_id === state.selectedId)) {
    state.selectedId = null;
  }

  renderDocList();
  renderReader();

  const busy = state.docs.some((d) => BUSY.includes(d.status));
  if (busy && state.refreshCount < MAX_AUTO_REFRESH) {
    state.refreshCount += 1;
    state.refreshTimer = setTimeout(() => loadDocuments({ auto: true }), AUTO_REFRESH_MS);
  }
}

function renderDocList() {
  const list = $("#docList");
  list.replaceChildren();

  if (state.docs.length === 0) {
    list.append(el("li", { class: "empty", text: "No documents yet. Add a PDF above to get started." }));
    return;
  }

  for (const doc of state.docs) {
    const item = el(
      "button",
      {
        class: "doc-item",
        type: "button",
        "aria-current": doc.document_id === state.selectedId ? "true" : "false",
        onclick: () => selectDocument(doc.document_id)
      },
      [el("span", { class: "doc-item-title", text: doc.title }), statusBadge(doc.status)]
    );
    list.append(el("li", {}, item));
  }
}

function selectDocument(id) {
  if (state.selectedId !== id) state.tab = "ask";
  state.selectedId = id;
  state.renderedKey = "";
  renderDocList();
  renderReader();
  // On phones the reader is below the list, so bring it into view.
  if (window.matchMedia("(max-width: 860px)").matches) {
    $("#reader").scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"
    });
  }
}

$("#refresh").addEventListener("click", () => loadDocuments());

// ---------- Reader ----------

function selectedDoc() {
  return state.docs.find((d) => d.document_id === state.selectedId);
}

function renderReader() {
  const reader = $("#reader");
  const doc = selectedDoc();

  // Only rebuild when something visible changed, so typing and answers are kept during auto-refresh.
  const key = doc
    ? `${doc.document_id}:${doc.status}:${state.tab}:${state.aiEnabled}`
    : `empty:${state.docs.length > 0}`;
  if (key === state.renderedKey) return;
  state.renderedKey = key;

  if (!doc) {
    reader.replaceChildren(
      el("p", {
        class: "reader-empty",
        text: state.docs.length
          ? "Choose a document on the left to read its text or search it."
          : "Add your first PDF on the left. Its text is extracted automatically, then you can search it here."
      })
    );
    return;
  }

  const meta = el("div", { class: "doc-meta" }, [
    statusBadge(doc.status),
    el("span", { text: doc.category }),
    doc.page_count ? el("span", { text: `${doc.page_count} ${doc.page_count === 1 ? "page" : "pages"}` }) : null,
    doc.file_size ? el("span", { text: formatSize(doc.file_size) }) : null,
    el("span", { text: `Added ${formatDate(doc.created_at)}` })
  ]);

  const actions = el("div", { class: "doc-actions" }, [
    HAS_FILE.includes(doc.status)
      ? el("button", { class: "btn btn-quiet", type: "button", text: "Open PDF", onclick: () => openPdf(doc) })
      : null,
    el("button", { class: "btn btn-danger", type: "button", text: "Delete", onclick: () => deleteDocument(doc) })
  ]);

  const head = el("div", { class: "doc-head" }, [
    el("div", {}, [el("h1", { class: "doc-title", text: doc.title }), meta]),
    actions
  ]);

  const parts = [head];

  if (doc.status === "ready") {
    parts.push(renderTabs(), el("div", { class: "panel", id: "panel" }));
  } else {
    parts.push(statusNotice(doc));
  }

  reader.replaceChildren(...parts);

  if (doc.status === "ready") {
    if (state.tab === "ask") renderAskPanel(doc);
    else renderTextPanel(doc);
  }
}

function statusNotice(doc) {
  const messages = {
    uploaded: "The PDF is queued. Its text will be extracted in a few seconds.",
    processing: "Extracting the text from this PDF. This usually takes a few seconds.",
    pending_upload: "This upload didn't finish. Delete it and upload the PDF again.",
    created: "This entry has no PDF attached.",
    rejected: "This file was rejected. Only PDF files up to 10 MB are accepted.",
    failed: `${doc.error_message || "The text couldn't be extracted."} Try a PDF with selectable text rather than a scan.`
  };
  return el("p", {
    class: "notice",
    "data-tone": doc.status === "failed" || doc.status === "rejected" ? "bad" : "info",
    text: messages[doc.status] || `Status: ${doc.status}`
  });
}

function renderTabs() {
  const tab = (id, label) =>
    el("button", {
      class: "tab",
      type: "button",
      role: "tab",
      "aria-selected": state.tab === id ? "true" : "false",
      text: label,
      onclick: () => {
        if (state.tab === id) return;
        state.tab = id;
        renderReader();
      }
    });
  return el("div", { class: "tabs", role: "tablist" }, [
    tab("ask", state.aiEnabled ? "Ask" : "Search"),
    tab("text", "Text")
  ]);
}

// ---------- Ask / search ----------

function renderAskPanel(doc) {
  const question = el("textarea", {
    id: "question",
    rows: "3",
    maxlength: "1000",
    placeholder: state.aiEnabled
      ? "Ask a question about this document"
      : "What are you looking for? For example: how are objects stored?",
    "aria-label": "Question"
  });
  const answer = el("div", { class: "answer", id: "answer" });

  const askButton = el("button", { class: "btn btn-primary", type: "submit", text: state.aiEnabled ? "Ask" : "Search" });
  const summarizeButton = state.aiEnabled
    ? el("button", { class: "btn btn-quiet", type: "button", text: "Summarize" })
    : null;

  const run = async (text) => {
    if (!text.trim()) {
      answer.replaceChildren(el("p", { class: "answer-intro", text: "Type a question first." }));
      question.focus();
      return;
    }
    [askButton, summarizeButton].forEach((b) => b && (b.disabled = true));
    answer.replaceChildren(el("p", { class: "answer-intro", text: state.aiEnabled ? "Thinking…" : "Searching…" }));
    try {
      const res = await apiRequest("POST", docQuery(doc.document_id, "ask"), { question: text });
      showAnswer(answer, res, text);
    } catch (err) {
      answer.replaceChildren(el("p", { class: "notice", "data-tone": "bad", text: err.message }));
    } finally {
      [askButton, summarizeButton].forEach((b) => b && (b.disabled = false));
    }
  };

  const form = el(
    "form",
    {
      class: "ask",
      onsubmit: (event) => {
        event.preventDefault();
        run(question.value);
      }
    },
    [
      question,
      el("div", { class: "ask-actions" }, [
        askButton,
        summarizeButton,
        el("span", { class: "ask-hint", text: "Ctrl + Enter to send" })
      ])
    ]
  );

  question.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) form.requestSubmit();
  });
  summarizeButton?.addEventListener("click", () => run("Summarize this document in a few bullet points."));

  $("#panel").replaceChildren(form, answer);
}

function showAnswer(container, res, question) {
  const terms = searchTerms(question);

  // No-AI mode: show the best passages, best first.
  if (res.passages) {
    if (!res.passages.length) {
      container.replaceChildren(el("p", { class: "answer-intro", text: "Nothing in this document matches that." }));
      return;
    }
    const matched = terms.length && res.passages.some((p) => terms.some((t) => p.text.toLowerCase().includes(t)));
    container.replaceChildren(
      el("p", {
        class: "answer-intro",
        text: matched
          ? "Passages that best match your question:"
          : "No exact match. Here is how the document starts:"
      }),
      ...res.passages.map((p) =>
        el("div", { class: "passage" }, [
          el("p", { class: "passage-page", text: `Page ${p.page}` }),
          el("p", { class: "passage-text" }, highlighted(p.text, terms))
        ])
      )
    );
    return;
  }

  // AI mode: answer plus the pages that were sent to the model.
  const parts = [el("p", { class: "answer-text", text: res.answer })];
  if (res.model) parts.push(el("small", { class: "answer-by", text: `Answered by ${res.model}` }));
  if (res.sources?.length) {
    parts.push(
      el("details", { class: "sources" }, [
        el("summary", { text: `Based on ${res.sources.length} ${res.sources.length === 1 ? "page" : "pages"}` }),
        ...res.sources.map((s) => el("p", {}, highlighted(`Page ${s.page}: “${s.snippet}…”`, terms)))
      ])
    );
  }
  container.replaceChildren(...parts);
}

// ---------- Text ----------

async function renderTextPanel(doc) {
  const panel = $("#panel");
  panel.replaceChildren(el("p", { class: "text-note", text: "Loading text…" }));

  let data = state.texts.get(doc.document_id);
  if (!data) {
    try {
      data = await apiRequest("GET", docQuery(doc.document_id, "text"));
      state.texts.set(doc.document_id, data);
    } catch (err) {
      panel.replaceChildren(el("p", { class: "notice", "data-tone": "bad", text: err.message }));
      return;
    }
  }
  if (state.selectedId !== doc.document_id || state.tab !== "text") return; // user moved on

  // The preview looks like "[Page 1]\n...text...\n\n[Page 2]\n...".
  const pages = [];
  const pattern = /\[Page (\d+)\]\n([\s\S]*?)(?=\n\n\[Page \d+\]\n|$)/g;
  for (const match of data.preview.matchAll(pattern)) pages.push({ page: match[1], text: match[2] });

  const find = el("input", {
    class: "find",
    type: "search",
    placeholder: "Find in text",
    "aria-label": "Find in text"
  });
  const pagesBox = el("div");

  const draw = () => {
    const terms = searchTerms(find.value);
    pagesBox.replaceChildren(
      ...pages.map((p) =>
        el("section", { class: "page" }, [
          el("p", { class: "page-number", text: `Page ${p.page}` }),
          el("p", { class: "page-text" }, highlighted(p.text, terms))
        ])
      )
    );
  };
  find.addEventListener("input", draw);
  draw();

  const note = `${data.page_count} ${data.page_count === 1 ? "page" : "pages"}, ${data.text_length.toLocaleString()} characters.` +
    (data.truncated ? " Showing the beginning of the text." : "");

  panel.replaceChildren(el("p", { class: "text-note", text: note }), find, pagesBox);
}

// ---------- Open / delete ----------

async function openPdf(doc) {
  const tab = window.open("", "_blank");
  try {
    const res = await apiRequest("GET", docQuery(doc.document_id, "download"));
    tab.location = res.download_url;
  } catch (err) {
    tab?.close();
    toast(err.message, "bad");
  }
}

async function deleteDocument(doc) {
  if (!confirm(`Delete “${doc.title}”? The PDF and its text are removed permanently.`)) return;
  try {
    await apiRequest("DELETE", docQuery(doc.document_id));
    state.texts.delete(doc.document_id);
    if (state.selectedId === doc.document_id) state.selectedId = null;
    toast(`Deleted “${doc.title}”`);
    await loadDocuments();
  } catch (err) {
    toast(err.message, "bad");
  }
}

// ---------- Upload ----------

function chooseFile(file) {
  if (!file) return;
  if (file.type !== "application/pdf") {
    toast("Only PDF files can be added.", "bad");
    return;
  }
  if (file.size > MAX_FILE_SIZE) {
    toast("This PDF is larger than 10 MB. Choose a smaller file.", "bad");
    return;
  }
  state.pendingFile = file;
  $("#fileChosen").textContent = `${file.name} (${formatSize(file.size)})`;
  $("#uploadTitle").value = file.name.replace(/\.pdf$/i, "").replace(/[_-]+/g, " ").trim();
  $("#uploadProgress").textContent = "";
  $("#uploadForm").hidden = false;
  $("#uploadTitle").focus();
}

function resetUpload() {
  state.pendingFile = null;
  $("#fileInput").value = "";
  $("#uploadForm").hidden = true;
  $("#uploadProgress").textContent = "";
}

$("#fileInput").addEventListener("change", (event) => chooseFile(event.target.files[0]));
$("#uploadCancel").addEventListener("click", resetUpload);

const dropTarget = $("#dropTarget");
["dragenter", "dragover"].forEach((type) =>
  dropTarget.addEventListener(type, (event) => {
    event.preventDefault();
    dropTarget.classList.add("is-over");
  })
);
["dragleave", "drop"].forEach((type) =>
  dropTarget.addEventListener(type, () => dropTarget.classList.remove("is-over"))
);
dropTarget.addEventListener("drop", (event) => {
  event.preventDefault();
  chooseFile(event.dataTransfer.files[0]);
});

$("#uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = state.pendingFile;
  const title = $("#uploadTitle").value.trim();
  const category = $("#uploadCategory").value.trim();
  const progress = $("#uploadProgress");
  const button = $("#uploadButton");
  if (!file) return;

  button.disabled = true;
  try {
    progress.textContent = "Preparing upload…";
    const created = await apiRequest("POST", "", {
      title,
      category,
      file_name: file.name,
      content_type: "application/pdf",
      file_size: file.size
    });

    progress.textContent = "Uploading…";
    const s3Response = await fetch(created.upload_url, {
      method: "PUT",
      headers: { "Content-Type": "application/pdf" },
      body: file
    });
    if (!s3Response.ok) throw new Error(`The upload to storage failed (HTTP ${s3Response.status}).`);

    progress.textContent = "Checking the file…";
    await apiRequest("POST", docQuery(created.document.document_id, "confirm_upload"));

    resetUpload();
    toast(`Uploaded “${title}”. Extracting the text…`);
    state.selectedId = created.document.document_id;
    state.tab = "ask";
    await loadDocuments();
  } catch (err) {
    progress.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

// ---------- Start ----------

async function initialize() {
  try {
    if (window.location.pathname === "/callback") {
      await auth.signinRedirectCallback();
      window.history.replaceState({}, "", "/");
    }
  } catch (err) {
    toast(`Sign-in failed: ${err.message}`, "bad");
  }

  const user = await auth.getUser();
  if (!user || user.expired) {
    showSignedOut();
    return;
  }
  showWorkspace(user);
  renderReader();
  await loadDocuments();
}

initialize();
