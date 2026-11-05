/* ป็อปอัพยืนยัน/แจ้งเตือนในหน้าเว็บ (แทน confirm()/alert() ของเบราว์เซอร์) + ส่งฟอร์มแบบไม่รีโหลดหน้า
 *
 * ใช้งาน:
 *   <form data-confirm="ข้อความถามยืนยัน">                  → ขึ้นป็อปอัพก่อนส่งฟอร์ม แล้วส่งตามปกติ
 *   <form data-confirm="..." data-ajax="ชื่อตัวจัดการ">      → ส่งแบบ fetch ไม่รีโหลดหน้า แล้วเรียก uiAjaxHandlers[ชื่อ](form, data)
 *   uiConfirm('ข้อความ', {title, okText, cancelText, danger}) → Promise<boolean>
 *   uiAlert('ข้อความ')                                       → ป็อปอัพแจ้งเตือน (ปุ่มตกลงอย่างเดียว)
 *   uiToast('ข้อความ', 'success' | 'error')                  → แจ้งผลมุมจอ
 */
(function () {
    'use strict';
    window.uiAjaxHandlers = window.uiAjaxHandlers || {};

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined) n.textContent = text;
        return n;
    }

    function openDialog(message, opts) {
        opts = opts || {};
        return new Promise(function (resolve) {
            const previous = document.activeElement;
            const backdrop = el('div', 'ui-modal-backdrop');
            const box = el('div', 'ui-modal');
            box.setAttribute('role', opts.alertOnly ? 'alertdialog' : 'dialog');
            box.setAttribute('aria-modal', 'true');
            const title = el('h3', 'ui-modal-title', opts.title || (opts.alertOnly ? 'แจ้งเตือน' : 'ยืนยันการทำรายการ'));
            title.id = 'uiModalTitle';
            box.setAttribute('aria-labelledby', 'uiModalTitle');
            const body = el('p', 'ui-modal-body', message);
            const actions = el('div', 'ui-modal-actions');
            const cancel = el('button', 'ui-btn ui-btn-cancel', opts.cancelText || 'ยกเลิก');
            cancel.type = 'button';
            const ok = el('button', 'ui-btn ' + (opts.danger ? 'ui-btn-danger' : 'ui-btn-primary'),
                          opts.okText || (opts.alertOnly ? 'ตกลง' : 'ยืนยัน'));
            ok.type = 'button';
            if (!opts.alertOnly) actions.appendChild(cancel);
            actions.appendChild(ok);
            box.append(title, body, actions);
            backdrop.appendChild(box);
            document.body.appendChild(backdrop);
            document.body.classList.add('ui-modal-open');
            requestAnimationFrame(function () { backdrop.classList.add('show'); });

            let done = false;
            function close(result) {
                if (done) return;
                done = true;
                document.removeEventListener('keydown', onKey, true);
                backdrop.classList.remove('show');
                document.body.classList.remove('ui-modal-open');
                setTimeout(function () { backdrop.remove(); }, 160);
                if (previous && previous.focus) { try { previous.focus(); } catch (e) { /* ignore */ } }
                resolve(result);
            }
            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); }
                else if (e.key === 'Tab') {            // ให้โฟกัสวนอยู่ในป็อปอัพ
                    const items = opts.alertOnly ? [ok] : [cancel, ok];
                    const i = items.indexOf(document.activeElement);
                    e.preventDefault();
                    items[(i + (e.shiftKey ? items.length - 1 : 1)) % items.length].focus();
                }
            }
            document.addEventListener('keydown', onKey, true);
            ok.addEventListener('click', function () { close(true); });
            cancel.addEventListener('click', function () { close(false); });
            backdrop.addEventListener('mousedown', function (e) { if (e.target === backdrop) close(false); });
            (opts.danger && !opts.alertOnly ? cancel : ok).focus();   // รายการอันตราย: โฟกัสที่ "ยกเลิก" ไว้ก่อน
        });
    }

    window.uiConfirm = function (message, opts) { return openDialog(message, opts); };
    window.uiAlert = function (message, opts) {
        return openDialog(message, Object.assign({ alertOnly: true }, opts || {}));
    };

    window.uiToast = function (message, type) {
        let host = document.getElementById('uiToastHost');
        if (!host) { host = el('div', 'ui-toast-host'); host.id = 'uiToastHost'; document.body.appendChild(host); }
        const t = el('div', 'ui-toast ui-toast-' + (type || 'success'), message);
        t.setAttribute('role', 'status');
        host.appendChild(t);
        requestAnimationFrame(function () { t.classList.add('show'); });
        setTimeout(function () {
            t.classList.remove('show');
            setTimeout(function () { t.remove(); }, 250);
        }, 3500);
    };

    // เอาแถวออกจากตารางแบบเลือนหายไป
    window.uiRemoveRow = function (row) {
        if (!row) return;
        row.classList.add('ui-row-removing');
        setTimeout(function () { row.remove(); }, 260);
    };

    // ดึงหน้าปัจจุบันมาใหม่เงียบๆ แล้วสลับเฉพาะส่วนที่ระบุ (ไม่รีโหลดทั้งหน้า ไม่กระทบช่องที่กำลังกรอกอยู่ส่วนอื่น)
    window.uiRefreshParts = async function (selectors) {
        try {
            const res = await fetch(location.href, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } });
            if (!res.ok) return;
            const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
            selectors.forEach(function (sel) {
                const cur = document.querySelector(sel), fresh = doc.querySelector(sel);
                if (cur && fresh) cur.innerHTML = fresh.innerHTML;
            });
        } catch (e) { /* ถ้าดึงไม่ได้ ข้อมูลจะอัปเดตเมื่อโหลดหน้าครั้งถัดไป */ }
    };

    async function ajaxSubmit(form, submitter) {
        const buttons = form.querySelectorAll('button');
        buttons.forEach(function (b) { b.disabled = true; });
        try {
            const res = await fetch(form.action, {
                method: (form.method || 'POST').toUpperCase(),
                body: new FormData(form),
                credentials: 'same-origin',
                headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
            });
            let data = null;
            try { data = await res.json(); } catch (e) { /* ไม่ใช่ JSON เช่น session หมดอายุ */ }
            if (!data) { location.reload(); return; }
            if (!res.ok || data.ok === false) {
                window.uiToast(data.message || 'ทำรายการไม่สำเร็จ', 'error');
                buttons.forEach(function (b) { b.disabled = false; });
                return;
            }
            const handler = window.uiAjaxHandlers[form.dataset.ajax];
            if (handler) handler(form, data);
            else location.reload();
        } catch (err) {
            window.uiToast('เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ กรุณาลองใหม่อีกครั้ง', 'error');
            buttons.forEach(function (b) { b.disabled = false; });
        }
    }

    // ดักการส่งฟอร์มที่มี data-confirm ทั้งหมด
    document.addEventListener('submit', function (e) {
        const form = e.target;
        if (!form.matches || !form.matches('form[data-confirm]')) return;
        if (form.dataset.confirmed === '1') { delete form.dataset.confirmed; return; }
        e.preventDefault();
        const submitter = e.submitter || null;
        window.uiConfirm(form.dataset.confirm, {
            danger: form.dataset.confirmDanger !== 'false',
            okText: form.dataset.confirmOk || 'ยืนยัน'
        }).then(function (ok) {
            if (!ok) return;
            if (form.dataset.ajax) { ajaxSubmit(form, submitter); return; }
            form.dataset.confirmed = '1';
            if (form.requestSubmit) form.requestSubmit(submitter); else form.submit();
        });
    }, true);
})();

