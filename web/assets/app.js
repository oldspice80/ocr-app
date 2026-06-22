const state = {
  route: location.hash.replace('#', '') || 'dashboard',
  dashboard: null,
  config: null,
  problems: [],
  reviewIndex: 0,
  selected: new Set(JSON.parse(localStorage.getItem('mathbank-selected') || '[]')),
  pollTimer: null,
  uploadMode: 'manual',
  manual: null,
};

const app = document.querySelector('#app');
const modal = document.querySelector('#modal');
const modalContent = document.querySelector('#modal-content');

const titles = {
  dashboard: ['WORKSPACE', '오늘의 문제은행'],
  upload: ['DOCUMENT INTAKE', 'PDF에서 문제 가져오기'],
  review: ['QUALITY CONTROL', '추출 결과 검수'],
  bank: ['PROBLEM LIBRARY', '문제은행'],
  exams: ['ASSESSMENT BUILDER', '시험지 제작'],
  settings: ['OCR CONNECTION', 'OCR 설정'],
  regions: ['MANUAL SEGMENTATION', '문제 영역 직접 지정'],
};

function esc(value = '') {
  return String(value).replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function fmtDate(value) {
  if (!value) return '-';
  return new Intl.DateTimeFormat('ko-KR', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}).format(new Date(value));
}

function statusLabel(status) {
  return ({queued:'대기', preparing_pages:'페이지 준비 중', region_setup:'영역 지정', processing:'추출 중', review:'검수 필요', approved:'승인 완료', failed:'실패', needs_review:'검수 필요', rejected:'제외'}[status] || status);
}

