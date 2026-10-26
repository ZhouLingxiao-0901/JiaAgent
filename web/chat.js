/* JiaAgent 网页界面逻辑。
 *
 * 只做三件事：
 *   1. 从后端取模型列表和会话列表，画在页面上
 *   2. 把用户输入发给 POST /api/chat/stream
 *   3. 逐段接收回复，边收边显示（打字机效果）
 *
 * 没有用任何前端框架，所以这里能看懂，就说明整个界面都能看懂。
 */

const API = "/api";

const state = {
  sessionId: null,     // 当前正在聊的会话 ID
  provider: null,      // 当前选的模型，null 表示用后端默认的
  providers: [],
  sending: false,      // 防止一次发两条
  tasks: [],           // 当前会话下的任务（右侧面板用）
  selectedTaskId: null,
  // 已经画过的待确认卡片编号。SSE 和轮询两条路都往这里登记，
  // 免得同一张卡片被画两遍。
  seenPendingIds: new Set(),
  emergencyStopped: false,   // 真实输入急停是否生效
  inputSessionToken: "",     // 输入控制接口的本地会话令牌（只在内存里）
  inputSessionExpiresAt: 0,  // 令牌到期的本地时间戳（毫秒）
};

const el = {
  messages: document.getElementById("messages"),
  emptyHint: document.getElementById("empty-hint"),
  composer: document.getElementById("composer"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  sessions: document.getElementById("session-list"),
  modelSelect: document.getElementById("model-select"),
  status: document.getElementById("status"),
  title: document.getElementById("chat-title"),
  newChat: document.getElementById("new-chat"),
  lookScreen: document.getElementById("look-screen"),
  emergencyStop: document.getElementById("emergency-stop"),
  visionModal: document.getElementById("vision-modal"),
  visionClose: document.getElementById("vision-close"),
  visionMeta: document.getElementById("vision-meta"),
  visionImage: document.getElementById("vision-image"),
  visionFoot: document.getElementById("vision-foot"),
};

/* ==================== 通用小工具 ==================== */

async function api(path, options) {
  const response = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = data.error || {};
    throw new Error(err.message || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

/** 取得/刷新输入控制接口的本地会话令牌。
 *
 * 令牌只存在内存里，刷新页面就重新申请。它不是用户身份认证，只是把
 * 本页面的请求和别的网站伪造的跨站请求区分开；服务端还会校验 Origin。
 */
async function ensureInputSession(force = false) {
  const now = Date.now();
  if (
    !force
    && state.inputSessionToken
    && now < state.inputSessionExpiresAt - 5000
  ) {
    return state.inputSessionToken;
  }
  const data = await api("/input/session", { method: "POST" });
  const token = data.token || "";
  if (!token) throw new Error("服务端没有返回本地会话令牌");
  state.inputSessionToken = token;
  state.inputSessionExpiresAt = now + (Number(data.expires_in_seconds) || 0) * 1000;
  return token;
}

/** 调用需要本地会话的输入控制接口。401 时清掉令牌，下一次重新申请。 */
async function inputApi(path, options = {}) {
  const token = await ensureInputSession();
  const response = await fetch(API + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Jia-Session": token,
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = data.error || {};
    if (response.status === 401) {
      state.inputSessionToken = "";
      state.inputSessionExpiresAt = 0;
    }
    throw new Error(err.message || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

/** 把模型回复里的 Markdown 记号渲染成真正的格式。
 *
 * 只支持最常用的三种：代码块、行内代码、加粗。
 * 关键在于先把 & < > 转义掉再套标签。模型输出里如果带了 HTML，
 * 不转义就会被浏览器当成真标签执行，等于自己给自己开了个注入口子。
 */
function renderMarkdown(text) {
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // 代码块先摘出来占位，免得里面的星号和反引号被后面的规则改掉
  const blocks = [];
  html = html.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) => {
    blocks.push(code.replace(/\n+$/, ""));
    return "\u0000B" + (blocks.length - 1) + "\u0000";
  });

  html = html
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");

  return html.replace(/\u0000B(\d+)\u0000/g, (_, i) => "<pre><code>" + blocks[+i] + "</code></pre>");
}

/** 往聊天区加一个气泡，返回气泡元素供后续追加文字 */
function addBubble(role, text, options) {
  const markdown = !!(options && options.markdown);

  el.emptyHint.style.display = "none";

  const row = document.createElement("div");
  row.className = "msg " + role;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (markdown) {
    bubble.innerHTML = renderMarkdown(text);
  } else {
    // 用户自己输入的内容和报错信息不走 Markdown，用 textContent 更安全
    bubble.textContent = text;
  }

  row.appendChild(bubble);
  el.messages.appendChild(row);
  scrollToBottom();
  return bubble;
}

/* ==================== 工具活动卡片 ==================== */

// 只读文件工具的执行中/结果状态文案。status 由后端 ToolResult 提供，
// 这里只负责显示；没见过的状态回退到原来的「执行完成/失败」。
const TOOL_RUNNING_LABELS = {
  read_text_file: "正在读取文件",
  search_text_files: "正在搜索文件",
  prepare_cc_handoff: "正在准备 CC 只读交接任务",
  validate_cc_result: "正在验证 CC 分析结果",
};
const TOOL_STATUS_LABELS = {
  read_ok: "读取完成",
  read_truncated: "读取完成（输出已截断）",
  search_ok: "搜索完成",
  search_truncated: "搜索完成（结果已截断）",
  path_not_allowed: "权限不足，未执行",
  no_allowed_roots: "未配置可读目录",
  sensitive_file: "文件受保护，未读取",
  file_not_found: "文件不存在",
  not_a_file: "不是普通文件",
  not_a_directory: "不是文件夹",
  binary_file: "不是文本文件",
  unsupported_encoding: "编码不支持",
  file_too_large: "文件过大",
  file_changed: "文件已变化，未返回内容",
  read_failed: "读取失败",
  invalid_argument: "参数不合法",
  rejected: "已拒绝，未执行",
  blocked: "未执行",
  cc_handoff_ready: "CC 只读交接包已准备",
  cc_disabled: "CC 交接未启用",
  cc_limit_exceeded: "超过 CC 交接限制",
  cc_snapshot_changed: "源文件已变化，未生成交接包",
  cc_snapshot_failed: "交接包生成失败",
  cc_task_not_found: "交接任务不存在",
  cc_result_not_found: "CC 结果文件不存在",
  cc_result_invalid: "CC 结果格式不合法",
  cc_result_too_large: "CC 结果文件过大",
  cc_result_changed: "CC 结果文件已变化",
  cc_package_invalid: "交接包不完整或被篡改",
  cc_result_ok: "CC 结果验证通过",
  cc_result_truncated: "CC 结果验证通过（已截断）",
  cc_result_failed: "CC 报告任务失败",
  cc_result_timeout: "CC 报告超时",
  cc_result_cancelled: "CC 报告已取消",
};

/** 往消息区插一张工具卡片，返回卡片元素 */
function addToolCard(kind) {
  el.emptyHint.style.display = "none";
  const card = document.createElement("div");
  card.className = "tool-card " + kind;
  el.messages.appendChild(card);
  scrollToBottom();
  return card;
}

/** 需要授权的卡片：显示这次要做什么，让用户点「允许 / 拒绝」 */
function addConfirmCard(action) {
  const card = addToolCard("confirm");

  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = "需要你的确认";
  card.appendChild(head);

  const summary = document.createElement("div");
  summary.className = "tool-summary";
  summary.textContent = action.summary || action.tool_label || action.tool;
  card.appendChild(summary);

  // 把关键参数摊开给用户看，确认到底要动哪里（用 textContent，不解析 HTML）
  if (action.arguments && Object.keys(action.arguments).length) {
    const detail = document.createElement("div");
    detail.className = "tool-detail";
    detail.textContent = JSON.stringify(action.arguments, null, 2);
    card.appendChild(detail);
  }

  const actions = document.createElement("div");
  actions.className = "tool-actions";
  const allow = document.createElement("button");
  allow.className = "tool-btn allow";
  allow.textContent = "允许";
  const deny = document.createElement("button");
  deny.className = "tool-btn deny";
  deny.textContent = "拒绝";

  const settle = async (approved) => {
    allow.disabled = true;
    deny.disabled = true;
    if (approved) {
      allow.textContent = "已允许，正在执行…";
      card.classList.add("ok");
    } else {
      deny.textContent = "已拒绝，未执行";
      card.classList.add("fail");
    }
    try {
      const data = await api(`/pending/${action.id}/${approved ? "approve" : "reject"}`, {
        method: "POST",
      });
      if (!data.ok) card.classList.add("expired");
    } catch {
      card.classList.add("expired");
    }
  };

  allow.addEventListener("click", () => settle(true));
  deny.addEventListener("click", () => settle(false));

  actions.appendChild(allow);
  actions.appendChild(deny);
  card.appendChild(actions);
  return card;
}

/** 需要你挑一个的卡片：工具对上了好几个候选，让你选到底要哪个。
 *
 * 候选项的 id 是服务端发来的一次性编号。我们只把它原样回传，不自己拼、
 * 也不接受别处来的编号——后端收到后会再核对一遍"这个编号是不是它自己
 * 发出去的那几个之一"。
 */
function addChoiceCard(action) {
  const card = addToolCard("confirm choose");

  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = "需要你选一个";
  card.appendChild(head);

  const question = document.createElement("div");
  question.className = "tool-summary";
  question.textContent = action.question || "有好几个都像，你要哪一个？";
  card.appendChild(question);

  const list = document.createElement("div");
  list.className = "choice-list";

  const options = action.options || [];
  for (const option of options) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "choice-item";

    const name = document.createElement("span");
    name.className = "choice-name";
    name.textContent = option.name || option.id;
    item.appendChild(name);

    const meta = document.createElement("span");
    meta.className = "choice-meta";
    meta.textContent = [option.source, option.target].filter(Boolean).join(" · ");
    item.appendChild(meta);

    item.addEventListener("click", () => {
      // 点过之后整组禁用，免得手快点两下
      for (const other of list.querySelectorAll("button")) other.disabled = true;
      item.classList.add("picked");
      settleChoice(card, action.id, option.id);
    });

    list.appendChild(item);
  }
  card.appendChild(list);

  if (!options.length) {
    const empty = document.createElement("div");
    empty.className = "tool-detail";
    empty.textContent = "（这次没有可选项）";
    card.appendChild(empty);
  }

  return card;
}

/** 把「我选了哪一个」告诉后端。编号原样回传，后端还会再核对一遍。 */
async function settleChoice(card, actionId, optionId) {
  try {
    const data = await api(`/pending/${actionId}/choose`, {
      method: "POST",
      body: JSON.stringify({ option_id: optionId }),
    });
    card.classList.add(data.ok ? "ok" : "expired");
  } catch {
    card.classList.add("expired");
  }
}

/** 工具开始执行：先挂一张「正在执行」的卡片，结果出来后再更新它。
 *  headText 只给「没有 tool_start、直接收到 tool_result」的情况用，
 *  免得权限不足/文件不存在时先闪一下「正在读取」。
 */
function addToolRunning(summary, tool, headText) {
  const card = addToolCard("running");
  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = headText
    || TOOL_RUNNING_LABELS[tool]
    || "正在执行";
  card.appendChild(head);
  const s = document.createElement("div");
  s.className = "tool-summary";
  s.textContent = summary || "";
  card.appendChild(s);
  return card;
}

function finishToolCard(card, ok, output, status) {
  if (!card) return;
  card.classList.remove("running");
  card.classList.add(ok ? "ok" : "fail");
  const head = card.querySelector(".tool-head");
  if (head) {
    head.textContent = TOOL_STATUS_LABELS[status]
      || (ok ? "执行完成" : "执行失败");
  }
  if (output) {
    const out = document.createElement("pre");
    out.className = "tool-output";
    out.textContent = output;
    card.appendChild(out);
  }
  scrollToBottom();
}

/** 历史记录里的工具活动，画成一条简单的灰卡片 */
function addHistoryToolCard(text) {
  const failed = /失败|拒绝|未执行|出错/.test(text || "");
  const card = addToolCard("history " + (failed ? "fail" : "ok"));
  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = "工具";
  card.appendChild(head);
  const s = document.createElement("div");
  s.className = "tool-summary";
  s.textContent = text;
  card.appendChild(s);
  return card;
}

function clearBubbles() {
  el.messages.querySelectorAll(".msg, .tool-card").forEach((node) => node.remove());
  el.emptyHint.style.display = "";
}

/* ==================== 会话列表 ==================== */

async function loadSessions() {
  const data = await api("/sessions");
  el.sessions.innerHTML = "";

  for (const session of data.sessions) {
    const item = document.createElement("div");
    item.className = "session" + (session.id === state.sessionId ? " active" : "");
    item.textContent = session.title || "未命名";
    item.title = "最后更新：" + session.updated_at;
    item.addEventListener("click", () => openSession(session.id, session.title));
    el.sessions.appendChild(item);
  }

  // 顺便刷新顶栏标题。不这样做的话，第一条消息发完、标题已经自动生成，
  // 顶栏却还停在"新对话"。
  const current = data.sessions.find((s) => s.id === state.sessionId);
  if (current) el.title.textContent = current.title || "对话";
}

async function openSession(id, title) {
  state.sessionId = id;
  localStorage.setItem("jiaagent.session", id);
  clearBubbles();

  // 查一下这个会话的信息：标题，以及它当初用的是哪个模型
  let session = null;
  try {
    const data = await api("/sessions");
    session = data.sessions.find((s) => s.id === id) || null;
  } catch {
    // 查不到就用兜底值，不影响下面加载历史
  }

  // 会话已经不在了（比如数据库被清空过、或者换过电脑），
  // 就清掉浏览器里存的旧编号，回到"新对话"状态。
  // 不这样做的话，页面会一直拿一个不存在的编号去发消息，每条都报错。
  if (!session) {
    localStorage.removeItem("jiaagent.session");
    state.sessionId = null;
    el.title.textContent = "新对话";
    await loadSessions();
    return;
  }

  el.title.textContent = title || session.title || "对话";

  // 切回这个会话上次用的模型（前提是它现在还能用）
  if (session && session.model) {
    const usable = state.providers.some((p) => p.name === session.model && p.key_ready);
    if (usable) {
      el.modelSelect.value = session.model;
      state.provider = session.model;
    }
  }

  try {
    const data = await api(`/sessions/${id}/messages?include_tool=1`);
    for (const message of data.messages) {
      if (message.role === "tool") {
        addHistoryToolCard(message.content);
      } else {
        addBubble(message.role, message.content, { markdown: message.role === "assistant" });
      }
    }
  } catch (err) {
    // 会话在数据库里已经不存在了（比如换过数据库），清掉本地记录
    localStorage.removeItem("jiaagent.session");
    state.sessionId = null;
    addBubble("error", "读取历史失败：" + err.message);
  }
  await loadSessions();
}

function newChat() {
  state.sessionId = null;
  localStorage.removeItem("jiaagent.session");
  el.title.textContent = "新对话";
  clearBubbles();
  loadSessions();
  el.input.focus();
}

/* ==================== 模型选择 ==================== */

function renderModels() {
  el.modelSelect.innerHTML = "";

  const none = document.createElement("option");
  none.value = "";
  none.textContent = "默认模型";
  el.modelSelect.appendChild(none);

  for (const provider of state.providers) {
    const option = document.createElement("option");
    option.value = provider.name;
    option.textContent = `${provider.label} · ${provider.model}`
      + (provider.key_ready ? "" : "（缺 Key）");
    option.disabled = !provider.key_ready;
    el.modelSelect.appendChild(option);
  }

  // 优先选默认模型；默认模型没配 Key 就退回第一个能用的
  const preferred =
    state.providers.find((p) => p.is_default && p.key_ready) ||
    state.providers.find((p) => p.key_ready);
  el.modelSelect.value = preferred ? preferred.name : "";
}

/* ==================== 发送消息 ==================== */

async function send(text) {
  if (state.sending) return;
  state.sending = true;
  el.send.disabled = true;

  addBubble("user", text);
  el.input.value = "";
  autoGrow();

  try {
    const response = await fetch(API + "/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        session_id: state.sessionId,
        provider: state.provider,
      }),
    });

    // 出错时后端返回的是普通 JSON（比如"没有配 API Key"）
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      const err = data.error || {};
      throw new Error(err.message || `请求失败（HTTP ${response.status}）`);
    }

    await readStream(response);
  } catch (err) {
    addBubble("error", "出错了：" + err.message);
  } finally {
    state.sending = false;
    el.send.disabled = false;
    el.input.focus();
  }
}

