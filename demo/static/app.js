const state = {
  selectedFile: null,
  currentJob: null,
  currentResult: null,
  pollTimer: null,
  opinionFilter: "all",
  appState: "idle",
  progressSteps: [],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function cleanMarkdown(value) {
  return String(value || "").replace(/\*\*/g, "").replace(/`/g, "").trim();
}

function inlineMarkdown(value) {
  return value.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`(.+?)`/g, "<code>$1</code>");
}

function markdownToHtml(markdown) {
  if (!markdown) return '<p class="empty">暂无内容。</p>';
  const lines = escapeHtml(markdown).split("\n");
  let html = "";
  let inList = false;
  let inTable = false;
  const closeList = () => {
    if (inList) {
      html += "</ul>";
      inList = false;
    }
  };
  const closeTable = () => {
    if (inTable) {
      html += "</tbody></table>";
      inTable = false;
    }
  };

  for (const line of lines) {
    if (/^\|(.+)\|$/.test(line) && !/^\|\s*-/.test(line)) {
      closeList();
      const cells = line.slice(1, -1).split("|").map((cell) => cell.trim());
      if (!inTable) {
        html += "<table><tbody>";
        inTable = true;
      }
      html += `<tr>${cells.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`;
      continue;
    }
    if (/^\|\s*-/.test(line)) continue;
    closeTable();
    if (/^###\s+/.test(line)) {
      closeList();
      html += `<h3>${inlineMarkdown(line.replace(/^###\s+/, ""))}</h3>`;
    } else if (/^##\s+/.test(line)) {
      closeList();
      html += `<h2>${inlineMarkdown(line.replace(/^##\s+/, ""))}</h2>`;
    } else if (/^#\s+/.test(line)) {
      closeList();
      html += `<h1>${inlineMarkdown(line.replace(/^#\s+/, ""))}</h1>`;
    } else if (/^\s*&gt;\s?/.test(line)) {
      closeList();
      html += `<blockquote>${inlineMarkdown(line.replace(/^\s*&gt;\s?/, ""))}</blockquote>`;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inlineMarkdown(line.replace(/^\s*[-*]\s+/, ""))}</li>`;
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      html += `<p>${inlineMarkdown(line)}</p>`;
    }
  }
  closeList();
  closeTable();
  return html;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function formatSize(size) {
  const bytes = Number(size || 0);
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes > 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

async function uploadFile(file) {
  if (!file) return;
  setJobStatus("上传中");
  $("#uploadBox").classList.add("uploading");
  $("#uploadTitle").textContent = file.name;
  $("#uploadMeta").textContent = "正在上传文件...";
  const formData = new FormData();
  formData.append("file", file);
  try {
    const data = await fetchJson("/api/upload", {
      method: "POST",
      body: formData,
    });
    state.selectedFile = data.file;
    $("#uploadBox").classList.remove("uploading");
    $("#uploadBox").classList.add("ready");
    $("#uploadTitle").textContent = data.file.name;
    $("#uploadMeta").textContent = `${data.file.category} · ${data.file.suffix} · ${formatSize(data.file.size)}`;
    $("#projectTitle").textContent = data.file.name;
    $("#projectCategory").textContent = data.file.category;
    $("#releaseStatus").textContent = "待生成";
    $("#releaseReason").textContent = "文件已上传，点击开始生成审查意见。";
    $("#startReview").disabled = false;
    setJobStatus("等待生成");
    renderBusinessProgress();
    setAppState("uploaded");
  } catch (error) {
    $("#uploadBox").classList.remove("uploading");
    $("#uploadMeta").textContent = error.message;
    setJobStatus("上传失败");
  }
}

function openDrawer(id) {
  const drawer = $(id);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}

function closeDrawers() {
  $$(".drawer").forEach((drawer) => {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  });
}

async function startReview() {
  if (!state.selectedFile) return;
  setAppState("running");
  $("#startReview").disabled = true;
  setJobStatus("生成中");
  $("#releaseStatus").textContent = "审查生成中";
  $("#releaseReason").textContent = "正在读取招标文件、路由知识、执行重点审查并生成质量门。";
  renderBusinessProgress(null, "running");
  const job = await fetchJson("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target: state.selectedFile.path }),
  });
  state.currentJob = job;
  $("#jobIdLabel").textContent = job.id;
  pollJob(job.id);
}

async function pollJob(jobId) {
  clearTimeout(state.pollTimer);
  const job = await fetchJson(`/api/jobs/${jobId}`);
  state.currentJob = job;
  renderJob(job);
  if (job.status === "running" && job.outputDir) {
    await loadPartialResult(job.outputDir);
  }
  if (job.status === "completed" && job.outputDir) {
    await loadResult(job.outputDir);
    $("#startReview").disabled = false;
    return;
  }
  if (job.status === "failed") {
    $("#startReview").disabled = false;
    $("#releaseStatus").textContent = "生成失败";
    $("#releaseReason").textContent = "请查看技术日志或检查模型服务配置。";
    renderBusinessProgress(null, "failed");
    setAppState("failed");
    return;
  }
  state.pollTimer = setTimeout(() => pollJob(jobId), 1800);
}

function renderJob(job) {
  const label = {
    queued: "排队中",
    running: "生成中",
    completed: "已完成",
    failed: "失败",
  }[job.status] || job.status;
  setJobStatus(label);
  $("#logsContent").textContent = job.logs?.join("\n") || "暂无日志。";
}

function setJobStatus(value) {
  $("#jobStatus").textContent = value;
}

async function loadResult(outputDir) {
  const result = await fetchJson(`/api/result?outputDir=${encodeURIComponent(outputDir)}`);
  state.currentResult = result;
  renderResult(result);
}

async function loadPartialResult(outputDir) {
  const result = await fetchJson(`/api/result?outputDir=${encodeURIComponent(outputDir)}`);
  state.currentResult = result;
  renderPartialResult(result);
}

function renderResult(result) {
  $("#projectTitle").textContent = result.project || result.outputDir;
  $("#projectMethod").textContent = result.method || "--";
  $("#projectCategory").textContent = result.category || state.selectedFile?.category || "--";
  $("#projectBudget").textContent = result.budget || "--";
  $("#riskCount").textContent = result.summary.riskCount;
  $("#highRiskCount").textContent = result.summary.highRiskCount;
  $("#manualReviewCount").textContent = result.summary.manualReviewCount;
  $("#stageCount").textContent = `${result.summary.stageCompleteCount}/8`;
  renderReleaseAdvice(result.releaseAdvice);
  renderExpertModules(result.expertModules || []);
  renderOpinions(result.risks || []);
  renderNextActions(result);
  renderBusinessProgress(result);
  renderQualityDigest(result);
  renderReport(result);
  renderArtifacts(result.artifacts);
  setJobStatus("已完成");
  setAppState("completed");
}

function renderPartialResult(result) {
  $("#projectTitle").textContent = result.project || state.selectedFile?.name || "审查生成中";
  $("#projectMethod").textContent = result.method || "--";
  $("#projectCategory").textContent = result.category || state.selectedFile?.category || "--";
  $("#projectBudget").textContent = result.budget || "--";
  $("#stageCount").textContent = `${result.summary.stageCompleteCount}/8`;
  renderBusinessProgress(result);
  renderArtifacts(result.artifacts);
  if (result.artifacts?.["07"]?.content) {
    renderReport(result);
  }
  if (result.quality?.rows?.length) {
    renderQualityDigest(result);
  }
}

function setAppState(nextState) {
  state.appState = nextState;
  document.body.dataset.state = nextState;
}

function renderReleaseAdvice(advice) {
  $("#releaseStatus").textContent = advice?.status || "待生成";
  $("#releaseReason").textContent = advice?.reason || "暂无发布建议。";
  $("#decisionPanel").className = `decision-panel ${advice?.tone || ""}`;
}

function renderExpertModules(modules) {
  if (!modules.length) {
    $("#expertModules").innerHTML = `
      ${["基本合规", "公平竞争", "评分办法", "采购需求", "合同履约"].map((name) => `
        <div class="module-card idle">
          <span class="module-status">待审</span>
          <h4>${name}</h4>
          <p>审查结果生成后显示模块判断。</p>
        </div>
      `).join("")}
    `;
    return;
  }
  $("#expertModules").innerHTML = modules.map((module) => `
    <div class="module-card ${moduleStatusClass(module.status)}">
      <span class="module-status">${escapeHtml(module.status)}</span>
      <h4>${escapeHtml(module.name)}</h4>
      <p>${escapeHtml(module.scope)}</p>
      <strong>${module.riskCount} 项风险 · ${module.highRiskCount} 项高风险</strong>
      <small>${escapeHtml(module.verdict)}</small>
    </div>
  `).join("");
}

function moduleStatusClass(status) {
  if (status.includes("重大")) return "danger";
  if (status.includes("风险")) return "warning";
  if (status.includes("核验")) return "review";
  if (status.includes("异常")) return "ok";
  return "idle";
}

function renderOpinions(risks) {
  const filtered = state.opinionFilter === "all" ? risks : risks.filter((risk) => risk.disposition === state.opinionFilter);
  if (!filtered.length) {
    $("#opinionList").innerHTML = '<div class="empty-state">审查完成后展示专家整改意见。</div>';
    return;
  }
  $("#opinionList").innerHTML = filtered.map((risk) => `
    <article class="opinion-card ${dispositionClass(risk.disposition)}">
      <div class="opinion-head">
        <span>${escapeHtml(risk.disposition)}</span>
        <strong>${escapeHtml(risk.id)} · ${escapeHtml(risk.module)}</strong>
      </div>
      <h4>${escapeHtml(risk.title)}</h4>
      <div class="opinion-meta">
        <span>风险等级：${escapeHtml(risk.level || "未标注")}</span>
        <span>来源动作：${escapeHtml(risk.action || "未标注")}</span>
      </div>
      <p><b>原文依据：</b>${escapeHtml(risk.position || "未标注")}</p>
      <p><b>处理建议：</b>${escapeHtml(risk.suggestion || "详见审查报告")}</p>
      <button class="link-button" data-open-evidence>查看报告与证据</button>
    </article>
  `).join("");
}

function dispositionClass(disposition) {
  if (disposition === "必须修改") return "must";
  if (disposition === "需人工复核") return "review";
  if (disposition === "建议修改") return "suggest";
  return "notice";
}

function renderNextActions(result) {
  const must = result.risks.filter((risk) => risk.disposition === "必须修改").length;
  const review = result.risks.filter((risk) => risk.disposition === "需人工复核").length;
  const suggest = result.risks.filter((risk) => risk.disposition === "建议修改").length;
  $("#nextActions").innerHTML = `
    <div class="action-row strong"><span>必须修改</span><b>${must}</b></div>
    <div class="action-row"><span>建议修改</span><b>${suggest}</b></div>
    <div class="action-row"><span>人工复核</span><b>${review}</b></div>
    <p>${escapeHtml(result.releaseAdvice?.reason || "暂无处理建议。")}</p>
  `;
}

function defaultBusinessSteps(mode = "idle") {
  const states = mode === "running"
    ? ["running", "pending", "pending", "pending", "pending", "pending", "pending", "pending"]
    : mode === "failed"
      ? ["failed", "pending", "pending", "pending", "pending", "pending", "pending", "pending"]
      : ["pending", "pending", "pending", "pending", "pending", "pending", "pending", "pending"];
  return [
    ["01-文件画像", "抽取项目名称、采购方式、预算、品目、地域、场景标签等画像字段。"],
    ["02-知识路由表", "根据画像路由全国通用、地域、品类、章节和横向专题知识。"],
    ["03-动作清单", "把已路由知识展开为本次必须执行的审查动作。"],
    ["04-动作执行记录", "逐动作读取公告、资格、采购需求、评分办法、合同条款等章节。"],
    ["05-原子风险清单", "把候选问题拆成可单独整改、可反链证据的风险项。"],
    ["06-质量门检查表", "反查动作覆盖、证据链、风险原子化、待确认项和异常低风险数量。"],
    ["07-AI审查记录", "汇总审查摘要、风险清单、修改建议和质量门结论。"],
    ["08-运行记录", "记录中间产物、知识调用、模型交互、执行边界和复现信息。"],
  ].map(([name, focus], index) => ({ name, focus, doneText: "已完成", status: states[index], keyFacts: [] }));
}

function buildBusinessSteps(result) {
  if (!result) return defaultBusinessSteps();
  const stageDone = (code) => result.stages.some((stage) => stage.code === code && stage.status === "completed");
  const artifactText = (code) => result.artifacts?.[code]?.content || "";
  const uniqueCount = (text, pattern) => new Set([...text.matchAll(pattern)].map((match) => match[1] || match[0])).size;
  const routeText = artifactText("02");
  const actionText = artifactText("03");
  const actionExecText = artifactText("04");
  const routeCount = uniqueCount(routeText, /\[\[(wiki\/[^\]]+)\]\]/g) || (routeText.match(/wiki\//g) || []).length;
  const actionCount = uniqueCount(actionText, /\b([A-Z]+-[A-Z]?\d{2,}|PROP-A\d{2}|NMG-A\d{2}|CDQ-A\d{2}|ACT-\d{2})\b/g);
  const executedCount = uniqueCount(actionExecText, /\b([A-Z]+-[A-Z]?\d{2,}|PROP-A\d{2}|NMG-A\d{2}|CDQ-A\d{2}|ACT-\d{2})\b/g);
  const quality = result.quality || { rows: [], failed: 0, pending: 0 };
  const profileSummary = [
    result.project && `项目：${result.project}`,
    result.method && `采购方式：${result.method}`,
    result.category && `品目/属性：${result.category}`,
    result.budget && `预算/限价：${result.budget}`,
  ].filter(Boolean).join("；");
  const moduleSummary = (result.expertModules || [])
    .filter((module) => module.riskCount)
    .map((module) => `${module.name}${module.riskCount}项`)
    .join("，");
  const artifact = (code) => result.artifacts?.[code]?.path || "";
  return [
    {
      name: "01-文件画像",
      codes: ["01"],
      focus: "抽取项目名称、采购方式、预算、品目、地域、场景标签等画像字段。",
      doneText: profileSummary || "文件画像已完成，关键字段已进入审查链路。",
      keyFacts: [
        ["项目名称", result.project || "--"],
        ["采购方式", result.method || "--"],
        ["采购品目/属性", result.category || "--"],
        ["预算/限价", result.budget || "--"],
      ],
      artifact: artifact("01"),
      content: artifactText("01"),
    },
    {
      name: "02-知识路由表",
      codes: ["02"],
      focus: "根据画像路由全国通用、地域、品类、章节和横向专题知识。",
      doneText: `已路由 ${routeCount || "--"} 个知识引用，明确本次审查调用范围。`,
      keyFacts: [
        ["知识引用", routeCount || "--"],
        ["通用/地域/品类", routeText ? summarizeRoute(routeText) : "--"],
      ],
      artifact: artifact("02"),
      content: artifactText("02"),
    },
    {
      name: "03-动作清单",
      codes: ["03"],
      focus: "把已路由知识展开为本次必须执行的审查动作。",
      doneText: `已展开 ${actionCount || "--"} 个审查动作，覆盖招标文件关键章节。`,
      keyFacts: [
        ["动作数量", actionCount || "--"],
        ["专项动作", summarizeActionPackages(actionText)],
      ],
      artifact: artifact("03"),
      content: artifactText("03"),
    },
    {
      name: "04-动作执行记录",
      codes: ["04"],
      focus: "逐动作读取公告、资格、采购需求、评分办法、合同条款等章节。",
      doneText: `已执行 ${executedCount || actionCount || "--"} 个动作；重点风险分布：${moduleSummary || "未见模块风险"}。`,
      keyFacts: [
        ["已执行动作", executedCount || actionCount || "--"],
        ["风险模块", moduleSummary || "未见模块风险"],
      ],
      artifact: artifact("04"),
      content: artifactText("04"),
    },
    {
      name: "05-原子风险清单",
      codes: ["05"],
      focus: "把候选问题拆成可单独整改、可反链证据的风险项。",
      doneText: `形成 ${result.summary.riskCount} 条风险意见，其中高风险 ${result.summary.highRiskCount} 条、需人工复核 ${result.summary.manualReviewCount} 条。`,
      keyFacts: [
        ["风险总数", result.summary.riskCount],
        ["高风险", result.summary.highRiskCount],
        ["人工复核", result.summary.manualReviewCount],
      ],
      artifact: artifact("05"),
      content: artifactText("05"),
    },
    {
      name: "06-质量门检查表",
      codes: ["06"],
      focus: "反查动作覆盖、证据链、风险原子化、待确认项和异常低风险数量。",
      doneText: `质量门 ${quality.rows.length || 0} 项，未通过 ${quality.failed || 0} 项，待确认 ${quality.pending || 0} 项。`,
      keyFacts: [
        ["检查项", quality.rows.length || 0],
        ["未通过", quality.failed || 0],
        ["待确认", quality.pending || 0],
      ],
      artifact: artifact("06"),
      content: artifactText("06"),
    },
    {
      name: "07-AI审查记录",
      codes: ["07"],
      focus: "汇总审查摘要、风险清单、修改建议和质量门结论。",
      doneText: result.releaseAdvice?.reason || "AI 审查记录已生成。",
      keyFacts: [
        ["发布意见", result.releaseAdvice?.status || "--"],
        ["报告路径", artifact("07") || "--"],
      ],
      artifact: artifact("07"),
      content: artifactText("07"),
    },
    {
      name: "08-运行记录",
      codes: ["08"],
      focus: "记录中间产物、知识调用、模型交互、执行边界和复现信息。",
      doneText: "运行记录已生成，审查过程可追溯。",
      keyFacts: [
        ["证据链完整度", `${result.summary.stageCompleteCount}/8`],
        ["运行记录", artifact("08") || "--"],
      ],
      artifact: artifact("08"),
      content: artifactText("08"),
    },
  ].map((step) => {
    const { codes } = step;
    const doneCount = codes.filter((code) => stageDone(code)).length;
    const status = doneCount === codes.length ? "completed" : doneCount ? "running" : "pending";
    return { ...step, status };
  });
}

function summarizeRoute(routeText) {
  const tags = [];
  if (/全国通用|基础必读|通用/.test(routeText)) tags.push("全国通用");
  if (/地域|内蒙古|深圳|广东|福建|泉州/.test(routeText)) tags.push("地域规则");
  if (/品目|物业|家具|信息化|货物|服务/.test(routeText)) tags.push("品类/标的");
  if (/横向|差别歧视|技术要求|评分|合同/.test(routeText)) tags.push("横向专题");
  return tags.length ? tags.join("、") : "已生成路由表";
}

function summarizeActionPackages(actionText) {
  const packages = [];
  if (/PROP-A\d{2}/.test(actionText)) packages.push("物业管理 PROP");
  if (/NMG-A\d{2}/.test(actionText)) packages.push("内蒙古 NMG");
  if (/CDQ-A\d{2}/.test(actionText)) packages.push("差别歧视 CDQ");
  if (/ACT-\d{2}/.test(actionText)) packages.push("通用 ACT");
  if (/[A-Z]+-A\d{2}/.test(actionText)) packages.push("专项动作");
  return packages.length ? packages.join("、") : "按通用审查动作展开";
}

function renderBusinessProgress(result = null, mode = "idle") {
  const steps = result ? buildBusinessSteps(result) : defaultBusinessSteps(mode);
  state.progressSteps = steps;
  const completed = steps.filter((step) => step.status === "completed").length;
  const active = steps.find((step) => step.status !== "completed") || steps.at(-1);
  $("#progressSummary").textContent = `${completed}/${steps.length} 步完成 · 当前：${active.name}`;
  $("#businessProgress").innerHTML = steps.map((step, index) => `
    <button class="business-step ${step.status} ${step === active ? "selected" : ""}" data-progress-index="${index}">
      <span>${index + 1}</span>
      <div>
        <strong>${escapeHtml(step.name)}</strong>
        <p>${escapeHtml(step.status === "completed" ? step.doneText || step.focus : step.focus)}</p>
      </div>
      <em>${step.status === "completed" ? "完成" : step.status === "running" ? "生成中" : step.status === "failed" ? "失败" : "等待"}</em>
    </button>
  `).join("");
  renderProgressInspector(active);
}

function renderProgressInspector(step) {
  $("#progressInspectorTitle").textContent = step?.name || "等待审查启动";
  if (!step) {
    $("#progressInspectorBody").innerHTML = "上传文件并开始生成后，这里展示当前阶段的业务摘要。";
    return;
  }
  const facts = step.keyFacts || [];
  $("#progressInspectorBody").innerHTML = `
    <p>${escapeHtml(step.status === "completed" ? step.doneText || step.focus : step.focus)}</p>
    ${facts.length ? `
      <div class="fact-list">
        ${facts.map(([label, value]) => `
          <div>
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `).join("")}
      </div>
    ` : ""}
    ${step.artifact ? `<small>产物：${escapeHtml(step.artifact)}</small>` : ""}
    ${step.content ? `
      <details class="stage-content">
        <summary>查看该阶段产物原文</summary>
        <pre>${escapeHtml(step.content)}</pre>
      </details>
    ` : ""}
  `;
}

function renderQualityDigest(result) {
  const quality = result.quality || { rows: [], failed: 0, pending: 0 };
  if (!quality.rows.length) {
    $("#qualityDigest").innerHTML = '<p class="empty">暂无质量门结果。</p>';
    return;
  }
  const passed = quality.rows.length - quality.failed - quality.pending;
  const keyRows = quality.rows.filter((row) => /动作|证据|风险|待确认|异常低|完整/.test(`${row.item}${row.id}${row.issue}`)).slice(0, 5);
  $("#qualityDigest").innerHTML = `
    <div class="quality-score">
      <div><span>通过</span><strong>${passed}</strong></div>
      <div><span>未通过</span><strong>${quality.failed}</strong></div>
      <div><span>待确认</span><strong>${quality.pending}</strong></div>
    </div>
    <div class="quality-mini-list">
      ${keyRows.map((row) => `
        <div>
          <span>${escapeHtml(cleanMarkdown(row.result))}</span>
          <p>${escapeHtml(cleanMarkdown(row.item || row.id))}</p>
        </div>
      `).join("") || '<p class="empty">质量门已生成，未解析到重点摘要项。</p>'}
    </div>
  `;
}

function renderReport(result) {
  const artifact = result.artifacts["07"];
  $("#reportPath").textContent = artifact.path || "";
  $("#reportContent").innerHTML = markdownToHtml(artifact.content);
}

function renderArtifacts(artifacts) {
  const entries = Object.entries(artifacts);
  $("#artifactNav").innerHTML = entries.map(([code, artifact], index) => `
    <button class="${index === 0 ? "active" : ""}" data-artifact="${code}">
      ${code} · ${escapeHtml(artifact.name)}
    </button>
  `).join("");
  showArtifact(entries[0]?.[0] || "01");
}

function showArtifact(code) {
  const artifact = state.currentResult?.artifacts?.[code];
  $$("#artifactNav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.artifact === code);
  });
  $("#artifactContent").textContent = artifact?.content || "暂无内容。";
}

function activateTab(tab) {
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  const panelMap = {
    report: "#reportPanel",
    artifacts: "#artifactsPanel",
    logs: "#logsPanel",
  };
  Object.values(panelMap).forEach((selector) => $(selector).classList.remove("active-panel"));
  $(panelMap[tab]).classList.add("active-panel");
}

document.addEventListener("click", (event) => {
  const artifactButton = event.target.closest("[data-artifact]");
  if (artifactButton) showArtifact(artifactButton.dataset.artifact);

  const progressButton = event.target.closest("[data-progress-index]");
  if (progressButton) {
    const step = state.progressSteps[Number(progressButton.dataset.progressIndex)];
    $$("#businessProgress .business-step").forEach((button) => button.classList.toggle("selected", button === progressButton));
    renderProgressInspector(step);
  }

  const filterButton = event.target.closest(".filter");
  if (filterButton) {
    state.opinionFilter = filterButton.dataset.filter;
    $$(".filter").forEach((button) => button.classList.toggle("active", button === filterButton));
    renderOpinions(state.currentResult?.risks || []);
  }

  if (event.target.closest("[data-close-drawer]")) closeDrawers();
  if (event.target.closest("[data-open-evidence]")) {
    openDrawer("#evidenceDrawer");
    activateTab("report");
  }
});

$("#fileInput").addEventListener("change", (event) => uploadFile(event.target.files[0]));
$("#startReview").addEventListener("click", startReview);
$("#openEvidence").addEventListener("click", () => {
  openDrawer("#evidenceDrawer");
  activateTab("report");
});
$$(".tab").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab)));

renderExpertModules([]);
renderBusinessProgress();
setAppState("idle");