function toast(message, type = '') {
  const item = document.createElement('div');
  item.className = `toast ${type}`;
  item.textContent = message;
  document.querySelector('#toast-stack').append(item);
  setTimeout(() => item.remove(), 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body && typeof options.body === 'string'
      ? {'Content-Type':'application/json', ...(options.headers || {})}
      : (options.headers || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `요청 실패 (${response.status})`);
  return data;
}

function renderMath(root = document) {
  if (!window.renderMathInElement) return;
  root.querySelectorAll('.math-render').forEach(element => {
    try {
      renderMathInElement(element, {
        delimiters: [
          {left: '\\[', right: '\\]', display: true},
          {left: '\\(', right: '\\)', display: false},
          {left: '$$', right: '$$', display: true},
          {left: '$', right: '$', display: false},
        ],
        throwOnError: false,
      });
    } catch (_) {}
  });
}

function setSelected(id, selected) {
  selected ? state.selected.add(Number(id)) : state.selected.delete(Number(id));
  localStorage.setItem('mathbank-selected', JSON.stringify([...state.selected]));
  document.querySelectorAll('[data-selection-count]').forEach(el => el.textContent = `${state.selected.size}문제 선택`);
  document.querySelectorAll('[data-delete-selected-problems]').forEach(button => button.disabled = state.selected.size === 0);
}

function go(route) {
  location.hash = route;
}

async function navigate(route, force = false) {
  const [baseRoute, routeParam] = String(route).split(':');
  if (!titles[baseRoute]) route = 'dashboard';
  const [base, param] = String(route).split(':');
  if (!force && state.route === route && app.dataset.ready === 'true') return;
  state.route = route;
  clearTimeout(state.pollTimer);
  document.body.classList.remove('menu-open');
  document.querySelectorAll('.side-nav button').forEach(button => button.classList.toggle('active', button.dataset.route === base));
  document.querySelector('#page-eyebrow').textContent = titles[base][0];
  document.querySelector('#page-title').textContent = titles[base][1];
  app.dataset.ready = 'false';
  app.innerHTML = '<div class="loading-state"><span class="spinner"></span><p>자료를 정리하고 있습니다.</p></div>';
  try {
    if (!state.config) state.config = await api('/api/config');
    const handlers = {dashboard: renderDashboard, upload: renderUpload, review: renderReview, bank: renderBank, exams: renderExams, settings: renderSettings};
    if (base === 'regions') await renderRegions(Number(param));
    else await handlers[base]();
    app.dataset.ready = 'true';
    app.focus({preventScroll:true});
  } catch (error) {
    app.innerHTML = `<div class="empty-state"><h2>화면을 불러오지 못했습니다.</h2><p>${esc(error.message)}</p><button class="primary-btn" data-retry>다시 시도</button></div>`;
    toast(error.message, 'error');
  }
}

function docRows(documents) {
  if (!documents.length) return '<div class="empty-state" style="min-height:210px"><p>아직 등록된 PDF가 없습니다.</p></div>';
  return documents.map(doc => {
    const progress = ['processing','queued','preparing_pages'].includes(doc.status) ? (doc.progress || 0) : 100;
    const manualReady = doc.status === 'region_setup' || doc.region_stage === 'ready';
    const canPrepare = !['queued','processing','preparing_pages'].includes(doc.status);
    const canReprocess = Boolean(state.config?.providers?.mathpix?.configured) && ['review','approved','failed'].includes(doc.status);
    const actions = [
      manualReady ? `<button data-open-regions="${doc.id}">영역 지정</button>` : '',
      canPrepare && !manualReady ? `<button data-prepare-regions="${doc.id}" data-problem-count="${doc.problem_count || 0}">수동 재분리</button>` : '',
      canReprocess ? `<button data-reprocess-document="${doc.id}">Mathpix 재처리</button>` : '',
    ].filter(Boolean).join('');
    return `<div class="doc-row">
      <div class="doc-icon">PDF</div>
      <div class="doc-meta"><b title="${esc(doc.title)}">${esc(doc.title)}</b><small>${fmtDate(doc.created_at)} · ${esc(doc.provider || 'local-layout')}</small>
        <div class="progress-line"><i style="--value:${progress}%"></i></div></div>
      <span class="status-pill status-${esc(doc.status)}">${statusLabel(doc.status)}</span>
      <div class="doc-count">${doc.problem_count || 0}문제<br>${doc.coverage_percent || 0}% 처리</div>
      <div class="doc-actions">${actions}</div>
    </div>`;
  }).join('');
}

async function refreshDashboard() {
  state.dashboard = await api('/api/dashboard');
  document.querySelector('#review-count').textContent = state.dashboard.totals.needs_review || 0;
  return state.dashboard;
}

async function renderDashboard() {
  const data = await refreshDashboard();
  const t = data.totals;
  const reviewRatio = t.problems ? Math.round((t.approved / t.problems) * 100) : 0;
  const unitMax = Math.max(1, ...data.units.map(item => item.count));
  app.innerHTML = `
    <section class="hero">
      <div><span class="hero-kicker">ACCURACY FIRST WORKFLOW</span><h2>한 문제도 조용히<br>사라지지 않도록.</h2>
      <p>원본 좌표와 추출 결과를 함께 보존합니다. 자동 처리가 애매하면 통과시키지 않고 검수함에 남깁니다.</p></div>
      <div class="hero-action"><button data-go="upload">새 PDF 분석하기</button><small>PDF · 최대 ${state.config.max_upload_mb}MB</small></div>
    </section>
    <section class="stat-grid">
      <article class="stat-card"><small>등록된 문제</small><strong>${t.problems || 0}<span>문제</span></strong><div class="bar"><i style="--value:70%"></i></div></article>
      <article class="stat-card"><small>검수 승인</small><strong>${t.approved || 0}<span>${reviewRatio}%</span></strong><div class="bar"><i style="--value:${reviewRatio}%"></i></div></article>
      <article class="stat-card"><small>확인이 필요한 문제</small><strong>${t.needs_review || 0}<span>대기</span></strong><div class="bar"><i style="--value:${t.needs_review ? 78 : 8}%;background:#c67b25"></i></div></article>
      <article class="stat-card"><small>만든 시험지</small><strong>${t.exams || 0}<span>세트</span></strong><div class="bar"><i style="--value:${Math.min(100, (t.exams || 0) * 12)}%"></i></div></article>
    </section>
    <section class="section-grid">
      <article class="panel"><div class="panel-head"><div><h3>최근 가져온 PDF</h3><p>처리율과 검수 상태를 한눈에 확인합니다.</p></div><button class="panel-action" data-go="upload">모두 보기 →</button></div><div class="doc-list">${docRows(data.documents)}</div></article>
      <article class="panel"><div class="panel-head"><div><h3>단원 분포</h3><p>자동 분류된 문제 기준</p></div></div><div class="unit-list">
        ${data.units.length ? data.units.map(item => `<div class="unit-row"><span>${esc(item.unit)}</span><b>${item.count}</b><div class="unit-bar"><i style="--value:${Math.round(item.count / unitMax * 100)}%"></i></div></div>`).join('') : '<div class="empty-state" style="min-height:180px"><p>문제를 등록하면 단원 분포가 표시됩니다.</p></div>'}
      </div></article>
    </section>`;
  const processing = data.documents.some(doc => ['queued','processing','preparing_pages'].includes(doc.status));
  if (processing) state.pollTimer = setTimeout(() => navigate('dashboard', true), 2200);
}

async function renderUpload() {
  state.config = await api('/api/config');
  const data = await refreshDashboard();
  const providers = state.config.providers;
  app.innerHTML = `<section class="upload-layout">
    <div>
      <div class="mode-switch" role="radiogroup" aria-label="문제 분리 방식">
        <label class="mode-card ${state.uploadMode === 'manual' ? 'selected' : ''}"><input type="radio" name="upload-mode" value="manual" ${state.uploadMode === 'manual' ? 'checked' : ''}><span>추천</span><b>직접 문제 영역 지정</b><small>페이지에서 문제마다 사각형을 그린 뒤 영역별로 Mathpix OCR을 실행합니다.</small></label>
        <label class="mode-card ${state.uploadMode === 'auto' ? 'selected' : ''}"><input type="radio" name="upload-mode" value="auto" ${state.uploadMode === 'auto' ? 'checked' : ''}><b>자동 문제 분리</b><small>문제 번호와 문단을 자동 분석합니다. 2단 편집은 검수가 필요할 수 있습니다.</small></label>
      </div>
      <label class="drop-zone" id="drop-zone">
        <input id="pdf-input" type="file" accept="application/pdf,.pdf">
        <span class="upload-symbol">↥</span><h2>PDF를 이곳에 놓아주세요</h2>
        <p id="upload-mode-copy">${state.uploadMode === 'manual' ? '페이지를 먼저 준비한 뒤 직접 문제 영역을 그립니다.' : '문제 번호와 페이지 구조를 자동으로 분석합니다.'}<br>수식·도형·그래프는 문제별 원본과 함께 보존합니다.</p>
        <strong>파일 선택</strong><div class="upload-progress" id="upload-progress" hidden><div class="progress-line"><i style="--value:0%"></i></div><p>업로드 준비 중</p></div>
      </label>
      <article class="panel" style="margin-top:18px"><div class="panel-head"><div><h3>처리 중인 문서</h3><p>페이지 누락 검사를 통과한 뒤 검수함으로 이동합니다.</p></div></div><div class="doc-list">${docRows(data.documents)}</div></article>
    </div>
    <aside class="panel guide-panel"><h3>가져오기 과정</h3>
      <div class="guide-step"><span>1</span><div><b>페이지 전수 확인</b><small>PDF 전체 페이지를 이미지와 텍스트로 분해합니다.</small></div></div>
      <div class="guide-step"><span>2</span><div><b>문제 단위 분리</b><small>번호·문단·페이지 연결을 이용해 문제를 묶습니다.</small></div></div>
      <div class="guide-step"><span>3</span><div><b>수식과 도형 보존</b><small>LaTeX 본문과 원본 영역을 함께 저장합니다.</small></div></div>
      <div class="guide-step"><span>4</span><div><b>누락 검수</b><small>낮은 신뢰도와 번호 공백을 선생님께 표시합니다.</small></div></div>
      <div class="provider-box"><b style="font-size:10px">분석기 연결 상태</b>
        ${Object.values(providers).map(item => `<div class="provider-row"><span>${esc(item.label)}</span><i class="${item.configured ? 'on' : ''}" title="${item.configured ? '사용 가능' : '키 미설정'}"></i></div>`).join('')}
        <button class="provider-settings-btn" data-go="settings">Mathpix API 키 설정 →</button>
      </div>
    </aside></section>`;
  const input = document.querySelector('#pdf-input');
  const zone = document.querySelector('#drop-zone');
  input.addEventListener('change', () => input.files[0] && uploadPdf(input.files[0]));
  ['dragenter','dragover'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.add('dragging'); }));
  ['dragleave','drop'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.remove('dragging'); }));
  zone.addEventListener('drop', event => event.dataTransfer.files[0] && uploadPdf(event.dataTransfer.files[0]));
  document.querySelectorAll('input[name="upload-mode"]').forEach(radio => radio.addEventListener('change', event => {
    state.uploadMode = event.target.value;
    renderUpload();
  }));
  const processing = data.documents.some(doc => ['queued','processing','preparing_pages'].includes(doc.status));
  if (processing) state.pollTimer = setTimeout(() => navigate('upload', true), 2200);
}