/** 读取 SSE 流：文字边收边显示，工具调用和授权请求画成卡片。 */
async function readStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  let textBubble = null;   // 当前正在写的助手气泡
  let textRaw = "";        // 这个气泡累积的原始文字（整段重渲染用）
  let toolCard = null;     // 当前正在执行的工具卡片

  const beginText = () => {
    if (!textBubble) {
      textBubble = addBubble("assistant", "", { markdown: true });
      textBubble.classList.add("typing");
      textRaw = "";
    }
    return textBubble;
  };
  const appendText = (t) => {
    const b = beginText();
    b.classList.remove("typing");
    textRaw += t;
    b.innerHTML = renderMarkdown(textRaw);
    scrollToBottom();
  };
  // 工具/授权卡片要插在文字中间，先把当前文字气泡"收尾"，
  // 下一个 delta 会另起一个气泡。
  const closeText = () => {
    if (textBubble && !textRaw) {
      textBubble.parentElement.remove();
    }
    textBubble = null;
    textRaw = "";
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE 用空行分隔每一帧；最后一段可能不完整，留到下一轮再拼
    const frames = buffer.split("\n\n");
    buffer = frames.pop();

    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith("data:")) continue;

      let event;
      try {
        event = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }

      if (event.type === "start") {
        // 后端新建了会话，记下 ID 并刷新左侧列表
        if (event.session_id && event.session_id !== state.sessionId) {
          state.sessionId = event.session_id;
          localStorage.setItem("jiaagent.session", event.session_id);
          loadSessions();
        }
      } else if (event.type === "delta") {
        appendText(event.text);
      } else if (event.type === "need_confirm") {
        closeText();
        // 登记一下，免得待办轮询把同一张卡片又画一遍
        markPendingSeen(event.action.id);
        // kind=choose：工具对上了好几个候选，先让用户挑一个；
        // 否则就是普通的「允许 / 拒绝」卡片。
        if (event.action.kind === "choose") {
          addChoiceCard(event.action);
        } else {
          addConfirmCard(event.action);
        }
      } else if (event.type === "workflow_started") {
        closeText();
        handleWorkflowStarted(event.task);
      } else if (event.type === "tool_start") {
        closeText();
        toolCard = addToolRunning(event.summary, event.tool);
      } else if (event.type === "tool_result") {
        // 被闸门拦下（权限不足/文件不存在/用户拒绝）时没有 tool_start，
        // 但结果也要显示出来，不能因为 toolCard 为空就悄悄丢掉。
        if (!toolCard) {
          toolCard = addToolRunning(
            event.summary || event.tool || "工具", event.tool, "执行结果"
          );
        }
        finishToolCard(toolCard, event.ok, event.output, event.status);
        toolCard = null;
      } else if (event.type === "done") {
        closeText();
        loadSessions();      // 标题可能刚被自动生成，刷新一下
      } else if (event.type === "error") {
        closeText();
        addBubble("error", "出错了：" + event.message
          + (event.detail ? "\n" + event.detail : ""));
      }
      // "final" 和别的不认识的类型都忽略：文字已经通过 delta 发完了
    }
  }
  closeText();
}

