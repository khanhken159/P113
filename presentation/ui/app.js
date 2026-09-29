const STORAGE_KEY = 'flowforge-gate1-demo-v1';
const steps = [
  ['dashboard', 'Dashboard'],
  ['new', 'New Pipeline'],
  ['planner', 'Planner'],
  ['processing', 'Agents'],
  ['evidence', 'Evidence'],
  ['diff', 'Git Diff'],
  ['review', 'Review'],
  ['pr', 'PR / Merge']
];

function emptyClarification() {
  return { status_scope: '', operation: '', target_currency: '', currency_by_file: {},
    conversion_rates: {}, currency_rates: {}, missing_currency_by_file: {},
    number_format_by_file: {}, number_format_by_currency: {}, date_format_by_file: {}, report_timezone: '',
    missing_amount_policy: {}, duplicate_resolution: '', duplicate_scope: '',
    join_duplicate_policy: '', orphan_policy: '', resolved_business_rules: {} };
}

const defaults = {
  screen: 'dashboard', maxStep: 0, name: 'Daily Revenue Unification',
  prompt: 'Gộp đơn hàng từ 3 nguồn và tính doanh thu theo ngày.',
  sources: ['Shopee', 'Tiki', 'Website'], schema: 'analytics',
  context: 'Chuẩn hóa trạng thái đơn hàng, loại trừ đơn bị hủy và đối soát doanh thu theo ngày.',
  timezone: 'Asia/Ho_Chi_Minh', currency: '', processingPhase: 0,
  approved: false, rejected: false, rejectionReason: '', showReject: false,
  prCreated: false, merged: false, selectedFile: 0, activeModal: '', uploadedFiles: [], uploadPreview: [],
  actualRun: null, actualRunError: '', actualRunBusy: false,
  clarificationRun: null, clarification: emptyClarification()
};

function readState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    const state = { ...defaults, ...saved };
    if (!steps.some(([id]) => id === state.screen)) state.screen = 'dashboard';
    if (!Array.isArray(state.sources)) state.sources = [...defaults.sources];
    const supportedSources = ['Shopee', 'Tiki', 'Website', 'Custom CSV'];
    const savedSources = state.sources;
    state.sources = savedSources.filter(source => supportedSources.includes(source));
    if (savedSources.length && !state.sources.length) state.sources = [...defaults.sources];
    if (!Array.isArray(state.uploadedFiles)) state.uploadedFiles = [];
    if (!Array.isArray(state.uploadPreview)) state.uploadPreview = [];
    // File bytes stay in memory only and must be selected again after reload.
    state.uploadedFiles = [];
    state.uploadPreview = [];
    state.actualRun = null;
    state.clarificationRun = null;
    state.clarification = emptyClarification();
    state.actualRunBusy = false;
    // Agent runs are explicit user actions; never resume a run just because the page reloaded.
    if (state.screen === 'processing') state.screen = 'planner';
    state.maxStep = Math.min(7, Math.max(0, Number(state.maxStep) || 0));
    state.processingPhase = Math.min(4, Math.max(0, Number(state.processingPhase) || 0));
    if (state.processingPhase < 4) state.maxStep = Math.min(state.maxStep, 3);
    if (!state.approved) { state.maxStep = Math.min(state.maxStep, 6); state.prCreated = false; state.merged = false; }
    if (steps.findIndex(([id]) => id === state.screen) > state.maxStep) state.screen = steps[state.maxStep][0];
    return state;
  } catch { return { ...defaults }; }
}

let state = readState();
let uploadContents = Object.create(null);
let simulationTimers = [];
let toastTimer;
const root = document.getElementById('app');

function save() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])); }
function icon(name) {
  const paths = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    chevron: '<path d="m9 18 6-6-6-6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    branch: '<circle cx="6" cy="3" r="2"/><path d="M6 5v12a4 4 0 0 0 4 4h4"/><circle cx="17" cy="6" r="2"/><path d="M17 8v9"/><circle cx="17" cy="19" r="2"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    code: '<path d="m8 8-4 4 4 4m8-8 4 4-4 4m-3-13-2 18"/>',
    bolt: '<path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z"/>',
    alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5m0 3h.01"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/>',
    file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="2"/><circle cx="16" cy="17" r="2"/>',
    activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    git: '<circle cx="6" cy="4" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="19" r="2"/><path d="M6 6v9a4 4 0 0 0 4 4h6M18 8v9"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.5 9A7 7 0 0 1 18 6l2 1M4 17l2 1a7 7 0 0 0 12.5-3"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    spark: '<path d="m12 2 1.5 7.5L21 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2Z"/>',
    x: '<path d="M5 5l14 14M19 5 5 19"/>'
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
}

function status(label, kind) { return `<span class="status status-${kind}">${label}</span>`; }
function btn(label, action, style = 'primary', extra = '') { return `<button type="button" class="btn btn-${style}" data-action="${action}" ${extra}>${label}</button>`; }
function pageHead(eyebrow, title, subtitle, actions = '') {
  return `<div class="page-head"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p class="subtitle">${subtitle}</p></div><div class="head-right">${actions}</div></div>`;
}
function panelTitle(title, caption = '', right = '') {
  return `<div class="panel-head"><div><h2>${title}</h2>${caption ? `<p class="section-caption">${caption}</p>` : ''}</div>${right}</div>`;
}

function modalView() {
  if (!state.activeModal) return '';
  const content = {
    sources: `${panelTitle('Data Sources', 'Các nguồn dữ liệu đang được kết nối trong workspace.', `<button class="icon-btn modal-close" data-action="close-modal" aria-label="Đóng">${icon('x')}</button>`)}<div class="modal-list"><div class="modal-row"><span class="row-icon">${icon('database')}</span><div><strong>Shopify</strong><small>Orders API · Connected</small></div>${status('Connected','pass')}</div><div class="modal-row"><span class="row-icon violet">${icon('database')}</span><div><strong>Stripe</strong><small>Payments API · Connected</small></div>${status('Connected','pass')}</div><div class="modal-row"><span class="row-icon blue">${icon('database')}</span><div><strong>PostgreSQL</strong><small>analytics database · Read only</small></div>${status('Ready','running')}</div></div><div class="modal-footer">${btn('Close','close-modal','outline')}</div>`,
    settings: `${panelTitle('Workspace Settings', 'Thiết lập hiển thị cho Gate 1 demo.', `<button class="icon-btn modal-close" data-action="close-modal" aria-label="Đóng">${icon('x')}</button>`)}<div class="settings-list"><label class="setting-row"><span><strong>Auto-save progress</strong><small>Lưu trạng thái pipeline vào trình duyệt</small></span><input type="checkbox" checked disabled></label><label class="setting-row"><span><strong>Demo notifications</strong><small>Hiển thị thông báo sau mỗi thao tác</small></span><input type="checkbox" checked disabled></label><div class="setting-row"><span><strong>Environment</strong><small>Chế độ hiện tại</small></span><span class="status status-running">Local demo</span></div></div><div class="modal-footer">${btn('Done','close-modal','primary')}</div>`,
    help: `${panelTitle('How this demo works', 'Một vòng pipeline mẫu từ requirement đến review.', `<button class="icon-btn modal-close" data-action="close-modal" aria-label="Đóng">${icon('x')}</button>`)}<ol class="help-list"><li><b>1</b><span>Nhập yêu cầu và chọn data sources.</span></li><li><b>2</b><span>Planner phân tích yêu cầu và tạo kế hoạch.</span></li><li><b>3</b><span>SQL Coder sinh truy vấn; Baseline Tester kiểm tra trước. Optimizer chỉ chạy sau khi baseline đạt, rồi Final Tester kiểm tra SQL được chọn.</span></li><li><b>4</b><span>Acceptance xác nhận kết quả rồi Reviewer chuyển sang bước phê duyệt.</span></li></ol><div class="hint-box">Các agent chạy qua backend cục bộ. PR và Merge là bước mô phỏng.</div><div class="modal-footer">${btn('Got it','close-modal','primary')}</div>`
  }[state.activeModal];
  return `<div class="modal-backdrop" data-action="close-modal"><section class="modal panel" role="dialog" aria-modal="true" aria-label="${state.activeModal}" data-modal-content>${content}</section></div>`;
}