function uploadPdf(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) return toast('PDF 파일만 올릴 수 있습니다.', 'error');
  const box = document.querySelector('#upload-progress');
  const bar = box.querySelector('i');
  const text = box.querySelector('p');
  box.hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', `/api/documents?filename=${encodeURIComponent(file.name)}&mode=${encodeURIComponent(state.uploadMode)}`);
  xhr.upload.onprogress = event => {
    if (!event.lengthComputable) return;
    const value = Math.round(event.loaded / event.total * 100);
    bar.style.setProperty('--value', `${value}%`);
    text.textContent = value < 100 ? `PDF 업로드 ${value}%` : '업로드 완료 · 페이지 분석을 시작합니다.';
  };
  xhr.onload = () => {
    try {
      const data = JSON.parse(xhr.responseText || '{}');
      if (xhr.status >= 400) throw new Error(data.error || '업로드에 실패했습니다.');
      if (state.uploadMode === 'manual') {
        toast('페이지를 준비하고 있습니다. 완료되면 영역 지정 화면으로 이동합니다.');
        waitForManualReady(data.document.id);
      } else {
        toast(data.duplicate ? '이미 등록된 PDF입니다.' : 'PDF 분석을 시작했습니다.');
        setTimeout(() => navigate('upload', true), 600);
      }
    } catch (error) { toast(error.message, 'error'); }
  };
  xhr.onerror = () => toast('업로드 중 연결이 끊겼습니다.', 'error');
  xhr.send(file);
}

