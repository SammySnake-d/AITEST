function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text);
    } else {
        return new Promise((resolve, reject) => {
            const textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position = "fixed";
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            try {
                const successful = document.execCommand('copy');
                document.body.removeChild(textArea);
                if (successful) {
                    resolve();
                } else {
                    reject(new Error('复制失败'));
                }
            } catch (err) {
                document.body.removeChild(textArea);
                reject(err);
            }
        });
    }
}

function copyKeys(type) {
    const keys = Array.from(document.querySelectorAll(`#${type}Keys .key-text`)).map(span => span.textContent.trim());
    const jsonKeys = JSON.stringify(keys);
    
    copyToClipboard(jsonKeys)
        .then(() => {
            showCopyStatus(`已成功复制${type === 'valid' ? '有效' : '无效'}密钥到剪贴板`);
        })
        .catch((err) => {
            console.error('无法复制文本: ', err);
            showCopyStatus('复制失败，请重试');
        });
}

function copyKey(key) {
    copyToClipboard(key)
        .then(() => {
            showCopyStatus(`已成功复制密钥到剪贴板`);
        })
        .catch((err) => {
            console.error('无法复制文本: ', err);
            showCopyStatus('复制失败，请重试');
        });
}

function showCopyStatus(message) {
    const statusElement = document.getElementById('copyStatus');
    statusElement.textContent = message;
    statusElement.style.opacity = 1;
    setTimeout(() => {
        statusElement.style.opacity = 0;
    }, 2000);
}

function scrollToTop() {
    const container = document.querySelector('.container');
    container.scrollTo({
        top: 0,
        behavior: 'smooth'
    });
}

function scrollToBottom() {
    const container = document.querySelector('.container');
    container.scrollTo({
        top: container.scrollHeight,
        behavior: 'smooth'
    });
}

function updateScrollButtons() {
    const container = document.querySelector('.container');
    const scrollButtons = document.querySelector('.scroll-buttons');
    if (container.scrollHeight > container.clientHeight) {
        scrollButtons.style.display = 'flex';
    } else {
        scrollButtons.style.display = 'none';
    }
}

function refreshPage(button) {
    button.classList.add('loading');
    button.disabled = true;
    
    setTimeout(() => {
        window.location.reload();
    }, 300);
}

function toggleSection(header, sectionId) {
    const toggleIcon = header.querySelector('.toggle-icon');
    const content = header.nextElementSibling;
    
    toggleIcon.classList.toggle('collapsed');
    content.classList.toggle('collapsed');
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    // 检查滚动按钮
    updateScrollButtons();

    // 监听展开/折叠事件
    document.querySelectorAll('.key-list h2').forEach(header => {
        header.addEventListener('click', () => {
            setTimeout(updateScrollButtons, 300);
        });
    });

    // 更新版权年份
    const copyrightYear = document.querySelector('.copyright script');
    if (copyrightYear) {
        copyrightYear.textContent = new Date().getFullYear();
    }
});

// Service Worker registration
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/static/service-worker.js')
            .then(registration => {
                console.log('ServiceWorker注册成功:', registration.scope);
            })
            .catch(error => {
                console.log('ServiceWorker注册失败:', error);
            });
    });
}
