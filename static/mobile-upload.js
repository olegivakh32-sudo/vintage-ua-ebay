(() => {
    const form = document.querySelector('#mobile-upload');
    if (!form || !window.createImageBitmap || !window.FormData || !window.XMLHttpRequest) return;
    const button = form.querySelector('button[type="submit"]');
    const status = document.querySelector('#upload-status');
    const progress = document.querySelector('#upload-progress');
    const reload = document.querySelector('#upload-reload');
    const input = form.elements.photos;
    let prepared;

    async function prepare(file) {
        if (file.type !== 'image/jpeg' || file.size < 1024 * 1024) return file;
        let bitmap;
        try {
            bitmap = await createImageBitmap(file, {imageOrientation: 'from-image'});
            const ratio = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height));
            if (ratio === 1) return file;
            const canvas = document.createElement('canvas');
            canvas.width = Math.round(bitmap.width * ratio);
            canvas.height = Math.round(bitmap.height * ratio);
            canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            const jpeg = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92));
            return jpeg && jpeg.size < file.size ? new File([jpeg], file.name, {type: 'image/jpeg'}) : file;
        } catch {
            return file;
        } finally {
            if (bitmap) bitmap.close();
        }
    }

    function offerRetry(message) {
        status.textContent = message;
        progress.hidden = true;
        reload.hidden = false;
        button.textContent = 'Повторити цю партію';
        button.disabled = false;
    }

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (button.disabled) return;
        const files = [...input.files];
        if (!prepared && !files.length) return;
        if (!prepared && files.length > Number(form.dataset.remaining)) {
            status.textContent = 'У лоті може бути щонайбільше 24 фото.';
            return;
        }
        button.disabled = true;
        input.disabled = true;
        progress.hidden = false;
        progress.removeAttribute('value');
        reload.hidden = true;
        if (!prepared) {
            prepared = new FormData();
            prepared.append('hold_csrf', form.elements.hold_csrf.value);
            prepared.append('upload_batch', form.elements.upload_batch.value);
            for (const [index, file] of files.entries()) {
                status.textContent = `Підготовка фото ${index + 1} з ${files.length}…`;
                prepared.append('photos', await prepare(file));
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
                input.disabled = false;
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