async function waitForManualReady(documentId) {
  clearTimeout(state.pollTimer);
  try {
    const document = await api(`/api/documents/${documentId}`);
    if (document.status === 'region_setup') return go(`regions:${documentId}`);
    if (document.status === 'failed') throw new Error(document.error || '페이지 준비에 실패했습니다.');
    state.pollTimer = setTimeout(() => waitForManualReady(documentId), 900);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function renderSettings() {
  const config = await api('/api/settings/ocr');
  state.config = {...(state.config || {}), ...config};
  const mathpix = config.mathpix;
  const badgeLabel = mathpix.verified ? '연결 확인됨' : mathpix.configured ? '키 저장됨' : '연결 필요';
  app.innerHTML = `<section class="settings-layout">
    <article class="panel settings-card">
      <div class="settings-head"><div class="settings-logo">M</div><div><span>MATHPIX OCR</span><h2>수식 인식 연결</h2><p>문제 영역 이미지를 Mathpix로 보내 한국어 본문과 수식을 Mathpix Markdown·LaTeX로 받습니다.</p></div><span class="connection-badge ${mathpix.verified ? 'connected' : mathpix.configured ? 'saved' : ''}">${badgeLabel}</span></div>
      <form id="mathpix-settings-form">
        <div class="field"><label>Mathpix App ID</label><input id="mathpix-app-id" autocomplete="off" placeholder="app_id를 입력하세요" value=""><small>${mathpix.app_id_masked ? `현재 등록: ${esc(mathpix.app_id_masked)} · ${esc(mathpix.source)}` : 'Mathpix Console의 API Keys에서 확인할 수 있습니다.'}</small></div>
        <div class="field"><label>Mathpix App Key</label><input id="mathpix-app-key" type="password" autocomplete="new-password" placeholder="app_key를 입력하세요"><small>키는 이 컴퓨터의 로컬 데이터베이스에만 저장하며 화면에 다시 표시하지 않습니다.</small></div>
        ${mathpix.last_error ? `<div class="connection-detail error"><b>최근 연결 확인 실패</b><span>${esc(mathpix.last_error)}</span></div>` : mathpix.verified ? `<div class="connection-detail success"><b>실제 이미지 OCR 연결 확인 완료</b><span>${esc(mathpix.verified_at || '')}</span></div>` : ''}
        <div class="settings-actions"><button type="button" class="danger-btn" id="clear-mathpix" ${mathpix.configured ? '' : 'disabled'}>연결 해제</button>${mathpix.configured ? '<button type="button" class="soft-btn" id="test-mathpix">저장된 키 다시 확인</button>' : ''}<button type="submit" class="primary-btn" id="save-mathpix">키 저장 및 실제 OCR 확인</button></div>
      </form>
    </article>
    <aside class="panel settings-help"><h3>정확도를 높이는 사용 순서</h3>
      <ol><li><b>키를 연결합니다.</b><span>사용량 조회가 제한된 계정은 실제 OCR 인증으로 다시 확인합니다.</span></li><li><b>자동 감지 박스를 확인합니다.</b><span>박스를 이동하거나 모서리를 끌어 문제 전체를 감싸세요.</span></li><li><b>영역별 OCR을 실행합니다.</b><span>전체 페이지가 아닌 문제 한 개씩 전송해 LaTeX 대응이 선명해집니다.</span></li></ol>
      <p class="cost-note">영역별 인식은 그린 영역 하나마다 Mathpix 이미지 OCR 요청이 1회 발생하며, 사용량은 Mathpix 계정 요금에 반영될 수 있습니다.</p>
      <a href="https://console.mathpix.com" target="_blank" rel="noreferrer">Mathpix Console 열기 ↗</a>
    </aside>
  </section>`;
  document.querySelector('#mathpix-settings-form').addEventListener('submit', saveMathpixSettings);
  document.querySelector('#test-mathpix')?.addEventListener('click', testMathpixSettings);
}

async function saveMathpixSettings(event) {
  event.preventDefault();
  const button = document.querySelector('#save-mathpix');
  const appId = document.querySelector('#mathpix-app-id').value.trim();
  const appKey = document.querySelector('#mathpix-app-key').value.trim();
  if (!appId || !appKey) return toast('App ID와 App Key를 모두 입력해 주세요.', 'error');
  button.disabled = true;
  button.textContent = 'Mathpix 연결 확인 중…';
  try {
    const result = await api('/api/settings/mathpix', {method:'POST', body:JSON.stringify({app_id:appId, app_key:appKey})});
    state.config = null;
    toast(result.verified ? 'Mathpix 키를 저장하고 실제 이미지 OCR 연결을 확인했습니다.' : '키는 저장했습니다. 아래의 연결 실패 상세 내용을 확인해 주세요.', result.verified ? '' : 'error');
    await renderSettings();
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
    button.textContent = '저장 후 연결 확인';
  }
}

async function testMathpixSettings() {
  const button = document.querySelector('#test-mathpix');
  button.disabled = true;
  button.textContent = '실제 이미지 OCR 확인 중…';
  try {
    const result = await api('/api/settings/mathpix/test', {method:'POST', body:'{}'});
    state.config = null;
    toast(result.verified ? 'Mathpix 실제 이미지 OCR 연결을 확인했습니다.' : '연결 확인에 실패했습니다. 상세 내용을 확인해 주세요.', result.verified ? '' : 'error');
    await renderSettings();
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
    button.textContent = '저장된 키 다시 확인';
  }
}

async function clearMathpixSettings() {
  await api('/api/settings/mathpix', {method:'DELETE'});
  state.config = null;
  toast('Mathpix 연결 정보를 지웠습니다.');
  await renderSettings();
}

async function renderRegions(documentId) {
  if (!Number.isInteger(documentId) || documentId <= 0) throw new Error('문서 번호가 올바르지 않습니다.');
  const data = await api(`/api/documents/${documentId}/regions`);
  state.manual = {
    documentId,
    document: data.document,
    pages: data.pages || [],
    regions: (data.regions || []).map((region, index) => ({...region, order:index + 1})),
    pageIndex: 0,
    selectedIndex: null,
    nextNumber: Math.max(0, ...(data.regions || []).map(region => Number(region.number) || 0)) + 1,
  };
  if (!state.manual.pages.length) throw new Error('표시할 PDF 페이지가 없습니다.');
  renderRegionWorkspace();
}

function renderRegionWorkspace() {
  const manual = state.manual;
  const page = manual.pages[manual.pageIndex];
  const currentRegions = manual.regions.map((region, index) => ({region,index})).filter(item => Number(item.region.page) === Number(page.page));
  const mathpixReady = Boolean(state.config?.providers?.mathpix?.configured);
  app.innerHTML = `<section class="region-layout">
    <div class="region-main panel">
      <div class="region-toolbar"><div><b>${esc(manual.document.title)}</b><small>자동 감지 박스를 이동·크기 조절하거나 빈 곳을 드래그해 추가하세요.</small></div>
        <div class="page-controls"><button data-region-page="prev" ${manual.pageIndex === 0 ? 'disabled' : ''}>←</button><select id="region-page-select">${manual.pages.map((item,index)=>`<option value="${index}" ${index===manual.pageIndex?'selected':''}>${item.page} / ${manual.pages.length}쪽</option>`).join('')}</select><button data-region-page="next" ${manual.pageIndex === manual.pages.length-1 ? 'disabled' : ''}>→</button></div>
      </div>
      <div class="region-canvas-scroll"><div class="region-stage" id="region-stage"><img src="${esc(page.url)}" alt="${page.page}쪽 PDF 페이지" draggable="false"><div class="region-layer" id="region-layer">
        ${currentRegions.map(({region,index}) => `<button class="region-box ${manual.selectedIndex===index?'selected':''} ${region.source==='auto'?'auto-detected':''}" data-region-index="${index}" style="left:${region.x*100}%;top:${region.y*100}%;width:${region.width*100}%;height:${region.height*100}%"><span>${esc(region.number)}번${region.source==='auto'?' · 자동':''}</span>${['nw','n','ne','e','se','s','sw','w'].map(handle=>`<i class="resize-handle handle-${handle}" data-resize="${handle}"></i>`).join('')}</button>`).join('')}
      </div></div></div>
    </div>
    <aside class="panel region-sidebar"><span class="region-kicker">PAGE ${page.page}</span><h3>문제 영역 ${currentRegions.length}개</h3><p>문제의 지문·보기·도형을 모두 포함해 그리세요. 같은 문제가 다음 페이지로 이어지면 같은 번호를 입력하면 합쳐집니다.</p>
      <div class="field"><label>다음에 그릴 문제 번호</label><input id="next-region-number" value="${esc(manual.nextNumber)}" inputmode="numeric"></div>
      <div class="region-list">${currentRegions.length ? currentRegions.map(({region,index}) => `<div class="region-list-item ${manual.selectedIndex===index?'selected':''}" data-select-region="${index}"><span>${currentRegions.indexOf(currentRegions.find(item=>item.index===index))+1}</span><div><label>문제 번호</label><input data-region-number="${index}" value="${esc(region.number)}"></div><button data-delete-region="${index}" title="영역 삭제">×</button></div>`).join('') : '<div class="region-empty">페이지 위에서 문제 영역을 드래그하세요.</div>'}</div>
      <div class="region-summary"><span>전체 지정</span><b>${manual.regions.length}개 영역</b></div>
      ${mathpixReady ? '<div class="region-ocr-state ready">Mathpix 연결됨 · 영역 1개당 이미지 OCR 요청 1회</div>' : '<div class="region-ocr-state">Mathpix 미연결 · PDF 내장 글자만 추출됩니다. <button data-go="settings">키 설정</button></div>'}
      <button class="primary-btn region-extract-btn" id="extract-regions" ${manual.regions.length ? '' : 'disabled'}>${mathpixReady ? '이 영역으로 Mathpix OCR 시작' : '영역 저장 후 로컬 추출'}</button>
      <button class="ghost-btn" data-go="upload" style="width:100%;margin-top:8px">나중에 계속하기</button>
    </aside>
  </section>`;
  document.querySelector('#region-page-select').addEventListener('change', event => {
    manual.pageIndex = Number(event.target.value); manual.selectedIndex = null; renderRegionWorkspace();
  });
  document.querySelector('#next-region-number').addEventListener('input', event => { manual.nextNumber = event.target.value; });
  document.querySelectorAll('[data-region-number]').forEach(input => input.addEventListener('change', event => {
    manual.regions[Number(event.target.dataset.regionNumber)].number = event.target.value.trim() || String(Number(event.target.dataset.regionNumber) + 1);
    renderRegionWorkspace();
  }));
  attachRegionDrawing();
}

function attachRegionDrawing() {
  const layer = document.querySelector('#region-layer');
  let start = null;
  let draft = null;
  let edit = null;
  const point = event => {
    const rect = layer.getBoundingClientRect();
    return {x:Math.max(0, Math.min(rect.width, event.clientX - rect.left)), y:Math.max(0, Math.min(rect.height, event.clientY - rect.top)), rect};
  };
  layer.addEventListener('pointerdown', event => {
    const box = event.target.closest('.region-box');
    if (box) {
      event.preventDefault();
      const index = Number(box.dataset.regionIndex);
      const region = state.manual.regions[index];
      state.manual.selectedIndex = index;
      edit = {index, mode:event.target.dataset.resize || 'move', origin:point(event), initial:{...region}};
      layer.setPointerCapture(event.pointerId);
      box.classList.add('selected');
      return;
    }
    if (event.target !== layer) return;
    start = point(event);
    draft = document.createElement('div');
    draft.className = 'region-draft';
    layer.append(draft);
    layer.setPointerCapture(event.pointerId);
  });
  layer.addEventListener('pointermove', event => {
    if (edit) {
      const current = point(event);
      const dx = (current.x - edit.origin.x) / current.rect.width;
      const dy = (current.y - edit.origin.y) / current.rect.height;
      const r = state.manual.regions[edit.index];
      const i = edit.initial;
      let x=i.x, y=i.y, width=i.width, height=i.height;
      if (edit.mode === 'move') { x=Math.max(0,Math.min(1-width,i.x+dx)); y=Math.max(0,Math.min(1-height,i.y+dy)); }
      else {
        if (edit.mode.includes('w')) { x=Math.max(0,Math.min(i.x+i.width-0.02,i.x+dx)); width=i.width+(i.x-x); }
        if (edit.mode.includes('e')) width=Math.max(0.02,Math.min(1-i.x,i.width+dx));
        if (edit.mode.includes('n')) { y=Math.max(0,Math.min(i.y+i.height-0.015,i.y+dy)); height=i.height+(i.y-y); }
        if (edit.mode.includes('s')) height=Math.max(0.015,Math.min(1-i.y,i.height+dy));
      }
      Object.assign(r,{x,y,width,height,source:'adjusted'});
      const box = layer.querySelector(`[data-region-index="${edit.index}"]`);
      if (box) Object.assign(box.style,{left:`${x*100}%`,top:`${y*100}%`,width:`${width*100}%`,height:`${height*100}%`});
      return;
    }
    if (!start || !draft) return;
    const current = point(event);
    const left = Math.min(start.x, current.x), top = Math.min(start.y, current.y);
    const width = Math.abs(current.x - start.x), height = Math.abs(current.y - start.y);
    Object.assign(draft.style, {left:`${left}px`,top:`${top}px`,width:`${width}px`,height:`${height}px`});
  });
  layer.addEventListener('pointerup', event => {
    if (edit) { edit=null; renderRegionWorkspace(); return; }
    if (!start) return;
    const current = point(event);
    const left = Math.min(start.x, current.x), top = Math.min(start.y, current.y);
    const width = Math.abs(current.x - start.x), height = Math.abs(current.y - start.y);
    draft?.remove();
    if (width >= 14 && height >= 12) {
      const number = String(state.manual.nextNumber || state.manual.regions.length + 1);
      state.manual.regions.push({page:state.manual.pages[state.manual.pageIndex].page, number, x:left/current.rect.width, y:top/current.rect.height, width:width/current.rect.width, height:height/current.rect.height, order:state.manual.regions.length+1});
      state.manual.selectedIndex = state.manual.regions.length - 1;
      if (/^\d+$/.test(number)) state.manual.nextNumber = Number(number) + 1;
    }
    start = null; draft = null; renderRegionWorkspace();
  });
}

async function extractManualRegions() {
  const button = document.querySelector('#extract-regions');
  button.disabled = true;
  button.textContent = '영역 저장 중…';
  try {
    await api(`/api/documents/${state.manual.documentId}/regions`, {method:'POST', body:JSON.stringify({regions:state.manual.regions})});
    toast(`${state.manual.regions.length}개 영역의 OCR을 시작했습니다.`);
    go('upload');
  } catch (error) {
    toast(error.message, 'error'); button.disabled = false; button.textContent = '이 영역으로 Mathpix OCR 시작';
  }
}

async function prepareRegionsForDocument(documentId, problemCount = 0) {
  if (Number(problemCount) > 0 && !confirm('기존 추출 결과는 영역 OCR이 성공한 뒤 새 결과로 교체됩니다. 수동 영역 지정을 시작할까요?')) return;
  await api(`/api/documents/${documentId}/prepare-regions`, {method:'POST', body:'{}'});
  toast('PDF 페이지를 준비하고 있습니다.');
  waitForManualReady(Number(documentId));
}

async function reprocessDocument(documentId) {
  if (!confirm('현재 문서의 추출 문제를 Mathpix 결과로 교체할까요? OCR가 성공한 뒤에만 기존 결과가 교체됩니다.')) return;
  await api(`/api/documents/${documentId}/reprocess`, {method:'POST', body:'{}'});
  toast('Mathpix로 문서 재처리를 시작했습니다.');
  go('upload');
}

function reviewMarkup(problem, index, total) {
  const confidence = Math.round((problem.confidence || 0) * 100);
  const mathpixConfigured = Boolean(state.config?.providers?.mathpix?.configured);
  const ocrBanner = mathpixConfigured
    ? `<div class="ocr-banner connected"><div><b>Mathpix 연결됨</b><span>현재 문제 원본만 다시 보내 LaTeX를 개선할 수 있습니다.</span></div><button data-problem-ocr="${problem.id}">이 문제 다시 인식</button></div>`
    : `<div class="ocr-banner"><div><b>Mathpix가 연결되지 않았습니다.</b><span>현재 결과는 PDF 내장 글자에 의존하므로 수식이 깨질 수 있습니다.</span></div><button data-go="settings">API 키 설정</button></div>`;
  return `${ocrBanner}<section class="review-card">
    <div class="source-pane"><div class="source-head"><b>원본 문제</b><span>${confidence}% 신뢰도</span></div>
      <div class="source-canvas">${problem.source_image ? `<img src="${esc(problem.source_image)}" alt="원본 ${esc(problem.number || '')}번 문제">` : '<p>원본 이미지를 만들지 못했습니다.</p>'}</div></div>
    <form class="editor-pane" id="review-form" data-id="${problem.id}"><div class="editor-head"><b>추출·편집 결과</b><small>${esc(problem.document_title)} · ${problem.page_start}${problem.page_end !== problem.page_start ? `–${problem.page_end}` : ''}쪽</small></div>
      <div class="form-grid">
        <div class="field"><label>문제 번호</label><input name="number" value="${esc(problem.number || '')}"></div>
        <div class="field"><label>문제 유형</label><select name="problem_type"><option ${problem.problem_type==='주관식'?'selected':''}>주관식</option><option ${problem.problem_type==='객관식'?'selected':''}>객관식</option></select></div>
        <div class="field"><label>학년</label><input name="grade" value="${esc(problem.grade)}"></div>
        <div class="field"><label>난이도</label><select name="difficulty">${[1,2,3,4,5].map(value => `<option value="${value}" ${Number(problem.difficulty)===value?'selected':''}>${'●'.repeat(value)}${'○'.repeat(5-value)}</option>`).join('')}</select></div>
        <div class="field"><label>단원</label><input name="unit" value="${esc(problem.unit)}"></div>
        <div class="field"><label>핵심 개념</label><input name="concept" value="${esc(problem.concept)}"></div>
        <div class="field full"><label>일반 본문</label><textarea name="content">${esc(problem.content)}</textarea></div>
        <div class="field full"><label>LaTeX 포함 본문</label><textarea name="latex" id="latex-input">${esc(problem.latex)}</textarea></div>
        <div class="field full"><label>수식 미리보기</label><div class="math-preview math-render" id="math-preview">${esc(problem.latex)}</div></div>
        <div class="field"><label>정답</label><input name="answer" value="${esc(problem.answer)}"></div>
        <div class="field"><label>검수 메모</label><input name="quality_notes" value="${esc(problem.quality_notes)}"></div>
        <div class="field full"><label>해설</label><textarea name="solution" style="min-height:85px">${esc(problem.solution)}</textarea></div>
      </div>
      ${problem.quality_notes ? `<div class="quality-note"><b>확인</b><span>${esc(problem.quality_notes)}</span></div>` : ''}
      <div class="review-actions"><button type="button" class="danger-btn" data-delete-problem="${problem.id}">완전 삭제</button><button type="button" class="soft-btn" data-review-action="rejected">제외</button><button type="button" class="soft-btn" data-review-action="save">임시 저장</button><button type="button" class="primary-btn" data-review-action="approved">검수 승인</button></div>
    </form></section>
    <div class="review-nav"><button data-review-prev ${index===0?'disabled':''}>← 이전 문제</button><span>${index + 1} / ${total}</span><button data-review-next ${index===total-1?'disabled':''}>다음 문제 →</button></div>`;
}

async function renderReview() {
  state.config = await api('/api/config');
  await refreshDashboard();
  state.problems = await api('/api/problems?status=needs_review');
  if (!state.problems.length) {
    app.innerHTML = '<div class="empty-state"><div class="upload-symbol">✓</div><h2>검수함이 비었습니다.</h2><p>모든 문제가 승인되었거나 아직 PDF가 등록되지 않았습니다.</p><button class="primary-btn" data-go="upload">PDF 가져오기</button></div>';
    return;
  }
  state.reviewIndex = Math.min(state.reviewIndex, state.problems.length - 1);
  app.innerHTML = reviewMarkup(state.problems[state.reviewIndex], state.reviewIndex, state.problems.length);
  renderMath(app);
  const input = document.querySelector('#latex-input');
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const preview = document.querySelector('#math-preview');
      preview.textContent = input.value;
      preview.removeAttribute('data-katex-rendered');
      renderMath(preview.parentElement);
    }, 180);
  });
}

