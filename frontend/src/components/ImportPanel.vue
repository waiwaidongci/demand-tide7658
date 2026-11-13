<script setup lang="ts">
import {computed, ref} from 'vue';
import type {ImportPreview, ImportResult, ImportRow, ImportRowStatus, ImportBatchMeta} from '../types/domain';
import {previewImport, commitImport, listImportBatches, getImportBatch} from '../api/series';
import {importStatusMeta, dispositionOrder, checkTotal} from '../constants/imports';
import {number} from '../utils/format';
import {useTask} from '../hooks/useTask';
import ErrorNotice from './common/ErrorNotice.vue';

const props = defineProps<{seriesId: number}>();
const emit = defineEmits<{ (e: 'imported'): void }>();
const {error, busy, execute} = useTask();

const content = ref('');
const filename = ref<string | null>(null);
const idemKey = ref(newIdemKey());
const preview = ref<ImportPreview | null>(null);
const result = ref<ImportResult | null>(null);
const batches = ref<ImportBatchMeta[]>([]);
const filter = ref<ImportRowStatus | 'all'>('all');

function newIdemKey(): string {
  const rand = (window.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`);
  return `import-${props.seriesId}-${rand}`.slice(0, 80);
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  filename.value = file.name;
  content.value = await file.text();
  resetPreview();
  input.value = '';
}

function resetPreview() {
  preview.value = null;
  result.value = null;
}

const doPreview = () => execute(async () => {
  result.value = null;
  preview.value = await previewImport(props.seriesId, content.value);
});

const doCommit = () => execute(async () => {
  result.value = await commitImport(props.seriesId, content.value, idemKey.value, filename.value);
  preview.value = null;
  await loadBatches();
  emit('imported');
});

const loadBatches = () => execute(async () => {
  batches.value = await listImportBatches(props.seriesId);
});

const openBatch = (id: number) => execute(async () => {
  result.value = await getImportBatch(props.seriesId, id);
  preview.value = null;
});

const shownRows = computed<ImportRow[]>(() => {
  const source = result.value?.rows ?? preview.value?.rows ?? [];
  return filter.value === 'all' ? source : source.filter(r => r.status === filter.value);
});

const shownSummary = computed(() => result.value?.summary ?? preview.value?.summary ?? null);
const totalOk = computed(() => (shownSummary.value ? checkTotal(shownSummary.value) : true));

const summaryCards = computed(() => {
  if (!shownSummary.value) return [];
  return dispositionOrder
    .map(status => ({status, count: shownSummary.value![status], meta: importStatusMeta[status]}))
    .filter(card => card.count > 0);
});

function resetForm() {
  content.value = '';
  filename.value = null;
  idemKey.value = newIdemKey();
  resetPreview();
}
defineExpose({loadBatches});
loadBatches();
</script>

<template>
  <section class="import-panel">
    <div class="section-heading">
      <h2>修订日报批量导入</h2>
      <span>按报表版本保留每个日期的最高版本观测</span>
    </div>
    <ErrorNotice :message="error" />
    <div class="import-controls">
      <label class="grow">CSV 文件（可选，也可直接粘贴文本）
        <input type="file" accept=".csv,text/csv,text/plain" @change="onFile" />
      </label>
      <label>批次幂等键（重复/并发提交不新增观测）
        <input v-model="idemKey" maxlength="80" required />
      </label>
    </div>
    <label>报表内容：表头需含日期列（day/date/日期）、需求量列（value/demand/需求量），版本列可选（report_version/version/报表版本，缺省为 0）
      <textarea v-model="content" rows="7" placeholder="day,value,report_version&#10;2026-11-10,152.5,2&#10;2026-11-11,148,2" @input="resetPreview"></textarea>
    </label>
    <div class="inline-form">
      <button class="primary" :disabled="busy || !content.trim()" @click="doPreview">逐行预览</button>
      <button :disabled="busy || !content.trim()" @click="doCommit">确认导入</button>
      <button :disabled="busy" @click="resetForm">清空</button>
      <small v-if="filename">已载入文件：{{ filename }}</small>
    </div>

    <div v-if="shownSummary" class="reconcile" :class="{broken: !totalOk}">
      <div class="reconcile-total"><b>{{ shownSummary.total }}</b><span>输入总行数</span></div>
      <div class="reconcile-cards">
        <span v-for="card in summaryCards" :key="card.status" class="disposition" :class="card.meta.kind">
          {{ card.meta.label }} <b>{{ card.count }}</b>
        </span>
      </div>
      <small v-if="totalOk">核对：{{ shownSummary.total }} = {{ shownSummary.inserted }} 新增 + {{ shownSummary.updated }} 更正 + {{ shownSummary.skipped }} 跳过 + {{ shownSummary.conflict }} 冲突 + {{ shownSummary.bad }} 坏行</small>
      <small v-else class="bad-text">处置数与输入总数不一致，请重新预览后再导入</small>
      <small v-if="result?.replayed">该批次为幂等重放，返回首次导入的处置结果，未新增观测。</small>
    </div>

    <div v-if="shownRows.length" class="table-wrap import-table">
      <div class="filter-bar">
        <button class="text" :class="{active: filter==='all'}" @click="filter='all'">全部 {{ shownSummary?.total ?? 0 }}</button>
        <button v-for="status in dispositionOrder" :key="status" class="text"
                :class="{active: filter===status}" @click="filter=status">
          {{ importStatusMeta[status].label }} {{ shownSummary?.[status] ?? 0 }}
        </button>
      </div>
      <table>
        <thead><tr><th>行</th><th>日期</th><th>需求量</th><th>版本</th><th>处置</th><th>原因 / 台账现值</th></tr></thead>
        <tbody>
          <tr v-for="row in shownRows" :key="row.line" :class="`row-${row.status}`">
            <td>{{ row.line }}</td>
            <td>{{ row.day ?? (row.raw_day || '—') }}</td>
            <td>{{ row.value === null ? row.raw_value || '—' : number(row.value) }}</td>
            <td>{{ row.report_version === null ? (row.raw_version || '—') : `v${row.report_version}` }}</td>
            <td><span class="badge disposition" :class="importStatusMeta[row.status].kind">{{ importStatusMeta[row.status].label }}</span></td>
            <td class="reason-cell">
              <span v-if="row.reason">{{ row.reason }}</span>
              <span v-else-if="row.current_version !== null">台账现值：v{{ row.current_version }} / {{ number(row.current_value ?? undefined) }}</span>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="batches.length" class="batch-history">
      <div class="section-heading"><h2>导入批次记录</h2><button class="text" @click="loadBatches">刷新 ↻</button></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>文件 / 幂等键</th><th>输入</th><th>新增</th><th>更正</th><th>跳过</th><th>冲突/坏行</th><th></th></tr></thead>
          <tbody>
            <tr v-for="b in batches" :key="b.id">
              <td>{{ b.id }}</td>
              <td><strong>{{ b.filename ?? '粘贴文本' }}</strong><small>{{ b.idem_key }}</small></td>
              <td>{{ b.total_rows }}</td>
              <td>{{ b.summary.inserted }}</td>
              <td>{{ b.summary.updated }}</td>
              <td>{{ b.summary.skipped }}</td>
              <td>{{ b.summary.conflict }} / {{ b.summary.bad }}</td>
              <td><button class="text" @click="openBatch(b.id)">查看逐行结果 ↗</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