/* ==================== 任务面板 ====================
 *
 * 工作流任务跑在服务端后台，不绑在这条 SSE 连接上，所以进度得靠**轮询**。
 * 这样刷新页面也不会丢——重新进来先拉一次就恢复了。
 *
 * 另外这里还有一个**确认卡片的轮询**。原因：工作流的授权请求不在当前这条
 * SSE 流里，只靠 SSE 是永远看不到的。加上它之后，"刷新页面卡片就没了"
 * 这个老毛病也顺带修好了。
 */

const TASK_POLL_MS = 1500;
const PENDING_POLL_MS = 2000;

/** 已经画过的待办编号。SSE 和轮询两条路都往这里登记，免得同一张卡片画两遍。 */
function markPendingSeen(id) {
  if (id) state.seenPendingIds.add(id);
}

/** 上一次画出来的数据的指纹。用来判断"要不要真的重画"。 */
let lastTaskSignature = "";

/** 拉当前会话的任务列表 + 选中任务的详情，画到右侧面板上。
 *
 * **只在数据真的变了的时候才重建 DOM。** 面板每 1.5 秒轮询一次，如果每次都
 * 整体重建，用户正好点下去的那一刻按钮可能刚被换掉，那一下就点空了；
 * 而且 hover / 焦点状态每次都会被清掉。所以先比指纹，一样就直接返回。
 */
