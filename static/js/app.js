(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行" || state === "宽限中"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁" ||
          state === "已放行" || state === "已补采"
          ? "ok"
          : state === "已失败" || state === "已过期"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${state ?? "—"}</span>`;
  }

  function flash(msg, bad = false) {
    const el = document.getElementById("flash");
    if (!el) return;
    el.textContent = msg;
    el.className = bad ? "flash bad" : "flash ok";
    clearTimeout(flash._t);
    flash._t = setTimeout(() => {
      el.textContent = "";
      el.className = "flash";
    }, 4000);
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <div id="flash" class="flash"></div>
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数、宽限天数与到期动作。宽限天数决定补采未完成时的保护窗口。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>宽限天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${p.domain_name}</td>
              <td class="mono">${p.domain_code}</td>
              <td>${p.keep_days}</td>
              <td>${p.grace_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <div id="flash" class="flash"></div>
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。宽限截止与可否销毁取自宽限登记快照，与裁决队列同源。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>宽限态</th><th>宽限截止</th><th>补采</th><th>可否销毁</th><th>归档地址</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${p.domain_code}</td>
              <td class="mono">${p.biz_date}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td>${p.grace_effective_status ? tag(p.grace_effective_status) : "—"}</td>
              <td class="mono">${p.grace_status === "宽限中" || p.grace_effective_status ? (p.grace_deadline || "—") : "—"}</td>
              <td>${p.backfill_status || "—"}</td>
              <td>${
                p.storage_state !== "在线"
                  ? "—"
                  : p.can_destroy
                    ? `<span class="tag ok">可销毁</span>`
                    : `<span class="tag bad">拦截</span>`
              }</td>
              <td class="mono">${p.archive_uri || "—"}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div id="flash" class="flash"></div>
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <p class="sub">「宽限拦截」为该批裁决中当前仍被宽限保护、销毁被拦下的条数。</p>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>宽限拦截</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.as_of_date}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.grace_blocked_count ? `<span class="tag bad">${j.grace_blocked_count}</span>` : 0}</td>
              <td>${j.message}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="6" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      try {
        await api("/api/scan-jobs", {
          method: "POST",
          body: JSON.stringify({ as_of_date }),
        });
        await renderScan();
      } catch (e) {
        flash(e.message, true);
      }
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <div id="flash" class="flash"></div>
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">宽限生效中的销毁裁决不可执行（按钮禁用并提示截止日）；归档不受宽限限制。宽限登记与裁决态按分区对齐。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>宽限截止</th><th>补采</th><th>可否执行</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.domain_code}</td>
              <td class="mono">${j.biz_date}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${j.decided_action}</td>
              <td>${tag(j.status)}</td>
              <td class="mono">${j.grace_deadline || "—"}</td>
              <td>${j.backfill_status || "—"}</td>
              <td>${
                j.status !== "待执行" && j.status !== "已失败"
                  ? "—"
                  : j.can_execute
                    ? `<span class="tag ok">可执行</span>`
                    : `<span class="tag bad" title="${j.block_reason}">拦截</span>`
              }</td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? j.can_execute
                    ? `<button type="button" data-exec="${j.id}">执行</button>`
                    : `<button type="button" class="disabled-btn" disabled title="${j.block_reason}">执行</button>`
                    : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="10" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
          await renderJudgments();
        } catch (e) {
          flash(e.message, true);
          await renderJudgments();
        }
      };
    });
  }

  async function renderGrace() {
    const [grace, parts] = await Promise.all([
      api("/api/grace"),
      api("/api/partitions"),
    ]);
    const onlineParts = parts.items.filter(
      (p) => p.storage_state === "在线"
    );
    main.innerHTML = `
      <div id="flash" class="flash"></div>
      <section class="panel">
        <h2>宽限登记</h2>
        <p class="sub">到期但补采入库未完成的在线分区可登记宽限。同一分区同时只允许一条生效登记，串行锁写；补采核验完成即放行，截止未补采则过期恢复销毁。</p>
        <div class="toolbar">
          <label class="field wide">分区
            <select id="grace-part">
              ${onlineParts
                .map(
                  (p) => `<option value="${p.id}">${p.domain_code} / ${p.biz_date}（分区#${p.id}）</option>`
                )
                .join("")}
            </select>
          </label>
          <label class="field">宽限天数（留空取策略）
            <input id="grace-days" type="number" min="1" placeholder="自动" />
          </label>
          <label class="field wide">事由 / 补采说明
            <input id="grace-reason" placeholder="如：补采任务 BF-20261116-01 未完" />
          </label>
          <button type="button" id="btn-grace-add">登记宽限</button>
        </div>
        <table>
          <thead><tr><th>编号</th><th>分区</th><th>业务日</th><th>登记态</th><th>宽限截止</th><th>补采状态</th><th>补采任务号</th><th>核验时间</th><th>可否销毁</th><th>操作</th></tr></thead>
          <tbody>
            ${
              grace.items.length
                ? grace.items
                    .map(
                      (g) => `<tr>
              <td>${g.id}</td>
              <td class="mono">${g.domain_code}#${g.partition_id}</td>
              <td class="mono">${g.biz_date}</td>
              <td>${tag(g.effective_status)}</td>
              <td class="mono">${g.grace_deadline}</td>
              <td>${tag(g.backfill_status)}</td>
              <td class="mono">${g.backfill_task_code || "—"}</td>
              <td class="mono">${g.backfill_verified_at || "—"}</td>
              <td>${
                g.can_block_destroy
                  ? `<span class="tag bad">拦截中</span>`
                  : `<span class="tag ok">可销毁</span>`
              }</td>
              <td class="grace-actions">
                ${
                  g.status === "宽限中"
                    ? `
                      <button type="button" class="secondary" data-bf="${g.id}" data-state="补采中">标记补采中</button>
                      <button type="button" data-bf="${g.id}" data-state="已补采">补采核验完成</button>
                      <button type="button" class="secondary" data-expire="${g.id}">置过期</button>
                      <button type="button" class="secondary" data-close="${g.id}">关闭</button>`
                    : "—"
                }
              </td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="10" class="empty">尚无宽限登记</td></tr>`
            }
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>补采任务核验</h2>
        <p class="sub">补采任务号随登记/更新落库可核；「补采核验完成」自动放行宽限，裁决队列随即恢复可执行。</p>
      </section>`;

    document.getElementById("btn-grace-add").onclick = async () => {
      const partition_id = Number(document.getElementById("grace-part").value);
      const daysRaw = document.getElementById("grace-days").value.trim();
      const reason = document.getElementById("grace-reason").value.trim();
      try {
        await api("/api/grace", {
          method: "POST",
          body: JSON.stringify({
            partition_id,
            reason,
            grace_days: daysRaw ? Number(daysRaw) : null,
          }),
        });
        flash("宽限登记成功，销毁裁决已拦截");
      } catch (e) {
        flash(e.message, true);
      }
      await renderGrace();
    };

    main.querySelectorAll("[data-bf]").forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.bf;
        const state = btn.dataset.state;
        const taskCode = window.prompt("补采任务号（可留空）", "") || "";
        try {
          await api(`/api/grace/${id}/backfill`, {
            method: "POST",
            body: JSON.stringify({
              backfill_status: state,
              backfill_task_code: taskCode,
            }),
          });
          flash(state === "已补采" ? "补采核验完成，宽限已放行" : "补采状态已更新");
        } catch (e) {
          flash(e.message, true);
        }
        await renderGrace();
      };
    });

    main.querySelectorAll("[data-expire]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/grace/${btn.dataset.expire}/expire`, { method: "POST" });
          flash("宽限已过期，裁决恢复可执行");
        } catch (e) {
          flash(e.message, true);
        }
        await renderGrace();
      };
    });

    main.querySelectorAll("[data-close]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/grace/${btn.dataset.close}/close`, {
            method: "POST",
            body: JSON.stringify({ note: "人工关闭" }),
          });
          flash("宽限登记已关闭");
        } catch (e) {
          flash(e.message, true);
        }
        await renderGrace();
      };
    });
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div id="flash" class="flash"></div>
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>生效宽限</th><th>错位</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${r.as_of_date}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.grace_open_count}</td>
              <td>${r.mismatch_count}</td>
              <td>${r.summary}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="9" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      try {
        await api("/api/reconcile-reports", {
          method: "POST",
          body: JSON.stringify({ as_of_date }),
        });
        await renderReconcile();
      } catch (e) {
        flash(e.message, true);
      }
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    grace: renderGrace,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
