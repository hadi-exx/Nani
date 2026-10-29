document.addEventListener('DOMContentLoaded', () => {
    const btnFetch = document.getElementById('btnFetch');
    const inputUrl = document.getElementById('mediaUrl');
    const loading = document.getElementById('loading');
    const resultCard = document.getElementById('resultCard');

    const resThumb = document.getElementById('resThumb');
    const imageSlider = document.getElementById('imageSlider');
    const currentImage = document.getElementById('currentImage');
    const prevImg = document.getElementById('prevImg');
    const nextImg = document.getElementById('nextImg');
    const imgCounter = document.getElementById('imgCounter');
    const downloadButtons = document.getElementById('downloadButtons');

    let currentImages = [];
    let currentImgIdx = 0;

    btnFetch.addEventListener('click', async () => {
        const url = inputUrl.value.trim();
        if (!url) return alert('Masukkan URL terlebih dahulu!');

        loading.classList.remove('hidden');
        resultCard.classList.add('hidden');
        resThumb.classList.add('hidden');
        imageSlider.classList.add('hidden');

        try {
            const res = await fetch('/downloader/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url })
            });

            const data = await res.json();
            loading.classList.add('hidden');

            if (data.error) {
                alert(data.error);
                return;
            }

            document.getElementById('resPlatform').textContent = data.platform;
            document.getElementById('resTitle').textContent = data.title;
            document.getElementById('resUploader').textContent = '@' + data.uploader;

            downloadButtons.innerHTML = '';

            // Format nama file berdasarkan judul dan uploader
            const safeTitle = (data.uploader + '_' + data.title).replace(/[^a-zA-Z0-9_\-]/g, '_').substring(0, 40);

            if (data.is_image && data.images.length > 0) {
                currentImages = data.images;
                currentImgIdx = 0;
                updateSlider();
                imageSlider.classList.remove('hidden');

                currentImages.forEach((imgUrl, index) => {
                    const btn = document.createElement('a');
                    btn.className = 'btn download-btn';
                    const fileName = `${safeTitle}_foto_${index+1}.jpg`;
                    btn.href = `/downloader/download_file?url=${encodeURIComponent(imgUrl)}&is_image=true&filename=${encodeURIComponent(fileName)}`;
                    btn.innerHTML = `<i data-lucide="image"></i> Download Foto ${index + 1}`;
                    downloadButtons.appendChild(btn);
                });

            } else {
                resThumb.src = data.thumbnail || 'https://via.placeholder.com/400x250';
                resThumb.classList.remove('hidden');

                const btn = document.createElement('a');
                btn.className = 'btn download-btn';
                const fileName = `${safeTitle}.mp4`;
                btn.href = `/downloader/download_file?raw_url=${encodeURIComponent(data.raw_url)}&url=${encodeURIComponent(data.download_url)}&filename=${encodeURIComponent(fileName)}`;
                btn.innerHTML = `<i data-lucide="download"></i> Download Sekarang`;
                downloadButtons.appendChild(btn);
            }

            resultCard.classList.remove('hidden');
            lucide.createIcons();

        } catch (err) {
            loading.classList.add('hidden');
            alert('Terjadi kesalahan koneksi server!');
        }
    });

    function updateSlider() {
        currentImage.src = currentImages[currentImgIdx];
        imgCounter.textContent = `${currentImgIdx + 1} / ${currentImages.length}`;
    }

    prevImg.addEventListener('click', () => {
        if (currentImgIdx > 0) {
            currentImgIdx--;
            updateSlider();
        }
    });

    nextImg.addEventListener('click', () => {
        if (currentImgIdx < currentImages.length - 1) {
            currentImgIdx++;
            updateSlider();
        }
    });
});
