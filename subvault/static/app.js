if (window.self !== window.top) document.documentElement.classList.add("embedded");
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { csrf: "", user: "", authMode: "standalone", nodes: [], allNodes: [], nodeOptionsAt: 0, nodeOptionsPromise: null, nodeRequest: 0, subscriptionRequest: 0, subscriptions: [], templates: [], pendingCreateSubscription: false };
const listState = {
  nodes: {q:"", group:"", status:"all", page:1, page_size:25},
  subscriptions: {q:"", group:"", status:"all", page:1, page_size:25},
};
const titles = { dashboard: "概览", subscriptions: "订阅管理", nodes: "节点管理", templates: "模板管理", logs: "访问日志", settings: "系统设置" };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}
function fmtBytes(value) {
  const n = Number(value || 0); if (!n) return "0 B";
  const units = ["B","KB","MB","GB","TB"]; const i = Math.min(Math.floor(Math.log(n)/Math.log(1024)),4);
  return `${(n/1024**i).toFixed(i ? 1 : 0)} ${units[i]}`;
}
function fmtDate(value) { if (!value) return "永久"; const d = new Date(value); return Number.isNaN(d.valueOf()) ? "—" : d.toLocaleString("zh-CN", {hour12:false}); }
function localInputDate(value) { if (!value) return ""; const d = new Date(value); const local = new Date(d.getTime()-d.getTimezoneOffset()*60000); return local.toISOString().slice(0,16); }
function statusOf(sub) {
  if (!sub.enabled) return ["已停用","bad"];
  if (sub.expires_at && new Date(sub.expires_at) <= new Date()) return ["已到期","bad"];
  if (sub.traffic_limit_bytes && sub.traffic_used_bytes >= sub.traffic_limit_bytes) return ["流量耗尽","warn"];
  return ["有效","ok"];
}
function showToast(message, error=false) { const el=$("#toast"); el.textContent=message; el.className=`toast show${error?" error":""}`; clearTimeout(showToast.timer); showToast.timer=setTimeout(()=>el.className="toast",2600); }