async function pollTasks() {
  if (!state.sessionId) return;

  let tasks = [];
  try {
    const data = await api(`/tasks?session_id=${encodeURIComponent(state.sessionId)}`);
    tasks = data.tasks || [];
  } catch {
    return;   // 拉不到就保持上一次的样子，别在界面上刷错误
  }
  state.tasks = tasks;

  const panel = document.getElementById("task-panel");
  if (!tasks.length) {
    panel.hidden = true;
    lastTaskSignature = "";
    return;
  }
  panel.hidden = false;

  if (!state.selectedTaskId || !tasks.some((t) => t.id === state.selectedTaskId)) {
    state.selectedTaskId = tasks[0].id;
    lastTaskSignature = "";     // 换了任务，必须重画
  }

  let detail = null;
  try {
    detail = (await api(`/tasks/${state.selectedTaskId}`)).task;
  } catch {
    /* 详情拉不到就先不更新，列表照画 */
  }

  const signature = JSON.stringify([tasks, detail]);
  if (signature === lastTaskSignature) return;
  lastTaskSignature = signature;

  renderTaskList();
  if (detail) renderTaskDetail(detail);
}

function renderTaskList() {
  const box = document.getElementById("task-list");
  box.innerHTML = "";
  for (const task of state.tasks) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "task-item" + (task.id === state.selectedTaskId ? " active" : "");
    item.textContent = task.title || task.goal;

    const badge = document.createElement("span");
    badge.className = "task-badge task-badge-" + task.status + (task.paused ? " paused" : "");
    badge.textContent = task.paused ? "已暂停" : task.status_label;
    item.appendChild(badge);

    item.addEventListener("click", async () => {
      state.selectedTaskId = task.id;
      lastTaskSignature = "";     // 换任务了，强制重画
      await pollTasks();
    });
    box.appendChild(item);
  }
}

