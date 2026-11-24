(() => {
    const form = document.querySelector('#mobile-upload');
    if (!form || !window.FormData || !window.XMLHttpRequest) return;
    const button = form.querySelector('button[type="submit"]');
    const status = document.querySelector('#upload-status');
    const progress = document.querySelector('#upload-progress');
    const reload = document.querySelector('#upload-reload');
    const inputs = [form.elements.photos, form.elements.camera_photo];
    let prepared;

    function offerRetry(message) {
        status.textContent = message;
        progress.hidden = true;
        reload.hidden = false;
        button.textContent = 'Повторити цю партію';
        button.disabled = false;
    }

    form.addEventListener('submit', event => {
        event.preventDefault();
        if (button.disabled) return;
        const files = inputs.flatMap(input => [...input.files]);
        if (!prepared && !files.length) {
            status.textContent = 'Оберіть фото або скористайтеся камерою.';
            return;
        }
        if (!prepared && files.length > Number(form.dataset.remaining)) {
            status.textContent = 'У лоті може бути щонайбільше 24 фото.';
            return;
        }
        if (!prepared && files.some(file => file.size > 10 * 1024 * 1024)) {
            status.textContent = 'Розмір одного фото має бути до 10 МБ.';
            return;
        }
        button.disabled = true;
        inputs.forEach(input => { input.disabled = true; });
        progress.hidden = false;
        progress.removeAttribute('value');
        reload.hidden = true;
        if (!prepared) {
            prepared = new FormData();
            prepared.append('hold_csrf', form.elements.hold_csrf.value);
            prepared.append('upload_batch', form.elements.upload_batch.value);
            for (const file of files) {
                prepared.append('photos', file);
            }
        }
        const upload = new XMLHttpRequest();
        upload.open('POST', form.action);
        upload.setRequestHeader('Accept', 'application/json');
        upload.responseType = 'json';
        upload.timeout = 180_000;
        upload.upload.onprogress = event => {
            if (event.lengthComputable) {
                progress.value = Math.round(100 * event.loaded / event.total);
                status.textContent = `Передавання фото… ${progress.value}%`;
            }
        };
        upload.upload.onload = () => {
            progress.removeAttribute('value');
            status.textContent = 'Сервер обробляє та зберігає фото…';
        };
        upload.onload = () => {
            if (upload.status === 201) {
                location.assign('/mobile');
                return;
            }
            const message = upload.response?.error || 'Сервер не підтвердив збереження.';
            if (upload.status >= 500 || !upload.status) {
                offerRetry(message);
                return;
            }
            status.textContent = message;
            progress.hidden = true;
            reload.hidden = false;
            if (upload.status !== 409 && upload.status !== 403) {
                prepared = undefined;
                button.textContent = 'Завантажити фото';
                button.disabled = false;
                inputs.forEach(input => { input.disabled = false; });
            }
        };
        const lostAnswer = () => offerRetry(
            'Відповідь не надійшла. Повторіть цю партію або перевірте фото в лоті.'
        );
        upload.onerror = lostAnswer;
        upload.ontimeout = lostAnswer;
        upload.send(prepared);
    });
})();
