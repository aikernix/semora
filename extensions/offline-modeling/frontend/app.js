const $ = s => document.querySelector(s)
const detail = $('#detail')
const base = './v1/imports'
let current, editId, timer, activeId
let products = []
const statusNames = { importing:'解析中', preview:'待确认导入', ready:'已导入', failed:'导入失败', active:'已生效', pending:'待确认', disabled:'已停用', rejected:'已拒绝' }
function elem(tag, value, parent, className) {
  const node = document.createElement(tag)
  if (value != null) node.textContent = value
  if (className) node.className = className
  parent && parent.append(node)
  return node
}
function button(parent, label, action, secondary = true) {
  const node = elem('button', label, parent, secondary ? 'secondary' : '')
  node.type = 'button'
  node.onclick = () => work(async () => {
    node.disabled = true
    try { await action() } finally { node.disabled = false }
  })
  return node
}
async function request(path, options) {
  const response = await fetch(path, options)
  const result = await response.json()
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : '请检查所填信息')
  return result
}
async function work(action) {
  const notice = $('dialog[open] [role=alert]') || $('#notice')
  notice.textContent = ''
  try { await action() } catch (err) { notice.textContent = err.message }
}
const post = (path, body, method = 'POST') => request(path, {method, ...(body ? {body:JSON.stringify(body),headers:{'Content-Type':'application/json'}} : {})})
async function refresh(id = activeId) {
  activeId = id
  clearTimeout(timer)
  const rows = await request(base)
  if (id !== activeId) return
  const nav = $('#sources')
  nav.replaceChildren()
  rows.forEach(row => button(nav, row.name + ' · ' + statusNames[row.status], () => refresh(row.id)))
  if (!id) return
  const state = await request(base + '/' + id)
  if (id !== activeId) return
  current = state
  render(state)
  if (state.status === 'importing' || state.analysis === '运行中') timer = setTimeout(() => work(() => refresh(id)), 1800)
}
function render(project) {
  detail.replaceChildren()
  elem('h2', project.name, detail)
  elem('p', [statusNames[project.status], project.config.product_label || project.config.dialect, project.config.product_label ? project.config.dialect : null, project.config.version, project.config.namespace].filter(Boolean).join(' · '),detail)
  if (project.error) elem('p',project.error,detail,'warning')
  const path = base + '/' + project.id
  if (project.status === 'failed') button(detail,'用原文件重试',async () => {
    await post(path + '/retry')
    await refresh()
  })
  if (!project.tables) return
  if (project.status === 'preview') button(detail, '确认导入并固定资料', async () => {
    if (confirm('确认后无法替换 DDL 或补传文档，是否导入？')) {
      await post(path + '/confirm')
      await refresh()
    }
  }, false)
  if (project.warnings.length) {
    const block = elem('details',null,detail)
    elem('summary','导入提示（' + project.warnings.length + '）',block)
    project.warnings.forEach(w => elem('p',w,block,'warning'))
  }
  const tables = elem('details',null,detail)
  elem('summary','表目录（' + Object.keys(project.tables).length + '）',tables)
  for (const table of Object.values(project.tables)) {
    const item = elem('details',null,tables)
    elem('summary',table.id + (table.comment ? ' · ' + table.comment : ''),item)
    for (const c of table.columns) elem('small',[c.name,c.type,c.comment].filter(Boolean).join(' · '),item,'field')
  }
  const actions = elem('div',null,detail,'actions')
  elem('h3','关系（' + Object.keys(project.relations).length + '）',actions)
  if (project.status === 'ready') {
    button(actions,'新增关联',() => openRelation())
    const analyze = button(actions,'分析关联',async () => { await post(path + '/analysis'); await refresh() })
    analyze.disabled = project.analysis === '运行中'
  }
  if (project.analysis) elem('small',project.analysis,detail)
  for (const [id, r] of Object.entries(project.relations)) {
    const item = elem('article',null,detail)
    elem('strong',r.source + ' → ' + r.target,item)
    elem('p',r.pairs.map(([a,b]) => a + ' = ' + b).join(' AND ') + (r.condition ? ' AND ' + r.condition : ''),item)
    elem('small',[statusNames[r.status],r.cardinality].join(' · '),item)
    if (r.status === 'pending' && r.review_reason) elem('p',r.review_reason,item)
    if (project.status !== 'ready') continue
    const buttons = elem('div',null,item,'actions')
    button(buttons,'编辑',() => openRelation(id,r))
    const setStatus = (label,status) => button(buttons,label,async () => {
      await post(path + '/relations/' + id,{...r,status},'PUT'); await refresh()
    })
    if (r.status === 'pending') {
      if (r.cardinality === 'unknown') elem('small','请编辑并选择基数后生效',buttons)
      else setStatus('确认','active')
      setStatus('拒绝','rejected')
    }
    if (r.status === 'active') setStatus('停用','disabled')
    if (r.status === 'disabled' || (r.status === 'rejected' && r.cardinality !== 'unknown')) setStatus('恢复','active')
  }
}
function options(select, values, selected) {
  select.replaceChildren()
  for (const value of values) {
    const option = elem('option',value,select)
    option.value = value
  }
  if (values.includes(selected)) select.value = selected
}
const relationForm = $('#relation-form')
const picks = relationForm.elements
function addPair(left, right) {
  const pair = elem('div',null,$('#pairs'),'row')
  function picker(side, selected) {
    const select = elem('select',null,pair)
    select.setAttribute('aria-label',side === 'source' ? '来源字段' : '目标字段')
    options(select,current.tables[picks[side].value]?.columns.map(c => c.name) || [],selected)
  }
  picker('source',left)
  picker('target',right)
  button(pair,'移除',() => { if ($('#pairs').children.length > 1) pair.remove() })
}
function resetPairs() { $('#pairs').replaceChildren(); addPair() }
function openRelation(id,r) {
  editId = id
  const tables = Object.keys(current.tables)
  options(picks.source,tables,r?.source)
  options(picks.target,tables,r?.target || tables[1])
  picks.cardinality.value = r?.cardinality || 'unknown'
  picks.condition.value = r?.condition || ''
  $('#pairs').replaceChildren()
  for (const [a,b] of r?.pairs || [[null,null]]) addPair(a,b)
  $('#relation-dialog').showModal()
}
picks.source.onchange = resetPairs
picks.target.onchange = resetPairs
$('#add-pair').onclick = () => addPair()
const importForm = $('#import-form')
function updateModes() {
  const modes = products.find(p => p.id === importForm.elements.product.value)?.modes || []
  const select = importForm.elements.compatibility_mode
  select.replaceChildren(new Option('请选择兼容模式',''))
  if (modes.length === 1) select.replaceChildren()
  for (const mode of modes) select.add(new Option(mode.label,mode.id))
  $('#mode-label').hidden = modes.length === 1
}
importForm.elements.product.onchange = updateModes
$('#new').onclick = () => work(async () => {
  if (!products.length) {
    products = await request('./v1/products')
    for (const p of products) importForm.elements.product.add(new Option(p.label,p.id))
  }
  updateModes()
  $('#import-dialog [role=alert]').textContent = ''
  $('#import-dialog').showModal()
})
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close())
function submit(form, action) {
  form.onsubmit = event => {
    event.preventDefault()
    work(async () => {
      const btn = form.querySelector('[type=submit]')
      btn.disabled = true
      try { await action() } finally { btn.disabled = false }
    })
  }
}
submit($('#import-form'),async () => {
  const form = new FormData($('#import-form'))
  const docs = form.getAll('documents').filter(file => file.size)
  form.delete('documents')
  docs.forEach(file => form.append('documents',file))
  form.append('auto_confirm','true')
  const response = await request(base,{method:'POST',body:form})
  $('#import-dialog').close()
  $('#import-form').reset()
  await refresh(response.id)
})
submit(relationForm,async () => {
  const pairs = [...$('#pairs').children].map(row => [...row.querySelectorAll('select')].map(s => s.value))
  const path = base + '/' + current.id + '/relations'
  await post(path + (editId ? '/' + editId : ''),{
    source:picks.source.value,target:picks.target.value,pairs,
    cardinality:picks.cardinality.value,condition:picks.condition.value,status:'active'
  },editId ? 'PUT' : 'POST')
  $('#relation-dialog').close()
  await refresh()
})
const entry = new URLSearchParams(location.search)
work(() => refresh(entry.get('source')))
if (entry.get('new') === '1') $('#new').click()
