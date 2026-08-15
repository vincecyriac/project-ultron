/**
 * GUI Engine for Project Ultron
 * Handles dynamic canvas displacement and generative widget rendering.
 */

class GUIEngineManager {
  constructor() {
    this.heroStage = null;
    this.widgetStage = null;
    this.widgets = new Map();
  }

  init() {
    this.heroStage = document.getElementById('hero-stage');
    this.widgetStage = document.getElementById('widgetStage');
    console.log("[GUIEngine] Initialized.");
  }

  /**
   * Handle incoming WebSocket payloads for widget management
   */
  handleEvent(payload) {
    if (!payload || !payload.action) return false;

    switch (payload.action) {
      case 'RENDER_WIDGET':
        this.renderWidget(payload);
        return true;
      case 'DESTROY_WIDGET':
        this.destroyWidget(payload.widget_id);
        return true;
      default:
        return false;
    }
  }

  renderWidget(payload) {
    const { widget_id, layout, components } = payload;
    
    let widgetEl = document.getElementById(widget_id);
    if (!widgetEl) {
      widgetEl = document.createElement('div');
      widgetEl.id = widget_id;
      widgetEl.className = 'glass-widget';
      this.widgetStage.appendChild(widgetEl);
    }
    
    // Apply optional layout spans
    if (layout && layout.col_span) {
      widgetEl.style.gridColumn = `span ${layout.col_span}`;
    }

    // Clear existing contents to re-render
    widgetEl.innerHTML = '';

    // Render components (Title-free)
    if (components && Array.isArray(components)) {
      components.forEach(comp => {
        const compEl = this.buildComponent(comp);
        if (compEl) widgetEl.appendChild(compEl);
      });
    }

    this.widgets.set(widget_id, payload);
    this.updateOrbState();
  }

  destroyWidget(widget_id) {
    const widgetEl = document.getElementById(widget_id);
    if (widgetEl) {
      widgetEl.remove();
    }
    this.widgets.delete(widget_id);
    this.updateOrbState();
  }

  buildComponent(comp) {
    // Basic component factory
    const wrapper = document.createElement('div');
    wrapper.className = `widget-comp comp-${comp.type.toLowerCase()}`;

    switch (comp.type) {
      case 'DataChart':
        // A simple placeholder for a data chart (e.g. sparkline)
        wrapper.innerHTML = `<div class="chart-placeholder">
          <svg viewBox="0 0 100 20" preserveAspectRatio="none" style="width:100%; height:100%; stroke: var(--accent); fill: none; stroke-width: 2;">
            <polyline points="${this.generateSparklinePoints(comp.data)}"></polyline>
          </svg>
        </div>`;
        break;
      case 'TextList':
        const ul = document.createElement('ul');
        if (comp.items) {
          comp.items.forEach(item => {
            const li = document.createElement('li');
            li.textContent = item;
            ul.appendChild(li);
          });
        }
        wrapper.appendChild(ul);
        break;
      default:
        wrapper.textContent = JSON.stringify(comp);
    }
    return wrapper;
  }

  generateSparklinePoints(data) {
    if (!data || data.length === 0) return '';
    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    
    return data.map((val, idx) => {
      const x = (idx / (data.length - 1)) * 100;
      const y = 20 - (((val - min) / range) * 20);
      return `${x},${y}`;
    }).join(' ');
  }

  updateOrbState() {
    if (!this.heroStage) return;

    if (this.widgets.size > 0) {
      // Displace orb to top-left
      this.heroStage.classList.add('displaced');
    } else {
      // Return to center
      this.heroStage.classList.remove('displaced');
    }
  }
}

export const GUIEngine = new GUIEngineManager();