function renderTaskDetail(task) {
  const box = document.getElementById("task-detail");
  box.innerHTML = "";

  const title = document.createElement("div");
  title.className = "task-detail-title";
  title.textContent = task.title || task.goal;
  box.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "task-detail-meta";
  meta.textContent = task.paused ? "已暂停" : task.status_label;
  if (task.summary) meta.textContent += " · " + task.summary.slice(0, 80);
  if (task.error) meta.textContent += " · " + task.error.slice(0, 80);
  box.appendChild(meta);

  // ---- 控制按钮 ----
  const actions = document.createElement("div");
  actions.className = "task-actions";
  const add = (label, action, cls) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tool-btn " + (cls || "");
    btn.textContent = label;
    btn.addEventListener("click", () => taskControl(task.id, action));
    actions.appendChild(btn);
  };

  if (task.status === "pending") {
    add("开始执行", "approve", "allow");
    add("取消", "cancel", "deny");
  } else if (task.status === "running" || task.status === "waiting_confirmation") {
    if (task.paused) add("继续", "resume");
    else add("暂停", "pause");
    add("取消", "cancel", "deny");
  } else {
    add("删除记录", "delete", "deny");
  }
  box.appendChild(actions);

  if (task.status === "waiting_confirmation") {
    const hint = document.createElement("div");
    hint.className = "task-hint";
    hint.textContent = "它正在等你确认某个操作，看聊天区里的卡片。";
    box.appendChild(hint);
  }

  // ---- 步骤 ----
  const steps = document.createElement("div");
  steps.className = "task-steps";
  for (const step of task.steps || []) {
    const row = document.createElement("div");
    row.className = "task-step step-" + step.status;

    const goal = document.createElement("div");
    goal.className = "task-step-goal";
    goal.textContent = `${step.step_index + 1}. ${step.goal}`;
    row.appendChild(goal);

    const detail = document.createElement("div");
    detail.className = "task-step-meta";
    detail.textContent = [step.status_label, step.tool, step.error || step.result]
      .filter(Boolean).join(" · ").slice(0, 140);
    row.appendChild(detail);

    steps.appendChild(row);
  }
  if (task.steps && task.steps.length) box.appendChild(steps);
}