async function rerunProblemOcr(problemId) {
  const button = document.querySelector(`[data-problem-ocr="${problemId}"]`);
  button.disabled = true;
  button.textContent = 'Mathpix 인식 중…';
  try {
    await api(`/api/problems/${problemId}/ocr`, {method:'POST', body:'{}'});
    toast('Mathpix 결과로 본문과 LaTeX를 갱신했습니다.');
    await navigate('review', true);
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
    button.textContent = '이 문제 다시 인식';
  }
}

function reviewFormData(status) {
  const form = document.querySelector('#review-form');
  const fields = Object.fromEntries(new FormData(form).entries());
  fields.difficulty = Number(fields.difficulty);
  if (status && status !== 'save') fields.quality_status = status;
  return {id: Number(form.dataset.id), fields};
}

async function saveReview(status) {
  const {id, fields} = reviewFormData(status);
  await api(`/api/problems/${id}`, {method:'PATCH', body:JSON.stringify(fields)});
  toast(status === 'approved' ? '검수 승인했습니다.' : status === 'rejected' ? '문제은행에서 제외했습니다.' : '수정 내용을 저장했습니다.');
  if (status === 'approved' || status === 'rejected') {
    state.problems.splice(state.reviewIndex, 1);
    if (state.reviewIndex >= state.problems.length) state.reviewIndex = Math.max(0, state.problems.length - 1);
    await navigate('review', true);
  }
}

