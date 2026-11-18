/* DComp Drive - painel de laboratório. Sem dependências e sem conteúdo dinâmico em innerHTML. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { nodes: [], files: [], demoEnabled: false };
  const labels = { HEALTHY: "HEALTHY", SUSPECT: "SUSPECT", DEAD: "DEAD", UNDER_REPLICATED: "SUB-REPLICADO", UNAVAILABLE: "INDISPONÍVEL" };

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + " MiB";
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GiB";
  }

  function readableError(response, body) {
    let detail = body && body.detail ? body.detail : "Erro na requisição";
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || "Entrada inválida").join("; ");
    return "HTTP " + response.status + ": " + detail;
  }

  async function api(url, options) {
    const response = await fetch(url, Object.assign({ cache: "no-store" }, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(readableError(response, data));
    return data;
  }

  function message(text, isError) {
    const notice = $("notice");
    notice.hidden = false;
    notice.classList.toggle("error", Boolean(isError));
    notice.textContent = text;
  }

  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }

  function pill(status) {
    const element = el("span", "pill", labels[status] || status || "—");
    if (status === "DEAD" || status === "UNAVAILABLE") element.classList.add("error");
    else if (status !== "HEALTHY") element.classList.add("warn");
    return element;
  }

  function demoLog(text) {
    if (!state.demoEnabled) return;
    const list = $("demo-events");
    if (list.children.length === 1 && list.firstElementChild.textContent === "Aguardando eventos...") {
      list.replaceChildren();
    }
    const entry = el("li", "", new Date().toLocaleTimeString("pt-BR") + " · " + text);
    list.prepend(entry);
    while (list.children.length > 20) list.lastElementChild.remove();
  }

  function updateDemoNodeOptions() {
    if (!state.demoEnabled) return;
    const select = $("demo-node");
    const previous = select.value;
    select.replaceChildren();
    for (const node of state.nodes) {
      const option = document.createElement("option");
      option.value = node.node_id;
      option.textContent = node.node_id + " (" + node.state + ")";
      select.append(option);
    }
    if (state.nodes.some((node) => node.node_id === previous)) select.value = previous;
  }

  async function demoRequest(endpoint, button, body) {
    if (!window.isSecureContext) throw new Error("Abra o painel em HTTPS ou localhost via túnel SSH.");
    const token = $("demo-token").value;
    if (!token) throw new Error("Informe o token administrativo do laboratório.");
    button.disabled = true;
    try {
      return await api(endpoint, {
        method: "POST",
        headers: { "X-Demo-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
      });
    } finally {
      button.disabled = false;
    }
  }

  async function demoAction(endpoint, button, body, description) {
    if (!window.confirm(description + "\n\nConfirma a operação apenas no laboratório isolado?")) return;
    try {
      const result = await demoRequest(endpoint, button, body);
      demoLog(description + (result.node_id ? " (" + result.node_id + ")" : "") + ".");
      if (result.corrupted_ids && result.corrupted_ids.length) {
        demoLog("Scrubbing detectou cópia corrompida: " + result.corrupted_ids.join(", "));
      }
      await refresh();
    } catch (error) {
      demoLog("ERRO: " + error.message);
      message("Falha no modo demonstração: " + error.message, true);
    }
  }

  async function initDemo() {
    try {
      const config = await api("/demo/config");
      if (!config.enabled) return;
      state.demoEnabled = true;
      $("demo-panel").hidden = false;
      $("demo-nav").hidden = false;
      if (!window.isSecureContext) {
        $("demo-security").hidden = false;
        for (const id of ["demo-fail", "demo-restore", "demo-corrupt"]) $(id).disabled = true;
      }
      updateDemoNodeOptions();
      demoLog("Modo de demonstração ativo. Use apenas arquivos /demo-*.");
    } catch (error) {
      console.warn("Painel de demonstração indisponível:", error.message);
    }
  }

  function renderNodes() {
    const grid = $("nodes-grid");
    grid.replaceChildren();
    if (!state.nodes.length) {
      grid.append(el("div", "panel empty-nodes", "Nenhum Storage Node registrado."));
      return;
    }
    for (const node of state.nodes) {
      const card = el("article", "panel node-card");
      card.append(el("div", "node-name", "▣ " + node.node_id));
      card.append(pill(node.state));
      card.append(el("div", "node-machine", "Máquina lógica: " + (node.machine_id || "—")));
      const bar = el("div", "node-bar");
      const fill = el("div", "node-fill");
      const percent = node.capacity_bytes > 0 ? Math.min(100, 100 * node.used_bytes / node.capacity_bytes) : 0;
      fill.style.width = percent + "%";
      if (node.state !== "HEALTHY") fill.style.background = "#ef9a86";
      bar.append(fill);
      card.append(bar);
      card.append(el("div", "node-meta", formatBytes(node.used_bytes) + " armazenados"));
      grid.append(card);
    }
  }

  function addCell(row, content, className) {
    const cell = el("td", className || "");
    if (content instanceof Node) cell.append(content);
    else cell.textContent = String(content);
    row.append(cell);
    return cell;
  }

  function renderFiles() {
    const body = $("file-rows");
    const term = $("file-filter").value.trim().toLocaleLowerCase("pt-BR");
    const matches = state.files.filter((file) => file.path.toLocaleLowerCase("pt-BR").includes(term));
    body.replaceChildren();
    if (!matches.length) {
      const row = el("tr");
      const cell = addCell(row, term ? "Nenhum arquivo corresponde à busca." : "Nenhum arquivo publicado.");
      cell.colSpan = 4;
      cell.className = "empty";
      body.append(row);
      return;
    }
    for (const file of matches) {
      const row = el("tr");
      const name = el("div", "file-main", "▤ " + file.path);
      const sub = el("div", "file-sub", "Versão " + file.version + " · SHA-256 " + file.sha256.slice(0, 12) + "…");
      const cell = addCell(row, name);
      cell.append(sub);
      addCell(row, formatBytes(file.size_bytes));
      const replicas = el("span");
      replicas.append(pill(file.status));
      replicas.append(el("div", "file-sub", file.replicas.length + " / " + file.replication_factor + " cópias"));
      addCell(row, replicas);
      const actions = addCell(row, el("span"));
      const detailButton = el("button", "button mini-button", "Detalhes");
      detailButton.type = "button";
      detailButton.addEventListener("click", () => showDetails(file.path));
      const downloadButton = el("button", "button mini-button", "Baixar");
      downloadButton.type = "button";
      downloadButton.addEventListener("click", () => downloadFile(file.path, downloadButton));
      actions.append(detailButton, downloadButton);
      body.append(row);
    }
  }

  function updateMetrics() {
    const healthy = state.nodes.filter((node) => node.state === "HEALTHY").length;
    $("metric-nodes").textContent = healthy + " / " + state.nodes.length;
    $("metric-files").textContent = state.files.length.toString();
    $("metric-replicas").textContent = state.files.reduce((sum, file) => sum + file.replicas.length, 0).toString();
    $("metric-storage").textContent = formatBytes(state.nodes.reduce((sum, node) => sum + Number(node.used_bytes || 0), 0));
  }

  function updateBadge(healthy) {
    const badge = $("cluster-badge");
    badge.className = "health-badge" + (healthy ? "" : " is-error");
    badge.replaceChildren(el("span", "status-dot"), document.createTextNode(healthy ? "Cluster saudável" : "Cluster degradado"));
  }

  async function refresh() {
    const results = await Promise.allSettled([api("/status"), api("/files/catalog?limit=200")]);
    if (results[0].status === "fulfilled") {
      const previous = new Map(state.nodes.map((node) => [node.node_id, node.state]));
      state.nodes = results[0].value.nodes || [];
      if (state.demoEnabled) {
        for (const node of state.nodes) {
          if (previous.has(node.node_id) && previous.get(node.node_id) !== node.state) {
            demoLog(node.node_id + ": " + previous.get(node.node_id) + " → " + node.state);
          }
        }
      }
      renderNodes();
      updateDemoNodeOptions();
      updateBadge(state.nodes.length > 0 && state.nodes.every((n) => n.state === "HEALTHY"));
    } else {
      $("cluster-badge").className = "health-badge is-error";
      $("cluster-badge").textContent = "Controlador indisponível";
      message("Não foi possível consultar o controlador: " + results[0].reason.message, true);
    }
    if (results[1].status === "fulfilled") {
      const previousFiles = new Map(state.files.map((file) => [file.path, file]));
      state.files = results[1].value || [];
      if (state.demoEnabled) {
        for (const file of state.files) {
          const old = previousFiles.get(file.path);
          if (old && file.path.startsWith("/demo-") &&
              (old.status !== file.status || old.replicas.length !== file.replicas.length)) {
            demoLog(file.path + ": " + file.status + " · " + file.replicas.length + "/" + file.replication_factor + " réplicas");
          }
        }
      }
      renderFiles();
    } else {
      message("Não foi possível listar os arquivos: " + results[1].reason.message, true);
    }
    updateMetrics();
    $("last-updated").textContent = "Atualizado às " + new Date().toLocaleTimeString("pt-BR");
  }

  function metaItem(label, value) {
    const item = el("div", "detail-item");
    item.append(el("span", "detail-label", label), el("strong", "detail-value", value));
    return item;
  }

  async function showDetails(path) {
    try {
      const info = await api("/files/metadata?path=" + encodeURIComponent(path));
      $("detail-path").textContent = info.path;
      const grid = $("detail-metadata");
      grid.replaceChildren(
        metaItem("Estado", labels[info.status] || info.status),
        metaItem("Versão", info.version),
        metaItem("Tamanho", formatBytes(info.size_bytes)),
        metaItem("Replicação", info.replicas.length + " / " + info.replication_factor),
        metaItem("SHA-256", info.sha256)
      );
      const replicas = $("detail-replicas");
      replicas.replaceChildren();
      if (!info.replicas.length) replicas.append(el("div", "file-sub", "Nenhuma réplica saudável."));
      for (const replica of info.replicas) {
        replicas.append(el("span", "replica-item", "● " + replica.node_id + " · " + replica.disk_id));
      }
      $("file-details").hidden = false;
      $("file-details").scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (error) {
      message("Falha ao consultar os metadados: " + error.message, true);
    }
  }

  async function downloadFile(path, button) {
    const initialText = button.textContent;
    button.disabled = true;
    button.textContent = "Baixando...";
    try {
      const info = await api("/files/metadata?path=" + encodeURIComponent(path));
      const response = await fetch("/files/content?path=" + encodeURIComponent(path), { cache: "no-store" });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(readableError(response, body));
      }
      const buffer = await response.arrayBuffer();
      if (!window.crypto || !window.crypto.subtle) {
        throw new Error("A verificação SHA-256 requer HTTPS ou acesso via http://localhost:8000/ (túnel SSH).");
      }
      const digest = await crypto.subtle.digest("SHA-256", buffer);
      const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
      if (hash !== info.sha256 || buffer.byteLength !== Number(info.size_bytes)) {
        throw new Error("Falha de integridade: SHA-256 ou tamanho divergente.");
      }
      const objectUrl = URL.createObjectURL(new Blob([buffer], { type: "application/octet-stream" }));
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = path.split("/").pop() || "download";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 15000);
      message("Download concluído e SHA-256 verificado: " + path, false);
    } catch (error) {
      message("Falha no download de " + path + ": " + error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = initialText;
    }
  }

  async function uploadFile(event) {
    event.preventDefault();
    const selected = $("local-file").files[0];
    const path = $("remote-path").value.trim();
    if (!selected) return message("Selecione um arquivo.", true);
    if (!path.startsWith("/") || path === "/" || path.endsWith("/")) return message("Informe um caminho absoluto, como /arquivo.txt.", true);
    if (selected.size > 32 * 1024 * 1024) return message("Este painel aceita arquivos de até 32 MiB na configuração atual.", true);
    const button = $("upload-button");
    button.disabled = true;
    button.textContent = "Enviando e replicando...";
    try {
      const info = await api("/files/content?path=" + encodeURIComponent(path), {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: selected
      });
      message("Arquivo enviado: " + info.path + " · " + info.replicas.length + "/" + info.replication_factor + " réplicas · SHA-256 " + info.sha256, false);
      await refresh();
      await showDetails(info.path);
    } catch (error) {
      message("Falha no upload: " + error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "Enviar para o cluster →";
    }
  }

  $("local-file").addEventListener("change", () => {
    const selected = $("local-file").files[0];
    $("selected-file").textContent = selected ? selected.name + " · " + formatBytes(selected.size) : "Clique aqui para selecionar um arquivo";
    if (selected && (!$("remote-path").value || $("remote-path").value.startsWith("/"))) {
      $("remote-path").value = "/" + selected.name;
    }
  });
  $("upload-form").addEventListener("submit", uploadFile);
  $("refresh").addEventListener("click", refresh);
  $("file-filter").addEventListener("input", renderFiles);
  $("close-details").addEventListener("click", () => { $("file-details").hidden = true; });
  $("demo-fail").addEventListener("click", () => {
    const node = $("demo-node").value;
    if (!node) return;
    demoAction("/demo/nodes/" + encodeURIComponent(node) + "/offline", $("demo-fail"),
      {}, "Simular interrupção lógica de " + node);
  });
  $("demo-restore").addEventListener("click", () => {
    const node = $("demo-node").value;
    if (!node) return;
    demoAction("/demo/nodes/" + encodeURIComponent(node) + "/restore", $("demo-restore"),
      {}, "Restaurar " + node);
  });
  $("demo-corrupt").addEventListener("click", () => {
    const path = $("demo-path").value.trim();
    if (!/^\/demo-[A-Za-z0-9_.-]{1,100}$/.test(path)) {
      return message("Use apenas arquivo descartável com caminho /demo-*.", true);
    }
    demoAction("/demo/files/corrupt", $("demo-corrupt"), { path },
      "Corromper uma réplica de " + path + " e executar scrubbing");
  });

  refresh();
  initDemo();
  setInterval(() => { if (state.demoEnabled) refresh(); }, 2000);
  setInterval(() => { if (!state.demoEnabled) refresh(); }, 10000);
})();