// ----- ส่วนที่ใช้ร่วมกันของหน้าตารางเรียน/ตารางสอน -----
(function () {
    // ตารางที่หมุน 90° บนมือถือ: ปรับขนาดกล่องให้พอดีกับตาราง
    function sizeRotatedTables() {
        document.querySelectorAll('.schedule-rotate-wrapper').forEach(function (wrapper) {
            var table = wrapper.querySelector('table');
            if (!table) return;
            var phone = window.innerWidth <= 640;
            wrapper.style.width = phone ? table.offsetHeight + 'px' : '';
            wrapper.style.height = phone ? table.offsetWidth + 'px' : '';
        });
    }
    ['resize', 'orientationchange', 'load'].forEach(function (ev) { window.addEventListener(ev, sizeRotatedTables); });
    document.addEventListener('DOMContentLoaded', sizeRotatedTables);

    // กล่อง "คาบตอนนี้ / คาบต่อไป": คำนวณที่ฝั่งเซิร์ฟเวอร์ (Python) แล้วดึงมาอัปเดตทุก 30 วินาที
    document.addEventListener('DOMContentLoaded', function () {
        var box = document.getElementById('periodNowBox');
        if (!box) return;
        setInterval(async function () {
            try {
                var res = await fetch('/period-box', { credentials: 'same-origin' });
                if (res.ok) box.innerHTML = await res.text();
            } catch (e) { /* ลองใหม่รอบถัดไป */ }
        }, 30000);
    });
})();
