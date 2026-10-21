// One dropdown controller for display presets and native form-backed selections.
// Rendering/data adapters supply items; opening, focus, selection and dismissal live here.
export class CanvasDropdown {
  constructor({trigger, menu, items, value, choose, onToggle = () => {}, refresh = () => {}}) {
    Object.assign(this, {trigger, menu, items, value, choose, onToggle, refresh});
    this.document = trigger.ownerDocument || document;
    this.opened = false;
    trigger.classList.add('canvas-dropdown-trigger');
    menu.classList.add('canvas-dropdown-menu');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.addEventListener('click', event => {
      event.preventDefault();
      if (this.opened) this.close(false); else this.open(event.detail === 0 ? 'first' : 'selected');
    });
    trigger.addEventListener('keydown', event => {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
        event.preventDefault(); event.stopPropagation();
        this.open(event.key === 'ArrowUp' ? 'last' : 'first');
      } else if (event.key === 'Escape' && this.opened) this.dismiss(event, true);
    });
    menu.addEventListener('keydown', event => {
      const items = this.enabled(), index = items.findIndex(item => item.node === event.target);
      const next = {ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: items.length - 1}[event.key];
      if (next !== undefined && items.length) {
        event.preventDefault(); event.stopPropagation();
        items[Math.max(0, Math.min(items.length - 1, next))].node.focus();
      } else if (event.key === 'Escape') this.dismiss(event, true);
      else if (event.key === 'Tab') this.close(true);
      else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault(); event.stopPropagation();
        if (items[index]) this.select(items[index]);
      }
    });
    menu.addEventListener('click', event => {
      const item = this.enabled().find(item => item.node.contains(event.target));
      if (item) { event.preventDefault(); this.select(item); }
    });
    menu.addEventListener('pointermove', event => {
      const item = this.enabled().find(item => item.node.contains(event.target));
      if (item && this.document.activeElement !== item.node) item.node.focus({preventScroll:true});
    });
    // Menus rendered above their drawer must not dismiss the containing drawer on pointerdown.
    menu.addEventListener('pointerdown', event => event.stopPropagation());
    this.document.addEventListener('pointerdown', event => {
      if (this.opened && !menu.contains(event.target) && !trigger.contains(event.target)) this.close(false);
    });
    this.document.addEventListener('focusin', event => {
      if (this.opened && !menu.contains(event.target) && !trigger.contains(event.target)) this.close(false);
    });
    this.document.addEventListener('scroll', () => { if (this.opened) this.position(); }, true);
    if (typeof window !== 'undefined') window.addEventListener('resize', () => { if (this.opened) this.position(); });
  }
  enabled() { return this.items().filter(item => !item.node.disabled); }
  open(start = 'selected') {
    if (this.trigger.disabled || this.opened) return;
    this.refresh();
    const items = this.enabled();
    if (!items.length) return;
    this.opened = true;
    for (const item of this.items()) {
      item.node.classList.add('canvas-dropdown-option');
      item.node.setAttribute('aria-selected', String(item.value === this.value()));
    }
    this.menu.classList.add('open');
    this.trigger.setAttribute('aria-expanded', 'true');
    this.position();
    this.onToggle(true);
    const selected = items.findIndex(item => item.value === this.value());
    const index = start === 'last' ? items.length - 1 : start === 'selected' && selected >= 0 ? selected : 0;
    items[index].node.focus({preventScroll:true});
  }
  close(refocus = false) {
    if (!this.opened) return;
    this.opened = false;
    this.menu.classList.remove('open');
    this.trigger.setAttribute('aria-expanded', 'false');
    this.onToggle(false);
    if (refocus && !this.trigger.disabled) this.trigger.focus();
  }
  dismiss(event, refocus) { event.preventDefault(); event.stopPropagation(); this.close(refocus); }
  select(item) { this.close(true); if (item.value !== this.value()) this.choose(item.value); }
  position() {
    if (!this.trigger.getBoundingClientRect || !this.menu.style) return;
    const rect = this.trigger.getBoundingClientRect(), root = this.document.documentElement;
    const width = root.clientWidth, height = root.clientHeight, gap = 5, edge = 8;
    const below = height - rect.bottom - gap - edge, above = rect.top - gap - edge;
    const up = below < Math.min(this.menu.scrollHeight, 240) && above > below;
    this.menu.style.maxHeight = Math.max(40, up ? above : below) + 'px';
    this.menu.style.minWidth = Math.min(rect.width, width - edge * 2) + 'px';
    this.menu.style.maxWidth = Math.min(Math.max(rect.width, 320), width - edge * 2) + 'px';
    const menuWidth = this.menu.getBoundingClientRect().width;
    this.menu.style.left = Math.max(edge, Math.min(rect.left, width - menuWidth - edge)) + 'px';
    this.menu.style.right = 'auto';
    this.menu.style.top = up ? 'auto' : (rect.bottom + gap) + 'px';
    this.menu.style.bottom = up ? (height - rect.top + gap) + 'px' : 'auto';
    this.menu.style.transformOrigin = up ? 'left bottom' : 'left top';
  }
}

