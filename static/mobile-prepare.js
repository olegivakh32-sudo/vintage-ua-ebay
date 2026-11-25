(() => {
    const form = document.getElementById('mobile-prepare');
    if (!form) return;
    const button = form.querySelector('button');
    const status = document.getElementById('mobile-prepare-status');
    const labels = {
        photos: 'Визначаю предмет, відбираю фото та обкладинку…',
        research: 'Перевіряю джерела, ціну, вагу й розміри…',
        draft: 'Розраховую доставку й готую чернетку…',
    };

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (button.disabled) return;
        button.disabled = true;
        try {
            let stage = 'photos';
            while (stage) {
                status.textContent = labels[stage];
                const data = new FormData(form);
                data.set('stage', stage);
                const response = await fetch(form.action, {method: 'POST', body: data});
                const result = await response.json();
                if (!response.ok) {
                    status.textContent = result.error || 'Підготовка зупинилася.';
                    if (result.open) {
                        const link = document.createElement('a');
                        link.href = result.open;
                        link.textContent = ' Відкрити результати';
                        status.append(link);
                    }
                    return;
                }
                if (result.open) {
                    window.location.assign(result.open);
                    return;
                }
                stage = result.next;
            }
        } catch {
            status.textContent = 'З’єднання перерване. Повторіть підготовку, щоб використати збережені етапи.';
        } finally {
            button.disabled = false;
        }
    });
})();
