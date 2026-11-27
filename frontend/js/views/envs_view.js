import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const EnvsView = {
  setup() {
    const items = ref([]); const err = ref(""); const msg = ref("");
    const tzForm = ref({ env_id: 3, timezone: "Asia/Shanghai" });
    async function reload() {
      err.value = "";
      try { items.value = (await api("/api/envs")).items || []; }
      catch (e) { err.value = e.message || String(e); }
    }
    async function saveTz(clear) {
      err.value = ""; msg.value = "";
      try {
        const body = { timezone: clear ? "" : tzForm.value.timezone };
        await api(`/api/envs/${tzForm.value.env_id}/timezone`, { method: "PUT", body: JSON.stringify(body) });
        msg.value = clear ? "已清除时区（跨区资格同步归零）" : "已保存时区";
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function setCross(envId, eligible) {
      err.value = ""; msg.value = "";
      try {
        await api(`/api/envs/${envId}/cross-region`, { method: "POST", body: JSON.stringify({ eligible }) });
        msg.value = eligible ? "已授予可跨区资格" : "已撤销可跨区资格";
        await reload();
      } catch (e) { err.value = e.message || String(e); await reload(); }
    }
    onMounted(reload);
    return { items, err, msg, tzForm, saveTz, setCross, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-7">
      <h2>环境</h2>
      <p class="hint">甲路径口径：缺时区 ⇒ 可跨区正式标记为零且为假，正式零增一律拒写；补齐时区后才可授予。</p>
      <div class="err" v-if="err">{{ err }}</div>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>风险等级</th><th>时区</th><th>时区校准</th><th>可跨区</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="it in items" :key="it.id">
            <td>{{ it.code }}</td><td>{{ it.title }}</td><td>{{ it.risk_level }}</td>
            <td><code>{{ it.timezone || "（空）" }}</code></td>
            <td><span class="pill" :class="it.tz_calibrated ? '' : 'danger'">{{ it.tz_calibrated ? "已校准" : "缺时区" }}</span></td>
            <td><span class="pill" :class="it.cross_region ? '' : 'warn'">{{ it.cross_region ? "可跨区" : "不可跨区" }}</span></td>
            <td>
              <button class="btn secondary" v-if="!it.cross_region" @click="setCross(it.id, true)">授予跨区</button>
              <button class="btn secondary" v-else @click="setCross(it.id, false)">撤销跨区</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel span-5">
      <h2>时区设置</h2>
      <p class="hint">时区是跨区排期对钟的基准；清除时区会同事务把该环境的正式跨区资格归零。</p>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="tzForm.env_id" /></div>
      <div class="field"><label>时区（IANA 名称或 UTC）</label><input v-model="tzForm.timezone" /></div>
      <button class="btn" @click="saveTz(false)">保存时区</button>
      <button class="btn secondary" style="margin-left:0.5rem" @click="saveTz(true)">清除时区</button>
    </div>
  </div>`
};
