/* DComp Drive - painel de laboratório. Sem dependências e sem conteúdo dinâmico em innerHTML. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { nodes: [], files: [], demoEnabled: false, publicDemo: false };
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
    if (!state.publicDemo && !token) throw new Error("Informe o token administrativo do laboratório.");
    const publicEndpoint = state.publicDemo ? endpoint.replace("/demo/", "/demo/public/") : endpoint;
    button.disabled = true;
    try {
      return await api(publicEndpoint, {
        method: "POST",
        headers: state.publicDemo
          ? { "Content-Type": "application/json" }
          : { "X-Demo-Token": token, "Content-Type": "application/json" },
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
      state.publicDemo = Boolean(config.public);
      if (state.publicDemo) {
        $("demo-token").hidden = true;
        const label = document.querySelector('label[for="demo-token"]');
        if (label) label.hidden = true;
        $("demo-private-note").hidden = true;
        $("demo-public-note").hidden = false;
        $("demo-expansion").hidden = false;
        $("demo-persistence").hidden = false;
      }
      $("demo-panel").hidden = false;
      $("demo-nav").hidden = false;
      if (!window.isSecureContext) {
        $("demo-security").hidden = false;
        for (const id of ["demo-fail", "demo-restore", "demo-corrupt", "demo-drain", "demo-undrain", "demo-concurrency"]) $(id).disabled = true;
      }
      updateDemoNodeOptions();
      demoLog("Modo de demonstração ativo. Use apenas arquivos /demo-*.");
    } catch (error) {
      console.warn("Painel de demonstração indisponível:", error.message);
    }
  }


  function validEntryName(name) {
    return Boolean(name) && name.trim() === name && name !== "." && name !== ".." &&
      !name.includes("/") && !name.includes("\\") && !name.includes("\u0000");
  }

  function parentOf(path) {
    return path.slice(0, path.lastIndexOf("/")) || "/";
  }

  function joinPath(parent, name) {
    return (parent === "/" ? "" : parent) + "/" + name;
  }

  function actionButton(text, handler) {
    const button = el("button", "button mini-button", text);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  async function listDirectory(target) {
    const path = (target || $("namespace-path").value).trim();
    if (!path.startsWith("/")) throw new Error("Use um caminho absoluto, por exemplo /documentos.");
    const entries = await api("/files/entries?path=" + encodeURIComponent(path));
    $("namespace-path").value = path;
    $("namespace-remove").disabled = path === "/";
    const body = $("namespace-rows");
    body.replaceChildren();

    if (!entries.length) {
      const tr = el("tr");
      const td = addCell(tr, "Diretório vazio", "empty");
      td.colSpan = 5;
      body.append(tr);
    }

    for (const entry of entries) {
      const tr = el("tr");
      const fullPath = joinPath(path, entry.name);
      addCell(tr, entry.name);
      addCell(tr, entry.kind === "dir" ? "Diretório" : "Arquivo");
      addCell(tr, entry.kind === "dir" ? "—" : formatBytes(entry.size));
      addCell(tr, entry.state || "—");
      const actionsCell = addCell(tr, el("div", "file-actions"));
      const actions = actionsCell.firstElementChild;
      if (entry.kind === "dir") {
        actions.append(actionButton("Abrir", () =>
          listDirectory(fullPath).catch(err => message("Diretório: " + err.message, true))));
        actions.append(actionButton("Excluir pasta", () => removeDirectory(fullPath)));
      } else {
        actions.append(
          actionButton("Detalhes", () => showDetails(fullPath)),
          actionButton("Baixar", event => downloadFile(fullPath, event.currentTarget)),
          actionButton("Renomear", () => renameFile(fullPath)),
          actionButton("Mover", () => moveFile(fullPath)),
          actionButton("Excluir", () => deleteFile(fullPath))
        );
      }
      body.append(tr);
    }

    $("namespace-notice").textContent = "Pasta " + path + " · " + entries.length + " item(ns)";
  }

  async function removeDirectory(path) {
    if (path === "/") return message("O diretório raiz não pode ser removido.", true);
    if (!window.confirm("Remover pasta vazia?\n" + path)) return;
    try {
      await api("/files/directories?path=" + encodeURIComponent(path), { method: "DELETE" });
      message("Diretório removido: " + path, false);
      await listDirectory(parentOf(path));
    } catch (err) {
      message("Remover pasta: " + err.message, true);
    }
  }

  async function manageDirectory(create) {
    const current = $("namespace-path").value.trim();
    if (!current.startsWith("/")) return message("Informe o diretório atual.", true);
    if (!create) return removeDirectory(current);
    const name = $("namespace-new-name").value;
    if (!validEntryName(name)) return message("Nome inválido. Informe apenas o nome da nova pasta, sem barras.", true);
    const path = joinPath(current, name);
    try {
      await api("/files/directories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path })
      });
      $("namespace-new-name").value = "";
      message("Diretório criado: " + path, false);
      await listDirectory(current);
    } catch (err) {
      message("Criar pasta: " + err.message, true);
    }
  }

  async function changeFilePath(src, dst, action) {
    if (!dst.startsWith("/") || dst === "/") return message("Caminho absoluto obrigatório.", true);
    if (src === dst) return message("O destino é igual à origem.", true);
    try {
      await api("/files/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src, dst })
      });
      message(action + ": " + src + " → " + dst, false);
      await refresh();
      await listDirectory($("namespace-path").value).catch(() => {});
    } catch (err) {
      message(action + ": " + err.message, true);
    }
  }

  async function renameFile(src) {
    const parent = parentOf(src);
    const oldName = src.slice(src.lastIndexOf("/") + 1);
    const newName = window.prompt("Novo nome do arquivo (sem /):", oldName);
    if (newName === null || newName === oldName) return;
    if (!validEntryName(newName)) return message("Nome inválido. Use um nome sem barras.", true);
    return changeFilePath(src, joinPath(parent, newName), "Renomear");
  }

  async function moveFile(src) {
    const dst = window.prompt(
      "Caminho completo do arquivo no destino (a pasta precisa existir).\nEx.: /trabalhos/arquivo.txt",
      src
    );
    if (dst === null || dst === src) return;
    return changeFilePath(src, dst.trim(), "Mover");
  }

  async function deleteFile(path) {
    if (!window.confirm("Excluir permanentemente o arquivo e suas réplicas?\n" + path)) return;
    try {
      const result = await api("/files/entry?path=" + encodeURIComponent(path), { method: "DELETE" });
      message("Arquivo excluído: " + path +
        (result.pending_cleanup ? " · limpeza física pendente em " + result.pending_cleanup + " storage(s)." : ""), false);
      await refresh();
      await listDirectory($("namespace-path").value).catch(() => {});
    } catch (err) {
      message("Excluir arquivo: " + err.message, true);
    }
  }

  function renderCapacity() {
    const body=$("capacity-rows"); body.replaceChildren();
    for(const node of state.nodes) for(const disk of node.disks || []) {
      const tr=el("tr"),pct=disk.capacity_bytes>0?(100*disk.used_bytes/disk.capacity_bytes).toFixed(1)+"%":"—";
      for(const val of [node.node_id,disk.disk_id,formatBytes(disk.capacity_bytes),formatBytes(disk.used_bytes),formatBytes(disk.free_bytes),pct])addCell(tr,val);
      body.append(tr);
    }
  }

  async function refreshTelemetry() {
    try {
      const [m,s]=await Promise.all([api("/metrics"),api("/status")]);
      const target=$("cluster-metrics"); target.replaceChildren();
      const data=[
        ["Reparos em andamento",s.repairs_in_progress??0],
        ["Arquivos por estado",Object.entries(s.files_by_state||{}).map(([k,v])=>k+": "+v).join(" · ")||"—"],
        ["Réplicas por estado",Object.entries(m.replicas_by_state||{}).map(([k,v])=>k+": "+v).join(" · ")||"—"],
        ["Requisições HTTP",m.requests_total??"—"],
        ["Latência média HTTP",m.request_latency_avg_ms===undefined?"—":m.request_latency_avg_ms+" ms"],
        ["Tempo ativo controlador",m.uptime_seconds===undefined?"—":Math.floor(m.uptime_seconds)+" s"]
      ];
      for(const [label,val] of data){
        const div=el("div","detail-item");
        div.append(el("span","detail-label",label),el("strong","detail-value",val));target.append(div);
      }
      renderCapacity();
    } catch(e){$("cluster-metrics").textContent="Falha: "+e.message;}
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
      const actionsCell = addCell(row, el("div", "file-actions"));
      const actions = actionsCell.firstElementChild;
      const detailButton = el("button", "button mini-button", "Detalhes");
      detailButton.type = "button";
      detailButton.addEventListener("click", () => showDetails(file.path));
      const downloadButton = el("button", "button mini-button", "Baixar");
      downloadButton.type = "button";
      downloadButton.addEventListener("click", () => downloadFile(file.path, downloadButton));
      const renameButton = actionButton("Renomear", () => renameFile(file.path));
      const moveButton = actionButton("Mover", () => moveFile(file.path));
      const deleteButton = actionButton("Excluir", () => deleteFile(file.path));

      actions.append(detailButton, downloadButton, renameButton, moveButton, deleteButton);
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
          if (!previous.has(node.node_id) && previous.size > 0) demoLog("Novo Storage Node descoberto: " + node.node_id);
          if (previous.has(node.node_id) && previous.get(node.node_id) !== node.state) {
            demoLog(node.node_id + ": " + previous.get(node.node_id) + " → " + node.state);
          }
        }
      }
      renderNodes();
      renderCapacity();
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
        metaItem("Criado em", info.created_at ? new Date(info.created_at).toLocaleString("pt-BR") : "—"),
        metaItem("Tamanho", formatBytes(info.size_bytes)),
        metaItem("Replicação", info.replicas.length + " / " + info.replication_factor),
        metaItem("SHA-256", info.sha256),
        metaItem("ID físico da versão", info.version_id || "—")
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
      const factor = Number($("upload-replicas").value);
      const info = await api("/files/content?path=" + encodeURIComponent(path) + "&replication_factor=" + factor, {
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

  $("namespace-list").addEventListener("click",()=>{
    listDirectory().catch(e=>message("Listagem: "+e.message,true));
  });
  $("namespace-up").addEventListener("click", () => {
    listDirectory(parentOf($("namespace-path").value.trim())).catch(err => message(err.message, true));
  });
  $("namespace-create").addEventListener("click",()=>manageDirectory(true));
  $("namespace-remove").addEventListener("click",()=>manageDirectory(false));
  $("metrics-refresh").addEventListener("click",refreshTelemetry);

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


  $("demo-drain").addEventListener("click", () => {
    const node = $("demo-node").value;
    if(node) demoAction("/demo/nodes/" + encodeURIComponent(node) + "/drain",
      $("demo-drain"), {}, "Drenagem planejada (RF7) de " + node);
  });
  $("demo-undrain").addEventListener("click", () => {
    const node = $("demo-node").value;
    if(node) demoAction("/demo/nodes/" + encodeURIComponent(node) + "/undrain",
      $("demo-undrain"), {}, "Reabilitar nó drenado " + node);
  });
  $("demo-persistence-run").addEventListener("click", async () => {
    if (!state.publicDemo) return;
    if (!window.confirm("Reiniciar o controlador SOMENTE do laboratório descartável para testar a persistência?")) return;
    const btn = $("demo-persistence-run"), status = $("demo-persistence-status");
    btn.disabled = true;
    try {
      const before = await api("/demo/public/persistence/snapshot");
      status.textContent = "RF10: snapshot anterior " + before.catalog_sha256.slice(0, 16) + "…";
      demoLog("RF10: " + before.files + " arquivos e " + before.directories + " diretórios; controlador " + before.controller_instance.slice(0, 8));
      await api("/demo/public/persistence/restart", { method: "POST" });
      status.textContent = "RF10: reiniciando processo controller; aguardando reaparecer...";
      let after = null;
      for (let attempt = 0; attempt < 40; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        try {
          const check = await api("/demo/public/persistence/snapshot");
          if (check.controller_instance !== before.controller_instance) {
            after = check;
            break;
          }
        } catch (_) { /* esperado durante o reinício */ }
      }
      if (!after) throw new Error("O processo não reapareceu após o reinício.");
      const preserved = before.catalog_sha256 === after.catalog_sha256 &&
        before.files === after.files && before.directories === after.directories;
      status.textContent = "RF10: reinício comprovado: " + before.controller_instance.slice(0,8) +
        " → " + after.controller_instance.slice(0,8) +
        " · catálogo " + (preserved ? "PRESERVADO" : "DIVERGENTE") +
        " · SHA-256 " + after.catalog_sha256.slice(0,16) + "…";
      demoLog(status.textContent);
      message(preserved ? "RF10: metadados preservados após reinício real." :
        "RF10: metadados divergentes após reinício. Verifique o cluster.", !preserved);
      await refresh();
    } catch (err) {
      status.textContent = "RF10: erro: " + err.message;
      message("RF10: " + err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("demo-activate-six").addEventListener("click", async () => {
    if (!state.publicDemo) return;
    if (!window.confirm("Ativar sexto Storage Node e iniciar seu registro automático?")) return;
    const btn = $("demo-activate-six");
    btn.disabled = true;
    try {
      const result = await api("/demo/public/nodes/activate-six", { method: "POST" });
      demoLog("RF6: ativação do " + result.node_id + " solicitada. Aguarde o registro via heartbeat.");
      message("RF6: novo nó ativado; aguarde o status HEALTHY no painel.", false);
      await refresh();
    } catch (error) { message("RF6: " + error.message, true); }
    finally { btn.disabled = false; }
  });
  $("demo-encryption").addEventListener("click", async () => {
    const path = $("demo-encryption-path").value.trim();
    if (!/^\/demo-[A-Za-z0-9_.-]{1,100}$/.test(path)) {
      return message("RF9: escolha um arquivo descartável /demo-*.", true);
    }
    const btn = $("demo-encryption");
    try {
      const proof = await demoRequest("/demo/files/encryption", btn, { path });
      const okay = proof.encrypted_on_disk && proof.disk_sha256 !== proof.plaintext_sha256;
      const summary = "RF9: " + proof.node_id + " · AES-256-GCM: " +
        (proof.encrypted_on_disk ? "cifrado" : "não cifrado") +
        " · SHA disco: " + proof.disk_sha256.slice(0,16) +
        "… · SHA texto claro: " + proof.plaintext_sha256.slice(0,16) + "…";
      $("demo-encryption-result").textContent = summary;
      demoLog(summary);
      message(okay ? "RF9: cifra física comprovada." : "RF9: cópia não cifrada ou evidência inconclusiva.", !okay);
    } catch (err) { message("RF9: " + err.message, true); }
  });
  
  $("demo-reset").addEventListener("click", async () => {
    if (!state.publicDemo) return message("Disponível apenas no laboratório público.", true);
    if (!window.confirm("Reabilitar todos os nós de teste?")) return;
    try {
      const out = await api("/demo/public/reset", { method: "POST" });
      demoLog("Reabilitação: " + out.total + " nó(s) receberam comando de restauração");
      await refresh();
    } catch (err) { message("Reabilitação: " + err.message, true); }
  });

  $("demo-concurrency").addEventListener("click", async () => {
    if(!state.demoEnabled || !window.isSecureContext || (!state.publicDemo && !$("demo-token").value)) {
      return message("A demonstração exige HTTPS e laboratório habilitado.", true);
    }
    if(!window.confirm("Executar duas gravações simultâneas em /demo-concorrencia.txt?")) return;
    const btn=$("demo-concurrency"), path="/demo-concorrencia.txt";
    const url="/files/content?path="+encodeURIComponent(path)+"&replication_factor=3";
    btn.disabled=true;
    try {
      const payloads=[
        new TextEncoder().encode("Escrita A, teste RF8, " + Date.now()),
        new TextEncoder().encode("Escrita B, teste RF8, " + Date.now())
      ];
      const requests=payloads.map(data=>fetch(url,{
        method:"PUT",headers:{"Content-Type":"application/octet-stream"},body:data
      }));
      const responses=await Promise.all(requests);
      demoLog("RF8: duas escritas simultâneas: HTTP " + responses.map(r=>r.status).join(" e "));
      if(!responses.some(r=>r.status===201)) throw new Error("Nenhuma gravação confirmada");
      const info=await api("/files/metadata?path="+encodeURIComponent(path));
      const reply=await fetch("/files/content?path=" + encodeURIComponent(path),{method:"GET",cache:"no-store"});
      if(!reply.ok) throw new Error("Download final HTTP "+reply.status);
      const bytes=await reply.arrayBuffer();
      const digest=await crypto.subtle.digest("SHA-256",bytes);
      const sha=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,"0")).join("");
      const valid=sha===info.sha256 && bytes.byteLength===Number(info.size_bytes);
      demoLog("RF8: versão "+info.version+" íntegra="+valid+" · SHA-256="+sha.slice(0,12)+"…");
      if(!valid) throw new Error("SHA-256 final divergente");
      await refresh();
    }catch(err){demoLog("RF8 ERRO: "+err.message);message("RF8: "+err.message,true);}
    finally{btn.disabled=false;}
  });

  refresh();
  listDirectory("/").catch(()=>{});
  refreshTelemetry();
  initDemo();
  setInterval(() => { if (state.demoEnabled) refresh(); }, 2000);
  setInterval(() => { if (!state.demoEnabled) refresh(); }, 10000);
  setInterval(refreshTelemetry, 10000);
})();
