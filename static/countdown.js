// รูปแบบตัวเลข: วันเต็ม ชั่วโมง และนาที (เช่น 2 วัน 04 ชม. 35 นาที)
(function () {
    const countdowns = Array.from(document.querySelectorAll('[data-due-at]'));
    if (!countdowns.length) return;

    function updateCountdowns() {
        const now = Date.now();
        for (const element of countdowns) {
            const due = Date.parse(element.dataset.dueAt);
            if (Number.isNaN(due)) continue;

            const overdue = now > due;
            const totalMinutes = Math.max(0, Math.floor((due - now) / 60000));
            const days = Math.floor(totalMinutes / 1440);
            const hours = Math.floor((totalMinutes % 1440) / 60);
            const minutes = totalMinutes % 60;
            const value = `${days} วัน ${String(hours).padStart(2, '0')} ชม. ${String(minutes).padStart(2, '0')} นาที`;
            element.classList.toggle('is-overdue', overdue);

            if (element.dataset.layout === 'detail') {
                if (overdue) {
                    element.textContent = 'เลยกำหนดแล้ว';
                } else {
                    element.replaceChildren(
                        document.createTextNode('เหลืออีก'),
                        document.createElement('br'),
                        document.createTextNode(value)
                    );
                }
            } else {
                element.textContent = overdue ? '(เลยกำหนดแล้ว)' : `(เหลืออีก ${value})`;
            }
        }
    }

    updateCountdowns();
    setInterval(updateCountdowns, 1000);
})();