async function api(path, options={}) {
  const headers = {"Content-Type":"application/json", ...(options.headers||{})};
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(`/vault${path}`, {cache:"no-store", ...options, headers});
  const data = await response.json().catch(()=>({error:"响应格式无效"}));
  if (response.status === 401) {
    if (state.authMode === "nexusgate") window.top.location.assign("/");
    else location.reload();
    throw new Error("登录已过期");
  }
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function showApp(session) {
  state.csrf=session.csrf;state.user=session.username;state.authMode=session.auth_mode;
  if(state.authMode==="nexusgate" && window.self===window.top) {
    const section=(location.hash||"#dashboard").slice(1);
    window.location.replace(`/#vault-${titles[section]?section:"dashboard"}`);
    return;
  }
  $("#adminName").textContent=session.username;route();
}

$("#logoutBtn").addEventListener("click", async()=>{
  try {
    if (state.authMode === "nexusgate") {
      const response=await fetch("/api/auth/logout", {method:"POST",headers:{"X-CSRF-Token":state.csrf}});
      if(!response.ok)throw new Error("退出失败，请重试");
      window.top.location.assign("/");
    } else {
      await api("/api/logout",{method:"POST",body:"{}"});
      state.csrf=""; location.reload();
    }
  } catch (error) { showToast(error.message,true); }
});
$("#menuBtn").addEventListener("click",()=>$(".sidebar").classList.toggle("open"));
$("#modalClose").addEventListener("click",closeModal);
$("#modal").addEventListener("click",e=>{if(e.target===$("#modal"))closeModal();});
window.addEventListener("hashchange",route);

function openModal(title, body, eyebrow="编辑") { $("#modalTitle").textContent=title;$("#modalEyebrow").textContent=eyebrow;$("#modalBody").innerHTML=body;$("#modal").classList.remove("hidden"); }
function closeModal(){ $("#modal").classList.add("hidden");$("#modalBody").innerHTML=""; }
function empty(title, text){ return `<div class="empty"><strong>${escapeHtml(title)}</strong>${escapeHtml(text)}</div>`; }
function badge(text, kind="blue"){return `<span class="badge ${kind}"><i class="dot"></i>${escapeHtml(text)}</span>`;}
function bindAction(selector, fn){$$(selector).forEach(el=>el.addEventListener("click",()=>fn(el)));}
function confirmAction(message){return window.confirm(message);}
function listQuery(kind){const value=listState[kind];return new URLSearchParams({q:value.q,group:value.group,status:value.status,page:value.page,page_size:value.page_size}).toString();}
function optionList(groups,current){return [`<option value="">全部分组</option>`,...groups.map(group=>`<option value="${escapeHtml(group)}" ${group===current?"selected":""}>${escapeHtml(group)}</option>`)].join("");}
function paginationBar(kind,data){return `<div class="pagination"><span>共 ${data.total} 条 · 第 ${data.page}/${data.pages} 页</span><div><button class="btn small page-btn" data-page="${data.page-1}" ${data.page<=1?"disabled":""}>上一页</button><button class="btn small page-btn" data-page="${data.page+1}" ${data.page>=data.pages?"disabled":""}>下一页</button><select class="page-size" aria-label="每页数量"><option value="25" ${data.page_size===25?"selected":""}>25/页</option><option value="50" ${data.page_size===50?"selected":""}>50/页</option><option value="100" ${data.page_size===100?"selected":""}>100/页</option></select></div></div>`;}
function filtersBar(kind,data,placeholder,statuses){const value=listState[kind];return `<form class="list-filters" id="${kind}Filters"><input name="q" value="${escapeHtml(value.q)}" placeholder="${escapeHtml(placeholder)}"><select name="group">${optionList(data.groups||[],value.group)}</select><select name="status">${statuses.map(item=>`<option value="${item[0]}" ${item[0]===value.status?"selected":""}>${item[1]}</option>`).join("")}</select><button class="btn primary" type="submit">搜索</button><button class="btn ghost reset-filters" type="button">重置</button></form>`;}
function bindListControls(kind,data,render){
  const form=$(`#${kind}Filters`);form.addEventListener("submit",event=>{event.preventDefault();const values=new FormData(form),value=listState[kind];value.q=String(values.get("q")||"").trim();value.group=String(values.get("group")||"");value.status=String(values.get("status")||"all");value.page=1;render();});
  $(".reset-filters",form).addEventListener("click",()=>{Object.assign(listState[kind],{q:"",group:"",status:"all",page:1});render();});
  bindAction(".page-btn",el=>{if(el.disabled)return;listState[kind].page=Number(el.dataset.page);render();});
  $(".page-size").addEventListener("change",event=>{listState[kind].page_size=Number(event.target.value);listState[kind].page=1;render();});
}
function bindBulkControls(kind,render){
  const endpoint=kind==="nodes"?"nodes":"subscriptions", boxes=()=>$$(`.${kind}-select`), count=$("#selectedCount");
  const refreshCount=()=>{count.textContent=`已选择 ${boxes().filter(box=>box.checked).length} 条`;};
  $("#selectPage").addEventListener("change",event=>{boxes().forEach(box=>{box.checked=event.target.checked;});refreshCount();});
  boxes().forEach(box=>box.addEventListener("change",refreshCount));
  bindAction(".bulk-action",async el=>{const ids=boxes().filter(box=>box.checked).map(box=>Number(box.value));if(!ids.length){showToast("请先选择记录",true);return;}const action=el.dataset.action;let group_name="";if(action==="group"){group_name=(window.prompt("请输入目标分组名称")||"").trim();if(!group_name)return;}if(action==="delete"&&!confirmAction(`确认批量删除选中的 ${ids.length} 条记录？`))return;try{const result=await api(`/api/${endpoint}/bulk`,{method:"POST",body:JSON.stringify({ids,action,group_name})});if(kind==="nodes")state.nodeOptionsAt=0;showToast(`已处理 ${result.affected} 条记录`);await render();}catch(error){showToast(error.message,true);}});
}

async function route(){
  if(!state.csrf)return;
  const page=(location.hash||"#dashboard").slice(1); const chosen=titles[page]?page:"dashboard";
  $$("#nav a").forEach(a=>a.classList.toggle("active",a.dataset.page===chosen));
  $("#pageTitle").textContent=titles[chosen];$("#breadcrumb").textContent=`控制台 / ${titles[chosen]}`;$(".sidebar").classList.remove("open");
  $("#content").innerHTML=`<div class="panel">${empty("正在载入","请稍候…")}</div>`;
  try { await ({dashboard:renderDashboard,subscriptions:renderSubscriptions,nodes:renderNodes,templates:renderTemplates,logs:renderLogs,settings:renderSettings}[chosen])(); }
  catch(error){if ((location.hash||"#dashboard").slice(1)===chosen) {$("#content").innerHTML=`<div class="panel">${empty("加载失败",error.message)}</div>`;showToast(error.message,true);}}
}

function showing(page) { return (location.hash || "#dashboard").slice(1) === page; }

async function renderDashboard(){
  const {totals,recent}=await api("/api/dashboard");
  if (!showing("dashboard")) return;
  $("#content").innerHTML=`
    <section class="hero"><div><h3>欢迎回来，${escapeHtml(state.user)}</h3><p>集中管理私人订阅、节点分发和访问边界。</p></div><button class="btn primary" id="quickSub">＋ 新建订阅</button></section>
    <section class="stats">
      ${stat("订阅总数",totals.subscriptions,"↗")}${stat("有效订阅",totals.active,"✓")}${stat("节点数量",totals.nodes,"◇")}${stat("24h 已拦截",totals.blocked_24h,"⊘")}
    </section>
    <section class="grid-two">
      <div class="panel"><div class="panel-head"><div><h3>最近访问</h3><p>允许与拦截记录</p></div><a class="link-btn" href="#logs">查看全部</a></div>
        <div class="activity">${recent.length?recent.map(log=>`<div class="activity-item"><i class="dot" style="color:${log.allowed?'#10b981':'#ef4444'}"></i><div><p>${escapeHtml(log.subscription_name||"未知订阅")} · ${escapeHtml(log.target)}</p><small>${escapeHtml(log.ip)} · ${escapeHtml(log.reason)}</small></div><small>${fmtDate(log.happened_at)}</small></div>`).join(""):empty("暂无访问","订阅被拉取后会显示在这里")}</div>
      </div>
      <div class="panel"><div class="panel-head"><div><h3>累计已上报流量</h3><p>由节点采集器主动上报</p></div></div><strong style="font-size:32px">${fmtBytes(totals.traffic_used)}</strong><div class="notice warning" style="margin-top:20px">面板无法从普通节点链接自动读取真实流量。接入采集器后，配额才可对代理流量准确计数。</div></div>
    </section>`;
  $("#quickSub").addEventListener("click",()=>{state.pendingCreateSubscription=true;location.hash="#subscriptions";});
}
function stat(label,value,icon){return `<div class="stat-card"><div class="stat-top"><span>${label}</span><i class="stat-badge">${icon}</i></div><strong>${value}</strong></div>`;}

async function renderNodes(){
  const request=++state.nodeRequest;
  const data=await api(`/api/nodes?${listQuery("nodes")}`),nodes=data.items;
  if (!showing("nodes") || request!==state.nodeRequest) return;
  state.nodes=nodes;
  if(data.page>data.pages){listState.nodes.page=data.pages;return renderNodes();}
  $("#content").innerHTML=`<section class="panel"><div class="panel-head"><div><h3>节点列表</h3><p>支持 SS、VMess、VLESS、Trojan、Hysteria2 与 TUIC</p></div><div class="toolbar"><button class="btn primary" id="addNode">＋ 添加 / 批量导入</button></div></div>
    ${filtersBar("nodes",data,"搜索名称、分组或链接",[["all","全部状态"],["enabled","已启用"],["disabled","已停用"]])}
    <div class="bulk-bar"><span id="selectedCount">已选择 0 条</span><div><button class="btn small bulk-action" data-action="enable">启用</button><button class="btn small bulk-action" data-action="disable">停用</button><button class="btn small bulk-action" data-action="group">移动分组</button><button class="btn small danger bulk-action" data-action="delete">批量删除</button></div></div>
    <div class="table-wrap"><table class="table"><thead><tr><th class="check-col"><input id="selectPage" type="checkbox" aria-label="选择本页"></th><th>节点</th><th>协议</th><th>分组</th><th>链接</th><th>状态</th><th></th></tr></thead><tbody>${nodes.map(n=>`<tr><td class="check-col"><input class="nodes-select" type="checkbox" value="${n.id}" aria-label="选择 ${escapeHtml(n.name)}"></td><td><div class="cell-main">${escapeHtml(n.name)}</div><div class="cell-sub">#${n.id} · ${fmtDate(n.created_at)}</div></td><td>${badge(n.uri.split(":",1)[0].toUpperCase())}</td><td>${escapeHtml(n.group_name)}</td><td><div class="truncate">${escapeHtml(n.uri)}</div></td><td>${badge(n.enabled?"启用":"停用",n.enabled?"ok":"bad")}</td><td><div class="actions"><button class="link-btn edit-node" data-id="${n.id}">编辑</button><button class="link-btn delete-node" data-id="${n.id}">删除</button></div></td></tr>`).join("")}</tbody></table>${nodes.length?"":empty("没有匹配的节点","调整搜索条件或添加新节点")}</div>${paginationBar("nodes",data)}</section>`;
  $("#addNode").addEventListener("click",()=>nodeModal());
  bindListControls("nodes",data,renderNodes);bindBulkControls("nodes",renderNodes);
  bindAction(".edit-node",el=>nodeModal(nodes.find(n=>n.id===Number(el.dataset.id))));
  bindAction(".delete-node",async el=>{if(!confirmAction("删除节点后，它会从所有订阅中移除。继续吗？"))return;el.disabled=true;try{await api(`/api/nodes/${el.dataset.id}`,{method:"DELETE",body:"{}"});state.nodeOptionsAt=0;await renderNodes();showToast("节点已删除");}catch(e){el.disabled=false;showToast(e.message,true);}});
}
function nodeModal(node=null){
  openModal(node?"编辑节点":"添加节点",`<form id="nodeForm" class="form-grid">
    ${node?`<label>节点名称<input name="name" value="${escapeHtml(node.name)}" required></label>`:""}
    <label>分组<input name="group_name" value="${escapeHtml(node?.group_name||"默认")}" required></label>
    <label class="switch-line"><input type="checkbox" name="enabled" ${node?.enabled===0?"":"checked"}>启用节点</label>
    <label class="span-2">${node?"节点链接":"节点链接（每行一条，可批量导入）"}<textarea name="uris" class="code-area" required>${escapeHtml(node?.uri||"")}</textarea><p class="help">节点机密会存入 SQLite，请保护数据库备份。</p></label>
    <div class="form-actions span-2"><button type="button" class="btn ghost modal-cancel">取消</button><button class="btn primary" type="submit">${node?"保存":"导入节点"}</button></div></form>`,node?"节点":"批量导入");
  $(".modal-cancel").addEventListener("click",closeModal);
  $("#nodeForm").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);const lines=String(fd.get("uris")).split(/\r?\n/).map(x=>x.trim()).filter(Boolean);const payload={name:fd.get("name")||"",group_name:fd.get("group_name"),enabled:fd.get("enabled")==="on",...(node?{uri:lines[0]}:{uris:lines})};try{await api(node?`/api/nodes/${node.id}`:"/api/nodes",{method:node?"PUT":"POST",body:JSON.stringify(payload)});state.nodeOptionsAt=0;closeModal();showToast(node?"节点已更新":`已导入 ${lines.length} 个节点`);await renderNodes();}catch(err){showToast(err.message,true);}});
}

