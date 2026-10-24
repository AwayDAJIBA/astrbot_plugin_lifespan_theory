/* 寿命论 · 年报模板管理页（原生 JS，无构建依赖、无 CDN，离线可用）
 *
 * 页面运行在 AstrBot WebUI 的受限 iframe 里：
 * - 不能直接访问 dashboard 的 cookie / localStorage；
 * - 所有后端调用必须走 window.AstrBotPluginPage bridge；
 * - sandbox 没有 allow-modals，所以原生 confirm/alert 会被浏览器吞掉 —— 确认框自己画。
 */
(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage || window.AstrBotPluginView || null;
  var ENDPOINT = {
    list: "yearbook/templates",
    save: "yearbook/templates",
    importFile: "yearbook/templates/import",
    remove: "yearbook/templates/delete",
    preview: "yearbook/preview"
  };
  var DEFAULT_NAME = "my-yearbook";
  // 与后端 webui.safe_template_name / yearbook.TemplateStore 一致：不含扩展名、不含点
  var TEMPLATE_NAME_RE = /^[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}$/;
  var MAX_TEMPLATE_CHARS = 512 * 1024;
  var MAX_IMPORT_BYTES = 4 * 1024 * 1024;

  var state = {
    templates: [],
    requiredVars: [],
    sampleVars: [],
    current: null, // {name, source, title, updated_at, html}
    dirty: false,
    busy: false,
    context: null
  };

  var el = {};
  var toastTimer = null;
  var confirmResolve = null;

  /* ------------------------------------------------------------ 小工具 */
  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function toast(message, isError) {
    if (!el.toast) return;
    el.toast.textContent = message;
    el.toast.className = "toast" + (isError ? " err" : "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.className = "toast hidden";
    }, isError ? 4200 : 2600);
  }

  function showError(message) {
    if (!el.error) return;
    el.error.textContent = message;
    el.error.classList.remove("hidden");
  }

  function hideError() {
    if (el.error) el.error.classList.add("hidden");
  }

  function setBusy(flag) {
    state.busy = !!flag;
    [
      el.btnSave,
      el.btnPreview,
      el.btnImport,
      el.btnNew,
      el.btnRefresh,
      el.btnDelete,
      el.btnDownload
    ].forEach(function (button) {
      if (!button) return;
      if (button === el.btnDelete) {
        button.disabled = state.busy || !canDelete();
      } else {
        button.disabled = state.busy;
      }
    });
  }

  function canDelete() {
    return !!(state.current && state.current.source !== "builtin");
  }

  function withRequest(promise) {
    setBusy(true);
    return promise.finally(function () {
      setBusy(false);
    });
  }

  function fmtTime(value) {
    if (!value) return "";
    var text = String(value);
    // TemplateStore 可能给 ISO 时间或文件 mtime 字符串；只做轻量美化
    var match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    return match ? match[1] + " " + match[2] : text;
  }

  function fmtSize(chars) {
    var n = Number(chars) || 0;
    if (n < 1024) return n + " 字";
    return (n / 1024).toFixed(1) + " KB";
  }

  function lineCount(text) {
    return String(text || "").split("\n").length;
  }

  function apiGet(endpoint, params) {
    if (!bridge) return Promise.reject(new Error("页面 bridge 未就绪"));
    return bridge.apiGet(endpoint, params || {});
  }

  function apiPost(endpoint, body) {
    if (!bridge) return Promise.reject(new Error("页面 bridge 未就绪"));
    return bridge.apiPost(endpoint, body || {});
  }

  function errorMessage(error) {
    if (!error) return "未知错误";
    if (typeof error === "string") return error;
    return error.message || error.error || String(error);
  }

  /* ------------------------------------------------------------ toast/确认框 */
  function showConfirm(message, title, okText) {
    if (!el.confirmModal) return Promise.resolve(false);
    el.confirmTitle.textContent = title || "确认";
    el.confirmMessage.textContent = message || "";
    el.confirmOk.textContent = okText || "确定";
    el.confirmModal.classList.remove("hidden");
    return new Promise(function (resolve) {
      confirmResolve = resolve;
      el.confirmOk.focus();
    });
  }

  function closeConfirm(result) {
    if (!el.confirmModal) return;
    el.confirmModal.classList.add("hidden");
    if (confirmResolve) {
      confirmResolve(!!result);
      confirmResolve = null;
    }
  }

  /* ------------------------------------------------------------ 渲染 */
  function renderChips() {
    if (!el.statusChips) return;
    var userCount = state.templates.filter(function (item) {
      return item.source === "user";
    }).length;
    var ctx = state.context || {};
    var chips = [
      "模板 " + esc(state.templates.length) + " 个",
      "用户 " + esc(userCount) + " / 内置 " + esc(state.templates.length - userCount),
      "必需变量 " + esc(state.requiredVars.length) + " 个"
    ];
    if (ctx.locale) chips.push("lang " + esc(ctx.locale));
    if (ctx.isDark) chips.push("深色主题");
    el.statusChips.innerHTML = chips
      .map(function (text) {
        return '<span class="chip">' + text + "</span>";
      })
      .join("");
  }

  function renderList() {
    if (!el.templateList) return;
    if (!state.templates.length) {
      el.templateList.innerHTML = '<div class="empty">还没有模板。点「新建」开始写一个吧。</div>';
    } else {
      el.templateList.innerHTML = state.templates
        .map(function (item) {
          var name = String(item.name || "");
          var title = String(item.title || "");
          var desc = String(item.desc || "");
          var source = item.source === "builtin" ? "builtin" : "user";
          var sourceText = source === "builtin" ? "内置" : "用户";
          var active = state.current && state.current.name === name ? " active" : "";
          var updated = fmtTime(item.updated_at);
          return (
            '<button type="button" class="template-item' +
            active +
            '" data-name="' +
            esc(name) +
            '" title="' +
            esc(name + (desc ? "：" + desc : "")) +
            '">' +
            '<span class="row"><span class="tname">' +
            esc(name) +
            '</span><span class="badge ' +
            source +
            '">' +
            sourceText +
            "</span></span>" +
            (title && title !== name
              ? '<span class="ttitle">' + esc(title) + "</span>"
              : "") +
            (desc ? '<span class="ttitle">' + esc(desc) + "</span>" : "") +
            (updated ? '<span class="ttitle">更新于 ' + esc(updated) + "</span>" : "") +
            "</button>"
          );
        })
        .join("");
    }
    var count = state.templates.length;
    if (el.templateCount) el.templateCount.textContent = count + " 个";
    Array.prototype.forEach.call(
      el.templateList.querySelectorAll(".template-item"),
      function (node) {
        node.addEventListener("click", function () {
          selectTemplate(node.getAttribute("data-name"));
        });
      }
    );
  }

  function renderVarChips(htmlText) {
    if (!el.varChips) return;
    var used = templateVars(htmlText || "");
    var required = state.requiredVars;
    if (!required.length && !state.sampleVars.length) {
      el.varChips.innerHTML = '<span class="muted">（模板仓库没有声明必需变量）</span>';
      return;
    }
    var chips = required.map(function (name) {
      var missing = used.indexOf(name) === -1 ? " missing" : "";
      return '<span class="var-chip' + missing + '" title="' +
        (missing ? "模板里没有引用这个必需变量" : "模板已引用") + '">' + esc(name) + "</span>";
    });
    el.varChips.innerHTML = chips.join("");
  }

  function templateVars(htmlText) {
    var text = String(htmlText || "");
    var found = [];
    var re = /{[{%]\s*([A-Za-z_][A-Za-z0-9_]*)/g;
    var match;
    while ((match = re.exec(text)) !== null) {
      if (found.indexOf(match[1]) === -1) found.push(match[1]);
    }
    return found;
  }

  function renderEditorMeta() {
    if (!el.editorMeta) return;
    if (!state.current) {
      el.editorMeta.innerHTML = '<span class="chip warn">新模板（未保存）</span>';
      return;
    }
    var source = state.current.source === "builtin" ? "内置" : "用户";
    var chips = ['<span class="chip">' + esc(source) + "模板</span>"];
    if (state.current.updated_at) {
      chips.push('<span class="chip">更新 ' + esc(fmtTime(state.current.updated_at)) + "</span>");
    }
    var chars = (el.html.value || "").length;
    chips.push('<span class="chip">' + esc(fmtSize(chars)) + "</span>");
    el.editorMeta.innerHTML = chips.join("");
  }

  function renderGutter() {
    if (!el.gutter) return;
    var count = lineCount(el.html.value);
    var lines = [];
    for (var i = 1; i <= count; i += 1) lines.push(i);
    el.gutter.textContent = lines.join("\n");
  }

  function renderDirty() {
    if (!el.dirtyFlag) return;
    var parts = [];
    if (state.dirty) parts.push("● 有未保存修改");
    if (state.current) {
      parts.push("来源：" + (state.current.source === "builtin" ? "内置模板（保存会生成用户覆盖版本）" : "用户模板"));
    } else {
      parts.push("新模板：保存后出现在左侧列表");
    }
    el.dirtyFlag.textContent = parts.join("　|　");
    renderEditorMeta();
  }

  function setEditor(htmlText, name) {
    el.html.value = htmlText || "";
    el.name.value = name || "";
    state.dirty = false;
    renderGutter();
    renderVarChips(el.html.value);
    renderDirty();
  }

  function clearEditorForNew() {
    state.current = null;
    setEditor(makeStarter(), uniqueNewName());
    renderList();
  }

  function uniqueNewName() {
    var base = DEFAULT_NAME;
    var existing = {};
    state.templates.forEach(function (item) {
      existing[item.name] = true;
    });
    if (!existing[base]) return base;
    var stem = base.replace(/\.html?$/i, "");
    for (var i = 2; i < 1000; i += 1) {
      var candidate = stem + "-" + i + ".html";
      if (!existing[candidate]) return candidate;
    }
    return "template-" + Date.now() + ".html";
  }

  function makeStarter() {
    var required = state.requiredVars.length
      ? state.requiredVars.slice()
      : ["title", "character_name", "age", "year", "entries", "stats"];
    // 后端保存时会校验「必需变量必须真的被模板引用」：常用变量写进正文，
    // 其余必需变量塞进隐藏区（{{ 变量 }} 不算注释，能被校验识别到）。
    var handled = {
      title: true,
      character_name: true,
      age: true,
      year: true,
      entries: true,
      stats: true
    };
    var hiddenVars = required.filter(function (name) {
      return !handled[name];
    });
    var lines = [
      "<!DOCTYPE html>",
      '<html lang="zh-CN">',
      "<head>",
      '  <meta charset="UTF-8" />',
      "  <title>{{ title }}</title>",
      "  <style>",
      "    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; color: #2f2a24; }",
      "    h1 { letter-spacing: 0.08em; }",
      "    .muted { color: #837a6c; font-size: 13px; }",
      "    .entry { border-bottom: 1px dashed #ddd3c2; padding: 8px 0; }",
      "  </style>",
      "</head>",
      "<body>",
      "  <h1>{{ title }}</h1>",
      '  <p class="muted">{{ character_name }} · {{ age }} 岁 · {{ year }}</p>',
      '  <p class="muted">共 {{ stats.total }} 条记录</p>',
      "  {% for entry in entries %}",
      '  <div class="entry">',
      '    <div class="muted">{{ entry.date_text }} · {{ entry.kind_label }}</div>',
      "    <div>{{ entry.text }}</div>",
      "  </div>",
      "  {% endfor %}",
      "  {% for m in media %}",
      '  <figure><img src="{{ m.src }}" alt="{{ m.caption }}" style="max-width:100%" /></figure>',
      "  {% endfor %}",
      hiddenVars.length
        ? '  <div hidden style="display:none">' +
          hiddenVars
            .map(function (name) {
              return "{{ " + name + " }}";
            })
            .join(" ") +
          "</div>"
        : "",
      "  <!-- 本模板引用的变量：" + required.join("、") + " -->",
      "</body>",
      "</html>",
      ""
    ];
    return lines.filter(function (line) { return line !== ""; }).join("\n");
  }

  /* ------------------------------------------------------------ 数据操作 */
  function loadContext() {
    if (!bridge) return Promise.resolve(null);
    var ready = bridge.ready ? bridge.ready() : Promise.resolve(null);
    // 页面被单独打开/父页面异常时 ready 永远不会 resolve，不能卡死在初始化
    var timeout = new Promise(function (resolve) {
      setTimeout(function () {
        resolve(null);
      }, 1800);
    });
    return Promise.race([ready, timeout]).then(function (context) {
      state.context = context || null;
      applyContext(state.context);
      return state.context;
    });
  }

  function applyContext(context) {
    if (!context) return;
    if (context.pluginName && el.pluginName) el.pluginName.textContent = context.pluginName;
    if (context.pageName && el.pageName) el.pageName.textContent = context.pageName;
    if (el.pageTitle) {
      el.pageTitle.textContent = bridge.t
        ? bridge.t("pages.yearbook.title", "年报模板")
        : "年报模板";
    }
    if (context.displayName && el.pageTitle && context.pageTitle) {
      el.pageTitle.textContent = context.pageTitle;
    }
    renderChips();
  }

  function loadList(options) {
    options = options || {};
    hideError();
    return apiGet(ENDPOINT.list)
      .then(function (data) {
        state.templates = Array.isArray(data.templates) ? data.templates : [];
        state.requiredVars = Array.isArray(data.required_vars) ? data.required_vars : [];
        state.sampleVars = Array.isArray(data.sample_vars) ? data.sample_vars : [];
        renderChips();
        renderList();
        if (options.select) {
          return selectTemplate(options.select, true);
        }
        if (!state.current && state.templates.length) {
          return selectTemplate(state.templates[0].name, true);
        }
        if (!state.templates.length) {
          clearEditorForNew();
        }
        return null;
      })
      .catch(function (error) {
        showError("读取模板列表失败：" + errorMessage(error));
        renderList();
      });
  }

  function selectTemplate(name, force) {
    var apply = function () {
      return apiGet(ENDPOINT.list + "/" + name).then(function (data) {
        state.current = {
          name: data.name || name,
          source: data.source || "",
          title: data.title || "",
          updated_at: data.updated_at || "",
          html: data.html || ""
        };
        setEditor(state.current.html, state.current.name);
        renderList();
        return data;
      });
    };
    if (!force && state.dirty) {
      return showConfirm("当前模板有未保存的修改，切换会丢弃它们。继续？", "放弃修改", "继续切换").then(
        function (ok) {
          return ok ? apply() : null;
        }
      );
    }
    return withRequest(apply()).catch(function (error) {
      showError("读取模板「" + name + "」失败：" + errorMessage(error));
    });
  }

  function saveCurrent() {
    var name = (el.name.value || "").trim();
    var html = el.html.value || "";
    if (!name) {
      toast("先给模板起个名字", true);
      el.name.focus();
      return Promise.resolve();
    }
    if (!TEMPLATE_NAME_RE.test(name)) {
      toast("模板名不合法：只能含中文/字母/数字/下划线/连字符（不含扩展名）", true);
      el.name.focus();
      return Promise.resolve();
    }
    if (!html.trim()) {
      toast("模板内容为空，写点什么再保存", true);
      el.html.focus();
      return Promise.resolve();
    }
    if (html.length > MAX_TEMPLATE_CHARS) {
      toast("模板太大（上限 512 KB）", true);
      return Promise.resolve();
    }
    return withRequest(
      apiPost(ENDPOINT.save, { name: name, html: html })
    )
      .then(function (data) {
        state.dirty = false;
        toast("已保存" + (data && data.name ? "：" + data.name : ""));
        return loadList({ select: (data && data.name) || name });
      })
      .catch(function (error) {
        var message = "保存失败：" + errorMessage(error);
        // 内置模板可能引用的变量不符合模板仓库的必需变量校验；给用户一个可操作的下一步
        if (/必需变量|校验失败|模板语法/.test(message)) {
          message += "（可改个模板名另存为用户模板）";
        }
        toast(message, true);
      });
  }

  function deleteCurrent() {
    if (!state.current) return Promise.resolve();
    if (!canDelete()) {
      toast("内置模板不能删除；可以先把它另存成用户模板", true);
      return Promise.resolve();
    }
    var name = state.current.name;
    return showConfirm(
      "删除用户模板「" + name + "」？内置模板不受影响。",
      "删除模板",
      "删除"
    ).then(function (ok) {
      if (!ok) return null;
      return withRequest(apiPost(ENDPOINT.remove, { name: name }))
        .then(function () {
          state.current = null;
          state.dirty = false;
          toast("已删除：" + name);
          return loadList();
        })
        .catch(function (error) {
          toast("删除失败：" + errorMessage(error), true);
        });
    });
  }

  function fileToBase64(file) {
    return file.arrayBuffer().then(function (buffer) {
      var bytes = new Uint8Array(buffer);
      var binary = "";
      var chunk = 0x8000;
      for (var i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
      }
      return window.btoa(binary);
    });
  }

  function readFileAsText(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        resolve(String(reader.result || ""));
      };
      reader.onerror = function () {
        reject(new Error("读取文件失败"));
      };
      reader.readAsText(file, "utf-8");
    });
  }

  function importFile(file) {
    if (!file) return Promise.resolve();
    if (file.size > MAX_IMPORT_BYTES) {
      toast("文件太大（上限 4 MB）", true);
      return Promise.resolve();
    }
    var lower = String(file.name || "").toLowerCase();
    var bodyPromise;
    if (lower.endsWith(".zip")) {
      bodyPromise = fileToBase64(file).then(function (base64) {
        return { filename: file.name, data_base64: base64 };
      });
    } else if (lower.endsWith(".html") || lower.endsWith(".htm")) {
      bodyPromise = readFileAsText(file).then(function (text) {
        return { filename: file.name, html: text };
      });
    } else {
      toast("只支持 .html / .htm / .zip", true);
      return Promise.resolve();
    }
    return bodyPromise
      .then(function (body) {
        return withRequest(apiPost(ENDPOINT.importFile, body));
      })
      .then(function (data) {
        var text = data && data.name ? "已导入：" + data.name : "导入完成";
        toast(text);
        var select = data && data.name ? data.name : undefined;
        return loadList(select ? { select: select } : {});
      })
      .catch(function (error) {
        toast("导入失败：" + errorMessage(error), true);
      });
  }

  function doPreview() {
    var html = el.html.value || "";
    if (!html.trim()) {
      toast("模板内容为空，没法预览", true);
      return Promise.resolve();
    }
    var name = (el.name.value || "").trim();
    if (name && !TEMPLATE_NAME_RE.test(name)) {
      toast("模板名不合法：只能含中文/字母/数字/下划线/连字符（不含扩展名）", true);
      return Promise.resolve();
    }
    return withRequest(apiPost(ENDPOINT.preview, { name: name, html: html }))
      .then(function (data) {
        openPreview(data || {});
      })
      .catch(function (error) {
        toast("预览失败：" + errorMessage(error), true);
      });
  }

  function openPreview(data) {
    if (el.previewModal) el.previewModal.classList.remove("hidden");
    if (el.previewSize) el.previewSize.textContent = fmtSize((data.html || "").length);
    var warnings = (data.warnings || []).slice();
    if (data.error) warnings.unshift(data.error);
    if (el.previewWarnings) {
      if (warnings.length) {
        el.previewWarnings.textContent = warnings.join("\n");
        el.previewWarnings.classList.remove("hidden");
      } else {
        el.previewWarnings.classList.add("hidden");
      }
    }
    if (el.previewFrame) {
      if (data.error) {
        el.previewFrame.srcdoc = errorPage(data.error);
      } else {
        el.previewFrame.srcdoc = data.html || errorPage("没有可渲染的内容");
      }
    }
  }

  function closePreview() {
    if (el.previewModal) el.previewModal.classList.add("hidden");
    if (el.previewFrame) el.previewFrame.srcdoc = "";
  }

  function errorPage(message) {
    return (
      '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
      "body{font-family:system-ui,sans-serif;background:#f6f2ea;color:#b3392f;" +
      "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:24px}" +
      "pre{white-space:pre-wrap;font-size:14px;max-width:720px}" +
      "</style></head><body><pre>" +
      esc(message) +
      "</pre></body></html>"
    );
  }

  function downloadCurrent() {
    var html = el.html.value || "";
    if (!html.trim()) {
      toast("模板内容为空", true);
      return;
    }
    var name = (el.name.value || DEFAULT_NAME).trim() || DEFAULT_NAME;
    // 模板名不含扩展名，落盘时补 .html，下载下来才是能直接打开的完整文件
    var filename = /\.html?$/i.test(name) ? name : name + ".html";
    var blob = new Blob([html], { type: "text/html;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
      anchor.remove();
    }, 0);
    toast("已下载：" + filename);
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bindEvents() {
    if (el.btnRefresh) {
      el.btnRefresh.addEventListener("click", function () {
        if (state.dirty) {
          showConfirm("刷新会丢弃未保存的修改，继续？", "刷新列表").then(function (ok) {
            if (ok) loadList();
          });
          return;
        }
        loadList();
      });
    }
    if (el.btnNew) {
      el.btnNew.addEventListener("click", function () {
        var go = function () {
          clearEditorForNew();
          el.name.focus();
          el.name.select();
        };
        if (state.dirty) {
          showConfirm("新建会丢弃当前未保存的修改，继续？", "新建模板").then(function (ok) {
            if (ok) go();
          });
          return;
        }
        go();
      });
    }
    if (el.btnImport) {
      el.btnImport.addEventListener("click", function () {
        if (el.importFile) el.importFile.click();
      });
    }
    if (el.importFile) {
      el.importFile.addEventListener("change", function () {
        var file = el.importFile.files && el.importFile.files[0];
        importFile(file).finally(function () {
          el.importFile.value = "";
        });
      });
    }
    if (el.btnSave) el.btnSave.addEventListener("click", saveCurrent);
    if (el.btnPreview) el.btnPreview.addEventListener("click", doPreview);
    if (el.btnDelete) el.btnDelete.addEventListener("click", deleteCurrent);
    if (el.btnDownload) el.btnDownload.addEventListener("click", downloadCurrent);
    if (el.btnPreviewClose) el.btnPreviewClose.addEventListener("click", closePreview);
    if (el.btnPreviewRefresh) {
      el.btnPreviewRefresh.addEventListener("click", function () {
        closePreview();
        doPreview();
      });
    }
    if (el.html) {
      el.html.addEventListener("input", function () {
        state.dirty = true;
        renderGutter();
        renderVarChips(el.html.value);
        renderDirty();
      });
      el.html.addEventListener("scroll", function () {
        if (el.gutter) el.gutter.scrollTop = el.html.scrollTop;
      });
      el.html.addEventListener("keydown", function (event) {
        if (event.key === "Tab") {
          event.preventDefault();
          var start = el.html.selectionStart;
          var end = el.html.selectionEnd;
          var value = el.html.value;
          el.html.value = value.slice(0, start) + "  " + value.slice(end);
          el.html.selectionStart = el.html.selectionEnd = start + 2;
          state.dirty = true;
          renderGutter();
        }
      });
    }
    if (el.name) {
      el.name.addEventListener("input", function () {
        state.dirty = true;
        renderDirty();
      });
    }
    if (el.btnConfirmOk) {
      el.btnConfirmOk.addEventListener("click", function () {
        closeConfirm(true);
      });
    }
    if (el.btnConfirmCancel) {
      el.btnConfirmCancel.addEventListener("click", function () {
        closeConfirm(false);
      });
    }
    if (el.confirmModal) {
      el.confirmModal.addEventListener("click", function (event) {
        if (event.target === el.confirmModal) closeConfirm(false);
      });
    }
    if (el.previewModal) {
      el.previewModal.addEventListener("click", function (event) {
        if (event.target === el.previewModal) closePreview();
      });
    }
    document.addEventListener("keydown", function (event) {
      var key = event.key ? event.key.toLowerCase() : "";
      if ((event.ctrlKey || event.metaKey) && key === "s") {
        event.preventDefault();
        saveCurrent();
      }
      if (event.key === "Escape") {
        if (el.previewModal && !el.previewModal.classList.contains("hidden")) closePreview();
        if (el.confirmModal && !el.confirmModal.classList.contains("hidden")) closeConfirm(false);
      }
    });
    window.addEventListener("beforeunload", function (event) {
      if (state.dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  function collectElements() {
    el = {
      pageTitle: $("page-title"),
      pluginName: $("plugin-name"),
      pageName: $("page-name"),
      statusChips: $("status-chips"),
      bridgeWarning: $("bridge-warning"),
      error: $("error"),
      templateList: $("template-list"),
      templateCount: $("template-count"),
      varChips: $("var-chips"),
      templateName: $("template-name"),
      name: $("template-name"),
      html: $("template-html"),
      gutter: $("line-gutter"),
      editorMeta: $("editor-meta"),
      dirtyFlag: $("dirty-flag"),
      toast: $("toast"),
      previewModal: $("preview-modal"),
      previewTitle: $("preview-title"),
      previewFrame: $("preview-frame"),
      previewWarnings: $("preview-warnings"),
      previewSize: $("preview-size"),
      confirmModal: $("confirm-modal"),
      confirmTitle: $("confirm-title"),
      confirmMessage: $("confirm-message"),
      confirmOk: $("btn-confirm-ok"),
      btnSave: $("btn-save"),
      btnPreview: $("btn-preview"),
      btnDelete: $("btn-delete"),
      btnDownload: $("btn-download"),
      btnImport: $("btn-import"),
      btnNew: $("btn-new"),
      btnRefresh: $("btn-refresh"),
      btnPreviewClose: $("btn-preview-close"),
      btnPreviewRefresh: $("btn-preview-refresh"),
      btnConfirmOk: $("btn-confirm-ok"),
      btnConfirmCancel: $("btn-confirm-cancel"),
      importFile: $("import-file")
    };
  }

  function init() {
    collectElements();
    bindEvents();
    renderGutter();
    renderVarChips("");
    renderDirty();
    setBusy(false);

    if (!bridge) {
      if (el.bridgeWarning) el.bridgeWarning.classList.remove("hidden");
      if (el.templateList) {
        el.templateList.innerHTML =
          '<div class="empty">没有 bridge：请从 AstrBot WebUI 的插件页打开。</div>';
      }
      return;
    }

    loadContext().then(function () {
      return loadList();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
