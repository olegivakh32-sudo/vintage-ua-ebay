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
    const timeouts = {photos: 165_000, research: 330_000, draft: 150_000};
    const expectedNext = {photos: 'research', research: 'draft'};

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
                const controller = new AbortController();
                const deadline = setTimeout(() => controller.abort(), timeouts[stage]);
                let response, result;
                try {
                    response = await fetch(form.action, {method: 'POST', body: data, signal: controller.signal});
                    result = await response.json();
                } catch (error) {
                    if (!controller.signal.aborted) throw error;
                    status.textContent = 'Сервер довго відповідає. Перевірте збережений лот перед повторною спробою.';
                    return;
                } finally {
                    clearTimeout(deadline);
                }
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
                if (!result.next || result.next !== expectedNext[stage]) {
                    status.textContent = 'Підготовка зупинилася. Відкрийте лот, щоб перевірити збережені результати.';
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