function problemCard(problem) {
  const selected = state.selected.has(Number(problem.id));
  return `<article class="problem-card ${selected?'selected':''}" data-problem-card="${problem.id}">
    <input class="select-problem" type="checkbox" aria-label="시험지에 선택" data-select-problem="${problem.id}" ${selected?'checked':''}>
    <div class="problem-thumb">${problem.source_image ? `<img src="${esc(problem.source_image)}" alt="문제 원본">` : '<span>원본 없음</span>'}</div>
    <div class="problem-body"><div class="problem-topline"><span class="problem-number">#${esc(problem.number || problem.id)}</span><span class="tag">${esc(problem.unit)}</span><span class="tag">난이도 ${problem.difficulty}</span></div>
      <div class="problem-text math-render">${esc(problem.latex || problem.content)}</div>
      <div class="problem-meta"><span>${esc(problem.document_title)}</span><span>${problem.page_start}쪽</span><span>${statusLabel(problem.quality_status)}</span></div>
      <div class="problem-actions"><button data-edit-problem="${problem.id}">내용 보기</button><button data-similar="${problem.id}">유사문제</button><button class="delete-problem-btn" data-delete-problem="${problem.id}">삭제</button></div>
    </div></article>`;
}

async function renderBank(query = '') {
  await refreshDashboard();
  const params = new URLSearchParams({status:'approved'});
  if (query) params.set('q', query);
  state.problems = await api(`/api/problems?${params}`);
  app.innerHTML = `<div class="toolbar"><div class="search-box"><input id="bank-search" placeholder="단원, 개념, 문제 내용을 검색" value="${esc(query)}"></div>
    <select class="filter-select" id="bank-unit"><option value="">모든 단원</option>${[...new Set(state.problems.map(p=>p.unit))].map(unit=>`<option>${esc(unit)}</option>`).join('')}</select>
    <span class="selection-chip" data-selection-count>${state.selected.size}문제 선택</span><button class="danger-btn batch-delete-btn" data-delete-selected-problems ${state.selected.size ? '' : 'disabled'}>선택 문제 삭제</button><button class="primary-btn" data-go="exams">시험지 만들기</button></div>
    ${state.problems.length ? `<section class="problem-grid">${state.problems.map(problemCard).join('')}</section>` : '<div class="empty-state"><h2>승인된 문제가 없습니다.</h2><p>추출 검수에서 문제를 승인하면 이곳에 나타납니다.</p><button class="primary-btn" data-go="review">검수하러 가기</button></div>'}`;
  renderMath(app);
  const search = document.querySelector('#bank-search');
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => renderBank(search.value.trim()), 280); });
  document.querySelector('#bank-unit')?.addEventListener('change', event => {
    const unit = event.target.value;
    document.querySelectorAll('[data-problem-card]').forEach(card => {
      const problem = state.problems.find(item => item.id === Number(card.dataset.problemCard));
      card.hidden = Boolean(unit && problem.unit !== unit);
    });
  });
}