async function loadAllNodes(){
  if (state.nodeOptionsAt && Date.now()-state.nodeOptionsAt < 60000) return state.allNodes;
  if (!state.nodeOptionsPromise) state.nodeOptionsPromise=(async()=>{
    const first=await api("/api/nodes/options?page=1&page_size=1000");
    const all=[...first.items];
    for(let page=2;page<=first.pages;page++) {
      const next=await api(`/api/nodes/options?page=${page}&page_size=1000`);
      all.push(...next.items);
    }
    state.allNodes=all;state.nodeOptionsAt=Date.now();return all;
  })();
  try {return await state.nodeOptionsPromise;} finally {state.nodeOptionsPromise=null;}
}

async function renderSubscriptions(){
  const request=++state.subscriptionRequest;
  const [data]=await Promise.all([api(`/api/subscriptions?${listQuery("subscriptions")}`),loadAllNodes()]);
  if (!showing("subscriptions") || request!==state.subscriptionRequest) return;
  state.subscriptions=data.items;state.templates=data.templates;
  if(data.page>data.pages){listState.subscriptions.page=data.pages;return renderSubscriptions();}
  const rows=state.subscriptions.map(sub=>{const st=statusOf(sub),pct=sub.traffic_limit_bytes?Math.min(100,sub.traffic_used_bytes/sub.traffic_limit_bytes*100):0;return `<tr><td class="check-col"><input class="subscriptions-select" type="checkbox" value="${sub.id}" aria-label="选择 ${escapeHtml(sub.name)}"></td><td><div class="cell-main">${escapeHtml(sub.name)}</div><div class="cell-sub">${escapeHtml(sub.group_name)} · ${sub.node_ids.length} 个节点 · #${sub.id}</div></td><td>${badge(st[0],st[1])}</td><td><div>${fmtBytes(sub.traffic_used_bytes)} / ${sub.traffic_limit_bytes?fmtBytes(sub.traffic_limit_bytes):"不限"}</div><div class="progress"><i style="width:${pct}%"></i></div></td><td><div>IP ${sub.ip_limit||"不限"} · 设备 ${sub.device_limit||"不限"}</div><div class="cell-sub">${sub.access_window_hours} 小时活跃窗口</div></td><td>${fmtDate(sub.expires_at)}</td><td><div class="actions"><button class="link-btn links-sub" data-id="${sub.id}">链接</button><button class="link-btn access-sub" data-id="${sub.id}">访问</button><button class="link-btn edit-sub" data-id="${sub.id}">编辑</button><button class="link-btn delete-sub" data-id="${sub.id}">删除</button></div></td></tr>`;}).join("");
  $("#content").innerHTML=`<section class="panel"><div class="panel-head"><div><h3>订阅列表</h3><p>每个订阅拥有独立随机 Token 与访问策略</p></div><button class="btn primary" id="addSubscription">＋ 新建订阅</button></div>${filtersBar("subscriptions",data,"搜索名称、分组或备注",[["all","全部状态"],["active","有效"],["disabled","已停用"],["expired","已到期"],["exhausted","流量耗尽"]])}<div class="bulk-bar"><span id="selectedCount">已选择 0 条</span><div><button class="btn small bulk-action" data-action="enable">启用</button><button class="btn small bulk-action" data-action="disable">停用</button><button class="btn small bulk-action" data-action="group">移动分组</button><button class="btn small danger bulk-action" data-action="delete">批量删除</button></div></div><div class="table-wrap"><table class="table"><thead><tr><th class="check-col"><input id="selectPage" type="checkbox" aria-label="选择本页"></th><th>订阅</th><th>状态</th><th>已上报流量</th><th>访问限制</th><th>到期时间</th><th></th></tr></thead><tbody>${rows}</tbody></table>${rows?"":empty("没有匹配的订阅","调整搜索条件或新建订阅")}</div>${paginationBar("subscriptions",data)}</section>`;
  $("#addSubscription").addEventListener("click",()=>subscriptionModal());
  if (state.pendingCreateSubscription) { state.pendingCreateSubscription=false; $("#addSubscription").click(); }
  bindListControls("subscriptions",data,renderSubscriptions);bindBulkControls("subscriptions",renderSubscriptions);
  bindAction(".edit-sub",el=>subscriptionModal(state.subscriptions.find(s=>s.id===Number(el.dataset.id))));
  bindAction(".links-sub",el=>linksModal(state.subscriptions.find(s=>s.id===Number(el.dataset.id))));
  bindAction(".access-sub",el=>accessModal(state.subscriptions.find(s=>s.id===Number(el.dataset.id))));
  bindAction(".delete-sub",async el=>{if(!confirmAction("确认永久删除这个订阅及其访问记录？"))return;el.disabled=true;try{await api(`/api/subscriptions/${el.dataset.id}`,{method:"DELETE",body:"{}"});await renderSubscriptions();showToast("订阅已删除");}catch(e){el.disabled=false;showToast(e.message,true);}});
}
function subscriptionModal(sub=null){
  const checked=new Set(sub?.node_ids||[]);
  const nodeGroups=[...new Set(state.allNodes.map(node=>node.group_name))].sort();
  openModal(sub?"编辑订阅":"新建订阅",`<form id="subForm" class="form-grid">
    <label>订阅名称<input name="name" value="${escapeHtml(sub?.name||"")}" placeholder="例如：小林的订阅" required></label>
    <label>分组<input name="group_name" value="${escapeHtml(sub?.group_name||"默认")}" placeholder="例如：设计组" required></label>
    <label>到期时间<input name="expires_at" type="datetime-local" value="${localInputDate(sub?.expires_at)}"><p class="help">留空表示永久</p></label>
    <label>流量上限（GB）<input name="traffic_gb" type="number" min="0" step="0.1" value="${sub?.traffic_limit_bytes?(sub.traffic_limit_bytes/1024**3).toFixed(1):0}"><p class="help">0 表示不限；需节点上报才准确</p></label>
    <label>活跃窗口（小时）<input name="access_window_hours" type="number" min="1" max="720" value="${sub?.access_window_hours||24}" required></label>
    <label>IP 数量上限<input name="ip_limit" type="number" min="0" value="${sub?.ip_limit||0}"><p class="help">0 表示不限</p></label>
    <label>设备数量上限<input name="device_limit" type="number" min="0" value="${sub?.device_limit||0}"><p class="help">客户端不发送设备 ID 时以 User-Agent 近似识别</p></label>
    <label class="switch-line span-2"><input type="checkbox" name="enabled" ${sub?.enabled===0?"":"checked"}>立即启用订阅</label>
    <div class="span-2"><p class="field-label">选择节点</p><div class="picker-tools"><input id="nodePickerSearch" placeholder="搜索节点名称"><select id="nodePickerGroup"><option value="">全部节点分组</option>${nodeGroups.map(group=>`<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("")}</select><button class="btn small" id="selectVisibleNodes" type="button">选择筛选结果</button><button class="btn small ghost" id="clearSelectedNodes" type="button">清空</button></div><div class="checkbox-row" id="nodeChoices">${state.allNodes.length?state.allNodes.map(n=>`<label class="check-chip picker-node" data-search="${escapeHtml(n.name.toLowerCase())}" data-group="${escapeHtml(n.group_name)}"><input type="checkbox" name="node_ids" value="${n.id}" ${checked.has(n.id)?"checked":""}>${escapeHtml(n.name)} <small>${escapeHtml(n.group_name)}</small></label>`).join(""):"请先添加节点"}</div></div>
    <div class="notice warning span-2">普通节点链接含有可复制的静态凭证，用户导入后仍能分享单节点。此处的 IP/设备限制保护订阅下载入口；要限制已导出的节点，必须给每份订阅分配独立节点凭证并在节点侧执行并发 IP/吊销策略。</div>
    <label class="span-2">备注<textarea name="notes" placeholder="仅管理员可见">${escapeHtml(sub?.notes||"")}</textarea></label>
    <div class="form-actions span-2"><button type="button" class="btn ghost modal-cancel">取消</button><button class="btn primary" type="submit">保存订阅</button></div></form>`,sub?"订阅策略":"创建独立 Token");
  $(".modal-cancel").addEventListener("click",closeModal);
  const filterNodes=()=>{const q=$("#nodePickerSearch").value.trim().toLowerCase(),group=$("#nodePickerGroup").value;$$('.picker-node').forEach(item=>item.classList.toggle("hidden",Boolean((q&&!item.dataset.search.includes(q))||(group&&item.dataset.group!==group))));};
  $("#nodePickerSearch").addEventListener("input",filterNodes);$("#nodePickerGroup").addEventListener("change",filterNodes);
  $("#selectVisibleNodes").addEventListener("click",()=>{$$('.picker-node:not(.hidden) input').forEach(input=>{input.checked=true;});});
  $("#clearSelectedNodes").addEventListener("click",()=>{$$('#nodeChoices input').forEach(input=>{input.checked=false;});});
  $("#subForm").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);const expiry=fd.get("expires_at");const payload={name:fd.get("name"),group_name:fd.get("group_name"),expires_at:expiry?new Date(expiry).toISOString():null,traffic_limit_bytes:Math.round(Number(fd.get("traffic_gb")||0)*1024**3),access_window_hours:Number(fd.get("access_window_hours")),ip_limit:Number(fd.get("ip_limit")||0),device_limit:Number(fd.get("device_limit")||0),enabled:fd.get("enabled")==="on",node_ids:fd.getAll("node_ids").map(Number),notes:fd.get("notes")};try{await api(sub?`/api/subscriptions/${sub.id}`:"/api/subscriptions",{method:sub?"PUT":"POST",body:JSON.stringify(payload)});closeModal();showToast(sub?"订阅已更新":"订阅已创建");renderSubscriptions();}catch(err){showToast(err.message,true);}});
}
function linksModal(sub){
  openModal(`${sub.name} 的订阅链接`,`<div class="notice">“自动识别”会根据客户端 User-Agent 返回 Clash、sing-box、Surge 或通用 Base64。每一种地址都可以复制或显示二维码。</div><div class="link-list" style="margin-top:14px">${sub.links.map(l=>`<div class="link-row"><strong>${escapeHtml(l.name)}</strong><code>${escapeHtml(l.url)}</code><div class="link-actions"><button class="btn small copy-link" data-url="${escapeHtml(l.url)}">复制</button><button class="btn small success qr-link" data-url="${escapeHtml(l.url)}" data-name="${escapeHtml(l.name)}">二维码</button></div></div>`).join("")}</div><div id="qrPreview" class="qr-preview hidden"><img id="qrImage" alt="订阅二维码"><div><p class="eyebrow">SCAN TO SUBSCRIBE</p><h4 id="qrTitle"></h4><p id="qrText" class="muted"></p><p class="help">用对应客户端的二维码扫描功能导入。二维码只在本面板生成，不会把订阅 Token 发送给第三方。</p></div></div><div class="form-actions" style="margin-top:17px"><button class="btn danger" id="rotateToken">使旧链接失效并重建</button><button class="btn" id="resetTraffic">重置流量</button><button class="btn ghost modal-cancel">关闭</button></div>`,"安全分发");
  bindAction(".copy-link",async el=>{try{await navigator.clipboard.writeText(el.dataset.url);showToast("链接已复制");}catch{showToast("浏览器未允许复制，请手动选择",true);}});$(".modal-cancel").addEventListener("click",closeModal);
  bindAction(".qr-link",el=>{const box=$("#qrPreview");$("#qrTitle").textContent=el.dataset.name;$("#qrText").textContent=el.dataset.url;$("#qrImage").src=`/vault/api/qr?data=${encodeURIComponent(el.dataset.url)}&title=${encodeURIComponent(el.dataset.name)}`;box.classList.remove("hidden");box.scrollIntoView({behavior:"smooth",block:"nearest"});});
  $("#rotateToken").addEventListener("click",async()=>{if(!confirmAction("重建后所有旧链接会失效。确认继续？"))return;try{await api(`/api/subscriptions/${sub.id}/rotate`,{method:"POST",body:"{}"});closeModal();showToast("Token 已重建");renderSubscriptions();}catch(e){showToast(e.message,true);}});
  $("#resetTraffic").addEventListener("click",async()=>{if(!confirmAction("确认把这个订阅的已上报流量清零？"))return;try{await api(`/api/subscriptions/${sub.id}/reset-traffic`,{method:"POST",body:"{}"});closeModal();showToast("流量已重置");renderSubscriptions();}catch(e){showToast(e.message,true);}});
}
async function accessModal(sub){
  try{const {items}=await api(`/api/subscriptions/${sub.id}/access`);openModal(`${sub.name} 的活跃访问`,`<div class="notice warning">设备限制优先读取 X-Device-ID / X-Client-ID；普通代理客户端通常不发送，因此会退化为 User-Agent 近似值。</div><div class="table-wrap" style="margin-top:12px"><table class="table"><thead><tr><th>类型</th><th>标识</th><th>首次</th><th>最后</th><th>次数</th></tr></thead><tbody>${items.map(x=>`<tr><td>${badge(x.kind==="ip"?"IP":"设备",x.kind==="ip"?"blue":"ok")}</td><td>${escapeHtml(x.label)}</td><td>${fmtDate(x.first_seen_at)}</td><td>${fmtDate(x.last_seen_at)}</td><td>${x.hits}</td></tr>`).join("")}</tbody></table>${items.length?"":empty("暂无记录","订阅被访问后会显示")}</div><div class="form-actions"><button class="btn danger" id="clearAccess">清空绑定</button><button class="btn ghost modal-cancel">关闭</button></div>`,"访问边界");$(".modal-cancel").addEventListener("click",closeModal);$("#clearAccess").addEventListener("click",async()=>{if(!confirmAction("确认清空这个订阅的 IP 与设备绑定？"))return;try{await api(`/api/subscriptions/${sub.id}/clear-access`,{method:"POST",body:"{}"});closeModal();showToast("访问绑定已清空");}catch(e){showToast(e.message,true);}});}catch(e){showToast(e.message,true);}
}

async function renderTemplates(){
  state.templates=(await api("/api/templates")).items;
  if (!showing("templates")) return;
  $("#content").innerHTML=`<section class="panel"><div class="panel-head"><div><h3>输出模板</h3><p>常用客户端模板已内置，可复制并定制</p></div><button class="btn primary" id="addTemplate">＋ 新建模板</button></div><div class="table-wrap"><table class="table"><thead><tr><th>模板</th><th>目标客户端</th><th>属性</th><th>更新时间</th><th></th></tr></thead><tbody>${state.templates.map(t=>`<tr><td class="cell-main">${escapeHtml(t.name)}</td><td>${badge(t.target.toUpperCase())}</td><td>${badge(t.builtin?"内置":"自定义",t.builtin?"ok":"blue")}</td><td>${fmtDate(t.updated_at)}</td><td><div class="actions"><button class="link-btn edit-template" data-id="${t.id}">编辑</button>${t.builtin?"":`<button class="link-btn delete-template" data-id="${t.id}">删除</button>`}</div></td></tr>`).join("")}</tbody></table></div></section>`;
  $("#addTemplate").addEventListener("click",()=>templateModal());bindAction(".edit-template",el=>templateModal(state.templates.find(t=>t.id===Number(el.dataset.id))));bindAction(".delete-template",async el=>{if(!confirmAction("确认删除这个自定义模板？"))return;el.disabled=true;try{await api(`/api/templates/${el.dataset.id}`,{method:"DELETE",body:"{}"});await renderTemplates();showToast("模板已删除");}catch(e){el.disabled=false;showToast(e.message,true);}});
}
function templateModal(t=null){
  openModal(t?"编辑模板":"新建模板",`<form id="templateForm" class="form-grid"><label>模板名称<input name="name" value="${escapeHtml(t?.name||"")}" required></label><label>输出类型<select name="target"><option value="base64">Base64</option><option value="clash">Clash Meta</option><option value="singbox">sing-box</option><option value="surge">Surge</option></select></label><label class="span-2">模板内容<textarea name="content" class="code-area">${escapeHtml(t?.content||"")}</textarea><p class="help">Clash：{{PROXIES}} / {{PROXY_NAMES}}；sing-box：{{OUTBOUNDS}} / {{FIRST_TAG}}；Surge：{{SURGE_PROXIES}} / {{SURGE_NAMES}}</p></label><div class="form-actions span-2"><button type="button" class="btn ghost modal-cancel">取消</button><button class="btn primary">保存模板</button></div></form>`,t?.builtin?"内置模板":"自定义输出");$("#templateForm select").value=t?.target||"clash";$(".modal-cancel").addEventListener("click",closeModal);$("#templateForm").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);try{await api(t?`/api/templates/${t.id}`:"/api/templates",{method:t?"PUT":"POST",body:JSON.stringify(Object.fromEntries(fd))});closeModal();showToast("模板已保存");renderTemplates();}catch(err){showToast(err.message,true);}});
}

async function renderLogs(){
  const [{items},settings]=await Promise.all([api("/api/logs?limit=300"),api("/api/settings")]);
  if (!showing("logs")) return;
  $("#content").innerHTML=`<section class="panel"><div class="panel-head"><div><h3>访问日志</h3><p>自动保留 ${settings.log_retention_days} 天且最多 ${settings.max_access_log_rows.toLocaleString()} 条；当前 ${settings.access_log_rows.toLocaleString()} 条</p></div><button class="btn ghost" id="refreshLogs">刷新</button></div><div class="table-wrap"><table class="table"><thead><tr><th>时间</th><th>订阅</th><th>结果</th><th>IP / 客户端</th><th>格式</th><th>返回大小</th></tr></thead><tbody>${items.map(l=>`<tr><td>${fmtDate(l.happened_at)}</td><td class="cell-main">${escapeHtml(l.subscription_name||"未知")}</td><td>${badge(l.reason,l.allowed?"ok":"bad")}</td><td><div>${escapeHtml(l.ip)}</div><div class="cell-sub truncate">${escapeHtml(l.device)}</div></td><td>${escapeHtml(l.target)}</td><td>${fmtBytes(l.response_bytes)}</td></tr>`).join("")}</tbody></table>${items.length?"":empty("暂无日志","拉取订阅后会在此显示")}</div></section>`;$("#refreshLogs").addEventListener("click",renderLogs);
}

async function renderSettings(){
  const s=await api("/api/settings");
  if (!showing("settings")) return;
  $("#content").innerHTML=`<section class="grid-two settings-layout"><div class="panel"><div class="panel-head"><div><h3>运行信息</h3><p>当前实例配置</p></div>${badge(`v${s.version}`,"blue")}</div><div class="settings-grid"><div><p class="eyebrow">PUBLIC URL</p><strong>${escapeHtml(s.public_url||"未设置")}</strong></div><div><p class="eyebrow">DATABASE + WAL</p><strong>${fmtBytes(s.database_bytes)}</strong></div><div><p class="eyebrow">磁盘空间</p><strong>可用 ${fmtBytes(s.disk_free_bytes)} / 共 ${fmtBytes(s.disk_total_bytes)}</strong></div><div><p class="eyebrow">自动清理</p><strong>访问日志 ${s.log_retention_days} 天 / 流量明细 ${s.usage_retention_days} 天</strong><p class="help">当前 ${s.access_log_rows.toLocaleString()} 条访问日志，${s.usage_report_rows.toLocaleString()} 条流量明细；每 ${Math.round(s.cleanup_interval_seconds/3600*10)/10} 小时检查一次。</p></div><div><p class="eyebrow">并发保护</p><strong>最多 ${s.max_workers} 个请求线程</strong><p class="help">超出时暂时返回 503，避免内存耗尽。</p></div><div><p class="eyebrow">USAGE REPORTING</p>${badge(s.usage_reporting?"已配置":"未配置",s.usage_reporting?"ok":"warn")}</div><div><p class="eyebrow">PROTOCOLS</p><p>${s.supported_protocols.map(x=>badge(x.toUpperCase())).join(" ")}</p></div></div><div class="notice warning" style="margin-top:16px">真实流量限制需要节点按订阅 Token 上报用量；普通节点链接本身不具备用户级计量能力。</div></div>
    <div class="panel"><div class="panel-head"><div><h3>管理员账号</h3><p>${state.authMode==="nexusgate"?"统一使用 NexusGate 账号":"修改账号或密码后需要重新登录"}</p></div></div>${state.authMode==="nexusgate"?`<div class="form-stack"><p>此服务由 NexusGate 会话授权。请在 NexusGate 的“设置与运维”修改账号或密码；更改后这里会同步生效。</p><button class="btn primary" id="openGateSettings" type="button">前往账号设置</button></div>`:`<form id="accountForm" class="form-stack"><label>管理员账号<input name="username" value="${escapeHtml(state.user)}" autocomplete="username" minlength="3" maxlength="64" required></label><label>当前密码<input name="current_password" type="password" autocomplete="current-password" required></label><label>新密码（留空不修改）<input name="new_password" type="password" autocomplete="new-password" minlength="10"></label><label>确认新密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="10"></label><button class="btn primary" type="submit">保存账号</button></form>`}</div></section>
    <section class="panel" style="margin-top:18px"><div class="panel-head"><div><h3>流量上报示例</h3><p>密钥从服务器 /etc/nexusgate-subvault.env 读取，不要放进公开仓库</p></div></div><div class="mono-card">curl -X POST '${escapeHtml(s.public_url)}/api/v1/usage' \\\n+  -H 'Authorization: Bearer YOUR_USAGE_REPORT_KEY' \\\n+  -H 'Content-Type: application/json' \\\n+  -d '{"subscription_token":"TOKEN","upload_bytes":1048576,"download_bytes":2097152,"source":"node-a"}'</div></section>`;
  if(state.authMode==="nexusgate") $("#openGateSettings").addEventListener("click",()=>{
    if(window.self!==window.top) window.parent.postMessage({type:"nexusgate:navigate",page:"operations"},location.origin);
    else window.location.assign("/");
  });
  else $("#accountForm").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);try{await api("/api/change-account",{method:"POST",body:JSON.stringify(Object.fromEntries(fd))});sessionStorage.setItem("subvault:last-username",String(fd.get("username")));location.reload();}catch(err){showToast(err.message,true);}});
}

(async function boot(){try{showApp(await api("/api/session"));}catch{if(state.authMode==="nexusgate")window.top.location.assign("/");else location.reload();}})();