function shell(view) {
  const active = state.screen;
  return `<div class="app-shell">
    <aside class="sidebar" aria-label="Điều hướng chính">
      <div class="brand"><span class="brand-mark">${icon('layers')}</span><div><strong>flowforge</strong><small>Pipeline Studio</small></div></div>
      <nav>
        <div class="nav-label">Workspace</div>
        <button class="nav-link ${active === 'dashboard' ? 'active' : ''}" data-action="dashboard" aria-label="Dashboard">${icon('grid')}<span>Dashboard</span></button>
        <button class="nav-link ${active !== 'dashboard' && active !== 'review' ? 'active' : ''}" data-action="nav-pipelines" aria-label="Pipelines">${icon('layers')}<span>Pipelines</span><span class="nav-count">03</span></button>
        <button class="nav-link ${active === 'review' ? 'active' : ''}" data-action="nav-reviews" aria-label="Reviews">${icon('check')}<span>Reviews</span><span class="nav-count">01</span></button>
        <div class="nav-label" style="margin-top:25px">Workspace</div>
        <button class="nav-link" data-action="nav-sources" aria-label="Data Sources">${icon('database')}<span>Data Sources</span></button>
        <button class="nav-link" data-action="nav-settings" aria-label="Settings">${icon('settings')}<span>Settings</span></button>
      </nav>
      <div class="nav-spacer"></div>
      <div class="side-card"><span class="mini-icon">${icon('spark')}</span><strong>Gate 1 prototype</strong><p>Khám phá toàn bộ flow từ yêu cầu đến phê duyệt pipeline.</p><button data-action="reset">Khởi động lại demo →</button></div>
      <div class="user-box"><span class="avatar">DA</span><div><strong>Data Team</strong><small>Workspace admin</small></div></div>
    </aside>
    <main class="main"><header class="topbar"><div class="breadcrumb"><span>Workspace</span>${icon('chevron')}<strong>${active === 'dashboard' ? 'Overview' : esc(state.name)}</strong></div><div class="top-actions"><span class="demo-tag"><i></i> INTERACTIVE DEMO</span><button class="icon-btn" data-action="help" aria-label="Thông tin demo">${icon('alert')}</button></div></header><div class="content">${view}</div></main>
  </div>${modalView()}`;
}

function stepper() {
  const current = steps.findIndex(([id]) => id === state.screen);
  return `<div class="stepper-wrap" aria-label="Các bước pipeline"><div class="stepper">${steps.map(([id,label], i) => {
    const cls = i === current ? 'current' : i < current ? 'done' : '';
    return `<button class="step ${cls}" data-action="step" data-step="${id}" ${i > state.maxStep || (id === 'pr' && !state.approved) ? 'disabled' : ''} ${i === current ? 'aria-current="step"' : ''}><span class="step-circle">${i < current ? icon('check') : String(i + 1).padStart(2,'0')}</span><span class="step-label">${label}</span></button>`;
  }).join('')}</div></div>`;
}

function dashboardView() {
  return `${pageHead('Workspace / Overview','Pipeline dashboard','Theo dõi các pipeline và tiếp tục xử lý các lần chạy đang chờ review.', btn(`${icon('plus')} New Pipeline`,'new'))}
  <div class="grid grid-4" style="margin-bottom:18px">
    <div class="stat-card"><small>Active pipelines</small><strong>03</strong><span>trong workspace</span></div>
    <div class="stat-card"><small>Successful runs</small><strong>28</strong><span>↑ 12% tháng này</span></div>
    <div class="stat-card"><small>Awaiting review</small><strong>01</strong><span>cần xác nhận</span></div>
    <div class="stat-card"><small>Agent availability</small><strong><i class="dot"></i>Online</strong></div>
  </div>
  <div class="grid grid-2">
    <section class="panel panel-pad">${panelTitle('Pipelines','Các pipeline gần đây trong workspace',btn(`${icon('plus')} New`,'new','outline btn-small'))}
      <div class="table-wrap"><table><thead><tr><th>Pipeline</th><th>Trạng thái</th><th>Lần chạy gần nhất</th><th></th></tr></thead><tbody>
        <tr><td><div class="row-name"><span class="row-icon">${icon('layers')}</span><div><strong>Daily Revenue Unification</strong><small>3 sources · dbt + Airflow</small></div></div></td><td>${status('Needs review','review')}</td><td>Hôm nay, 09:42</td><td><button class="text-link" data-action="open-sample">Open →</button></td></tr>
        <tr><td><div class="row-name"><span class="row-icon violet">${icon('activity')}</span><div><strong>Fulfillment SLA</strong><small>2 sources · dbt</small></div></div></td><td>${status('Running','running')}</td><td>Hôm qua, 16:18</td><td><button class="text-link" data-action="preview-only">Open →</button></td></tr>
        <tr><td><div class="row-name"><span class="row-icon blue">${icon('database')}</span><div><strong>Inventory Snapshot</strong><small>4 sources · Airflow</small></div></div></td><td>${status('Pass','pass')}</td><td>18 Sep, 11:30</td><td><button class="text-link" data-action="preview-only">Open →</button></td></tr>
      </tbody></table></div>
    </section>
    <aside class="quick-card"><p class="eyebrow" style="color:#53d7c5">START SOMETHING NEW</p><h2>Describe it. Let agents build it.</h2><p>Biến yêu cầu bằng ngôn ngữ tự nhiên thành pipeline có test và evidence.</p>${btn(`${icon('arrow')} Create pipeline`,'new')}</aside>
  </div>`;
}