async function taskControl(taskId, action) {
  const method = action === "delete" ? "DELETE" : "POST";
  const path = action === "delete" ? `/tasks/${taskId}` : `/tasks/${taskId}/${action}`;
  try {
    const data = await api(path, { method });
    if (data && data.ok === false) {
      console.warn("任务操作被拒：" + (data.error || ""));
    }
    if (data && data.note) console.info(data.note);
  } catch (err) {
    console.warn("任务操作失败：" + err.message);
  }
  lastTaskSignature = "";   // 刚操作过，强制重画一次
  await pollTasks();
}

/** 轮询待确认的动作。
 *
 * 为什么要它：工作流的授权请求是**服务端后台**发出来的，不在当前这条 SSE
 * 流里，只靠 SSE 永远看不到。顺带也修好了"刷新页面确认卡片就没了"。
 */
async function pollPending() {
  if (!state.sessionId) return;
  let actions = [];
  try {
    const data = await api(`/pending?session_id=${encodeURIComponent(state.sessionId)}`);
    actions = data.actions || [];
  } catch {
    return;
  }

  for (const action of actions) {
    if (state.seenPendingIds.has(action.id)) continue;
    markPendingSeen(action.id);
    if (action.kind === "choose") addChoiceCard(action);
    else addConfirmCard(action);
  }
}