async function openProblem(id) {
  const problem = await api(`/api/problems/${id}`);
  modalContent.innerHTML = `<div class="modal-head"><h3>${esc(problem.number || problem.id)}번 문제</h3><button data-close-modal>×</button></div>
    <div class="modal-body"><div class="review-card" style="grid-template-columns:1fr 1fr;min-height:450px"><div class="source-pane"><div class="source-canvas"><img src="${esc(problem.source_image)}" alt="원본"></div></div>
    <div class="editor-pane"><div class="math-preview math-render">${esc(problem.latex || problem.content)}</div><p style="font-size:10px;color:#69746f;line-height:1.7"><b>단원</b> ${esc(problem.unit)} · <b>개념</b> ${esc(problem.concept)} · <b>난이도</b> ${problem.difficulty}</p>
    <p style="font-size:10px;color:#69746f"><b>정답</b> ${esc(problem.answer || '미입력')}</p><div class="math-preview">${esc(problem.solution || '해설이 아직 없습니다.')}</div></div></div></div>`;
  modal.showModal(); renderMath(modal);
}

async function openSimilar(id) {
  const items = await api(`/api/problems/${id}/similar`);
  modalContent.innerHTML = `<div class="modal-head"><h3>유사문제 추천</h3><button data-close-modal>×</button></div><div class="modal-body">
    ${items.length ? `<div class="similar-list">${items.map(item => `<div class="similar-item"><img src="${esc(item.source_image)}" alt="유사문제"><div><b>${esc(item.unit)} · ${esc(item.concept)}</b><p>${esc(item.content)}</p></div><span class="similar-score">${Math.round(item.similarity*100)}%</span></div>`).join('')}</div>` : '<div class="empty-state" style="min-height:220px"><p>비교할 승인 문제가 더 필요합니다.</p></div>'}
    </div>`;
  modal.showModal();
}

async function deleteProblem(id) {
  const problem = state.problems.find(item => Number(item.id) === Number(id));
  const label = problem?.number ? `${problem.number}번 문제` : '이 문제';
  if (!confirm(`${label}를 완전히 삭제할까요?\n삭제한 문제는 복구할 수 없습니다.`)) return;
  await api(`/api/problems/${id}`, {method:'DELETE'});
  setSelected(id, false);
  state.problems = state.problems.filter(item => Number(item.id) !== Number(id));
  if (modal.open) modal.close();
  toast(`${label}를 삭제했습니다.`);
  if (state.route.split(':')[0] === 'review') {
    state.reviewIndex = Math.min(state.reviewIndex, Math.max(0, state.problems.length - 1));
    await navigate('review', true);
  } else {
    await navigate('bank', true);
  }
}

async function deleteSelectedProblems() {
  const ids = [...state.selected].filter(id => Number(id) > 0);
  if (!ids.length) return toast('삭제할 문제를 먼저 선택해 주세요.', 'error');
  if (!confirm(`선택한 ${ids.length}문제를 모두 삭제할까요?\n삭제한 문제는 복구할 수 없습니다.`)) return;
  const result = await api('/api/problems', {method:'DELETE', body:JSON.stringify({problem_ids:ids})});
  for (const id of result.deleted_ids) state.selected.delete(Number(id));
  localStorage.setItem('mathbank-selected', JSON.stringify([...state.selected]));
  toast(`${result.deleted_count}문제를 삭제했습니다.`);
  await navigate('bank', true);
}

async function renderExams() {
  const [exams, approved] = await Promise.all([api('/api/exams'), api('/api/problems?status=approved')]);
  const chosen = approved.filter(problem => state.selected.has(Number(problem.id)));
  app.innerHTML = `<section class="exam-layout"><article class="panel"><div class="panel-head"><div><h3>저장된 시험지</h3><p>원본 우선 또는 편집본 형태로 인쇄할 수 있습니다.</p></div><button class="panel-action" data-go="bank">문제 더 고르기 →</button></div>
    <div class="exam-list">${exams.length ? exams.map(exam => `<div class="exam-row"><div><b>${esc(exam.title)}</b><small>${exam.item_count}문제 · ${exam.total_points}점 · ${fmtDate(exam.created_at)}</small></div><div class="exam-row-actions"><a href="/print/exams/${exam.id}?mode=original" target="_blank">원본 인쇄</a><a href="/print/exams/${exam.id}?mode=edited" target="_blank">편집본</a><a href="/print/exams/${exam.id}?mode=edited&answers=1" target="_blank">해설지</a><button data-delete-exam="${exam.id}">삭제</button></div></div>`).join('') : '<div class="empty-state" style="min-height:260px"><p>아직 만든 시험지가 없습니다.</p></div>'}</div></article>
    <aside class="panel builder"><h3>새 시험지</h3><p>문제은행에서 선택한 순서대로 배치합니다. 승인된 문제만 사용할 수 있습니다.</p>
      <div class="field"><label>시험지 제목</label><input id="exam-title" value="수학 단원평가"></div>
      <div class="form-grid"><div class="field"><label>시험 시간</label><input id="exam-duration" type="number" value="50" min="5"></div><div class="field"><label>단 구성</label><select id="exam-columns"><option value="1">1단</option><option value="2">2단</option></select></div></div>
      <div class="selected-list">${chosen.length ? chosen.map((problem,index)=>`<div class="selected-row"><span>${index+1}</span><b>${esc(problem.number || problem.id)}번 · ${esc(problem.unit)}</b><button data-remove-selected="${problem.id}">×</button></div>`).join('') : '<div class="empty-state" style="min-height:120px"><p>선택한 문제가 없습니다.</p></div>'}</div>
      <button class="soft-btn" style="width:100%" data-go="bank">문제은행에서 선택</button><button class="primary-btn" id="create-exam" ${chosen.length?'':'disabled'}>${chosen.length}문제로 시험지 만들기</button>
    </aside></section>`;
}

