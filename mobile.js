/* Phone navigation and keyboard layout. Native DSH owns sessions, uploads, workspaces, and answers. */
(() => {
  if (window.__dshAttachMobileV2) return;
  window.__dshAttachMobileV2 = true;
  const root = document.documentElement;
  const mq = matchMedia('(max-width: 760px)');
  const selectors = {
    frame: '._6Qf49G_frame',
    sidebar: '._6Qf49G_sidebarCol',
    scroll: '.ST7X_W_scrollBody',
    editor: '.yhfFVG_input[contenteditable="true"]',
    fileInput: '.yhfFVG_root input[type="file"][multiple]',
    fileAdd: '.yhfFVG_add'
  };
  let frame, bar, backdrop, menu, title, subtitle, doneButton, menuButton, workspaceButton, attachButton;
  let scheduled = false, composing = false, keyboardOpen = false, lastHeight = 0;
  let menuOpen = false, previousDrawer = false, restoreFocus = null, pendingSession = '';
  const meta = document.querySelector('meta[name="viewport"]');
  if (meta) meta.content = 'width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content';
  const $ = s => document.querySelector(s);
  const editable = e => !!e && (e.isContentEditable || /^(TEXTAREA|INPUT)$/.test(e.tagName));
  const zh = true;
  const ui = zh ? {
    drawer: '切换对话列表', workspace: '项目/工作区', attach: '图片/文件', newSession: '新建对话', done: '收起键盘', more: '更多功能',
    menu: '更多功能', placeholder: '发消息…', rightbar: '文件与终端', agents: '智能体团队',
    sessionActions: '会话操作', plugins: '插件', automations: '自动化任务'
  } : {
    drawer: 'Conversations', workspace: 'Project / workspace', attach: 'Images / files', newSession: 'New conversation', done: 'Dismiss keyboard', more: 'More',
    menu: 'More', placeholder: 'Message…', rightbar: 'Files and terminal', agents: 'Agent team',
    sessionActions: 'Session actions', plugins: 'Plugins', automations: 'Automations'
  };
  const labels = {
    collapseSidebar: [/^收起侧边栏$/i, /^collapse\s+sidebar$/i, /^hide\s+sidebar$/i],
    expandSidebar: [/^打开侧边栏$/i, /^open\s+sidebar$/i, /^show\s+sidebar$/i],
    newSession: [/^新会话$/i, /^新建会话$/i, /^new\s+(?:session|conversation)$/i],
    rightbar: [/^(?:打开|关闭)右侧边栏$/i, /^(?:open|close|show|hide)\s+(?:right\s+)?sidebar$/i],
    rightbarOpen: [/^打开右侧边栏$/i, /^(?:open|show)\s+(?:right\s+)?sidebar$/i],
    rightbarClose: [/^(?:关闭|收起)右侧边栏$/i, /^(?:close|hide|collapse)\s+(?:right\s+)?sidebar$/i],
    agents: [/^智能体团队$/i, /^agent\s+team$/i, /^agent\s+teams$/i],
    sessionActions: [/^更多操作$/i, /^session\s+actions$/i, /^more\s+actions$/i],
    plugins: [/^插件$/i, /^plugins?$/i],
    automations: [/^自动化任务$/i, /^automations?$/i, /^scheduled\s+tasks?$/i],
    workspaceViewOptions: [/^视图选项$/i, /^view\s+options$/i]
  };
  function matchesLabel(button, patterns) {
    const values = [button.getAttribute('aria-label'), button.getAttribute('title'), button.textContent].filter(Boolean);
    return patterns.some(pattern => values.some(value => pattern.test(value.trim())));
  }
  function originalButton(action) {
    const patterns = labels[action] || [];
    return Array.from(document.querySelectorAll('button[aria-label], [role="button"][aria-label]'))
      .find(button => !button.closest('[data-dsh-phone-ui]') && matchesLabel(button, patterns)) || null;
  }
  const clickOriginal = action => {
    const button = originalButton(action);
    if (button && !button.disabled) { button.click(); return true; }
    return false;
  };
  function attrIsTrue(element, name) {
    const value = element.getAttribute(name);
    return value === '' || /^(true|yes|1)$/i.test(value || '');
  }
  function sidebarCollapsed() {
    // DSH removes this attribute when open. Our own drawer CSS hides the
    // column until this state is mirrored; reading its style creates a loop.
    if (frame) return frame.hasAttribute('data-sidebar-collapsed') && attrIsTrue(frame, 'data-sidebar-collapsed');
    return !originalButton('collapseSidebar');
  }
  function rightbarCollapsed() {
    if (frame) return frame.hasAttribute('data-rightbar-collapsed') && attrIsTrue(frame, 'data-rightbar-collapsed');
    if (originalButton('rightbarClose')) return false;
    if (originalButton('rightbarOpen')) return true;
    return true;
  }
  function setVar(name, value) { if (root.style.getPropertyValue(name) !== value) root.style.setProperty(name, value); }
  function text(el, value) { if (el.textContent !== value) el.textContent = value; }
  function closeDrawer() { clickOriginal('collapseSidebar'); }
  function nativeFileInput() {
    const input = $(selectors.fileInput);
    const add = input?.closest('.yhfFVG_root')?.querySelector(selectors.fileAdd);
    if (!input || input.disabled || !add || add.disabled || add.closest('[hidden]')) return null;
    return input;
  }
  function openNativeFilePicker() {
    const input = nativeFileInput();
    if (!input) return false;
    input.click();
    return true;
  }
  function nativeWorkspaceViewOptions() {
    const buttons = document.querySelectorAll(selectors.sidebar + ' button[aria-label], ' + selectors.sidebar + ' [role="button"][aria-label]');
    return Array.from(buttons)
      .find(button => matchesLabel(button, labels.workspaceViewOptions)) || null;
  }
  function openWorkspaceDrawer() {
    setMenu(false);
    if (sidebarCollapsed()) clickOriginal('expandSidebar');
    // The native Workspace browser owns grouping and ordering. Give it focus after
    // the one React layout pass without clicking or changing the current view.
    requestAnimationFrame(() => {
      const viewOptions = nativeWorkspaceViewOptions();
      if (viewOptions) viewOptions.focus({preventScroll: true});
      schedule();
    });
  }
  function setMenu(open) {
    menuOpen = open;
    root.classList.toggle('dsh-phone-menu-open', open);
    if (menuButton) menuButton.setAttribute('aria-expanded', String(open));
    if (open) {
      closeDrawer();
      menu.replaceChildren();
      const actions = [
        [ui.workspace, openWorkspaceDrawer],
        [ui.attach, openNativeFilePicker],
        [ui.rightbar, () => clickOriginal('rightbar')],
        [ui.agents, () => clickOriginal('agents')],
        [ui.sessionActions, () => clickOriginal('sessionActions')],
        [ui.plugins, () => clickOriginal('plugins')],
        [ui.automations, () => clickOriginal('automations')],
        ['用量与状态', () => root.classList.toggle('dsh-phone-stats')],
        [ui.done, () => { if (editable(document.activeElement)) document.activeElement.blur(); }]
      ];
      for (const [label, action] of actions) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        if (label === ui.attach) button.disabled = nativeFileInput() === null;
        button.addEventListener('click', () => { setMenu(false); action(); schedule(); });
        menu.appendChild(button);
      }
    }
  }
  function icon(path) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + path + '</svg>'; }
  function makeButton(label, svg, action) {
    const b = document.createElement('button'); b.type = 'button'; b.setAttribute('aria-label', label); b.innerHTML = svg; b.addEventListener('click', action); return b;
  }
  function createShell() {
    bar = document.createElement('nav'); bar.className = 'dsh-phone-bar'; bar.dataset.dshPhoneUi = 'true'; bar.setAttribute('aria-label', '手机导航');
    const drawerButton = makeButton(ui.drawer, icon('<path d="M4 6h16M4 12h16M4 18h16"/>'), () => { setMenu(false); if (!clickOriginal('collapseSidebar')) clickOriginal('expandSidebar'); schedule(); });
    bar.appendChild(drawerButton);
    const heading = document.createElement('div'); heading.className = 'dsh-phone-heading';
    title = document.createElement('div'); title.className = 'dsh-phone-title';
    subtitle = document.createElement('div'); subtitle.className = 'dsh-phone-subtitle'; text(subtitle, 'DeepSeek Harness');
    heading.append(title, subtitle); bar.appendChild(heading);
    workspaceButton = makeButton(ui.workspace, icon('<path d="M3 7h6l2 2h10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M3 9h18"/>'), openWorkspaceDrawer);
    workspaceButton.classList.add('dsh-phone-workspace-button'); bar.appendChild(workspaceButton);
    attachButton = makeButton(ui.attach, icon('<path d="m8.5 12.5 5.9-5.9a3.4 3.4 0 0 1 4.8 4.8l-7.6 7.6a4.8 4.8 0 0 1-6.8-6.8l7-7"/>'), openNativeFilePicker);
    attachButton.classList.add('dsh-phone-attach-button'); bar.appendChild(attachButton);
    bar.appendChild(makeButton(ui.newSession, icon('<path d="M12 5v14M5 12h14"/>'), () => {
      setMenu(false);
      const started = clickOriginal('newSession');
      if (started) setTimeout(closeDrawer, 140);
      else closeDrawer();
      schedule();
    }));
    doneButton = makeButton(ui.done, icon('<path d="m5 12 4 4L19 6"/>'), () => { if (editable(document.activeElement)) document.activeElement.blur(); }); doneButton.hidden = true; bar.appendChild(doneButton);
    menuButton = makeButton(ui.more, icon('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'), () => setMenu(!menuOpen));
    menuButton.setAttribute('aria-expanded', 'false'); bar.appendChild(menuButton);
    backdrop = document.createElement('div'); backdrop.className = 'dsh-phone-backdrop'; backdrop.dataset.dshPhoneUi = 'true'; backdrop.setAttribute('aria-hidden', 'true');
    backdrop.addEventListener('click', () => { setMenu(false); closeDrawer(); });
    menu = document.createElement('div'); menu.className = 'dsh-phone-menu'; menu.dataset.dshPhoneUi = 'true'; menu.setAttribute('aria-label', ui.menu);
    document.body.append(bar, backdrop, menu);
    // Keep a tap on Send from closing the software keyboard before the click.
    document.addEventListener('pointerdown', event => {
      if (!mq.matches || composing || !(event.target instanceof Element) || !event.target.closest('.yhfFVG_primary')) return;
      const editor = $(selectors.editor);
      if (editor && (document.activeElement === editor || editor.contains(document.activeElement))) { restoreFocus = editor; event.preventDefault(); }
    }, true);
    document.addEventListener('click', event => {
      if (!mq.matches) return;
      if (event.target.closest('.yhfFVG_primary') && restoreFocus) {
        const editor = restoreFocus; restoreFocus = null;
        requestAnimationFrame(() => { if (editor.isConnected) editor.focus({preventScroll: true}); });
      }
      const row = event.target.closest('[role="treeitem"]');
      if (row && !row.hasAttribute('aria-expanded') && row.closest(selectors.sidebar)) {
        // Do not unmount the async session picker before its resume completes.
        pendingSession = row.textContent.trim();
        schedule();
      }
    }, true);
  }
  function applyViewport() {
    if (!mq.matches || !frame) return;
    const vv = window.visualViewport;
    // Do not fight pinch zoom; Safari pans the visual viewport while zoomed.
    if (vv && Math.abs(vv.scale - 1) > .05) return;
    const focused = editable(document.activeElement);
    const visualHeight = Math.round(vv ? vv.height : innerHeight);
    const layoutHeight = Math.round(Math.max(innerHeight, document.documentElement.clientHeight || 0, visualHeight));
    const offsetTop = Math.max(0, Math.round(vv ? vv.offsetTop : 0));
    const height = Math.max(1, Math.min(visualHeight, layoutHeight - offsetTop));
    const delta = layoutHeight - (height + offsetTop);
    if (keyboardOpen ? delta < 40 : delta > 100) keyboardOpen = delta > (keyboardOpen ? 40 : 100);
    const scroll = $(selectors.scroll);
    const follow = scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
    setVar('--dsh-phone-top', offsetTop + 'px');
    setVar('--dsh-phone-height', height + 'px');
    root.classList.toggle('dsh-phone-keyboard', keyboardOpen);
    if (doneButton) doneButton.hidden = !focused;
    requestAnimationFrame(() => {
      if (scroll && scroll.isConnected) {
        if (lastHeight && Math.abs(lastHeight - height) > 60 && follow) scroll.scrollTop = scroll.scrollHeight;
      }
      lastHeight = height;
    });
  }
  function sync() {
    scheduled = false;
    if (!mq.matches) {
      root.classList.remove('dsh-phone-drawer', 'dsh-phone-tools', 'dsh-phone-keyboard', 'dsh-phone-menu-open', 'dsh-phone-stats');
      return;
    }
    frame = $(selectors.frame);
    if (!frame) return;
    if (!bar) createShell();
    const drawer = !sidebarCollapsed();
    root.classList.toggle('dsh-phone-drawer', drawer);
    root.classList.toggle('dsh-phone-tools', !rightbarCollapsed());
    if (drawer !== previousDrawer) {
      bar.querySelector('button').setAttribute('aria-expanded', String(drawer)); previousDrawer = drawer;
    }
    if (attachButton) {
      const available = nativeFileInput() !== null;
      attachButton.disabled = !available;
      attachButton.setAttribute('aria-disabled', String(!available));
      if (menuOpen) {
        const menuAttach = Array.from(menu.children).find(button => button.textContent === ui.attach);
        if (menuAttach) menuAttach.disabled = !available;
      }
    }
    const crumb = $('.ST7X_W_crumbCurrent');
    const currentTitle = crumb ? crumb.textContent.trim() : '';
    text(title, currentTitle || '新对话');
    if (pendingSession && currentTitle && pendingSession.includes(currentTitle)) { pendingSession = ''; closeDrawer(); }
    for (const editor of document.querySelectorAll(selectors.editor)) {
      if (editor.getAttribute('enterkeyhint') !== 'send') editor.setAttribute('enterkeyhint', 'send');
      if (editor.getAttribute('inputmode') !== 'text') editor.setAttribute('inputmode', 'text');
    }
    for (const placeholder of document.querySelectorAll('.yhfFVG_placeholder')) text(placeholder, ui.placeholder);
    applyViewport();
  }
  function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(sync); } }
  const observer = new MutationObserver(records => {
    // Layout/nav changes only: ignore our own shell and streaming text. The
    // native question and attachment roots are included so the file button
    // follows Session availability without observing every React mutation.
    const nativeRoots = 'header, ' + selectors.sidebar + ', .ST7X_W_root, .yhfFVG_root, [data-question-key], [data-plan-review-key]';
    const addedRoots = selectors.frame + ', .ST7X_W_root, .yhfFVG_root, [data-question-key], [data-plan-review-key], .dVdiKa_rail';
    if (records.some(record => {
      if (record.target instanceof Element && record.target.closest('[data-dsh-phone-ui]')) return false;
      if (record.type === 'attributes') return record.target instanceof Element && (record.target.matches(selectors.frame) || record.target.closest(nativeRoots));
      if (!(record.target instanceof Element) || record.target.closest('[data-dsh-phone-ui]')) return false;
      if (record.target.closest(nativeRoots)) return true;
      return Array.from(record.addedNodes).some(node => node instanceof Element && !node.closest('[data-dsh-phone-ui]') && (node.matches(addedRoots) || node.querySelector(addedRoots)));
    })) schedule();
  });
  observer.observe(document.body, {subtree:true, childList:true, attributes:true, attributeFilter:['data-sidebar-collapsed','data-rightbar-collapsed','data-phase','aria-label']});
  window.addEventListener('resize', schedule, {passive:true});
  window.addEventListener('orientationchange', () => { keyboardOpen = false; schedule(); }, {passive:true});
  window.addEventListener('pageshow', schedule);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
  if (window.visualViewport) { visualViewport.addEventListener('resize', schedule, {passive:true}); visualViewport.addEventListener('scroll', schedule, {passive:true}); }
  document.addEventListener('focusin', schedule, true);
  document.addEventListener('focusout', schedule, true);
  document.addEventListener('compositionstart', () => { composing = true; }, true);
  document.addEventListener('compositionend', () => { composing = false; }, true);
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && mq.matches) { setMenu(false); closeDrawer(); } }, true);
  mq.addEventListener('change', schedule);
  schedule();
})();