/** 服务端说"这句话我当成一个多步任务了"，把面板打开。 */
function handleWorkflowStarted(task) {
  state.selectedTaskId = task.id;
  const panel = document.getElementById("task-panel");
  panel.hidden = false;

  const card = addToolCard("running");
  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = "当成一个任务来做";
  card.appendChild(head);
  const body = document.createElement("div");
  body.className = "tool-summary";
  body.textContent = "我先把这件事拆成了几步，你看右边面板，确认之后我就开始执行。";
  card.appendChild(body);

  pollTasks();
}

/* ==================== 屏幕预览（阶段 0） ==================== */

/* 只在你自己点「看屏幕」时才调用。截一张图、显示出来、把坐标一并列清楚。
 * 阶段 0 没有鼠标键盘动作，所以这里没有任何"操作"入口。 */
async function openVisionPreview() {
  el.visionModal.hidden = false;
  el.visionMeta.textContent = "正在截屏……";
  el.visionImage.removeAttribute("src");
  el.visionFoot.textContent = "阶段 0：只看，不动。鼠标和键盘一个动作都没发。";

  try {
    const data = await api("/vision/snapshot?annotate=1");
    el.visionImage.src = data.image;

    const m = data.metrics;
    const fg = data.foreground;
    const p = data.preview;
    const consistent =
      m.capture_matches_metrics === true ? "是"
      : m.capture_matches_metrics === false ? "否（坐标会错位）"
      : "未核对";

    const lines = [
      `屏幕：${m.virtual[2]}×${m.virtual[3]} 物理像素，DPI ${m.dpi}（缩放 ${m.scale}）`,
      `DPI 感知：${m.awareness}（${m.awareness_method}）`,
      `截图与系统度量一致：${consistent}`,
      `预览：${p.size[0]}×${p.size[1]}（原图 ${p.original_size[0]}×${p.original_size[1]}，缩放 ${p.scale}）`,
    ];

    if (fg) {
      lines.push(`前台窗口：${fg.title || "（无标题）"}`);
      lines.push(`　进程：${fg.process || "（拿不到）"}（PID ${fg.pid}）`);
      lines.push(`　矩形：left=${fg.rect[0]} top=${fg.rect[1]} right=${fg.rect[2]} bottom=${fg.rect[3]}（${fg.rect_source}）`);
    } else {
      lines.push("前台窗口：（当前没有）");
    }
    if (p.note) lines.push(`备注：${p.note}`);
    lines.push(`截图落盘：${data.persisted ? "是" : "否（只在内存里）"}`);

    el.visionMeta.textContent = lines.join("\n");
  } catch (err) {
    el.visionImage.removeAttribute("src");
    el.visionMeta.textContent = "截图失败：" + err.message;
  }
}

function closeVisionPreview() {
  el.visionModal.hidden = true;
  el.visionImage.removeAttribute("src");
}

/* ==================== 真实输入：急停 ==================== */

/** 刷新急停和真实输入状态。只读，不会改变任何开关。 */
async function refreshInputStatus() {
  try {
    const data = await inputApi("/input/status");
    applyInputStatus(data);
    return data;
  } catch {
    // 后端没起来时不要让按钮误显示成"已急停"。
    return null;
  }
}