// Keep the original select/value/change contract. Only the popup is replaced.
export function installCanvasSelects(Dropdown) {
  if (typeof MutationObserver !== 'function') return;
  for (const source of document.querySelectorAll('select:not([multiple])')) {
    if (source.canvasDropdown) continue;
    const menu = document.createElement('div');
    menu.id = source.id + '-dropdown';
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', source.getAttribute('aria-label') || source.title || 'Options');
    document.body.appendChild(menu);
    source.setAttribute('aria-controls', menu.id);
    source.setAttribute('aria-haspopup', 'listbox');
    let entries = [];
    const refresh = () => {
      menu.replaceChildren();
      entries = [...source.options].map(option => {
        const node = document.createElement('button');
        node.type = 'button'; node.tabIndex = -1; node.disabled = option.disabled;
        node.setAttribute('role', 'option');
        const check = document.createElement('span'); check.className = 'canvas-dropdown-check';
        check.textContent = '✓'; check.setAttribute('aria-hidden', 'true');
        const label = document.createElement('span'); label.textContent = option.textContent;
        node.appendChild(check); node.appendChild(label); menu.appendChild(node);
        return {node, value:option.value};
      });
    };
    source.canvasDropdown = new Dropdown({trigger:source, menu, items:() => entries, value:() => source.value, refresh,
      choose:value => { source.value = value; source.dispatchEvent(new Event('input', {bubbles:true})); source.dispatchEvent(new Event('change', {bubbles:true})); }});
    // Prevent the OS picker, including touch, while retaining the select's form semantics.
    source.addEventListener('pointerdown', event => { if (!source.disabled) { event.preventDefault(); source.focus(); } });
    source.addEventListener('mousedown', event => event.preventDefault());
    new MutationObserver(() => { if (source.disabled) source.canvasDropdown.close(false); }).observe(source, {attributes:true, attributeFilter:['disabled']});
  }
}

export function dropdownStyles() {
  return `
html:root .canvas-dropdown-trigger{transform:none}
html:root .canvas-dropdown-menu{position:fixed;z-index:30;display:block;width:max-content;max-width:calc(100vw - 16px);margin:0;padding:5px;border:1px solid var(--line-2);border-radius:14px;background:var(--raised);color:var(--text);box-shadow:var(--shadow);font:13px/1.4 var(--font);overflow:auto;overscroll-behavior:contain;backdrop-filter:none;opacity:0;visibility:hidden;transform:translateY(-3px) scale(.98);transform-origin:left top;transition:opacity var(--motion-duration) var(--motion-easing),transform var(--motion-duration) var(--motion-easing),visibility 0s linear var(--motion-duration)}
html:root .canvas-dropdown-menu.open{opacity:1;visibility:visible;transform:none;transition-delay:0s}
html:root .canvas-dropdown-option{display:grid;grid-template-columns:18px minmax(0,1fr) auto;align-items:center;gap:8px;width:100%;min-height:30px;padding:5px 10px;border:0;border-radius:9px;background:transparent;color:var(--text);font:inherit;text-align:left}
html:root .canvas-dropdown-option:focus{outline:none}
html:root .canvas-dropdown-option:focus-visible{background:var(--fill-2)}
html:root .canvas-dropdown-option[aria-selected=true]{font-weight:500}
.canvas-dropdown-check{visibility:hidden;font-size:12px}
[aria-selected=true]>.canvas-dropdown-check{visibility:visible}
@media (hover:hover){html:root .canvas-dropdown-option:not(:disabled):hover{background:var(--fill-2)}}
@media (prefers-reduced-motion:reduce){html:root .canvas-dropdown-menu,html:root .canvas-dropdown-trigger,html:root .canvas-dropdown-option{transition:none;transform:none}}
@media (forced-colors:active){html:root .canvas-dropdown-option:focus-visible{outline:2px solid CanvasText}}
`;
}
