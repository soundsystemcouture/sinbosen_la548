// Gradio HTML controller: the overlay never adds traces to the exported figure.
let host = document.querySelector('#response-main');
host.style.position = 'relative';
const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
svg.classList.add('eq-handle-overlay');
Object.assign(svg.style, {position: 'absolute', inset: '0', zIndex: '10', pointerEvents: 'none', overflow: 'hidden'});
host.appendChild(svg);
let message = element.querySelector('.drag-message');
let metadata = {}, plot = null, drag = null, timer = null, frame = null;
let busy = false, lastPreview = 0, plotWork = Promise.resolve();
let drawingKey = '';
function readMetadata() {
    message = element.querySelector('.drag-message');
    try { metadata = JSON.parse(props.value || '{}'); } catch { metadata = {}; }
    svg.dataset.active = metadata.active || '';
    if (drag && (metadata.choice !== drag.choice || metadata.revision !== drag.revision)) abandon(false);
    if (drag && drag.context) {
        if (metadata.active !== drag.context || metadata.choice !== drag.choice) {
            abandon(false);
        }
    } else if (drag && metadata.request === drag.request && metadata.active) {
        drag.context = metadata.active;
        drag.session = metadata.session;
        message.textContent = 'Previewing frequency — release to stage, then Apply. Live is off.';
        if (drag.ending) end(drag.ending);
        else schedulePreview();
    } else if (drag && metadata.request === drag.request && !metadata.active) {
        abandon(false);
        message.textContent = 'Graph edit unavailable. Check the device status and try again.';
    }
    redraw();
}
function getPlot() {
    // Gradio can retain a collapsed previous plot container while replacing its output.
    const currentHost = [...document.querySelectorAll('#response-main')].filter(node =>
        node.querySelector('.js-plotly-plot')).sort((a, b) =>
        b.getBoundingClientRect().height - a.getBoundingClientRect().height)[0];
    if (currentHost && currentHost !== host) {
        resize.unobserve(host); observer.disconnect();
        host = currentHost; host.style.position = 'relative'; host.appendChild(svg);
        resize.observe(host); observer.observe(host, {childList: true, subtree: true});
        drawingKey = '';
    }
    const next = host.querySelector('.js-plotly-plot');
    if (next !== plot) {
        plot = next;
        // Gradio's Plots.resize clears layout.height. Give the autosized graph a
        // definite containing height before any custom react can restore that layout.
        if (plot) plot.style.height = '640px';
        if (plot && plot.on) {
            plot.on('plotly_afterplot', redraw);
            plot.on('plotly_relayout', redraw);
        }
    }
    return plot && plot._fullLayout?.xaxis && plot._fullLayout?.yaxis ? plot : null;
}
function redraw() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
        frame = null;
        const gd = getPlot();
        if (!gd) return;
        const bounds = host.getBoundingClientRect(), chart = gd.getBoundingClientRect();
        svg.setAttribute('width', bounds.width);
        svg.setAttribute('height', Math.max(bounds.height, chart.top - bounds.top + chart.height));
        const x = gd._fullLayout.xaxis, y = gd._fullLayout.yaxis;
        const bands = [...(metadata.handles || [])].sort((a, b) => Number(a.selected) - Number(b.selected));
        const nextKey = JSON.stringify([bands, drag?.frequency, drag?.band, x.range, y.range,
            x._offset, x._length, y._offset, y._length, bounds.width, bounds.height,
            chart.left - bounds.left, chart.top - bounds.top]);
        if (nextKey === drawingKey && svg.childElementCount) return;
        drawingKey = nextKey;
        svg.replaceChildren();
        for (const band of bands) {
            const freq = drag && band.band === drag.band ? drag.frequency : band.frequency;
            const log = Math.log10(freq);
            if (log < Math.min(...x.range) || log > Math.max(...x.range)) continue;
            if (band.gain < Math.min(...y.range) || band.gain > Math.max(...y.range)) continue;
            const px = chart.left - bounds.left + x._offset + x.l2p(log);
            const py = chart.top - bounds.top + y._offset + y.l2p(band.gain);
            const group = document.createElementNS(svg.namespaceURI, 'g');
            group.setAttribute('transform', `translate(${px},${py})`);
            group.setAttribute('data-band', band.band);
            group.setAttribute('data-frequency', freq);
            group.setAttribute('role', 'button');
            group.setAttribute('tabindex', '0');
            const label = `Band ${band.band + 1} · ${band.type} · ${freq} Hz · ${band.gain} dB · Q ${band.q}`;
            group.setAttribute('aria-label', label);
            group.style.pointerEvents = 'all';
            group.style.cursor = 'ew-resize';
            const title = document.createElementNS(svg.namespaceURI, 'title');
            title.textContent = `${label}. ${band.enabled ? 'Enabled' : 'Disabled'}. Height is band gain; drag horizontally to change frequency. Gain and Q are edited below.`;
            const shape = document.createElementNS(svg.namespaceURI, band.allpass ? 'path' : 'circle');
            if (band.allpass) shape.setAttribute('d', 'M 0 -9 L 9 0 L 0 9 L -9 0 Z');
            else shape.setAttribute('r', '8');
            shape.setAttribute('fill', band.enabled ? (band.allpass ? '#8b5cf6' : '#5368ed') : '#ffffff');
            shape.setAttribute('stroke', band.selected ? '#111827' : '#5368ed');
            shape.setAttribute('stroke-width', band.selected ? '3' : '2');
            group.append(title, shape);
            if (band.selected) {
                const text = document.createElementNS(svg.namespaceURI, 'text');
                text.setAttribute('x', '13'); text.setAttribute('y', '-12');
                text.setAttribute('fill', '#111827'); text.style.font = 'bold 12px system-ui';
                text.style.paintOrder = 'stroke'; text.setAttribute('stroke', '#ffffff');
                text.setAttribute('stroke-width', '3');
                text.textContent = `${freq} Hz`;
                group.append(text);
            }
            group.addEventListener('pointerdown', event => start(event, band));
            group.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    trigger('drag_start', {action: 'select', band: band.band});
                }
            });
            svg.appendChild(group);
        }
    });
}
function start(event, band) {
    if (event.button !== 0 || drag || !getPlot()) return;
    event.preventDefault(); event.stopPropagation();
    svg.setPointerCapture(event.pointerId);
    const gd = plot;
    delete svg.dataset.previewSequence;
    drag = {band: band.band, frequency: band.frequency, choice: metadata.choice, revision: metadata.revision,
        request: crypto.randomUUID(), sequence: 0, wanted: 0, moved: false,
        original: {data: structuredClone(gd.data), layout: structuredClone(gd.layout)},
        pointer: event.pointerId, startX: event.clientX};
    drag.original.layout.height = 640;
}
function move(event) {
    if (!drag || event.pointerId !== drag.pointer || drag.ending) return;
    const gd = getPlot(); if (!gd) return;
    if (Math.abs(event.clientX - drag.startX) < 2 && !drag.moved) return;
    event.preventDefault();
    if (!drag.moved) {
        message.textContent = 'Preparing graph edit — pausing Live…';
        trigger('drag_start', {action: 'begin', band: drag.band, request: drag.request,
            choice: drag.choice, revision: drag.revision});
    }
    drag.moved = true;
    const x = gd._fullLayout.xaxis, rect = gd.getBoundingClientRect();
    const pixel = Math.min(x._length, Math.max(0, event.clientX - rect.left - x._offset));
    drag.frequency = Math.min(20000, Math.max(10, Math.round(10 ** x.p2l(pixel))));
    drag.wanted++;
    redraw(); schedulePreview();
}
function schedulePreview() {
    if (!drag?.context || busy || timer || drag.ending || !drag.moved) return;
    timer = setTimeout(preview, Math.max(0, 100 - (performance.now() - lastPreview)));
}
async function preview() {
    timer = null;
    if (!drag?.context || drag.ending) return;
    const current = drag, wanted = current.wanted;
    const sequence = ++current.sequence;
    busy = true; lastPreview = performance.now();
    try {
        const result = await server.preview_drag({session: current.session, id: current.context,
            frequency: current.frequency, sequence});
        if (drag === current && !current.ending && result.sequence === sequence && result.figure) {
            plotWork = plotWork.then(async () => {
                if (drag !== current || current.ending) return;
                const figure = JSON.parse(result.figure);
                // Preserve pan/zoom and legend visibility through Plotly's uirevision.
                await window.Plotly.react(getPlot(), figure.data, figure.layout, {responsive: true});
                if (drag === current && !current.ending) svg.dataset.previewSequence = sequence;
                redraw();
            });
            await plotWork;
        } else if (drag === current && !current.ending && result.error) {
            message.textContent = result.error;
            abandon(true);
        }
    } catch (error) {
        if (drag === current && !current.ending) {
            abandon(true);
            message.textContent = 'Preview unavailable. Use the frequency editor below.';
        }
    } finally {
        busy = false;
        if (drag === current && !current.ending && current.wanted !== wanted) schedulePreview();
    }
}
function end(action) {
    if (!drag) return;
    const current = drag;
    if (svg.hasPointerCapture(current.pointer)) svg.releasePointerCapture(current.pointer);
    if (!current.moved) {
        drag = null;
        if (action !== 'cancel') trigger('drag_start', {action: 'select', band: current.band});
        message.textContent = action === 'cancel' ? 'Graph edit cancelled.' : 'Band selected.';
        redraw(); return;
    }
    current.ending = action;
    if (timer) { clearTimeout(timer); timer = null; }
    if (!current.context) return; // Wait for begin acknowledgement, even for very quick clicks.
    drag = null;
    // The backend restores its authoritative full-resolution figure, after any in-flight react.
    plotWork = plotWork.catch(() => {}).then(() => {
        trigger('drag_end', {action: action === 'cancel' || !current.moved ? 'cancel' : 'finish',
            id: current.context, band: current.band, frequency: current.frequency});
    });
    message.textContent = action === 'cancel' ? 'Graph edit cancelled. Live remains off.' :
        current.moved ? 'Frequency staged locally — use Apply changes.' : 'Band selected. Live remains off.';
    redraw();
}
function abandon(restore) {
    if (!drag) return;
    const current = drag;
    if (restore && current.context) { end('cancel'); return; }
    if (svg.hasPointerCapture(current.pointer)) svg.releasePointerCapture(current.pointer);
    drag = null;
    if (timer) {clearTimeout(timer); timer = null;}
    // A band-only selection does not redraw the curves. Restore the draft curve in that case.
    if (metadata.choice === current.choice && metadata.revision === current.revision &&
        metadata.selected !== current.band) {
        plotWork = plotWork.catch(() => {}).then(async () => {
            const gd = getPlot();
            if (gd && !drag) await window.Plotly.react(gd, current.original.data, current.original.layout,
                                                     {responsive: true});
        });
    }
    message.textContent = 'Graph edit ended because the draft or selection changed.';
    redraw();
}
function up(event) { if (drag && event.pointerId === drag.pointer) end('finish'); }
function cancel(event) { if (drag && event.pointerId === drag.pointer) end('cancel'); }
function key(event) { if (drag && event.key === 'Escape') {event.preventDefault(); end('cancel');} }
window.addEventListener('pointermove', move, {passive: false});
window.addEventListener('pointerup', up);
window.addEventListener('pointercancel', cancel);
window.addEventListener('keydown', key);
const resize = new ResizeObserver(redraw); resize.observe(host);
const observer = new MutationObserver(records => {
    if (records.some(record => record.target !== svg && !svg.contains(record.target))) redraw();
});
observer.observe(host, {childList: true, subtree: true});
watch('value', readMetadata);
readMetadata();
// Remove handlers when Gradio unmounts this controller.
const removal = new MutationObserver(() => {
    if (element.isConnected) return;
    window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', cancel); window.removeEventListener('keydown', key);
    resize.disconnect(); observer.disconnect(); removal.disconnect(); svg.remove();
    if (timer) clearTimeout(timer);
    if (frame) cancelAnimationFrame(frame);
});
removal.observe(document.body, {childList: true, subtree: true});