function applyInputStatus(data) {
  const stopped = !!(data && data.emergency && data.emergency.stopped);
  const active = !!(data && data.real_mouse && data.real_mouse.real_input_active);
  state.emergencyStopped = stopped;
  el.emergencyStop.classList.toggle("stopped", stopped);
  el.emergencyStop.textContent = stopped ? "已急停 · 恢复" : "急停";
  el.emergencyStop.title = stopped
    ? "急停生效中。点击后需要再次确认才能恢复；恢复不会自动启用真实输入。"
    : active
      ? "真实输入已启用。点击立即停止所有尚未执行的真实输入。"
      : "真实输入当前未启用；急停仍会阻止任何后续真实输入。";
}

async function onEmergencyClick() {
  if (!state.emergencyStopped) {
    // 急停本身不需要确认：它只会减少能力，越早生效越好。
    try {
      const data = await api("/input/emergency-stop", { method: "POST" });
      applyInputStatus({ emergency: data.emergency, real_mouse: {} });
      addBubble(
        "assistant",
        "已急停。所有尚未执行的真实鼠标输入都会被拒绝；恢复需要你再次确认。"
      );
    } catch (err) {
      addBubble("error", "急停失败：" + err.message);
    }
    return;
  }

  // 恢复必须显式确认。服务端不只认 confirmed：还要用这个会话换取一次性
  // 挑战，挑战短时有效、只能用一次，并与当前急停状态绑定。
  // 恢复急停不等于启用真实输入，双重条件仍然有效。
  if (!window.confirm(
    "确认恢复真实鼠标输入吗？\n\n"
    + "恢复不会自动启用真实输入；只有配置和环境变量同时满足时，"
    + "后端才会执行真实点击。"
  )) {
    return;
  }
  try {
    const prepared = await inputApi("/input/emergency-reset/prepare", {
      method: "POST",
    });
    await inputApi("/input/emergency-reset", {
      method: "POST",
      body: JSON.stringify({ confirmed: true, nonce: prepared.nonce }),
    });
    await refreshInputStatus();
    addBubble("assistant", "急停已恢复。真实输入是否可用仍取决于启用条件。");
  } catch (err) {
    addBubble("error", "恢复失败：" + err.message);
  }
}

/* ==================== 输入框行为 ==================== */

function autoGrow() {
  el.input.style.height = "auto";
  el.input.style.height = Math.min(el.input.scrollHeight, 180) + "px";
}

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = el.input.value.trim();
  if (!text || state.sending) return;
  send(text);
});

el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el.composer.requestSubmit();
  }
});

el.input.addEventListener("input", autoGrow);
el.newChat.addEventListener("click", newChat);

el.lookScreen.addEventListener("click", openVisionPreview);
el.emergencyStop.addEventListener("click", onEmergencyClick);
el.visionClose.addEventListener("click", closeVisionPreview);
el.visionModal.addEventListener("click", (event) => {
  if (event.target === el.visionModal) closeVisionPreview();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !el.visionModal.hidden) closeVisionPreview();
});
el.modelSelect.addEventListener("change", () => {
  state.provider = el.modelSelect.value || null;
});

/* ==================== 启动 ==================== */

async function init() {
  try {
    const data = await api("/models");
    state.providers = data.providers.filter((p) => p.enabled && p.supported);
    renderModels();

    const ready = state.providers.filter((p) => p.key_ready);
    if (ready.length) {
      el.status.textContent = "已就绪：" + ready.map((p) => p.label).join("、");
    } else {
      el.status.textContent = "还没有配置任何 API Key，请编辑项目根目录的 .env 后重启";
      el.status.classList.add("warn");
    }
  } catch (err) {
    el.status.textContent = "读取模型配置失败：" + err.message;
    el.status.classList.add("warn");
  }

  await loadSessions().catch(() => {});

  // 急停状态来自服务端；如果存在持久化的急停，按钮必须一打开就是"已急停"。
  await refreshInputStatus();

  const last = localStorage.getItem("jiaagent.session");
  if (last) {
    await openSession(last, null);
  }

  // 任务进度和待确认的卡片都靠轮询——它们来自服务端后台，不在这条 SSE 流里。
  // 边跑边拉，刷新页面也不丢。
  await pollPending();
  await pollTasks();
  setInterval(pollPending, PENDING_POLL_MS);
  setInterval(pollTasks, TASK_POLL_MS);

  el.input.focus();
}

init();