async function createExam() {
  const problemIds = [...state.selected];
  if (!problemIds.length) return toast('문제를 먼저 선택해 주세요.', 'error');
  const exam = await api('/api/exams', {method:'POST', body:JSON.stringify({
    title: document.querySelector('#exam-title').value,
    duration: Number(document.querySelector('#exam-duration').value),
    columns_count: Number(document.querySelector('#exam-columns').value),
    problem_ids: problemIds,
  })});
  state.selected.clear(); setSelected(-1, false); state.selected.delete(-1);
  localStorage.setItem('mathbank-selected', '[]');
  toast(`${exam.items.length}문제 시험지를 만들었습니다.`);
  await navigate('exams', true);
}

document.addEventListener('click', async event => {
  const goButton = event.target.closest('[data-go]');
  if (goButton) return go(goButton.dataset.go);
  const navButton = event.target.closest('[data-route]');
  if (navButton) return go(navButton.dataset.route);
  if (event.target.closest('.mobile-menu')) return document.body.classList.toggle('menu-open');
  if (event.target.closest('#refresh-btn') || event.target.closest('[data-retry]')) return navigate(state.route, true);
  if (event.target.closest('#clear-mathpix')) {
    if (confirm('저장된 Mathpix 연결 정보를 지울까요?')) clearMathpixSettings().catch(error => toast(error.message,'error'));
    return;
  }
  const openRegions = event.target.closest('[data-open-regions]');
  if (openRegions) return go(`regions:${openRegions.dataset.openRegions}`);
  const prepareRegions = event.target.closest('[data-prepare-regions]');
  if (prepareRegions) return prepareRegionsForDocument(prepareRegions.dataset.prepareRegions, prepareRegions.dataset.problemCount).catch(error => toast(error.message,'error'));
  const reprocess = event.target.closest('[data-reprocess-document]');
  if (reprocess) return reprocessDocument(reprocess.dataset.reprocessDocument).catch(error => toast(error.message,'error'));
  const problemOcr = event.target.closest('[data-problem-ocr]');
  if (problemOcr) return rerunProblemOcr(Number(problemOcr.dataset.problemOcr));
  const deleteProblemButton = event.target.closest('[data-delete-problem]');
  if (deleteProblemButton) return deleteProblem(Number(deleteProblemButton.dataset.deleteProblem)).catch(error => toast(error.message,'error'));
  if (event.target.closest('[data-delete-selected-problems]')) return deleteSelectedProblems().catch(error => toast(error.message,'error'));
  const pageNav = event.target.closest('[data-region-page]');
  if (pageNav && state.manual) {
    state.manual.pageIndex += pageNav.dataset.regionPage === 'next' ? 1 : -1;
    state.manual.pageIndex = Math.max(0, Math.min(state.manual.pages.length - 1, state.manual.pageIndex));
    state.manual.selectedIndex = null; renderRegionWorkspace(); return;
  }
  const deleteRegion = event.target.closest('[data-delete-region]');
  if (deleteRegion && state.manual) {
    state.manual.regions.splice(Number(deleteRegion.dataset.deleteRegion), 1);
    state.manual.regions.forEach((region,index)=>region.order=index+1);
    state.manual.selectedIndex = null; renderRegionWorkspace(); return;
  }
  const selectRegion = event.target.closest('[data-select-region], [data-region-index]');
  if (selectRegion && state.manual) {
    state.manual.selectedIndex = Number(selectRegion.dataset.selectRegion ?? selectRegion.dataset.regionIndex);
    renderRegionWorkspace(); return;
  }
  if (event.target.closest('#extract-regions')) return extractManualRegions();
  const action = event.target.closest('[data-review-action]');
  if (action) {
    action.disabled = true;
    try { await saveReview(action.dataset.reviewAction); } catch (error) { toast(error.message,'error'); action.disabled=false; }
    return;
  }
  if (event.target.closest('[data-review-prev]')) { state.reviewIndex--; app.innerHTML = reviewMarkup(state.problems[state.reviewIndex], state.reviewIndex, state.problems.length); renderMath(app); return; }
  if (event.target.closest('[data-review-next]')) { state.reviewIndex++; app.innerHTML = reviewMarkup(state.problems[state.reviewIndex], state.reviewIndex, state.problems.length); renderMath(app); return; }
  const select = event.target.closest('[data-select-problem]');
  if (select) { setSelected(select.dataset.selectProblem, select.checked); select.closest('.problem-card').classList.toggle('selected', select.checked); return; }
  const edit = event.target.closest('[data-edit-problem]');
  if (edit) return openProblem(edit.dataset.editProblem);
  const similar = event.target.closest('[data-similar]');
  if (similar) return openSimilar(similar.dataset.similar);
  if (event.target.closest('[data-close-modal]')) return modal.close();
  const remove = event.target.closest('[data-remove-selected]');
  if (remove) { setSelected(remove.dataset.removeSelected, false); return renderExams(); }
  if (event.target.closest('#create-exam')) return createExam().catch(error => toast(error.message,'error'));
  const del = event.target.closest('[data-delete-exam]');
  if (del && confirm('이 시험지를 삭제할까요?')) { await api(`/api/exams/${del.dataset.deleteExam}`, {method:'DELETE'}); toast('시험지를 삭제했습니다.'); return renderExams(); }
});

window.addEventListener('hashchange', () => navigate(location.hash.replace('#','') || 'dashboard', true));
modal.addEventListener('click', event => { if (event.target === modal) modal.close(); });
navigate(state.route, true);