function newView() {
  const sources = ['Shopee','Tiki','Website','Custom CSV'];
  const uploadPreview = state.uploadPreview?.length ? `<div class="upload-preview"><div class="preview-head"><strong>Preview dữ liệu</strong><span>${state.uploadPreview.length} dòng mẫu</span></div><div class="preview-table"><table><thead><tr>${Object.keys(state.uploadPreview[0]).map(key => `<th>${esc(key)}</th>`).join('')}</tr></thead><tbody>${state.uploadPreview.map(row => `<tr>${Object.keys(state.uploadPreview[0]).map(key => `<td>${esc(row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>` : '';
  const uploadedFiles = state.uploadedFiles?.length ? `<div class="uploaded-files">${state.uploadedFiles.map(file => `<div class="uploaded-file"><span class="file-badge">${icon('file')}</span><div><strong>${esc(file.name)}</strong><small>${esc(file.size)} · ${esc(file.type || 'text data')}</small></div><button type="button" class="icon-btn" data-action="remove-upload" data-file-name="${esc(file.name)}" aria-label="Xóa ${esc(file.name)}">${icon('x')}</button></div>`).join('')}</div>` : `<p class="upload-empty">Chưa có file nào được thêm.</p>`;
  return `${pageHead('Create / Step 01','New Pipeline','Mô tả kết quả bạn cần. Planner sẽ chuyển yêu cầu thành kế hoạch có thể review.', `<span class="run-id">DRAFT · RUN #024</span>`)}${stepper()}
  <div class="grid grid-2">
    <section class="panel panel-pad">${panelTitle('Describe requirement','Các thông tin dưới đây được dùng để tạo plan và phạm vi xử lý.')}
      <div class="form-group"><label class="form-label" for="pipeline-name">Pipeline name</label><input id="pipeline-name" type="text" data-field="name" maxlength="80" value="${esc(state.name)}" /></div>
      <div class="form-group"><label class="form-label" for="prompt">What should this pipeline do? <span class="field-hint">Natural language</span></label><textarea id="prompt" data-field="prompt" placeholder="Ví dụ: Gộp đơn hàng từ 3 nguồn và tính doanh thu theo ngày.">${esc(state.prompt)}</textarea></div>
      <div class="form-group"><span class="form-label">Data sources <span class="field-hint">Nguồn kết nối</span></span><div class="source-list">${sources.map(source => `<label class="source-check"><input type="checkbox" data-source="${source}" ${state.sources.includes(source) ? 'checked' : ''}/><span>${icon('database')} ${source} ${state.sources.includes(source) ? '<b class="checkmark">✓</b>' : ''}</span></label>`).join('')}</div></div>
      <div class="form-group upload-group"><span class="form-label">Upload data <span class="field-hint">Any CSV dataset</span></span><label class="upload-drop" for="data-upload"><span class="upload-icon">${icon('plus')}</span><span><strong>Chọn file dữ liệu</strong><small>CSV hoặc JSON · tối đa 10MB mỗi file</small></span><input id="data-upload" type="file" accept=".csv,.json,text/csv,application/json" multiple /></label>${uploadedFiles}${uploadPreview}<p class="field-note">Custom CSV headers are mapped by Planner. File contents stay in the local runner; only headers and your request are sent to the Planner.</p></div>
      <div class="form-row"><div class="form-group"><label class="form-label" for="schema">Target schema</label><select id="schema" data-field="schema"><option value="analytics" ${state.schema === 'analytics' ? 'selected' : ''}>analytics</option><option value="staging" ${state.schema === 'staging' ? 'selected' : ''}>staging</option><option value="mart" ${state.schema === 'mart' ? 'selected' : ''}>mart</option></select></div><div class="form-group"><label class="form-label" for="context">Business context</label><input id="context" type="text" data-field="context" value="${esc(state.context)}" /></div></div>
      <div class="action-bar"><span class="helper">Planner sẽ hỏi thêm khi yêu cầu chưa rõ.</span>${btn(`Generate plan ${icon('arrow')}`,'generate')}</div>
    </section>
    <aside class="grid" style="align-content:start"><section class="panel panel-pad">${panelTitle('What happens next','Bốn chặng kiểm soát trước khi tạo PR.')}<ol class="plan-list"><li><span class="plan-index">1</span><span>Planner làm rõ logic và xác định dữ liệu đầu ra.</span></li><li><span class="plan-index">2</span><span>Coder sinh SQL, Tester đo baseline, rồi Optimizer thử cải thiện.</span></li><li><span class="plan-index">3</span><span>Final Tester kiểm tra SQL được chọn; nếu hiệu năng giảm quá ngưỡng sẽ rollback baseline.</span></li><li><span class="plan-index">4</span><span>Người dùng review và quyết định phê duyệt.</span></li></ol></section><div class="hint-box"><strong>Human in the loop</strong>PR và merge trong demo được khóa cho đến khi bước Human Review được Approve.</div></aside>
  </div>`;
}

function plannerView() {
  const mappings = state.sources.map(source => {
    if (source === 'Custom CSV') {
      const columns = state.uploadPreview.length ? Object.keys(state.uploadPreview[0]) : [];
      return columns.map(column => `<div class="map-row"><code>${esc(column)}</code><b>\u2192</b><span>Planner maps from request</span></div>`).join('') || '<p class="section-caption">Upload a CSV to preview its headers.</p>';
    }
    const fields = { Shopee: ['order_id', 'order_date', 'total_amount', 'status'], Tiki: ['id', 'created_at', 'amount', 'order_status'], Website: ['OrderID', 'OrderDate', 'GrandTotal', 'Status'] }[source] || [];
    return fields.map((field, index) => `<div class="map-row"><code>${esc(source.toLowerCase())}.${esc(field)}</code><b>\u2192</b><span>${['order_id','order_date','revenue','order_status'][index]}</span></div>`).join('');
  }).join('');
  return `${pageHead('Plan / Step 02','Planner & clarification','Planner sẽ phân tích yêu cầu khi bạn bấm Confirm & run agents.', `<span class="run-id">READY TO RUN PIPELINE</span>`)}${stepper()}
  <div class="grid grid-2"><div class="grid" style="align-content:start">
    <section class="panel panel-pad">${panelTitle('Requirement for Planner','Plan được tạo sau khi xác nhận')}<div class="hint-box" style="margin-bottom:12px"><strong>Yêu cầu gửi tới Planner</strong>${esc(state.prompt)}<br/><small>Runner chuẩn bị dữ liệu tại máy cục bộ; SQL Coder trả về một query theo plan.</small></div><ol class="plan-list"><li><span class="plan-index">1</span><span>Đọc nguồn đã chọn: ${state.sources.map(esc).join(', ') || 'tự nhận diện từ file đã tải lên'}.</span></li><li><span class="plan-index">2</span><span>Planner chọn bộ lọc và cách nhóm theo yêu cầu.</span></li><li><span class="plan-index">3</span><span>SQL Coder sinh query; runner ghép query vào pipeline thực thi.</span></li><li><span class="plan-index">4</span><span>Tester chạy pipeline và kiểm tra CSV đầu ra.</span></li></ol></section>
    <section class="panel panel-pad">${panelTitle('Clarification needed','Hai quyết định ảnh hưởng đến cách tính doanh thu.')}
      <div class="clarify-grid"><div class="clarify-box"><h3>Ngày doanh thu theo timezone nào?</h3><p>Dùng để quy đổi <code>ordered_at</code> trước khi nhóm theo ngày.</p><select aria-label="Timezone" data-field="timezone"><option value="Asia/Ho_Chi_Minh" ${state.timezone === 'Asia/Ho_Chi_Minh' ? 'selected' : ''}>Asia/Ho_Chi_Minh (UTC+7)</option><option value="UTC" ${state.timezone === 'UTC' ? 'selected' : ''}>UTC</option></select></div><div class="clarify-box"><h3>Đơn vị tiền tệ đầu ra?</h3><p>Chuẩn hóa giá trị trước khi tính tổng doanh thu.</p><select aria-label="Currency" data-field="currency"><option value="" disabled ${state.currency ? '' : 'selected'}>Chọn đơn vị tiền</option><option value="VND" ${state.currency === 'VND' ? 'selected' : ''}>VND</option><option value="USD" ${state.currency === 'USD' ? 'selected' : ''}>USD</option></select></div></div>
      <div class="action-bar">${btn('← Edit requirement','back-new','outline')}${btn(`Confirm & run agents ${icon('arrow')}`,'run-agents')}</div>
    </section></div>
    <aside class="grid" style="align-content:start"><section class="panel panel-pad">${panelTitle('Input field mapping','Mapping used by generated pipeline')}<div class="source-map">${mappings || '<p class="section-caption">Chọn ít nhất một nguồn dữ liệu.</p>'}</div><div class="hint-box" style="margin-top:16px"><strong>Output</strong><code>${esc(state.schema)}.fct_daily_revenue</code><br/>Day and month compare request creates adjacent daily_revenue and monthly_revenue columns.</div></section><section class="panel panel-pad">${panelTitle('Local execution')}<div class="review-item"><span>Selected CSV / JSON</span>${status(state.uploadedFiles.length ? `${state.uploadedFiles.length} ready` : 'Sample data','running')}</div><p class="section-caption" style="margin-top:10px">Custom CSV headers will be mapped by Planner for this run.</p></section></aside>
  </div>`;
}

function processingView() {
  const phase = state.processingPhase;
  const agents = [
    { name: 'Planner', detail: 'Builds the source, filter, and output plan', symbol: 'P' },
    { name: 'SQL Coder', detail: 'Generates one DuckDB query from the plan', symbol: 'SQL' },
    { name: 'Baseline Tester', detail: 'Checks original SQL and records baseline metrics', symbol: 'T1' },
    { name: 'SQL Optimizer', detail: 'Runs only after baseline correctness passes', symbol: 'SQL' },
    { name: 'Final Tester', detail: 'Checks the candidate or restored baseline', symbol: 'T2' },
    { name: 'Acceptance', detail: 'Keeps only a passing version', symbol: '?' },
    { name: 'Reviewer', detail: 'Waits for human approval', symbol: 'R' }
  ];
  const rows = agents.map((agent, index) => {
    let kind = 'waiting', label = 'Ready';
    if (state.actualRunBusy) { kind = index === 0 ? 'running' : 'waiting'; label = index === 0 ? 'Running' : 'Queued'; }
    else if (state.actualRun) {
      const run = state.actualRun;
      const optimizerStatus = run.optimizer_status || run.sql_optimization_report?.[0]?.optimizer_status;
      const baselineStatus = run.baseline_test_status || (run.optimization_phase ? undefined : run.test_status);
      if (index === 0 && run.plan) { kind = 'pass'; label = 'Pass'; }
      else if (index === 1 && (run.pipeline_code || run.sql_baseline)) { kind = 'pass'; label = 'Pass'; }
      else if (index === 2 && baselineStatus) { kind = baselineStatus === 'PASS' ? 'pass' : 'fail'; label = baselineStatus; }
      else if (index === 3 && optimizerStatus) { kind = 'pass'; label = optimizerStatus; }
      else if (index === 4 && run.test_status) { kind = run.test_status === 'PASS' ? 'pass' : 'fail'; label = run.optimizer_rollback ? `${run.test_status} · Baseline restored` : run.test_status; }
      else if (index === 5 && run.acceptance_status === 'ACCEPTED') { kind = 'pass'; label = 'Pass'; }
      else if (index === 6) { kind = 'review'; label = run.review_status === 'APPROVED' ? 'Approved' : 'Needs approval'; }
      else { kind = 'review'; label = 'Check'; }
    }
    const width = kind === 'pass' ? 100 : kind === 'running' ? 63 : 0;
    return `<div class="agent-row"><span class="agent-symbol">${agent.symbol}</span><div><strong>${agent.name}</strong><small>${agent.detail}</small></div><div class="progress-track"><div class="progress-fill ${kind === 'running' ? 'running' : ''}" style="width:${width}%"></div></div>${status(label,kind)}</div>`;
  }).join('');
  const logs = state.actualRunBusy ? [
    ['--:--:--','Request sent to local pipeline runner','run'],
    ['--:--:--','Planner → SQL Coder → Baseline Tester → SQL Optimizer → Final Tester → Acceptance → Reviewer','run']
  ] : state.actualRun ? [
    ['--:--:--','Planner: plan saved','ok'],
    ['--:--:--','SQL Coder: one query generated from the validated plan','ok'],
    ['--:--:--',`Baseline Tester: ${state.actualRun.baseline_test_status || 'not reported'} (${Number.isFinite(state.actualRun.baseline_metrics?.runtime_seconds) ? `${state.actualRun.baseline_metrics.runtime_seconds.toFixed(2)}s` : 'runtime n/a'})`,'ok'],
    ['--:--:--',`SQL Optimizer: ${state.actualRun.optimizer_status || state.actualRun.sql_optimization_report?.[0]?.optimizer_status || 'not reached'}`,'ok'],
    ['--:--:--',`Final Tester: ${state.actualRun.test_status || 'not reached'} · ${(state.actualRun.output_rows || []).length} output rows${state.actualRun.optimizer_rollback ? ' · baseline restored' : ''}`,'ok'],
    ['--:--:--',`Acceptance: ${state.actualRun.acceptance_status || 'not reached'}`,'ok'],
    ['--:--:--',`Reviewer: ${state.actualRun.review_status || 'review required'}`,'warn']
  ] : [['--:--:--','Ready to run the complete local pipeline','run']];
  const actual = state.actualRun;
  const clarificationPanel = state.clarificationRun?.status === 'clarification_required'
    ? `<section class="panel panel-pad" style="margin-top:18px"><h3>Agent cần bạn làm rõ trước khi sinh SQL</h3><p class="section-caption">Pipeline đã dừng; chưa tạo SQL hoặc kết quả doanh thu.</p>${(state.clarificationRun.clarification_questions || []).map((question, index) => {
        const field = state.clarificationRun.clarification_fields?.[index] || {};
        const current = field.file ? (state.clarification[field.key]?.[field.file] || '') : (state.clarification[field.key] || '');
        const input = field.type === 'select'
          ? `<select data-clarification-key="${esc(field.key)}" data-clarification-file="${esc(field.file || '')}"><option value="">Chọn câu trả lời</option>${(field.options || []).map(option => `<option value="${esc(option)}" ${current === option ? 'selected' : ''}>${esc(option)}</option>`).join('')}</select>`
          : `<input type="text" data-clarification-key="${esc(field.key || '')}" data-clarification-file="${esc(field.file || '')}" value="${esc(current)}" placeholder="${field.type === 'rate' ? 'Ví dụ: 25000' : 'Ví dụ: VND'}" />`;
        return `<label class="field" style="display:block;margin:12px 0"><span>${esc(question)}</span>${input}</label>`;
      }).join('')}<div class="action-bar">${btn('Gửi câu trả lời và chạy tiếp','answer-clarification','primary')}${btn('Sửa yêu cầu','back-new','outline')}</div></section>` : '';
  const genericMapping = actual?.plan?.dataset_mode === 'generic_csv' ? `<div class="hint-box" style="margin:12px 0"><strong>Planner interpretation</strong><p class="section-caption">Request: ${esc(actual.plan.request || '')}</p><p class="section-caption">Operation: ${esc(actual.plan.request_interpretation?.operation || 'single_source')}; filter: ${esc(JSON.stringify(actual.plan.request_interpretation?.filter || {}))}; group: ${esc((actual.plan.group_dimensions || []).join(', ') || 'all rows')}</p><strong>Field mapping from schema</strong>${actual.plan.files.map(file => `<p class="section-caption">${esc(file.name)}: ${Object.entries(file.fields || {}).map(([field,column]) => `${esc(column)} -> ${esc(field)}`).join(', ')}${Object.entries(file.constants || {}).map(([field,value]) => `; ${field} = ${value}`).join('')}</p>`).join('')}<p class="section-caption">Metrics: ${esc((actual.plan.metrics || []).join(', '))}</p></div>` : '';
  const actualPanel = actual ? `<section class="panel panel-pad" style="margin-top:18px">${panelTitle('Kết quả chạy thật','DuckDB pipeline output · output/fct_daily_revenue.csv',status(actual.test_status || 'UNKNOWN', actual.test_status === 'PASS' ? 'pass' : 'review'))}<div class="hint-box" style="margin-bottom:12px"><strong>Plan từ Planner</strong>Nguồn: ${esc((actual.plan?.sources || []).join(', '))} · Trạng thái hợp lệ: ${esc((actual.plan?.valid_statuses || []).join(', '))} · Nhóm theo: ${esc(actual.plan?.group_by || '')}</div><div class="action-bar" style="margin-top:0;padding-top:0;border:0"><span class="helper">Download result and run evidence</span>${btn('Tải CSV kết quả','download-result','outline')}${btn('Tải evidence JSON','download-evidence','outline')}</div><div class="preview-table"><table><thead><tr>${Object.keys(actual.output_rows?.[0] || {}).map(key => `<th>${esc(key)}</th>`).join('')}</tr></thead><tbody>${(actual.output_rows || []).map(row => `<tr>${Object.keys(row).map(key => `<td>${esc(row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${genericMapping}<details style="margin-top:14px"><summary>Runtime pipeline · generated_pipeline.py</summary><pre class="log" style="height:260px;white-space:pre-wrap">${esc(actual.pipeline_code || '')}</pre></details><p class="section-caption">${esc(actual.test_report?.stdout || '')}</p></section>` : '';
  const humanReviewPanel = actual?.test_status === 'FAIL' && actual.run_id ? `<section class="panel panel-pad" style="margin-top:18px"><div class="hint-box"><strong>Human review required</strong>Tester failure is saved with metrics. Edit the pipeline below, then submit it for retest.</div><label class="field" style="display:block;margin:14px 0"><span>generated_pipeline.py</span><textarea id="human-fix-code" spellcheck="false" style="width:100%;min-height:320px;font-family:var(--mono);font-size:12px">${esc(actual.pipeline_code || '')}</textarea></label>${btn(state.actualRunBusy ? 'Retesting' : 'Retest pipeline','submit-human-fix','primary',state.actualRunBusy ? 'disabled' : '')}<details style="margin-top:12px"><summary>Tester report and failed checks</summary><pre class="log" style="white-space:pre-wrap">${esc(JSON.stringify(actual.test_report || {}, null, 2))}</pre></details></section>` : '';
  return `${pageHead('Build / Step 03','Agent processing',state.actualRunBusy ? 'Các agent đang xử lý yêu cầu qua backend cục bộ.' : phase >= 4 ? 'Pipeline đã chạy và Tester đã kiểm tra CSV đầu ra.' : 'Bấm Confirm & run agents để chạy pipeline.', `<span class="run-id">LOCAL RUN · ${state.actualRunBusy ? 'RUNNING' : phase >= 4 ? 'COMPLETE' : 'READY'}</span>`)}${stepper()}
    <div class="grid grid-2"><section class="panel"> <div class="panel-pad" style="padding-bottom:4px">${panelTitle('Execution pipeline','Planner → SQL Coder → Baseline Tester → SQL Optimizer → Final Tester → Acceptance → Reviewer')}</div>${rows}<div class="panel-pad" style="padding-top:12px"><div class="action-bar" style="margin-top:0;border:0;padding:0"><span class="helper">${state.actualRunBusy ? 'Đang chạy pipeline cục bộ; giữ trang này mở trong lúc agent xử lý…' : actual ? 'SQL do agent sinh đã chạy qua toàn bộ pipeline.' : state.actualRunError ? esc(state.actualRunError) : 'Bấm chạy để thực thi pipeline.'}</span>${state.actualRunError ? btn('Retry agent run','run-agents','outline') : btn(`View results ${icon('arrow')}`,'to-evidence','primary',phase < 4 ? 'disabled' : '')}</div></div></section>
    <aside class="grid" style="align-content:start"><section class="panel panel-pad">${panelTitle('Live execution log',actual ? 'Local agent run' : 'Local runner')}<div class="log" aria-live="polite">${logs.map(([time,line,kind]) => `<div class="log-line"><span class="log-time">[${time}]</span> <span class="${kind}">${esc(line)}</span></div>`).join('')}</div></section><div class="hint-box"><strong>Execution sandbox</strong>Tester chạy Python pipeline trong subprocess local và kiểm tra CSV. Nhánh DuckDB sandbox riêng chỉ nhận SQL.</div></aside></div>${actualPanel}${humanReviewPanel}${clarificationPanel}`;
}

const originalSQL = [
  'SELECT DATE(ordered_at) AS revenue_date,',
  '       SUM(amount) AS gross_revenue,',
  '       COUNT(*) AS order_count',
  'FROM stg_unified_orders',
  "WHERE status != 'cancelled'",
  'GROUP BY DATE(ordered_at)',
  'ORDER BY revenue_date;'
];
const optimizedSQL = [
  'WITH filtered_orders AS (',
  '  SELECT amount, ordered_at',
  '  FROM stg_unified_orders',
  "  WHERE status != 'cancelled'",
  ')',
  'SELECT DATE(ordered_at) AS revenue_date,',
  '       SUM(amount) AS gross_revenue,',
  '       COUNT(*) AS order_count',
  'FROM filtered_orders',
  'GROUP BY 1 ORDER BY 1;'
];
function codeCard(title, tag, lines) {
  return `<div class="code-card"><div class="code-head">${title}<small>${tag}</small></div><div class="code">${lines.map((line,i) => `<div class="code-line"><span class="line-num">${i+1}</span><code>${esc(line)}</code></div>`).join('')}</div></div>`;
}

function evidenceView() {
  const actual = state.actualRun;
  const optimizerRun = actual?.sql_optimization_report?.[0];
  const optimizerDetail = actual?.sql_optimizer_details?.[0] || {};
  const baseline = (optimizerDetail.original_sql || actual?.sql_baseline || actual?.sql_query || '').split(/\r?\n/).filter(Boolean);
  const finalSql = (actual?.sql_query || '').split(/\r?\n/).filter(Boolean);
  const statusText = optimizerRun?.optimizer_status || 'Waiting for run';
  const note = optimizerRun?.optimized
    ? `A rewrite passed correctness and reached ${Number(optimizerRun.speedup || 0).toFixed(2)}x speedup.`
    : optimizerRun
      ? 'No rewrite reached the 1.1x verified speedup threshold. The original SQL was kept.'
      : 'Run the pipeline to see generated SQL and optimizer evidence.';
  const report = actual?.test_report || {};
  const metrics = report.metrics || {};
  const checks = report.checks || {};
  const passed = Object.values(checks).filter(Boolean).length;
  const metricPanel = actual
    ? `<section class="panel panel-pad" style="margin-bottom:18px"><div class="grid grid-4"><div class="metric"><small>Runtime</small><strong>${metrics.runtime_seconds == null ? '?' : `${Number(metrics.runtime_seconds).toFixed(2)}s`}</strong></div><div class="metric"><small>CPU / memory</small><strong>${metrics.cpu_seconds == null ? '?' : `${Number(metrics.cpu_seconds).toFixed(2)}s`} / ${metrics.peak_memory_mb == null ? '?' : `${Number(metrics.peak_memory_mb).toFixed(1)} MB`}</strong></div><div class="metric"><small>Optimizer</small><strong>${esc(statusText)}</strong><em>${optimizerRun?.speedup == null ? 'No accepted speedup' : `${Number(optimizerRun.speedup).toFixed(2)}x`}</em></div><div class="metric"><small>Correctness</small><strong class="${report.status === 'PASS' ? 'pass-value' : ''}">${passed}/${Object.keys(checks).length} ? ${esc(report.status || 'not run')}</strong></div></div></section>`
    : `<section class="panel panel-pad" style="margin-bottom:18px"><div class="hint-box"><strong>Evidence appears after a run</strong>Runtime, optimizer decisions, SQL, and Tester checks will be shown here.</div></section>`;
  const queryPanel = actual
    ? `<section class="panel panel-pad">${panelTitle('SQL comparison','Baseline SQL ? Final SQL',status(statusText, optimizerRun?.optimized ? 'pass' : 'review'))}<div class="sql-grid">${codeCard('Baseline SQL','before', baseline.length ? baseline : ['No SQL recorded'])}${codeCard('Final SQL','DuckDB', finalSql.length ? finalSql : ['No SQL recorded'])}</div><div class="evidence-note" style="margin-top:16px"><span><strong>Optimizer result:</strong> ${esc(note)} The candidate must preserve the baseline result before it can be selected.</span></div><details style="margin-top:14px"><summary>Optimization report</summary><pre class="log" style="white-space:pre-wrap">${esc(JSON.stringify(actual.sql_optimization_report || [], null, 2))}</pre></details></section>`
    : `<section class="panel panel-pad">${panelTitle('SQL comparison','Baseline SQL ? Final SQL',status('Waiting','running'))}<p class="section-caption">Run the pipeline to populate this comparison with real SQL.</p></section>`;
  const resultPanel = actual ? `<section class="panel panel-pad" style="margin-top:18px">${panelTitle('Pipeline result','output/fct_daily_revenue.csv',status(actual.test_status || 'UNKNOWN', actual.test_status === 'PASS' ? 'pass' : 'review'))}<div class="hint-box" style="margin-bottom:12px"><strong>Planner</strong>Sources: ${esc((actual.plan?.sources || []).join(', '))} ? Group by: ${esc(actual.plan?.group_by || '')} ? Acceptance: ${esc(actual.acceptance_status || 'not reached')}</div><div class="action-bar"><span class="helper">Download the actual run output and evidence.</span>${btn('Download CSV','download-result','outline')}${btn('Download evidence JSON','download-evidence','outline')}</div><div class="preview-table"><table><thead><tr>${Object.keys(actual.output_rows?.[0] || {}).map(key => `<th>${esc(key)}</th>`).join('')}</tr></thead><tbody>${(actual.output_rows || []).map(row => `<tr>${Object.keys(row).map(key => `<td>${esc(row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div><details style="margin-top:14px"><summary>Generated pipeline code</summary><pre class="log" style="height:260px;white-space:pre-wrap">${esc(actual.pipeline_code || '')}</pre></details></section>` : '';
  return `${pageHead('Run evidence','SQL optimizer and test evidence','Inspect the query, verified rewrite decision, runtime, and actual pipeline output.', `<span class="run-id">${esc(statusText)}</span>`)}${stepper()}${metricPanel}${queryPanel}${resultPanel}`;
}
const files = [
  { name: 'models/fct_daily_revenue.sql', plus: 10, minus: 7, lines: [
    ['hunk','@@ -1,7 +1,10 @@'],
    ['del','-SELECT DATE(ordered_at) AS revenue_date,'],
    ['del','-       SUM(amount) AS gross_revenue,'],
    ['del','-       COUNT(*) AS order_count'],
    ['del','-FROM stg_unified_orders'],
    ['del',"-WHERE status != 'cancelled'"],
    ['del','-GROUP BY DATE(ordered_at)'],
    ['del','-ORDER BY revenue_date;'],
    ['add','+WITH filtered_orders AS ('],
    ['add','+  SELECT amount, ordered_at'],
    ['add','+  FROM {{ ref("stg_unified_orders") }}'],
    ['add',"+  WHERE status != 'cancelled'"],
    ['add','+)'],
    ['add','+SELECT DATE(ordered_at) AS revenue_date,'],
    ['add','+       SUM(amount) AS gross_revenue,'],
    ['add','+       COUNT(*) AS order_count'],
    ['add','+FROM filtered_orders'],
    ['add','+GROUP BY 1 ORDER BY 1;']
  ] },
  { name: 'tests/schema.yml', plus: 11, minus: 0, lines: [
    ['hunk','@@ -0,0 +1,11 @@'],
    ['add','+version: 2'],
    ['add','+models:'],
    ['add','+  - name: fct_daily_revenue'],
    ['add','+    description: Daily revenue from unified orders'],
    ['add','+    columns:'],
    ['add','+      - name: revenue_date'],
    ['add','+        tests: [not_null, unique]'],
    ['add','+      - name: gross_revenue'],
    ['add','+        tests: [not_null]'],
    ['add','+      - name: order_count'],
    ['add','+        tests: [not_null]']
  ] },
  { name: 'dags/daily_revenue.py', plus: 12, minus: 0, lines: [
    ['hunk','@@ -0,0 +1,12 @@'],
    ['add','+from airflow import DAG'],
    ['add','+from airflow.operators.bash import BashOperator'],
    ['add','+from datetime import datetime'],
    ['add','+'],
    ['add','+with DAG('],
    ['add','+    dag_id="daily_revenue",'],
    ['add','+    start_date=datetime(2026, 9, 1),'],
    ['add','+    schedule="@daily",'],
    ['add','+    catchup=False,'],
    ['add','+) as dag:'],
    ['add','+    run_dbt = BashOperator('],
    ['add','+        task_id="run_dbt", bash_command="dbt run --select fct_daily_revenue")']
  ] }
];

function diffPanel(compact = false) {
  const file = files[state.selectedFile] || files[0];
  return `<div class="file-tabs">${files.map((f,i) => `<button class="file-tab ${state.selectedFile === i ? 'active' : ''}" data-action="file" data-file="${i}">${esc(f.name.split('/').pop())}<b>+${f.plus}</b></button>`).join('')}</div>
    <div class="diff"><div class="diff-head">${esc(file.name)} · ${compact ? 'review preview' : 'proposed changes'}</div>${file.lines.slice(0,compact ? 11 : undefined).map(([kind,line],i) => `<div class="diff-line ${kind}"><span class="num">${kind === 'hunk' ? '··' : i}</span><code>${esc(line)}</code></div>`).join('')}</div><div class="diff-footer"><span><b>+${file.plus}</b> additions</span><span><b>−${file.minus}</b> deletions</span><span>3 changed files</span></div>`;
}

function diffView() {
  const actual = state.actualRun;
  const pipeline = actual?.pipeline_code || '';
  const sql = actual?.sql_query || '';
  const output = actual?.output_rows || [];
  return `${pageHead('Changes / Step 05','Generated artifacts','Review the code, SQL, and output produced by this run.', `<span class="run-id">LOCAL PIPELINE</span>`)}${stepper()}
  <div class="grid grid-2"><section class="panel panel-pad">${panelTitle('Pipeline code','generated_pipeline.py',status(actual?.test_status || 'Not run',actual?.test_status === 'PASS' ? 'pass' : 'review'))}<details open><summary>Generated Python pipeline</summary><pre class="log" style="height:300px;white-space:pre-wrap">${esc(pipeline || 'Run the pipeline to generate code.')}</pre></details><div class="action-bar"><span class="helper">Review the artifacts generated by this pipeline run.</span>${btn(`Continue to review ${icon('arrow')}`,'to-review','primary',!actual?.run_id ? 'disabled' : '')}</div></section><aside class="grid" style="align-content:start"><section class="panel panel-pad">${panelTitle('Generated SQL','generated_query.sql',status(actual?.sql_optimization_report?.[0]?.optimizer_status || 'Not run','running'))}<pre class="log" style="max-height:300px;white-space:pre-wrap">${esc(sql || 'SQL appears after a run.')}</pre></section><section class="panel panel-pad">${panelTitle('Output preview',`${output.length} rows`)}<div class="review-item"><span>Sources</span><b>${esc((actual?.used_sources || []).join(', ') || '—')}</b></div><div class="review-item"><span>Tester</span><b>${esc(actual?.test_status || 'Not run')}</b></div><div class="review-item"><span>Acceptance</span><b>${esc(actual?.acceptance_status || 'Pending')}</b></div><div class="hint-box" style="margin-top:12px"><strong>Delivery step</strong>PR and Merge are simulated in this UI; generated files remain in the run workspace.</div></section></aside></div>`;
}

function reviewView() {
  const actual = state.actualRun;
  const checks = actual?.test_report?.checks || {};
  const passedChecks = Object.values(checks).filter(Boolean).length;
  const checkCount = Object.keys(checks).length;
  const metrics = actual?.test_report?.metrics || {};
  const optimizer = actual?.sql_optimization_report?.[0] || {};
  const bannerClass = state.rejected ? 'rejected' : state.approved ? 'approved' : '';
  const bannerTitle = state.rejected ? 'Changes rejected' : state.approved ? 'Approved for PR' : 'Awaiting human approval';
  const bannerText = state.rejected ? `Lý do: ${esc(state.rejectionReason)}` : state.approved ? 'Bạn đã xác nhận diff, test và evidence. Bước tạo PR hiện đã mở.' : 'Kiểm tra toàn bộ kết quả bên dưới trước khi Approve hoặc Reject.';
  return `${pageHead('Decision / Step 06','Human review','Một quyết định rõ ràng trước khi pipeline được đưa vào quy trình PR / merge.', `<span class="run-id">RUN · ${esc(actual?.run_id?.slice(0,8) || 'REVIEW')}</span>`)}${stepper()}
    <div class="review-banner ${bannerClass}"><span class="badge-icon">${icon(state.rejected ? 'x' : state.approved ? 'check' : 'clock')}</span><div><strong>${bannerTitle}</strong><p>${bannerText}</p></div></div>
    <div class="grid grid-2"><div class="grid" style="align-content:start"><section class="panel panel-pad">${panelTitle('Generated pipeline','Coder output · actual run')}<div class="review-item"><span>Sources</span><b>${esc((actual?.used_sources || []).join(', ') || '—')}</b></div><div class="review-item"><span>SQL</span><b>${esc(actual?.sql_optimization_report?.[0]?.optimizer_status || 'Not run')}</b></div><details style="margin-top:12px"><summary>Review generated code</summary><pre class="log" style="max-height:300px;white-space:pre-wrap">${esc(actual?.pipeline_code || '')}</pre></details><button class="text-link" style="margin-top:15px" data-action="to-diff">View generated artifacts →</button></section>
      <section class="panel panel-pad">${panelTitle('Run evidence','SQL optimizer and Tester results')}<div class="grid grid-4" style="gap:9px"><div class="hint-box"><strong>Runtime</strong>${metrics.runtime_seconds == null ? '—' : `${Number(metrics.runtime_seconds).toFixed(2)}s`}</div><div class="hint-box"><strong>SQL Optimizer</strong>${esc(optimizer.optimizer_status || 'Not run')}</div><div class="hint-box"><strong>Equivalent</strong><span style="color:var(--green);font-weight:700">${esc(actual?.test_status || '—')}</span></div><div class="hint-box"><strong>Tests</strong><span style="color:var(--green);font-weight:700">${passedChecks}/${checkCount || '—'} pass</span></div></div><button class="text-link" style="margin-top:15px" data-action="to-evidence">View full evidence →</button></section></div>
      <aside class="grid" style="align-content:start"><section class="panel"><div class="review-card-title"><h2>Review decision</h2>${status(state.rejected ? 'Rejected' : state.approved ? 'Approved' : 'Pending',state.rejected ? 'fail' : state.approved ? 'approved' : 'review')}</div><div class="review-card-body"><div class="review-item"><span>Generated artifacts</span><b>${[actual?.pipeline_code, actual?.sql_query, actual?.output_rows?.length].filter(Boolean).length} available</b></div><div class="review-item"><span>Automated tests</span><b style="color:var(--green)">${passedChecks} / ${checkCount || '—'} pass</b></div><div class="review-item"><span>Acceptance gate</span><b>${esc(actual?.acceptance_status || 'Pending')}</b></div><div class="review-item"><span>SQL rewrite</span><b>${esc(optimizer.optimizer_status || 'Not run')}${optimizer.speedup ? ` · ${Number(optimizer.speedup).toFixed(2)}x` : ''}</b></div><div class="review-item"><span>Execution</span><b>Local pipeline</b></div>
      ${state.approved ? `<div class="action-bar">${btn(`Continue to PR ${icon('arrow')}`,'to-pr')}</div>` : state.rejected ? `<div class="action-bar">${btn('Revise requirement','revise','outline')}</div>` : `<div class="action-bar" style="display:block"><div class="approval-actions">${btn('Reject','show-reject','danger',!actual?.run_id || actual.test_status !== 'PASS' || actual.acceptance_status !== 'ACCEPTED' ? 'disabled' : '')}${btn(`${icon('check')} Approve`,'approve','primary',!actual?.run_id || actual.test_status !== 'PASS' || actual.acceptance_status !== 'ACCEPTED' ? 'disabled' : '')}</div><div class="lock-note">${actual?.run_id && actual.test_status === 'PASS' && actual.acceptance_status === 'ACCEPTED' ? `${icon('lock')} The Reviewer records this decision in the pipeline.` : 'A passing Tester result and Acceptance decision are required.'}</div></div>`}
      ${state.showReject && !state.rejected && !state.approved ? `<div class="reject-form"><label class="form-label" for="rejection">Reason for rejection</label><textarea id="rejection" data-field="rejectionReason" placeholder="Ví dụ: Cần đối soát hoàn tiền trước khi tính doanh thu.">${esc(state.rejectionReason)}</textarea><div class="approval-actions" style="margin-top:10px">${btn('Cancel','cancel-reject','outline')}${btn('Confirm rejection','confirm-reject','danger')}</div></div>` : ''}
      </div></section><div class="hint-box"><strong>Decision rule</strong>Approve mở bước Create PR. Reject đưa yêu cầu về vòng chỉnh sửa.</div></aside></div>`;
}

function prView() {
  if (!state.approved) return reviewView();
  const statusLabel = state.merged ? 'Merged' : state.prCreated ? 'PR open' : 'Approved';
  return `${pageHead('Delivery / Step 07','Create PR / Merge','Sau khi phê duyệt, tạo PR mẫu và mô phỏng bước merge trong workflow.', status(statusLabel,state.merged ? 'merged' : 'approved'))}${stepper()}
    <div class="grid grid-2"><section class="panel panel-pad">${panelTitle(state.merged ? 'Pipeline merged' : state.prCreated ? 'Pull request created' : 'Ready to create pull request',state.prCreated ? 'PR #128 · feat/daily-revenue → main' : 'Human review đã được Approve.')}
      ${state.prCreated ? `<div class="pr-number">#128</div><p class="pr-detail"><strong style="color:var(--ink);font-size:13px">Add daily revenue unification pipeline</strong><br/>3 files changed · 11/11 tests pass · runtime 7.1s</p>` : `<div class="hint-box"><strong>Ready for delivery</strong>Diff, tests và benchmark đã được phê duyệt. Tạo pull request để hoàn tất bước review mã nguồn.</div>`}
      <div class="action-bar"><span class="helper">${state.merged ? 'Đã hoàn tất workflow demo.' : state.prCreated ? 'PR mẫu đã được tạo. Merge để kết thúc flow.' : 'Chỉ tạo PR sau khi Human Review được Approve.'}</span>${state.merged ? btn(`${icon('check')} Merged`,'noop','outline','disabled') : state.prCreated ? btn(`${icon('git')} Merge PR`,'merge','dark') : btn(`${icon('branch')} Create PR`,'create-pr')}</div>
    </section><aside class="panel panel-pad">${panelTitle('Workflow','Trạng thái từng gate.')}<div class="pr-step done"><span class="pr-step-number">${icon('check')}</span><div><strong>Human approval</strong><p>Reviewer đã phê duyệt lần chạy #024.</p></div></div><div class="pr-step ${state.prCreated ? 'done' : ''}"><span class="pr-step-number">${state.prCreated ? icon('check') : '2'}</span><div><strong>Create PR</strong><p>${state.prCreated ? 'PR #128 đã được tạo trong demo.' : 'Chờ thao tác tạo pull request.'}</p></div></div><div class="pr-step ${state.merged ? 'done' : ''}"><span class="pr-step-number">${state.merged ? icon('check') : '3'}</span><div><strong>Merge</strong><p>${state.merged ? 'Thay đổi đã được merge trong demo.' : 'Chỉ mở sau khi PR được tạo.'}</p></div></div></aside></div>`;
}

function render() {
  const views = { dashboard: dashboardView, new: newView, planner: plannerView, processing: processingView, evidence: evidenceView, diff: diffView, review: reviewView, pr: prView };
  const active = document.activeElement;
  const restore = active && root.contains(active) ? (active.id ? `#${active.id}` : active.dataset.source ? `[data-source="${active.dataset.source}"]` : active.dataset.file ? `[data-file="${active.dataset.file}"]` : '') : '';
  root.innerHTML = shell(views[state.screen]());
  if (restore) root.querySelector(restore)?.focus({ preventScroll: true });
  save();
}

function navigate(screen) {
  const index = steps.findIndex(([id]) => id === screen);
  if (index < 0 || index > state.maxStep || (index >= 4 && state.processingPhase !== 4) || (screen === 'pr' && !state.approved)) return;
  state.screen = screen;
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function advance(screen) {
  const index = steps.findIndex(([id]) => id === screen);
  state.maxStep = Math.max(index, state.maxStep);
  state.screen = screen;
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function toast(message) {
  document.querySelector('.toast')?.remove();
  const el = document.createElement('div');
  el.className = 'toast'; el.setAttribute('role','status'); el.setAttribute('aria-live','polite'); el.textContent = message; document.body.appendChild(el);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.remove(), 3300);
}
function downloadFile(name, contents, type) {
  const url = URL.createObjectURL(new Blob([contents], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function stopSimulation() { simulationTimers.forEach(clearTimeout); simulationTimers = []; }
function startSimulation() {
  stopSimulation();
  if (!state.clarification.target_currency && state.currency) state.clarification.target_currency = state.currency;
  if (state.clarificationRun?.status === 'clarification_required') {
    const missing = (state.clarificationRun.clarification_fields || []).some(field =>
      !String(field.file ? state.clarification[field.key]?.[field.file] || '' : state.clarification[field.key] || '').trim());
    if (missing) { toast('Vui lòng trả lời tất cả câu hỏi trước khi chạy tiếp.'); return; }
  }
  const missingUpload = state.uploadedFiles.find(file => !uploadContents[file.name]);
  if (missingUpload) { toast(`Hãy chọn lại ${missingUpload.name} để gửi file cho agent.`); return; }
  state.maxStep = 2;
  state.processingPhase = 0;
  state.approved = false; state.rejected = false; state.rejectionReason = '';
  state.showReject = false; state.prCreated = false; state.merged = false;
  state.actualRun = null; state.actualRunError = ''; state.actualRunBusy = true;
  advance('processing');
  state.processingPhase = 1; render();
  fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request: state.prompt, clarification: state.clarification, selected_sources: state.sources.map(source => source.toLowerCase()), uploads: state.uploadedFiles.map(file => ({ name: file.name, content: uploadContents[file.name] })) }) })
    .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Agent run failed'); return data; })
    .then(data => {
      state.actualRunBusy = false;
      if (data.status === 'clarification_required') {
        state.clarificationRun = data; state.processingPhase = 1;
      } else {
        state.clarificationRun = null; state.actualRun = data; state.processingPhase = 4;
      }
      render();
    })
    .catch(error => { state.actualRunBusy = false; state.actualRunError = `${error.message}. Hãy mở UI qua scripts/web_server.py.`; render(); });
}
async function submitHumanFix() {
  const code = document.getElementById('human-fix-code')?.value;
  if (!state.actualRun?.run_id || !code) { toast('Pipeline code or pending run is missing.'); return; }
  state.actualRunBusy = true; state.actualRunError = ''; render();
  try {
    const response = await fetch('/api/retest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: state.actualRun.run_id, pipeline_code: code }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Retest failed');
    state.actualRun = data; state.actualRunBusy = false; state.processingPhase = 4; render();
    toast(data.test_status === 'PASS' ? 'Retest PASS; version saved.' : 'Retest still fails; edit the code and try again.');
  } catch (error) { state.actualRunBusy = false; state.actualRunError = error.message; render(); }
}
async function submitReview(decision, reason = '') {
  if (!state.actualRun?.run_id) { toast('Không tìm thấy pipeline đang chờ review.'); return; }
  state.actualRunBusy = true; state.actualRunError = ''; render();
  try {
    const response = await fetch('/api/review', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: state.actualRun.run_id, decision, reason })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Review decision failed');
    state.actualRun = data;
    state.actualRunBusy = false;
    state.showReject = false;
    state.approved = decision === 'approve' && data.review_status === 'APPROVED';
    state.rejected = decision === 'reject' && data.review_status === 'REJECTED';
    state.prCreated = false; state.merged = false;
    if (state.rejected) { render(); toast('Reviewer đã ghi nhận quyết định từ chối.'); }
    else if (state.approved) { advance('pr'); toast('Reviewer đã phê duyệt pipeline.'); }
    else { render(); throw new Error('Reviewer did not approve this pipeline.'); }
  } catch (error) { state.actualRunBusy = false; state.actualRunError = error.message; render(); }
}

function reset() {
  stopSimulation(); state = { ...defaults, sources: [...defaults.sources],
    clarification: emptyClarification() }; render();
  toast('Demo đã được khởi động lại.');
}
function invalidateFromInputs(field) {
  const requirementFields = ['name','prompt','schema','context'];
  const clarificationFields = ['timezone','currency'];
  if (requirementFields.includes(field)) state.maxStep = 1;
  else if (clarificationFields.includes(field)) state.maxStep = 2;
  else return;
  state.processingPhase = 0;
  state.approved = false; state.rejected = false; state.showReject = false;
  state.prCreated = false; state.merged = false;
  stopSimulation();
  if (requirementFields.includes(field)) {
    state.clarificationRun = null;
    state.clarification = emptyClarification();
  }
}

root.addEventListener('input', event => {
  if (event.target.dataset.clarificationKey) {
    const key = event.target.dataset.clarificationKey;
    const file = event.target.dataset.clarificationFile;
    if (file) { state.clarification[key] = state.clarification[key] || {}; state.clarification[key][file] = event.target.value.trim(); }
    else state.clarification[key] = event.target.value.trim();
    return;
  }
  const field = event.target.dataset.field;
  if (field && Object.prototype.hasOwnProperty.call(state,field)) { if (state[field] !== event.target.value) invalidateFromInputs(field); state[field] = event.target.value; save(); }
});
root.addEventListener('change', event => {
  if (event.target.dataset.clarificationKey) {
    const key = event.target.dataset.clarificationKey;
    const file = event.target.dataset.clarificationFile;
    if (file) { state.clarification[key] = state.clarification[key] || {}; state.clarification[key][file] = event.target.value.trim(); }
    else state.clarification[key] = event.target.value.trim();
    return;
  }
  if (event.target.dataset.source) {
    const source = event.target.dataset.source;
    invalidateFromInputs('prompt');
    state.sources = event.target.checked ? [...new Set([...state.sources,source])] : state.sources.filter(item => item !== source);
    render();
  }
  const field = event.target.dataset.field;
  if (field && Object.prototype.hasOwnProperty.call(state,field)) {
    if (state[field] !== event.target.value) invalidateFromInputs(field);
    state[field] = event.target.value;
    if (field === 'currency') { state.clarification.target_currency = event.target.value; state.clarification.conversion_rates = {}; }
    save();
  }
});
root.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;
  if (action === 'download-result' || action === 'download-evidence') {
    const run = state.actualRun;
    if (!run) { toast('Chưa có kết quả để tải.'); return; }
    if (action === 'download-result') {
      const outputFiles = Array.isArray(run.output_files) ? run.output_files : [];
      if (outputFiles.length) {
        const csvCell = value => `"${String(value ?? '').replace(/"/g, '""')}"`;
        let downloaded = 0;
        for (const output of outputFiles) {
          const rows = Array.isArray(output.rows) ? output.rows : [];
          const fileName = String(output.file_name || 'pipeline_output.csv').split(/[\\/]/).pop();
          const format = String(output.format || fileName.split('.').pop() || 'csv').toLowerCase();
          if (format === 'json') {
            downloadFile(fileName, JSON.stringify(rows, null, 2), 'application/json;charset=utf-8');
          } else if (format === 'csv') {
            const columns = Array.isArray(output.columns) && output.columns.length
              ? output.columns
              : Object.keys(rows[0] || {});
            const csv = '\uFEFF' + [columns.map(csvCell).join(','), ...rows.map(row => columns.map(column => csvCell(row[column])).join(','))].join('\r\n');
            downloadFile(fileName, csv, 'text/csv;charset=utf-8');
          } else {
            continue;
          }
          downloaded += 1;
        }
        if (!downloaded) { toast('No supported CSV or JSON result files are available.'); return; }
        toast(`Started download for ${downloaded} result file${downloaded === 1 ? '' : 's'}.`);
        return;
      }
      const rows = Array.isArray(run.output_rows) ? run.output_rows : [];
      if (!rows.length) { toast('Pipeline has no result rows yet. Check the Tester report before downloading.'); return; }
      const columns = Object.keys(rows[0] || {});
      const csvCell = value => `"${String(value ?? '').replace(/"/g, '""')}"`;
      const csv = '\uFEFF' + [columns.map(csvCell).join(','), ...rows.map(row => columns.map(column => csvCell(row[column])).join(','))].join('\r\n');
      downloadFile('fct_daily_revenue.csv', csv, 'text/csv;charset=utf-8');
    } else {
      const evidence = {
        created_at: new Date().toISOString(),
        status: run.status,
        test_status: run.test_status,
        baseline_test_status: run.baseline_test_status,
        baseline_metrics: run.baseline_metrics,
        candidate_test_status: run.candidate_test_status,
        candidate_metrics: run.candidate_metrics,
        fallback_test_status: run.fallback_test_status,
        fallback_metrics: run.fallback_metrics,
        performance_metrics: run.performance_metrics,
        optimization_phase: run.optimization_phase,
        optimizer_status: run.optimizer_status,
        optimization_thresholds: run.optimization_thresholds,
        optimizer_rollback: run.optimizer_rollback,
        review_status: run.review_status,
        used_sources: run.used_sources,
        plan: run.plan,
        test_report: run.test_report,
        review_decision: run.review_decision,
        output_rows: run.output_rows,
        output_files: run.output_files,
        output_manifest: run.output_manifest,
        generated_pipeline: run.pipeline_code,
        sql_baseline: run.sql_baseline,
        sql_query: run.sql_query,
        sql_optimization_report: run.sql_optimization_report
      };
      downloadFile('flowforge_run_evidence.json', JSON.stringify(evidence, null, 2), 'application/json;charset=utf-8');
    }
    return;
  }
  if (action === 'dashboard' || action === 'nav-pipelines') { navigate('dashboard'); return; }
  if (action === 'nav-reviews') { if (state.maxStep >= 6) navigate('review'); else toast('Chưa có review nào trong lần chạy hiện tại.'); return; }
  if (action === 'nav-sources') { state.activeModal = 'sources'; render(); return; }
  if (action === 'nav-settings') { state.activeModal = 'settings'; render(); return; }
  if (action === 'help') { state.activeModal = 'help'; render(); return; }
  if (action === 'close-modal') {
    const clickedInsideModal = event.target.closest('[data-modal-content]');
    const clickedClose = event.target.closest('[data-action="close-modal"]') === target;
    if (clickedInsideModal && !clickedClose) return;
    state.activeModal = ''; render(); return;
  }
  if (action === 'step') { navigate(target.dataset.step); return; }
  if (action === 'reset') { reset(); return; }
  if (action === 'new') { stopSimulation(); state = { ...defaults, sources: [...defaults.sources],
    clarification: emptyClarification(), screen: 'new', maxStep: 1 }; render(); return; }
  if (action === 'open-sample') { stopSimulation(); state = { ...defaults, sources: [...defaults.sources], processingPhase: 4, maxStep: 6, screen: 'review' }; render(); return; }
  if (action === 'preview-only') { toast('Hãy mở Daily Revenue Unification để xem flow mẫu.'); return; }
  if (action === 'generate') {
    if (!state.name.trim() || !state.prompt.trim()) { toast('Vui lòng nhập tên pipeline và yêu cầu.'); return; }
    if (!state.sources.length && !state.uploadedFiles.length) { toast('Vui lòng chọn data source hoặc upload ít nhất một file.'); return; }
    state.maxStep = 1; state.processingPhase = 0;
    state.approved = false; state.rejected = false; state.prCreated = false; state.merged = false; state.showReject = false;
    advance('planner'); return;
  }
  if (action === 'back-new') { navigate('new'); return; }
  if (action === 'revise') { state.maxStep = 1; state.screen = 'new'; state.approved = false; state.rejected = false; state.prCreated = false; state.merged = false; render(); return; }
  if (action === 'run-agents' || action === 'answer-clarification') { startSimulation(); return; }
  if (action === 'submit-human-fix') { submitHumanFix(); return; }
  if (action === 'to-evidence') { if (state.processingPhase === 4) advance('evidence'); return; }
  if (action === 'to-diff') { advance('diff'); return; }
  if (action === 'to-review') { advance('review'); return; }
  if (action === 'file') { state.selectedFile = Number(target.dataset.file) || 0; render(); return; }
  if (action === 'remove-upload') {
    delete uploadContents[target.dataset.fileName];
    state.uploadedFiles = state.uploadedFiles.filter(file => file.name !== target.dataset.fileName);
    if (!state.uploadedFiles.some(file => /\.csv$/i.test(file.name))) {
      state.sources = state.sources.filter(source => source !== 'Custom CSV');
      if (!state.sources.length) state.sources = [...defaults.sources];
    }
    if (!state.uploadedFiles.length) state.uploadPreview = [];
    invalidateFromInputs('prompt'); render(); return;
  }
  if (action === 'show-reject') { state.showReject = true; render(); return; }
  if (action === 'cancel-reject') { state.showReject = false; render(); return; }
  if (action === 'confirm-reject') {
    if (!state.rejectionReason.trim()) { toast('Vui lòng ghi lý do từ chối.'); return; }
    submitReview('reject', state.rejectionReason); return;
  }
  if (action === 'approve') { if (state.rejected || state.processingPhase !== 4 || state.maxStep < 6) return; submitReview('approve'); return; }
  if (action === 'to-pr') { navigate('pr'); return; }
  if (action === 'create-pr') { if (!state.approved) return; state.prCreated = true; render(); toast('PR #128 đã được tạo trong demo.'); return; }
  if (action === 'merge') { if (!state.approved || !state.prCreated) return; state.merged = true; render(); toast('PR #128 đã được merge trong demo.'); return; }
});

root.addEventListener('change', async event => {
  if (event.target.id !== 'data-upload' || !event.target.files?.length) return;
  const files = [...event.target.files];
  if (files.length + state.uploadedFiles.length > 10) { toast('Upload up to 10 files per run.'); return; }
  const tooLarge = files.find(file => file.size > 10 * 1024 * 1024);
  if (tooLarge) { toast(`${tooLarge.name} vượt quá giới hạn 10MB.`); return; }
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0) + state.uploadedFiles.reduce((sum, file) => sum + (uploadContents[file.name]?.length || 0), 0);
  if (totalBytes > 20 * 1024 * 1024) { toast('Tổng dung lượng file tải lên tối đa 20MB.'); return; }
  invalidateFromInputs('prompt');
  state.uploadedFiles = [...state.uploadedFiles, ...files.map(file => ({ name: file.name, size: formatBytes(file.size), type: file.type }))].filter((file, index, list) => list.findIndex(item => item.name === file.name) === index);
  if (files.some(file => /\.csv$/i.test(file.name))) state.sources = ['Custom CSV'];
  else { state.sources = state.sources.filter(source => source !== 'Custom CSV'); if (!state.sources.length) state.sources = [...defaults.sources]; }
  const previewFile = files.find(file => /\.json$|application\/json/i.test(`${file.name} ${file.type}`)) || files[0];
  try {
    const contents = await Promise.all(files.map(file => file.text()));
    files.forEach((file, index) => { uploadContents[file.name] = contents[index]; });
    try { state.uploadPreview = parsePreview(uploadContents[previewFile.name] || '', previewFile.name); }
    catch { state.uploadPreview = []; toast('Không đọc được file. Hãy dùng CSV hoặc JSON hợp lệ.'); }
    render(); toast(`${files.length} file đã được thêm vào pipeline.`);
  } catch { toast('Không đọc được nội dung file đã chọn.'); }
});

function formatBytes(bytes) { if (bytes < 1024) return `${bytes} B`; if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`; return `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function parsePreview(text, name) {
  if (/\.json$/i.test(name)) {
    const data = JSON.parse(text); const rows = Array.isArray(data) ? data : [data]; return rows.slice(0, 5).map(row => typeof row === 'object' && row ? row : { value: row });
  }
  const lines = text.split(/\r?\n/).filter(Boolean).slice(0, 6); if (lines.length < 2) return [];
  const headers = lines[0].split(',').map(item => item.trim().replace(/^"|"$/g, ''));
  return lines.slice(1).map(line => { const cells = line.split(',').map(item => item.trim().replace(/^"|"$/g, '')); return Object.fromEntries(headers.map((header, index) => [header || `column_${index + 1}`, cells[index] || ''])); });
}

render();

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.activeModal) { state.activeModal = ''; render(); }
});
