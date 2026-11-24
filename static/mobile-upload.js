(() => {
    const form = document.querySelector('#mobile-upload');
    if (!form || !window.createImageBitmap || !window.FormData || !window.XMLHttpRequest) return;
    const button = form.querySelector('button[type="submit"]');
    const status = document.querySelector('#upload-status');
    const progress = document.querySelector('#upload-progress');

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

    form.addEventListener('submit', async event => {
        const files = [...form.elements.photos.files];
        if (!files.length || button.disabled) return;
        event.preventDefault();
        if (files.length > Number(form.dataset.remaining)) {
            status.textContent = 'У лоті може бути щонайбільше 24 фото.';
            return;
        }
        button.disabled = true;
        progress.hidden = false;
        progress.removeAttribute('value');
        const data = new FormData();
        data.append('hold_csrf', form.elements.hold_csrf.value);
        for (const [index, file] of files.entries()) {
            status.textContent = `Підготовка фото ${index + 1} з ${files.length}…`;
            data.append('photos', await prepare(file));
        }
        const upload = new XMLHttpRequest();
        upload.open('POST', form.action);
        upload.setRequestHeader('Accept', 'application/json');
        upload.responseType = 'json';
        let serverProcessing = false;
        upload.upload.onprogress = event => {
            if (event.lengthComputable) {
                progress.value = Math.round(100 * event.loaded / event.total);
                status.textContent = `Передавання фото… ${progress.value}%`;
            }
        };
        upload.upload.onload = () => {
            serverProcessing = true;
            progress.removeAttribute('value');
            status.textContent = 'Сервер обробляє та зберігає фото…';
        };
        upload.onload = () => {
            if (upload.status === 201) {
                location.assign('/mobile');
                return;
            }
            status.textContent = upload.response?.error || 'Сервер не підтвердив збереження. Відкрийте лот, щоб перевірити фото.';
            if (upload.status < 500) button.disabled = false;
            progress.hidden = true;
        };
        upload.onerror = () => {
            status.textContent = serverProcessing
                ? 'Зв’язок перервано. Відкрийте лот і перевірте, які фото збереглися.'
                : 'Мережевий збій. Перевірте з’єднання й повторіть передавання.';
            if (!serverProcessing) button.disabled = false;
            progress.hidden = true;
        };
        upload.send(data);
    });
})();
